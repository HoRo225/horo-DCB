from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from src.admin_panel import MAX_STEAM_OFFERS_SHOWN, AdminPanelView
from src.ai_client import AIRuntimeStatus
from src.steam_free_games import SteamFetchResult, SteamGuildStatus, SteamOffer
from src.temp_voice import TempVoiceGuildStatus


def make_offer(app_id=214340, name="Deponia", price="NT$ 278", header_image=None):
    return SteamOffer(
        app_id=app_id,
        name=name,
        old_price=price,
        description="Adventure",
        developers=("Daedalic",),
        header_image=header_image,
    )


class FakeResponse:
    def __init__(self):
        self.edits = []
        self.defer_count = 0

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def defer(self):
        self.defer_count += 1


class FakeInteraction:
    def __init__(self, *, user_id=1, guild_id=10, administrator=True):
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        self.permissions = SimpleNamespace(administrator=administrator)
        self.response = FakeResponse()
        self.original_edits = []

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)


class FakeTempVoice:
    def __init__(self):
        self.reconcile = AsyncMock()

    def get_guild_status(self, guild_id):
        return TempVoiceGuildStatus(True, 123 if guild_id == 10 else None, 2)


class FakeSteam:
    def __init__(self):
        self.fetch_current_offers = AsyncMock(
            return_value=SteamFetchResult(frozenset({214340}), (make_offer(),))
        )

    def get_guild_status(self, guild_id):
        return SteamGuildStatus(True, 900, 456 if guild_id == 10 else None, 1)


class AdminPanelViewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai_status = AIRuntimeStatus("GPT 5.6 Luna", "auto", True, "0.5.55")
        ai_client = SimpleNamespace(
            model="horo-main",
            internal_value="SENSITIVE_VALUE",
            get_runtime_status=AsyncMock(return_value=self.ai_status),
        )
        self.chat = SimpleNamespace(
            ai_client=ai_client,
            history_limit=50,
            context_char_limit=16_000,
            cooldown_seconds=5.0,
            agent_timeout_seconds=120.0,
        )
        self.temp_voice = FakeTempVoice()
        self.steam = FakeSteam()
        self.view = AdminPanelView(
            user_id=1,
            guild_id=10,
            chat=self.chat,
            ai_status=self.ai_status,
            temp_voice=self.temp_voice,
            steam_free_games=self.steam,
        )

    @staticmethod
    def all_components(view):
        components = []

        def collect(component):
            components.append(component)
            for child in component.get("components", []):
                collect(child)
            accessory = component.get("accessory")
            if accessory:
                collect(accessory)

        for component in view.to_components():
            collect(component)
        return components

    @classmethod
    def text_contents(cls, view):
        return [
            component["content"]
            for component in cls.all_components(view)
            if component.get("type") == discord.ComponentType.text_display.value
        ]

    @classmethod
    def text(cls, view):
        return "\n".join(cls.text_contents(view))

    @classmethod
    def buttons(cls, view):
        return {
            component.get("label"): component
            for component in cls.all_components(view)
            if component.get("type") == discord.ComponentType.button.value
        }

    @classmethod
    def selects(cls, view):
        return [
            component
            for component in cls.all_components(view)
            if component.get("type") == discord.ComponentType.string_select.value
        ]

    @staticmethod
    def selected(select):
        return [option["value"] for option in select["options"] if option.get("default")]

    def test_overview_shows_health_setup_and_activity_without_detail(self):
        self.assertTrue(self.view.has_components_v2())
        text = self.text(self.view)
        self.assertIn("# 管理控制台\n-# 系統狀態與管理工具", text)
        self.assertIn("## 系統狀態\n**全部正常**", text)
        self.assertNotIn("## 需要注意", text)
        self.assertIn("## 設定\n**2 / 2 已設定**", text)
        self.assertIn("Steam 通知目前追蹤 1 款活動", text)
        self.assertRegex(text, r"狀態更新 <t:\d+:R>")
        self.assertNotIn("GPT 5.6 Luna", text)
        self.assertNotIn("horo-main", text)
        self.assertNotIn("SENSITIVE_VALUE", text)
        container = self.view.to_components()[0]
        self.assertEqual(container["accent_color"], 0x5865F2)
        self.assertEqual(
            container["components"][1]["spacing"],
            discord.SeparatorSpacing.large.value,
        )
        self.assertEqual(
            container["components"][3]["type"],
            discord.ComponentType.separator.value,
        )

    def test_overview_distinguishes_normal_pending_and_error(self):
        self.view.ai_status = AIRuntimeStatus(None, None, False, None)
        self.temp_voice.get_guild_status = lambda guild_id: TempVoiceGuildStatus(
            True, None, 0
        )
        self.steam.get_guild_status = lambda guild_id: SteamGuildStatus(
            True, 900, 456, 0
        )

        self.view._render_overview()
        text = self.text(self.view)

        self.assertIn("**1 個正常 · 1 個待設定 · 1 個異常**", text)
        self.assertIn("## 需要注意", text)
        self.assertIn("**AI 助手 · 異常**", text)
        self.assertIn("9Router 無法連線", text)
        self.assertIn("**臨時語音 · 待設定**", text)
        self.assertIn("入口頻道尚未綁定", text)
        self.assertNotIn("**Steam 免費遊戲 · 正常**", text)
        self.assertIn("## 設定\n**1 / 2 已設定**", text)
        self.assertIn("尚未完成：臨時語音入口", text)

    def test_overview_reports_steam_unconfigured_as_pending(self):
        self.steam.get_guild_status = lambda guild_id: SteamGuildStatus(
            True, 900, None, 0
        )

        self.view._render_overview()
        text = self.text(self.view)

        self.assertIn("**2 個正常 · 1 個待設定 · 0 個異常**", text)
        self.assertIn("**Steam 免費遊戲 · 待設定**", text)
        self.assertIn("通知頻道尚未綁定", text)
        self.assertIn("## 設定\n**1 / 2 已設定**", text)
        self.assertIn("尚未完成：Steam 通知頻道", text)
        self.assertIn("Steam 通知目前追蹤 0 款活動", text)

    def test_overview_reports_incomplete_ai_metadata_as_error(self):
        self.view.ai_status = AIRuntimeStatus(None, "auto", True, "0.5.55")
        self.view._render_overview()
        text = self.text(self.view)

        self.assertIn("**AI 助手 · 異常**", text)
        self.assertIn("模型資訊無法完整取得", text)
        self.assertNotIn("horo-main", text)
        self.assertNotIn("SENSITIVE_VALUE", text)

    def test_overview_treats_missing_optional_effort_as_normal(self):
        self.view.ai_status = AIRuntimeStatus("horo-main", None, True, "0.5.55")

        self.view._render_overview()
        text = self.text(self.view)

        self.assertIn("## 系統狀態\n**全部正常**", text)
        self.assertNotIn("## 需要注意", text)

    def test_overview_reports_unavailable_state_files_and_zero_setup(self):
        self.temp_voice.get_guild_status = lambda guild_id: TempVoiceGuildStatus(
            False, None, 0
        )
        self.steam.get_guild_status = lambda guild_id: SteamGuildStatus(
            False, 900, None, 0
        )

        self.view._render_overview()
        text = self.text(self.view)

        self.assertIn("**1 個正常 · 0 個待設定 · 2 個異常**", text)
        self.assertIn("臨時語音功能已停用", text)
        self.assertIn("通知功能已停用", text)
        self.assertIn("## 設定\n**0 / 2 已設定**", text)
        self.assertIn("尚未完成：臨時語音入口 · Steam 通知頻道", text)
        self.assertIn("Steam 通知活動追蹤狀態無法取得", text)

    def test_overview_zero_child_and_active_counts_are_normal(self):
        self.temp_voice.get_guild_status = lambda guild_id: TempVoiceGuildStatus(
            True, 123, 0
        )
        self.steam.get_guild_status = lambda guild_id: SteamGuildStatus(
            True, 900, 456, 0
        )

        self.view._render_overview()
        text = self.text(self.view)

        self.assertIn("**全部正常**", text)
        self.assertNotIn("## 需要注意", text)
        self.assertIn("Steam 通知目前追蹤 0 款活動", text)

    async def test_overview_update_time_changes_only_on_overview_refresh(self):
        initial = self.view._overview_updated_at

        await self.view.handle_action(FakeInteraction(), "ai")
        self.assertEqual(self.view._overview_updated_at, initial)
        await self.view.handle_action(FakeInteraction(), "overview")
        self.assertEqual(self.view._overview_updated_at, initial)

        with patch("src.admin_panel.time.time", return_value=initial + 60):
            await self.view.handle_action(FakeInteraction(), "refresh")

        self.assertEqual(self.view._overview_updated_at, initial + 60)
        self.assertIn(f"<t:{initial + 60}:R>", self.text(self.view))
        self.temp_voice.reconcile.assert_not_awaited()
        self.steam.fetch_current_offers.assert_not_awaited()

    def test_main_select_sits_right_below_the_large_separator(self):
        for page, render in (
            ("overview", self.view._render_overview),
            ("ai", self.view._render_ai),
            ("modules", self.view._render_modules),
            ("voice", self.view._render_voice),
            ("steam", self.view._render_steam),
        ):
            with self.subTest(page=page):
                render()
                children = self.view.to_components()[0]["components"]
                self.assertEqual(
                    children[1]["type"], discord.ComponentType.separator.value
                )
                self.assertEqual(
                    children[1]["spacing"], discord.SeparatorSpacing.large.value
                )
                self.assertEqual(
                    children[2]["type"], discord.ComponentType.action_row.value
                )
                self.assertEqual(
                    children[2]["components"][0]["type"],
                    discord.ComponentType.string_select.value,
                )

    def test_main_select_marks_current_page(self):
        self.assertEqual(self.selected(self.selects(self.view)[0]), ["overview"])
        self.view._render_ai()
        self.assertEqual(self.selected(self.selects(self.view)[0]), ["ai"])
        self.view._render_modules()
        self.assertEqual(self.selected(self.selects(self.view)[0]), ["modules"])

    def test_module_select_only_exists_on_the_modules_branch(self):
        self.assertEqual(len(self.selects(self.view)), 1)
        self.view._render_ai()
        self.assertEqual(len(self.selects(self.view)), 1)

        self.view._render_modules()
        selects = self.selects(self.view)
        self.assertEqual(len(selects), 2)
        self.assertEqual(self.selected(selects[0]), ["modules"])
        self.assertEqual(self.selected(selects[1]), [])

        self.view._render_voice()
        selects = self.selects(self.view)
        self.assertEqual(self.selected(selects[0]), ["modules"])
        self.assertEqual(self.selected(selects[1]), ["voice"])

        self.view._render_steam()
        selects = self.selects(self.view)
        self.assertEqual(self.selected(selects[0]), ["modules"])
        self.assertEqual(self.selected(selects[1]), ["steam"])

    def test_overview_action_buttons(self):
        buttons = self.buttons(self.view)
        self.assertEqual(
            buttons["重新整理"]["style"], discord.ButtonStyle.secondary.value
        )
        self.assertEqual(
            buttons["關閉控制台"]["style"], discord.ButtonStyle.danger.value
        )
        for removed in ("查看 AI", "查看語音", "查看 Steam", "返回控制台"):
            self.assertNotIn(removed, buttons)

    async def test_interaction_is_restricted_to_invoking_admin_and_guild(self):
        self.assertTrue(await self.view.interaction_check(FakeInteraction()))
        self.assertFalse(await self.view.interaction_check(FakeInteraction(user_id=2)))
        self.assertFalse(await self.view.interaction_check(FakeInteraction(guild_id=11)))
        self.assertFalse(await self.view.interaction_check(FakeInteraction(administrator=False)))

    async def test_navigation_edits_existing_message(self):
        interaction = FakeInteraction()
        await self.view.handle_action(interaction, "ai")

        self.chat.ai_client.get_runtime_status.assert_awaited_once_with()
        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(interaction.response.edits, [])
        self.assertEqual(len(interaction.original_edits), 1)
        text = self.text(self.view)
        self.assertIn("AI 助手", text)
        self.assertIn("由 9Router 即時提供模型與服務狀態", text)
        self.assertIn("## 模型與服務", text)
        self.assertIn("## 對話", text)
        self.assertIn("## Agent", text)
        self.assertIn("GPT 5.6 Luna", text)
        self.assertIn("Effort", text)
        self.assertIn("Auto", text)
        self.assertIn("v0.5.55", text)
        self.assertNotIn("horo-main", text)
        self.assertNotIn("SENSITIVE_VALUE", text)
        self.assertNotIn("AI Runtime", text)
        self.assertNotIn("Agent timeout", text)

    async def test_select_navigation_edits_the_same_message(self):
        interaction = FakeInteraction()
        await self.view.handle_action(interaction, "modules")

        self.assertEqual(interaction.response.defer_count, 0)
        self.assertEqual(len(interaction.response.edits), 1)
        self.assertEqual(interaction.original_edits, [])
        text = self.text(self.view)
        self.assertIn("# 功能模組", text)
        self.assertIn("## 臨時語音", text)
        self.assertIn("## Steam 免費遊戲", text)

    def test_modules_page_shows_both_module_summaries(self):
        self.view._render_modules()
        text = self.text(self.view)
        self.assertIn("**2 個頻道追蹤中**", text)
        self.assertIn("-# 入口 <#123> · 狀態正常", text)
        self.assertIn("**1 款活動中**", text)
        self.assertIn("-# 通知 <#456> · 每 15 分鐘檢查 · 狀態正常", text)

    def test_unavailable_state_files_are_reported_in_the_main_value(self):
        self.temp_voice.get_guild_status = lambda guild_id: TempVoiceGuildStatus(
            False, None, 0
        )
        self.steam.get_guild_status = lambda guild_id: SteamGuildStatus(
            False, 900, None, 0
        )
        self.view._render_modules()
        text = self.text(self.view)
        self.assertIn("## 臨時語音\n**狀態檔不可用**", text)
        self.assertIn("## Steam 免費遊戲\n**狀態檔不可用**", text)

    def test_router_failure_is_reported_in_the_main_value(self):
        self.view.ai_status = AIRuntimeStatus(None, None, False, None)
        self.view._render_ai()
        text = self.text(self.view)
        self.assertIn("**無法取得模型**", text)
        self.assertIn("9Router 無法連線", text)

    def test_voice_note_is_markdown_escaped(self):
        self.view._render_voice("**粗體**")
        text = self.text(self.view)
        self.assertIn("## 目前狀態", text)
        self.assertIn("## 最近操作", text)
        self.assertIn(r"\*\*粗體\*\*", text)
        self.assertNotIn("**粗體**\n", text)

    def test_steam_query_result_has_separate_heading(self):
        self.view._render_steam(self.steam.fetch_current_offers.return_value)
        text = self.text(self.view)
        self.assertIn("## 通知狀態", text)
        self.assertIn("## 查詢結果", text)
        self.assertIn("Deponia", text)

    def test_steam_offer_with_header_image_uses_a_thumbnail(self):
        result = SteamFetchResult(
            frozenset({1}),
            (make_offer(header_image="https://example.invalid/deponia.jpg"),),
        )
        self.view._render_steam(result)
        types = [component.get("type") for component in self.all_components(self.view)]
        self.assertIn(discord.ComponentType.section.value, types)
        self.assertIn(discord.ComponentType.thumbnail.value, types)

    def test_steam_offer_without_header_image_stays_text_only(self):
        self.view._render_steam(self.steam.fetch_current_offers.return_value)
        types = [component.get("type") for component in self.all_components(self.view)]
        self.assertNotIn(discord.ComponentType.thumbnail.value, types)
        self.assertIn("Deponia", self.text(self.view))

    def test_steam_results_are_capped(self):
        offers = tuple(
            make_offer(app_id=i, name=f"Game {i}") for i in range(MAX_STEAM_OFFERS_SHOWN + 2)
        )
        self.view._render_steam(SteamFetchResult(frozenset(), offers))
        text = self.text(self.view)
        self.assertEqual(MAX_STEAM_OFFERS_SHOWN, 5)
        self.assertIn(f"Game {MAX_STEAM_OFFERS_SHOWN - 1}", text)
        self.assertNotIn(f"Game {MAX_STEAM_OFFERS_SHOWN}", text)
        self.assertIn(f"只顯示前 {MAX_STEAM_OFFERS_SHOWN} 款，共 {len(offers)} 款。", text)

    def test_steam_empty_result_says_so(self):
        self.view._render_steam(SteamFetchResult(frozenset(), ()))
        self.assertIn("目前沒有符合條件的限時免費遊戲。", self.text(self.view))

    def test_detail_action_buttons_use_expected_hierarchy(self):
        self.view._render_voice()
        voice_buttons = self.buttons(self.view)
        self.assertEqual(
            voice_buttons["重新同步"]["style"], discord.ButtonStyle.primary.value
        )
        self.assertEqual(
            voice_buttons["關閉控制台"]["style"], discord.ButtonStyle.danger.value
        )
        self.assertNotIn("返回控制台", voice_buttons)

        self.view._render_steam()
        steam_buttons = self.buttons(self.view)
        self.assertEqual(
            steam_buttons["重新查詢"]["style"], discord.ButtonStyle.primary.value
        )
        self.assertEqual(
            steam_buttons["關閉控制台"]["style"], discord.ButtonStyle.danger.value
        )
        self.assertNotIn("返回控制台", steam_buttons)

    async def test_refresh_re_renders_the_current_page(self):
        await self.view.handle_action(FakeInteraction(), "modules")
        interaction = FakeInteraction()
        await self.view.handle_action(interaction, "refresh")

        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertEqual(self.view.page, "modules")
        self.assertIn("# 功能模組", self.text(self.view))

    async def test_close_renders_a_dead_panel_in_the_same_message(self):
        interaction = FakeInteraction()
        await self.view.handle_action(interaction, "close")

        self.assertIn("控制台已關閉", self.text(self.view))
        self.assertEqual(len(interaction.response.edits), 1)
        self.assertEqual(interaction.original_edits, [])
        self.assertEqual(self.selects(self.view), [])
        self.assertTrue(self.buttons(self.view)["已關閉"]["disabled"])

    async def test_voice_sync_reuses_reconcile_and_edits_original_response(self):
        interaction = FakeInteraction()
        await self.view.handle_action(interaction, "voice_sync")

        self.temp_voice.reconcile.assert_awaited_once_with(
            [interaction.guild],
            prune_absent=False,
        )
        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertEqual(interaction.response.edits, [])

    async def test_disabled_voice_module_cannot_run_reconcile(self):
        view = AdminPanelView(
            user_id=1,
            guild_id=10,
            chat=self.chat,
            ai_status=self.ai_status,
            temp_voice=self.temp_voice,
            steam_free_games=self.steam,
            temp_voice_enabled=False,
        )
        view._render_voice()

        self.assertIn("依設定停用", self.text(view))
        self.assertTrue(self.buttons(view)["重新同步"]["disabled"])

        interaction = FakeInteraction()
        await view.handle_action(interaction, "voice_sync")

        self.temp_voice.reconcile.assert_not_awaited()
        self.assertIn("功能已停用，未執行同步", self.text(view))

    def test_disabled_modules_are_not_reported_as_broken_or_unconfigured(self):
        view = AdminPanelView(
            user_id=1,
            guild_id=10,
            chat=self.chat,
            ai_status=self.ai_status,
            temp_voice=self.temp_voice,
            steam_free_games=self.steam,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
        )

        text = self.text(view)
        self.assertIn("已啟用功能正常", text)
        self.assertIn("臨時語音與 Steam 自動通知依設定停用", text)
        self.assertIn("## 設定\n**無需設定**", text)
        self.assertNotIn("## 需要注意", text)

    async def test_steam_query_only_fetches_and_edits_original_response(self):
        interaction = FakeInteraction()
        await self.view.handle_action(interaction, "steam_query")

        self.steam.fetch_current_offers.assert_awaited_once_with()
        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertIn("Deponia", self.text(self.view))


if __name__ == "__main__":
    unittest.main()
