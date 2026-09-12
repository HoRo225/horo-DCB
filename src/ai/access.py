from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from src.state import write_json_atomic

DEFAULT_CODEX_ACCESS_STATE_PATH = Path("/app/data/codex_access.json")
MAX_CODEX_ALLOWED_CHANNELS = 25


def member_role_ids(member: object) -> frozenset[int]:
    return frozenset(
        role_id
        for role in getattr(member, "roles", ())
        if type(role_id := getattr(role, "id", None)) is int and role_id > 0
    )


class CodexAccess:
    def __init__(
        self,
        enabled: bool,
        guild_id: int | None,
        *,
        state_path: Path | str | None = None,
    ) -> None:
        self.enabled = enabled
        self.guild_id = guild_id
        self.channel_ids: frozenset[int] = frozenset()
        self.role_ids: frozenset[int] = frozenset()
        self.state_available = True
        self.generation = 0
        self.mutation_lock = asyncio.Lock()
        self._suspended = False
        self._state_path = Path(state_path) if state_path is not None else None
        if self._state_path is None:
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or (
                type(payload.get("guild_id")) is not int
                or payload["guild_id"] <= 0
                or payload["guild_id"] != guild_id
            ):
                raise ValueError("invalid Codex access state")
            version = payload.get("version")
            if type(version) is not int:
                raise ValueError("invalid Codex access state")
            if version == 3 and set(payload) == {
                "version", "guild_id", "channel_ids", "role_ids"
            }:
                channel_ids = payload.get("channel_ids")
                role_ids = payload.get("role_ids")
                if (
                    not self._valid_ids(channel_ids, minimum=1)
                    or not self._valid_ids(role_ids, minimum=0)
                    or guild_id in role_ids
                ):
                    raise ValueError("invalid Codex access state")
                self.channel_ids = frozenset(channel_ids)
                self.role_ids = frozenset(role_ids)
            else:
                raise ValueError("invalid Codex access state")
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError):
            self.channel_ids = frozenset()
            self.role_ids = frozenset()
            self.state_available = False
            logging.error("Codex 白名單狀態檔無法讀取；AI 對話已停用。")

    @staticmethod
    def _valid_ids(values: object, *, minimum: int) -> bool:
        return (
            isinstance(values, list)
            and minimum <= len(values) <= MAX_CODEX_ALLOWED_CHANNELS
            and all(type(value) is int and value > 0 for value in values)
            and len(values) == len(set(values))
        )

    def allows(
        self,
        guild_id: int | None,
        channel_id: int | None,
        role_ids: frozenset[int] = frozenset(),
    ) -> bool:
        return (
            self.enabled
            and self.state_available
            and not self._suspended
            and guild_id == self.guild_id
            and channel_id in self.channel_ids
            and bool(self.role_ids & role_ids)
        )

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
        if self._state_path is None:
            return
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
            or not 1 <= len(channel_ids) <= MAX_CODEX_ALLOWED_CHANNELS
            or any(type(value) is not int or value <= 0 for value in channel_ids)
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
            or not 1 <= len(role_ids) <= MAX_CODEX_ALLOWED_CHANNELS
            or guild_id in role_ids
            or any(type(value) is not int or value <= 0 for value in role_ids)
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
