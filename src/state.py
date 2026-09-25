from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile


def consume_task_exception(task: asyncio.Future[object]) -> BaseException | None:
    return None if task.cancelled() else task.exception()


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
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
