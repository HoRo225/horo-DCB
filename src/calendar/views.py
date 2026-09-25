from __future__ import annotations

import asyncio
import calendar as month_calendar
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from src.brand import BRAND_COLOUR, banner, branded_title
from src.calendar.models import (
    CALENDAR_TZ,
    CalendarBinding,
    CalendarCreateUncertain,
    CalendarEditUnavailable,
    CalendarUserError,
    build_calendar_event_input,
    calendar_now,
)
from src.calendar.discord_models import (
    event_local_time,
    event_location,
    event_to_input,
    event_url,
    is_external_scheduled,
    safe_event_name,
)

if TYPE_CHECKING:
    from src.calendar.discord import CalendarController
    from src.calendar.manager import CalendarManager

MAX_UPCOMING_SHOWN = 3
EVENTS_PER_PAGE = 25
BROWSE_EVENTS_PER_PAGE = 8
BOARD_CREATE_CUSTOM_ID = "horo:calendar:create"
BOARD_EDIT_CUSTOM_ID = "horo:calendar:edit"
BOARD_BROWSE_CUSTOM_ID = "horo:calendar:browse"
BOARD_REFRESH_CUSTOM_ID = "horo:calendar:refresh"

CALENDAR_STATE_UNAVAILABLE_NOTICE = (
    "⚠️ 行事曆狀態目前不可用，請管理員檢查儲存狀態並重新啟動 Bot。"
)


def render_board_text(
    guild_name: str,
    events: list[discord.ScheduledEvent] | tuple[discord.ScheduledEvent, ...],
) -> str:
    current = calendar_now().astimezone(CALENDAR_TZ)
    event_days = {
        local.day
        for event in events
        if (local := event_local_time(event)) is not None
        and local.year == current.year
        and local.month == current.month
    }
    weeks = month_calendar.Calendar(firstweekday=month_calendar.SUNDAY).monthdayscalendar(
        current.year,
        current.month,
    )
    weekday_labels = "日一二三四五六"
    calendar_lines = ["".join(f"{label:^5}" for label in weekday_labels).rstrip()]
    for week in weeks:
        cells = []
        for day in week:
            if day == 0:
                cells.append("     ")
            elif day == current.day:
                cells.append(f"[{day:>2}]{'•' if day in event_days else ' '}")
            else:
                cells.append(f"{day:>2}{'•' if day in event_days else ' '}  ")
        calendar_lines.append("".join(cells).rstrip())
    safe_guild = discord.utils.escape_markdown(guild_name)[:100]
    lines = [
        f"# 📅 {safe_guild} 行事曆",
        f"## {current.year} 年 {current.month} 月",
        "```text",
        *calendar_lines,
        "```",
        f"-# 今天：{current.year}/{current.month}/{current.day}（{weekday_labels[(current.weekday() + 1) % 7]}） · [日期] 代表今天 · • 代表活動",
        "-# 所有輸入時間均以 UTC+8 解讀",
        "## 即將到來",
    ]
    upcoming = list(events[:MAX_UPCOMING_SHOWN])
    if not upcoming:
        lines.append("目前沒有即將到來的活動。\n-# 有「管理活動」權限的成員可從下方新增第一個活動。")
    for event in upcoming:
        start = getattr(event, "start_time", None)
        if not isinstance(start, datetime):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        lines.append(f"**{safe_event_name(event)}**")
        lines.append(
            f"{discord.utils.format_dt(start, style='F')} · "
            f"{discord.utils.format_dt(start, style='R')}"
        )
        lines.append(f"-# {event_location(event)}")
        url = event_url(event)
        if url:
            lines.append(f"[開啟活動]({url})")
    extra = len(events) - len(upcoming)
    if extra > 0:
        lines.append(f"-# 另有 {extra} 個活動，按「瀏覽活動」查看。")
    return "\n".join(lines)


class _CalendarAdminChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="選擇新的行事曆頻道",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CalendarAdminView) or interaction.guild is None:
            return
        if not view.manager.state_available:
            await view.publish_state_unavailable(interaction)
            return
        selected = self.values[0] if self.values else None
        channel = selected.resolve() if selected is not None else None
        if channel is None or getattr(channel, "type", None) != discord.ChannelType.text:
            await interaction.response.send_message(
                "只能選擇目前伺服器中的文字頻道。",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        view.pending_channel_id = channel.id
        view.pending_channel = channel
        view.notice = None
        view.unbind_target = None
        await interaction.response.defer()
        await view.publish(interaction)


class _CalendarAdminActionButton(discord.ui.Button):
    def __init__(self, action: str) -> None:
        label, style = {
            "apply": ("套用綁定", discord.ButtonStyle.primary),
            "refresh": ("重新整理", discord.ButtonStyle.secondary),
            "unbind": ("解除綁定", discord.ButtonStyle.secondary),
            "unbind_confirm": ("確認解除", discord.ButtonStyle.danger),
            "unbind_cancel": ("取消", discord.ButtonStyle.secondary),
        }[action]
        super().__init__(
            label=label,
            style=style,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CalendarAdminView) or interaction.guild is None:
            return
        if not view.manager.state_available:
            await view.publish_state_unavailable(interaction)
            return
        if self.action == "unbind":
            view.unbind_target = view.manager.get_binding(view.guild_id)
            view.notice = None if view.unbind_target is not None else "目前沒有綁定行事曆看板。"
            await interaction.response.defer()
            await view.publish(interaction)
            return
        if self.action == "unbind_cancel":
            view.unbind_target = None
            await interaction.response.defer()
            await view.publish(interaction)
            return
        channel = view.pending_channel
        unbind_target = view.unbind_target
        binding_revision = (
            view.manager.get_binding_revision(view.guild_id)
            if self.action == "apply" else None
        )
        if self.action in {"apply", "unbind_confirm"}:
            view.operation_version += 1
        operation_version = view.operation_version
        await interaction.response.defer()
        if self.action in {"apply", "unbind_confirm"} and operation_version != view.operation_version:
            return
        if self.action == "apply":
            if channel is None:
                view.notice = "⚠️ 請先選擇文字頻道。"
            else:
                try:
                    binding = await view.manager.bind(
                        interaction.guild, channel, actor_id=interaction.user.id,
                        is_current=lambda: (
                            operation_version == view.operation_version
                            and binding_revision == view.manager.get_binding_revision(
                                view.guild_id
                            )
                        ),
                    )
                    if operation_version != view.operation_version:
                        return
                    view.notice = f"✓ 已綁定到 <#{binding.channel_id}>。"
                    if view.pending_channel_id == channel.id:
                        view.pending_channel_id = None
                        view.pending_channel = None
                except CalendarUserError as exc:
                    if operation_version != view.operation_version:
                        return
                    view.notice = f"⚠️ {exc}"
        elif self.action == "refresh":
            ok = await view.manager.refresh_guild(interaction.guild)
            view.notice = "✓ 行事曆看板已重新整理。" if ok else "⚠️ 行事曆看板目前無法重新整理。"
        else:
            if unbind_target is None or view.unbind_target != unbind_target:
                view.notice = "解除確認已取消或失效，請重新確認。"
            else:
                try:
                    removed = await view.manager.unbind(
                        interaction.guild,
                        actor_id=interaction.user.id,
                        expected_binding=unbind_target,
                        is_current=lambda: (
                            operation_version == view.operation_version
                            and view.unbind_target == unbind_target
                        ),
                    )
                    if operation_version != view.operation_version:
                        return
                    view.notice = "✓ 已解除行事曆看板。" if removed else "目前沒有綁定行事曆看板。"
                except CalendarUserError as exc:
                    if operation_version != view.operation_version:
                        return
                    view.notice = f"⚠️ {exc}"
            if view.unbind_target == unbind_target:
                view.unbind_target = None
        await view.publish(interaction)


class CalendarAdminView(discord.ui.LayoutView):
    def __init__(self, manager: CalendarManager, *, user_id: int, guild_id: int) -> None:
        super().__init__(timeout=15 * 60)
        self.manager = manager
        self.user_id = user_id
        self.guild_id = guild_id
        self.pending_channel_id: int | None = None
        self.pending_channel: discord.TextChannel | None = None
        self.notice: str | None = None
        self.unbind_target: CalendarBinding | None = None
        self.operation_version = 0
        self._publish_lock = asyncio.Lock()
        self._channel_select = _CalendarAdminChannelSelect()
        self._action_buttons = {
            action: _CalendarAdminActionButton(action)
            for action in (
                "apply", "refresh", "unbind", "unbind_confirm", "unbind_cancel",
            )
        }
        self.render()

    async def publish_state_unavailable(self, interaction: discord.Interaction) -> None:
        self.notice = CALENDAR_STATE_UNAVAILABLE_NOTICE
        self.unbind_target = None
        await interaction.response.defer()
        await self.publish(interaction)

    def _button(self, action: str, *, disabled: bool) -> _CalendarAdminActionButton:
        button = self._action_buttons[action]
        button.disabled = disabled
        return button

    async def publish(self, interaction: discord.Interaction) -> None:
        async with self._publish_lock:
            old_children = tuple(self.children)
            try:
                self.render()
                await interaction.edit_original_response(
                    view=self,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (Exception, asyncio.CancelledError):
                self.clear_items()
                for child in old_children:
                    self.add_item(child)
                raise

    def render(self) -> None:
        self.clear_items()
        state_available = self.manager.state_available
        if state_available:
            if self.notice == CALENDAR_STATE_UNAVAILABLE_NOTICE:
                self.notice = None
            binding = self.manager.get_binding(self.guild_id)
            bound = binding is not None
            status = (
                f"**目前綁定**　<#{binding.channel_id}>\n-# 看板訊息會隨活動與日期自動更新。"
                if binding is not None
                else "**尚未綁定**\n-# 選擇文字頻道後按「套用綁定」。"
            )
        else:
            self.notice = CALENDAR_STATE_UNAVAILABLE_NOTICE
            self.unbind_target = None
            binding = None
            bound = False
            status = (
                "**行事曆狀態目前不可用**\n"
                "-# 請管理員檢查儲存狀態並重新啟動 Bot。"
            )
        self._channel_select.disabled = not state_available
        if self.pending_channel_id is not None:
            status += f"\n\n**待套用**　<#{self.pending_channel_id}>"
        if self.notice and self.notice != CALENDAR_STATE_UNAVAILABLE_NOTICE:
            status += f"\n\n{discord.utils.escape_markdown(self.notice)}"
        children: list[discord.ui.Item] = [
            branded_title("📅 行事曆管理", "設定公開看板的位置與更新狀態"),
            discord.ui.Separator(spacing=discord.SeparatorSpacing.large),
            discord.ui.TextDisplay(status),
            discord.ui.ActionRow(self._channel_select),
        ]
        if self.unbind_target is not None:
            children.extend(
                (
                    discord.ui.TextDisplay(
                        "### 解除綁定？\n-# 看板訊息將被移除；既有 Discord 活動不會刪除。"
                    ),
                    discord.ui.ActionRow(
                        self._button("unbind_confirm", disabled=False),
                        self._button("unbind_cancel", disabled=False),
                    ),
                )
            )
        else:
            buttons: list[discord.ui.Button] = [
                self._button(
                    "apply",
                    disabled=not state_available or self.pending_channel_id is None,
                ),
                self._button("refresh", disabled=not state_available or not bound),
            ]
            if binding is not None:
                buttons.append(
                    discord.ui.Button(
                        label="開啟看板",
                        style=discord.ButtonStyle.link,
                        url=(
                            f"https://discord.com/channels/{self.guild_id}/"
                            f"{binding.channel_id}/{binding.message_id}"
                        ),
                    )
                )
            buttons.append(self._button("unbind", disabled=not state_available or not bound))
            children.append(discord.ui.ActionRow(*buttons))
        self.add_item(discord.ui.Container(*children, accent_colour=BRAND_COLOUR))

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if (
            interaction.user.id == self.user_id
            and interaction.guild_id == self.guild_id
            and getattr(interaction.permissions, "administrator", False)
        ):
            return True
        await interaction.response.send_message(
            "只有開啟面板的伺服器管理員可以操作這個行事曆面板。",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False


class _CalendarBoardButton(discord.ui.Button):
    def __init__(
        self,
        action: str,
        label: str,
        custom_id: str,
        *,
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            label=label, custom_id=custom_id, style=style, disabled=disabled,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, CalendarBoardView):
            await view.controller.handle_board_action(interaction, self.action)


class CalendarBoardView(discord.ui.LayoutView):
    def __init__(
        self, controller: CalendarController, text: str, *, can_edit: bool = True,
    ) -> None:
        super().__init__(timeout=None)
        self.controller = controller
        children: list[discord.ui.Item] = []
        if (brand_banner := banner()) is not None:
            children.append(brand_banner)
        children.extend(
            (
                discord.ui.TextDisplay(text),
                discord.ui.Separator(),
                discord.ui.TextDisplay("-# 新增與編輯需要「管理活動」權限。"),
                discord.ui.ActionRow(
                    _CalendarBoardButton(
                        "create",
                        "新增活動",
                        BOARD_CREATE_CUSTOM_ID,
                        style=discord.ButtonStyle.primary,
                    ),
                    _CalendarBoardButton(
                        "edit", "編輯活動", BOARD_EDIT_CUSTOM_ID,
                        disabled=not can_edit,
                    ),
                    _CalendarBoardButton("browse", "瀏覽活動", BOARD_BROWSE_CUSTOM_ID),
                    _CalendarBoardButton("refresh", "重新整理", BOARD_REFRESH_CUSTOM_ID),
                ),
            )
        )
        self.add_item(
            discord.ui.Container(*children, accent_colour=BRAND_COLOUR)
        )


DRAFT_NOTICE_CONTENT_LIMIT = 4000


def _draft_notice_text(title: str, message: str, draft: dict[str, str]) -> str:
    safe = lambda value: discord.utils.escape_markdown(str(value))
    content = "\n".join(
        (
            f"## ⚠️ {safe(title)}",
            safe(message),
            "",
            "**原草稿（可複製）**",
            f"活動名稱：{safe(draft.get('name', ''))}",
            f"開始時間：{safe(draft.get('start', ''))}",
            f"活動長度（分鐘）：{safe(draft.get('duration', ''))}",
            f"地點：{safe(draft.get('location', ''))}",
            f"說明：{safe(draft.get('description', ''))}",
        )
    )
    if len(content) <= DRAFT_NOTICE_CONTENT_LIMIT:
        return content
    suffix = "\n\n-# 原草稿過長，部分內容已省略。"
    prefix_limit = DRAFT_NOTICE_CONTENT_LIMIT - len(suffix) - 1
    prefix = content[:max(0, prefix_limit)].rstrip("\\")
    return f"{prefix}…{suffix}"


class _CalendarDraftNoticeView(discord.ui.LayoutView):
    def __init__(self, title: str, message: str, draft: dict[str, str]) -> None:
        super().__init__(timeout=5 * 60)
        self.add_item(discord.ui.TextDisplay(
            _draft_notice_text(title, message, draft)
        ))


class _CalendarModalRetryView(discord.ui.View):
    def __init__(
        self,
        manager: CalendarManager,
        *,
        user_id: int,
        guild_id: int,
        draft: dict[str, str],
        event_id: int | None,
    ) -> None:
        super().__init__(timeout=5 * 60)
        self.manager = manager
        self.user_id = user_id
        self.guild_id = guild_id
        self.draft = draft
        self.event_id = event_id

    @discord.ui.button(label="返回修改", style=discord.ButtonStyle.primary)
    async def retry(
        self, interaction: discord.Interaction, _button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(
            CalendarEventModal(
                self.manager, draft=self.draft, event_id=self.event_id,
            )
        )

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        return interaction.user.id == self.user_id and interaction.guild_id == self.guild_id


class CalendarEventModal(discord.ui.Modal):
    def __init__(
        self,
        manager: CalendarManager,
        event: discord.ScheduledEvent | None = None,
        *,
        draft: dict[str, str] | None = None,
        event_id: int | None = None,
    ) -> None:
        event_input = event_to_input(event) if event is not None else None
        editing = event is not None or event_id is not None
        super().__init__(
            title="編輯活動" if editing else "新增活動",
            timeout=5 * 60,
        )
        self.manager = manager
        self.event_id = event.id if event is not None else event_id

        def initial(key: str, fallback: str | None) -> str | None:
            return draft.get(key, "") if draft is not None else fallback

        self.name_input = discord.ui.TextInput(
            label="活動名稱",
            min_length=1,
            max_length=100,
            default=initial("name", event_input.name if event_input is not None else None),
        )
        self.start_input = discord.ui.TextInput(
            label="開始時間（UTC+8）",
            placeholder="例如 2026-09-20 19:30",
            min_length=16,
            max_length=16,
            default=initial("start", (
                event_input.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M")
                if event_input is not None else None
            )),
        )
        self.duration_input = discord.ui.TextInput(
            label="活動長度（分鐘）",
            min_length=1,
            max_length=5,
            default=initial("duration", (
                str(event_input.duration_minutes) if event_input is not None else "60"
            )),
        )
        self.location_input = discord.ui.TextInput(
            label="地點",
            min_length=1,
            max_length=100,
            default=initial("location", (
                event_input.location if event_input is not None else "Discord"
            )),
        )
        self.description_input = discord.ui.TextInput(
            label="說明（選填）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            default=initial("description", (
                event_input.description if event_input is not None else None
            )),
        )
        for item in (
            self.name_input,
            self.start_input,
            self.duration_input,
            self.location_input,
            self.description_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = {
            "name": str(self.name_input.value),
            "start": str(self.start_input.value),
            "duration": str(self.duration_input.value),
            "location": str(self.location_input.value),
            "description": str(self.description_input.value or ""),
        }
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("行事曆只能在伺服器中使用。", ephemeral=True)
            return
        try:
            duration = int(str(self.duration_input.value).strip())
        except ValueError:
            await self._send_error(interaction, "活動長度必須是整數分鐘。", draft)
            return
        try:
            event_input = build_calendar_event_input(
                name=str(self.name_input.value),
                start=str(self.start_input.value),
                duration_minutes=duration,
                location=str(self.location_input.value),
                description=str(self.description_input.value or ""),
            )
            if self.event_id is None:
                event = await self.manager.create_event(
                    interaction.guild, event_input, interaction.user,
                )
                action = "建立"
            else:
                event = await self.manager.edit_event(
                    interaction.guild, self.event_id, event_input, interaction.user,
                )
                action = "修改"
        except CalendarCreateUncertain as exc:
            await self._send_error(
                interaction,
                str(exc),
                draft,
                retryable=False,
                title="建立結果尚未確認",
            )
            return
        except CalendarEditUnavailable as exc:
            await self._send_error(interaction, str(exc), draft, retryable=False)
            return
        except CalendarUserError as exc:
            await self._send_error(interaction, str(exc), draft)
            return
        start = event_input.start_time
        await interaction.followup.send(
            f"## ✓ 已{action}活動\n"
            f"**{discord.utils.escape_markdown(event_input.name)}**\n"
            f"{discord.utils.format_dt(start, style='F')} · "
            f"{discord.utils.escape_markdown(event_input.location)}\n"
            f"[開啟活動]({event_url(event) or getattr(event, 'url', '')})",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _send_error(
        self,
        interaction: discord.Interaction,
        message: str,
        draft: dict[str, str],
        *,
        retryable: bool = True,
        title: str = "編輯活動已失效",
    ) -> None:
        if not retryable:
            await interaction.followup.send(
                view=_CalendarDraftNoticeView(title, message, draft),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.followup.send(
            f"⚠️ {message}",
            view=_CalendarModalRetryView(
                self.manager,
                user_id=interaction.user.id,
                guild_id=interaction.guild_id or 0,
                draft=draft,
                event_id=self.event_id,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _EditSelect(discord.ui.Select["CalendarEditPickerView"]):
    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CalendarEditPickerView):
            return
        try:
            event_id = int(self.values[0])
            if interaction.guild is None:
                raise CalendarUserError("行事曆只能在伺服器中使用。")
            view.manager.assert_user_can_manage(interaction.user)
            event = next((item for item in view.events if item.id == event_id), None)
            if event is None or not is_external_scheduled(event):
                raise CalendarUserError("這個活動已失效，請重新選擇。")
            event = view.manager.get_editable_event(interaction.guild, event_id)
            modal = CalendarEventModal(view.manager, event)
        except (ValueError, CalendarUserError) as exc:
            text = str(exc) if isinstance(exc, CalendarUserError) else "活動選擇不正確。"
            await interaction.response.send_message(text, ephemeral=True)
            return
        await interaction.response.send_modal(modal)


class _CalendarPageButton(discord.ui.Button):
    def __init__(self, direction: int, *, disabled: bool) -> None:
        super().__init__(
            label="上一頁" if direction < 0 else "下一頁",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, (CalendarEditPickerView, CalendarBrowseView)):
            return
        await interaction.response.defer()
        async with view._publish_lock:
            old_page = view.page
            view.page += self.direction
            try:
                view.render()
                kwargs = {"view": view}
                if isinstance(view, CalendarBrowseView):
                    kwargs["content"] = view.page_text()
                await interaction.edit_original_response(**kwargs)
            except (Exception, asyncio.CancelledError):
                view.page = old_page
                view.render()
                raise


class CalendarEditPickerView(discord.ui.View):
    def __init__(
        self,
        manager: CalendarManager,
        user_id: int,
        guild_id: int,
        events: list[discord.ScheduledEvent],
    ) -> None:
        super().__init__(timeout=5 * 60)
        self.manager = manager
        self.user_id = user_id
        self.guild_id = guild_id
        self.events = tuple(events)
        self.page = 0
        self._page_count = max(1, (len(self.events) + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE)
        self._publish_lock = asyncio.Lock()
        self._edit_select = _EditSelect(placeholder="選擇活動", options=[])
        self._previous_page_button = _CalendarPageButton(-1, disabled=True)
        self._next_page_button = _CalendarPageButton(1, disabled=False)
        self.add_item(self._edit_select)
        if self._page_count > 1:
            self.add_item(self._previous_page_button)
            self.add_item(self._next_page_button)
        self.render()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.user_id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message("只有原操作使用者可以使用這個選單。", ephemeral=True)
        return False

    def render(self) -> None:
        self.page = min(max(self.page, 0), self._page_count - 1)
        start = self.page * EVENTS_PER_PAGE
        page_events = self.events[start : start + EVENTS_PER_PAGE]
        options = []
        for event in page_events:
            local = event_local_time(event)
            description = local.strftime("%Y-%m-%d %H:%M") if local else "時間未知"
            options.append(
                discord.SelectOption(
                    label=safe_event_name(event)[:100],
                    description=description[:100],
                    value=str(event.id),
                )
            )
        self._edit_select.options = options
        if self._page_count > 1:
            self._previous_page_button.disabled = self.page <= 0
            self._next_page_button.disabled = self.page >= self._page_count - 1


class CalendarBrowseView(discord.ui.View):
    def __init__(
        self,
        user_id: int,
        guild_id: int,
        events: list[discord.ScheduledEvent],
    ) -> None:
        super().__init__(timeout=5 * 60)
        self.user_id = user_id
        self.guild_id = guild_id
        self.events = tuple(events)
        self.page = 0
        self._page_count = max(
            1,
            (len(self.events) + BROWSE_EVENTS_PER_PAGE - 1) // BROWSE_EVENTS_PER_PAGE,
        )
        self._publish_lock = asyncio.Lock()
        self._previous_page_button = _CalendarPageButton(-1, disabled=True)
        self._next_page_button = _CalendarPageButton(1, disabled=False)
        if self._page_count > 1:
            self.add_item(self._previous_page_button)
            self.add_item(self._next_page_button)
        self.render()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.user_id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message("只有原操作使用者可以使用這個選單。", ephemeral=True)
        return False

    def render(self) -> None:
        self.page = min(max(self.page, 0), self._page_count - 1)
        if self._page_count > 1:
            self._previous_page_button.disabled = self.page <= 0
            self._next_page_button.disabled = self.page >= self._page_count - 1

    def page_text(self) -> str:
        if not self.events:
            return "目前沒有即將到來的活動。"
        start_index = self.page * BROWSE_EVENTS_PER_PAGE
        page_events = self.events[start_index : start_index + BROWSE_EVENTS_PER_PAGE]
        lines = [f"## 活動列表 · 第 {self.page + 1} 頁"]
        for event in page_events:
            start = getattr(event, "start_time", None)
            if not isinstance(start, datetime):
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            line = f"**{safe_event_name(event)}** · {discord.utils.format_dt(start, style='F')}"
            url = event_url(event)
            if url:
                line += f" · [開啟]({url})"
            lines.append(line)
        return "\n".join(lines)
