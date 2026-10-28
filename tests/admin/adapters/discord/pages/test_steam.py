import unittest

from src.steam import SteamOffer
from src.steam.adapters.sqlite.store import STEAM_CHANNEL_ID
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

    async def test_toggle_and_notification_roles(self) -> None:
        self.channels[700] = text_channel(700, self.guild)
        put_setting(self.db, STEAM_CHANNEL_ID, 700)
        await self.open_panel()
        await self.go("Steam 免費遊戲")
        steam, role = self.admin.steam, self.role(900)
        roles = self.select(ROLES)
        self.assertTrue(roles.disabled)
        self.assertIn("Steam 自動通知未開啟，未修改身分組設定。", self.screen(await self.choose(roles, role)))
        self.assertEqual(steam.status().role_ids, ())

        clicked = await self.click(self.button("開啟 Steam 自動通知"))
        self.assertTrue(steam.enabled)
        self.assertIn("✅ 已開啟 Steam 自動通知。", self.screen(clicked))
        self.assertFalse(roles.disabled)

        shown = self.screen(await self.choose(roles, role))
        self.assertEqual(steam.status().role_ids, (900,))
        self.assertIn("已更新 Steam 免費遊戲通知身分組，共 1 個。", shown)
        self.assertIn("通知身分組 <@&900>", shown)
        shown = self.screen(await self.choose(roles))
        self.assertEqual(steam.status().role_ids, ())
        self.assertIn("已取消 Steam 免費遊戲通知身分組。", shown)

        self.view.toggled_at.clear()
        clicked = await self.click(self.button("關閉 Steam 自動通知"))
        self.assertFalse(steam.enabled)
        self.assertIn("✅ 已關閉 Steam 自動通知。", self.screen(clicked))
        self.assertTrue(roles.disabled)

    async def test_channel_select_binds_the_notification_channel(self) -> None:
        self.channels[700] = channel = text_channel(700, self.guild)
        await self.open_panel()
        await self.go("Steam 免費遊戲")
        shown = self.screen(await self.choose(self.select("選擇 Steam 通知頻道"), resolved(channel)))
        self.assertEqual(self.admin.steam.status().channel_id, 700)
        self.assertIn("已綁定 Steam 免費遊戲通知頻道。", shown)


if __name__ == "__main__":
    unittest.main()
