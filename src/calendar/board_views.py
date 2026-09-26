from __future__ import annotations

import calendar as month_calendar
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING

import discord

from src.brand import BRAND_COLOUR, banner
from src.calendar.discord_models import (
    event_local_time,
    event_location,
    event_url,
    safe_event_name,
)
from src.calendar.models import (
    CALENDAR_TZ,
    calendar_now,
)

if TYPE_CHECKING:
    from src.calendar.discord import CalendarController


MAX_UPCOMING_SHOWN = 3


BOARD_CREATE_CUSTOM_ID = "horo:calendar:create"


BOARD_EDIT_CUSTOM_ID = "horo:calendar:edit"


BOARD_BROWSE_CUSTOM_ID = "horo:calendar:browse"


BOARD_REFRESH_CUSTOM_ID = "horo:calendar:refresh"


WEEKDAY_LABELS = "日一二三四五六"


def event_lines(event: discord.ScheduledEvent) -> str | None:
    start = event_local_time(event)
    if start is None:
        return None
    details = [
        discord.utils.format_dt(start, style="F"),
        discord.utils.format_dt(start, style="R"),
        event_location(event),
    ]
    if url := event_url(event):
        details.append(f"[開啟活動]({url})")
    return f"**{safe_event_name(event)}**\n-# {' · '.join(details)}"


def render_board_heading(guild_name: str, *, now: datetime | None = None) -> str:
    current = (now or calendar_now()).astimezone(CALENDAR_TZ)
    safe_guild = discord.utils.escape_markdown(guild_name)[:100]
    weekday = WEEKDAY_LABELS[(current.weekday() + 1) % 7]
    return (
        f"# 📅 {safe_guild} 行事曆\n"
        f"-# 今天 {current.month}/{current.day}（{weekday}） · 時間以 UTC+8 解讀"
    )


def render_month_text(
    events: list[discord.ScheduledEvent] | tuple[discord.ScheduledEvent, ...],
    *,
    now: datetime | None = None,
) -> str:
    current = (now or calendar_now()).astimezone(CALENDAR_TZ)
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
    calendar_lines = ["".join(f"{label:^5}" for label in WEEKDAY_LABELS).rstrip()]
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
    return "\n".join(
        (
            f"## {current.year} 年 {current.month} 月",
            "```text",
            *calendar_lines,
            "```",
            "-# [日期] 代表今天 · • 代表活動",
        )
    )


def render_upcoming_text(
    events: list[discord.ScheduledEvent] | tuple[discord.ScheduledEvent, ...],
) -> str:
    lines = ["## 即將到來"]
    upcoming = list(events[:MAX_UPCOMING_SHOWN])
    if not upcoming:
        lines.append(
            "目前沒有即將到來的活動。\n-# 有「管理活動」權限的成員可從下方新增第一個活動。"
        )
    lines.extend(text for event in upcoming if (text := event_lines(event)) is not None)
    extra = len(events) - len(upcoming)
    if extra > 0:
        lines.append(f"-# 另有 {extra} 個活動，按「瀏覽活動」查看。")
    return "\n".join(lines)


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
            label=label,
            custom_id=custom_id,
            style=style,
            disabled=disabled,
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, CalendarBoardView):
            await view.controller.handle_board_action(interaction, self.action)


class CalendarBoardView(discord.ui.LayoutView):
    def __init__(
        self,
        controller: CalendarController,
        body: Sequence[discord.ui.Item] = (),
        *,
        can_edit: bool = True,
    ) -> None:
        super().__init__(timeout=None)
        self.controller = controller
        children: list[discord.ui.Item] = []
        if (brand_banner := banner()) is not None:
            children.append(brand_banner)
        children.extend(
            (
                *body,
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
                        "edit",
                        "編輯活動",
                        BOARD_EDIT_CUSTOM_ID,
                        disabled=not can_edit,
                    ),
                    _CalendarBoardButton("browse", "瀏覽活動", BOARD_BROWSE_CUSTOM_ID),
                    _CalendarBoardButton("refresh", "重新整理", BOARD_REFRESH_CUSTOM_ID),
                ),
            )
        )
        self.add_item(discord.ui.Container(*children, accent_colour=BRAND_COLOUR))
