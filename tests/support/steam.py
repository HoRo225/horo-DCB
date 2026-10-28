from __future__ import annotations

import asyncio
import dataclasses
import itertools
from collections.abc import Awaitable, Callable, Mapping, Sequence

from src.core.domain.records import DiscordEffect
from src.steam.app.ports import DeliveryError, RoleState
from src.steam.domain.offers import ANNOUNCE, OFFER_ENDED, Announcement, SteamFetchResult, SteamOffer, announced_app

MISSING_CHANNEL = "找不到已綁定的通知頻道，請在控制台重新選擇。"
GONE_CHANNEL = "指定通知頻道已不存在，未綁定頻道。"


def offer(app_id: int, **fields: object) -> SteamOffer:
    return SteamOffer(**{"app_id": app_id, "name": f"game{app_id}", "old_price": "NT$100", "description": "desc",
                         "developers": ("dev",), "header_image": None} | fields)


def fetched(*offers: SteamOffer, failed: int = 0) -> SteamFetchResult:
    return SteamFetchResult(frozenset(item.app_id for item in offers), offers, failed_app_count=failed)


def announcement_effect(effect_id: str, app_id: int, *, status: str = "pending", channel_id: int = 10,
                        role_ids: tuple[int, ...] = (), error: str | None = None, nonce: str | None = "nonce",
                        message_id: int | None = None) -> DiscordEffect:
    payload = Announcement(offer(app_id), role_ids).payload()
    return DiscordEffect(effect_id, None, ANNOUNCE, status, channel_id, message_id, nonce, payload, error, 0, 0)


class FakeStore:
    def __init__(self, *, enabled: bool = True, channel_id: int | None = 10) -> None:
        self.settings: dict[str, object] = {"enabled": enabled, "channel_id": channel_id, "role_ids": []}
        self.receipts: dict[str, tuple[str, object]] = {}
        self.effects: dict[str, DiscordEffect] = {}
        self.posted_ids: set[int] = set()
        self.failure: Exception | None = None

    def _write(self) -> None:
        if self.failure is not None:
            raise self.failure

    def _save(self, key: str, value: object, operation_id: str | None) -> bool:
        self._write()
        if operation_id is not None:
            if (prior := self.receipts.get(operation_id)) is not None:
                if prior != (key, value):
                    raise ValueError("operation replay inputs differ")
                return False
            self.receipts[operation_id] = (key, value)
        self.settings[key] = value
        return True

    def add(self, *effects: DiscordEffect) -> None:
        self.effects.update((effect.effect_id, effect) for effect in effects)

    def _set(self, effect_id: str, **changes: object) -> None:
        self.effects[effect_id] = dataclasses.replace(self.effects[effect_id], **changes)

    def enabled(self) -> bool:
        return self.settings["enabled"]

    def set_enabled(self, value: bool, *, operation_id: str | None) -> bool:
        return self._save("enabled", value, operation_id)

    def channel_id(self) -> int | None:
        return self.settings["channel_id"]

    def bind_channel(self, channel_id: int, *, operation_id: str | None) -> bool:
        return self._save("channel_id", channel_id, operation_id)

    def role_ids(self) -> list[int]:
        return list(self.settings["role_ids"])

    def set_roles(self, role_ids: list[int], *, operation_id: str | None) -> bool:
        return self._save("role_ids", role_ids, operation_id)

    def keep_roles(self, role_ids: list[int]) -> None:
        self.settings["role_ids"] = role_ids

    def posted(self) -> set[int]:
        return set(self.posted_ids)

    def announcements(self, *statuses: str) -> list[DiscordEffect]:
        return [effect for effect in self.effects.values() if effect.status in statuses]

    def announcement(self, effect_id: str) -> DiscordEffect | None:
        return self.effects.get(effect_id)

    def retire(self, active_app_ids: frozenset[int]) -> None:
        self._write()
        self.posted_ids &= active_app_ids
        for effect in self.announcements("pending", "failed", "skipped"):
            ended = effect.status == "skipped" and effect.error == OFFER_ENDED
            if not ended and announced_app(effect.payload) not in active_app_ids:
                self._set(effect.effect_id, status="skipped", error=OFFER_ENDED)

    def queue(self, effect_id: str, channel_id: int, payload: Mapping[str, object], nonce: str) -> None:
        self.effects[effect_id] = DiscordEffect(effect_id, None, ANNOUNCE, "pending", channel_id, None, nonce,
                                                dict(payload), None, 0, 0)

    def begin_delivery(self, effect: DiscordEffect) -> None:
        self._set(effect.effect_id, status="unknown")

    def fail_delivery(self, effect_id: str, status: str, error: str) -> None:
        self._set(effect_id, status=status, error=error)

    def finish_delivery(self, effect: DiscordEffect, message_id: int | None) -> None:
        self._write()
        self._set(effect.effect_id, status="done", message_id=message_id)
        self.settle(effect)

    def settle(self, effect: DiscordEffect) -> None:
        self.posted_ids.add(announced_app(effect.payload))


class FakeSource:
    def __init__(self, result: SteamFetchResult | None = None) -> None:
        self.result = result if result is not None else fetched()
        self.tracked: list[frozenset[int] | None] = []
        self.during: Callable[[], object] | None = None
        self.closed = False

    async def fetch_current_offers(self, *, tracked_app_ids: frozenset[int] | None = None) -> SteamFetchResult | None:
        self.tracked.append(tracked_app_ids)
        if self.during is not None:
            self.during()
        return self.result

    async def close(self) -> None:
        self.closed = True


class FakeGuild:
    def __init__(self, *channel_ids: int) -> None:
        self.channels: dict[int, str | None] = dict.fromkeys(channel_ids or (10,))
        self.roles: dict[int, RoleState] = {}
        self.sent: list[tuple[int, int, tuple[int, ...], str | None]] = []
        self.failure: DeliveryError | None = None
        self.during: Callable[[], Awaitable[object] | None] | None = None
        self._ids = itertools.count(700)

    def channel_problem(self, channel_id: int | None) -> str | None:
        return self.channels[channel_id] if channel_id in self.channels else MISSING_CHANNEL

    def bind_problem(self, channel_id: int) -> str | None:
        return self.channels[channel_id] if channel_id in self.channels else GONE_CHANNEL

    def role(self, role_id: int, channel_id: int | None) -> RoleState:
        return self.roles.get(role_id, "missing")

    async def announce(self, channel_id: int, offer: SteamOffer, role_ids: Sequence[int], nonce: str | None) -> int | None:
        self.sent.append((channel_id, offer.app_id, tuple(role_ids), nonce))
        if self.during is not None and (pending := self.during()) is not None:
            await pending
        if self.failure is not None:
            raise self.failure
        return next(self._ids)


class FakeAnnouncer:
    def __init__(self, guild: FakeGuild | None) -> None:
        self.current = guild
        self.opened = asyncio.Event()
        self.opened.set()

    async def ready(self) -> None:
        await self.opened.wait()

    def guild(self) -> FakeGuild | None:
        return self.current
