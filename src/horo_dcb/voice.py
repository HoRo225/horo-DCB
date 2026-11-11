import asyncio
from enum import Enum, auto
from typing import TYPE_CHECKING

import discord

from . import log_event

if TYPE_CHECKING:
    from .bot import HoroBot


BOT_GRANTS = {"view_channel": True, "connect": True, "manage_channels": True, "move_members": True}
OWNER_GRANTS = {"manage_channels": True, "move_members": True}


class Verdict(Enum):
    FORGET = auto()
    WAIT = auto()
    DELETE = auto()
    TRANSFER = auto()
    KEEP = auto()


def judge(room: discord.VoiceChannel | None, owner_id: int, waiting: set[int]) -> Verdict:
    if room is None:
        return Verdict.FORGET
    if owner_id in waiting:
        return Verdict.WAIT
    humans = {person.id for person in room.members if not person.bot}
    if not humans:
        return Verdict.DELETE
    return Verdict.KEEP if owner_id in humans else Verdict.TRANSFER


def revoke(overwrites: dict, target_id: int) -> dict:
    return {target: overwrite for target, overwrite in overwrites.items() if target.id != target_id}


def grant(overwrites: dict, target: discord.Member, grants: dict[str, bool]) -> dict:
    current = next((overwrite for key, overwrite in overwrites.items() if key.id == target.id), discord.PermissionOverwrite())
    merged = discord.PermissionOverwrite.from_pair(*current.pair())
    merged.update(**grants)
    return revoke(overwrites, target.id) | {target: merged}


async def attempt(code: str, request) -> bool:
    try:
        await request
    except Exception as error:
        log_event(code, error)
        return False
    return True


class Voice:
    def __init__(self, bot: HoroBot) -> None:
        self.bot = bot
        self.lock = asyncio.Lock()

    async def settle(self, guild: discord.Guild) -> None:
        async with self.lock:
            settings = await self.bot.store.settings()
            hubs = [channel for channel in map(guild.get_channel, sorted(settings.voice_hub_ids)) if isinstance(channel, discord.VoiceChannel)]
            waiting = [(hub, person) for hub in hubs for person in hub.members if not person.bot]
            for hub, person in waiting:
                await self._seat(guild, hub, person)
            waiting_ids = {person.id for _, person in waiting}
            for room_id, owner_id in self.bot.store.rooms().items():
                channel = self.bot.get_channel(room_id)
                room = channel if isinstance(channel, discord.VoiceChannel) else None
                if room is None or room.guild.id == guild.id:
                    await self._tend(room_id, room, owner_id, waiting_ids)

    async def _seat(self, guild: discord.Guild, hub: discord.VoiceChannel, person: discord.Member) -> None:
        owned = (guild.get_channel(room_id) for room_id, owner_id in self.bot.store.rooms().items() if owner_id == person.id)
        room = next((channel for channel in owned if isinstance(channel, discord.VoiceChannel)), None)
        if room is None:
            overwrites = grant(grant(hub.overwrites, guild.me, BOT_GRANTS), person, OWNER_GRANTS)
            try:
                room = await guild.create_voice_channel(f"▍{person.display_name}的房間"[:100], category=hub.category, bitrate=hub.bitrate, user_limit=0, overwrites=overwrites)
            except Exception as error:
                log_event("voice_create_error", error)
                return
            await self.bot.store.set_room(room.id, person.id)
        await attempt("voice_move_error", person.move_to(room))

    async def _tend(self, room_id: int, room: discord.VoiceChannel | None, owner_id: int, waiting: set[int]) -> None:
        match judge(room, owner_id, waiting):
            case Verdict.FORGET:
                await self.bot.store.remove_room(room_id)
            case Verdict.DELETE:
                if await attempt("voice_delete_error", room.delete()):
                    await self.bot.store.remove_room(room_id)
            case Verdict.TRANSFER:
                heir = next(person for person in room.members if not person.bot)
                if await attempt("voice_transfer_error", room.edit(overwrites=grant(revoke(room.overwrites, owner_id), heir, OWNER_GRANTS))):
                    await self.bot.store.set_room(room_id, heir.id)
