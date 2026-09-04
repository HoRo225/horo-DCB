from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from src.admin_panel import MAX_STEAM_OFFERS_SHOWN, AdminPanelView
from src.codex_bridge_client import CodexRuntimeStatus
from src.server_activity import ActivitySummary, ServerActivityStatus, StoredActivityEvent
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
    def __init__(self, *, done=False):
        self.edits = []
        self.defer_count = 0
        self.messages = []
        self._done = done

    def is_done(self):
        return self._done

    async def send_message(self, content, **kwargs):
        self.messages.append((content, kwargs))
        self._done = True

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def defer(self):
        self.defer_count += 1


class FakeInteraction:
    def __init__(self, *, user_id=1, guild_id=10, administrator=True, response_done=False):
        self.user = SimpleNamespace(id=user_id)
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        self.permissions = SimpleNamespace(administrator=administrator)
        self.response = FakeResponse(done=response_done)
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
        self.role_ids = ()
        self.set_notification_roles = AsyncMock(side_effect=self._set_notification_roles)
        self.clear_notification_roles = AsyncMock(
            side_effect=self._clear_notification_roles
        )

    async def _set_notification_roles(self, _guild, roles):
        self.role_ids = tuple(sorted(role.id for role in roles))

    async def _clear_notification_roles(self, _guild_id):
        removed = bool(self.role_ids)
        self.role_ids = ()
        return removed

    def get_guild_status(self, guild_id):
        return SteamGuildStatus(
            True,
            900,
            456 if guild_id == 10 else None,
            1,
            self.role_ids,
        )


class FakeActivity:
    def __init__(self, status=None, summary=None, recent=()):
        self.status = status or ServerActivityStatus(True, 2, 100, 0)
        self.get_summary = AsyncMock(return_value=summary or ActivitySummary())
        self.get_recent_events = AsyncMock(return_value=list(recent))

    def get_runtime_status(self):
        return self.status


class AdminPanelViewTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.codex_status = CodexRuntimeStatus(
            True,
            True,
            "free",
            "0.147.0",
            "0.147.0",
            "live",
            3,
        )
        self.codex_client = SimpleNamespace(
            internal_value="SENSITIVE_VALUE",
            get_runtime_status=AsyncMock(return_value=self.codex_status),
        )
        self.temp_voice = FakeTempVoice()
        self.steam = FakeSteam()
        self.activity = FakeActivity()
        self.view = AdminPanelView(
            user_id=1,
            guild_id=10,
            codex_client=self.codex_client,
            codex_status=self.codex_status,
            temp_voice=self.temp_voice,
            steam_free_games=self.steam,
            server_activity=self.activity,
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
        self.assertIn("伺服器活動皆可用", text)
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
        self.view.codex_status = CodexRuntimeStatus(False, False, None, "0.147.0", None, "live", 0)
        self.temp_voice.get_guild_status = lambda guild_id: TempVoiceGuildStatus(
            True, None, 0
        )
        self.steam.get_guild_status = lambda guild_id: SteamGuildStatus(
            True, 900, 456, 0
        )

        self.view._render_overview()
        text = self.text(self.view)

        self.assertIn("**2 個正常 · 1 個待設定 · 1 個異常**", text)
        self.assertIn("## 需要注意", text)
        self.assertIn("**AI 助手 · 異常**", text)
        self.assertIn("Codex bridge 無法連線", text)
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

        self.assertIn("**3 個正常 · 1 個待設定 · 0 個異常**", text)
        self.assertIn("**Steam 免費遊戲 · 待設定**", text)
        self.assertIn("通知頻道尚未綁定", text)
        self.assertIn("## 設定\n**1 / 2 已設定**", text)
        self.assertIn("尚未完成：Steam 通知頻道", text)
        self.assertIn("Steam 通知目前追蹤 0 款活動", text)

    def test_overview_reports_unauthenticated_codex_as_pending(self):
        self.view.codex_status = CodexRuntimeStatus(
            True, False, None, "0.147.0", "0.147.0", "live", 0
        )
        self.view._render_overview()
        text = self.text(self.view)

        self.assertIn("**AI 助手 · 待設定**", text)
        self.assertIn("Codex 尚未登入", text)
        self.assertNotIn("SENSITIVE_VALUE", text)

    def test_overview_treats_missing_optional_runtime_as_normal(self):
        self.view.codex_status = CodexRuntimeStatus(
            True, True, "free", "0.147.0", None, "live", 0
        )

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

        self.assertIn("**2 個正常 · 0 個待設定 · 2 個異常**", text)
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
            ("activity", self.view._render_activity),
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
        self.view._render_activity()
        self.assertEqual(self.selected(self.selects(self.view)[0]), ["activity"])

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

    async def test_interaction_check_allows_original_admin_without_responding(self):
        interaction = FakeInteraction()

        self.assertTrue(await self.view.interaction_check(interaction))
        self.assertEqual(interaction.response.messages, [])

    async def test_interaction_check_rejects_wrong_user_guild_and_revoked_admin(self):
        for interaction in (
            FakeInteraction(user_id=2),
            FakeInteraction(guild_id=11),
            FakeInteraction(administrator=False),
        ):
            with self.subTest(interaction=interaction):
                self.assertFalse(await self.view.interaction_check(interaction))
                self.assertEqual(len(interaction.response.messages), 1)
                content, kwargs = interaction.response.messages[0]
                self.assertEqual(content, "只有開啟控制台的伺服器管理員可以操作這個控制台。")
                self.assertTrue(kwargs["ephemeral"])
                allowed = kwargs["allowed_mentions"]
                self.assertFalse(allowed.everyone)
                self.assertFalse(allowed.users)
                self.assertFalse(allowed.roles)

    async def test_interaction_check_rejects_silently_when_response_is_done(self):
        interaction = FakeInteraction(user_id=2, response_done=True)

        self.assertFalse(await self.view.interaction_check(interaction))
        self.assertEqual(interaction.response.messages, [])

    async def test_navigation_edits_existing_message(self):
        interaction = FakeInteraction()
        await self.view.handle_action(interaction, "ai")

        self.codex_client.get_runtime_status.assert_awaited_once_with()
        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(interaction.response.edits, [])
        self.assertEqual(len(interaction.original_edits), 1)
        text = self.text(self.view)
        self.assertIn("AI 助手", text)
        self.assertIn("官方 Codex OAuth 與持久對話", text)
        self.assertIn("## 帳號與服務", text)
        self.assertIn("## 對話", text)
        self.assertIn("## 安全邊界", text)
        self.assertIn("Free", text)
        self.assertIn("Runtime 0.147.0", text)
        self.assertIn("Web Search Live", text)
        self.assertNotIn("9Router", text)
        self.assertNotIn("SENSITIVE_VALUE", text)

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

    def test_codex_failure_is_reported_in_the_main_value(self):
        self.view.codex_status = CodexRuntimeStatus(
            False, False, None, "0.147.0", None, "live", 0
        )
        self.view._render_ai()
        text = self.text(self.view)
        self.assertIn("**Unknown · 未登入**", text)
        self.assertNotIn("9Router", text)

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

    def test_steam_page_has_multi_role_select_and_clear_control(self):
        self.view._render_steam()
        role_selects = [
            component
            for component in self.all_components(self.view)
            if component.get("type") == discord.ComponentType.role_select.value
        ]
        self.assertEqual(len(role_selects), 1)
        self.assertFalse(role_selects[0].get("disabled", False))
        self.assertEqual(role_selects[0]["min_values"], 1)
        self.assertEqual(role_selects[0]["max_values"], 25)
        self.assertIn("未設定通知身分組", self.text(self.view))
        self.assertTrue(self.buttons(self.view)["取消身分組通知"]["disabled"])

    async def test_steam_role_select_updates_multiple_roles_without_panel_ping(self):
        roles = (SimpleNamespace(id=77), SimpleNamespace(id=88))
        interaction = FakeInteraction()

        await self.view.handle_steam_role_select(interaction, roles)

        self.steam.set_notification_roles.assert_awaited_once_with(interaction.guild, roles)
        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(len(interaction.original_edits), 1)
        allowed_mentions = interaction.original_edits[0]["allowed_mentions"]
        self.assertFalse(allowed_mentions.everyone)
        self.assertFalse(allowed_mentions.users)
        self.assertFalse(allowed_mentions.roles)
        text = self.text(self.view)
        self.assertIn("<@&77>", text)
        self.assertIn("<@&88>", text)
        self.assertIn("共 2 個", text)
        self.assertFalse(self.buttons(self.view)["取消身分組通知"]["disabled"])

    async def test_steam_role_clear_removes_all_settings(self):
        self.steam.role_ids = (77, 88)
        self.view._render_steam()
        interaction = FakeInteraction()

        await self.view.handle_action(interaction, "steam_role_clear")

        self.steam.clear_notification_roles.assert_awaited_once_with(10)
        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertIn("未設定通知身分組", self.text(self.view))
        self.assertTrue(self.buttons(self.view)["取消身分組通知"]["disabled"])

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

    async def test_local_pages_refresh_without_querying_ai_status(self):
        for page in ("modules", "voice", "steam"):
            with self.subTest(page=page):
                await self.view.handle_action(FakeInteraction(), page)
                self.codex_client.get_runtime_status.reset_mock()

                interaction = FakeInteraction()
                await self.view.handle_action(interaction, "refresh")

                self.codex_client.get_runtime_status.assert_not_awaited()
                self.assertEqual(self.view.page, page)
                self.assertEqual(interaction.response.defer_count, 1)
                self.assertEqual(len(interaction.original_edits), 1)

    async def test_overview_and_ai_refresh_query_ai_status(self):
        for page in ("overview", "ai"):
            with self.subTest(page=page):
                await self.view.handle_action(FakeInteraction(), page)
                self.codex_client.get_runtime_status.reset_mock()

                await self.view.handle_action(FakeInteraction(), "refresh")

                self.codex_client.get_runtime_status.assert_awaited_once_with()

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
            codex_client=self.codex_client,
            codex_status=self.codex_status,
            temp_voice=self.temp_voice,
            steam_free_games=self.steam,
            temp_voice_enabled=False,
            server_activity=self.activity,
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
            codex_client=self.codex_client,
            codex_status=self.codex_status,
            temp_voice=self.temp_voice,
            steam_free_games=self.steam,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
            server_activity=self.activity,
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

    def test_activity_constructor_and_page_use_components_v2(self):
        self.view._render_activity()
        self.assertTrue(self.view.has_components_v2())
        self.assertEqual(self.view.page, "activity")
        self.assertIn("# 伺服器活動\n-# 活動監聽與稽核紀錄", self.text(self.view))
        self.assertEqual(self.selected(self.selects(self.view)[0]), ["activity"])
        self.assertEqual(self.selected(self.selects(self.view)[1]), ["all"])
        self.assertEqual(
            [option["value"] for option in self.selects(self.view)[1]["options"]],
            ["all", "admin", "member", "message", "voice"],
        )

    def test_overview_reports_activity_normal_disabled_unavailable_and_dropped(self):
        self.assertIn("**全部正常**", self.text(self.view))

        disabled = AdminPanelView(
            user_id=1,
            guild_id=10,
            codex_client=self.codex_client,
            codex_status=self.codex_status, temp_voice=self.temp_voice,
            steam_free_games=self.steam,
        )
        disabled_text = self.text(disabled)
        self.assertIn("已啟用功能正常", disabled_text)
        self.assertIn("伺服器活動依設定停用", disabled_text)
        self.assertNotIn("**伺服器活動 · 異常**", disabled_text)

        self.activity.status = ServerActivityStatus(False, 0, 100, 0)
        self.view._render_overview()
        self.assertIn("**伺服器活動 · 異常**", self.text(self.view))
        self.assertIn("活動儲存空間不可用", self.text(self.view))

        self.activity.status = ServerActivityStatus(True, 4, 100, 3)
        self.view._render_overview()
        self.assertIn("**伺服器活動 · 異常**", self.text(self.view))
        self.assertIn("有活動紀錄因佇列已滿而遺失", self.text(self.view))

    async def test_activity_navigation_queries_summary_and_recent_and_edits_same_message(self):
        self.activity.get_summary.return_value = ActivitySummary(12, 1, 2, 3, 4, 2)
        interaction = FakeInteraction()

        await self.view.handle_action(interaction, "activity")

        self.activity.get_summary.assert_awaited_once_with(10)
        self.activity.get_recent_events.assert_awaited_once_with(10, "all", limit=10)
        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(interaction.response.edits, [])
        self.assertEqual(len(interaction.original_edits), 1)
        allowed = interaction.original_edits[0]["allowed_mentions"]
        self.assertFalse(allowed.everyone)
        self.assertFalse(allowed.users)
        self.assertFalse(allowed.roles)
        text = self.text(self.view)
        self.assertIn("30 天保存 · 佇列 2 / 100 · 已遺失 0 筆", text)
        self.assertIn("**總計 12**", text)
        self.assertIn("管理 1", text)
        self.assertIn("成員 2", text)
        self.assertIn("訊息 3", text)
        self.assertIn("語音 4", text)
        self.assertIn("其他 2", text)

    async def test_activity_filter_and_refresh_query_current_filter(self):
        await self.view.handle_action(FakeInteraction(), "activity_filter:message")
        self.activity.get_recent_events.assert_awaited_once_with(10, "message", limit=10)
        self.assertEqual(self.selected(self.selects(self.view)[1]), ["message"])

        self.activity.get_recent_events.reset_mock()
        interaction = FakeInteraction()
        await self.view.handle_action(interaction, "refresh")
        self.activity.get_recent_events.assert_awaited_once_with(10, "message", limit=10)
        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(len(interaction.original_edits), 1)

    async def test_disabled_activity_page_does_not_query_database(self):
        view = AdminPanelView(
            user_id=1,
            guild_id=10,
            codex_client=self.codex_client,
            codex_status=self.codex_status, temp_voice=self.temp_voice,
            steam_free_games=self.steam, server_activity=None,
        )
        interaction = FakeInteraction()
        await view.handle_action(interaction, "activity")
        self.activity.get_summary.assert_not_awaited()
        self.activity.get_recent_events.assert_not_awaited()
        self.assertIn("**依設定停用**", self.text(view))
        self.assertIn("保存期限 30 天", self.text(view))

    async def test_activity_query_failure_logs_generic_message_and_renders_safely(self):
        self.activity.get_summary.side_effect = RuntimeError("SECRET DATABASE DETAIL")
        with self.assertLogs(level="ERROR") as captured:
            await self.view.handle_action(FakeInteraction(), "activity")

        self.assertEqual(captured.output, ["ERROR:root:管理控制台讀取伺服器活動失敗。"])
        text = self.text(self.view)
        self.assertIn("目前無法取得伺服器活動", text)
        self.assertNotIn("SECRET DATABASE DETAIL", text)
        self.assertNotIn("最近 24 小時", text)

    async def test_activity_recent_escapes_event_type_and_never_renders_mentions_or_details(self):
        events = [
            StoredActivityEvent(100 + index, "**edit**", 123, 456, 789, 987)
            for index in range(12)
        ]
        self.activity.get_recent_events.return_value = events

        await self.view.handle_action(FakeInteraction(), "activity")

        text = self.text(self.view)
        self.assertEqual(text.count(r"\*\*edit\*\*"), 10)
        self.assertIn("actor=123 target=456 channel=789 message=987", text)
        self.assertNotIn("<@", text)
        self.assertNotIn("<#", text)
        self.assertNotIn("details_json", text)


if __name__ == "__main__":
    unittest.main()
