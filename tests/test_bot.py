import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from src.ai.adapters.claude.service import ClaudeService
from src.bot import HoroBot, main
from src.core.app.supervisor import WORK
from src.games.adapters.discord.views import lobby_view
from src.games.domain.catalog import GAMES
from src.steam import SteamOffer
from src.steam.adapters.discord.views import build_offer_view
from tests.support import testing

G, BOT = 100, 999
COGS = ["GamesCog", "VoiceCog", "AdminCog", "AiCog", "SteamCog"]
COMMANDS = {
    "控制台": {
        "name": "控制台",
        "description": "開啟管理控制台",
        "type": 1, "options": [], "nsfw": False, "dm_permission": False,
        "default_member_permissions": 8, "contexts": [0], "integration_types": None,
    },
}


def fake_model() -> MagicMock:
    model = MagicMock()
    model.start = AsyncMock()
    model.close = AsyncMock()
    model.cancel = AsyncMock()
    return model


def step(order: list[str], name: str):
    async def run(*args: object, **kwargs: object) -> None:
        order.append(name)

    return run


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.db = testing.memory_db(self)
        self.bot = HoroBot(fake_model(), guild_id=G, db=self.db)

    async def start(self) -> None:
        await self.enterAsyncContext(self.bot)
        await self.bot.add_cogs()

    async def test_guild_gets_the_feature_cogs_and_the_control_panel_command(self) -> None:
        await self.start()
        tree = self.bot.tree
        self.assertEqual(list(self.bot.cogs), COGS)
        self.assertEqual({c.name: c.to_dict(tree) for c in tree.get_commands(guild=discord.Object(G))}, COMMANDS)
        self.assertEqual(tree.get_commands(), [])
        self.assertIsNone(self.bot.help_command)

    async def test_every_persistent_custom_id_reaches_a_registered_handler(self) -> None:
        await self.start()
        patterns = self.bot._connection._view_store._dynamic_items
        views = [lobby_view("HoRo"), build_offer_view(SteamOffer(730, "G", "", "", None))]
        ids = {custom_id for view in views for item in view.walk_children()
               if (custom_id := getattr(item, "custom_id", None))}
        self.assertIn("horo:game:pick", ids)
        for custom_id in ids | {f"horo:game:rules:{key}" for key in GAMES}:
            with self.subTest(custom_id):
                self.assertTrue(any(pattern.fullmatch(custom_id) for pattern in patterns))

    async def test_setup_loads_emojis_before_cogs_and_syncs_commands_last(self) -> None:
        order: list[str] = []
        with (
            patch("src.bot.sync_discord_brand", lambda bot: order.append("brand")),
            patch("src.bot.load_app_emojis", step(order, "emojis")),
            patch.object(self.bot, "add_cogs", step(order, "cogs")),
            patch.object(self.bot.tree, "sync", step(order, "sync")),
            patch("src.bot.install_stack_dump_signal"),
            patch("src.bot.run_watchdog", AsyncMock()) as watchdog,
            patch.object(self.bot.runtime.maintenance, "start") as maintenance,
        ):
            await self.bot.setup_hook()
            await asyncio.sleep(0)
        self.assertEqual(order, ["brand", "emojis", "cogs", "sync"])
        watchdog.assert_awaited_once_with(self.bot)
        maintenance.assert_called_once_with()

    async def test_mentions_never_run_prefix_commands(self) -> None:
        self.bot._connection.user = MagicMock(id=BOT)
        message = MagicMock(content=f"<@{BOT}> help")
        message.guild.id = G
        with patch.object(self.bot, "process_commands", AsyncMock()) as process_commands:
            await self.bot.on_message(message)
        process_commands.assert_not_awaited()


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.model = fake_model()
        self.db = testing.memory_db(self)
        self.bot = HoroBot(self.model, guild_id=G, db=self.db)

    async def test_close_removes_cogs_and_their_commands(self) -> None:
        await self.enterAsyncContext(self.bot)
        await self.bot.add_cogs()
        await self.bot.close()
        self.assertEqual(dict(self.bot.cogs), {})
        self.assertEqual(self.bot.tree.get_commands(guild=discord.Object(G)), [])

    async def test_accepted_work_commits_before_discord_closes_and_then_the_database_may_close(self) -> None:
        self.db.execute("CREATE TABLE shutdown_check (value INTEGER)")
        order: list[str] = []
        gate = asyncio.Event()

        async def accepted() -> None:
            await gate.wait()
            self.db.execute("INSERT INTO shutdown_check VALUES (1)")
            self.assertFalse(self.bot.db_safe_to_close)
            order.append("commit")

        owner = self.bot.runtime.supervisor.group("admin", WORK).spawn(accepted())
        asyncio.get_running_loop().call_later(0.03, gate.set)
        with patch("discord.Client.close", step(order, "discord")):
            await self.bot.close()
        self.assertEqual(order, ["commit", "discord"])
        self.assertTrue(owner.done())
        self.assertTrue(self.bot.db_safe_to_close)
        self.assertEqual(self.db.execute("SELECT value FROM shutdown_check").fetchone(), (1,))
        self.model.cancel.assert_awaited_once_with(G)
        self.model.close.assert_awaited_once()

class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db, self.lock = MagicMock(), MagicMock()

    def run_main(self, *, safe: bool) -> None:
        config = SimpleNamespace(guild_id=G, discord_token="fixture", claude_password="fixture")
        with (
            patch("src.bot.AppConfig.from_env", return_value=config),
            patch("logging.basicConfig"),
            patch("src.bot.hold_lock", return_value=self.lock),
            patch("src.bot.open_db", return_value=self.db),
            patch("src.bot.HoroBot", return_value=MagicMock(db_safe_to_close=safe)) as bot,
        ):
            main()
        self.assertIsInstance(bot.call_args.args[0], ClaudeService)

    def test_database_and_lock_close_after_a_safe_shutdown(self) -> None:
        self.run_main(safe=True)
        self.db.close.assert_called_once()
        self.lock.close.assert_called_once()

    def test_database_and_lock_are_retained_when_owners_are_unfinished(self) -> None:
        with (
            patch("os._exit", side_effect=testing.Fatal), patch("logging.shutdown"),
            self.assertLogs(level="CRITICAL"), self.assertRaises(testing.Fatal),
        ):
            self.run_main(safe=False)
        self.db.close.assert_not_called()
        self.lock.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
