import asyncio
import itertools
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.brand import BRAND_COLOUR, ICONS, NEUTRAL_COLOUR
from src.lfg.cog import LfgCog
from src.lfg.operations import LFG_SECONDS, LfgOperations
from src.lfg.store import LfgCard, LfgStore
from src.lfg.views import LfgButton, card_view, list_view
from src.state import open_db
from src.testing import text as screen

HOST, A, B, MESSAGE = 1, 2, 3, 99
INTERACTION_IDS = itertools.count(1000)


class LfgButtonTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.store = LfgStore(db)
        self.store._add(LfgCard(MESSAGE, 5, HOST, "Apex", 2, None, [HOST], 2**31))
        self.members = {}
        self.guild = MagicMock(spec=discord.Guild, id=1)
        self.guild.fetch_member = AsyncMock(side_effect=self.members.get)
        self.guild.get_member.return_value = None
        self.bot = MagicMock(lfg=self.store)
        self.bot.get_guild.return_value = self.guild
        self.bot.unavailable_reason.return_value = None
        self.bot.temp_voice.open_party = AsyncMock(return_value=None)
        self.bot.lfg_operations = LfgOperations(self.bot)

    def interaction(self, user_id: int) -> MagicMock:
        interaction = MagicMock()
        interaction.id, interaction.guild_id, interaction.channel_id, interaction.channel.id = next(INTERACTION_IDS), 1, 5, 5
        interaction.guild = self.guild
        interaction.guild.get_channel_or_thread.return_value = interaction.channel
        interaction.client = self.bot
        interaction.user.id = user_id
        self.members[user_id] = interaction.user
        interaction.message.id = MESSAGE
        interaction.guild.get_member.return_value = None
        interaction.channel.send = AsyncMock()
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.response.defer = AsyncMock(
            side_effect=lambda: setattr(interaction.response.is_done, "return_value", True)
        )
        interaction.edit_original_response = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def click(self, action: str, user_id: int) -> MagicMock:
        interaction = self.interaction(user_id)
        await LfgButton(action).callback(interaction)
        return interaction

    def notice(self, interaction: MagicMock) -> str:
        return screen(interaction.followup.send.await_args.kwargs["view"])

    async def test_full_party_departs_once(self) -> None:
        joined = await self.click("join", A)
        card = joined.edit_original_response.await_args.kwargs["view"]
        self.assertIn("🚀 已出發", screen(card))
        self.assertEqual(card.children[0].accent_colour, NEUTRAL_COLOUR)
        self.assertEqual(len(card.children), 1)
        reply = joined.channel.send.await_args
        self.assertIn(f"<@{HOST}> <@{A}>", screen(reply.kwargs["view"]))
        self.assertEqual(reply.kwargs["files"][0].filename, "depart.png")
        self.assertIn("attachment://depart.png", str(reply.kwargs["view"].to_components()))
        mentions = discord.AllowedMentions.none().merge(reply.kwargs["allowed_mentions"])
        self.assertEqual(mentions.to_dict(), {"users": [HOST, A], "parse": []})
        self.assertIsNone(self.store.get(MESSAGE))
        for action, user_id in (("join", B), ("go", HOST)):
            late = await self.click(action, user_id)
            self.assertIn("已經結束", self.notice(late))
            late.channel.send.assert_not_awaited()

    async def test_cheer_survives_a_restart_and_ends_with_the_card(self) -> None:
        self.store._finish(MESSAGE)
        self.store._add(LfgCard(MESSAGE, 5, HOST, "Apex", 3, None, [HOST], 2**31))
        self.bot.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
        await self.bot.lfg_operations.cheer(MESSAGE, "快來")
        self.assertEqual(self.store.get(MESSAGE).cheer, "快來")
        self.bot.lfg_operations = LfgOperations(self.bot)
        joined = await self.click("join", A)
        self.assertIn("**樂奈**　快來", screen(joined.edit_original_response.await_args.kwargs["view"]))
        went = await self.click("go", HOST)
        self.assertNotIn("樂奈", screen(went.edit_original_response.await_args.kwargs["view"]))
        self.assertIsNone(self.store.get(MESSAGE))

    async def test_shutdown_click_changes_nothing(self) -> None:
        interaction = self.interaction(A)
        interaction.client.unavailable_reason.return_value = "Bot 正在重新啟動，請稍後再試。"
        interaction.response.send_message = AsyncMock()
        await LfgButton("join").callback(interaction)
        notice = interaction.response.send_message.await_args.kwargs["view"]
        self.assertIn("Bot 正在重新啟動", screen(notice))
        interaction.response.defer.assert_not_awaited()
        self.assertEqual(self.store.get(MESSAGE).members, [HOST])

    async def test_only_host_can_depart_or_cancel(self) -> None:
        self.store._set_members(MESSAGE, [HOST, A])
        for action in ("go", "cancel"):
            clicked = await self.click(action, A)
            self.assertIn("只有發起人可以按「出發」或「取消揪團」", self.notice(clicked))
        self.assertIsNotNone(self.store.get(MESSAGE))

    async def test_host_leaving_is_pointed_to_cancel(self) -> None:
        clicked = await self.click("leave", HOST)
        self.assertIn("發起人請按「取消揪團」", self.notice(clicked))
        clicked.edit_original_response.assert_not_awaited()
        self.assertEqual(self.store.get(MESSAGE).members, [HOST])

    async def test_host_cancels_without_ping(self) -> None:
        clicked = await self.click("cancel", HOST)
        card = clicked.edit_original_response.await_args.kwargs["view"]
        self.assertIn("🚫 已取消", screen(card))
        self.assertEqual(card.children[0].accent_colour, NEUTRAL_COLOUR)
        clicked.channel.send.assert_not_awaited()
        self.assertIsNone(self.store.get(MESSAGE))

    def test_open_card_escapes_text_and_offers_four_buttons(self) -> None:
        card = card_view(LfgCard(MESSAGE, 5, HOST, "**Apex**", 3, "> *今晚*", [HOST], 2**31))
        text = screen(card)
        self.assertIn(r"\*\*Apex\*\*", text)
        self.assertIn(r"> \> \*今晚\*", text)
        self.assertIn("還差 2 人", text)
        self.assertNotIn("-# 還差", text)
        self.assertEqual([item.custom_id for item in card.children[1].children],
                         ["horo:lfg:join", "horo:lfg:leave", "horo:lfg:go", "horo:lfg:cancel"])

    def test_list_is_a_card_title_card_not_a_bold_notice(self) -> None:
        view = list_view([LfgCard(MESSAGE, 5, HOST, "**Apex**", 3, None, [HOST], 2**31)], 1)
        self.assertEqual(view.children[0].children[0].content, f"## {ICONS['lfg']} 揪團")
        self.assertEqual(screen(view).split("\n")[1:],
                         [rf"[\*\*Apex\*\*](https://discord.com/channels/1/5/{MESSAGE}) · 1 / 3 人 · <t:{2**31}:R> 結束"])
        self.assertEqual(view.children[0].accent_colour, BRAND_COLOUR)
        self.assertEqual(screen(list_view([], 1)), f"## {ICONS['lfg']} 揪團\n目前沒有可見的揪團。")
        many = [LfgCard(MESSAGE + n, 5, HOST, f"g{n}", 4, None, [HOST, 2], 2**31) for n in range(16)]
        self.assertEqual(screen(list_view(many, 1)).split("\n")[1:][-1],
                         f"[g14](https://discord.com/channels/1/5/{MESSAGE + 14}) · 2 / 4 人 · <t:{2**31}:R> 結束")

    async def test_rejects_duplicate_join(self) -> None:
        clicked = await self.click("join", HOST)
        self.assertEqual(self.notice(clicked), f"{ICONS['info']} 你已經在隊伍裡了。")
        self.assertEqual(clicked.followup.send.await_args.kwargs["view"].children[0].accent_colour, BRAND_COLOUR)
        self.assertEqual(self.store.get(MESSAGE).members, [HOST])

    async def test_expired_card_says_so_in_grey(self) -> None:
        self.store._finish(MESSAGE)
        self.store._add(LfgCard(MESSAGE, 5, HOST, "Apex", 2, None, [HOST], 0))
        clicked = await self.click("join", A)
        self.assertEqual(self.notice(clicked), f"{ICONS['deadline']} 這個揪團已過期，請用 /揪團 重新開一團。")
        self.assertEqual(clicked.followup.send.await_args.kwargs["view"].children[0].accent_colour, NEUTRAL_COLOUR)
        self.assertIsNone(self.store.get(MESSAGE))

    async def test_member_leaves_once(self) -> None:
        self.store._set_members(MESSAGE, [HOST, A])
        left = await self.click("leave", A)
        self.assertIn("1 / 2 人", screen(left.edit_original_response.await_args.kwargs["view"]))
        self.assertIn("不在這個隊伍", self.notice(await self.click("leave", A)))
        self.assertEqual(self.store.get(MESSAGE).members, [HOST])

    async def test_clicks_wait_for_the_previous_edit(self) -> None:
        self.store._finish(MESSAGE)
        self.store._add(LfgCard(MESSAGE, 5, HOST, "Apex", 3, None, [HOST], 2**31))
        editing, release = asyncio.Event(), asyncio.Event()

        async def slow_edit(**_) -> None:
            editing.set()
            await release.wait()

        joined, went = self.interaction(A), self.interaction(HOST)
        joined.edit_original_response.side_effect = slow_edit
        first = asyncio.create_task(LfgButton("join").callback(joined))
        await editing.wait()
        second = asyncio.create_task(LfgButton("go").callback(went))
        await asyncio.sleep(0)
        went.response.defer.assert_awaited_once()
        went.edit_original_response.assert_not_awaited()
        self.assertEqual(self.store.get(MESSAGE).members, [HOST, A])
        release.set()
        await asyncio.gather(first, second)
        self.assertIn("🚀 已出發", screen(went.edit_original_response.await_args.kwargs["view"]))
        self.assertIn(f"<@{HOST}> <@{A}>", screen(went.channel.send.await_args.kwargs["view"]))
        self.assertIsNone(self.store.get(MESSAGE))

    async def test_departure_ping_survives_a_deleted_card(self) -> None:
        joined = self.interaction(A)
        joined.edit_original_response.side_effect = discord.NotFound(MagicMock(status=404), "gone")
        with self.assertLogs(level="ERROR"):
            await LfgButton("join").callback(joined)
        self.assertIn(f"<@{HOST}> <@{A}>", screen(joined.channel.send.await_args.kwargs["view"]))
        joined.message.to_reference.assert_called_once_with(fail_if_not_exists=False)
        self.assertIsNone(self.store.get(MESSAGE))

    async def test_overdue_card_ends_on_load(self) -> None:
        self.store._add(LfgCard(MESSAGE + 1, 5, HOST, "LoL", 2, None, [HOST], 0, "快來"))
        bot = MagicMock(lfg=self.store)
        bot.get_guild.return_value = None
        bot.lfg_operations = LfgOperations(bot)
        bot.wait_until_ready = AsyncMock()
        edit = bot.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
        cog = LfgCog(bot)
        await cog.cog_load()
        await asyncio.sleep(0.01)
        await cog.cog_unload()
        bot.add_dynamic_items.assert_called_once_with(LfgButton)
        self.assertIn("🔒 已結束", screen(edit.await_args.kwargs["view"]))
        self.assertNotIn("樂奈", screen(edit.await_args.kwargs["view"]))
        self.assertIsNone(self.store.get(MESSAGE + 1))
        self.assertIsNotNone(self.store.get(MESSAGE))


class RanaCheerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        db = open_db(":memory:")
        self.addCleanup(db.close)
        self.store = LfgStore(db)
        self.bot = MagicMock(lfg=self.store)
        self.bot.get_guild.return_value = None
        self.bot.wait_until_ready = AsyncMock()
        self.edit = self.bot.get_partial_messageable.return_value.get_partial_message.return_value.edit = AsyncMock()
        self.bot.lfg_operations = LfgOperations(self.bot)
        self.ai = AsyncMock(return_value="還差一個，Apex 不等人喔")
        ask = patch("src.lfg.views.ask_once", self.ai)
        ask.start()
        self.addCleanup(ask.stop)
        self.cog = LfgCog(self.bot)

    def card(self, age: int, members: tuple[int, ...] = (HOST,)) -> LfgCard:
        card = LfgCard(MESSAGE, 5, HOST, "Apex", 2, "晚上", list(members), int(time.time()) - age + LFG_SECONDS)
        self.store._add(card)
        return card

    async def run_cheers(self, card: LfgCard, *, new: bool) -> None:
        self.cog.schedule(card, new=new)
        await asyncio.sleep(0.01)
        await self.cog.cog_unload()

    async def test_new_card_cheer_goes_into_the_card_last(self) -> None:
        await self.run_cheers(self.card(0), new=True)
        self.ai.assert_awaited_once()
        self.assertIn('"再喊一次": false', self.ai.await_args.args[2])
        view = self.edit.await_args.kwargs["view"]
        self.assertIn("**樂奈**　還差一個，Apex 不等人喔", view.children[0].children[-1].content)
        self.assertIn("horo:lfg:join", str(view.to_components()))
        self.assertEqual(self.store.get(MESSAGE).cheer, "還差一個，Apex 不等人喔")

    async def test_no_ai_answer_edits_nothing(self) -> None:
        self.ai.return_value = None
        await self.run_cheers(self.card(0), new=True)
        self.edit.assert_not_awaited()

    async def test_cheers_again_after_half_an_hour_while_open(self) -> None:
        with patch("src.lfg.cog.CHEER_AFTER", 0):
            await self.run_cheers(self.card(0), new=True)
        self.assertEqual(self.edit.await_count, 2)
        self.assertIn('"再喊一次": true', self.ai.await_args.args[2])

    async def test_full_or_ended_card_gets_no_cheer(self) -> None:
        with patch("src.lfg.cog.CHEER_AFTER", 0):
            await self.run_cheers(self.card(0, [HOST, A]), new=True)
            self.store._finish(MESSAGE)
            await self.cog._rana(MESSAGE, again=True)
        self.ai.assert_not_awaited()
        self.edit.assert_not_awaited()

    async def test_card_filled_while_rana_thinks_is_left_alone(self) -> None:
        self.card(0)
        self.ai.side_effect = lambda *args, **kwargs: self.store._set_members(MESSAGE, [HOST, A]) or "快來"
        await self.cog._rana(MESSAGE, again=False)
        self.edit.assert_not_awaited()
        self.assertIsNone(self.store.get(MESSAGE).cheer)

    async def test_restart_skips_the_first_cheer_and_a_passed_check(self) -> None:
        card = self.card(60)
        self.store._set_cheer(MESSAGE, "快來")
        self.cog.schedule(card, new=False)
        self.assertEqual(len(self.cog._expiring), 2)
        await asyncio.sleep(0.01)
        self.ai.assert_not_awaited()
        self.edit.assert_not_awaited()
        self.assertEqual(self.store.get(MESSAGE).cheer, "快來")
        await self.cog.cog_unload()
        self.store._finish(MESSAGE)
        self.cog.schedule(self.card(31 * 60), new=False)
        self.assertEqual(len(self.cog._expiring), 1)
        await self.cog.cog_unload()


if __name__ == "__main__":
    unittest.main()
