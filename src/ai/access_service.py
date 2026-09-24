from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from src.ai.access import CodexAccess, MAX_CODEX_ALLOWED_CHANNELS
from src.ai.client import CodexBridgeClient


AccessChangeOutcome = Literal[
    "unchanged",
    "updated",
    "persist_failed",
    "archive_failed",
    "updated_archive_failed",
    "archived_persist_failed",
]


class AiAccessService:
    def __init__(self, access: CodexAccess, client: CodexBridgeClient) -> None:
        self.access = access
        self.client = client

    def _validate(self, guild_id: int, selected: frozenset[int], *, roles: bool) -> None:
        if (
            type(guild_id) is not int
            or guild_id != self.access.guild_id
            or not self.access.enabled
            or not isinstance(selected, frozenset)
            or not 1 <= len(selected) <= MAX_CODEX_ALLOWED_CHANNELS
            or any(type(value) is not int or value <= 0 for value in selected)
            or (roles and (
                not self.access.state_available
                or not self.access.channel_ids
                or guild_id in selected
            ))
        ):
            raise ValueError("invalid Codex access change")

    async def change_channels(
        self,
        guild_id: int,
        channel_ids: frozenset[int],
        *,
        still_current: Callable[[], bool] | None = None,
    ) -> AccessChangeOutcome:
        self._validate(guild_id, channel_ids, roles=False)
        async with self.access.mutation_lock:
            if still_current is not None and not still_current():
                return "unchanged"
            try:
                previous = self.access.set_channels(guild_id, channel_ids)
            except OSError:
                return "persist_failed"
            if previous == channel_ids:
                outcome = "unchanged"
            elif previous - channel_ids:
                self.access.suspend()
                try:
                    await self.client.archive_scope(guild_id)
                except Exception:
                    outcome = "updated_archive_failed"
                else:
                    outcome = "updated"
                finally:
                    self.access.resume()
            else:
                outcome = "updated"
            return outcome

    async def change_roles(
        self,
        guild_id: int,
        role_ids: frozenset[int],
        *,
        still_current: Callable[[], bool] | None = None,
    ) -> AccessChangeOutcome:
        self._validate(guild_id, role_ids, roles=True)
        async with self.access.mutation_lock:
            if still_current is not None and not still_current():
                return "unchanged"
            if role_ids == self.access.role_ids:
                try:
                    self.access.set_roles(guild_id, role_ids)
                except OSError:
                    outcome = "persist_failed"
                else:
                    outcome = "unchanged"
                return outcome

            self.access.suspend()
            try:
                await self.client.archive_scope(guild_id)
            except Exception:
                outcome = "archive_failed"
            else:
                try:
                    self.access.set_roles(guild_id, role_ids)
                except OSError:
                    outcome = "archived_persist_failed"
                else:
                    outcome = "updated"
            finally:
                self.access.resume()
            return outcome
