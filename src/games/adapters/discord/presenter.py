from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

from src.core.adapters.discord.brand import ICONS, send_notice
from src.core.adapters.discord.channels import post_problem
from src.core.adapters.discord.live import LiveGuild
from src.games.adapters.discord.access import hub_link
from src.games.adapters.discord.art import banner_file, banner_png, render
from src.games.adapters.discord.format import slot_symbol
from src.games.adapters.discord.tables import SeatView, seat_view
from src.games.adapters.discord.views import lobby_view
from src.games.app.lobby import Lobby
from src.games.app.ports import PresentError, TableGone
from src.games.app.tables import LiveTable, Posted


async def draw_banner() -> discord.File | None:
    try:
        await render(banner_png)
    except Exception:
        logging.exception("Game hub banner could not be drawn.")
        return None
    return banner_file()


class DiscordTables:
    def __init__(
        self, client: discord.Client, live: LiveGuild, lobby: Lobby,
        banner: Callable[[], Awaitable[discord.File | None]] = draw_banner,
    ) -> None:
        self._client = client
        self._live = live
        self._lobby = lobby
        self._banner = banner

    def symbol(self, name: str) -> str:
        return slot_symbol(name)

    def hub_link(self) -> list[discord.ui.Button]:
        return hub_link(self._lobby.hub_message, self._live.guild_id)

    async def lobby_message(self, interaction: discord.Interaction) -> dict[str, object]:
        try:
            attachments = (await interaction.message.fetch()).attachments
        except discord.HTTPException:
            attachments = []
        if attachments:
            return {"view": lobby_view(), "attachments": attachments}
        if interaction.app_permissions.attach_files and (file := await self._banner()):
            return {"view": lobby_view(), "attachments": [file]}
        return {"view": lobby_view(banner=False), "attachments": []}

    def view(self, table: LiveTable) -> SeatView:
        return seat_view(table)

    def reply(self, interaction: discord.Interaction) -> InteractionReply:
        return InteractionReply(interaction, self)

    def _message(self, posted: Posted) -> discord.PartialMessage:
        return self._client.get_partial_messageable(posted.channel_id).get_partial_message(posted.message_id)

    def url(self, channel_id: int, message_id: int) -> str | None:
        url = self._message(Posted(channel_id, message_id)).jump_url
        return url if isinstance(url, str) else None

    async def post(self, channel_id: int, table: LiveTable) -> Posted:
        channel = self._live.channel(channel_id)
        if channel is None:
            raise PresentError("game channel is unavailable")
        try:
            message = await channel.send(view=self.view(table))
        except discord.HTTPException as error:
            raise PresentError(str(error)) from error
        url = getattr(message, "jump_url", None)
        return Posted(channel_id, message.id, url if isinstance(url, str) else None)

    async def edit(self, table: LiveTable) -> None:
        try:
            await self._message(table.posted).edit(view=self.view(table))
        except discord.NotFound as error:
            raise TableGone(str(error)) from error
        except discord.HTTPException as error:
            raise PresentError(str(error)) from error

    async def delete(self, channel_id: int, message_id: int) -> bool:
        try:
            await self._message(Posted(channel_id, message_id)).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logging.warning("Game message %s could not be deleted.", message_id)
            return False
        return True


class InteractionReply:
    def __init__(self, interaction: discord.Interaction, tables: DiscordTables) -> None:
        self._interaction = interaction
        self._tables = tables

    async def defer(self) -> None:
        if not self._interaction.response.is_done():
            await self._interaction.response.defer()

    async def notice(self, text: str, *, tone: str = "warn", link: bool = False) -> None:
        await send_notice(self._interaction, text, tone, items=self._tables.hub_link() if link else ())

    async def show(self, table: LiveTable) -> None:
        await self._tables.view(table).present(self._interaction)

    async def opened(self, text: str, url: str | None) -> None:
        items = [discord.ui.Button(label="前往牌桌", url=url, emoji=ICONS["manage"])] if url else []
        await send_notice(self._interaction, text, "ok", items=items)


class DiscordLobbyBoard:
    def __init__(
        self, client: discord.Client, live: LiveGuild,
        banner: Callable[[], Awaitable[discord.File | None]] = draw_banner,
    ) -> None:
        self._client = client
        self._live = live
        self._banner = banner

    def _channel(self, channel_id: int | None) -> discord.abc.GuildChannel | None:
        guild = self._live.guild()
        return guild.get_channel(channel_id) if guild is not None and channel_id is not None else None

    def has_channel(self, channel_id: int) -> bool:
        return self._channel(channel_id) is not None

    def channel_problem(self, channel_id: int | None) -> str | None:
        guild = self._live.guild()
        if guild is None:
            return None
        return post_problem(guild, self._channel(channel_id), "遊戲頻道")

    async def post(self, channel_id: int, still_current: Callable[[], bool]) -> int | None:
        guild = self._live.guild()
        channel = guild.get_channel(channel_id) if guild is not None else None
        if channel is None:
            raise PresentError("game channel is unavailable")
        try:
            message = await self._send(guild, channel, still_current)
        except discord.HTTPException as error:
            raise PresentError(str(error)) from error
        return None if message is None else message.id

    async def _send(
        self, guild: discord.Guild, channel: discord.TextChannel, still_current: Callable[[], bool],
    ) -> discord.Message | None:
        if channel.permissions_for(guild.me).attach_files and (file := await self._banner()):
            if not still_current():
                return None
            try:
                return await channel.send(view=lobby_view(), file=file)
            except discord.Forbidden:
                logging.warning("Game lobby banner was refused; posting without it.")
        if not still_current():
            return None
        return await channel.send(view=lobby_view(banner=False))

    async def withdraw(self, channel_id: int, message_id: int) -> bool:
        try:
            await self._client.get_partial_messageable(channel_id).get_partial_message(message_id).delete()
        except discord.HTTPException:
            return False
        return True

    async def clear(self, hub: list[int] | None) -> None:
        channel = self._channel(hub[0]) if hub else None
        if channel is None:
            return
        try:
            await channel.get_partial_message(hub[1]).delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logging.warning("Previous game hub message could not be deleted.")

    async def refresh(self, hub: list[int]) -> None:
        message = self._client.get_partial_messageable(hub[0]).get_partial_message(hub[1])
        try:
            if file := await self._banner():
                try:
                    await message.edit(view=lobby_view(), attachments=[file])
                    return
                except discord.Forbidden:
                    logging.warning("Game lobby banner was refused; refreshing without it.")
            await message.edit(view=lobby_view(banner=False), attachments=[])
        except discord.HTTPException:
            logging.warning("Published game lobby could not be refreshed.")
