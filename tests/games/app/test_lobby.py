import asyncio
import unittest

from src.games.app.lobby import BIND_STALE, NO_CHANNEL, PUBLISH_FAILED, PUBLISH_RECORDED, PUBLISH_REVOKED, PUBLISH_STALE
from src.games.app.ports import PresentError
from tests.support.games import CHANNEL, GUILD, OTHER_GUILD, RESTARTING, ROLE, GameHarness


class AccessTests(unittest.IsolatedAsyncioTestCase):
    def test_lobby_gates_by_guild_channel_identity_and_role(self) -> None:
        game = GameHarness(self)
        game.presence.closing = True
        self.assertEqual(game.lobby.problem(GUILD, CHANNEL, frozenset({ROLE})), RESTARTING)
        game.presence.closing = False
        self.assertEqual(game.lobby.problem(GUILD + 1, CHANNEL, frozenset({ROLE})), OTHER_GUILD)
        self.assertEqual(game.lobby.problem(GUILD, CHANNEL + 1, None), f"請到 <#{CHANNEL}> 使用遊戲大廳。")
        self.assertEqual(game.lobby.problem(GUILD, CHANNEL, None), "目前無法確認你的伺服器身分，請稍後再試。")
        self.assertEqual(game.lobby.problem(GUILD, CHANNEL, frozenset()), "你沒有使用這個功能的身分組。")
        self.assertIsNone(game.lobby.problem(GUILD, CHANNEL, frozenset({ROLE})))
        game.lobby.set_roles([])
        self.assertIsNone(game.lobby.problem(GUILD, CHANNEL, frozenset()))

    def test_closed_lobby_is_named_before_identity(self) -> None:
        game = GameHarness(self)
        game.db.execute("DELETE FROM settings WHERE key = 'games.channel_id'")
        self.assertEqual(game.lobby.problem(GUILD, CHANNEL, None), "遊戲大廳尚未開放，請等管理員設定遊戲頻道。")

    def test_roles_are_stored_sorted(self) -> None:
        game = GameHarness(self)
        game.lobby.set_roles({9, 3})
        self.assertEqual(game.lobby.role_ids, [3, 9])


def move_lobby(game: GameHarness, channel_id: int):
    async def moved() -> None:
        game.board.channels.add(channel_id)
        game.store.set_channel(channel_id, operation_id=None)

    return moved


class PublishTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_records_the_hub_and_clears_the_previous_one(self) -> None:
        game = GameHarness(self)
        self.assertIsNone(await game.lobby.publish_hub(operation_id="first"))
        self.assertIsNone(await game.lobby.publish_hub(operation_id="second"))
        self.assertEqual(game.board.sent, [(CHANNEL, 500), (CHANNEL, 501)])
        self.assertEqual(game.lobby.hub_message, [CHANNEL, 501])
        self.assertEqual(game.board.cleared, [None, [CHANNEL, 500]])
        effect = game.records.get_effect("second:game-hub")
        self.assertEqual((effect.kind, effect.status, effect.message_id, effect.payload),
                         ("game_hub_publish", "done", 501, {"previous_hub": [CHANNEL, 500]}))

    async def test_missing_channel_sends_nothing(self) -> None:
        game = GameHarness(self)
        game.board.channels.clear()
        self.assertEqual(await game.lobby.publish_hub(operation_id="none"), NO_CHANNEL)
        self.assertIsNone(game.records.get_effect("none:game-hub"))

    async def test_revocation_while_drawing_prevents_any_send(self) -> None:
        game = GameHarness(self)
        drawing, release = asyncio.Event(), asyncio.Event()
        allowed = True

        async def draw() -> None:
            drawing.set()
            await release.wait()

        game.board.drawing = draw
        owner = asyncio.create_task(game.lobby.publish_hub(operation_id="revoked", still_current=lambda: allowed))
        await drawing.wait()
        self.assertIn(owner, game.tasks.pending())
        allowed = False
        release.set()
        self.assertEqual(await owner, PUBLISH_REVOKED)
        self.assertEqual((game.board.sent, game.lobby.hub_message), ([], None))
        effect = game.records.get_effect("revoked:game-hub")
        self.assertEqual((effect.status, effect.error), ("failed", "revoked_before_send"))
        self.assertFalse(game.tasks.pending())

    async def test_same_publication_is_serialized_and_never_sends_twice(self) -> None:
        game = GameHarness(self)
        drawing, release = asyncio.Event(), asyncio.Event()

        async def draw() -> None:
            drawing.set()
            await release.wait()

        game.board.drawing = draw
        first = asyncio.create_task(game.lobby.publish_hub(operation_id="same"))
        await drawing.wait()
        second = asyncio.create_task(game.lobby.publish_hub(operation_id="same"))
        await asyncio.sleep(0)
        self.assertEqual(game.tasks.pending(), {first, second})
        release.set()
        self.assertEqual(await asyncio.gather(first, second), [None, PUBLISH_RECORDED])
        self.assertEqual(len(game.board.sent), 1)

    async def test_slow_publish_yields_to_a_newer_channel(self) -> None:
        game = GameHarness(self)
        await game.lobby.publish_hub()
        game.board.sending = move_lobby(game, 12)
        self.assertIsNone(await game.lobby.publish_hub(operation_id="slow"))
        self.assertEqual(game.board.withdrawn, [(CHANNEL, 501)])
        self.assertEqual(game.lobby.hub_message, [CHANNEL, 500])
        effect = game.records.get_effect("slow:game-hub")
        self.assertEqual((effect.status, effect.message_id, effect.error), ("skipped", 501, "superseded"))

    async def test_superseded_message_that_cannot_be_withdrawn_awaits_review(self) -> None:
        game = GameHarness(self)
        game.board.withdraw_ok = False
        game.board.sending = move_lobby(game, 12)
        with self.assertLogs(level="WARNING"):
            self.assertIsNone(await game.lobby.publish_hub(operation_id="stuck"))
        effect = game.records.get_effect("stuck:game-hub")
        self.assertEqual((effect.status, effect.error), ("unknown", "superseded_cleanup_failed"))
        self.assertIsNone(game.lobby.hub_message)

    async def test_failed_send_is_reported_and_left_unknown(self) -> None:
        game = GameHarness(self)
        game.board.post_error = PresentError("forbidden")
        with self.assertLogs(level="ERROR"):
            self.assertEqual(await game.lobby.publish_hub(operation_id="down"), PUBLISH_FAILED)
        self.assertEqual(game.records.get_effect("down:game-hub").status, "unknown")
        self.assertIsNone(game.lobby.hub_message)

    async def test_stale_grant_publishes_nothing(self) -> None:
        game = GameHarness(self)
        self.assertEqual(await game.lobby.publish_hub(operation_id="late", still_current=lambda: False), PUBLISH_STALE)
        self.assertIsNone(game.records.get_effect("late:game-hub"))


class BindTests(unittest.IsolatedAsyncioTestCase):
    async def test_binding_sets_the_channel_then_publishes(self) -> None:
        game = GameHarness(self)
        game.board.channels.add(12)
        self.assertIsNone(await game.lobby.bind_channel(12, operation_id="bind"))
        self.assertEqual((game.lobby.channel_id, game.lobby.hub_message), (12, [12, 500]))
        self.assertEqual(game.records.get("bind:channel").kind, "games.channel")
        self.assertEqual(game.records.get_effect("bind:game-hub").status, "done")

    async def test_binding_reports_a_failed_publish(self) -> None:
        game = GameHarness(self)
        game.board.channels.clear()
        self.assertEqual(await game.lobby.bind_channel(12), f"已設定遊戲頻道，但{NO_CHANNEL}")
        self.assertEqual(game.lobby.channel_id, 12)

    async def test_stale_binding_changes_nothing(self) -> None:
        game = GameHarness(self)
        self.assertEqual(await game.lobby.bind_channel(12, still_current=lambda: False), BIND_STALE)
        self.assertEqual(game.lobby.channel_id, CHANNEL)

    async def test_refresh_redraws_only_a_published_hub(self) -> None:
        game = GameHarness(self)
        await game.lobby.refresh()
        await game.lobby.publish_hub()
        await game.lobby.refresh()
        self.assertEqual(game.board.refreshed, [[CHANNEL, 500]])


if __name__ == "__main__":
    unittest.main()
