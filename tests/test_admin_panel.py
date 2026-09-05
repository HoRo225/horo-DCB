from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord

from src.admin_panel import MAX_STEAM_OFFERS_SHOWN, AdminPanelView
from src.codex_bridge_client import CodexAccess, CodexRuntimeStatus
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
    def __init__(
        self, *, user_id=1, role_ids=(), guild_id=10,
        administrator=True, response_done=False,
    ):
        self.user = SimpleNamespace(
            id=user_id,
            roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        )
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
        self.permissions = SimpleNamespace(administrator=administrator)
        self.response = FakeResponse(done=response_done)
        self.original_edits = []

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)


def make_role(role_id, *, guild_id=10, default=False):
    return SimpleNamespace(
        id=role_id,
        guild=SimpleNamespace(id=guild_id),
        is_default=lambda: default,
    )


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
        self.codex_access = CodexAccess(True, 10, 20, frozenset({1}))
        self.codex_client = SimpleNamespace(
            internal_value="SENSITIVE_VALUE",
            get_runtime_status=AsyncMock(return_value=self.codex_status),
            archive_scope=AsyncMock(),
        )
        self.temp_voice = FakeTempVoice()
        self.steam = FakeSteam()
        self.view = AdminPanelView(
            user_id=1,
            guild_id=10,
            codex_client=self.codex_client,
            codex_access=self.codex_access,
            codex_status=self.codex_status,
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

    @classmethod
    def channel_selects(cls, view):
        return [
            component
            for component in cls.all_components(view)
            if component.get("type") == discord.ComponentType.channel_select.value
        ]

    @classmethod
    def role_selects(cls, view):
        return [
            component
            for component in cls.all_components(view)
            if component.get("type") == discord.ComponentType.role_select.value
        ]

    @staticmethod
    def selected(select):
        return [option["value"] for option in select["options"] if option.get("default")]

    def test_overview_shows_health_and_setup_without_internal_detail(self):
        self.assertTrue(self.view.has_components_v2())
        text = self.text(self.view)
        self.assertIn("# 管理控制台\n-# 系統狀態與管理工具", text)
        self.assertIn("## 系統狀態\n**全部正常**", text)
        self.assertIn("AI 助手、臨時語音與 Steam 免費遊戲皆可用", text)
        self.assertNotIn("## 需要注意", text)
        self.assertIn("## 設定\n**3 / 3 已設定**", text)
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

        self.assertIn("**1 個正常 · 1 個待設定 · 1 個異常**", text)
        self.assertIn("## 需要注意", text)
        self.assertIn("**AI 助手 · 異常**", text)
        self.assertIn("Codex bridge 無法連線", text)
        self.assertIn("**臨時語音 · 待設定**", text)
        self.assertIn("入口頻道尚未綁定", text)
        self.assertNotIn("**Steam 免費遊戲 · 正常**", text)
        self.assertIn("## 設定\n**2 / 3 已設定**", text)
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
        self.assertIn("## 設定\n**2 / 3 已設定**", text)
        self.assertIn("尚未完成：Steam 通知頻道", text)
        self.assertIn("Steam 通知目前追蹤 0 款活動", text)

    def test_overview_reports_disabled_unconfigured_and_corrupt_ai_access(self):
        corrupt = CodexAccess(True, 10, 20, frozenset({1}))
        corrupt.state_available = False
        cases = (
            (
                "disabled",
                CodexAccess(False, 10, 20, frozenset({1})),
                "**AI 助手 · 停用**",
                "AI 對話目前依設定停用",
            ),
            (
                "other_guild",
                CodexAccess(True, 11, 20, frozenset({1})),
                "**AI 助手 · 停用**",
                "此伺服器不在 AI 白名單",
            ),
            (
                "missing_channels",
                CodexAccess(True, 10, None, frozenset({1})),
                "**AI 助手 · 待設定**",
                "白名單頻道尚未設定",
            ),
            (
                "missing_mode",
                CodexAccess(True, 10, 20, frozenset()),
                "**AI 助手 · 待設定**",
                "白名單身分組或舊使用者尚未設定",
            ),
            (
                "corrupt",
                corrupt,
                "**AI 助手 · 異常**",
                "白名單狀態檔不可用",
            ),
        )

        for name, access, heading, detail in cases:
            with self.subTest(name=name):
                view = AdminPanelView(
                    user_id=1,
                    guild_id=10,
                    codex_client=self.codex_client,
                    codex_access=access,
                    codex_status=self.codex_status,
                    temp_voice=self.temp_voice,
                    steam_free_games=self.steam,
                )
                text = self.text(view)
                self.assertIn(heading, text)
                self.assertIn(detail, text)
                if "待設定" in heading or "異常" in heading:
                    self.assertIn("AI 白名單", text)
                    self.assertNotIn("## 系統狀態\n**全部正常**", text)

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

        self.assertIn("**1 個正常 · 0 個待設定 · 2 個異常**", text)
        self.assertIn("臨時語音功能已停用", text)
        self.assertIn("通知功能已停用", text)
        self.assertIn("## 設定\n**1 / 3 已設定**", text)
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

    def test_main_navigation_excludes_retired_activity(self):
        self.assertEqual(
            [
                option["value"]
                for option in self.selects(self.view)[0]["options"]
            ],
            ["overview", "ai", "modules"],
        )

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

    def test_ai_page_has_multi_text_channel_selector_with_current_defaults(self):
        self.codex_access.set_channels(10, frozenset({20, 21}))
        self.view._render_ai()

        channel_selects = self.channel_selects(self.view)
        self.assertEqual(len(channel_selects), 1)
        self.assertEqual(channel_selects[0]["min_values"], 1)
        self.assertEqual(channel_selects[0]["max_values"], 25)
        self.assertEqual(
            channel_selects[0]["channel_types"],
            [discord.ChannelType.text.value],
        )
        self.assertEqual(
            channel_selects[0]["default_values"],
            [
                {"id": 20, "type": "channel"},
                {"id": 21, "type": "channel"},
            ],
        )
        self.assertIn("白名單頻道 2 個", self.text(self.view))
        self.assertIn("<#20> <#21>", self.text(self.view))

    def test_ai_page_shows_legacy_user_mode_and_current_operator_status(self):
        self.view._render_ai()

        role_select = self.role_selects(self.view)[0]
        self.assertEqual(role_select["min_values"], 1)
        self.assertEqual(role_select["max_values"], 25)
        self.assertEqual(role_select.get("default_values", []), [])
        text = self.text(self.view)
        self.assertIn("暫用舊使用者白名單（1 人）", text)
        self.assertIn("目前操作者：已允許", text)

    def test_ai_page_preselects_roles_and_reports_operator_access(self):
        self.codex_access.set_roles(10, frozenset({70, 80}))
        self.view.user_role_ids = frozenset({60, 80})

        self.view._render_ai()

        role_select = self.role_selects(self.view)[0]
        self.assertEqual(
            role_select["default_values"],
            [
                {"id": 70, "type": "role"},
                {"id": 80, "type": "role"},
            ],
        )
        text = self.text(self.view)
        self.assertIn("白名單身分組 2 個", text)
        self.assertIn("<@&70> <@&80>", text)
        self.assertIn("目前操作者：已允許", text)

        self.view.user_role_ids = frozenset({60})
        self.view._render_ai()
        self.assertIn("目前操作者：未允許", self.text(self.view))

    async def test_role_change_archives_before_applying_new_roles(self):
        self.codex_access.set_roles(10, frozenset({70}))
        interaction = FakeInteraction(role_ids=(80,))
        roles = (make_role(80),)

        async def archive(guild_id):
            self.assertEqual(guild_id, 10)
            self.assertEqual(self.codex_access.role_ids, frozenset({70}))
            self.assertFalse(
                self.codex_access.allows(10, 20, 1, frozenset({70}))
            )

        self.codex_client.archive_scope.side_effect = archive

        await self.view.handle_codex_role_select(interaction, roles)

        self.assertEqual(self.codex_access.role_ids, frozenset({80}))
        self.codex_client.archive_scope.assert_awaited_once_with(10)
        self.assertIn("已更新 1 個白名單身分組", self.text(self.view))
        self.assertIn("目前操作者：已允許", self.text(self.view))

    async def test_role_archive_failure_keeps_old_roles(self):
        self.codex_access.set_roles(10, frozenset({70}))
        interaction = FakeInteraction(role_ids=(80,))
        self.codex_client.archive_scope.side_effect = RuntimeError("SENSITIVE_DETAIL")

        with self.assertLogs(level="ERROR"):
            await self.view.handle_codex_role_select(
                interaction,
                (make_role(80),),
            )

        self.assertEqual(self.codex_access.role_ids, frozenset({70}))
        text = self.text(self.view)
        self.assertIn("角色設定未變更", text)
        self.assertNotIn("SENSITIVE_DETAIL", text)

    async def test_role_select_rejects_default_or_other_guild_role(self):
        for role in (
            make_role(10, default=True),
            make_role(80, guild_id=11),
        ):
            with self.subTest(role_id=role.id):
                interaction = FakeInteraction(role_ids=(role.id,))

                await self.view.handle_codex_role_select(interaction, (role,))

                self.assertEqual(self.codex_access.role_ids, frozenset())
                self.codex_client.archive_scope.assert_not_awaited()
                self.assertIn("只能選擇目前伺服器的一般身分組", self.text(self.view))

    async def test_adding_channel_does_not_archive_existing_conversations(self):
        interaction = FakeInteraction()
        channels = (
            SimpleNamespace(id=20, type=discord.ChannelType.text, guild=interaction.guild),
            SimpleNamespace(id=21, type=discord.ChannelType.text, guild=interaction.guild),
        )

        await self.view.handle_codex_channel_select(interaction, channels)

        self.assertTrue(self.codex_access.allows(10, 20, 1))
        self.assertTrue(self.codex_access.allows(10, 21, 1))
        self.codex_client.archive_scope.assert_not_awaited()
        self.assertIn("已更新 2 個白名單頻道", self.text(self.view))

    async def test_removing_channel_archives_guild_while_access_is_suspended(self):
        self.codex_access.set_channels(10, frozenset({20, 21}))
        interaction = FakeInteraction()
        channels = (
            SimpleNamespace(id=21, type=discord.ChannelType.text, guild=interaction.guild),
        )

        async def archive(guild_id):
            self.assertEqual(guild_id, 10)
            self.assertFalse(self.codex_access.allows(10, 21, 1))

        self.codex_client.archive_scope.side_effect = archive

        await self.view.handle_codex_channel_select(interaction, channels)

        self.assertTrue(self.codex_access.allows(10, 21, 1))
        self.assertFalse(self.codex_access.allows(10, 20, 1))
        self.codex_client.archive_scope.assert_awaited_once_with(10)
        self.assertEqual(interaction.response.defer_count, 1)
        self.assertEqual(len(interaction.original_edits), 1)
        self.assertIn("已更新 1 個白名單頻道並封存舊對話", self.text(self.view))

    async def test_same_channels_are_saved_without_archiving(self):
        interaction = FakeInteraction()
        channels = (
            SimpleNamespace(id=20, type=discord.ChannelType.text, guild=interaction.guild),
        )

        await self.view.handle_codex_channel_select(interaction, channels)

        self.codex_client.archive_scope.assert_not_awaited()
        self.assertTrue(self.codex_access.allows(10, 20, 1))
        self.assertIn("目前已設定 1 個白名單頻道", self.text(self.view))

    async def test_archive_failure_keeps_new_channel_and_reports_safe_warning(self):
        self.codex_access.set_channels(10, frozenset({20, 21}))
        interaction = FakeInteraction()
        channels = (
            SimpleNamespace(id=21, type=discord.ChannelType.text, guild=interaction.guild),
        )
        self.codex_client.archive_scope.side_effect = RuntimeError("SENSITIVE_DETAIL")

        with self.assertLogs(level="ERROR"):
            await self.view.handle_codex_channel_select(interaction, channels)

        self.assertTrue(self.codex_access.allows(10, 21, 1))
        text = self.text(self.view)
        self.assertIn("舊對話封存失敗", text)
        self.assertNotIn("SENSITIVE_DETAIL", text)

    async def test_channel_select_rejects_voice_or_other_guild(self):
        for channels in (
            (
                SimpleNamespace(
                    id=21,
                    type=discord.ChannelType.voice,
                    guild=SimpleNamespace(id=10),
                ),
            ),
            (
                SimpleNamespace(
                    id=22,
                    type=discord.ChannelType.text,
                    guild=SimpleNamespace(id=11),
                ),
            ),
        ):
            with self.subTest(channel_id=channels[0].id):
                interaction = FakeInteraction()

                await self.view.handle_codex_channel_select(interaction, channels)

                self.assertTrue(self.codex_access.allows(10, 20, 1))
                self.codex_client.archive_scope.assert_not_awaited()
                self.assertIn("只能選擇目前伺服器的一般文字頻道", self.text(self.view))

    def test_other_guild_cannot_use_channel_selector(self):
        self.view.guild_id = 11

        self.view._render_ai()

        channel_select = self.channel_selects(self.view)[0]
        self.assertTrue(channel_select["disabled"])
        self.assertIn("此伺服器不在 AI 白名單", self.text(self.view))

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
            codex_access=self.codex_access,
            codex_status=self.codex_status,
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
            codex_client=self.codex_client,
            codex_access=self.codex_access,
            codex_status=self.codex_status,
            temp_voice=self.temp_voice,
            steam_free_games=self.steam,
            temp_voice_enabled=False,
            steam_free_games_enabled=False,
        )

        text = self.text(view)
        self.assertIn("已啟用功能正常", text)
        self.assertIn("臨時語音與 Steam 自動通知依設定停用", text)
        self.assertIn("## 設定\n**1 / 1 已設定**", text)
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
