import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.admin.panel import AdminPanelView
from src.bot import HoroBot
from src.ai.access import CodexAccess
from src.ai.protocol import CodexRuntimeStatus
from src.steam.notifier import SteamGuildStatus
from src.voice.manager import TempVoiceGuildStatus
from tests.support.access import configured_access


class AdminCommandTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_bot(codex=None):
        return HoroBot(
            codex or SimpleNamespace(get_runtime_status=AsyncMock()),
            configured_access(True, 10, channel_ids=(20,)),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
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

    async def test_control_panel_defers_before_delayed_status_and_edits_full_v2_panel(self):
        status_started = asyncio.Event()
        release_status = asyncio.Event()

        async def delayed_failure():
            status_started.set()
            await release_status.wait()
            raise RuntimeError("sidecar unavailable")

        codex = SimpleNamespace(
            get_runtime_status=AsyncMock(side_effect=delayed_failure),
        )
        temp_voice = SimpleNamespace(
            get_guild_status=lambda _guild_id: TempVoiceGuildStatus(True, 123, 1)
        )
        steam = SimpleNamespace(
            get_guild_status=lambda _guild_id: SteamGuildStatus(True, 900, 456, 1)
        )
        access = configured_access(True, 10, channel_ids=(20,))
        bot = HoroBot(
            codex,
            access,
            temp_voice,
            steam,
            SimpleNamespace(),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=10),
            permissions=SimpleNamespace(administrator=True),
            user=SimpleNamespace(
                id=1,
                roles=[SimpleNamespace(id=70), SimpleNamespace(id=80)],
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        command = bot.tree.get_command("控制台")
        callback = asyncio.create_task(command.callback(interaction))
        try:
            await asyncio.wait_for(status_started.wait(), timeout=1)
            interaction.response.defer.assert_awaited_once_with(ephemeral=True)
            interaction.edit_original_response.assert_not_awaited()
            self.assertFalse(callback.done())
        finally:
            release_status.set()
            await asyncio.wait_for(
                asyncio.gather(callback, return_exceptions=True),
                timeout=1,
            )
        callback.result()

        codex.get_runtime_status.assert_awaited_once_with()
        kwargs = interaction.edit_original_response.await_args.kwargs
        self.assertIsInstance(kwargs["view"], AdminPanelView)
        self.assertFalse(hasattr(kwargs["view"], "server_activity"))
        self.assertIs(kwargs["view"].codex_access, access)
        self.assertEqual(kwargs["view"].user_role_ids, frozenset({70, 80}))
        self.assertTrue(kwargs["view"].has_components_v2())
        rendered = repr(kwargs["view"].to_components())
        self.assertIn("AI 助手", rendered)
        self.assertIn("臨時語音", rendered)
        self.assertIn("Steam 免費遊戲", rendered)
        self.assertNotIn("伺服器活動", rendered)
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertFalse(kwargs["allowed_mentions"].users)
        self.assertFalse(kwargs["allowed_mentions"].roles)


if __name__ == "__main__":
    unittest.main()
