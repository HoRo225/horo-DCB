from __future__ import annotations

import asyncio
import faulthandler
import io
import logging
import os
import signal
import sys

import discord

PROBE_INTERVAL_SECONDS = 60.0
PROBE_TIMEOUT_SECONDS = 15.0
FAILURES_BEFORE_RESTART = 2
LAG_PROBE_SECONDS = 0.5
LAG_WARNING_SECONDS = 1.0


def dump_stacks() -> None:
    buffer = io.StringIO()
    for task in asyncio.all_tasks():
        task.print_stack(file=buffer)
    logging.warning("Asyncio task stacks:\n%s", buffer.getvalue())
    faulthandler.dump_traceback(file=sys.stderr, all_threads=True)


def install_stack_dump_signal() -> None:
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, dump_stacks)
    except NotImplementedError, AttributeError:
        pass


async def run_watchdog(
    client: discord.Client,
    *,
    interval: float = PROBE_INTERVAL_SECONDS,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    failures_before_restart: int = FAILURES_BEFORE_RESTART,
) -> None:
    lag = asyncio.create_task(lag_probe())
    try:
        failures = 0
        while not client.is_closed():
            await asyncio.sleep(interval)
            if client.is_closed():
                return
            try:
                async with asyncio.timeout(timeout):
                    await client.application_info()
            except Exception:
                failures += 1
                logging.warning(
                    "Discord REST probe failed (%d/%d).", failures, failures_before_restart, exc_info=True
                )
                if failures >= failures_before_restart:
                    dump_stacks()
                    logging.critical("Bot cannot reach Discord; exiting so Docker restarts it.")
                    logging.shutdown()
                    os._exit(1)
            else:
                failures = 0
    finally:
        lag.cancel()


async def lag_probe(
    *, interval: float = LAG_PROBE_SECONDS, threshold: float = LAG_WARNING_SECONDS
) -> None:
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(interval)
        lag = loop.time() - started - interval
        if lag > threshold:
            logging.warning("Event loop lagged %.2fs.", lag)
