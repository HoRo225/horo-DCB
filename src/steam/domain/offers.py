from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

ANNOUNCE = "steam.announce"
OFFER_ENDED = "offer-ended"


@dataclass(frozen=True, slots=True)
class SteamOffer:
    app_id: int
    name: str
    old_price: str
    description: str
    header_image: str | None
    ends_at: int | None = None

    @property
    def store_url(self) -> str:
        return f"https://store.steampowered.com/app/{self.app_id}/"


@dataclass(frozen=True, slots=True)
class Announcement:
    offer: SteamOffer
    role_ids: tuple[int, ...]

    def payload(self) -> dict[str, object]:
        return {"offer": asdict(self.offer), "role_ids": list(self.role_ids)}

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> Announcement:
        return cls(SteamOffer(**payload["offer"]), tuple(payload["role_ids"]))


def announced_app(payload: Mapping[str, Any]) -> int:
    return payload["offer"]["app_id"]


@dataclass(frozen=True, slots=True)
class PostedCard:
    channel_id: int
    message_id: int
    offer: SteamOffer

    def ended(self, active_app_ids: frozenset[int], now: float) -> bool:
        return self.offer.app_id not in active_app_ids or (self.offer.ends_at is not None and self.offer.ends_at <= now)


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
