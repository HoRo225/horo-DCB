from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import logging
import time

import aiohttp
import discord

from src.brand import BRAND_COLOUR, CARD_FILENAME, brand_files, branded_title
from src.admin.sessions import PanelSession, PanelSessionRegistry
from src.ai.access import MAX_CODEX_ALLOWED_CHANNELS, CodexAccess, member_role_ids, valid_allowlist_ids
from src.ai.access_service import AiAccessService
from src.ai.client import CodexBridgeClient
from src.ai.protocol import CodexRateLimits, CodexRuntimeStatus, EMPTY_CODEX_RUNTIME_STATUS
from src.discord_utils import is_text_channel, missing_channel_permissions
from src.steam.notifier import (
    SteamConfigurationError,
    SteamFreeGamesNotifier,
    SteamGuildStatus,
)
from src.steam.provider import SteamFetchResult
from src.steam.views import offer_items
from src.voice.manager import TempVoiceGuildStatus, TempVoiceManager

MAX_STEAM_OFFERS_SHOWN = 5
RATE_REFRESH_SECONDS = 30.0
RATE_EXPIRY_MARGIN_SECONDS = 10.0
RATE_PAGES = frozenset({"overview", "ai", "ai_tech"})

MAIN_PAGES = (
    ("overview", "總覽", "控制台首頁"),
    ("ai", "AI 助手", "Codex OAuth 與對話"),
    ("modules", "功能模組", "臨時語音與 Steam 免費遊戲"),
)
AI_PAGES = (
    ("ai", "狀態", "帳號狀態與額度"),
    ("ai_access", "使用權限", "頻道與身分組白名單"),
    ("ai_tech", "技術資訊", "版本、工作與安全邊界"),
)
MODULE_PAGES = (
    ("voice", "臨時語音", "入口頻道與同步狀態"),
    ("steam", "Steam 免費遊戲", "通知設定與手動查詢"),
)
PAGES = frozenset(page for group in (MAIN_PAGES, AI_PAGES, MODULE_PAGES) for page, *_ in group)


@dataclass(frozen=True, slots=True)
class _VoiceState:
    status: TempVoiceGuildStatus
    problem: str | None
    label: str
    detail: str
    entry: str
    next_step: str | None


@dataclass(frozen=True, slots=True)
class _SteamState:
    status: SteamGuildStatus
    problem: str | None
    label: str
    detail: str
    channel: str
    role_status: str
    next_step: str | None


@dataclass(frozen=True, slots=True)
class _AiState:
    label: str
    detail: str
    allowlist_detail: str | None
    stale_counts: tuple[int | None, int | None]
    permission_detail: str | None


async def load_codex_status(client: CodexBridgeClient) -> CodexRuntimeStatus:
    try:
        return await client.get_runtime_status()
    except Exception:
        logging.exception("管理控制台讀取 Codex 狀態失敗。")
        return EMPTY_CODEX_RUNTIME_STATUS


async def load_codex_rate_limits(client: CodexBridgeClient) -> CodexRateLimits:
    try:
        return await client.get_rate_limits()
    except Exception:
        logging.error("管理控制台讀取 Codex 額度失敗。")
        return CodexRateLimits(error="unavailable")


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
        self.pages = pages
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
            page = self.values[0]
            action = (
                "noop"
                if page == view.page and view._layout_is_published()
                else page
            )
            await view.handle_action(interaction, action)


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
        access_service: AiAccessService,
        panel_registry: PanelSessionRegistry,
        panel_session: PanelSession,
        user_role_ids: frozenset[int] = frozenset(),
        temp_voice_enabled: bool = True,
        steam_free_games_enabled: bool = True,
    ) -> None:
        super().__init__(timeout=15 * 60)
        self.user_id = user_id
        self.guild_id = guild_id
        self.codex_client = codex_client
        self.codex_access = codex_access
        self.access_service = access_service
        self.codex_status = codex_status
        self.temp_voice = temp_voice
        self.steam_free_games = steam_free_games
        self._steam_result: SteamFetchResult | None = None
        self._steam_error: str | None = None
        self._steam_fetched_at: int | None = None
        self.user_role_ids = user_role_ids
        self.temp_voice_enabled = temp_voice_enabled
        self.steam_free_games_enabled = steam_free_games_enabled
        self.codex_rate_limits = CodexRateLimits()
        self._panel_registry = panel_registry
        self._panel_session = panel_session
        self._rate_refresh_task: asyncio.Task[None] | None = None
        self._rate_interaction: discord.Interaction | None = None
        self._rate_cutoff_at: float | None = None
        self._retirement_task: asyncio.Task[None] | None = None
        self._rate_display_item: discord.ui.TextDisplay | None = None
        self._operation = 0
        self._edit_lock = asyncio.Lock()
        self._control_cache: dict[tuple[object, ...], discord.ui.Item] = {}
        self._desired_children: tuple[discord.ui.Item, ...] | None = None
        self._published_children: tuple[discord.ui.Item, ...] | None = None
        self._render_overview()

    async def open_panel(self, interaction: discord.Interaction) -> None:
        published = False
        opened = False
        try:
            await interaction.response.defer(ephemeral=True)
            if not self._registry_current():
                return
            self._panel_registry.attach_view(self._panel_session, self)
            self.bind_interaction(interaction)
            if not await self._refresh_ai_data():
                return
            self._render_overview()
            async with self._edit_lock:
                if not await self._can_publish():
                    return
                published = True
                await interaction.edit_original_response(
                    attachments=brand_files(CARD_FILENAME),
                    view=self,
                )
                self.record_published()
            if not await self._can_publish():
                return
            self.start_rate_refresh(interaction)
            opened = True
        finally:
            if not opened:
                self._remove_registry()
                if published:
                    self.close_stale_message()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        allowed = (
            self._registry_current()
            and (self._rate_interaction is None or self._before_cutoff())
            and interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
            and interaction.permissions.administrator
        )
        if allowed:
            self.user_role_ids = member_role_ids(interaction.user)
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "只有開啟控制台的伺服器管理員可以操作這個控制台。",
                ephemeral=True,
            )
        return False

    def _reuse_control(self, item: discord.ui.Item) -> discord.ui.Item:
        if isinstance(item, _PanelButton):
            key = ("button", item.action)
            cached = self._control_cache.get(key)
            if cached is None:
                self._control_cache[key] = item
                return item
            cached.label = item.label
            cached.style = item.style
            cached.disabled = item.disabled
            return cached

        if isinstance(item, _PanelSelect):
            key = ("select", item.pages)
            cached = self._control_cache.get(key)
            if cached is None:
                self._control_cache[key] = item
                return item
            cached.placeholder = item.placeholder
            cached.options = list(item.options)
            cached.min_values = item.min_values
            cached.max_values = item.max_values
            cached.disabled = item.disabled
            return cached

        if isinstance(item, _CodexChannelSelect):
            key = ("select", type(item))
            cached = self._control_cache.get(key)
            if cached is None:
                self._control_cache[key] = item
                return item
            cached.placeholder = item.placeholder
            cached.min_values = item.min_values
            cached.max_values = item.max_values
            cached.channel_types = list(item.channel_types)
            cached.default_values = list(item.default_values)
            cached.disabled = item.disabled
            return cached

        if isinstance(item, (_CodexRoleSelect, _SteamRoleSelect)):
            key = ("select", type(item))
            cached = self._control_cache.get(key)
            if cached is None:
                self._control_cache[key] = item
                return item
            cached.placeholder = item.placeholder
            cached.min_values = item.min_values
            cached.max_values = item.max_values
            cached.default_values = list(item.default_values)
            cached.disabled = item.disabled
            return cached

        return item

    def _set_container(self, *children: discord.ui.Item) -> None:
        normalized = [
            discord.ui.ActionRow(
                *(self._reuse_control(item) for item in child.children)
            )
            if isinstance(child, discord.ui.ActionRow) else child
            for child in children
        ]
        self.clear_items()
        self.add_item(
            discord.ui.Container(*normalized, accent_colour=BRAND_COLOUR)
        )
        self._desired_children = tuple(self.children)

    def record_published(self) -> None:
        self._published_children = tuple(self.children)

    @staticmethod
    def _main_select(current: str) -> discord.ui.ActionRow:
        selected = current if current in {"overview", "ai", "modules"} else None
        placeholder = (
            "返回 AI 助手" if current in {"ai_access", "ai_tech"}
            else "返回功能模組" if current in {"voice", "steam"}
            else "選擇頁面"
        )
        return discord.ui.ActionRow(
            _PanelSelect(MAIN_PAGES, placeholder=placeholder, current=selected)
        )

    @staticmethod
    def _ai_select(current: str) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            _PanelSelect(
                AI_PAGES, placeholder="選擇 AI 頁面", current=current,
            )
        )

    @staticmethod
    def _module_select(current: str | None) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            _PanelSelect(
                MODULE_PAGES, placeholder="選擇模組", current=current,
            )
        )

    @staticmethod
    def _close_button() -> _PanelButton:
        return _PanelButton("close", "關閉", style=discord.ButtonStyle.secondary)

    def _header(self, title: str, page: str, section: str | None = None) -> list[discord.ui.Item]:
        self.page = page
        self._rate_display_item = None
        children: list[discord.ui.Item] = [branded_title(title, ""), self._main_select(page)]
        if section == "ai":
            children.append(self._ai_select(page))
        elif section == "modules":
            children.append(self._module_select(page if page != "modules" else None))
        children.append(discord.ui.Separator())
        return children

    @staticmethod
    def _detail(title: str, message: str, *, escape: bool = False) -> discord.ui.TextDisplay:
        return discord.ui.TextDisplay(
            f"## {title}\n-# {discord.utils.escape_markdown(message) if escape else message}"
        )

    def _actions(self, *buttons: _PanelButton, refresh: bool = False) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            *([_PanelButton("refresh", "重新整理")] if refresh else []),
            *buttons,
            self._close_button(),
        )

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

    def _rate_visible(self) -> bool:
        return self.codex_access.enabled and self.codex_access.guild_id == self.guild_id

    @staticmethod
    def _rate_period_label(minutes: int | None, slot: str) -> str:
        if minutes == 300:
            return "5 小時"
        if minutes == 10080:
            return "每週"
        if minutes == 43200:
            return "30 天"
        if minutes is None:
            return f"週期未提供（{slot}）"
        return f"{minutes} 分鐘"

    def _rate_text(self, *, stopped: bool = False, compact: bool = False) -> str:
        lines = ["## 帳號額度" if compact else "## Codex 帳號額度"]
        limits = self.codex_rate_limits
        if limits.fetched_at is None:
            lines.extend(("**暫時無法取得**", "-# 上游未提供可驗證的額度快照"))
        else:
            durations = set()
            for window in limits.windows:
                durations.add(window.window_minutes)
                used = window.used_percent
                remaining = max(0, 100 - used)
                label = self._rate_period_label(window.window_minutes, window.slot)
                heading = label if window.window_minutes is None else f"{label}額度"
                if compact:
                    lines.append(
                        f"**{label} · 剩餘 {remaining:g}%**"
                    )
                else:
                    lines.append(
                        f"**{heading}**　剩餘 {remaining:g}% · "
                        f"已用 {used:g}%"
                    )
                if window.resets_at is None:
                    lines.append("-# 重設時間未提供")
                elif window.resets_at <= time.time():
                    lines.append(
                        f"-# 重設時間 <t:{window.resets_at}:f> · 等待上游更新"
                    )
                else:
                    lines.append(f"-# 重設時間 <t:{window.resets_at}:f>")
            if not compact:
                missing = []
                if 300 not in durations:
                    missing.append("5 小時")
                if 10080 not in durations:
                    missing.append("每週")
                if missing:
                    lines.append(f"-# {'／'.join(missing)}額度：上游未提供")
            lines.append(
                f"-# {'額度快照' if compact else '資料取得'} "
                f"<t:{limits.fetched_at}:T>"
            )
        if stopped:
            lines.append("-# 自動更新已停止，請重新開啟 /控制台")
        elif self._rate_cutoff_at is not None and not compact:
            lines.append(
                f"-# 每 30 秒嘗試更新 · 自動更新至 <t:{int(self._rate_cutoff_at)}:T>"
            )
        return "\n".join(lines)

    def _rate_item(self, *, compact: bool = False) -> discord.ui.TextDisplay:
        self._rate_display_item = discord.ui.TextDisplay(self._rate_text(compact=compact))
        return self._rate_display_item

    async def _refresh_ai_data(self, operation: int | None = None) -> bool:
        if self._rate_interaction is not None and not await self._can_publish():
            return False
        async with asyncio.TaskGroup() as group:
            status = group.create_task(load_codex_status(self.codex_client))
            self._panel_registry.track(self._panel_session, status)
            limits = None
            if self._rate_visible():
                limits = group.create_task(load_codex_rate_limits(self.codex_client))
                self._panel_registry.track(self._panel_session, limits)
        if operation is not None and not self._is_current(operation):
            return False
        if self._rate_interaction is not None and not await self._can_publish():
            return False
        self.codex_status = status.result()
        if limits is not None:
            self.codex_rate_limits = limits.result()
        return True

    def _registry_current(self) -> bool:
        return self._panel_registry.is_current(self._panel_session)

    def _remove_registry(self) -> None:
        self._panel_registry.retire(self._panel_session)

    def retire_session(self) -> None:
        self.stop()
        if self._retirement_task is None:
            self.close_stale_message()
        task = self._rate_refresh_task
        if task is not None and task is not asyncio.current_task() and not task.done() and not task.cancelling():
            task.cancel()

    def bind_interaction(self, interaction: discord.Interaction) -> None:
        if self._rate_interaction is not None:
            return
        self._rate_interaction = interaction
        expires_at = getattr(interaction, "expires_at", None)
        if isinstance(expires_at, datetime):
            self._rate_cutoff_at = expires_at.timestamp() - RATE_EXPIRY_MARGIN_SECONDS

    def _before_cutoff(self) -> bool:
        return self._rate_cutoff_at is not None and time.time() < self._rate_cutoff_at

    async def _can_publish(self) -> bool:
        return (
            self._registry_current() and self._before_cutoff()
            and await self._current_admin()
            and self._registry_current() and self._before_cutoff()
        )

    def close_stale_message(self) -> None:
        if self._retirement_task is not None and not self._retirement_task.done():
            return
        self._retirement_task = asyncio.create_task(self._close_stale_message())
        self._panel_registry.track(self._panel_session, self._retirement_task)

    async def _close_stale_message(self) -> None:
        try:
            async with asyncio.timeout(3):
                async with self._edit_lock:
                    self._render_closed()
                    if self._rate_interaction is not None:
                        await self._rate_interaction.edit_original_response(
                            view=self
                        )
        except Exception:
            logging.error("Admin panel retirement notification failed.")

    async def _current_admin(self) -> bool:
        interaction = self._rate_interaction
        if interaction is None or not self._registry_current():
            return False
        guild = getattr(interaction, "guild", None)
        if getattr(guild, "id", None) != self.guild_id:
            return False
        member = guild.get_member(self.user_id) if hasattr(guild, "get_member") else None
        if member is None and hasattr(guild, "fetch_member"):
            try:
                async with asyncio.timeout(3):
                    member = await guild.fetch_member(self.user_id)
            except Exception:
                return False
        permissions = getattr(member, "guild_permissions", None)
        return self._registry_current() and bool(getattr(permissions, "administrator", False))

    def start_rate_refresh(self, interaction: discord.Interaction) -> None:
        if not self._registry_current() or not self._rate_visible() or self._rate_refresh_task is not None:
            return
        if getattr(getattr(interaction, "user", None), "id", None) != self.user_id:
            return
        if getattr(getattr(interaction, "guild", None), "id", None) != self.guild_id:
            return
        self.bind_interaction(interaction)
        if not self._before_cutoff():
            return
        self._rate_refresh_task = asyncio.create_task(self._rate_refresh_loop())
        self._panel_registry.track(self._panel_session, self._rate_refresh_task)

    async def _publish_rate_stop_notice(self) -> None:
        item = self._rate_display_item
        interaction = self._rate_interaction
        if item is None or interaction is None or not self._registry_current():
            return
        async with self._edit_lock:
            if (
                item is not self._rate_display_item or self.page not in RATE_PAGES
                or not await self._current_admin()
            ):
                return
            item.content = self._rate_text(
                stopped=True, compact=self.page != "ai_tech",
            )
            try:
                await interaction.edit_original_response(
                    view=self
                )
                self.record_published()
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                return
            finally:
                if not self._registry_current():
                    self.close_stale_message()

    async def _rate_refresh_loop(self) -> None:
        current = asyncio.current_task()
        try:
            while True:
                if not await self._current_admin():
                    return
                cutoff = self._rate_cutoff_at
                if cutoff is None:
                    return
                remaining = cutoff - time.time()
                if remaining <= 0:
                    if await self._current_admin():
                        await self._publish_rate_stop_notice()
                    return
                await asyncio.sleep(min(RATE_REFRESH_SECONDS, remaining))
                if not await self._current_admin():
                    return
                if cutoff - time.time() <= 0:
                    continue
                if self.page not in RATE_PAGES:
                    continue
                if not await self._current_admin():
                    return
                operation = self._operation
                page = self.page
                item = self._rate_display_item
                result = await load_codex_rate_limits(self.codex_client)
                cutoff = self._rate_cutoff_at
                if cutoff is None or cutoff - time.time() <= 0:
                    if await self._current_admin():
                        await self._publish_rate_stop_notice()
                    return
                if not await self._current_admin():
                    return
                interaction = self._rate_interaction
                if interaction is None:
                    return
                async with self._edit_lock:
                    if not await self._can_publish():
                        continue
                    if (
                        operation != self._operation
                        or page != self.page
                        or item is None
                        or item is not self._rate_display_item
                        or not self._registry_current()
                    ):
                        continue
                    self.codex_rate_limits = result
                    item.content = self._rate_text(compact=self.page != "ai_tech")
                    try:
                        await interaction.edit_original_response(
                            view=self
                        )
                        self.record_published()
                    except (discord.Forbidden, discord.NotFound):
                        return
                    except (discord.HTTPException, aiohttp.ClientError, TimeoutError):
                        continue
                    finally:
                        if not self._registry_current():
                            self.close_stale_message()
        except asyncio.CancelledError:
            raise
        finally:
            if self._rate_refresh_task is current:
                self._rate_refresh_task = None
                self._remove_registry()
                self.stop()

    async def stop_rate_refresh(self) -> None:
        task = self._rate_refresh_task
        self._rate_refresh_task = None
        self._remove_registry()
        self.stop()
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def on_timeout(self) -> None:
        await self.stop_rate_refresh()

    def _voice_entry_problem(
        self, status: TempVoiceGuildStatus, guild: discord.Guild | None = None,
    ) -> str | None:
        if not self.temp_voice_enabled:
            return None
        if guild is None:
            interaction = self._rate_interaction
            guild = interaction.guild if interaction is not None else None
        if guild is None:
            return None
        if guild.id != self.guild_id:
            return None

        if not status.state_available or status.parent_channel_id is None:
            return None
        return self.temp_voice.entry_problem(guild)

    def _codex_stale_allowlist_counts(self) -> tuple[int | None, int | None]:
        access = self.codex_access
        if (
            not access.enabled
            or access.guild_id != self.guild_id
            or not access.state_available
        ):
            return None, None
        interaction = self._rate_interaction
        guild = getattr(interaction, "guild", None) if interaction is not None else None
        if (
            getattr(guild, "id", None) != self.guild_id
            or getattr(guild, "unavailable", False)
        ):
            return None, None

        def stale_count(ids: frozenset[int], getter_name: str) -> int | None:
            if not ids:
                return 0
            getter = getattr(guild, getter_name, None)
            if not callable(getter):
                return None
            return sum(getter(entity_id) is None for entity_id in ids)

        return (
            stale_count(access.channel_ids, "get_channel"),
            stale_count(access.role_ids, "get_role"),
        )

    @staticmethod
    def _codex_stale_detail(
        counts: tuple[int | None, int | None],
    ) -> str | None:
        stale_channels, stale_roles = counts
        details = []
        if stale_channels:
            details.append(f"{stale_channels} 個白名單頻道已不存在")
        if stale_roles:
            details.append(f"{stale_roles} 個白名單身分組已不存在")
        return f"{'；'.join(details)}，請重新選擇。" if details else None

    def _codex_channel_permission_detail(self) -> str | None:
        access = self.codex_access
        if (
            not access.enabled
            or access.guild_id != self.guild_id
            or not access.state_available
            or not access.channel_ids
        ):
            return None
        interaction = self._rate_interaction
        guild = getattr(interaction, "guild", None) if interaction is not None else None
        if (
            getattr(guild, "id", None) != self.guild_id
            or getattr(guild, "unavailable", False)
        ):
            return None
        get_channel = getattr(guild, "get_channel", None)
        bot_member = getattr(guild, "me", None)
        if not callable(get_channel) or bot_member is None:
            return None

        unusable = 0
        for channel_id in access.channel_ids:
            channel = get_channel(channel_id)
            if not is_text_channel(channel):
                unusable += 1
                continue
            permissions_for = getattr(channel, "permissions_for", None)
            if not callable(permissions_for):
                return None
            if missing_channel_permissions(
                channel, bot_member,
                (("view_channel", "View Channel"), ("send_messages", "Send Messages")),
            ):
                unusable += 1
        if unusable:
            return (
                f"{unusable} 個 AI 白名單頻道不存在、類型不支援或缺少 Bot 權限，"
                "請重新選擇頻道或調整權限。"
            )
        return None

    def _ai_state(self) -> _AiState:
        access = self.codex_access
        configured_guild = access.enabled and access.guild_id == self.guild_id
        stale_counts = self._codex_stale_allowlist_counts()
        permission_detail = self._codex_channel_permission_detail()
        allowlist_detail = self._codex_stale_detail(stale_counts) or permission_detail
        if not access.enabled:
            label, detail = "停用", "AI 對話目前依設定停用"
        elif not configured_guild:
            label, detail = "停用", "此伺服器不在 AI 白名單"
        elif not access.state_available:
            label, detail = "需要處理", "白名單狀態檔不可用"
        elif not access.channel_ids:
            label, detail = "需要處理", "白名單頻道尚未設定"
        elif not access.configured:
            label, detail = "需要處理", "白名單身分組尚未設定"
        elif allowlist_detail is not None:
            label, detail = "需要處理", allowlist_detail
        elif not self.codex_status.available:
            label, detail = "需要處理", "Codex bridge 無法連線"
        elif not self.codex_status.authenticated:
            label, detail = "需要處理", "Codex 尚未登入"
        else:
            label, detail = "狀態正常", ""
        return _AiState(label, detail, allowlist_detail, stale_counts, permission_detail)

    @staticmethod
    def _codex_summary(state: _AiState) -> str | None:
        detail = state.allowlist_detail
        if detail is None:
            return None
        return f"## AI 助手\n**需要處理**\n-# {detail}"

    def _voice_state(self, guild: discord.Guild | None = None) -> _VoiceState:
        status = self.temp_voice.get_guild_status(self.guild_id)
        problem = self._voice_entry_problem(status, guild)
        if not self.temp_voice_enabled:
            label, detail = "停用", "依設定停用"
            entry, next_step = "不會建立、同步或管理語音頻道", None
        elif not status.state_available:
            label, detail = "需要處理", "狀態檔不可用，臨時語音功能已停用"
            entry = "入口無法讀取"
            next_step = "請檢查並修復私有狀態檔後重新啟動 Bot；目前無法同步。"
        elif status.parent_channel_id is None:
            label, detail = "需要處理", "入口頻道尚未綁定"
            entry = "尚未綁定入口"
            next_step = "請確認 Bot 有管理頻道權限，再按「重新同步」尋找或建立入口。"
        elif problem is not None:
            label, detail = "需要處理", problem
            entry, next_step = "入口不可用", problem
        else:
            label, detail = "狀態正常", ""
            entry, next_step = f"入口 <#{status.parent_channel_id}>", None
        return _VoiceState(status, problem, label, detail, entry, next_step)

    @staticmethod
    def _voice_summary(state: _VoiceState) -> str:
        status = state.status
        if state.label == "停用":
            return "## 臨時語音\n**依設定停用**\n-# 不會建立、同步或管理語音頻道"
        if not status.state_available:
            return "## 臨時語音\n**需要處理**\n-# 入口無法讀取 · 追蹤暫停"
        detail = f"-# {state.entry} · {state.label}"
        if state.problem is not None:
            detail += f" · {state.problem}"
        return (
            "## 臨時語音\n"
            f"**{status.tracked_child_count} 個頻道追蹤中**\n"
            f"{detail}"
        )

    def _steam_notification_problem(self, status: SteamGuildStatus) -> str | None:
        if not self.steam_free_games_enabled:
            return None
        interaction = self._rate_interaction
        guild = getattr(interaction, "guild", None) if interaction is not None else None
        if (
            getattr(guild, "id", None) != self.guild_id
            or getattr(guild, "unavailable", False)
        ):
            return None
        if not status.state_available or status.channel_id is None:
            return None
        return self.steam_free_games.notification_problem(guild)

    def _steam_state(self) -> _SteamState:
        status = self.steam_free_games.get_guild_status(self.guild_id)
        problem = self._steam_notification_problem(status)
        if not self.steam_free_games_enabled:
            label, detail = "停用", "自動通知依設定停用"
            channel, role_status, next_step = "手動查詢仍可使用", "身分組通知依設定停用", None
        elif not status.state_available:
            label, detail = "需要處理", "狀態檔不可用，通知功能已停用"
            channel, role_status = "通知頻道無法讀取", "身分組設定不可用"
            next_step = "請檢查並修復私有狀態檔後重新啟動 Bot；目前無法修改通知設定。"
        else:
            label = "需要處理" if status.channel_id is None or problem else "狀態正常"
            detail = "通知頻道尚未綁定" if status.channel_id is None else problem or ""
            channel = f"通知 <#{status.channel_id}>" if status.channel_id else "尚未綁定通知頻道"
            role_status = (
                "通知身分組 " + " ".join(f"<@&{role_id}>" for role_id in status.role_ids)
                if status.role_ids else "未設定通知身分組"
            )
            next_step = problem or (
                "請確認 Bot 有管理頻道、檢視與傳送訊息權限；既有流程會尋找或建立通知頻道。"
                if status.channel_id is None else None
            )
        return _SteamState(status, problem, label, detail, channel, role_status, next_step)

    @staticmethod
    def _steam_summary(state: _SteamState) -> str:
        status = state.status
        if state.label == "停用":
            return "## Steam 免費遊戲\n**自動通知依設定停用**\n-# 手動查詢仍可使用"
        if not status.state_available:
            return "## Steam 免費遊戲\n**需要處理**\n-# 通知頻道無法讀取"
        detail = f"-# {state.channel} · 每 {int(status.poll_interval_seconds // 60)} 分鐘檢查 · {state.label}"
        if state.problem is not None:
            detail += f" · {state.problem}"
        return (
            "## Steam 免費遊戲\n"
            f"**{status.active_app_count} 款活動中**\n"
            f"{detail}"
        )

    def _render_overview(self) -> None:
        voice = self._voice_state()
        steam = self._steam_state()
        ai = self._ai_state()
        voice_status, voice_problem = voice.status, voice.problem
        steam_status, steam_problem = steam.status, steam.problem
        ai_allowlist_detail = ai.allowlist_detail

        statuses = [
            ("AI 助手", ai.label, ai.detail),
            ("臨時語音", voice.label, voice.detail),
            ("Steam 免費遊戲", steam.label, steam.detail),
        ]
        ai_access_enabled = (
            self.codex_access.enabled
            and self.codex_access.guild_id == self.guild_id
        )

        setup_items: list[tuple[str, bool]] = []
        if ai_access_enabled:
            setup_items.append(
                (
                    "AI 白名單",
                    self.codex_access.configured and ai_allowlist_detail is None,
                )
            )
        if self.temp_voice_enabled:
            setup_items.append(
                (
                    "臨時語音入口",
                    voice_status.state_available
                    and voice_status.parent_channel_id is not None
                    and voice_problem is None,
                )
            )
        if self.steam_free_games_enabled:
            setup_items.append(
                (
                    "Steam 通知頻道",
                    steam_status.state_available and steam_status.channel_id is not None,
                )
            )
        missing_setup = [name for name, configured in setup_items if not configured]
        issues = [item for item in statuses if item[1] == "需要處理"]
        if issues:
            status_lines = ["## ⚠ 需要處理"]
            for name, status, detail in issues:
                status_lines.extend((f"**{name} · {status}**", f"-# {detail}"))
            if missing_setup:
                status_lines.append(f"-# 尚未完成：{' · '.join(missing_setup)}")
        else:
            setup_status = "無需設定" if not setup_items else "設定已完成"
            disabled = [
                f"{name}：{detail}"
                for name, status, detail in statuses if status == "停用"
            ]
            suffix = f" · {'；'.join(disabled)}" if disabled else ""
            status_lines = [
                "## ✓ 狀態正常",
                f"-# {setup_status}{suffix}",
            ]

        shortcuts: list[_PanelButton] = []
        if ai_access_enabled and (
            not self.codex_access.state_available
            or not self.codex_access.channel_ids
            or not self.codex_access.configured
            or ai_allowlist_detail is not None
        ):
            shortcuts.append(
                _PanelButton(
                    "ai_access", "設定 AI 白名單", style=discord.ButtonStyle.primary,
                )
            )
        elif ai_access_enabled and (
            not self.codex_status.available or not self.codex_status.authenticated
        ):
            shortcuts.append(
                _PanelButton(
                    "ai", "查看 AI 狀態", style=discord.ButtonStyle.primary,
                )
            )
        if self.temp_voice_enabled and (
            not voice_status.state_available
            or voice_status.parent_channel_id is None
            or voice_problem is not None
        ):
            shortcuts.append(
                _PanelButton(
                    "voice", "設定臨時語音", style=discord.ButtonStyle.primary,
                )
            )
        if self.steam_free_games_enabled and (
            not steam_status.state_available
            or steam_status.channel_id is None
            or steam_problem is not None
        ):
            shortcuts.append(
                _PanelButton(
                    "steam", "設定 Steam 通知", style=discord.ButtonStyle.primary,
                )
            )

        children = self._header("管理控制台", "overview")
        children.append(discord.ui.TextDisplay("\n".join(status_lines)))
        if self._rate_visible():
            children.append(self._rate_item(compact=True))
        if shortcuts:
            children.extend((
                self._detail("下一步", "選擇下方入口前往設定或檢查頁面。"),
                discord.ui.ActionRow(*shortcuts),
            ))
        children.append(self._actions(refresh=True))
        self._set_container(*children)

    def _render_ai(self, note: str | None = None) -> None:
        ai = self._ai_state()
        plan = self._ai_display()[0]
        authenticated = "已登入" if self.codex_status.authenticated else "未登入"
        children = self._header("AI 助手", "ai", "ai")
        if self.codex_status.last_error:
            children.append(discord.ui.TextDisplay(
                "## ⚠ 最近錯誤\n"
                f"{discord.utils.escape_markdown(self.codex_status.last_error)}"
            ))
        children.append(discord.ui.TextDisplay(
            f"**{plan} · {authenticated} · "
            f"{ai.label}**"
        ))
        if self._rate_visible():
            children.append(self._rate_item(compact=True))
        if not self.codex_status.available:
            children.append(self._detail("下一步", "請確認 AI 服務正在執行且連線設定正確，再重新整理。"))
        elif not self.codex_status.authenticated:
            children.append(self._detail("下一步", "請完成 AI 帳號登入，再重新整理。"))
        if note:
            children.append(self._detail("最近操作", note, escape=True))
        children.append(self._actions(refresh=True))
        self._set_container(*children)

    def _render_ai_access(self, note: str | None = None) -> None:
        ai = self._ai_state()
        configured_guild = self.codex_access.guild_id == self.guild_id
        stale_channel_count, stale_role_count = ai.stale_counts
        permission_detail = ai.permission_detail
        channel_ids = self.codex_access.channel_ids if self.codex_access.state_available else frozenset()
        role_ids = self.codex_access.role_ids if self.codex_access.state_available else frozenset()
        if not configured_guild:
            channel_detail = "此伺服器不在 AI 白名單"
        elif not self.codex_access.enabled:
            channel_detail = "AI 對話目前依設定停用"
        elif not self.codex_access.state_available:
            channel_detail = "狀態檔不可用；重新選擇可修復"
        elif not channel_ids:
            channel_detail = "請選擇可使用 AI 的文字頻道"
        else:
            channel_detail = "僅所選頻道及其 Thread 可使用 AI"
        role_detail = (
            "擁有任一所選身分組即可使用"
            if role_ids else "請選擇白名單身分組；目前尚未開放"
        )
        if stale_channel_count:
            channel_detail += (
                f"；{stale_channel_count} 個白名單頻道已不存在，請重新選擇"
            )
        if permission_detail:
            channel_detail += f"；{permission_detail}"
        if stale_role_count:
            role_detail += (
                f"；{stale_role_count} 個白名單身分組已不存在，請重新選擇"
            )
        current_allowed = self.codex_access.allows(
            self.guild_id, next(iter(channel_ids), None), self.user_role_ids,
        )
        children = self._header("AI 使用權限", "ai_access", "ai")
        children.extend((
            discord.ui.TextDisplay(
                f"## 白名單頻道 · {len(channel_ids)} 個\n"
                f"-# {channel_detail}"
            ),
            discord.ui.ActionRow(
                _CodexChannelSelect(
                    disabled=not self.codex_access.enabled or not configured_guild,
                    channel_ids=channel_ids,
                )
            ),
            discord.ui.TextDisplay(
                f"## 白名單身分組 · {len(role_ids)} 個\n"
                f"-# {role_detail} · 目前操作者："
                f"{'已允許' if current_allowed else '未允許'}"
            ),
            discord.ui.ActionRow(
                _CodexRoleSelect(
                    disabled=not self.codex_access.enabled or not configured_guild
                    or not self.codex_access.state_available or not channel_ids,
                    role_ids=role_ids,
                )
            ),
            self._actions(),
        ))
        if note:
            children.insert(-1, self._detail("最近操作", note, escape=True))
        self._set_container(*children)

    def _render_ai_tech(self) -> None:
        plan, runtime, search = self._ai_display()
        sdk = self.codex_status.sdk_version or "Unknown"
        children = self._header("AI 技術資訊", "ai_tech", "ai")
        children.append(discord.ui.TextDisplay(
                "## 執行環境\n"
                f"**{plan} · Runtime {runtime} · SDK {sdk}**\n"
                f"-# Web Search {search}\n"
                "## 工作與對話\n"
                f"**持久 Thread**　{self.codex_status.thread_count} 條\n"
                f"**Bot**　執行 {self.codex_status.bot_active_requests} · 等待 {self.codex_status.bot_queued_requests}\n"
                f"**Bridge**　執行 {self.codex_status.active_requests} · 等待 {self.codex_status.queued_requests}"
        ))
        if self._rate_visible():
            children.append(self._rate_item())
        children.extend((
            discord.ui.TextDisplay(
                "## 安全邊界\n**Read-only · Deny-all**\n"
                "-# Shell、MCP、Apps、Subagents 與全域 Memories 均停用"
            ),
            self._actions(refresh=True),
        ))
        self._set_container(*children)

    def _render_modules(self) -> None:
        codex_summary = self._codex_summary(self._ai_state())
        voice = self._voice_state()
        steam = self._steam_state()
        children = self._header("功能模組", "modules", "modules")
        if codex_summary is not None:
            children.append(discord.ui.TextDisplay(codex_summary))
        children.extend(
            (
                discord.ui.TextDisplay(self._voice_summary(voice)),
                discord.ui.TextDisplay(self._steam_summary(steam)),
                self._actions(refresh=True),
            )
        )
        self._set_container(*children)

    def _render_voice(self, note: str | None = None, *, voice: _VoiceState | None = None) -> None:
        voice = voice or self._voice_state()
        status = voice.status
        children = self._header("臨時語音", "voice", "modules")
        children.append(discord.ui.TextDisplay(
                "## 目前狀態\n"
                f"**{status.tracked_child_count} 個臨時語音頻道**\n"
                f"-# {voice.entry} · {voice.label}"
        ))
        if voice.next_step:
            children.append(self._detail("下一步", voice.next_step))
        if note:
            children.append(self._detail("最近操作", note, escape=True))
        children.append(
            self._actions(
                _PanelButton(
                    "voice_sync",
                    "重新同步",
                    style=discord.ButtonStyle.primary,
                    disabled=not self.temp_voice_enabled or not status.state_available,
                ),
            )
        )
        self._set_container(*children)

    def _render_steam(
        self,
        result: SteamFetchResult | None = None,
        *,
        error: str | None = None,
        notice: str | None = None,
    ) -> None:
        if error is not None:
            self._steam_result = None
            self._steam_error = error
            self._steam_fetched_at = None
        elif result is not None:
            self._steam_result = result
            self._steam_error = None
            self._steam_fetched_at = int(time.time())

        result = self._steam_result
        error = self._steam_error
        steam = self._steam_state()
        status = steam.status
        role_controls_disabled = (
            not self.steam_free_games_enabled or not status.state_available
        )
        children = self._header("Steam 限時免費", "steam", "modules")
        children.append(discord.ui.TextDisplay(
                "## 通知狀態\n"
                f"**{status.active_app_count} 款活動中**\n"
                f"-# {steam.channel} · {steam.role_status} · 每 {int(status.poll_interval_seconds // 60)} 分鐘檢查 · {steam.label}"
        ))
        children.append(discord.ui.ActionRow(
                _SteamRoleSelect(
                    disabled=role_controls_disabled,
                    configured=bool(status.role_ids),
                )
        ))

        if steam.next_step:
            children.append(self._detail("下一步", steam.next_step))
        if notice:
            children.append(self._detail("最近操作", notice, escape=True))

        if error is not None:
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
            query_time = (
                f"\n-# 查詢時間 <t:{self._steam_fetched_at}:T>"
                if self._steam_fetched_at is not None else ""
            )
            if result.failed_app_count > 0:
                children.append(
                    discord.ui.TextDisplay(
                        "## 查詢結果"
                        f"{query_time}\n"
                        f"-# 資料不完整：{result.failed_app_count} 款遊戲詳細資料無法取得；"
                        "請稍後再查詢。"
                    )
                )
            elif not offers:
                children.append(
                    discord.ui.TextDisplay(
                        "## 查詢結果"
                        f"{query_time}\n-# 目前沒有符合條件的限時免費遊戲。"
                    )
                )
            else:
                children.append(
                    discord.ui.TextDisplay(f"## 查詢結果{query_time}")
                )
            if offers:
                children.extend(offer_items(offer, compact=True)[0] for offer in offers)
                if len(result.offers) > len(offers):
                    children.append(
                        discord.ui.TextDisplay(
                            f"-# 只顯示前 {len(offers)} 款，共 {len(result.offers)} 款。"
                        )
                    )

        children.append(
            self._actions(
                _PanelButton(
                    "steam_role_clear",
                    "取消身分組通知",
                    disabled=role_controls_disabled or not status.role_ids,
                ),
                _PanelButton("steam_query", "重新查詢", style=discord.ButtonStyle.primary),
            )
        )
        self._set_container(*children)

    def _render_closed(self) -> None:
        self.page = "closed"
        self._rate_display_item = None
        self._set_container(
            branded_title("管理控制台", ""),
            discord.ui.TextDisplay("-# 控制台已關閉；重新輸入 /控制台 可再開啟。"),
            discord.ui.ActionRow(_PanelButton("closed", "已關閉", disabled=True)),
        )
        self.stop()

    def _render_page(self, page: str) -> None:
        if page == "ai":
            self._render_ai()
        elif page == "ai_access":
            self._render_ai_access()
        elif page == "ai_tech":
            self._render_ai_tech()
        elif page == "modules":
            self._render_modules()
        elif page == "voice":
            self._render_voice()
        elif page == "steam":
            self._render_steam()
        else:
            self._render_overview()

    def _begin_operation(self) -> int:
        self._operation += 1
        return self._operation

    async def _defer_operation(self, interaction: discord.Interaction) -> int | None:
        operation = self._begin_operation()
        await interaction.response.defer()
        return operation if self._is_current(operation) else None

    def _is_current(self, operation: int) -> bool:
        return (
            operation == self._operation and self._registry_current()
            and (self._rate_interaction is None or self._before_cutoff())
        )

    async def _edit_original(
        self,
        interaction: discord.Interaction,
        operation: int,
    ) -> None:
        await self._edit_owned(interaction, operation, original=True)

    async def _edit_owned(
        self, interaction: discord.Interaction, operation: int, *, original: bool,
    ) -> None:
        if not self._is_current(operation):
            return
        publication = asyncio.create_task(self._publish_edit(interaction, operation, original=original))
        self._panel_registry.track(self._panel_session, publication)
        try:
            await publication
        except asyncio.CancelledError:
            # A direct callback cancellation still propagates, even after retirement.
            if (
                asyncio.current_task().cancelling()
                or not self._panel_session.retired
            ):
                raise

    def _layout_is_published(self) -> bool:
        return (
            self._desired_children is not None
            and self._desired_children == self._published_children
        )

    async def _publish_edit(
        self, interaction: discord.Interaction, operation: int, *, original: bool,
    ) -> None:
        async with self._edit_lock:
            if not self._is_current(operation):
                return
            if self._rate_interaction is not None and not await self._can_publish():
                return
            if not self._is_current(operation):
                return
            desired = self._desired_children
            sent_children = tuple(self.children)
            try:
                edit = interaction.edit_original_response if original else interaction.response.edit_message
                await edit(
                    view=self,
                )
            except (Exception, asyncio.CancelledError):
                restore = self._published_children
                if self._desired_children is not desired and self._desired_children is not None:
                    restore = self._desired_children
                if restore is None:
                    restore = sent_children
                self.clear_items()
                for child in restore:
                    self.add_item(child)
                raise
            else:
                self._published_children = sent_children
            finally:
                if not self._registry_current():
                    self.close_stale_message()

    async def handle_codex_channel_select(
        self,
        interaction: discord.Interaction,
        channels: tuple[object | None, ...],
    ) -> None:
        operation = await self._defer_operation(interaction)
        if operation is None:
            return
        guild_id = getattr(getattr(interaction, "guild", None), "id", None)
        channel_ids = [getattr(channel, "id", None) for channel in channels]
        if (
            not self.codex_access.enabled
            or guild_id != self.guild_id
            or guild_id != self.codex_access.guild_id
            or not valid_allowlist_ids(channel_ids, container=list, minimum=1)
            or any(
                getattr(getattr(channel, "guild", None), "id", None) != guild_id
                or not is_text_channel(channel)
                for channel in channels
            )
        ):
            self._render_ai_access("只能選擇目前伺服器的一般文字頻道。")
        else:
            selected = frozenset(channel_ids)
            previous = self.codex_access.channel_ids
            result = await self.access_service.change_channels(
                guild_id, selected, still_current=lambda: self._is_current(operation),
            )
            if not self._is_current(operation):
                return
            count = len(selected)
            if result == "persist_failed":
                logging.error("管理控制台保存 Codex 白名單頻道失敗。")
                note = "白名單頻道無法保存，設定未變更。"
            elif result == "unchanged":
                note = f"目前已設定 {count} 個白名單頻道。"
            elif result == "updated_archive_failed":
                logging.error("管理控制台封存舊 Codex 對話失敗。")
                note = f"已更新 {count} 個白名單頻道，但舊對話封存失敗。"
            elif previous - selected:
                note = f"已更新 {count} 個白名單頻道並封存舊對話。"
            else:
                note = f"已更新 {count} 個白名單頻道。"
            self._render_ai_access(note)
        await self._edit_original(interaction, operation)

    async def handle_codex_role_select(
        self,
        interaction: discord.Interaction,
        roles: tuple[discord.Role, ...],
    ) -> None:
        operation = await self._defer_operation(interaction)
        if operation is None:
            return
        guild_id = getattr(getattr(interaction, "guild", None), "id", None)
        self.user_role_ids = member_role_ids(interaction.user)
        role_ids = [getattr(role, "id", None) for role in roles]
        if (
            not self.codex_access.enabled
            or guild_id != self.guild_id
            or guild_id != self.codex_access.guild_id
            or not self.codex_access.state_available
            or not self.codex_access.channel_ids
            or not valid_allowlist_ids(role_ids, container=list, minimum=1)
            or any(
                getattr(getattr(role, "guild", None), "id", None) != guild_id
                or role.is_default()
                for role in roles
            )
        ):
            self._render_ai_access("只能選擇目前伺服器的一般身分組。")
        else:
            selected = frozenset(role_ids)
            result = await self.access_service.change_roles(
                guild_id, selected, still_current=lambda: self._is_current(operation),
            )
            if not self._is_current(operation):
                return
            if result == "persist_failed":
                logging.error("管理控制台保存 Codex 白名單身分組失敗。")
                note = "白名單身分組無法保存，設定未變更。"
            elif result == "unchanged":
                note = f"目前已設定 {len(selected)} 個白名單身分組。"
            elif result == "archive_failed":
                logging.error("管理控制台切換白名單身分組前封存對話失敗。")
                note = "舊對話封存失敗，角色設定未變更。"
            elif result == "archived_persist_failed":
                logging.error("管理控制台保存 Codex 白名單身分組失敗。")
                note = "舊對話已封存，但角色設定無法保存。"
            else:
                note = f"已更新 {len(selected)} 個白名單身分組。"
            self._render_ai_access(note)
        await self._edit_original(interaction, operation)

    async def handle_steam_role_select(
        self,
        interaction: discord.Interaction,
        roles: tuple[discord.Role, ...],
    ) -> None:
        operation = await self._defer_operation(interaction)
        if operation is None:
            return
        if not self.steam_free_games_enabled:
            self._render_steam(notice="Steam 自動通知已停用，未修改身分組設定。")
        elif interaction.guild is None:
            self._render_steam(notice="無法取得目前伺服器。")
        else:
            try:
                await self.steam_free_games.set_notification_roles(interaction.guild, roles)
            except SteamConfigurationError as exc:
                if not self._is_current(operation):
                    return
                self._render_steam(notice=str(exc))
            else:
                if not self._is_current(operation):
                    return
                self._render_steam(
                    notice=f"已更新 Steam 免費遊戲通知身分組，共 {len(roles)} 個。"
                )
        await self._edit_original(interaction, operation)

    async def handle_action(self, interaction: discord.Interaction, action: str) -> None:
        if action == "noop":
            await interaction.response.defer()
            return
        if action not in PAGES | {"refresh", "close", "voice_sync", "steam_role_clear", "steam_query"}:
            return
        operation = await self._defer_operation(interaction)
        if operation is None:
            return
        if action in PAGES - {"ai"}:
            self._render_page(action)
            await self._edit_original(interaction, operation)
            return
        if action == "close":
            self._render_closed()
            await self._edit_original(interaction, operation)
            await self.stop_rate_refresh()
            return
        if action == "ai":
            if not await self._refresh_ai_data(operation):
                return
            self._render_ai()
        elif action == "refresh":
            if self.page in RATE_PAGES:
                if not await self._refresh_ai_data(operation):
                    return
            self._render_page(self.page)
        elif action == "voice_sync":
            voice = self._voice_state()
            if not self.temp_voice_enabled:
                self._render_voice("功能已停用，未執行同步。", voice=voice)
            elif not voice.status.state_available:
                self._render_voice("狀態檔不可用，未執行同步。", voice=voice)
            else:
                guild = interaction.guild
                if guild is None or guild.id != self.guild_id:
                    self._render_voice("無法取得目前伺服器。")
                else:
                    try:
                        await self.temp_voice.reconcile([guild], prune_absent=False)
                    except Exception:
                        if not self._is_current(operation):
                            return
                        logging.exception("管理控制台重新同步臨時語音失敗。")
                        self._render_voice("重新同步失敗，請查看 Bot 紀錄。")
                    else:
                        if not self._is_current(operation):
                            return
                        voice = self._voice_state(guild)
                        status, problem = voice.status, voice.problem
                        if (
                            not status.state_available
                            or status.parent_channel_id is None
                            or problem is not None
                        ):
                            detail = problem or (
                                "入口頻道尚未綁定，請稍後重試並確認 Bot 有管理頻道權限。"
                            )
                            self._render_voice(f"同步未完成；{detail}", voice=voice)
                        else:
                            self._render_voice("入口檢查通過；已執行同步流程。", voice=voice)
        elif action == "steam_role_clear":
            if not self.steam_free_games_enabled:
                self._render_steam(notice="Steam 自動通知已停用，未修改身分組設定。")
            else:
                try:
                    removed = await self.steam_free_games.clear_notification_roles(
                        self.guild_id
                    )
                except SteamConfigurationError as exc:
                    if not self._is_current(operation):
                        return
                    self._render_steam(notice=str(exc))
                else:
                    if not self._is_current(operation):
                        return
                    self._render_steam(
                        notice=(
                            "已取消 Steam 免費遊戲通知身分組。"
                            if removed
                            else "目前沒有設定 Steam 通知身分組。"
                        )
                    )
        else:
            try:
                result = await self.steam_free_games.fetch_current_offers()
            except Exception:
                if not self._is_current(operation):
                    return
                logging.exception("管理控制台查詢 Steam 免費遊戲失敗。")
                self._render_steam(error="目前無法取得 Steam 資料。")
            else:
                if not self._is_current(operation):
                    return
                if result is None:
                    self._render_steam(error="目前無法取得 Steam 資料。")
                else:
                    self._render_steam(result)
        await self._edit_original(interaction, operation)
