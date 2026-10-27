import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.ai.private_results import TTL_SECONDS, PrivateResults
from src.brand import ICONS, NEUTRAL_COLOUR, notice_view
from src.testing import interaction, text


class PrivateResultsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = SimpleNamespace(id=1000, author=SimpleNamespace(id=1), guild=SimpleNamespace(id=5),
                                      channel=SimpleNamespace(id=10))
        self.authorize = AsyncMock(return_value=None)
        self.render = AsyncMock(return_value={"view": notice_view("私人團名 9876", "info"), "attachments": []})
        self.registry = PrivateResults(self.authorize, self.render, {"help": "功能說明", "steam_query": "Steam 免費遊戲"})
        self.token = self.registry.issue(self.source, "help")
        self.registry.bind_message(self.token, 3000)

    def click(self, user_id=1, guild_id=5, channel_id=10, message_id=3000):
        clicked = interaction()
        clicked.user.id, clicked.guild_id, clicked.channel_id, clicked.message.id = user_id, guild_id, channel_id, message_id
        clicked.message.edit = AsyncMock()
        return clicked

    async def test_result_is_a_new_private_response_and_never_edits_public_message(self):
        clicked = self.click()
        await self.registry.make_buttons([self.token])[0].callback(clicked)
        clicked.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.authorize.assert_has_awaits(self.authorize.await_args_list)
        self.assertEqual(self.authorize.await_count, 2)
        clicked.message.edit.assert_not_awaited()
        self.assertIn("9876", text(clicked.edit_original_response.await_args.kwargs["view"]))

    async def test_identity_position_and_copied_message_are_denied_before_read(self):
        for options in ({"user_id": 2}, {"guild_id": 6}, {"channel_id": 11}, {"message_id": 3001}):
            clicked = self.click(**options)
            await self.registry.show(self.token, clicked)
            self.assertNotIn("9876", text(clicked.response.send_message.await_args.kwargs["view"]))
        self.authorize.assert_not_awaited()
        self.render.assert_not_awaited()

    async def test_unbound_expired_and_restart_token_cannot_read(self):
        unbound = self.registry.issue(self.source, "help")
        await self.registry.show(unbound, self.click())
        self.render.assert_not_awaited()
        with patch("src.ai.private_results.time.monotonic", return_value=0):
            expired = self.registry.issue(self.source, "help")
            self.registry.bind_message(expired, 3000)
        with patch("src.ai.private_results.time.monotonic", return_value=31 * 60):
            await self.registry.show(expired, late := self.click())
        restarted = PrivateResults(self.authorize, self.render, {})
        await restarted.show(self.token, after_restart := self.click())
        self.render.assert_not_awaited()
        for clicked in (late, after_restart):
            notice = clicked.response.send_message.await_args.kwargs["view"]
            self.assertEqual(text(notice), f"{ICONS['deadline']} 私人結果已過期，請再 @樂奈 問一次。")
            self.assertEqual(notice.children[0].accent_colour, NEUTRAL_COLOUR)

    async def test_authorization_is_rechecked_after_slow_render(self):
        self.authorize.side_effect = [None, "你目前沒有查看權限。"]
        clicked = self.click()
        await self.registry.show(self.token, clicked)
        self.render.assert_awaited_once()
        clicked.edit_original_response.assert_not_awaited()
        self.assertNotIn("9876", text(clicked.followup.send.await_args.kwargs["view"]))

    async def test_late_render_cannot_display_after_expiry(self):
        with patch("src.ai.private_results.time.monotonic", return_value=0) as clock:
            token = self.registry.issue(self.source, "help")
            self.registry.bind_message(token, 3000)
            async def slow_render(*args):
                clock.return_value = TTL_SECONDS + 1
                return {"view": notice_view("私人團名 9876", "info")}
            self.render.side_effect = slow_render
            clicked = self.click()
            await self.registry.show(token, clicked)
        clicked.edit_original_response.assert_not_awaited()
        self.assertNotIn("9876", text(clicked.followup.send.await_args.kwargs["view"]))

    async def test_registry_is_bounded_and_cannot_rebind_to_copied_message(self):
        for _ in range(20):
            self.registry.issue(self.source, "help")
        self.assertEqual(len(self.registry._items), 10)
        for user_id in range(2, 250):
            self.source.author.id = user_id
            self.registry.issue(self.source, "help")
        self.assertEqual(len(self.registry._items), 200)
        token = self.registry.issue(self.source, "help")
        self.registry.bind_message(token, 3000)
        with self.assertRaises(ValueError):
            self.registry.bind_message(token, 3001)

    def test_only_live_unbound_results_can_attach_to_a_receipt(self):
        self.assertFalse(self.registry.can_bind(self.token))
        self.assertFalse(self.registry.can_bind("unknown"))
        with patch("src.ai.private_results.time.monotonic", return_value=0) as clock:
            fresh = self.registry.issue(self.source, "help")
            self.assertTrue(self.registry.can_bind(fresh))
            clock.return_value = TTL_SECONDS
            self.assertFalse(self.registry.can_bind(fresh))

    async def test_result_opens_for_thirty_minutes(self):
        with patch("src.ai.private_results.time.monotonic", return_value=0) as clock:
            live, stale = self.registry.issue(self.source, "help"), self.registry.issue(self.source, "help")
            for token in (live, stale):
                self.registry.bind_message(token, 3000)
            clock.return_value = 29 * 60
            await self.registry.show(live, opened := self.click())
            self.assertIn("9876", text(opened.edit_original_response.await_args.kwargs["view"]))
            clock.return_value = 31 * 60
            await self.registry.show(stale, late := self.click())
        self.render.assert_awaited_once()
        self.assertEqual(text(late.response.send_message.await_args.kwargs["view"]), f"{ICONS['deadline']} 私人結果已過期，請再 @樂奈 問一次。")

    def test_buttons_are_named_by_kind_and_one_per_distinct_result(self):
        again = self.registry.issue(self.source, "help")
        queries = [self.registry.issue(self.source, "steam_query", {"page": page}) for page in (2, 2, 3)]
        buttons = self.registry.make_buttons([self.token, again, *queries, "gone"])
        self.assertEqual([button.item.label for button in buttons], ["查看功能說明", "查看Steam 免費遊戲", "查看Steam 免費遊戲", "查看私人結果"])
        self.assertEqual([button.item.custom_id for button in buttons],
                         [f"horo:private:{token}" for token in (self.token, queries[0], queries[2], "gone")])

    async def test_eviction_during_render_revokes_the_in_flight_result(self):
        async def render_and_evict(entry, interaction):
            for _ in range(10):
                self.registry.issue(self.source, "help")
            return {"view": notice_view("私人團名 9876", "info")}
        self.render.side_effect = render_and_evict
        clicked = self.click()
        await self.registry.show(self.token, clicked)
        clicked.edit_original_response.assert_not_awaited()
        self.assertNotIn("9876", text(clicked.followup.send.await_args.kwargs["view"]))


if __name__ == "__main__":
    unittest.main()
