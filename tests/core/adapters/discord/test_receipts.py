import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from src.core.adapters.discord.brand import ICONS
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.discord.receipts import DiscordReceipts, receipt_view
from src.core.adapters.sqlite.db import open_db, transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.actions import ActionGroup, ActionRegistry, ActionSpec, Query
from src.core.app.ports import Source
from tests.support.discord import Client, Guild, member
from tests.support.testing import interaction, text

G, CHANNEL, USER, BOT, SENT = 5, 10, 1, 99, 3000
DONE = {"ok": True, "code": "done", "text": "管理操作已完成。"}


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = open_db(":memory:")
        self.addCleanup(self.db.close)
        self.operations = OperationStore(self.db)
        self.guild = Guild(G).add(member(USER))
        self.guild.me = SimpleNamespace(id=BOT)
        self.posted = SimpleNamespace(id=SENT, edit=AsyncMock())
        self.permissions = discord.Permissions(view_channel=True, send_messages=True)
        self.channel = SimpleNamespace(id=CHANNEL, guild=self.guild, send=AsyncMock(return_value=self.posted),
                                       permissions_for=lambda _member: self.permissions)
        self.guild.channels[CHANNEL] = self.channel
        self.resent = AsyncMock(return_value=SimpleNamespace(id=SENT + 1))
        self.client = Client(self.guild, user_id=BOT)
        self.client.get_channel = lambda channel_id: self.channel if channel_id == CHANNEL else None
        self.client.get_partial_messageable = lambda channel_id: SimpleNamespace(
            send=self.resent, get_partial_message=lambda message_id: SimpleNamespace(id=message_id, edit=AsyncMock()),
        )
        self.render = AsyncMock(return_value={"view": receipt_view([DONE]), "attachments": []})
        registry = ActionRegistry()
        registry.register(ActionGroup("info", (
            ActionSpec("help", {}, admin=False, effect=Query("功能說明", self.render)),
            ActionSpec("admin_status", {}, admin=True, effect=Query("管理狀態", self.render)),
        )))
        self.registry = registry
        self.live = LiveGuild(self.client, G, lambda: False)
        self.receipts = DiscordReceipts(self.operations, self.live, self.client, registry)
        self.source = Source(1000, G, CHANNEL, USER, "", frozenset(), frozenset())

    def query(self, name: str = "help") -> dict:
        token = self.receipts.private(self.source, self.registry.spec(name), {})
        return {"ok": True, "code": "done", "text": "已查詢功能說明。", "private_ref": token}

    def effect(self, request_id: str = "req"):
        return self.operations.get_effect(f"request:{request_id}:receipt")

    def click(self, token_owner: int = USER) -> MagicMock:
        clicked = interaction()
        clicked.user.id, clicked.guild_id, clicked.channel_id, clicked.message.id = token_owner, G, CHANNEL, SENT
        return clicked


class PublishTests(ReceiptTests):
    async def test_receipt_lists_results_and_names_its_private_buttons(self) -> None:
        results = [DONE, self.query(), self.query("admin_status")]
        self.assertTrue(await self.receipts.publish(self.source, "req", results))
        sent = self.channel.send.await_args.kwargs
        view = sent["view"]
        self.assertEqual(view.children[0].children[0].content,
                         f"## {ICONS['ai']} 操作結果\n-# 按鈕 30 分鐘內有效，只有你打得開")
        self.assertIn(f"{ICONS['ok']} **完成**　管理操作已完成。", text(view))
        buttons = [button.item for button in view.children[-1].children]
        self.assertEqual([button.label for button in buttons], ["查看功能說明", "查看管理狀態"])
        self.assertEqual([button.custom_id for button in buttons],
                         [f"horo:private:{results[index]['private_ref']}" for index in (1, 2)])
        self.assertIsNone(sent["reference"])
        effect = self.effect()
        self.assertEqual((effect.kind, effect.status, effect.message_id, effect.nonce), ("action.receipt", "done", SENT, sent["nonce"]))
        self.assertEqual(effect.payload, {"results": results, "source_message_id": 1000})
        self.assertTrue(await self.receipts.publish(self.source, "req", results))
        self.channel.send.assert_awaited_once()

    async def test_receipt_replies_only_with_read_message_history(self) -> None:
        self.permissions = discord.Permissions(view_channel=True, send_messages=True, read_message_history=True)
        await self.receipts.publish(self.source, "req", [DONE])
        self.assertEqual(self.channel.send.await_args.kwargs["reference"].message_id, 1000)

    async def test_actor_who_cannot_send_gets_no_receipt(self) -> None:
        self.permissions = discord.Permissions(view_channel=True)
        self.assertFalse(await self.receipts.publish(self.source, "req", [DONE]))
        self.assertIsNone(self.effect())
        self.channel.send.assert_not_awaited()

    async def test_unknown_delivery_is_never_resent_or_explained(self) -> None:
        self.channel.send.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="ERROR"):
            self.assertTrue(await self.receipts.publish(self.source, "req", [DONE]))
        self.assertEqual(self.effect().status, "unknown")
        self.assertTrue(await self.receipts.publish(self.source, "req", [DONE]))
        await self.receipts.explain(self.source, "說明", lambda: True)
        self.channel.send.assert_awaited_once()
        self.posted.edit.assert_not_awaited()


class ExplainTests(ReceiptTests):
    async def test_explanation_uses_the_stored_results(self) -> None:
        results = [self.query()]
        request = self.operations.register_request(1000, USER, G, CHANNEL, {"decision": {}})
        await self.receipts.publish(self.source, request.request_id, results)
        results[0]["text"] = "洩漏私人內容 9876"
        await self.receipts.explain(self.source, "完成", lambda: False)
        self.posted.edit.assert_not_awaited()
        await self.receipts.explain(self.source, "完成", lambda: True)
        view = self.posted.edit.await_args.kwargs["view"]
        self.assertNotIn("9876", text(view))
        self.assertIn(f"{ICONS['ok']} **完成**　已查詢功能說明。", text(view))
        self.assertEqual(view.children[0].children[-1].content, "> **樂奈**　完成")


class ResendTests(ReceiptTests):
    def pending(self) -> str:
        with transaction(self.db):
            self.operations.create_effect("request:req:receipt", "action.receipt", CHANNEL,
                                          {"results": [self.query()], "source_message_id": 1000}, nonce="n" * 24)
            self.operations.update_effect("request:req:receipt", "unknown")
        return "request:req:receipt"

    async def test_resend_authorizes_after_the_lock_without_exposing_pending(self) -> None:
        effect_id = self.pending()
        authorized = True
        async with self.receipts._lock:
            manual = asyncio.create_task(self.receipts.resend(effect_id, allow_unknown=True, still_current=lambda: authorized))
            normal = asyncio.create_task(self.receipts.resend(effect_id))
            await asyncio.sleep(0)
            self.assertEqual(self.effect().status, "unknown")
            authorized = False
        self.assertEqual(await asyncio.gather(manual, normal), [False, False])
        self.assertEqual(self.effect().status, "unknown")
        self.resent.assert_not_awaited()
        self.assertIsNone(await self.receipts.effect_kind().retry(effect_id, lambda: True))
        sent = self.resent.await_args.kwargs
        self.assertEqual(sent["nonce"], "n" * 24)
        self.assertEqual([button.item.label for button in sent["view"].children[-1].children], ["查看功能說明"])
        self.assertEqual((self.effect().status, self.effect().message_id), ("done", SENT + 1))

    async def test_only_receipts_are_resent(self) -> None:
        with transaction(self.db):
            self.operations.create_effect("other", "voice.create", CHANNEL, {})
        self.assertFalse(await self.receipts.resend("other", allow_unknown=True, still_current=lambda: True))
        self.assertFalse(await self.receipts.resend("missing", allow_unknown=True, still_current=lambda: True))


class PrivateTests(ReceiptTests):
    async def bound(self, name: str) -> str:
        result = self.query(name)
        await self.receipts.publish(self.source, name, [result])
        return result["private_ref"]

    async def test_owner_sees_the_rendered_query(self) -> None:
        token = await self.bound("help")
        await self.receipts.private_results.show(token, clicked := self.click())
        self.render.assert_awaited()
        self.assertEqual(self.render.await_args.args[0].user_id, USER)
        self.assertIs(clicked.edit_original_response.await_args.kwargs["view"], self.render.return_value["view"])

    async def test_admin_query_is_refused_to_members_and_unknown_member_fails_closed(self) -> None:
        token = await self.bound("admin_status")
        await self.receipts.private_results.show(token, clicked := self.click())
        self.assertIn("此操作僅限伺服器管理員使用。", text(clicked.followup.send.await_args.kwargs["view"]))
        self.render.assert_not_awaited()
        token = await self.bound("help")
        self.guild.members.clear()
        await self.receipts.private_results.show(token, clicked := self.click())
        self.assertIn("無法確認目前的伺服器身分。", text(clicked.followup.send.await_args.kwargs["view"]))


if __name__ == "__main__":
    unittest.main()
