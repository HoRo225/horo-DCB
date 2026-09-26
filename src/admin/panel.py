from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

import aiohttp
import discord

from src.admin import presentation
from src.admin.components import _CalendarChannelSelect
from src.admin.components import (
    _CodexChannelSelect as _CodexChannelSelect,
)
from src.admin.components import (
    _CodexRoleSelect as _CodexRoleSelect,
)
from src.admin.components import (
    _PanelButton as _PanelButton,
)
from src.admin.components import (
    _PanelSelect as _PanelSelect,
)
from src.admin.components import (
    _SteamRoleSelect as _SteamRoleSelect,
)
from src.admin.presentation import (
    MAX_STEAM_OFFERS_SHOWN as MAX_STEAM_OFFERS_SHOWN,
)
from src.admin.presentation import (
    STATUS_DOTS as STATUS_DOTS,
)
from src.admin.presentation import (
    _AiState as _AiState,
)
from src.admin.presentation import (
    _SteamState as _SteamState,
)
from src.admin.presentation import (
    _VoiceState as _VoiceState,
)
from src.admin.sessions import PanelSession, PanelSessionRegistry
from src.ai.access import (
    CodexAccess,
    member_role_ids,
    valid_allowlist_ids,
)
from src.ai.access_service import AiAccessService
from src.ai.client import CodexBridgeClient
from src.ai.protocol import EMPTY_CODEX_RUNTIME_STATUS, CodexRateLimits, CodexRuntimeStatus
from src.brand import BRAND_COLOUR, CARD_FILENAME, brand_files, branded_title
from src.calendar.manager import CalendarManager
from src.calendar.models import CalendarBinding, CalendarUserError
from src.discord_utils import is_text_channel
from src.steam.notifier import (
    SteamConfigurationError,
    SteamFreeGamesNotifier,
)
from src.steam.provider import SteamFetchResult
from src.voice.manager import TempVoiceManager

IDLE_TIMEOUT_SECONDS = 120.0
RATE_REFRESH_SECONDS = 30.0
RATE_EXPIRY_MARGIN_SECONDS = 10.0
RATE_PAGES = frozenset({"overview", "ai", "ai_tech"})

MAIN_PAGES = (
    ("overview", "總覽", "控制台首頁"),
    ("ai", "AI 助手", "Codex OAuth 與對話"),
    ("modules", "功能模組", "臨時語音、Steam 與行事曆"),
)
AI_PAGES = (
    ("ai", "狀態", "帳號狀態與額度"),
    ("ai_access", "使用權限", "頻道與身分組白名單"),
    ("ai_tech", "技術資訊", "版本、工作與安全邊界"),
)
MODULE_PAGES = (
    ("voice", "臨時語音", "入口頻道與同步狀態"),
    ("steam", "Steam 免費遊戲", "通知設定與手動查詢"),
    ("calendar", "行事曆", "公開看板的位置與更新狀態"),
)
PAGES = frozenset(page for group in (MAIN_PAGES, AI_PAGES, MODULE_PAGES) for page, *_ in group)


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


class AdminPanelView(discord.ui.LayoutView):
    def __init__(
        self,
        *,
        user_id: int,
        guild_id: int,
        guild: discord.Guild,
        calendar: CalendarManager,
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
        super().__init__(timeout=None)
        self.user_id = user_id
        self.guild_id = guild_id
        self.guild = guild
        self.calendar = calendar
        self.pending_calendar_channel: discord.TextChannel | None = None
        self.calendar_unbind_target: CalendarBinding | None = None
        self.calendar_notice: str | None = None
        self._calendar_channel_control = _CalendarChannelSelect()
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
        self._idle_task: asyncio.Task[None] | None = None
        self._idle_deadline: float | None = None
        self._retirement_task: asyncio.Task[None] | None = None
        self._rate_display_item: discord.ui.TextDisplay | None = None
        self._operation = 0
        self._edit_lock = asyncio.Lock()
        self._control_cache: dict[tuple[object, ...], discord.ui.Item] = {}
        self._desired_children: tuple[discord.ui.Item, ...] | None = None
        self._published_children: tuple[discord.ui.Item, ...] | None = None
        presentation.render_overview(self)

    async def open_panel(self, interaction: discord.Interaction) -> None:
        published = False
        opened = False
        try:
            await interaction.response.defer(ephemeral=True)
            self.bind_interaction(interaction)
            self._panel_registry.attach_view(self._panel_session, self)
            if not self._registry_current():
                return
            if not await self._refresh_ai_data():
                return
            presentation.render_overview(self)
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
            if self._idle_deadline is None:
                self._idle_deadline = time.monotonic() + IDLE_TIMEOUT_SECONDS
            self._idle_task = asyncio.create_task(self._idle_loop())
            self._panel_registry.track(self._panel_session, self._idle_task)
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
            and self._before_idle_deadline()
            and (self._rate_interaction is None or self._before_cutoff())
            and interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
            and interaction.permissions.administrator
        )
        if allowed:
            self.user_role_ids = member_role_ids(interaction.user)
            self._idle_deadline = time.monotonic() + IDLE_TIMEOUT_SECONDS
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "只有開啟控制台的伺服器管理員可以操作這個控制台。",
                ephemeral=True,
            )
        return False

    def _button(
        self,
        action: str,
        label: str,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> _PanelButton:
        key = ("button", action)
        item = self._control_cache.get(key)
        if item is None:
            item = _PanelButton(action, label, style=style, disabled=disabled)
            self._control_cache[key] = item
        assert isinstance(item, _PanelButton)
        item.label, item.style, item.disabled = label, style, disabled
        return item

    def _page_select(
        self,
        pages: tuple[tuple[str, str, str], ...],
        *,
        placeholder: str,
        current: str | None,
    ) -> _PanelSelect:
        key = ("select", pages)
        item = self._control_cache.get(key)
        if item is None:
            item = _PanelSelect(pages, placeholder=placeholder, current=current)
            self._control_cache[key] = item
        assert isinstance(item, _PanelSelect)
        item.placeholder = placeholder
        for option in item.options:
            option.default = option.value == current
        return item

    def _codex_channel_select(
        self,
        *,
        disabled: bool,
        channel_ids: frozenset[int],
    ) -> _CodexChannelSelect:
        key = ("select", _CodexChannelSelect)
        item = self._control_cache.get(key)
        if item is None:
            item = _CodexChannelSelect(disabled=disabled, channel_ids=channel_ids)
            self._control_cache[key] = item
        assert isinstance(item, _CodexChannelSelect)
        item.disabled = disabled
        item.default_values = [
            discord.SelectDefaultValue.from_channel(discord.Object(id=channel_id))
            for channel_id in sorted(channel_ids)
        ]
        return item

    def _codex_role_select(
        self,
        *,
        disabled: bool,
        role_ids: frozenset[int],
    ) -> _CodexRoleSelect:
        key = ("select", _CodexRoleSelect)
        item = self._control_cache.get(key)
        if item is None:
            item = _CodexRoleSelect(disabled=disabled, role_ids=role_ids)
            self._control_cache[key] = item
        assert isinstance(item, _CodexRoleSelect)
        item.disabled = disabled
        item.default_values = [
            discord.SelectDefaultValue.from_role(discord.Object(id=role_id))
            for role_id in sorted(role_ids)
        ]
        return item

    def _steam_role_select(
        self,
        *,
        disabled: bool,
        configured: bool,
    ) -> _SteamRoleSelect:
        key = ("select", _SteamRoleSelect)
        item = self._control_cache.get(key)
        if item is None:
            item = _SteamRoleSelect(disabled=disabled, configured=configured)
            self._control_cache[key] = item
        assert isinstance(item, _SteamRoleSelect)
        item.disabled = disabled
        item.placeholder = "重新選擇 Steam 通知身分組" if configured else "選擇 Steam 通知身分組"
        item.default_values = []
        return item

    def _set_container(self, *children: discord.ui.Item) -> None:
        self.clear_items()
        self.add_item(discord.ui.Container(*children, accent_colour=BRAND_COLOUR))
        self._desired_children = tuple(self.children)

    def record_published(self) -> None:
        self._published_children = tuple(self.children)

    def _main_select(self, current: str) -> discord.ui.ActionRow:
        selected = current if current in {"overview", "ai", "modules"} else None
        placeholder = (
            "返回 AI 助手"
            if current in {"ai_access", "ai_tech"}
            else "返回功能模組"
            if current in {"voice", "steam", "calendar"}
            else "選擇頁面"
        )
        return discord.ui.ActionRow(
            self._page_select(MAIN_PAGES, placeholder=placeholder, current=selected)
        )

    def _ai_select(self, current: str) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            self._page_select(
                AI_PAGES,
                placeholder="選擇 AI 頁面",
                current=current,
            )
        )

    def _module_select(self, current: str | None) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            self._page_select(
                MODULE_PAGES,
                placeholder="選擇模組",
                current=current,
            )
        )

    def _close_button(self) -> _PanelButton:
        return self._button("close", "關閉")

    def _header(
        self,
        title: str,
        page: str,
        section: str | None = None,
        subtitle: str = "",
    ) -> list[discord.ui.Item]:
        if page != "calendar":
            self.calendar_unbind_target = None
        self.page = page
        self._rate_display_item = None
        children: list[discord.ui.Item] = [branded_title(title, subtitle), self._main_select(page)]
        if section == "ai":
            children.append(self._ai_select(page))
        elif section == "modules":
            children.append(self._module_select(page if page != "modules" else None))
        children.append(discord.ui.Separator())
        return children

    def _actions(self, *buttons: _PanelButton, refresh: bool = True) -> discord.ui.ActionRow:
        # Page actions first; refresh and close always sit at the end.
        return discord.ui.ActionRow(
            *buttons,
            *([self._button("refresh", "重新整理")] if refresh else []),
            self._close_button(),
        )

    def _rate_item(self, *, compact: bool = False) -> discord.ui.TextDisplay:
        self._rate_display_item = discord.ui.TextDisplay(
            presentation.rate_text(self, compact=compact)
        )
        return self._rate_display_item

    async def _refresh_ai_data(self, operation: int | None = None) -> bool:
        if self._rate_interaction is not None and not await self._can_publish():
            return False
        async with asyncio.TaskGroup() as group:
            status = group.create_task(load_codex_status(self.codex_client))
            self._panel_registry.track(self._panel_session, status)
            limits = None
            if presentation.rate_visible(self):
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
        self._operation += 1
        self.stop()
        current = asyncio.current_task()
        for task in tuple(self._panel_session.tasks):
            if (
                task is not current
                and task is not self._retirement_task
                and not task.done()
                and not task.cancelling()
            ):
                task.cancel()
        self.close_stale_message()

    def bind_interaction(self, interaction: discord.Interaction) -> None:
        if self._rate_interaction is not None and interaction.id <= self._rate_interaction.id:
            return
        self._rate_interaction = interaction
        expires_at = getattr(interaction, "expires_at", None)
        self._rate_cutoff_at = (
            expires_at.timestamp() - RATE_EXPIRY_MARGIN_SECONDS
            if isinstance(expires_at, datetime)
            else None
        )

    def _before_idle_deadline(self) -> bool:
        return self._idle_deadline is None or time.monotonic() < self._idle_deadline

    async def _idle_loop(self) -> None:
        while self._registry_current():
            remaining = self._idle_deadline - time.monotonic()
            if remaining <= 0:
                self._remove_registry()
                return
            await asyncio.sleep(remaining)

    def _before_cutoff(self) -> bool:
        return self._rate_cutoff_at is not None and time.time() < self._rate_cutoff_at

    async def _can_publish(self) -> bool:
        return (
            self._registry_current()
            and self._before_cutoff()
            and self._before_idle_deadline()
            and await self._current_admin()
            and self._registry_current()
            and self._before_cutoff()
            and self._before_idle_deadline()
        )

    def close_stale_message(self) -> None:
        if self._retirement_task is not None:
            return
        self._retirement_task = asyncio.create_task(self._close_stale_message())
        self._panel_registry.track(self._panel_session, self._retirement_task)

    async def _close_stale_message(self) -> None:
        async with self._edit_lock:
            interaction = self._rate_interaction
            if interaction is None:
                return
            try:
                async with asyncio.timeout(3):
                    await interaction.delete_original_response()
            except discord.NotFound:
                return
            except Exception:
                logging.error("Admin panel message deletion failed.")
                try:
                    self._render_closed()
                    async with asyncio.timeout(3):
                        await interaction.edit_original_response(view=self)
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
        if (
            not self._registry_current()
            or not presentation.rate_visible(self)
            or self._rate_refresh_task is not None
        ):
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
                item is not self._rate_display_item
                or self.page not in RATE_PAGES
                or not await self._current_admin()
            ):
                return
            item.content = presentation.rate_text(
                self,
                stopped=True,
                compact=self.page != "ai_tech",
            )
            try:
                await interaction.edit_original_response(view=self)
                self.record_published()
            except discord.Forbidden, discord.NotFound, discord.HTTPException:
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
                if not self._before_cutoff():
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
                    item.content = presentation.rate_text(self, compact=self.page != "ai_tech")
                    try:
                        await self._rate_interaction.edit_original_response(view=self)
                        self.record_published()
                    except discord.Forbidden, discord.NotFound:
                        return
                    except discord.HTTPException, aiohttp.ClientError, TimeoutError:
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
        self._remove_registry()
        if self._retirement_task is not None:
            await asyncio.shield(self._retirement_task)

    async def on_timeout(self) -> None:
        await self.stop_rate_refresh()

    def _render_closed(self) -> None:
        self.calendar_unbind_target = None
        self.page = "closed"
        self._rate_display_item = None
        self._set_container(
            branded_title("管理控制台", ""),
            discord.ui.TextDisplay("-# 控制台已關閉；重新輸入 /控制台 可再開啟。"),
            discord.ui.ActionRow(self._button("closed", "已關閉", disabled=True)),
        )
        self.stop()

    def _render_page(self, page: str) -> None:
        if page == "ai":
            presentation.render_ai(self)
        elif page == "ai_access":
            presentation.render_ai_access(self)
        elif page == "ai_tech":
            presentation.render_ai_tech(self)
        elif page == "modules":
            presentation.render_modules(self)
        elif page == "voice":
            presentation.render_voice(self)
        elif page == "calendar":
            presentation.render_calendar(self)
        elif page == "steam":
            presentation.render_steam(self)
        else:
            presentation.render_overview(self)

    def _begin_operation(self) -> int:
        self._operation += 1
        return self._operation

    async def _acknowledge(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self._registry_current() and self._before_idle_deadline():
            self.bind_interaction(interaction)

    async def _defer_operation(self, interaction: discord.Interaction) -> int | None:
        operation = self._begin_operation()
        await self._acknowledge(interaction)
        return operation if self._is_current(operation) else None

    async def _run_operation(
        self,
        interaction: discord.Interaction,
        work: Callable[[int], Awaitable[Callable[[], None] | None]],
        *,
        close: bool = False,
    ) -> None:
        operation = await self._defer_operation(interaction)
        if operation is None:
            return
        if close:
            await self.stop_rate_refresh()
            return
        # Manager guards reject stale starts; accepted mutations finish their cleanup.
        render = await work(operation)
        if render is None or not self._is_current(operation):
            return
        render()
        await self._edit_original(interaction, operation)

    def _is_current(self, operation: int) -> bool:
        return (
            operation == self._operation
            and self._registry_current()
            and self._before_idle_deadline()
            and (self._rate_interaction is None or self._before_cutoff())
        )

    async def _edit_original(
        self,
        interaction: discord.Interaction,
        operation: int,
    ) -> None:
        if not self._is_current(operation):
            return
        publication = asyncio.create_task(self._publish_edit(interaction, operation))
        self._panel_registry.track(self._panel_session, publication)
        try:
            await publication
        except asyncio.CancelledError:
            # A direct callback cancellation still propagates, even after retirement.
            if asyncio.current_task().cancelling() or not self._panel_session.retired:
                raise

    def _layout_is_published(self) -> bool:
        return (
            self._desired_children is not None
            and self._desired_children == self._published_children
        )

    async def _publish_edit(
        self,
        interaction: discord.Interaction,
        operation: int,
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
                await interaction.edit_original_response(
                    view=self,
                )
            except Exception, asyncio.CancelledError:
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
        async def work(operation: int) -> Callable[[], None]:
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
                return lambda: presentation.render_ai_access(
                    self, "只能選擇目前伺服器的一般文字頻道。"
                )
            selected = frozenset(channel_ids)
            previous = self.codex_access.channel_ids
            result = await self.access_service.change_channels(
                guild_id,
                selected,
                still_current=lambda: self._is_current(operation),
            )
            count = len(selected)
            if result == "persist_failed":
                logging.error("管理控制台保存 Codex 白名單頻道失敗。")
                note = "白名單頻道無法保存，設定未變更。"
            elif result == "unchanged":
                note = f"目前已設定 {count} 個白名單頻道。"
            elif result == "archive_failed":
                logging.error("管理控制台停止續接舊 Codex 對話失敗。")
                note = "無法確認停止續接舊對話，頻道設定未變更。"
            elif result == "archived_persist_failed":
                note = "已停止續接舊對話，但頻道設定無法保存。"
            elif result == "updated_with_warning":
                note = f"已更新 {count} 個白名單頻道；已停止續接舊對話，部分 SDK 封存未確認。"
            elif previous - selected:
                note = f"已更新 {count} 個白名單頻道，已停止續接舊對話。"
            else:
                note = f"已更新 {count} 個白名單頻道。"
            return lambda: presentation.render_ai_access(self, note)

        await self._run_operation(interaction, work)

    async def handle_codex_role_select(
        self,
        interaction: discord.Interaction,
        roles: tuple[discord.Role, ...],
    ) -> None:
        async def work(operation: int) -> Callable[[], None]:
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
                return lambda: presentation.render_ai_access(
                    self, "只能選擇目前伺服器的一般身分組。"
                )
            selected = frozenset(role_ids)
            result = await self.access_service.change_roles(
                guild_id,
                selected,
                still_current=lambda: self._is_current(operation),
            )
            if result == "persist_failed":
                logging.error("管理控制台保存 Codex 白名單身分組失敗。")
                note = "白名單身分組無法保存，設定未變更。"
            elif result == "unchanged":
                note = f"目前已設定 {len(selected)} 個白名單身分組。"
            elif result == "archive_failed":
                logging.error("管理控制台切換白名單身分組前封存對話失敗。")
                note = "無法確認停止續接舊對話，角色設定未變更。"
            elif result == "archived_persist_failed":
                logging.error("管理控制台保存 Codex 白名單身分組失敗。")
                note = "已停止續接舊對話，但角色設定無法保存。"
            elif result == "updated_with_warning":
                note = f"已更新 {len(selected)} 個白名單身分組；已停止續接舊對話，部分 SDK 封存未確認。"
            else:
                note = f"已更新 {len(selected)} 個白名單身分組。"
            return lambda: presentation.render_ai_access(self, note)

        await self._run_operation(interaction, work)

    async def handle_steam_role_select(
        self,
        interaction: discord.Interaction,
        roles: tuple[discord.Role, ...],
    ) -> None:
        async def work(operation: int) -> Callable[[], None]:
            if not self.steam_free_games_enabled:
                note = "Steam 自動通知已停用，未修改身分組設定。"
            elif interaction.guild is None:
                note = "無法取得目前伺服器。"
            else:
                try:
                    await self.steam_free_games.set_notification_roles(
                        interaction.guild,
                        roles,
                        still_current=lambda: self._is_current(operation),
                    )
                except SteamConfigurationError as exc:
                    note = str(exc)
                else:
                    note = f"已更新 Steam 免費遊戲通知身分組，共 {len(roles)} 個。"
            return lambda: presentation.render_steam(self, notice=note)

        await self._run_operation(interaction, work)

    async def handle_calendar_channel_select(
        self, interaction: discord.Interaction, channel: object | None
    ) -> None:
        if self.page != "calendar":
            await self._acknowledge(interaction)
            return

        async def work(operation: int) -> Callable[[], None] | None:
            if not self.calendar.state_available:
                return lambda: presentation.render_calendar(self)
            if (
                not is_text_channel(channel)
                or getattr(getattr(channel, "guild", None), "id", None) != self.guild_id
            ):
                return lambda: presentation.render_calendar(
                    self, "只能選擇目前伺服器的一般文字頻道。"
                )
            self.pending_calendar_channel = channel
            self.calendar_unbind_target = None
            self.calendar_notice = None
            return lambda: presentation.render_calendar(self)

        await self._run_operation(interaction, work)

    async def _calendar_action(
        self,
        interaction: discord.Interaction,
        action: str,
        operation: int,
        channel: discord.TextChannel | None,
        target: CalendarBinding | None,
        revision: int,
    ) -> Callable[[], None] | None:
        manager = self.calendar
        if not manager.state_available:
            return lambda: presentation.render_calendar(self)
        if action == "calendar_unbind":
            self.calendar_unbind_target = manager.get_binding(self.guild_id)
            return lambda: presentation.render_calendar(self)
        if action == "calendar_unbind_cancel":
            self.calendar_unbind_target = None
            return lambda: presentation.render_calendar(self)
        member_snapshot = self.guild.get_member(self.user_id)
        if member_snapshot is None:
            try:
                async with asyncio.timeout(3):
                    member_snapshot = await self.guild.fetch_member(self.user_id)
            except TimeoutError, discord.HTTPException:
                return None
        if not member_snapshot.guild_permissions.administrator or not self._is_current(operation):
            return None

        def current() -> bool:
            member = self.guild.get_member(self.user_id) or member_snapshot
            return (
                self._is_current(operation)
                and self.page == "calendar"
                and member is not None
                and member.guild_permissions.administrator
                and revision == manager.get_binding_revision(self.guild_id)
                and (action != "calendar_unbind_confirm" or self.calendar_unbind_target == target)
            )

        try:
            if action == "calendar_apply":
                if channel is None:
                    note = "⚠️ 請先選擇文字頻道。"
                else:
                    binding = await manager.bind(
                        self.guild,
                        channel,
                        actor_id=self.user_id,
                        is_current=current,
                    )
                    if not self._is_current(operation):
                        return None
                    note = f"✓ 已綁定到 <#{binding.channel_id}>。"
                    if self.pending_calendar_channel == channel:
                        self.pending_calendar_channel = None
            elif action == "calendar_unbind_confirm":
                if target is None or self.calendar_unbind_target != target:
                    note = "解除確認已取消或失效，請重新確認。"
                else:
                    removed = await manager.unbind(
                        self.guild,
                        actor_id=self.user_id,
                        expected_binding=target,
                        is_current=current,
                    )
                    note = "✓ 已解除行事曆看板。" if removed else "目前沒有綁定行事曆看板。"
                if self._is_current(operation):
                    self.calendar_unbind_target = None
            else:
                if not current():
                    return None
                ok = await manager.refresh_guild(self.guild, is_current=current)
                note = "✓ 行事曆看板已重新整理。" if ok else "⚠️ 行事曆看板目前無法重新整理。"
        except CalendarUserError as exc:
            note = f"⚠️ {exc}"
            if action == "calendar_unbind_confirm" and self._is_current(operation):
                self.calendar_unbind_target = None
        return lambda: presentation.render_calendar(self, note)

    async def handle_action(self, interaction: discord.Interaction, action: str) -> None:
        if action == "noop":
            await self._acknowledge(interaction)
            return
        if action not in PAGES | {
            "refresh",
            "close",
            "voice_sync",
            "steam_role_clear",
            "steam_query",
            "calendar_apply",
            "calendar_unbind",
            "calendar_unbind_confirm",
            "calendar_unbind_cancel",
        }:
            return

        calendar_action = action.startswith("calendar_") or (
            action == "refresh" and self.page == "calendar"
        )
        if calendar_action and self.page != "calendar":
            await self._acknowledge(interaction)
            return
        channel = self.pending_calendar_channel
        target = self.calendar_unbind_target
        revision = self.calendar.get_binding_revision(self.guild_id)
        if (action in PAGES and action != "calendar") or action == "close":
            self.calendar_unbind_target = None

        async def work(operation: int) -> Callable[[], None] | None:
            if calendar_action:
                return await self._calendar_action(
                    interaction, action, operation, channel, target, revision
                )
            if action in PAGES - {"ai"}:
                return lambda: self._render_page(action)
            if action == "close":
                return self._render_closed
            if action == "ai":
                if not await self._refresh_ai_data(operation):
                    return None
                return lambda: presentation.render_ai(self)
            if action == "refresh":
                page = self.page
                if page in RATE_PAGES and not await self._refresh_ai_data(operation):
                    return None
                return lambda: self._render_page(page)
            if action == "voice_sync":
                voice = presentation.voice_state(self)
                if not self.temp_voice_enabled:
                    return lambda: presentation.render_voice(
                        self, "功能已停用，未執行同步。", voice=voice
                    )
                if not voice.status.state_available:
                    return lambda: presentation.render_voice(
                        self, "狀態檔不可用，未執行同步。", voice=voice
                    )
                guild = interaction.guild
                if guild is None or guild.id != self.guild_id:
                    return lambda: presentation.render_voice(self, "無法取得目前伺服器。")
                try:
                    await self.temp_voice.reconcile(
                        [guild],
                        prune_absent=False,
                        still_current=lambda: self._is_current(operation),
                    )
                except Exception:
                    logging.exception("管理控制台重新同步臨時語音失敗。")
                    return lambda: presentation.render_voice(
                        self, "重新同步失敗，請查看 Bot 紀錄。"
                    )
                voice = presentation.voice_state(self, guild)
                status, problem = voice.status, voice.problem
                if (
                    not status.state_available
                    or status.parent_channel_id is None
                    or problem is not None
                ):
                    detail = problem or "入口頻道尚未綁定，請稍後重試並確認 Bot 有管理頻道權限。"
                    return lambda: presentation.render_voice(
                        self, f"同步未完成；{detail}", voice=voice
                    )
                return lambda: presentation.render_voice(
                    self, "入口檢查通過；已執行同步流程。", voice=voice
                )
            if action == "steam_role_clear":
                if not self.steam_free_games_enabled:
                    note = "Steam 自動通知已停用，未修改身分組設定。"
                else:
                    try:
                        removed = await self.steam_free_games.clear_notification_roles(
                            self.guild_id,
                            still_current=lambda: self._is_current(operation),
                        )
                    except SteamConfigurationError as exc:
                        note = str(exc)
                    else:
                        note = (
                            "已取消 Steam 免費遊戲通知身分組。"
                            if removed
                            else "目前沒有設定 Steam 通知身分組。"
                        )
                return lambda: presentation.render_steam(self, notice=note)
            try:
                result = await self.steam_free_games.fetch_current_offers()
            except Exception:
                logging.exception("管理控制台查詢 Steam 免費遊戲失敗。")
                return lambda: presentation.render_steam(self, error="目前無法取得 Steam 資料。")
            if result is None:
                return lambda: presentation.render_steam(self, error="目前無法取得 Steam 資料。")
            return lambda: presentation.render_steam(self, result)

        await self._run_operation(interaction, work, close=action == "close")
