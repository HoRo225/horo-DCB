import asyncio
import unittest

from src.ai import ModelError
from tests.support.admin import LOGGED_OUT, READY, PanelCase
from tests.support.testing import text


class CodexLoginTests(PanelCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.codex.get_runtime_status.return_value = LOGGED_OUT
        await self.open_panel()
        await self.go("樂奈")

    async def test_device_code_is_shown_then_the_page_redraws_when_login_finishes(self) -> None:
        self.assertIn("尚未登入。按「登入」取得裝置碼", text(self.view))
        clicked = await self.click(self.button("登入"))
        shown = self.screen(clicked)
        self.assertIn("1. 開啟 https://auth.openai.com/codex/device\n2. 輸入代碼 `ABCD-1234`", shown)
        self.button("取消登入")
        self.codex.get_runtime_status.return_value = READY
        self.codex.login_finished.set_result(True)
        await self.settle()
        redrawn = clicked.edit_original_response.await_args.kwargs["view"]
        self.assertIs(redrawn, self.view)
        self.assertIn("✅ 已登入 Codex。", text(redrawn))
        self.assertIn("已登入 ChatGPT 帳號。", text(redrawn))
        self.button("登出")
        self.codex.login_cancel.assert_not_awaited()

    async def test_unfinished_login_asks_to_try_again(self) -> None:
        clicked = await self.click(self.button("登入"))
        self.codex.login_finished.set_result(False)
        await self.settle()
        self.assertIn("登入未完成，請重新按「登入」。", text(clicked.edit_original_response.await_args.kwargs["view"]))
        self.button("登入")

    async def test_login_that_cannot_start_says_so(self) -> None:
        self.codex.login_start.side_effect = ModelError("down")
        shown = self.screen(await self.click(self.button("登入")))
        self.assertIn("目前無法開始登入，請稍後再試。", shown)
        self.button("登入")

    async def test_login_is_cancelled_by_button_or_closing_the_panel(self) -> None:
        await self.click(self.button("登入"))
        clicked = await self.click(self.button("取消登入"))
        self.codex.login_cancel.assert_awaited_once_with("login-1")
        self.assertIn("已取消登入。", self.screen(clicked))
        await self.click(self.button("登入"))
        await self.admin.panels.close(deadline=asyncio.get_running_loop().time() + 5)
        self.assertEqual(self.codex.login_cancel.await_count, 2)


class CodexLogoutTests(PanelCase):
    async def test_logout_asks_for_confirmation_first(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        shown = self.screen(await self.click(self.button("登出")))
        self.assertIn("### 確認登出 Codex", shown)
        self.codex.logout.assert_not_awaited()
        self.assertNotIn("確認登出 Codex", self.screen(await self.click(self.button("取消"))))
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
