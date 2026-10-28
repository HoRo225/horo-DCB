import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord.components import _component_factory

from src.core.adapters.discord.brand import ICONS, NEUTRAL_COLOUR, notice_view
from src.core.adapters.discord.private import TTL_SECONDS, PrivateResults
from src.core.app.ports import Source
from tests.support.testing import interaction, text

LABELS = {"help": "功能說明", "steam_query": "Steam 免費遊戲"}
EXPIRED = f"{ICONS['deadline']} 私人結果已過期，請再 @霍蘿 問一次。"


def source(user_id: int = 1) -> Source:
    return Source(1000, 5, 10, user_id, "", frozenset(), frozenset())


class PrivateResultsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.authorize = AsyncMock(return_value=None)
        self.render = AsyncMock(return_value={"view": notice_view("私人團名 9876", "info"), "attachments": []})
        self.registry = PrivateResults(self.authorize, self.render, LABELS.__getitem__, lambda: "霍蘿")
        self.token = self.registry.issue(source(), "help")
        self.registry.bind_message(self.token, 3000)

    def click(self, user_id=1, guild_id=5, channel_id=10, message_id=3000):
        clicked = interaction()
        clicked.user.id, clicked.guild_id, clicked.channel_id, clicked.message.id = user_id, guild_id, channel_id, message_id
        clicked.message.edit = AsyncMock()
        return clicked

    async def test_button_opens_a_new_private_response_and_never_edits_the_receipt(self):
        clicked = self.click()
        await self.registry.make_buttons([self.token])[0].callback(clicked)
        clicked.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertEqual(self.authorize.await_count, 2)
        clicked.message.edit.assert_not_awaited()
        self.assertIn("9876", text(clicked.edit_original_response.await_args.kwargs["view"]))

    async def test_restored_button_class_reaches_the_same_registry(self):
        match = {"token": self.token}
        button = await self.registry.item.from_custom_id(None, None, match)
        self.assertEqual(button.item.custom_id, f"horo:private:{self.token}")
        await button.callback(clicked := self.click())
        self.assertIn("9876", text(clicked.edit_original_response.await_args.kwargs["view"]))

    async def test_identity_position_and_copied_message_are_denied_before_read(self):
        for options in ({"user_id": 2}, {"guild_id": 6}, {"channel_id": 11}, {"message_id": 3001}):
            clicked = self.click(**options)
            await self.registry.show(self.token, clicked)
            self.assertNotIn("9876", text(clicked.response.send_message.await_args.kwargs["view"]))
        self.authorize.assert_not_awaited()
        self.render.assert_not_awaited()

    async def test_unbound_expired_and_restart_token_cannot_read(self):
        unbound = self.registry.issue(source(), "help")
        await self.registry.show(unbound, self.click())
        self.render.assert_not_awaited()
        with patch("src.core.adapters.discord.private.time.monotonic", return_value=0):
            expired = self.registry.issue(source(), "help")
            self.registry.bind_message(expired, 3000)
        with patch("src.core.adapters.discord.private.time.monotonic", return_value=31 * 60):
            await self.registry.show(expired, late := self.click())
        restarted = PrivateResults(self.authorize, self.render, LABELS.__getitem__, lambda: "霍蘿")
        await restarted.show(self.token, after_restart := self.click())
        self.render.assert_not_awaited()
        for clicked in (late, after_restart):
            notice = clicked.response.send_message.await_args.kwargs["view"]
            self.assertEqual(text(notice), EXPIRED)
            self.assertEqual(notice.children[0].accent_colour, NEUTRAL_COLOUR)

    def receipt(self, *tokens):
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(discord.ui.TextDisplay("操作結果")))
        view.add_item(discord.ui.ActionRow(*self.registry.make_buttons(tokens)))
        return SimpleNamespace(id=3000, components=[_component_factory(data) for data in view.to_components()])

    async def test_expired_button_is_disabled_on_the_receipt_and_explained_privately(self):
        other = self.registry.issue(source(), "steam_query")
        self.registry.bind_message(other, 3000)
        clicked = self.click()
        clicked.message = self.receipt("gone", other)
        await self.registry.item("gone").callback(clicked)
        edited = clicked.response.edit_message.await_args.kwargs["view"]
        buttons = {item.custom_id: item.disabled for item in edited.walk_children() if isinstance(item, discord.ui.Button)}
        self.assertEqual(buttons, {"horo:private:gone": True, f"horo:private:{other}": False})
        self.assertIn("操作結果", text(edited))
        self.assertTrue(edited.is_finished())
        notice = clicked.followup.send.await_args.kwargs
        self.assertTrue(notice["ephemeral"])
        self.assertEqual(text(notice["view"]), EXPIRED)
        self.render.assert_not_awaited()

    async def test_expired_button_still_explains_when_the_receipt_cannot_be_edited(self):
        clicked = self.click()
        clicked.message = self.receipt("gone")
        clicked.response.edit_message.side_effect = discord.HTTPException(MagicMock(status=500), "down")
        with self.assertLogs(level="WARNING"):
            await self.registry.show("gone", clicked)
        notice = clicked.response.send_message.await_args.kwargs
        self.assertTrue(notice["ephemeral"])
        self.assertEqual(text(notice["view"]), EXPIRED)

    async def test_button_errors_reply_with_an_error_card(self):
        self.registry.show = AsyncMock(side_effect=RuntimeError("db gone"))
        clicked = self.click()
        with self.assertLogs(level="ERROR"):
            await self.registry.item(self.token).callback(clicked)
        self.assertIn("發生未預期的錯誤", text(clicked.response.send_message.await_args.kwargs["view"]))

    async def test_authorization_is_rechecked_after_slow_render(self):
        self.authorize.side_effect = [None, "你目前沒有查看權限。"]
        clicked = self.click()
        await self.registry.show(self.token, clicked)
        self.render.assert_awaited_once()
        clicked.edit_original_response.assert_not_awaited()
        self.assertNotIn("9876", text(clicked.followup.send.await_args.kwargs["view"]))

    async def test_late_render_cannot_display_after_expiry(self):
        with patch("src.core.adapters.discord.private.time.monotonic", return_value=0) as clock:
            token = self.registry.issue(source(), "help")
            self.registry.bind_message(token, 3000)

            async def slow_render(*args):
                clock.return_value = TTL_SECONDS + 1
                return {"view": notice_view("私人團名 9876", "info")}

            self.render.side_effect = slow_render
            clicked = self.click()
            await self.registry.show(token, clicked)
        clicked.edit_original_response.assert_not_awaited()
        self.assertNotIn("9876", text(clicked.followup.send.await_args.kwargs["view"]))

    async def test_render_failure_is_a_generic_notice(self):
        self.render.side_effect = LookupError("gone")
        with self.assertLogs(level="ERROR"):
            await self.registry.show(self.token, clicked := self.click())
        self.assertIn("私人結果暫時無法顯示", text(clicked.followup.send.await_args.kwargs["view"]))

    async def test_registry_is_bounded_and_cannot_rebind_to_copied_message(self):
        for _ in range(20):
            self.registry.issue(source(), "help")
        self.assertEqual(len(self.registry._items), 10)
        for user_id in range(2, 250):
            self.registry.issue(source(user_id), "help")
        self.assertEqual(len(self.registry._items), 200)
        token = self.registry.issue(source(), "help")
        self.registry.bind_message(token, 3000)
        with self.assertRaises(ValueError):
            self.registry.bind_message(token, 3001)

    def test_only_live_unbound_results_can_attach_to_a_receipt(self):
        self.assertFalse(self.registry.can_bind(self.token))
        self.assertFalse(self.registry.can_bind("unknown"))
        with patch("src.core.adapters.discord.private.time.monotonic", return_value=0) as clock:
            fresh = self.registry.issue(source(), "help")
            self.assertTrue(self.registry.can_bind(fresh))
            clock.return_value = TTL_SECONDS
            self.assertFalse(self.registry.can_bind(fresh))

    async def test_result_opens_for_thirty_minutes(self):
        with patch("src.core.adapters.discord.private.time.monotonic", return_value=0) as clock:
            live, stale = self.registry.issue(source(), "help"), self.registry.issue(source(), "help")
            for token in (live, stale):
                self.registry.bind_message(token, 3000)
            clock.return_value = 29 * 60
            await self.registry.show(live, opened := self.click())
            self.assertIn("9876", text(opened.edit_original_response.await_args.kwargs["view"]))
            clock.return_value = 31 * 60
            await self.registry.show(stale, late := self.click())
        self.render.assert_awaited_once()
        self.assertEqual(text(late.response.send_message.await_args.kwargs["view"]), EXPIRED)

    def test_buttons_are_named_by_kind_and_one_per_distinct_result(self):
        again = self.registry.issue(source(), "help")
        queries = [self.registry.issue(source(), "steam_query", {"page": page}) for page in (2, 2, 3)]
        buttons = self.registry.make_buttons([self.token, again, *queries, "gone"])
        self.assertEqual([button.item.label for button in buttons],
                         ["查看功能說明", "查看Steam 免費遊戲", "查看Steam 免費遊戲", "查看私人結果"])
        self.assertEqual([button.item.custom_id for button in buttons],
                         [f"horo:private:{token}" for token in (self.token, queries[0], queries[2], "gone")])

    async def test_eviction_during_render_revokes_the_in_flight_result(self):
        async def render_and_evict(entry, interaction):
            for _ in range(10):
                self.registry.issue(source(), "help")
            return {"view": notice_view("私人團名 9876", "info")}

        self.render.side_effect = render_and_evict
        clicked = self.click()
        await self.registry.show(self.token, clicked)
        clicked.edit_original_response.assert_not_awaited()
        self.assertNotIn("9876", text(clicked.followup.send.await_args.kwargs["view"]))


if __name__ == "__main__":
    unittest.main()
