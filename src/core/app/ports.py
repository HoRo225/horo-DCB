from __future__ import annotations

from typing import Protocol


class Owner(Protocol):
    def stop_new_work(self) -> None: ...

    async def close(self, *, deadline: float) -> None: ...
