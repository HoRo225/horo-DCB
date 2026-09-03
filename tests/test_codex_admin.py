from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.admin_panel import AdminPanelView
from src.codex_bridge_client import CodexRuntimeStatus


class CodexAdminPanelTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.status = CodexRuntimeStatus(
            True,
            True,
            "free",
            "0.147.0",
            "0.147.0",
            "live",
            3,
        )
        self.client = SimpleNamespace(
            get_runtime_status=AsyncMock(return_value=self.status)
        )
        voice = SimpleNamespace(
            get_guild_status=lambda _guild_id: SimpleNamespace(
                state_available=True,
                parent_channel_id=1,
                child_count=0,
            )
        )
        steam = SimpleNamespace(
            get_guild_status=lambda _guild_id: SimpleNamespace(
                state_available=True,
                channel_id=2,
                active_app_count=0,
                poll_interval_seconds=900,
                role_ids=(),
            )
        )
        self.view = AdminPanelView(
            user_id=1,
            guild_id=10,
            codex_client=self.client,
            codex_status=self.status,
            temp_voice=voice,
            steam_free_games=steam,
            server_activity=None,
        )

    def test_ai_page_reports_codex_runtime_without_legacy_router_fields(self):
        self.view._render_ai()

        rendered = repr(self.view.to_components())

        self.assertIn("Codex", rendered)
        self.assertIn("Free", rendered)
        self.assertIn("0.147.0", rendered)
        self.assertIn("Live", rendered)
        self.assertIn("3", rendered)
        self.assertNotIn("9Router", rendered)
        self.assertNotIn("Agent", rendered)

    async def test_refresh_uses_codex_client(self):
        await self.view._refresh_ai_status()

        self.client.get_runtime_status.assert_awaited_once_with()
        self.assertIs(self.view.codex_status, self.status)


if __name__ == "__main__":
    unittest.main()
