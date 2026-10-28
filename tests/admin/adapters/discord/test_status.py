import unittest

from src.admin.adapters.discord.status import admin_actions
from src.core.adapters.discord.brand import ICONS
from src.core.app.ports import Actor
from src.testing import text

ADMIN = Actor(1, "管理員", "", frozenset(), True)


class AdminStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_switches_and_pending_count(self) -> None:
        (status,) = admin_actions(lambda: True, lambda: False, lambda: True, lambda: 2).specs
        rendered = text((await status.effect.render(ADMIN))["view"])
        self.assertEqual(rendered, f"## {ICONS['status']} 系統狀態\nAI：開啟\n語音：停用\nSteam：執行中\n待處理：2 項")
        self.assertTrue(status.admin)


if __name__ == "__main__":
    unittest.main()
