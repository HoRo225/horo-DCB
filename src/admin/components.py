from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from src.ai.access import MAX_CODEX_ALLOWED_CHANNELS

if TYPE_CHECKING:
    pass


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
        from src.admin.panel import AdminPanelView

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
        from src.admin.panel import AdminPanelView

        view = self.view
        if isinstance(view, AdminPanelView):
            page = self.values[0]
            action = "noop" if page == view.page and view._layout_is_published() else page
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
        from src.admin.panel import AdminPanelView

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
        from src.admin.panel import AdminPanelView

        view = self.view
        if isinstance(view, AdminPanelView):
            await view.handle_codex_role_select(interaction, tuple(self.values))


class _SteamRoleSelect(discord.ui.RoleSelect):
    def __init__(self, *, disabled: bool, configured: bool) -> None:
        super().__init__(
            placeholder=("重新選擇 Steam 通知身分組" if configured else "選擇 Steam 通知身分組"),
            min_values=1,
            max_values=25,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from src.admin.panel import AdminPanelView

        view = self.view
        if isinstance(view, AdminPanelView) and self.values:
            await view.handle_steam_role_select(interaction, tuple(self.values))


class _CalendarChannelSelect(discord.ui.ChannelSelect):
    def __init__(self) -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text],
            placeholder="選擇新的行事曆頻道",
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from src.admin.panel import AdminPanelView

        view = self.view
        if isinstance(view, AdminPanelView):
            channel = self.values[0].resolve() if self.values else None
            await view.handle_calendar_channel_select(interaction, channel)
