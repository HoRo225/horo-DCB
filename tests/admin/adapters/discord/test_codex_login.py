import asyncio
import re
import time
import unittest
from unittest.mock import AsyncMock

import discord

from src.admin.adapters.discord.logins import CodexLogins
from src.ai import ModelError
from src.core.adapters.discord.brand import ICONS
from src.core.app.supervisor import WORK, Supervisor
from tests.support.admin import LOGGED_OUT, LOGIN, OWNER, READY, FakeCodex, PanelCase
from tests.support.testing import text

OTHER = OWNER + 1


class CodexLoginTests(PanelCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.codex.get_runtime_status.return_value = LOGGED_OUT
        await self.open_panel()
        await self.go("樂奈")

    def finish(self, success: bool) -> None:
        if success:
            self.codex.get_runtime_status.return_value = READY
        self.codex.login_finished.set_result(success)

    def redrawn(self, interaction) -> str:
        return text(interaction.edit_original_response.await_args.kwargs["view"])

    async def test_device_code_and_expiry_are_shown_then_the_page_redraws_when_login_finishes(self) -> None:
        self.assertIn("尚未登入。按「登入」取得裝置碼", text(self.view))
        clicked = await self.click(self.button("登入"))
        shown = self.screen(clicked)
        self.assertIn("1. 開啟 https://auth.openai.com/codex/device\n2. 輸入代碼 `ABCD-1234`", shown)
        expires = int(re.search(r"代碼將在 <t:(\d+):R> 失效", shown).group(1))
        self.assertAlmostEqual(expires, time.time() + 900, delta=5)
        self.button("取消登入")
        self.finish(True)
        await self.settle()
        self.assertIs(clicked.edit_original_response.await_args.kwargs["view"], self.view)
        self.assertIn("✅ 已登入 Codex。", self.redrawn(clicked))
        self.assertIn("已登入 ChatGPT 帳號。", self.redrawn(clicked))
        self.button("登出")
        self.codex.login_cancel.assert_not_awaited()

    async def test_unfinished_login_asks_to_try_again(self) -> None:
        clicked = await self.click(self.button("登入"))
        self.finish(False)
        await self.settle()
        self.assertIn("登入未完成，請重新按「登入」。", self.redrawn(clicked))
        self.button("登入")

    async def test_login_that_cannot_start_says_so(self) -> None:
        self.codex.login_start.side_effect = ModelError("down")
        shown = self.screen(await self.click(self.button("登入")))
        self.assertIn("目前無法開始登入，請稍後再試。", shown)
        self.button("登入")

    async def test_closing_or_reopening_the_panel_keeps_the_login_waiting(self) -> None:
        await self.click(self.button("登入"))
        await self.view.on_timeout()
        await self.open_panel()
        await self.go("樂奈")
        self.assertIn("輸入代碼 `ABCD-1234`", text(self.view))
        await self.open_panel()
        self.codex.login_cancel.assert_not_awaited()
        self.finish(True)
        await self.settle()
        await self.open_panel()
        await self.go("樂奈")
        self.assertIn("已登入 ChatGPT 帳號。", text(self.view))
        self.codex.login_start.assert_awaited_once()
        self.codex.login_cancel.assert_not_awaited()

    async def test_every_admin_shares_one_login_and_every_open_ai_page_redraws(self) -> None:
        mine = self.view
        await self.open_panel(OTHER)
        await self.go("樂奈", OTHER)
        theirs = self.view
        self.view = mine
        started = await self.click(self.button("登入"))
        self.view = theirs
        joined = await self.click(self.button("登入"), self.interaction(OTHER))
        self.assertIn("1. 開啟 https://auth.openai.com/codex/device\n2. 輸入代碼 `ABCD-1234`", self.screen(joined))
        self.codex.login_start.assert_awaited_once()
        self.finish(True)
        await self.settle()
        for interaction, view in ((started, mine), (joined, theirs)):
            self.assertIs(interaction.edit_original_response.await_args.kwargs["view"], view)
            self.assertIn("✅ 已登入 Codex。", self.redrawn(interaction))

    async def test_cancel_stops_the_login_for_everyone(self) -> None:
        await self.click(self.button("登入"))
        clicked = await self.click(self.button("取消登入"))
        self.codex.login_cancel.assert_awaited_once_with("login-1")
        shown = self.screen(clicked)
        self.assertIn("已取消登入。", shown)
        self.assertNotIn("ABCD-1234", shown)
        await self.open_panel(OTHER)
        await self.go("樂奈", OTHER)
        self.assertNotIn("ABCD-1234", text(self.view))
        await self.click(self.button("登入"), self.interaction(OTHER))
        self.assertEqual(self.codex.login_start.await_count, 2)


class CodexLoginOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.codex = FakeCodex()
        self.logins = CodexLogins(self.codex, Supervisor().group("admin", WORK), seconds=0.2)
        self.finished: list[bool] = []
        self.logins.subscribe(self.finished.append)

    async def asyncTearDown(self) -> None:
        await self.logins.close(deadline=asyncio.get_running_loop().time() + 5)

    async def test_login_waits_out_the_code_window_then_cancels_the_code(self) -> None:
        pending = await self.logins.start()
        self.assertEqual(pending.login, LOGIN)
        self.assertAlmostEqual(pending.expires_at, time.time() + 0.2, delta=1)
        await asyncio.sleep(0.05)
        self.assertIs(self.logins.pending, pending)
        self.codex.login_cancel.assert_not_awaited()
        await asyncio.sleep(0.4)
        self.assertIsNone(self.logins.pending)
        self.codex.login_cancel.assert_awaited_once_with("login-1")
        self.assertEqual(self.finished, [False])

    async def test_concurrent_starts_share_one_login(self) -> None:
        async def slow() -> object:
            await asyncio.sleep(0.01)
            return LOGIN

        self.codex.login_start = AsyncMock(side_effect=slow)
        first, second = await asyncio.gather(self.logins.start(), self.logins.start())
        self.assertIs(first, second)
        self.codex.login_start.assert_awaited_once()


class CodexLogoutTests(PanelCase):
    async def test_logout_asks_for_confirmation_first(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        shown = self.screen(await self.click(self.button("登出")))
        self.assertIn(f"### {ICONS['warn']} 確認登出 Codex\n登出後 AI 功能會停止，直到再次登入。", shown)
        self.assertEqual(self.button("確認登出").style, discord.ButtonStyle.danger)
        self.codex.logout.assert_not_awaited()
        shown = self.screen(await self.click(self.button("取消")))
        self.assertNotIn("確認登出 Codex", shown)
        self.assertIn("已登入 ChatGPT 帳號。", shown)
        self.codex.logout.assert_not_awaited()
        await self.click(self.button("登出"))
        self.codex.get_runtime_status.return_value = LOGGED_OUT
        clicked = await self.click(self.button("確認登出"))
        self.codex.logout.assert_awaited_once()
        shown = self.screen(clicked)
        self.assertIn("✅ 已登出 Codex。", shown)
        self.assertIn("尚未登入。", shown)

    async def test_failed_logout_keeps_the_account_signed_in(self) -> None:
        self.codex.logout.side_effect = ModelError("down")
        await self.open_panel()
        await self.go("樂奈")
        await self.click(self.button("登出"))
        shown = self.screen(await self.click(self.button("確認登出")))
        self.assertIn("登出失敗，請稍後再試。", shown)
        self.assertIn("已登入 ChatGPT 帳號。", shown)


if __name__ == "__main__":
    unittest.main()
