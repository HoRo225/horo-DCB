from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Literal

from src.ai.access import AiSettings
from src.ai.client import CodexBridgeClient
from src.codex.protocol import CodexBridgeError, scope_matches
from src.tasks import cancel_all, track, wait_until

AccessChangeOutcome = Literal["unchanged", "updated"]


class AiAccessService:
    def __init__(self, access: AiSettings, client: CodexBridgeClient, *, role_ids_for_user: Callable[[int], frozenset[int] | None] | None = None) -> None:
        self.access = access
        self.client = client
        self._tasks: set[asyncio.Task[AccessChangeOutcome]] = set()
        self._lock = asyncio.Lock()
        self._closed = False
        self._role_ids_for_user = role_ids_for_user

    async def change(
        self,
        guild_id: int,
        selected: frozenset[int],
        *,
        roles: bool,
        still_current: Callable[[], bool] | None = None,
        operation_id: str | None = None,
    ) -> AccessChangeOutcome:
        self.access.validate_change(guild_id, selected, roles=roles)
        if self._closed or (still_current is not None and not still_current()):
            return "unchanged"
        task = track(
            self._tasks, asyncio.create_task(self._change(guild_id, selected, roles=roles, still_current=still_current, operation_id=operation_id))
        )
        return await asyncio.shield(task)

    async def _change(
        self,
        guild_id: int,
        selected: frozenset[int],
        *,
        roles: bool,
        still_current: Callable[[], bool] | None = None,
        operation_id: str | None = None,
    ) -> AccessChangeOutcome:
        async with self._lock:
            if self._closed or (still_current is not None and not still_current()):
                return "unchanged"
            previous = self.access.role_ids if roles else self.access.channel_ids
            removed = previous - selected
            detach = not roles and bool(removed)
            if detach:
                self.access.suspend(removed)
            try:
                if detach:
                    def in_removed(job) -> bool:
                        return any(
                            scope_matches(job.key, guild_id, channel_id,
                                          parent_channel_id=job.parent_channel_id, include_children=True)
                            for channel_id in removed
                        )
                    try:
                        await self.client.cancel_revoked(guild_id, in_removed)
                    except CodexBridgeError:
                        logging.exception("AI requests in removed allowlist channels did not stop in time.")
                self.access.set_allowlist(guild_id, selected, roles=roles, operation_id=operation_id)
                if roles and removed:
                    if self._role_ids_for_user is None:
                        await self.client.cancel(guild_id)
                    else:
                        def revoked(job) -> bool:
                            if job.user_id is None:
                                return False
                            member_roles = self._role_ids_for_user(job.user_id)
                            return member_roles is None or not selected.intersection(member_roles)
                        await self.client.cancel_revoked(guild_id, revoked)
                return "unchanged" if previous == selected else "updated"
            finally:
                if detach:
                    self.access.resume(removed)

    async def close(self, *, deadline: float) -> None:
        self._closed = True
        pending = await wait_until(self._tasks, deadline)
        cancel_all(pending)
        if pending:
            await asyncio.sleep(0)
