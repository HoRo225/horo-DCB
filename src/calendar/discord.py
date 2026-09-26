from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO

import discord

from src.brand import BANNER_FILENAME, brand_files
from src.calendar.discord_models import is_external_scheduled
from src.calendar.image import MONTH_IMAGE_FILENAME, render_month_png
from src.calendar.manager import CalendarManager
from src.calendar.models import CalendarUserError, calendar_now
from src.calendar.views import (
    CalendarBoardView,
    CalendarBrowseView,
    CalendarEditPickerView,
    CalendarEventModal,
    render_board_heading,
    render_month_text,
    render_upcoming_text,
)


class CalendarController:
    def __init__(self, manager: CalendarManager) -> None:
        self.manager = manager

    def persistent_board_view(self) -> CalendarBoardView:
        return CalendarBoardView(self, can_edit=False)

    def build_board(
        self,
        guild_name: str,
        events: Sequence[discord.ScheduledEvent],
    ) -> tuple[CalendarBoardView, list[discord.File]]:
        now = calendar_now()
        events = list(events)
        files = brand_files(BANNER_FILENAME)
        # ponytail: renders on the event loop (~tens of ms); move to asyncio.to_thread if many boards refresh at once.
        month_image = render_month_png(events, now=now)
        if month_image is None:
            month: discord.ui.Item = discord.ui.TextDisplay(render_month_text(events, now=now))
        else:
            files.append(discord.File(BytesIO(month_image), filename=MONTH_IMAGE_FILENAME))
            month = discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    f"attachment://{MONTH_IMAGE_FILENAME}",
                    description="本月行事曆：今天以綠色填滿，有活動的日期加上外框",
                )
            )
        view = CalendarBoardView(
            self,
            (
                discord.ui.TextDisplay(render_board_heading(guild_name, now=now)),
                month,
                discord.ui.TextDisplay(render_upcoming_text(events)),
            ),
            can_edit=any(is_external_scheduled(event) for event in events),
        )
        return view, files

    def board_interaction_is_current(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or interaction.message is None:
            return False
        binding = self.manager.get_binding(interaction.guild.id)
        return bool(
            binding is not None
            and self.manager.binding_channel_is_valid(interaction.guild)
            and interaction.channel_id == binding.channel_id
            and interaction.message.id == binding.message_id
        )

    @staticmethod
    async def _reply_ephemeral(interaction: discord.Interaction, text: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(
                text,
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                text,
                ephemeral=True,
            )

    async def handle_board_action(
        self,
        interaction: discord.Interaction,
        action: str,
    ) -> None:
        try:
            self.manager._assert_accepting_work()
        except CalendarUserError as exc:
            await self._reply_ephemeral(interaction, str(exc))
            return
        if not self.board_interaction_is_current(interaction):
            await self._reply_ephemeral(
                interaction,
                "這個行事曆看板已失效，請使用目前綁定的看板。",
            )
            return
        if interaction.guild is None:
            await self._reply_ephemeral(interaction, "行事曆只能在伺服器中使用。")
            return
        if action in {"create", "edit"}:
            try:
                self.manager.assert_user_can_manage(interaction.user)
            except CalendarUserError as exc:
                await self._reply_ephemeral(interaction, str(exc))
                return
        if action == "create":
            await interaction.response.send_modal(CalendarEventModal(self.manager))
            return
        if action == "edit":
            events = self.manager.get_editable_events(interaction.guild)
            if not events:
                await self._reply_ephemeral(
                    interaction,
                    "目前沒有可由 Horo 編輯的 External 活動。",
                )
                return
            await interaction.response.send_message(
                view=CalendarEditPickerView(
                    self.manager,
                    interaction.user.id,
                    interaction.guild.id,
                    events,
                ),
                ephemeral=True,
            )
            return
        if action == "browse":
            events = self.manager.cached_events(interaction.guild)
            if not events:
                await self._reply_ephemeral(interaction, "目前沒有即將到來的活動。")
                return
            await interaction.response.send_message(
                view=CalendarBrowseView(interaction.user.id, interaction.guild.id, events),
                ephemeral=True,
            )
            return
        if action == "refresh":
            view, files = self.build_board(
                interaction.guild.name,
                self.manager.cached_events(interaction.guild),
            )
            await interaction.response.edit_message(attachments=files, view=view)
