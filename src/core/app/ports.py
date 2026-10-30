from __future__ import annotations

from typing import Protocol


class Owner(Protocol):
    def stop_new_work(self) -> None: ...

    async def close(self, *, deadline: float) -> None: ...


class Presence(Protocol):
    def unavailable_reason(self, guild_id: int | None) -> str | None: ...
