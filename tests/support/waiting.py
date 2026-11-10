import asyncio
from collections.abc import Callable


async def until(condition: Callable[[], object], timeout: float = 3) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.005)


async def next_event(queue: asyncio.Queue, timeout: float = 3):
    async with asyncio.timeout(timeout):
        return await queue.get()


async def events_until(queue: asyncio.Queue, method: str, timeout: float = 3) -> list[tuple[str, object]]:
    events = []
    async with asyncio.timeout(timeout):
        while True:
            event = await queue.get()
            events.append(event)
            if event[0] == method:
                return events
