import unittest

import discord

from src.ai import MODEL_CATALOG, ModelChoice, ModelSettings
from src.core.adapters.discord.brand import ICONS
from tests.support.admin import PanelCase
from tests.support.testing import text

UNSAVED = "● 尚有未儲存的變更"
SONNET_THINKING = "Claude Sonnet 5.5（思考）"


class ModelPageTests(PanelCase):
    async def open_models(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        await self.go("模型")

    async def test_draft_is_saved_only_when_asked_and_survives_reopening(self) -> None:
        await self.open_models()
        self.assertNotIn(UNSAVED, text(self.view))
        self.assertIn("### 主模型\n**Claude Haiku 5.5**", text(self.view))
        await self.choose(self.select("選擇主模型"), self.option(SONNET_THINKING))
        shown = self.screen(await self.choose(self.select("選擇備援模型（可停用）"), "off"))
        self.assertIn(f"### 主模型\n**{SONNET_THINKING}**", shown)
        self.assertNotIn("推理強度", shown)
        self.assertIn(UNSAVED, shown)
        self.assertNotEqual(self.admin.ai.models.primary, ModelChoice("claude-sonnet-5-5-thinking"))
        shown = self.screen(await self.click(self.button("儲存設定")))
        self.assertIn(f"### 主模型\n**{SONNET_THINKING}**", shown)
        self.assertIn("### 備援模型\n**停用**", shown)
        self.assertIn("已儲存全 Bot 模型設定", shown)
        self.assertNotIn(UNSAVED, shown)
        self.assertEqual(self.admin.ai.models, ModelSettings(ModelChoice("claude-sonnet-5-5-thinking"), None))
        shown = self.screen(await self.click(self.button("重新整理")))
        self.assertIn(f"### 主模型\n**{SONNET_THINKING}**", shown)
        self.assertNotIn(UNSAVED, shown)

        await self.open_panel()
        await self.go("樂奈")
        shown = self.screen(await self.go("模型"))
        self.assertIn(f"### 主模型\n**{SONNET_THINKING}**", shown)
        self.assertIn("### 備援模型\n**停用**", shown)

    async def test_both_sides_offer_the_whole_catalog(self) -> None:
        await self.open_models()
        names = [info.display_name for info in MODEL_CATALOG]
        self.assertEqual([option.label for option in self.select("選擇主模型").options], names)
        self.assertEqual([option.label for option in self.select("選擇備援模型（可停用）").options], ["停用備援", *names])

    async def test_every_way_out_asks_before_discarding_the_draft(self) -> None:
        await self.open_models()
        saved = self.admin.ai.models
        menu = self.select("前往其他頁面…")
        for way_out, leave, title in (
            ("重新整理", lambda: self.click(self.button("重新整理")), "## 🎛️ 樂奈模型設定"),
            ("返回總覽", lambda: self.click(self.button("返回總覽")), "## 🏠 管理控制台"),
            ("分頁", lambda: self.click(self.button("權限")), "## 🔐 樂奈使用權限"),
            ("下拉", lambda: self.choose(menu, "voice"), "## 🎙️ 臨時語音"),
        ):
            with self.subTest(way_out):
                await self.choose(self.select("選擇主模型"), self.option(SONNET_THINKING))
                shown = self.screen(await leave())
                self.assertIn(f"### {ICONS['warn']} 捨棄未儲存的變更？", shown)
                self.assertEqual(self.button("捨棄").style, discord.ButtonStyle.danger)
                self.assertEqual(self.button("繼續編輯").style, discord.ButtonStyle.secondary)
                shown = self.screen(await self.click(self.button("繼續編輯")))
                self.assertIn(UNSAVED, shown)
                self.assertIn(f"### 主模型\n**{SONNET_THINKING}**", shown)
                await leave()
                self.assertEqual(self.title(await self.click(self.button("捨棄"))), title)
                if self.view.page != "ai_models":
                    await self.go("樂奈")
                    await self.go("模型")
                shown = text(self.view)
                self.assertNotIn(UNSAVED, shown)
                self.assertNotIn(f"**{SONNET_THINKING}**", shown)
        self.assertEqual(self.admin.ai.models, saved)

    async def test_leaving_without_changes_does_not_ask(self) -> None:
        await self.open_models()
        self.assertEqual(self.title(await self.click(self.button("返回總覽"))), "## 🏠 管理控制台")


if __name__ == "__main__":
    unittest.main()
