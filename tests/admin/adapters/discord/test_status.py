import unittest

from src.admin.adapters.discord.status import admin_actions
from src.core.adapters.discord.brand import ICONS
from src.core.app.ports import Actor
from tests.support.testing import text

ADMIN = Actor(1, "管理員", "", frozenset(), True)


class AdminStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_reports_switches_lobby_and_pending_count(self) -> None:
        (status,) = admin_actions(lambda: True, lambda: False, lambda: True, lambda: 2, lambda: None).specs
        rendered = text((await status.effect.render(ADMIN))["view"])
        self.assertEqual(
            rendered, f"## {ICONS['status']} 系統狀態\nAI：開啟\n語音：停用\nSteam：執行中\n遊戲大廳：正常\n待處理：2 項"
        )
        self.assertTrue(status.admin)

    async def test_lobby_problem_is_named(self) -> None:
        (status,) = admin_actions(lambda: True, lambda: True, lambda: True, lambda: 0, lambda: "請先選擇遊戲頻道。").specs
        rendered = text((await status.effect.render(ADMIN))["view"])
        self.assertIn("遊戲大廳：需要處理（請先選擇遊戲頻道）\n", rendered)


if __name__ == "__main__":
    unittest.main()
