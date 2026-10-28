import unittest

import discord

from src.ai import ModelChoice, ModelInfo, ModelSettings
from src.core.adapters.discord.brand import ICONS
from tests.support.admin import PanelCase
from tests.support.testing import text

UNSAVED = "● 尚有未儲存的變更"


class ModelPageTests(PanelCase):
    async def open_models(self) -> None:
        await self.open_panel()
        await self.go("樂奈")
        await self.go("模型")

    async def test_draft_is_saved_only_when_asked_and_survives_reopening(self) -> None:
        await self.open_models()
        self.assertNotIn(UNSAVED, text(self.view))
        await self.choose(self.select("選擇主模型"), self.option("Model B"))
        shown = self.screen(await self.choose(self.select("選擇備援模型（可停用）"), "off"))
        self.assertIn("### 主模型\n**gpt-b** · 推理強度：高", shown)
        self.assertIn(UNSAVED, shown)
        self.assertNotEqual(self.admin.ai.models.primary, ModelChoice("gpt-b"))
        shown = self.screen(await self.click(self.button("儲存設定")))
        self.assertIn("### 主模型\n**gpt-b** · 推理強度：高", shown)
        self.assertIn("### 備援模型\n**停用**", shown)
        self.assertIn("已儲存全 Bot 模型設定", shown)
        self.assertNotIn(UNSAVED, shown)
        self.assertEqual(self.admin.ai.models, ModelSettings(ModelChoice("gpt-b"), None))
        shown = self.screen(await self.click(self.button("重新整理")))
        self.assertIn("### 主模型\n**gpt-b** · 推理強度：高", shown)
        self.assertNotIn(UNSAVED, shown)

        await self.open_panel()
        await self.go("樂奈")
        shown = self.screen(await self.go("模型"))
        self.assertIn("### 主模型\n**gpt-b** · 推理強度：高", shown)
        self.assertIn("### 備援模型\n**停用**", shown)

    async def test_every_way_out_asks_before_discarding_the_draft(self) -> None:
        await self.open_models()
        saved = self.admin.ai.models
        menu = self.select("前往其他頁面…")
        for way_out, leave, title in (
            ("重新整理", lambda: self.click(self.button("重新整理")), "## 🎛️ 樂奈模型設定"),
            ("返回總覽", lambda: self.click(self.button("返回總覽")), "## 🏠 管理控制台"),
            ("分頁", lambda: self.click(self.button("技術")), "## 🛠️ 樂奈技術資訊"),
            ("下拉", lambda: self.choose(menu, "voice"), "## 🎙️ 臨時語音"),
        ):
            with self.subTest(way_out):
                await self.choose(self.select("選擇主模型"), self.option("Model B"))
                shown = self.screen(await leave())
                self.assertIn(f"### {ICONS['warn']} 捨棄未儲存的變更？", shown)
                self.assertEqual(self.button("捨棄").style, discord.ButtonStyle.danger)
                self.assertEqual(self.button("繼續編輯").style, discord.ButtonStyle.secondary)
                shown = self.screen(await self.click(self.button("繼續編輯")))
                self.assertIn(UNSAVED, shown)
                self.assertIn("### 主模型\n**gpt-b** · 推理強度：高", shown)
                await leave()
                self.assertEqual(self.title(await self.click(self.button("捨棄"))), title)
                if self.view.page != "ai_models":
                    await self.go("樂奈")
                    await self.go("模型")
                shown = text(self.view)
                self.assertNotIn(UNSAVED, shown)
                self.assertNotIn("**gpt-b**", shown)
        self.assertEqual(self.admin.ai.models, saved)

    async def test_leaving_without_changes_does_not_ask(self) -> None:
        await self.open_models()
        self.assertEqual(self.title(await self.click(self.button("返回總覽"))), "## 🏠 管理控制台")

    async def test_effort_follows_the_chosen_model(self) -> None:
        await self.open_models()
        await self.choose(self.select("選擇主模型"), self.option("Model A"))
        await self.choose(self.select("選擇主模型推理強度"), self.option("低"))
        await self.choose(self.select("選擇備援模型（可停用）"), "off")
        await self.click(self.button("儲存設定"))
        self.assertEqual(self.admin.ai.models, ModelSettings(ModelChoice("gpt-a", "low"), None))

    async def test_model_gone_before_saving_keeps_the_saved_settings(self) -> None:
        await self.open_models()
        saved = self.admin.ai.models
        await self.choose(self.select("選擇主模型"), self.option("Model B"))
        await self.choose(self.select("選擇備援模型（可停用）"), "off")
        self.codex.get_models.return_value = (ModelInfo("gpt-a", "Model A", "medium", ("medium",), ("text",)),)
        shown = self.screen(await self.click(self.button("儲存設定")))
        self.assertIn("模型或推理強度已失效，請重新選擇；已儲存設定未變更。", shown)
        self.assertEqual(self.admin.ai.models, saved)

    async def test_page_buttons_turn_their_own_side(self) -> None:
        self.codex.get_models.return_value = tuple(
            ModelInfo(f"gpt-{n}", f"Model {n}", "low", ("low",), ("text",)) for n in range(30)
        )
        await self.open_models()
        shown = self.screen(await self.click(self.button("下一頁")))
        self.assertIn("### 主模型 · 第 2/2 頁", shown)
        self.assertIn("### 備援模型 · 第 1/2 頁", shown)
        self.assertTrue(self.button("下一頁").disabled)

    async def test_unavailable_catalog_leaves_saving_disabled(self) -> None:
        self.codex.get_models.side_effect = RuntimeError("down")
        with self.assertLogs(level="ERROR"):
            await self.open_models()
            shown = self.screen(await self.click(self.button("重新整理")))
        self.assertIn("目前無法取得模型清單；未變更已儲存設定。", shown)
        self.assertTrue(self.button("儲存設定").disabled)


if __name__ == "__main__":
    unittest.main()
