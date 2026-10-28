import unittest

from src.core.adapters.sqlite.db import transaction
from src.steam import SteamOffer
from src.steam.adapters.sqlite.store import STEAM_CHANNEL_ID, STEAM_ROLE_IDS
from tests.support.admin import PanelCase, resolved, text_channel
from tests.support.steam import fetched
from tests.support.testing import put_setting

ROLES = "選擇通知身分組（清空即取消）"


class SteamPageTests(PanelCase):
    async def test_query_renders_each_offer_with_its_price(self) -> None:
        await self.open_panel()
        await self.go("Steam 免費遊戲")
        self.admin.offers.result = fetched(SteamOffer(10, "Test Game", "NT$100", "", (), None))
        shown = self.screen(await self.click(self.button("重新查詢")))
        self.assertIn("**Test Game**\n~~NT$100~~ → **免費**", shown)

    async def test_query_without_data_says_steam_is_unreachable(self) -> None:
        await self.open_panel()
        await self.go("Steam 免費遊戲")
        self.admin.offers.result = None
        shown = self.screen(await self.click(self.button("重新查詢")))
        self.assertIn("### 查詢結果\n目前無法取得 Steam 資料。", shown)

    async def test_page_has_no_pending_items_button(self) -> None:
        with transaction(self.db):
            self.admin.operations.create_effect("e1", "action.receipt", 700, {})
        await self.open_panel()
        await self.go("Steam 免費遊戲")
        self.assertEqual(self.labels(), ["返回總覽", "開啟 Steam 自動通知", "重新查詢", "重新整理"])
        self.assertIn("待處理項目", [option.label for option in self.select("前往其他頁面…").options])

    async def test_toggle_and_notification_roles(self) -> None:
        self.channels[700] = text_channel(700, self.guild)
        put_setting(self.db, STEAM_CHANNEL_ID, 700)
        await self.open_panel()
        await self.go("Steam 免費遊戲")
        steam, role = self.admin.steam, self.role(900)
        roles = self.select(ROLES)
        self.assertFalse(roles.disabled)
        shown = self.screen(await self.choose(roles, role))
        self.assertEqual(steam.status().role_ids, (900,))
        self.assertIn("已更新 Steam 免費遊戲通知身分組，共 1 個。", shown)
        self.assertIn("### 通知狀態\n⚪ 已關閉\n", shown)
        self.assertIn("⚪ **此功能目前關閉**　按下方的開啟按鈕即可開啟；設定仍可修改。", shown)
        self.assertIn("通知 <#700>\n通知身分組 <@&900>", shown)

        clicked = await self.click(self.button("開啟 Steam 自動通知"))
        self.assertTrue(steam.enabled)
        shown = self.screen(clicked)
        self.assertIn("✅ 已開啟 Steam 自動通知。", shown)
        self.assertIn("### 通知狀態\n🟢 已開啟\n", shown)
        self.assertNotIn("此功能目前關閉", shown)
        self.assertFalse(roles.disabled)

        shown = self.screen(await self.choose(roles))
        self.assertEqual(steam.status().role_ids, ())
        self.assertIn("已取消 Steam 免費遊戲通知身分組。", shown)

        self.view.toggled_at.clear()
        clicked = await self.click(self.button("關閉 Steam 自動通知"))
        self.assertFalse(steam.enabled)
        self.assertIn("✅ 已關閉 Steam 自動通知。", self.screen(clicked))
        self.assertFalse(roles.disabled)

    async def test_deleted_notification_targets_are_named_by_id(self) -> None:
        put_setting(self.db, STEAM_CHANNEL_ID, 700)
        put_setting(self.db, STEAM_ROLE_IDS, [900, 901])
        self.role(900)
        await self.open_panel()
        shown = self.screen(await self.go("Steam 免費遊戲"))
        self.assertIn("通知 （已刪除）ID 700\n通知身分組 <@&900>、（已刪除）ID 901", shown)

    async def test_channel_select_binds_the_notification_channel(self) -> None:
        self.channels[700] = channel = text_channel(700, self.guild)
        await self.open_panel()
        await self.go("Steam 免費遊戲")
        shown = self.screen(await self.choose(self.select("選擇 Steam 通知頻道"), resolved(channel)))
        self.assertEqual(self.admin.steam.status().channel_id, 700)
        self.assertIn("已綁定 Steam 免費遊戲通知頻道。", shown)


if __name__ == "__main__":
    unittest.main()
