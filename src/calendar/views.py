from __future__ import annotations

import calendar as month_calendar
from datetime import datetime, timezone

import discord

from src.calendar.manager import CalendarManager
from src.calendar.models import (
    CALENDAR_TZ,
    CalendarUserError,
    _event_local_time,
    _event_location,
    _event_url,
    _is_external_scheduled,
    _safe_event_name,
    build_calendar_event_input,
    calendar_now,
    event_to_input,
)

MAX_UPCOMING_SHOWN = 8
EVENTS_PER_PAGE = 25
BROWSE_EVENTS_PER_PAGE = 8
BOARD_CREATE_CUSTOM_ID = "horo:calendar:create"
BOARD_EDIT_CUSTOM_ID = "horo:calendar:edit"
BOARD_BROWSE_CUSTOM_ID = "horo:calendar:browse"
BOARD_REFRESH_CUSTOM_ID = "horo:calendar:refresh"


def render_board_text(
    guild_name: str,
    events: list[discord.ScheduledEvent] | tuple[discord.ScheduledEvent, ...],
    *,
    now: datetime | None = None,
) -> str:
    current = (now or calendar_now()).astimezone(CALENDAR_TZ)
    event_days = {
        local.day
        for event in events
        if (local := _event_local_time(event)) is not None
        and local.year == current.year
        and local.month == current.month
    }
    weeks = month_calendar.Calendar(firstweekday=month_calendar.SUNDAY).monthdayscalendar(
        current.year,
        current.month,
    )
    calendar_lines = ["日  一  二  三  四  五  六"]
    for week in weeks:
        cells = []
        for day in week:
            if day == 0:
                cells.append("   ")
            else:
                cells.append(f"{day:>2}{'•' if day in event_days else ' '}")
        calendar_lines.append(" ".join(cells).rstrip())
    safe_guild = discord.utils.escape_markdown(guild_name)[:100]
    lines = [
        f"# 📅 {safe_guild} 行事曆",
        f"## {current.year} 年 {current.month} 月",
        "```text",
        *calendar_lines,
        "```",
        "-# • 代表當天有活動 · 輸入時間以 UTC+8 解讀",
        "## 即將到來",
    ]
    upcoming = list(events[:MAX_UPCOMING_SHOWN])
    if not upcoming:
        lines.append("目前沒有即將到來的活動。")
    for event in upcoming:
        start = getattr(event, "start_time", None)
        if not isinstance(start, datetime):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        lines.append(f"**{_safe_event_name(event)}**")
        lines.append(
            f"{discord.utils.format_dt(start, style='F')} · "
            f"{discord.utils.format_dt(start, style='R')}"
        )
        lines.append(f"-# {_event_location(event)}")
        url = _event_url(event)
        if url:
            lines.append(f"[開啟 Discord 活動]({url})")
    extra = len(events) - len(upcoming)
    if extra > 0:
        lines.append(f"-# 另有 {extra} 個活動，按「瀏覽活動」查看。")
    return "\n".join(lines)


def admin_panel_text(
    manager: CalendarManager,
    guild: discord.Guild,
    *,
    notice: str | None = None,
) -> str:
    binding = manager.get_binding(guild.id)
    lines = ["## 📅 行事曆管理"]
    if binding is None:
        lines.append("目前尚未綁定行事曆看板。")
    else:
        lines.append(f"目前綁定：<#{binding.channel_id}>")
    lines.append("從下方選擇文字頻道即可綁定或重新綁定。")
    if notice:
        lines.extend(("", notice))
    return "\n".join(lines)


class _CalendarAdminChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            custom_id="horo:calendar:admin:channel",
            channel_types=[discord.ChannelType.text],
            placeholder="選擇文字頻道以綁定行事曆看板",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CalendarAdminView) or interaction.guild is None:
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

        await interaction.response.defer()
        current = view.manager.get_binding(interaction.guild.id)
        if current is not None and current.channel_id == channel.id:
            notice = f"目前已綁定到 <#{channel.id}>。"
        else:
            try:
                binding = await view.manager.bind(
                    interaction.guild,
                    channel,
                    actor_id=interaction.user.id,
                )
                notice = f"已將行事曆看板綁定到 <#{binding.channel_id}>。"
            except CalendarUserError as exc:
                notice = f"⚠️ {exc}"
        view.render()
        await interaction.edit_original_response(
            content=admin_panel_text(view.manager, interaction.guild, notice=notice),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _CalendarAdminActionButton(discord.ui.Button):
    def __init__(self, action: str, *, disabled: bool) -> None:
        if action == "refresh":
            label = "重新整理看板"
            style = discord.ButtonStyle.secondary
        else:
            label = "解除綁定"
            style = discord.ButtonStyle.danger
        super().__init__(
            label=label,
            custom_id=f"horo:calendar:admin:{action}",
            style=style,
            disabled=disabled,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, CalendarAdminView) or interaction.guild is None:
            return
        await interaction.response.defer()
        if self.action == "refresh":
            ok = await view.manager.refresh_guild(interaction.guild)
            notice = "行事曆看板已重新整理。" if ok else "⚠️ 行事曆看板目前無法重新整理。"
        else:
            try:
                removed = await view.manager.unbind(
                    interaction.guild,
                    actor_id=interaction.user.id,
                )
                notice = "已解除行事曆看板。" if removed else "此伺服器目前沒有綁定行事曆看板。"
            except CalendarUserError as exc:
                notice = f"⚠️ {exc}"
        view.render()
        await interaction.edit_original_response(
            content=admin_panel_text(view.manager, interaction.guild, notice=notice),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class CalendarAdminView(discord.ui.View):
    def __init__(self, manager: CalendarManager, *, user_id: int, guild_id: int) -> None:
        super().__init__(timeout=15 * 60)
        self.manager = manager
        self.user_id = user_id
        self.guild_id = guild_id
        self.render()

    def render(self) -> None:
        self.clear_items()
        self.add_item(_CalendarAdminChannelSelect())
        bound = self.manager.has_binding(self.guild_id)
        self.add_item(_CalendarAdminActionButton("refresh", disabled=not bound))
        self.add_item(_CalendarAdminActionButton("unbind", disabled=not bound))

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
    ) -> None:
        super().__init__(label=label, custom_id=custom_id, style=style)
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        manager = getattr(self.view, "manager", None)
        if isinstance(manager, CalendarManager):
            await manager.handle_board_action(interaction, self.action)


class CalendarBoardView(discord.ui.LayoutView):
    def __init__(self, manager: CalendarManager, text: str) -> None:
        super().__init__(timeout=None)
        self.manager = manager
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(),
                discord.ui.ActionRow(
                    _CalendarBoardButton(
                        "create",
                        "新增活動",
                        BOARD_CREATE_CUSTOM_ID,
                        style=discord.ButtonStyle.primary,
                    ),
                    _CalendarBoardButton("edit", "編輯活動", BOARD_EDIT_CUSTOM_ID),
                    _CalendarBoardButton("browse", "瀏覽活動", BOARD_BROWSE_CUSTOM_ID),
                    _CalendarBoardButton("refresh", "重新整理", BOARD_REFRESH_CUSTOM_ID),
                ),
            )
        )


class CalendarEventModal(discord.ui.Modal):
    def __init__(
        self,
        manager: CalendarManager,
        event: discord.ScheduledEvent | None = None,
    ) -> None:
        event_input = event_to_input(event) if event is not None else None
        super().__init__(
            title="編輯活動" if event is not None else "新增活動",
            timeout=5 * 60,
        )
        self.manager = manager
        self.event_id = event.id if event is not None else None
        self.name_input = discord.ui.TextInput(
            label="活動名稱",
            min_length=1,
            max_length=100,
            default=event_input.name if event_input is not None else None,
        )
        self.start_input = discord.ui.TextInput(
            label="開始時間（YYYY-MM-DD HH:MM，UTC+8）",
            min_length=16,
            max_length=16,
            default=(
                event_input.start_time.astimezone(CALENDAR_TZ).strftime("%Y-%m-%d %H:%M")
                if event_input is not None else None
            ),
        )
        self.duration_input = discord.ui.TextInput(
            label="活動長度（分鐘）",
            min_length=1,
            max_length=5,
            default=str(event_input.duration_minutes) if event_input is not None else "60",
        )
        self.location_input = discord.ui.TextInput(
            label="地點",
            min_length=1,
            max_length=100,
            default=event_input.location if event_input is not None else "Discord",
        )
        self.description_input = discord.ui.TextInput(
            label="說明（選填）",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            default=event_input.description if event_input is not None else None,
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
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.guild is None:
            await interaction.followup.send("行事曆只能在伺服器中使用。", ephemeral=True)
            return
        try:
            duration = int(str(self.duration_input.value).strip())
        except ValueError:
            await interaction.followup.send("活動長度必須是整數分鐘。", ephemeral=True)
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
        except CalendarUserError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"已{action}活動：{event.url}",
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
            view.manager._assert_user_can_manage(interaction.user)
            event = next((item for item in view.events if item.id == event_id), None)
            if event is None or not _is_external_scheduled(event):
                raise CalendarUserError("這個活動已失效，請重新選擇。")
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
        view.page += self.direction
        view.render()
        if isinstance(view, CalendarBrowseView):
            await interaction.response.edit_message(content=view.page_text(), view=view)
        else:
            await interaction.response.edit_message(view=view)


class CalendarEditPickerView(discord.ui.View):
    def __init__(
        self,
        manager: CalendarManager,
        user_id: int,
        guild_id: int,
        events: list[discord.ScheduledEvent],
        *,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=5 * 60)
        self.manager = manager
        self.user_id = user_id
        self.guild_id = guild_id
        self.events = tuple(events)
        self.page = page
        self.render()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.user_id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message("只有原操作使用者可以使用這個選單。", ephemeral=True)
        return False

    def render(self) -> None:
        self.clear_items()
        page_count = max(1, (len(self.events) + EVENTS_PER_PAGE - 1) // EVENTS_PER_PAGE)
        self.page = min(max(self.page, 0), page_count - 1)
        start = self.page * EVENTS_PER_PAGE
        page_events = self.events[start : start + EVENTS_PER_PAGE]
        options = []
        for event in page_events:
            local = _event_local_time(event)
            description = local.strftime("%Y-%m-%d %H:%M") if local else "時間未知"
            options.append(
                discord.SelectOption(
                    label=_safe_event_name(event)[:100],
                    description=description[:100],
                    value=str(event.id),
                )
            )
        self.add_item(_EditSelect(placeholder="選擇活動", options=options))
        if page_count > 1:
            self.add_item(_CalendarPageButton(-1, disabled=self.page <= 0))
            self.add_item(_CalendarPageButton(1, disabled=self.page >= page_count - 1))


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
        self.render()

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.user_id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message("只有原操作使用者可以使用這個選單。", ephemeral=True)
        return False

    def render(self) -> None:
        self.clear_items()
        page_count = max(
            1,
            (len(self.events) + BROWSE_EVENTS_PER_PAGE - 1) // BROWSE_EVENTS_PER_PAGE,
        )
        self.page = min(max(self.page, 0), page_count - 1)
        if page_count > 1:
            self.add_item(_CalendarPageButton(-1, disabled=self.page <= 0))
            self.add_item(_CalendarPageButton(1, disabled=self.page >= page_count - 1))

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
            line = f"**{_safe_event_name(event)}** · {discord.utils.format_dt(start, style='F')}"
            url = _event_url(event)
            if url:
                line += f" · [開啟]({url})"
            lines.append(line)
        return "\n".join(lines)
