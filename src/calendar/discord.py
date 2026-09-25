from __future__ import annotations

from collections.abc import Sequence

import discord

from src.calendar.manager import CalendarManager
from src.calendar.views import (
    BROWSE_EVENTS_PER_PAGE,
    CalendarAdminView,
    CalendarBoardView,
    CalendarBrowseView,
    CalendarEditPickerView,
    CalendarEventModal,
    render_board_text,
)
from src.calendar.models import CalendarUserError
from src.calendar.discord_models import is_external_scheduled


class CalendarController:
    def __init__(self, manager: CalendarManager) -> None:
        self.manager = manager

    def persistent_board_view(self) -> CalendarBoardView:
        return CalendarBoardView(self, "行事曆", can_edit=False)

    def admin_view(self, *, user_id: int, guild_id: int) -> CalendarAdminView:
        return CalendarAdminView(self.manager, user_id=user_id, guild_id=guild_id)

    def build_board_view(
        self,
        guild_name: str,
        events: Sequence[discord.ScheduledEvent],
    ) -> CalendarBoardView:
        return CalendarBoardView(
            self,
            render_board_text(guild_name, list(events)),
            can_edit=any(is_external_scheduled(event) for event in events),
        )

    def board_interaction_is_current(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None or interaction.message is None:
            return False
        binding = self.manager.get_binding(interaction.guild_id)
        return bool(
            binding is not None
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
        self, interaction: discord.Interaction, action: str,
    ) -> None:
        if not self.board_interaction_is_current(interaction):
            await self._reply_ephemeral(
                interaction, "這個行事曆看板已失效，請使用目前綁定的看板。",
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
                    interaction, "目前沒有可由 Horo 編輯的 External 活動。",
                )
                return
            await interaction.response.send_message(
                "選擇要編輯的活動：",
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
            view = CalendarBrowseView(interaction.user.id, interaction.guild.id, events)
            if len(events) <= BROWSE_EVENTS_PER_PAGE:
                await self._reply_ephemeral(interaction, view.page_text())
                return
            await interaction.response.send_message(
                view.page_text(),
                view=view,
                ephemeral=True,
            )
            return
        if action == "refresh":
            view = self.build_board_view(
                interaction.guild.name,
                self.manager.cached_events(interaction.guild),
            )
            await interaction.response.edit_message(view=view)
