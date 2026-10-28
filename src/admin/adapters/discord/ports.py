from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import discord

from src.admin.adapters.discord.sessions import PanelSessionRegistry
from src.ai import AiAccess, AiSettings, DeviceLogin, ModelInfo, RateLimits, RuntimeStatus
from src.core.adapters.discord.effects import EffectResolver
from src.core.adapters.discord.live import LiveGuild
from src.games import Lobby
from src.steam import Steam, SteamOffer
from src.voice import Voice


class CodexAccount(Protocol):
    async def get_runtime_status(self) -> RuntimeStatus: ...
    async def get_rate_limits(self) -> RateLimits: ...
    async def get_models(self) -> tuple[ModelInfo, ...]: ...
    def request_counts(self) -> tuple[int, int]: ...
    async def login_start(self) -> DeviceLogin: ...
    async def wait_login(self, login_id: str, *, timeout: float = ...) -> bool: ...
    async def login_cancel(self, login_id: str) -> None: ...
    async def logout(self) -> None: ...


@dataclass(frozen=True, slots=True)
class AdminServices:
    live: LiveGuild
    panels: PanelSessionRegistry
    codex: CodexAccount
    ai: AiSettings
    access: AiAccess
    voice: Voice
    steam: Steam
    games: Lobby
    effects: EffectResolver
    codex_error: Callable[[str, str], str]
    hub_link: Callable[[], list[discord.ui.Button]]
    offer_summary: Callable[[SteamOffer], discord.ui.Item]
