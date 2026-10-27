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
    ends_at: int | None = None

    @property
    def store_url(self) -> str:
        return f"https://store.steampowered.com/app/{self.app_id}/"


def offer_from_payload(fields: dict) -> SteamOffer:
    return SteamOffer(**{**fields, "developers": tuple(fields["developers"])})


@dataclass(frozen=True, slots=True)
class SteamFetchResult:
    active_app_ids: frozenset[int]
    offers: tuple[SteamOffer, ...]
    failed_app_count: int = 0


class SteamConfigurationError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SteamStatus:
    poll_interval_seconds: float
    channel_id: int | None
    active_app_count: int
    role_ids: tuple[int, ...] = ()
    running: bool = False
    last_success: int | None = None
    last_error: str | None = None
    pending_count: int = 0
