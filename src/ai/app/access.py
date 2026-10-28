from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Literal

from src.ai.app.ports import Job, ModelError, TextModel
from src.ai.app.settings import AiSettings
from src.ai.domain.conversation import scope_matches
from src.core.app.supervisor import TaskGroup, cancel_all, wait_until

AccessChangeOutcome = Literal["unchanged", "updated"]


class AiAccess:
    def __init__(
        self, settings: AiSettings, model: TextModel, tasks: TaskGroup, *,
        role_ids_for_user: Callable[[int], frozenset[int] | None] | None = None,
    ) -> None:
        self._settings = settings
        self._model = model
        self._tasks = tasks
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
        self._settings.validate_change(guild_id, selected, roles=roles)
        if self._closed or (still_current is not None and not still_current()):
            return "unchanged"
        task = self._tasks.spawn(
            self._change(guild_id, selected, roles=roles, still_current=still_current, operation_id=operation_id), log=False,
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
        settings = self._settings
        async with self._lock:
            if self._closed or (still_current is not None and not still_current()):
                return "unchanged"
            previous = settings.role_ids if roles else settings.channel_ids
            removed = previous - selected
            detach = not roles and bool(removed)
            if detach:
                settings.suspend(removed)
            try:
                if detach:
                    def in_removed(job: Job) -> bool:
                        return any(
                            scope_matches(job.key, guild_id, channel_id,
                                          parent_channel_id=job.parent_channel_id, include_children=True)
                            for channel_id in removed
                        )
                    try:
                        await self._model.cancel_revoked(guild_id, in_removed)
                    except ModelError:
                        logging.exception("AI requests in removed allowlist channels did not stop in time.")
                settings.set_allowlist(guild_id, selected, roles=roles, operation_id=operation_id)
                if roles and removed:
                    if self._role_ids_for_user is None:
                        await self._model.cancel(guild_id)
                    else:
                        def revoked(job: Job) -> bool:
                            if job.user_id is None:
                                return False
                            member_roles = self._role_ids_for_user(job.user_id)
                            return member_roles is None or not selected.intersection(member_roles)
                        await self._model.cancel_revoked(guild_id, revoked)
                return "unchanged" if previous == selected else "updated"
            finally:
                if detach:
                    settings.resume(removed)

    def stop_new_work(self) -> None:
        pass

    async def close(self, *, deadline: float) -> None:
        self._closed = True
        pending = await wait_until(self._tasks.pending(), deadline)
        cancel_all(pending)
        if pending:
            await asyncio.sleep(0)
