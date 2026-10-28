import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src import testing
from src.ai.cog import AiCog
from src.ai.private_results import PrivateResultButton
from src.bot import HoroBot
from src.games.views import GamePickSelect, RulesButton, lobby_view
from src.steam.cog import SteamCog
from src.steam.models import SteamOffer
from src.steam.views import SteamClaim, build_offer_view

G, BOT = 100, 999
COGS = ["GamesCog", "VoiceCog", "AdminCog", "AiCog", "SteamCog"]

COMMANDS = {
    "新話題": {
        "name": "新話題",
        "description": "開始新的 AI 對話，封存這個範圍的舊對話",
        "type": 1, "options": [], "nsfw": False, "dm_permission": False,
        "default_member_permissions": None, "contexts": [0], "integration_types": None,
    },
    "控制台": {
        "name": "控制台",
        "description": "開啟管理控制台",
        "type": 1,
        "options": [],
        "nsfw": False,
        "dm_permission": False,
        "default_member_permissions": 8,
        "contexts": [0],
        "integration_types": None,
    },
}


def step(order: list[str], name: str, *, stuck: bool = False):
    async def run(*args: object, **kwargs: object) -> None:
        order.append(name)
        if stuck:
            await asyncio.Event().wait()

    return run


class HoroBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        codex = MagicMock()
        codex.request_counts.return_value = (0, 0)
        codex.start = AsyncMock()
        codex.close = AsyncMock()
        codex.cancel = AsyncMock()
        self.bot = HoroBot(codex, guild_id=G, db=testing.memory_db(self))

    async def start(self) -> None:
        await self.enterAsyncContext(self.bot)
        await self.bot.add_cogs()

    async def test_slash_commands_unchanged(self) -> None:
        await self.start()
        tree = self.bot.tree
        self.assertEqual(list(self.bot.cogs), COGS)
        self.assertEqual({c.name: c.to_dict(tree) for c in tree.get_commands(guild=discord.Object(G))}, COMMANDS)
        self.assertEqual(tree.get_commands(), [])
        self.assertIn("on_interaction", self.bot.extra_events)
        self.assertEqual(
            set(self.bot._connection._view_store._dynamic_items.values()),
            {GamePickSelect, RulesButton, PrivateResultButton, SteamClaim},
        )

    async def test_hub_custom_ids_reach_registered_handlers(self) -> None:
        await self.start()
        patterns = self.bot._connection._view_store._dynamic_items
        views = [
            lobby_view(),
            build_offer_view(SteamOffer(730, "G", "", "", (), None)),
        ]
        ids = {custom_id for view in views for item in view.walk_children()
               if (custom_id := getattr(item, "custom_id", None))}
        self.assertIn("horo:game:pick", ids)
        for custom_id in ids | {"horo:game:rules:blackjack", "horo:steam:claim:730"}:
            with self.subTest(custom_id):
                self.assertTrue(custom_id.startswith("horo:"))
                self.assertTrue(any(pattern.fullmatch(custom_id) for pattern in patterns))

    async def test_cogs_removed_in_add_order_with_their_commands(self) -> None:
        await self.start()
        with patch.object(self.bot, "remove_cog", wraps=self.bot.remove_cog) as remove_cog:
            await self.bot.close()
        self.assertEqual([c.args[0] for c in remove_cog.await_args_list], COGS)
        self.assertEqual(self.bot.tree.get_commands(guild=discord.Object(G)), [])

    async def test_every_cog_task_set_is_cancelled_and_drained_at_close(self) -> None:
        from src.tasks import track

        await self.start()
        probes = {
            (name, field): track(value, asyncio.create_task(asyncio.Event().wait()))
            for name, cog in self.bot.cogs.items()
            for field, value in vars(cog).items()
            if isinstance(value, set) and all(isinstance(task, asyncio.Task) for task in value)
        }
        self.assertLessEqual({("AiCog", "_requests"), ("GamesCog", "_redrawing")}, probes.keys())
        self.bot._shutdown_failstop = MagicMock(side_effect=testing.Fatal)
        await self.bot.close()
        for key, probe in probes.items():
            with self.subTest(key):
                self.assertTrue(probe in self.bot._shutdown_seen)
                self.assertTrue(probe.cancelled())

    async def test_stuck_cog_unload_only_cut_off_by_its_own_limit(self) -> None:
        await self.start()
        order: list[str] = []
        loop = asyncio.get_running_loop()
        start = loop.time()
        with (
            patch("src.tasks.SHUTDOWN_STAGE_SECONDS", 0.5),
            patch.object(self.bot.admin_panels, "close", step(order, "panel", stuck=True)),
            patch.object(self.bot.access_service, "close", step(order, "access")),
            patch.object(self.bot.codex, "close", step(order, "codex")),
            patch.object(self.bot.steam_free_games, "close", step(order, "steam")),
            patch("discord.Client.close", step(order, "discord")),
            self.assertLogs(level="ERROR") as logs,
        ):
            await self.bot.close()
        self.assertLess(loop.time() - start, 3)
        self.assertEqual(set(order), {"panel", "access", "codex", "steam", "discord"})
        shutdown_logs = [r.getMessage() for r in logs.records if "shutdown" in r.getMessage()]
        self.assertEqual(shutdown_logs, ["Panel shutdown timed out."])

    async def test_setup_hook_loads_emojis_before_cogs(self) -> None:
        order: list[str] = []
        with (
            patch("src.bot.sync_discord_brand", lambda bot: order.append("brand")),
            patch("src.bot.load_app_emojis", step(order, "emojis")),
            patch.object(self.bot, "add_cogs", step(order, "cogs")),
            patch.object(self.bot.tree, "sync", step(order, "sync")),
            patch("src.bot.install_stack_dump_signal"),
            patch("src.bot.run_watchdog", AsyncMock()),
        ):
            await self.bot.setup_hook()
            await self.bot._watchdog
        self.assertEqual(order, ["brand", "emojis", "cogs", "sync"])

    def test_no_help_command(self) -> None:
        self.assertIsNone(self.bot.help_command)

    async def test_mention_goes_to_ai_not_prefix_commands(self) -> None:
        self.bot._connection.user = MagicMock(id=BOT)
        message = MagicMock(content=f"<@{BOT}> help")
        message.guild.id = G
        with (
            patch.object(self.bot, "process_commands", AsyncMock()) as process_commands,
            patch("src.ai.cog.ai_discord.handle_message", AsyncMock()) as handle_message,
        ):
            await self.bot.on_message(message)
            await AiCog(self.bot).on_message(message)
        process_commands.assert_not_awaited()
        handle_message.assert_awaited_once()

    async def test_stuck_stage_only_cut_off_by_its_own_limit(self) -> None:
        order: list[str] = []
        await self.bot.add_cog(AiCog(self.bot), guild=discord.Object(G))
        await self.bot.add_cog(SteamCog(self.bot), guild=discord.Object(G))
        self.bot.codex.close = step(order, "codex", stuck=True)
        loop = asyncio.get_running_loop()
        start = loop.time()
        with (
            patch("src.tasks.SHUTDOWN_STAGE_SECONDS", 0.5),
            patch.object(self.bot.steam_free_games, "close", step(order, "steam")),
            patch("discord.Client.close", step(order, "discord")),
            self.assertLogs(level="ERROR") as logs,
        ):
            await self.bot.close()
        self.assertLess(loop.time() - start, 3)
        self.assertEqual(set(order), {"codex", "steam", "discord"})
        self.assertEqual([r.getMessage() for r in logs.records], ["Codex shutdown timed out."])


class HoroShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        codex = MagicMock()
        codex.request_counts.return_value = (0, 0)
        codex.start = AsyncMock()
        codex.close = AsyncMock()
        codex.cancel = AsyncMock()
        self.db = testing.memory_db(self)
        self.bot = HoroBot(codex, guild_id=G, db=self.db)
        self.bot._shutdown_failstop = MagicMock(side_effect=testing.Fatal)

    async def test_accepted_mutation_finishes_before_transport_shutdown(self) -> None:
        from src.tasks import track

        self.db.execute("CREATE TABLE shutdown_check (value INTEGER)")
        order = []
        gate = asyncio.Event()

        async def accepted() -> None:
            await gate.wait()
            self.db.execute("INSERT INTO shutdown_check VALUES (1)")
            self.assertFalse(self.bot._db_safe_to_close)
            order.append("commit")

        owner = track(self.bot.admin_panels.mutations, asyncio.create_task(accepted()))
        asyncio.get_running_loop().call_later(0.03, gate.set)
        with patch("discord.Client.close", step(order, "discord")):
            await self.bot.close()
        self.assertEqual(order, ["commit", "discord"])
        self.assertTrue(owner.done())
        self.assertTrue(self.bot._db_safe_to_close)
        self.assertEqual(self.db.execute("SELECT value FROM shutdown_check").fetchone(), (1,))
        self.bot.codex.close.assert_awaited_once()

    async def test_operation_owners_drain_before_transport_shutdown(self) -> None:
        gate, entered = asyncio.Event(), asyncio.Event()
        order = []
        self.db.execute("CREATE TABLE shutdown_check (owner TEXT)")

        async def game_action(*_args, **_kwargs):
            order.append("game")
            entered.set()
            await gate.wait()
            self.db.execute("INSERT INTO shutdown_check VALUES (?)", ("game",))

        with (
            patch.object(self.bot.game_operations, "_execute", side_effect=game_action),
            patch("discord.Client.close", step(order, "discord")),
        ):
            game = asyncio.create_task(self.bot.game_operations.execute(None, "deal"))
            async with asyncio.timeout(2):
                await entered.wait()
            self.assertIn(game, self.bot.game_operations.pending_tasks())
            asyncio.get_running_loop().call_soon(gate.set)
            await self.bot.close()
            await game
        self.assertEqual(order[-1], "discord")
        self.assertEqual(self.db.execute("SELECT owner FROM shutdown_check").fetchall(), [("game",)])
        self.assertEqual(self.bot.game_operations.pending_tasks(), set())
        self.assertTrue(self.bot._db_safe_to_close)

    async def test_reentrant_close_and_caller_cancellation_share_one_owner(self) -> None:
        gate = asyncio.Event()
        owner = self.bot.admin_panels.own_mutation(asyncio.create_task(gate.wait()))
        with patch("discord.Client.close", AsyncMock()) as transport:
            first = asyncio.create_task(self.bot.close())
            await asyncio.sleep(0)
            shared = self.bot._close_task
            second = asyncio.create_task(self.bot.close())
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(shared.done())
            gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await first
            await second
        self.assertIs(self.bot._close_task, shared)
        self.assertTrue(owner.done())
        transport.assert_awaited_once()
        self.assertTrue(self.bot._db_safe_to_close)

    async def test_late_card_keeps_cleanup_owner_and_discord_open(self) -> None:
        from src.ai.output import CARD_CLEANUPS, _delete_when_landed
        from src.tasks import track

        order = []
        message = MagicMock(delete=AsyncMock(side_effect=lambda: order.append("delete")))

        async def send():
            await asyncio.sleep(0.4)
            return message

        sending = asyncio.create_task(send())
        cleanup = track(CARD_CLEANUPS, asyncio.create_task(_delete_when_landed(sending)))
        with (patch("src.tasks.SHUTDOWN_SECONDS", 1),
              patch("src.tasks.SHUTDOWN_STAGE_SECONDS", 0.7),
              patch("discord.Client.close", step(order, "discord"))):
            await self.bot.close()
        self.assertEqual(order, ["delete", "discord"])
        self.assertTrue(sending.done())
        self.assertFalse(cleanup.cancelled())
        self.assertTrue(self.bot._db_safe_to_close)

    async def test_stubborn_writer_forces_exit_at_total_deadline_without_closing_database(self) -> None:
        gate = asyncio.Event()

        async def stubborn() -> None:
            while not gate.is_set():
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    pass

        owner = self.bot.admin_panels.own_mutation(asyncio.create_task(stubborn()))
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            with (patch("src.tasks.SHUTDOWN_SECONDS", 0.12),
                  patch("src.tasks.SHUTDOWN_STAGE_SECONDS", 0.03),
                  patch("discord.Client.close", AsyncMock()), patch("logging.error"),
                  self.assertRaises(testing.Fatal)):
                await self.bot.close()
            self.assertLess(loop.time() - started, 0.5)
            self.assertFalse(self.bot._db_safe_to_close)
            self.assertFalse(owner.done())
            self.assertEqual(self.db.execute("SELECT 1").fetchone(), (1,))
        finally:
            gate.set()
            await owner

    async def test_simultaneous_stages_share_total_deadline(self) -> None:
        from src.tasks import shutdown_deadline

        await self.bot.add_cogs()
        deadlines = []
        all_started = asyncio.Event()

        async def hang(**_kwargs):
            deadlines.append(shutdown_deadline.get())
            if len(deadlines) == 4:
                all_started.set()
            await all_started.wait()

        with (patch("src.tasks.SHUTDOWN_SECONDS", 1),
              patch("src.tasks.SHUTDOWN_STAGE_SECONDS", 0.5),
              patch.object(self.bot.admin_panels, "close", hang),
              patch.object(self.bot.access_service, "close", hang),
              patch.object(self.bot.codex, "close", hang),
              patch.object(self.bot.steam_free_games, "close", hang),
              patch("discord.Client.close", AsyncMock()), self.assertLogs(level="ERROR")):
            await self.bot.close()
        self.assertEqual(len(deadlines), 4)
        self.assertEqual(len(set(deadlines)), 1)
        self.assertIsNotNone(deadlines[0])
        self.assertTrue(self.bot._db_safe_to_close)


class MainResourceTests(unittest.TestCase):
    def test_main_closes_database_and_lock_after_safe_shutdown(self) -> None:
        from src.bot import main
        from types import SimpleNamespace

        db, lock = MagicMock(), MagicMock()
        config = SimpleNamespace(guild_id=G, codex_token="fixture", discord_token="fixture")
        bot = MagicMock(_db_safe_to_close=True)
        with (patch("src.bot.AppConfig.from_env", return_value=config),
              patch("logging.basicConfig"),
              patch("src.bot.hold_lock", return_value=lock), patch("src.bot.open_db", return_value=db),
              patch("src.bot.HoroBot", return_value=bot)):
            main()
        db.close.assert_called_once()
        lock.close.assert_called_once()

    def test_main_retains_database_and_lock_when_owners_are_not_finished(self) -> None:
        from src.bot import main
        from types import SimpleNamespace

        db, lock = MagicMock(), MagicMock()
        config = SimpleNamespace(guild_id=G, codex_token="fixture", discord_token="fixture")
        bot = MagicMock(_db_safe_to_close=False)
        with (patch("src.bot.AppConfig.from_env", return_value=config),
              patch("logging.basicConfig"),
              patch("src.bot.hold_lock", return_value=lock), patch("src.bot.open_db", return_value=db),
              patch("src.bot.HoroBot", return_value=bot), patch("os._exit", side_effect=testing.Fatal),
              patch("logging.shutdown"), self.assertLogs(level="CRITICAL"), self.assertRaises(testing.Fatal)):
            main()
        db.close.assert_not_called()
        lock.close.assert_not_called()
