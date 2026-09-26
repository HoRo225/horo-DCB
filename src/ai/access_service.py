from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal

from src.ai.access import CodexAccess, valid_allowlist_ids
from src.ai.client import CodexBridgeClient
from src.state import consume_task_exception


AccessChangeOutcome = Literal[
    "unchanged", "updated", "updated_with_warning", "persist_failed",
    "archive_failed", "archived_persist_failed",
]


class AiAccessService:
    def __init__(self, access: CodexAccess, client: CodexBridgeClient) -> None:
        self.access = access
        self.client = client
        self._tasks: set[asyncio.Task[AccessChangeOutcome]] = set()
        self._closed = False

    def _validate(self, guild_id: int, selected: frozenset[int], *, roles: bool) -> None:
        if (
            type(guild_id) is not int
            or guild_id != self.access.guild_id
            or not self.access.enabled
            or not valid_allowlist_ids(selected, container=frozenset, minimum=1)
            or (roles and (
                not self.access.state_available
                or not self.access.channel_ids
                or guild_id in selected
            ))
        ):
            raise ValueError("invalid Codex access change")

    async def change_channels(
        self, guild_id: int, channel_ids: frozenset[int], *,
        still_current: Callable[[], bool] | None = None,
    ) -> AccessChangeOutcome:
        return await self._accept(guild_id, channel_ids, roles=False, still_current=still_current)

    async def change_roles(
        self, guild_id: int, role_ids: frozenset[int], *,
        still_current: Callable[[], bool] | None = None,
    ) -> AccessChangeOutcome:
        return await self._accept(guild_id, role_ids, roles=True, still_current=still_current)

    async def _accept(
        self, guild_id: int, selected: frozenset[int], *, roles: bool,
        still_current: Callable[[], bool] | None,
    ) -> AccessChangeOutcome:
        self._validate(guild_id, selected, roles=roles)
        if self._closed or (still_current is not None and not still_current()):
            return "unchanged"
        task = asyncio.create_task(self._change(guild_id, selected, roles=roles))
        self._tasks.add(task)

        def observe(done: asyncio.Task[AccessChangeOutcome]) -> None:
            self._tasks.discard(done)
            consume_task_exception(done)

        task.add_done_callback(observe)
        # Retiring a panel only stops its caller, not an accepted mutation owner.
        return await asyncio.shield(task)

    async def _change(
        self, guild_id: int, selected: frozenset[int], *, roles: bool,
    ) -> AccessChangeOutcome:
        async with self.access.mutation_lock:
            previous = self.access.role_ids if roles else self.access.channel_ids
            detach = selected != previous and (roles or bool(previous - selected))
            result = None
            if detach:
                self.access.suspend()
            try:
                if detach:
                    try:
                        result = await self.client.archive_scope(guild_id)
                    except Exception:
                        return "archive_failed"
                try:
                    if roles:
                        self.access.set_roles(guild_id, selected)
                    else:
                        self.access.set_channels(guild_id, selected)
                except OSError:
                    return "archived_persist_failed" if detach else "persist_failed"
                if previous == selected:
                    return "unchanged"
                if result is not None and result.archive_unconfirmed_count:
                    return "updated_with_warning"
                return "updated"
            finally:
                if detach:
                    self.access.resume()

    async def close(self, *, deadline: float) -> None:
        self._closed = True
        tasks = set(self._tasks)
        if not tasks:
            return
        remaining = max(0, deadline - asyncio.get_running_loop().time())
        _, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in pending:
            if not task.cancelling():
                task.cancel()
        # Owners remain strongly referenced until their finally blocks finish.
        if pending:
            await asyncio.sleep(0)
