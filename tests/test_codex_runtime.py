from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.bot import HoroBot, main
from src.codex_bridge_client import CodexAccess


class CodexRuntimeWiringTest(unittest.TestCase):
    def test_main_wires_codex_without_legacy_ai_services(self):
        config = SimpleNamespace(
            discord_token="discord",
            codex_enabled=True,
            codex_allowed_guild_id=10,
            codex_allowed_channel_id=20,
            codex_allowed_user_ids=frozenset({30}),
            codex_bridge_token="a" * 64,
            server_activity_enabled=False,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
            ai_text_display_enabled=True,
        )

        with (
            patch("src.bot.AppConfig.from_env", return_value=config),
            patch("src.bot.CodexBridgeClient") as codex_class,
            patch("src.bot.TempVoiceManager") as voice_class,
            patch("src.bot.SteamFreeGamesNotifier") as steam_class,
            patch("src.bot.CalendarManager") as calendar_class,
            patch("src.bot.ServerActivityMonitor") as activity_class,
            patch("src.bot.HoroBot") as bot_class,
        ):
            main()

        codex_class.assert_called_once_with(
            "http://codex:8765",
            config.codex_bridge_token,
        )
        activity_class.assert_not_called()
        bot_class.assert_called_once_with(
            codex_class.return_value,
            CodexAccess(True, 10, 20, frozenset({30})),
            voice_class.return_value,
            steam_class.return_value,
            calendar=calendar_class.return_value,
            server_activity=None,
            ai_text_display_enabled=True,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
        )
        bot_class.return_value.run.assert_called_once_with(
            "discord",
            log_handler=None,
        )


class CodexBotLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def test_temp_voice_and_steam_default_to_dormant(self):
        codex = SimpleNamespace(start=AsyncMock())
        temp_voice = SimpleNamespace(reconcile=AsyncMock())
        steam = SimpleNamespace(start=Mock())
        calendar = SimpleNamespace(
            persistent_board_view=lambda: object(),
            start=AsyncMock(),
        )
        bot = HoroBot(
            codex,
            CodexAccess(True, 10, 20, frozenset({30})),
            temp_voice,
            steam,
            calendar,
        )
        bot.tree.sync = AsyncMock()
        bot.add_view = Mock()

        await bot.setup_hook()
        await bot.on_ready()

        steam.start.assert_not_called()
        temp_voice.reconcile.assert_not_awaited()

    async def test_setup_starts_codex_and_close_closes_it(self):
        codex = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        calendar = SimpleNamespace(
            persistent_board_view=lambda: object(),
            start=AsyncMock(),
            close=AsyncMock(),
        )
        steam = SimpleNamespace(start=lambda _bot: None, close=AsyncMock())
        bot = object.__new__(HoroBot)
        bot.codex = codex
        bot.server_activity = None
        bot.calendar = calendar
        bot.steam_free_games = steam
        bot.steam_free_games_enabled = False
        bot.tree = SimpleNamespace(sync=AsyncMock())
        bot.add_view = lambda _view: None

        await HoroBot.setup_hook(bot)
        with patch("src.bot.discord.Client.close", AsyncMock()) as discord_close:
            await HoroBot.close(bot)

        codex.start.assert_awaited_once_with()
        codex.close.assert_awaited_once_with()
        calendar.close.assert_awaited_once_with()
        discord_close.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
