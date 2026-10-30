from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable

from src.ai.app.ports import Job, ModelError, ModelReply, RuntimeStatus
from src.ai.domain.models import ModelSettings


class UnavailableModel:
    async def start(self) -> None:
        pass

    def try_start_request(self, user_id: int) -> bool:
        return True

    @contextlib.asynccontextmanager
    async def accepted_request(
        self, key: str, *, deadline: float, still_current: Callable[[], bool],
        user_id: int | None = None, parent_channel_id: int | None = None,
    ) -> AsyncIterator[Job]:
        raise ModelError("unavailable")
        yield

    async def chat(
        self, key: str, text: str, images: tuple[str, ...], *, instructions: str, job: Job, models: ModelSettings,
        on_progress: Callable[[str, str], None] | None = None, request_id: str | None = None,
    ) -> ModelReply:
        raise ModelError("unavailable")

    async def cancel(self, guild_id: int, user_id: int | None = None) -> None:
        pass

    async def cancel_revoked(self, guild_id: int, predicate: Callable[[Job], bool]) -> None:
        pass

    async def forget_scope(
        self, guild_id: int, channel_id: int | None = None, *, include_children: bool = False,
    ) -> None:
        pass

    async def get_runtime_status(self) -> RuntimeStatus:
        return RuntimeStatus("unavailable")

    def stop_new_work(self) -> None:
        pass

    async def close(self, *, deadline: float) -> None:
        pass
