from __future__ import annotations

import logging
import time

import discord

from src.codex_bridge_client import (
    MAX_CODEX_ALLOWED_CHANNELS,
    CodexAccess,
    CodexBridgeClient,
    CodexRuntimeStatus,
)
from src.steam_free_games import (
    SteamConfigurationError,
    SteamFetchResult,
    SteamFreeGamesNotifier,
    SteamOffer,
)
from src.temp_voice import TempVoiceManager

MAX_STEAM_OFFERS_SHOWN = 5
PANEL_ACCENT_COLOUR = discord.Colour.from_rgb(88, 101, 242)

MAIN_PAGES = (
    ("overview", "總覽", "控制台首頁"),
    ("ai", "AI 助手", "Codex OAuth 與對話"),
    ("modules", "功能模組", "臨時語音與 Steam 免費遊戲"),
)
MODULE_PAGES = (
    ("voice", "臨時語音", "入口頻道與同步狀態"),
    ("steam", "Steam 免費遊戲", "限時免費查詢"),
)


class _PanelButton(discord.ui.Button["AdminPanelView"]):
    def __init__(
        self,
        action: str,
        label: str,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        super().__init__(label=label, style=style, disabled=disabled)
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, AdminPanelView):
            await view.handle_action(interaction, self.action)


class _PanelSelect(discord.ui.Select["AdminPanelView"]):
    def __init__(
        self,
        pages: tuple[tuple[str, str, str], ...],
        *,
        placeholder: str,
        current: str | None,
    ) -> None:
        super().__init__(
            placeholder=placeholder,
            options=[
                discord.SelectOption(
                    label=label,
                    value=value,
                    description=description,
                    default=value == current,
                )
                for value, label, description in pages
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, AdminPanelView):
            await view.handle_action(interaction, self.values[0])


class _CodexChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, *, disabled: bool, channel_ids: frozenset[int]) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="選擇 AI 白名單文字頻道（可複選）",
            min_values=1,
            max_values=MAX_CODEX_ALLOWED_CHANNELS,
            disabled=disabled,
            default_values=[
                discord.SelectDefaultValue.from_channel(discord.Object(id=channel_id))
                for channel_id in sorted(channel_ids)
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, AdminPanelView):
            return
        channels = tuple(value.resolve() for value in self.values)
        await view.handle_codex_channel_select(interaction, channels)


class _CodexRoleSelect(discord.ui.RoleSelect):
    def __init__(self, *, disabled: bool, role_ids: frozenset[int]) -> None:
        super().__init__(
            placeholder="選擇 AI 白名單身分組（可複選）",
            min_values=1,
            max_values=MAX_CODEX_ALLOWED_CHANNELS,
            disabled=disabled,
            default_values=[
                discord.SelectDefaultValue.from_role(discord.Object(id=role_id))
                for role_id in sorted(role_ids)
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, AdminPanelView):
            await view.handle_codex_role_select(interaction, tuple(self.values))


class _SteamRoleSelect(discord.ui.RoleSelect):
    def __init__(self, *, disabled: bool, configured: bool) -> None:
        super().__init__(
            placeholder=(
                "重新選擇 Steam 通知身分組"
                if configured
                else "選擇 Steam 通知身分組"
            ),
            min_values=1,
            max_values=25,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, AdminPanelView) and self.values:
            await view.handle_steam_role_select(interaction, tuple(self.values))


class AdminPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
        codex_client: CodexBridgeClient,
        codex_access: CodexAccess,
        codex_status: CodexRuntimeStatus,
        temp_voice: TempVoiceManager,
        steam_free_games: SteamFreeGamesNotifier,
        user_role_ids: frozenset[int] = frozenset(),
        temp_voice_enabled: bool = True,
        steam_free_games_enabled: bool = True,
    ) -> None:
        super().__init__(timeout=15 * 60)
        self.user_id = user_id
        self.guild_id = guild_id
        self.codex_client = codex_client
        self.codex_access = codex_access
        self.codex_status = codex_status
        self.temp_voice = temp_voice
        self.steam_free_games = steam_free_games
        self.user_role_ids = user_role_ids
        self.temp_voice_enabled = temp_voice_enabled
        self.steam_free_games_enabled = steam_free_games_enabled
        self._overview_updated_at = int(time.time())
        self.page = "overview"
        self._render_overview()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        allowed = (
            interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
            and interaction.permissions.administrator
        )
        if allowed:
            self.user_role_ids = frozenset(
                role_id
                for role in getattr(interaction.user, "roles", ())
                if type(role_id := getattr(role, "id", None)) is int
                and role_id > 0
            )
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "只有開啟控制台的伺服器管理員可以操作這個控制台。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return False

    def _set_container(self, *children: discord.ui.Item) -> None:
        self.clear_items()
        self.add_item(
            discord.ui.Container(*children, accent_colour=PANEL_ACCENT_COLOUR)
        )

    @staticmethod
    def _row(*buttons: _PanelButton) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(*buttons)

    @staticmethod
    def _main_select(current: str) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            _PanelSelect(MAIN_PAGES, placeholder="選擇頁面", current=current)
        )

    @staticmethod
    def _module_select(current: str | None) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            _PanelSelect(MODULE_PAGES, placeholder="選擇模組", current=current)
        )

    @staticmethod
    def _close_button() -> _PanelButton:
        return _PanelButton("close", "關閉控制台", style=discord.ButtonStyle.danger)

    @staticmethod
    def _title(heading: str, subtitle: str) -> discord.ui.TextDisplay:
        return discord.ui.TextDisplay(f"# {heading}\n-# {subtitle}")

    @staticmethod
    def _gap() -> discord.ui.Separator:
        return discord.ui.Separator(visible=False)

    def _ai_display(self) -> tuple[str, str, str]:
        plan = (
            self.codex_status.plan.replace("_", " ").title()
            if self.codex_status.plan
            else "Unknown"
        )
        runtime = (
            discord.utils.escape_markdown(self.codex_status.runtime_version)
            if self.codex_status.runtime_version
            else "Unknown"
        )
        search = (
            self.codex_status.web_search.title()
            if self.codex_status.web_search
            else "Unknown"
        )
        return plan, runtime, search

    def _voice_summary(self) -> str:
        if not self.temp_voice_enabled:
            return "## 臨時語音\n**依設定停用**\n-# 不會建立、同步或管理語音頻道"
        status = self.temp_voice.get_guild_status(self.guild_id)
        if not status.state_available:
            return "## 臨時語音\n**狀態檔不可用**\n-# 入口無法讀取 · 追蹤暫停"
        entry = (
            f"入口 <#{status.parent_channel_id}>"
            if status.parent_channel_id
            else "尚未綁定入口"
        )
        return (
            "## 臨時語音\n"
            f"**{status.tracked_child_count} 個頻道追蹤中**\n"
            f"-# {entry} · 狀態正常"
        )

    def _steam_summary(self) -> str:
        if not self.steam_free_games_enabled:
            return "## Steam 免費遊戲\n**自動通知依設定停用**\n-# 手動查詢仍可使用"
        status = self.steam_free_games.get_guild_status(self.guild_id)
        if not status.state_available:
            return "## Steam 免費遊戲\n**狀態檔不可用**\n-# 通知頻道無法讀取"
        channel = (
            f"通知 <#{status.channel_id}>"
            if status.channel_id
            else "尚未綁定通知頻道"
        )
        return (
            "## Steam 免費遊戲\n"
            f"**{status.active_app_count} 款活動中**\n"
            f"-# {channel} · 每 {int(status.poll_interval_seconds // 60)} 分鐘檢查 · 狀態正常"
        )

    async def _refresh_ai_status(self) -> None:
        try:
            self.codex_status = await self.codex_client.get_runtime_status()
        except Exception:
            logging.exception("管理控制台讀取 Codex 狀態失敗。")
            self.codex_status = CodexRuntimeStatus(
                False,
                False,
                None,
                None,
                None,
                None,
                0,
            )

    def _render_overview(self) -> None:
        self.page = "overview"
        voice_status = self.temp_voice.get_guild_status(self.guild_id)
        steam_status = self.steam_free_games.get_guild_status(self.guild_id)

        statuses: list[tuple[str, str, str]] = []
        ai_access_enabled = (
            self.codex_access.enabled
            and self.codex_access.guild_id == self.guild_id
        )
        if not self.codex_access.enabled:
            statuses.append(("AI 助手", "停用", "AI 對話目前依設定停用"))
        elif not ai_access_enabled:
            statuses.append(("AI 助手", "停用", "此伺服器不在 AI 白名單"))
        elif not self.codex_access.state_available:
            statuses.append(("AI 助手", "異常", "白名單狀態檔不可用"))
        elif not self.codex_access.channel_ids:
            statuses.append(("AI 助手", "待設定", "白名單頻道尚未設定"))
        elif not self.codex_access.configured:
            statuses.append(
                ("AI 助手", "待設定", "白名單身分組尚未設定" if self.codex_access.mode == "roles"
                 else "白名單身分組或舊使用者尚未設定")
            )
        elif not self.codex_status.available:
            statuses.append(("AI 助手", "異常", "Codex bridge 無法連線"))
        elif not self.codex_status.authenticated:
            statuses.append(("AI 助手", "待設定", "Codex 尚未登入"))
        else:
            statuses.append(("AI 助手", "正常", ""))

        if not self.temp_voice_enabled:
            statuses.append(("臨時語音", "停用", "依設定停用"))
        elif not voice_status.state_available:
            statuses.append(("臨時語音", "異常", "狀態檔不可用，臨時語音功能已停用"))
        elif voice_status.parent_channel_id is None:
            statuses.append(("臨時語音", "待設定", "入口頻道尚未綁定"))
        else:
            statuses.append(("臨時語音", "正常", ""))

        if not self.steam_free_games_enabled:
            statuses.append(("Steam 免費遊戲", "停用", "自動通知依設定停用"))
        elif not steam_status.state_available:
            statuses.append(("Steam 免費遊戲", "異常", "狀態檔不可用，通知功能已停用"))
        elif steam_status.channel_id is None:
            statuses.append(("Steam 免費遊戲", "待設定", "通知頻道尚未綁定"))
        else:
            statuses.append(("Steam 免費遊戲", "正常", ""))

        normal_count = sum(status == "正常" for _name, status, _detail in statuses)
        pending_count = sum(status == "待設定" for _name, status, _detail in statuses)
        error_count = sum(status == "異常" for _name, status, _detail in statuses)
        disabled_count = sum(status == "停用" for _name, status, _detail in statuses)
        disabled_details = [
            f"**{name} · {status}** · {detail}"
            for name, status, detail in statuses
            if name == "AI 助手" and status == "停用"
        ]
        disabled_features = []
        if not self.temp_voice_enabled:
            disabled_features.append("臨時語音")
        if not self.steam_free_games_enabled:
            disabled_features.append("Steam 自動通知")
        if len(disabled_features) > 1:
            disabled_details.append(
                f"{'、'.join(disabled_features[:-1])}與 {disabled_features[-1]}依設定停用"
            )
        elif disabled_features:
            disabled_details.append(f"{disabled_features[0]}依設定停用")
        disabled_text = " · ".join(disabled_details)

        if error_count == 0 and pending_count == 0:
            if disabled_count:
                health_text = (
                    "## 系統狀態\n"
                    "**已啟用功能正常**\n"
                    f"-# {disabled_text}"
                )
            else:
                health_text = (
                    "## 系統狀態\n"
                    "**全部正常**\n"
                    "-# AI 助手、臨時語音與 Steam 免費遊戲皆可用"
                )
        else:
            disabled_suffix = f" · {disabled_count} 個停用" if disabled_count else ""
            health_text = (
                "## 系統狀態\n"
                f"**{normal_count} 個正常 · {pending_count} 個待設定 · "
                f"{error_count} 個異常{disabled_suffix}**"
            )
            if disabled_text:
                health_text += f"\n-# {disabled_text}"

        setup_items: list[tuple[str, bool]] = []
        if ai_access_enabled:
            setup_items.append(
                (
                    "AI 白名單",
                    self.codex_access.configured,
                )
            )
        if self.temp_voice_enabled:
            setup_items.append(
                (
                    "臨時語音入口",
                    voice_status.state_available and voice_status.parent_channel_id is not None,
                )
            )
        if self.steam_free_games_enabled:
            setup_items.append(
                (
                    "Steam 通知頻道",
                    steam_status.state_available and steam_status.channel_id is not None,
                )
            )
        setup_complete = sum(configured for _name, configured in setup_items)
        missing_setup = [name for name, configured in setup_items if not configured]
        if not setup_items:
            setup_text = "**無需設定**"
            setup_detail = disabled_text
        else:
            setup_text = f"**{setup_complete} / {len(setup_items)} 已設定**"
            setup_detail = (
                " · ".join(name for name, _configured in setup_items)
                if not missing_setup
                else f"尚未完成：{' · '.join(missing_setup)}"
            )

        if not self.steam_free_games_enabled:
            steam_tracking = "Steam 自動通知已停用；手動查詢仍可使用"
        elif steam_status.state_available:
            steam_tracking = f"Steam 通知目前追蹤 {steam_status.active_app_count} 款活動"
        else:
            steam_tracking = "Steam 通知活動追蹤狀態無法取得"

        children: list[discord.ui.Item] = [
            self._title("管理控制台", "系統狀態與管理工具"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            self._main_select("overview"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(health_text),
        ]
        issues = [item for item in statuses if item[1] in {"待設定", "異常"}]
        if issues:
            issue_lines = ["## 需要注意"]
            for name, status, detail in issues:
                issue_lines.extend((f"**{name} · {status}**", f"-# {detail}"))
            children.extend((self._gap(), discord.ui.TextDisplay("\n".join(issue_lines))))

        children.extend(
            (
                self._gap(),
                discord.ui.TextDisplay(
                    "## 設定\n"
                    f"{setup_text}\n"
                    f"-# {setup_detail}"
                ),
                self._gap(),
                discord.ui.TextDisplay(
                    f"-# {steam_tracking} · 狀態更新 <t:{self._overview_updated_at}:R>"
                ),
                self._row(_PanelButton("refresh", "重新整理"), self._close_button()),
            )
        )
        self._set_container(*children)

    def _render_ai(self, note: str | None = None) -> None:
        self.page = "ai"
        plan, runtime, search = self._ai_display()
        sdk = self.codex_status.sdk_version or "Unknown"
        authenticated = "已登入" if self.codex_status.authenticated else "未登入"
        configured_guild = self.codex_access.guild_id == self.guild_id
        channel_ids = (
            self.codex_access.channel_ids
            if self.codex_access.state_available
            else frozenset()
        )
        channels = " ".join(f"<#{channel_id}>" for channel_id in sorted(channel_ids))
        role_ids = (
            self.codex_access.role_ids
            if self.codex_access.state_available
            else frozenset()
        )
        roles = " ".join(f"<@&{role_id}>" for role_id in sorted(role_ids))
        current_allowed = self.codex_access.allows(
            self.guild_id,
            next(iter(channel_ids), None),
            self.user_id,
            self.user_role_ids,
        )
        role_detail = (
            "擁有任一所選身分組即可使用"
            if role_ids
            else "請選擇白名單身分組；目前尚未開放"
            if self.codex_access.mode == "roles"
            else f"暫用舊使用者白名單（{len(self.codex_access.user_ids)} 人）"
        )
        if not configured_guild:
            channel_detail = "此伺服器不在 AI 白名單"
        elif not self.codex_access.enabled:
            channel_detail = "AI 對話目前依設定停用"
        elif not self.codex_access.state_available:
            channel_detail = "狀態檔不可用；重新選擇可修復"
        else:
            channel_detail = "僅這些頻道及其 Thread 可使用 AI"
        children: list[discord.ui.Item] = [
            self._title("AI 助手", "官方 Codex OAuth 與持久對話"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            self._main_select("ai"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "## 帳號與服務\n"
                f"**{plan} · {authenticated}**\n"
                f"-# Runtime {runtime} · SDK {sdk} · Web Search {search}"
            ),
            self._gap(),
            discord.ui.TextDisplay(
                "## 對話\n"
                f"**持久 Thread**　{self.codex_status.thread_count} 條\n"
                f"**Bot 工作**　{self.codex_status.bot_active_requests} 個 · 等待 {self.codex_status.bot_queued_requests} 個\n"
                f"**Bridge 工作**　{self.codex_status.active_requests} 個 · 等待 {self.codex_status.queued_requests} 個\n"
                f"**最近錯誤**　{self.codex_status.last_error or '無'}\n"
                "-# 只保存直接 @Bot 或 Reply Bot 的 allowlisted 對話"
            ),
            self._gap(),
            discord.ui.TextDisplay(
                f"## 白名單頻道 {len(channel_ids)} 個\n"
                f"{channels or '尚未設定'}\n-# {channel_detail}"
            ),
            discord.ui.ActionRow(
                _CodexChannelSelect(
                    disabled=not self.codex_access.enabled or not configured_guild,
                    channel_ids=channel_ids,
                )
            ),
            self._gap(),
            discord.ui.TextDisplay(
                f"## 白名單身分組 {len(role_ids)} 個\n"
                f"{roles or '尚未設定'}\n"
                f"-# {role_detail}\n"
                f"-# 目前操作者：{'已允許' if current_allowed else '未允許'}"
            ),
            discord.ui.ActionRow(
                _CodexRoleSelect(
                    disabled=not self.codex_access.enabled or not configured_guild
                    or not self.codex_access.state_available or not channel_ids,
                    role_ids=role_ids,
                )
            ),
            self._gap(),
            discord.ui.TextDisplay(
                "## 安全邊界\n"
                "**Read-only · Deny-all**\n"
                "-# Shell、MCP、Apps、Subagents 與全域 Memories 均停用"
            ),
            self._row(_PanelButton("refresh", "重新整理"), self._close_button()),
        ]
        if note:
            children.insert(
                -1,
                discord.ui.TextDisplay(
                    f"## 最近操作\n-# {discord.utils.escape_markdown(note)}"
                ),
            )
        self._set_container(*children)

    def _render_modules(self) -> None:
        self.page = "modules"
        self._set_container(
            self._title("功能模組", "臨時語音與 Steam 免費遊戲"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            self._main_select("modules"),
            self._module_select(None),
            discord.ui.Separator(),
            discord.ui.TextDisplay(self._voice_summary()),
            self._gap(),
            discord.ui.TextDisplay(self._steam_summary()),
            self._row(_PanelButton("refresh", "重新整理"), self._close_button()),
        )

    def _render_voice(self, note: str | None = None) -> None:
        self.page = "voice"
        status = self.temp_voice.get_guild_status(self.guild_id)
        if not self.temp_voice_enabled:
            state = "依設定停用"
            entry = "不會建立、同步或管理語音頻道"
        elif not status.state_available:
            state = "狀態檔不可用"
            entry = "入口無法讀取"
        else:
            state = "狀態檔正常"
            entry = (
                f"入口 <#{status.parent_channel_id}>"
                if status.parent_channel_id
                else "尚未綁定入口"
            )
        recent = discord.utils.escape_markdown(note) if note else "尚無操作"
        self._set_container(
            self._title("臨時語音", "入口頻道與臨時頻道的同步狀態"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            self._main_select("modules"),
            self._module_select("voice"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "## 目前狀態\n"
                f"**{status.tracked_child_count} 個臨時語音頻道**\n"
                f"-# {entry} · {state}"
            ),
            self._gap(),
            discord.ui.TextDisplay(f"## 最近操作\n-# {recent}"),
            self._row(
                _PanelButton(
                    "voice_sync",
                    "重新同步",
                    style=discord.ButtonStyle.primary,
                    disabled=not self.temp_voice_enabled,
                ),
                self._close_button(),
            ),
        )

    def _offer_item(self, offer: SteamOffer) -> discord.ui.Item:
        name = discord.utils.escape_markdown(offer.name)
        price = discord.utils.escape_markdown(offer.old_price) if offer.old_price else "—"
        text = f"### {name}\n原價　{price}　·　折扣 100%"
        if offer.header_image:
            return discord.ui.Section(
                text,
                accessory=discord.ui.Thumbnail(
                    offer.header_image,
                    description=f"{offer.name} Steam 商店圖片",
                ),
            )
        return discord.ui.TextDisplay(text)

    def _render_steam(
        self,
        result: SteamFetchResult | None = None,
        *,
        error: str | None = None,
        notice: str | None = None,
    ) -> None:
        self.page = "steam"
        status = self.steam_free_games.get_guild_status(self.guild_id)
        if not self.steam_free_games_enabled:
            state = "自動通知依設定停用"
            channel = "手動查詢仍可使用"
            role_status = "身分組通知依設定停用"
        elif not status.state_available:
            state = "狀態檔不可用"
            channel = "通知頻道無法讀取"
            role_status = "身分組設定不可用"
        else:
            state = "狀態檔正常"
            channel = (
                f"通知 <#{status.channel_id}>"
                if status.channel_id
                else "尚未綁定通知頻道"
            )
            role_status = (
                "通知身分組 " + " ".join(f"<@&{role_id}>" for role_id in status.role_ids)
                if status.role_ids
                else "未設定通知身分組"
            )
        role_controls_disabled = (
            not self.steam_free_games_enabled or not status.state_available
        )
        children: list[discord.ui.Item] = [
            self._title("Steam 限時免費", "管理自動通知與手動查詢"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            self._main_select("modules"),
            self._module_select("steam"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "## 通知狀態\n"
                f"**{status.active_app_count} 款活動中**\n"
                f"-# {channel} · {role_status} · 每 {int(status.poll_interval_seconds // 60)} 分鐘檢查 · {state}"
            ),
            discord.ui.ActionRow(
                _SteamRoleSelect(
                    disabled=role_controls_disabled,
                    configured=bool(status.role_ids),
                )
            ),
            self._gap(),
        ]

        if notice:
            children.append(
                discord.ui.TextDisplay(
                    f"## 最近操作\n-# {discord.utils.escape_markdown(notice)}"
                )
            )

        if error:
            children.append(
                discord.ui.TextDisplay(
                    f"## 查詢結果\n-# {discord.utils.escape_markdown(error)}"
                )
            )
        elif result is None:
            children.append(
                discord.ui.TextDisplay(
                    "## 查詢結果\n-# 按「重新查詢」取得目前符合條件的限時免費遊戲。"
                )
            )
        else:
            offers = result.offers[:MAX_STEAM_OFFERS_SHOWN]
            if not offers:
                children.append(
                    discord.ui.TextDisplay(
                        "## 查詢結果\n-# 目前沒有符合條件的限時免費遊戲。"
                    )
                )
            else:
                children.append(discord.ui.TextDisplay("## 查詢結果"))
                children.extend(self._offer_item(offer) for offer in offers)
                if len(result.offers) > len(offers):
                    children.append(
                        discord.ui.TextDisplay(
                            f"-# 只顯示前 {len(offers)} 款，共 {len(result.offers)} 款。"
                        )
                    )

        children.append(
            self._row(
                _PanelButton(
                    "steam_role_clear",
                    "取消身分組通知",
                    disabled=role_controls_disabled or not status.role_ids,
                ),
                _PanelButton("steam_query", "重新查詢", style=discord.ButtonStyle.primary),
                self._close_button(),
            )
        )
        self._set_container(*children)

    def _render_closed(self) -> None:
        self.page = "closed"
        self._set_container(
            self._title("管理控制台", "控制台已關閉，重新輸入 /控制台 可再開啟。"),
            self._row(_PanelButton("closed", "已關閉", disabled=True)),
        )
        self.stop()

    def _render_page(self, page: str) -> None:
        if page == "ai":
            self._render_ai()
        elif page == "modules":
            self._render_modules()
        elif page == "voice":
            self._render_voice()
        elif page == "steam":
            self._render_steam()
        else:
            self._render_overview()

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_codex_channel_select(
        self,
        interaction: discord.Interaction,
        channels: tuple[object | None, ...],
    ) -> None:
        await interaction.response.defer()
        async with self.codex_access.mutation_lock:
            guild_id = getattr(getattr(interaction, "guild", None), "id", None)
            channel_ids = [getattr(channel, "id", None) for channel in channels]
            if (
                not self.codex_access.enabled
                or guild_id != self.guild_id
                or guild_id != self.codex_access.guild_id
                or not 1 <= len(channels) <= MAX_CODEX_ALLOWED_CHANNELS
                or any(
                    getattr(getattr(channel, "guild", None), "id", None) != guild_id
                    or getattr(channel, "type", None) != discord.ChannelType.text
                    or type(channel_id) is not int
                    or channel_id <= 0
                    for channel, channel_id in zip(channels, channel_ids, strict=True)
                )
                or len(channel_ids) != len(set(channel_ids))
            ):
                self._render_ai("只能選擇目前伺服器的一般文字頻道。")
            else:
                selected = frozenset(channel_ids)
                try:
                    previous = self.codex_access.set_channels(guild_id, selected)
                except (OSError, ValueError):
                    logging.error("管理控制台保存 Codex 白名單頻道失敗。")
                    self._render_ai("白名單頻道無法保存，設定未變更。")
                else:
                    count = len(selected)
                    if previous == selected:
                        note = f"目前已設定 {count} 個白名單頻道。"
                    elif previous - selected:
                        self.codex_access.suspend()
                        try:
                            await self.codex_client.archive_scope(guild_id)
                        except Exception:
                            logging.error("管理控制台封存舊 Codex 對話失敗。")
                            note = f"已更新 {count} 個白名單頻道，但舊對話封存失敗。"
                        else:
                            note = f"已更新 {count} 個白名單頻道並封存舊對話。"
                        finally:
                            self.codex_access.resume()
                    else:
                        note = f"已更新 {count} 個白名單頻道。"
                    self._render_ai(note)
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_codex_role_select(
        self,
        interaction: discord.Interaction,
        roles: tuple[discord.Role, ...],
    ) -> None:
        await interaction.response.defer()
        async with self.codex_access.mutation_lock:
            guild_id = getattr(getattr(interaction, "guild", None), "id", None)
            self.user_role_ids = frozenset(
                role_id
                for role in getattr(interaction.user, "roles", ())
                if type(role_id := getattr(role, "id", None)) is int and role_id > 0
            )
            role_ids = [getattr(role, "id", None) for role in roles]
            if (
                not self.codex_access.enabled
                or guild_id != self.guild_id
                or guild_id != self.codex_access.guild_id
                or not self.codex_access.state_available
                or not self.codex_access.channel_ids
                or not 1 <= len(roles) <= MAX_CODEX_ALLOWED_CHANNELS
                or any(
                    getattr(getattr(role, "guild", None), "id", None) != guild_id
                    or role.is_default()
                    or type(role_id) is not int
                    or role_id <= 0
                    for role, role_id in zip(roles, role_ids, strict=True)
                )
                or len(role_ids) != len(set(role_ids))
            ):
                self._render_ai("只能選擇目前伺服器的一般身分組。")
            else:
                selected = frozenset(role_ids)
                if selected == self.codex_access.role_ids:
                    try:
                        self.codex_access.set_roles(guild_id, selected)
                    except (OSError, ValueError):
                        logging.error("管理控制台保存 Codex 白名單身分組失敗。")
                        self._render_ai("白名單身分組無法保存，設定未變更。")
                    else:
                        self._render_ai(f"目前已設定 {len(selected)} 個白名單身分組。")
                else:
                    self.codex_access.suspend()
                    note: str
                    try:
                        await self.codex_client.archive_scope(guild_id)
                    except Exception:
                        logging.error("管理控制台切換白名單身分組前封存對話失敗。")
                        note = "舊對話封存失敗，角色設定未變更。"
                    else:
                        try:
                            self.codex_access.set_roles(guild_id, selected)
                        except (OSError, ValueError):
                            logging.error("管理控制台保存 Codex 白名單身分組失敗。")
                            note = "舊對話已封存，但角色設定無法保存。"
                        else:
                            note = f"已更新 {len(selected)} 個白名單身分組。"
                    finally:
                        self.codex_access.resume()
                    self._render_ai(note)
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_steam_role_select(
        self,
        interaction: discord.Interaction,
        roles: tuple[discord.Role, ...],
    ) -> None:
        await interaction.response.defer()
        if not self.steam_free_games_enabled:
            self._render_steam(notice="Steam 自動通知已停用，未修改身分組設定。")
        elif interaction.guild is None:
            self._render_steam(notice="無法取得目前伺服器。")
        else:
            try:
                await self.steam_free_games.set_notification_roles(interaction.guild, roles)
            except SteamConfigurationError as exc:
                self._render_steam(notice=str(exc))
            else:
                self._render_steam(
                    notice=f"已更新 Steam 免費遊戲通知身分組，共 {len(roles)} 個。"
                )
        await interaction.edit_original_response(
            view=self,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def handle_action(self, interaction: discord.Interaction, action: str) -> None:
        if action in ("overview", "modules", "voice", "steam"):
            self._render_page(action)
            await self._edit(interaction)
            return
        if action == "ai":
            await interaction.response.defer()
            await self._refresh_ai_status()
            self._render_ai()
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if action == "refresh":
            await interaction.response.defer()
            if self.page in ("overview", "ai"):
                await self._refresh_ai_status()
            if self.page == "overview":
                self._overview_updated_at = int(time.time())
            self._render_page(self.page)
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if action == "close":
            self._render_closed()
            await self._edit(interaction)
            return

        if action == "voice_sync":
            await interaction.response.defer()
            if not self.temp_voice_enabled:
                self._render_voice("功能已停用，未執行同步。")
                await interaction.edit_original_response(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            guild = interaction.guild
            if guild is None:
                self._render_voice("無法取得目前伺服器。")
            else:
                try:
                    await self.temp_voice.reconcile([guild], prune_absent=False)
                except Exception:
                    logging.exception("管理控制台重新同步臨時語音失敗。")
                    self._render_voice("重新同步失敗，請查看 Bot 紀錄。")
                else:
                    self._render_voice("已重新執行同步流程。")
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action == "steam_role_clear":
            await interaction.response.defer()
            if not self.steam_free_games_enabled:
                self._render_steam(notice="Steam 自動通知已停用，未修改身分組設定。")
            else:
                try:
                    removed = await self.steam_free_games.clear_notification_roles(
                        self.guild_id
                    )
                except SteamConfigurationError as exc:
                    self._render_steam(notice=str(exc))
                else:
                    self._render_steam(
                        notice=(
                            "已取消 Steam 免費遊戲通知身分組。"
                            if removed
                            else "目前沒有設定 Steam 通知身分組。"
                        )
                    )
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if action == "steam_query":
            await interaction.response.defer()
            try:
                result = await self.steam_free_games.fetch_current_offers()
            except Exception:
                logging.exception("管理控制台查詢 Steam 免費遊戲失敗。")
                self._render_steam(error="目前無法取得 Steam 資料。")
            else:
                if result is None:
                    self._render_steam(error="目前無法取得 Steam 資料。")
                else:
                    self._render_steam(result)
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
