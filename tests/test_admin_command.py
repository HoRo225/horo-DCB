from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.admin_panel import AdminPanelView
from src.bot import HoroBot
from src.codex_bridge_client import CodexAccess, CodexRuntimeStatus
from src.server_activity import ServerActivityStatus
from src.steam_free_games import SteamGuildStatus
from src.temp_voice import TempVoiceGuildStatus


class AdminCommandTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_bot(codex=None, server_activity=None):
        return HoroBot(
            codex or SimpleNamespace(get_runtime_status=AsyncMock()),
            CodexAccess(True, 10, 20, frozenset({1})),
            SimpleNamespace(),
            SimpleNamespace(),
            server_activity=server_activity,
        )

    async def test_control_panel_command_is_registered_for_administrators(self):
        bot = self.make_bot()

        command = bot.tree.get_command("控制台")

        self.assertIsNotNone(command)
        self.assertEqual(command.description, "開啟管理控制台")
        self.assertTrue(command.guild_only)
        self.assertTrue(command.default_permissions.administrator)
        self.assertFalse(bot.allowed_mentions.everyone)
        self.assertFalse(bot.allowed_mentions.users)
        self.assertFalse(bot.allowed_mentions.roles)
        self.assertFalse(bot.allowed_mentions.replied_user)

    async def test_control_panel_initial_response_is_ephemeral_components_v2(self):
        status = CodexRuntimeStatus(
            True, True, "free", "0.147.0", "0.147.0", "live", 1
        )
        codex = SimpleNamespace(
            get_runtime_status=AsyncMock(return_value=status),
        )
        temp_voice = SimpleNamespace(
            get_guild_status=lambda _guild_id: TempVoiceGuildStatus(True, 123, 1)
        )
        steam = SimpleNamespace(
            get_guild_status=lambda _guild_id: SteamGuildStatus(True, 900, 456, 1)
        )
        server_activity = SimpleNamespace(
            get_runtime_status=lambda: ServerActivityStatus(True, 0, 100, 0)
        )
        bot = HoroBot(
            codex,
            CodexAccess(True, 10, 20, frozenset({1})),
            temp_voice,
            steam,
            server_activity=server_activity,
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            permissions=SimpleNamespace(administrator=True),
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        command = bot.tree.get_command("控制台")
        await command.callback(interaction)

        codex.get_runtime_status.assert_awaited_once_with()
        kwargs = interaction.response.send_message.await_args.kwargs
        self.assertTrue(kwargs["ephemeral"])
        self.assertIsInstance(kwargs["view"], AdminPanelView)
        self.assertIs(kwargs["view"].server_activity, server_activity)
        self.assertTrue(kwargs["view"].has_components_v2())
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].users)
        self.assertFalse(kwargs["allowed_mentions"].roles)


if __name__ == "__main__":
    unittest.main()
