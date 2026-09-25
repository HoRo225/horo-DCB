from __future__ import annotations

import asyncio
from pathlib import Path

from src.state import load_state_or_disable, read_json_state, write_json_atomic

DEFAULT_CODEX_ACCESS_STATE_PATH = Path("/app/data/codex_access.json")
MAX_CODEX_ALLOWED_CHANNELS = 25


def member_role_ids(member: object) -> frozenset[int]:
    return frozenset(
        role_id
        for role in getattr(member, "roles", ())
        if type(role_id := getattr(role, "id", None)) is int and role_id > 0
    )


def valid_allowlist_ids(values: object, *, container: type, minimum: int) -> bool:
    return (
        isinstance(values, container)
        and minimum <= len(values) <= MAX_CODEX_ALLOWED_CHANNELS
        and all(type(value) is int and value > 0 for value in values)
        and len(values) == len(set(values))
    )


class CodexAccess:
    def __init__(
        self,
        enabled: bool,
        guild_id: int | None,
    ) -> None:
        self.enabled = enabled
        self.guild_id = guild_id
        self.channel_ids: frozenset[int] = frozenset()
        self.role_ids: frozenset[int] = frozenset()
        self.state_available = True
        self.generation = 0
        self.mutation_lock = asyncio.Lock()
        self._suspended = False
        self._state_path = DEFAULT_CODEX_ACCESS_STATE_PATH
        (self.channel_ids, self.role_ids), self.state_available = load_state_or_disable(
            self._load_state,
            (frozenset(), frozenset()),
            "Codex 白名單狀態檔無法讀取；AI 對話已停用。",
        )

    def _load_state(self) -> tuple[frozenset[int], frozenset[int]]:
        payload = read_json_state(self._state_path, 3)
        if (
            type(payload.get("guild_id")) is not int
            or payload["guild_id"] <= 0
            or payload["guild_id"] != self.guild_id
            or set(payload) != {"version", "guild_id", "channel_ids", "role_ids"}
        ):
            raise ValueError("invalid Codex access state")
        channel_ids = payload.get("channel_ids")
        role_ids = payload.get("role_ids")
        if (
            not valid_allowlist_ids(channel_ids, container=list, minimum=1)
            or not valid_allowlist_ids(role_ids, container=list, minimum=0)
            or self.guild_id in role_ids
        ):
            raise ValueError("invalid Codex access state")
        return frozenset(channel_ids), frozenset(role_ids)

    def denial_reason(
        self,
        guild_id: int | None,
        channel_id: int | None,
        role_ids: frozenset[int] = frozenset(),
    ) -> str | None:
        if not self.enabled:
            return "disabled"
        if not self.state_available:
            return "state_unavailable"
        if self._suspended:
            return "suspended"
        if guild_id != self.guild_id:
            return "guild"
        if channel_id not in self.channel_ids:
            return "channel"
        if not self.role_ids & role_ids:
            return "role"
        return None

    def allows(
        self,
        guild_id: int | None,
        channel_id: int | None,
        role_ids: frozenset[int] = frozenset(),
    ) -> bool:
        return self.denial_reason(guild_id, channel_id, role_ids) is None

    @property
    def configured(self) -> bool:
        return bool(
            self.state_available and self.channel_ids
            and self.role_ids
        )

    def _persist(
        self,
        guild_id: int,
        channel_ids: frozenset[int],
        role_ids: frozenset[int],
    ) -> None:
        write_json_atomic(self._state_path, {
            "version": 3,
            "guild_id": guild_id,
            "channel_ids": sorted(channel_ids),
            "role_ids": sorted(role_ids),
        })

    def set_channels(
        self,
        guild_id: int,
        channel_ids: frozenset[int],
    ) -> frozenset[int]:
        if (
            type(guild_id) is not int
            or guild_id != self.guild_id
            or not valid_allowlist_ids(channel_ids, container=frozenset, minimum=1)
        ):
            raise ValueError("invalid Codex allowlist channels")
        self._persist(guild_id, channel_ids, self.role_ids)
        previous = self.channel_ids
        if channel_ids != previous or not self.state_available:
            self.generation += 1
        self.channel_ids = channel_ids
        self.state_available = True
        return previous

    def set_roles(self, guild_id: int, role_ids: frozenset[int]) -> frozenset[int]:
        if (
            type(guild_id) is not int
            or guild_id != self.guild_id
            or not self.channel_ids
            or not valid_allowlist_ids(role_ids, container=frozenset, minimum=1)
            or guild_id in role_ids
        ):
            raise ValueError("invalid Codex allowlist roles")
        self._persist(guild_id, self.channel_ids, role_ids)
        previous = self.role_ids
        if role_ids != previous or not self.state_available:
            self.generation += 1
        self.role_ids = role_ids
        self.state_available = True
        return previous

    def is_current(self, generation: int) -> bool:
        return generation == self.generation and not self._suspended

    def suspend(self) -> None:
        if not self._suspended:
            self.generation += 1
        self._suspended = True

    def resume(self) -> None:
        self._suspended = False
