import asyncio
import logging
import signal
from pathlib import Path

from . import log_event


async def main() -> None:
    logging.disable(logging.CRITICAL)
    from .bot import HoroBot

    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda loop, context: log_event("asyncio_error", context.get("exception")))
    stopped = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopped.set)
    bot = HoroBot()
    runner = None
    waiter = None
    try:
        token = Path("/run/secrets/discord_token").read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("missing_secret")
        runner = asyncio.create_task(bot.start(token))
        waiter = asyncio.create_task(stopped.wait())
        await asyncio.wait({runner, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if runner.done():
            await runner
    except Exception as error:
        log_event("startup_error", error)
    finally:
        deadline = loop.time() + 30
        try:
            await bot.shutdown(deadline)
        except TimeoutError:
            log_event("shutdown_timeout")
        except Exception as error:
            log_event("shutdown_error", error)
        remaining_tasks = {task for task in (runner, waiter) if task is not None}
        for task in remaining_tasks:
            task.cancel()
        if remaining_tasks:
            await asyncio.wait(remaining_tasks, timeout=max(0, deadline - loop.time()))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        log_event("fatal_error", error)
