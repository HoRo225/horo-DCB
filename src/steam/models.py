from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SteamOffer:
    app_id: int
    name: str
    old_price: str
    description: str
    developers: tuple[str, ...]
    header_image: str | None

    @property
    def store_url(self) -> str:
        return f"https://store.steampowered.com/app/{self.app_id}/"


@dataclass(frozen=True, slots=True)
class SteamFetchResult:
    active_app_ids: frozenset[int]
    offers: tuple[SteamOffer, ...]
    failed_app_count: int = 0


class SteamConfigurationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SteamGuildStatus:
    state_available: bool
    poll_interval_seconds: float
    channel_id: int | None
    active_app_count: int
    role_ids: tuple[int, ...] = ()
