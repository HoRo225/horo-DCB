from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import struct
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from src.ai.protocol import (
    MAX_IMAGE_BYTES,
    MEDIA_CHUNK_BYTES,
    MEDIA_HEADER_LIMIT,
    MEDIA_KINDS,
    OUTPUT_IMAGE_TYPES,
    CodexBridgeError,
    ImageAttachmentError,
    validate_image_bytes,
)
from src.state import consume_task_exception

_MAX_WORKERS = 2
_MAX_PENDING = 4
_WORKER_ERRORS = {
    "image_format": "目前無法解讀這個圖片格式。",
    "image_invalid": "圖片格式驗證失敗，請重新上傳圖片。",
    "image_pixels": "圖片尺寸過大，請縮小後再試。",
    "image_frames": "動畫影格過多，請縮短後再試。",
    "media_invalid": "目前無法解讀這個媒體。",
    "video_invalid": "目前無法解讀這個 GIF 動畫。",
    "lottie_invalid": "目前無法解讀這個 Lottie 貼圖。",
}


@dataclass(eq=False, slots=True)
class _Job:
    deadline: float
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    runner: asyncio.Task[tuple[str, bytes] | Exception] | None = None
    process: asyncio.subprocess.Process | None = None
    waiter: asyncio.Task[object] | None = None
    pipes: set[asyncio.Task[object]] = field(default_factory=set)
    collector: asyncio.Task[tuple[str, bytes]] | None = None
    temporary_dir: Path | None = None


class MediaExecutor:
    def __init__(self) -> None:
        self._permits = asyncio.Semaphore(_MAX_WORKERS)
        self._jobs: set[_Job] = set()
        self._closed = False
        self._failed = False
        self._close_deadline: float | None = None

    async def decode(
        self,
        kind: Literal["image", "video", "lottie"],
        data: bytes,
        content_type: str | None,
        *,
        deadline: float,
    ) -> tuple[str, bytes]:
        loop = asyncio.get_running_loop()
        if deadline <= loop.time():
            raise CodexBridgeError("timeout")
        if (
            not isinstance(kind, str)
            or kind not in MEDIA_KINDS
            or not isinstance(data, bytes)
            or len(data) > MAX_IMAGE_BYTES
            or (
                content_type is not None
                and (not isinstance(content_type, str) or len(content_type) > 128)
            )
        ):
            raise CodexBridgeError("unavailable")
        if self._closed or self._failed:
            raise CodexBridgeError("unavailable")
        if len(self._jobs) >= _MAX_WORKERS + _MAX_PENDING:
            raise CodexBridgeError("busy")

        job = _Job(deadline)
        self._jobs.add(job)
        runner = asyncio.create_task(self._run(job, kind, data, content_type))
        job.runner = runner
        runner.add_done_callback(consume_task_exception)
        try:
            async with asyncio.timeout_at(deadline):
                outcome = await asyncio.shield(runner)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        except TimeoutError as exc:
            job.cancelled.set()
            raise CodexBridgeError("timeout") from exc
        except asyncio.CancelledError:
            job.cancelled.set()
            raise

    async def close(self, *, deadline: float) -> None:
        self._closed = True
        self._close_deadline = (
            deadline if self._close_deadline is None else min(self._close_deadline, deadline)
        )
        for job in tuple(self._jobs):
            job.cancelled.set()
        tasks = {
            job.runner for job in self._jobs if job.runner is not None and not job.runner.done()
        }
        if tasks:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            _done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                self._failed = True
                raise CodexBridgeError("unavailable")
        if self._jobs or self._failed:
            raise CodexBridgeError("unavailable")

    async def _run(
        self,
        job: _Job,
        kind: str,
        data: bytes,
        content_type: str | None,
    ) -> tuple[str, bytes] | Exception:
        acquired = False
        result: tuple[str, bytes] | None = None
        error: Exception | None = None
        try:
            acquire = asyncio.create_task(self._permits.acquire())
            cancelled = asyncio.create_task(job.cancelled.wait())
            try:
                done, _pending = await asyncio.wait(
                    {acquire, cancelled},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if acquire in done:
                    acquired = True
                else:
                    acquire.cancel()
                    await asyncio.gather(acquire, return_exceptions=True)
                if job.cancelled.is_set() or job.deadline <= asyncio.get_running_loop().time():
                    code = (
                        "timeout"
                        if job.deadline <= asyncio.get_running_loop().time()
                        else "unavailable"
                    )
                    raise CodexBridgeError(code)
            finally:
                cancelled.cancel()
                await asyncio.gather(cancelled, return_exceptions=True)

            if job.cancelled.is_set():
                raise CodexBridgeError("unavailable")
            job.temporary_dir = Path(tempfile.mkdtemp(prefix="horo-media-"))
            spawn = asyncio.create_task(self._spawn(job.temporary_dir))
            cancelled = asyncio.create_task(job.cancelled.wait())
            try:
                done, _pending = await asyncio.wait(
                    {spawn, cancelled},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancelled in done and spawn not in done:
                    job.process = await asyncio.shield(spawn)
                    self._register_waiter(job)
                    raise CodexBridgeError("unavailable")
                job.process = spawn.result()
                self._register_waiter(job)
            finally:
                cancelled.cancel()
                await asyncio.gather(cancelled, return_exceptions=True)

            if job.cancelled.is_set():
                raise CodexBridgeError("unavailable")
            result = await self._exchange(job, kind, data, content_type)
        except (ImageAttachmentError, CodexBridgeError) as exc:
            error = exc
        except Exception:
            error = CodexBridgeError("unavailable")
        finally:
            cleanup_ok = await self._cleanup(job)
            if cleanup_ok:
                if acquired:
                    self._permits.release()
                self._jobs.discard(job)
            else:
                self._failed = True
                error = CodexBridgeError("unavailable")
        if error is not None:
            return error
        assert result is not None
        return result

    async def _exchange(
        self,
        job: _Job,
        kind: str,
        data: bytes,
        content_type: str | None,
    ) -> tuple[str, bytes]:
        process = job.process
        assert process is not None
        metadata = json.dumps(
            {"kind": kind, "content_type": content_type, "length": len(data)},
            separators=(",", ":"),
        ).encode()
        if len(metadata) > MEDIA_HEADER_LIMIT:
            raise CodexBridgeError("unavailable")
        assert job.waiter is not None
        job.pipes = {
            asyncio.create_task(self._write_request(process, metadata, data)),
            asyncio.create_task(self._read_reply(process)),
            asyncio.create_task(self._drain_stderr(process)),
            job.waiter,
        }
        job.collector = asyncio.create_task(self._collect_pipes(job, process))
        for task in job.pipes:
            if task is not job.waiter:
                task.add_done_callback(consume_task_exception)
        job.collector.add_done_callback(consume_task_exception)
        cancelled = asyncio.create_task(job.cancelled.wait())
        try:
            done, _pending = await asyncio.wait(
                {job.collector, cancelled},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancelled in done:
                raise CodexBridgeError("unavailable")
            return job.collector.result()
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)

    async def _collect_pipes(
        self,
        job: _Job,
        process: asyncio.subprocess.Process,
    ) -> tuple[str, bytes]:
        done, _pending = await asyncio.wait(
            job.pipes,
            return_when=asyncio.FIRST_EXCEPTION,
        )
        for task in done:
            exception = consume_task_exception(task)
            if exception is not None:
                raise exception
        values = [task.result() for task in done]
        if process.returncode != 0:
            raise CodexBridgeError("unavailable")
        return next(value for value in values if isinstance(value, tuple))

    async def _write_request(
        self,
        process: asyncio.subprocess.Process,
        metadata: bytes,
        data: bytes,
    ) -> None:
        assert process.stdin is not None
        process.stdin.write(struct.pack(">I", len(metadata)) + metadata)
        await process.stdin.drain()
        for offset in range(0, len(data), MEDIA_CHUNK_BYTES):
            process.stdin.write(data[offset : offset + MEDIA_CHUNK_BYTES])
            await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()

    async def _read_reply(
        self,
        process: asyncio.subprocess.Process,
    ) -> tuple[str, bytes]:
        assert process.stdout is not None
        prefix = await process.stdout.readexactly(4)
        header_length = struct.unpack(">I", prefix)[0]
        if not 0 < header_length <= MEDIA_HEADER_LIMIT:
            raise CodexBridgeError("unavailable")
        raw_header = await process.stdout.readexactly(header_length)
        try:
            header = json.loads(raw_header)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexBridgeError("unavailable") from exc
        if not isinstance(header, dict) or type(header.get("length")) is not int:
            raise CodexBridgeError("unavailable")
        length = header["length"]
        if header.get("ok") is False:
            if set(header) != {"ok", "error", "length"} or length != 0:
                raise CodexBridgeError("unavailable")
            error = header.get("error")
            if error not in _WORKER_ERRORS:
                raise CodexBridgeError("unavailable")
            if await process.stdout.read(1):
                raise CodexBridgeError("unavailable")
            raise ImageAttachmentError(_WORKER_ERRORS[error])
        if (
            header.get("ok") is not True
            or set(header) != {"ok", "content_type", "length"}
            or not 0 < length <= MAX_IMAGE_BYTES
            or header.get("content_type") not in OUTPUT_IMAGE_TYPES
        ):
            raise CodexBridgeError("unavailable")
        body = await process.stdout.readexactly(length)
        if await process.stdout.read(1):
            raise CodexBridgeError("unavailable")
        media_type = header["content_type"]
        validate_image_bytes(media_type, body, 0)
        return media_type, body

    def _register_waiter(self, job: _Job) -> None:
        assert job.process is not None
        job.waiter = asyncio.create_task(job.process.wait())
        job.waiter.add_done_callback(consume_task_exception)

    @staticmethod
    async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while await process.stderr.read(MEDIA_CHUNK_BYTES):
            pass

    def _cleanup_deadline(self, deadline: float) -> float:
        if self._close_deadline is not None:
            return min(deadline, self._close_deadline)
        return deadline

    async def _wait_cleanup_task(
        self,
        task: asyncio.Future[object],
        deadline: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        while not task.done():
            remaining = self._cleanup_deadline(deadline) - loop.time()
            if remaining <= 0:
                raise TimeoutError
            await asyncio.wait({task}, timeout=min(remaining, 0.05))
        task.result()

    async def _wait_group_gone(self, pid: int, deadline: float) -> bool:
        loop = asyncio.get_running_loop()
        while self._group_exists(pid):
            remaining = self._cleanup_deadline(deadline) - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(remaining, 0.05))
        return True

    async def _cleanup(self, job: _Job) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5
        process = job.process
        try:
            if process is not None and self._group_exists(process.pid):
                self._signal_group(process.pid, signal.SIGTERM)
                term_deadline = min(deadline, loop.time() + 1)
                if not await self._wait_group_gone(process.pid, term_deadline):
                    self._signal_group(process.pid, signal.SIGKILL)
                    if loop.time() >= self._cleanup_deadline(deadline):
                        return False
                    if not await self._wait_group_gone(process.pid, deadline):
                        return False
            if process is not None and process.returncode is None:
                assert job.waiter is not None
                await self._wait_cleanup_task(job.waiter, deadline)
            if job.pipes:
                await self._wait_cleanup_task(
                    asyncio.gather(*job.pipes, return_exceptions=True),
                    deadline,
                )
            if job.collector is not None:
                await asyncio.gather(job.collector, return_exceptions=True)
            if job.temporary_dir is not None:
                shutil.rmtree(job.temporary_dir)
            return True
        except OSError, TimeoutError:
            return False

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    @staticmethod
    def _group_exists(pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        return True

    async def _spawn(self, temporary_dir: Path) -> asyncio.subprocess.Process:
        root = Path(__file__).resolve().parents[2]
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-m",
            "src.ai.media_worker",
            cwd=root,
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": os.environ.get("LANG", "C.UTF-8"),
                "TMPDIR": str(temporary_dir),
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            close_fds=True,
            start_new_session=True,
        )
