from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any, TypeVar


T = TypeVar("T")


def start_task(
    task: asyncio.Task[T] | None,
    coroutine: Callable[..., Coroutine[Any, Any, T]],
    *args: Any,
    name: str,
) -> asyncio.Task[T]:
    if task is not None and not task.done():
        return task
    return asyncio.create_task(coroutine(*args), name=name)


def consume_task_exception(task: asyncio.Future[object]) -> BaseException | None:
    return None if task.cancelled() else task.exception()


async def cancel_task(task: asyncio.Future[Any] | None) -> None:
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def read_json_state(path: Path | str, version: int) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != version
    ):
        raise ValueError("invalid state version")
    return payload


def load_state_or_disable(load: Callable[[], T], empty: T, message: str) -> tuple[T, bool]:
    try:
        return load(), True
    except FileNotFoundError:
        return empty, True
    except (OSError, ValueError, TypeError):
        logging.exception(message)
        return empty, False


def persist_or_disable(persist: Callable[[], None], available: bool, message: str) -> bool:
    if not available:
        return False
    try:
        persist()
    except OSError:
        logging.exception(message)
        return False
    return True


def write_json_atomic(path: Path | str, payload: object) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
