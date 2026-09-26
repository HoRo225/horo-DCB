from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord

from src.brand import BRAND_COLOUR
from src.calendar.discord_models import (
    event_local_time,
    is_external_scheduled,
    safe_event_name,
)
from src.calendar.models import (
    CalendarUserError,
)

if TYPE_CHECKING:
    from src.calendar.manager import CalendarManager


from src.calendar.board_views import event_lines
from src.calendar.event_views import CalendarEventModal

EVENTS_PER_PAGE = 25


BROWSE_EVENTS_PER_PAGE = 8


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
        if not isinstance(view, _CalendarPagedView):
            return
        await interaction.response.defer()
        async with view._publish_lock:
            old_page = view.page
            view.page += self.direction
            try:
                view.render()
                await interaction.edit_original_response(view=view)
            except Exception, asyncio.CancelledError:
                view.page = old_page
                view.render()
                raise


class _CalendarPagedView(discord.ui.LayoutView):
    def __init__(
        self,
        user_id: int,
        guild_id: int,
        events: list[discord.ScheduledEvent],
        page_size: int,
    ) -> None:
        super().__init__(timeout=5 * 60)
        self.user_id = user_id
        self.guild_id = guild_id
        self.events = tuple(events)
        self.page = 0
        self._page_size = page_size
        self._page_count = max(1, (len(self.events) + page_size - 1) // page_size)
        self._publish_lock = asyncio.Lock()
        self._previous_page_button = _CalendarPageButton(-1, disabled=True)
        self._next_page_button = _CalendarPageButton(1, disabled=False)

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.user.id == self.user_id and interaction.guild_id == self.guild_id:
            return True
        await interaction.response.send_message(
            "只有原操作使用者可以使用這個選單。", ephemeral=True
        )
        return False

    def _body(self) -> list[discord.ui.Item]:
        raise NotImplementedError

    def render(self) -> None:
        self.page = min(max(self.page, 0), self._page_count - 1)
        children = self._body()
        if self._page_count > 1:
            self._previous_page_button.disabled = self.page <= 0
            self._next_page_button.disabled = self.page >= self._page_count - 1
            children.extend(
                (
                    discord.ui.Separator(),
                    discord.ui.TextDisplay(f"-# 第 {self.page + 1} / {self._page_count} 頁"),
                    discord.ui.ActionRow(self._previous_page_button, self._next_page_button),
                )
            )
        self.clear_items()
        self.add_item(discord.ui.Container(*children, accent_colour=BRAND_COLOUR))

    def _page_events(self) -> tuple[discord.ScheduledEvent, ...]:
        start = self.page * self._page_size
        return self.events[start : start + self._page_size]


class CalendarEditPickerView(_CalendarPagedView):
    def __init__(
        self,
        manager: CalendarManager,
        user_id: int,
        guild_id: int,
        events: list[discord.ScheduledEvent],
    ) -> None:
        super().__init__(user_id, guild_id, events, EVENTS_PER_PAGE)
        self.manager = manager
        self._edit_select = _EditSelect(placeholder="選擇活動", options=[])
        self.render()

    def _body(self) -> list[discord.ui.Item]:
        options = []
        for event in self._page_events():
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
        return [
            discord.ui.TextDisplay("## 編輯活動\n-# 選擇要編輯的 External 活動。"),
            discord.ui.ActionRow(self._edit_select),
        ]


class CalendarBrowseView(_CalendarPagedView):
    def __init__(
        self,
        user_id: int,
        guild_id: int,
        events: list[discord.ScheduledEvent],
    ) -> None:
        super().__init__(user_id, guild_id, events, BROWSE_EVENTS_PER_PAGE)
        self.render()

    def _body(self) -> list[discord.ui.Item]:
        lines = ["## 活動列表"]
        lines.extend(
            text for event in self._page_events() if (text := event_lines(event)) is not None
        )
        if not self.events:
            lines.append("目前沒有即將到來的活動。")
        return [discord.ui.TextDisplay("\n".join(lines))]
