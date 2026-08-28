from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from src.admin_panel import AdminPanelView
from src.ai_client import AIRuntimeStatus
from src.bot import HoroBot
from src.server_activity import ServerActivityStatus
from src.steam_free_games import SteamGuildStatus
from src.temp_voice import TempVoiceGuildStatus


class AdminCommandTest(unittest.IsolatedAsyncioTestCase):
    async def test_control_panel_command_is_registered_for_administrators(self):
        bot = HoroBot(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            ai_client=SimpleNamespace(),
        )

        command = bot.tree.get_command("控制台")

        self.assertIsNotNone(command)
        self.assertEqual(command.description, "開啟管理控制台")
        self.assertTrue(command.guild_only)
        self.assertIsNotNone(command.default_permissions)
        self.assertTrue(command.default_permissions.administrator)
        self.assertFalse(bot.allowed_mentions.everyone)
        self.assertFalse(bot.allowed_mentions.users)
        self.assertFalse(bot.allowed_mentions.roles)
        self.assertFalse(bot.allowed_mentions.replied_user)

    async def test_control_panel_initial_response_is_ephemeral_components_v2(self):
        ai_status = AIRuntimeStatus("GPT 5.6 Luna", "auto", True, "0.5.55")
        ai_client = SimpleNamespace(
            model="horo-main",
            get_runtime_status=AsyncMock(return_value=ai_status),
        )
        chat = SimpleNamespace(
            history_limit=50,
            context_char_limit=16_000,
            cooldown_seconds=5.0,
            agent_timeout_seconds=120.0,
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
            chat,
            temp_voice,
            steam,
            server_activity=server_activity,
            ai_client=ai_client,
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            permissions=SimpleNamespace(administrator=True),
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        command = bot.tree.get_command("控制台")
        await command.callback(interaction)

        ai_client.get_runtime_status.assert_awaited_once_with()
        interaction.response.send_message.assert_awaited_once()
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
