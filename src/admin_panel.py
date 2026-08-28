from __future__ import annotations

import logging
import time

import discord

from src.ai_client import AIClient, AIRuntimeStatus
from src.chat import MAX_AGENT_TURNS, MAX_TOTAL_TOOL_CALLS, ChatManager
from src.server_activity import (
    ActivitySummary,
    ServerActivityMonitor,
    ServerActivityStatus,
    StoredActivityEvent,
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
    ("ai", "AI 助手", "模型、對話與 Agent 設定"),
    ("modules", "功能模組", "臨時語音與 Steam 免費遊戲"),
    ("activity", "伺服器活動", "活動監聽與稽核紀錄"),
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


class _ActivityFilterSelect(discord.ui.Select["AdminPanelView"]):
    def __init__(self, current: str) -> None:
        options = (
            ("all", "全部"),
            ("admin", "管理"),
            ("member", "成員"),
            ("message", "訊息"),
            ("voice", "語音"),
        )
        super().__init__(
            placeholder="篩選活動",
            options=[
                discord.SelectOption(label=label, value=value, default=value == current)
                for value, label in options
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, AdminPanelView):
            await view.handle_action(interaction, f"activity_filter:{self.values[0]}")


class AdminPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
        chat: ChatManager,
        ai_client: AIClient,
        ai_status: AIRuntimeStatus,
        temp_voice: TempVoiceManager,
        steam_free_games: SteamFreeGamesNotifier,
        temp_voice_enabled: bool = True,
        steam_free_games_enabled: bool = True,
        server_activity: ServerActivityMonitor | None = None,
    ) -> None:
        super().__init__(timeout=15 * 60)
        self.user_id = user_id
        self.guild_id = guild_id
        self.chat = chat
        self.ai_client = ai_client
        self.ai_status = ai_status
        self.temp_voice = temp_voice
        self.steam_free_games = steam_free_games
        self.temp_voice_enabled = temp_voice_enabled
        self.steam_free_games_enabled = steam_free_games_enabled
        self.server_activity = server_activity
        self.activity_filter = "all"
        self._activity_summary: ActivitySummary | None = None
        self._activity_recent: tuple[StoredActivityEvent, ...] = ()
        self._activity_error = False
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

    @staticmethod
    def _effort_label(effort: str | None) -> str:
        if not effort:
            return "無法取得"
        normalized = effort.strip().lower()
        labels = {
            "auto": "Auto",
            "none": "None",
            "minimal": "Minimal",
            "low": "Low",
            "medium": "Medium",
            "high": "High",
            "xhigh": "XHigh",
            "max": "Max",
        }
        return labels.get(normalized, discord.utils.escape_markdown(effort.strip()))

    def _ai_display(self) -> tuple[str, str, str]:
        model = (
            discord.utils.escape_markdown(self.ai_status.model_name)
            if self.ai_status.model_name
            else "無法取得模型"
        )
        effort = self._effort_label(self.ai_status.effort)
        if not self.ai_status.router_available:
            router = "無法連線"
        elif not self.ai_status.router_version:
            router = "已連線 · 版本未知"
        else:
            version = discord.utils.escape_markdown(self.ai_status.router_version)
            if not version.lower().startswith("v"):
                version = f"v{version}"
            router = f"已連線 · {version}"
        return model, effort, router

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
            self.ai_status = await self.ai_client.get_runtime_status()
        except Exception:
            logging.exception("管理控制台讀取 9Router 狀態失敗。")
            self.ai_status = AIRuntimeStatus(None, None, False, None)

    def _render_overview(self) -> None:
        self.page = "overview"
        voice_status = self.temp_voice.get_guild_status(self.guild_id)
        steam_status = self.steam_free_games.get_guild_status(self.guild_id)

        statuses: list[tuple[str, str, str]] = []
        if not self.ai_status.router_available:
            statuses.append(("AI 助手", "異常", "9Router 無法連線"))
        elif not self.ai_status.model_name:
            statuses.append(("AI 助手", "異常", "模型資訊無法完整取得"))
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

        activity_status = (
            self.server_activity.get_runtime_status()
            if self.server_activity is not None
            else None
        )
        if activity_status is None:
            statuses.append(("伺服器活動", "停用", "依設定停用"))
        elif not activity_status.available:
            statuses.append(("伺服器活動", "異常", "活動儲存空間不可用"))
        elif activity_status.dropped_event_count:
            statuses.append(("伺服器活動", "異常", "有活動紀錄因佇列已滿而遺失"))
        else:
            statuses.append(("伺服器活動", "正常", ""))

        normal_count = sum(status == "正常" for _name, status, _detail in statuses)
        pending_count = sum(status == "待設定" for _name, status, _detail in statuses)
        error_count = sum(status == "異常" for _name, status, _detail in statuses)
        disabled_count = sum(status == "停用" for _name, status, _detail in statuses)
        disabled_features = []
        if not self.temp_voice_enabled:
            disabled_features.append("臨時語音")
        if not self.steam_free_games_enabled:
            disabled_features.append("Steam 自動通知")
        if self.server_activity is None:
            disabled_features.append("伺服器活動")
        if len(disabled_features) > 1:
            disabled_text = f"{'、'.join(disabled_features[:-1])}與 {disabled_features[-1]}依設定停用"
        elif disabled_features:
            disabled_text = f"{disabled_features[0]}依設定停用"
        else:
            disabled_text = ""

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
                    "-# AI 助手、臨時語音、Steam 免費遊戲與伺服器活動皆可用"
                )
        else:
            disabled_suffix = f" · {disabled_count} 個停用" if disabled_count else ""
            health_text = (
                "## 系統狀態\n"
                f"**{normal_count} 個正常 · {pending_count} 個待設定 · "
                f"{error_count} 個異常{disabled_suffix}**"
            )

        setup_items: list[tuple[str, bool]] = []
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

    def _render_activity(self) -> None:
        self.page = "activity"
        status: ServerActivityStatus | None = (
            self.server_activity.get_runtime_status()
            if self.server_activity is not None
            else None
        )
        children: list[discord.ui.Item] = [
            self._title("伺服器活動", "活動監聽與稽核紀錄"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            self._main_select("activity"),
            discord.ui.ActionRow(_ActivityFilterSelect(self.activity_filter)),
            discord.ui.Separator(),
        ]
        if status is None:
            state_text = "## 目前狀態\n**依設定停用**\n-# 不會讀取活動資料 · 保存期限 30 天"
        elif self._activity_error or not status.available:
            state_text = "## 目前狀態\n**目前無法取得伺服器活動**\n-# 請稍後重新整理 · 保存期限 30 天"
        else:
            state_text = (
                "## 目前狀態\n"
                "**活動監聽正常**\n"
                f"-# 30 天保存 · 佇列 {status.queue_size} / {status.queue_capacity} · "
                f"已遺失 {status.dropped_event_count} 筆"
            )
        children.append(discord.ui.TextDisplay(state_text))

        if status is not None and status.available and not self._activity_error:
            summary = self._activity_summary or ActivitySummary()
            children.extend(
                (
                    self._gap(),
                    discord.ui.TextDisplay(
                        "## 最近 24 小時\n"
                        f"**總計 {summary.total}**　管理 {summary.admin}　成員 {summary.member}　"
                        f"訊息 {summary.message}　語音 {summary.voice}　其他 {summary.other}"
                    ),
                    self._gap(),
                    discord.ui.TextDisplay("## 最近活動"),
                )
            )
            if not self._activity_recent:
                children.append(discord.ui.TextDisplay("-# 尚無活動紀錄。"))
            else:
                for event in self._activity_recent[:10]:
                    ids = " ".join(
                        f"{name}={value}"
                        for name, value in (
                            ("actor", event.actor_id),
                            ("target", event.target_id),
                            ("channel", event.channel_id),
                            ("message", event.message_id),
                        )
                        if value is not None
                    )
                    event_type = discord.utils.escape_markdown(event.event_type)
                    suffix = f" · {ids}" if ids else ""
                    children.append(
                        discord.ui.TextDisplay(
                            f"<t:{event.occurred_at}:f> · **{event_type}**{suffix}"
                        )
                    )
        children.append(self._row(_PanelButton("refresh", "重新整理"), self._close_button()))
        self._set_container(*children)

    async def _refresh_activity(self) -> None:
        if self.server_activity is None:
            self._activity_summary = None
            self._activity_recent = ()
            self._activity_error = False
            return
        try:
            summary = await self.server_activity.get_summary(self.guild_id)
            recent = await self.server_activity.get_recent_events(
                self.guild_id, self.activity_filter, limit=10
            )
        except Exception:
            logging.error("管理控制台讀取伺服器活動失敗。")
            self._activity_summary = None
            self._activity_recent = ()
            self._activity_error = True
        else:
            self._activity_summary = summary
            self._activity_recent = tuple(recent[:10])
            self._activity_error = False

    def _render_ai(self) -> None:
        self.page = "ai"
        model, effort, router = self._ai_display()
        self._set_container(
            self._title("AI 助手", "由 9Router 即時提供模型與服務狀態"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            self._main_select("ai"),
            discord.ui.Separator(),
            discord.ui.TextDisplay(
                "## 模型與服務\n"
                f"**{model}**\n"
                f"-# Effort {effort} · 9Router {router}"
            ),
            self._gap(),
            discord.ui.TextDisplay(
                "## 對話\n"
                f"**記憶**　{self.chat.history_limit} 則　"
                f"**Context**　{self.chat.context_char_limit:,} 字元　"
                f"**Cooldown**　{self.chat.cooldown_seconds:g} 秒"
            ),
            self._gap(),
            discord.ui.TextDisplay(
                "## Agent\n"
                f"**逾時**　{self.chat.agent_timeout_seconds:g} 秒　"
                f"**模型回合**　{MAX_AGENT_TURNS}　"
                f"**工具呼叫**　{MAX_TOTAL_TOOL_CALLS} 次"
            ),
            self._row(_PanelButton("refresh", "重新整理"), self._close_button()),
        )

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
        elif page == "activity":
            self._render_activity()
        else:
            self._render_overview()

    async def _edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
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
        if action == "activity" or action.startswith("activity_filter:"):
            if action.startswith("activity_filter:"):
                requested_filter = action.partition(":")[2]
                if requested_filter in {"all", "admin", "member", "message", "voice"}:
                    self.activity_filter = requested_filter
            await interaction.response.defer()
            await self._refresh_activity()
            self._render_activity()
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
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
            if self.page == "activity":
                await self._refresh_activity()
                self._render_activity()
                await interaction.edit_original_response(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
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
