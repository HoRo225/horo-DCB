from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import discord

from src.admin.adapters.discord.sessions import PanelSessionRegistry
from src.ai import AiAccess, AiSettings, RuntimeStatus
from src.core.adapters.discord.effects import EffectResolver
from src.core.adapters.discord.live import LiveGuild
from src.games import Lobby
from src.steam import Steam, SteamOffer
from src.voice import Voice


class AiRuntime(Protocol):
    async def get_runtime_status(self) -> RuntimeStatus: ...


@dataclass(frozen=True, slots=True)
class AdminServices:
    live: LiveGuild
    panels: PanelSessionRegistry
    model: AiRuntime
    ai: AiSettings
    access: AiAccess
    voice: Voice
    steam: Steam
    games: Lobby
    effects: EffectResolver
    ai_error: Callable[[str, str], str]
    hub_link: Callable[[], list[discord.ui.Button]]
    offer_summary: Callable[[SteamOffer], discord.ui.Item]
    last_backup: Callable[[], int | None]
