"""Verify the formal Bridge or public SDK cancellation; never print payloads."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import time
import urllib.request


BASE_URL = "http://codex:8765"
TEST_ID_BASE = 9_000_000_000_000_000_000
TEST_ID_LIMIT = TEST_ID_BASE + 1_000_000_000_000_000


def request(token: str, path: str, payload: dict | None = None, timeout: float = 3) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE_URL + path, data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = json.load(response)
    if not isinstance(body, dict):
        raise ValueError("invalid response")
    return body


def wait_ready(token: str, seconds: float = 60) -> dict:
    end = time.monotonic() + seconds
    def poll(path: str) -> dict:
        remaining = end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("readiness cutoff")
        return request(token, path, timeout=min(3, remaining))

    while time.monotonic() < end:
        try:
            status = poll("/v1/status")
            if (
                type(status.get("protocol_version")) is int
                and status["protocol_version"] == 2
                and status.get("ready") is True
                and status.get("reason") == "ready"
                and status.get("status_stale") is False
                and status.get("sdk_version") == "0.156.1"
                and status.get("runtime_version") == "0.156.1"
                and type(status.get("active_requests")) is int
            ):
                if poll("/livez").get("status") != "live" or poll("/readyz").get("status") != "ready":
                    raise ValueError("inconsistent health probes")
                return status
        except (OSError, ValueError):
            pass
        time.sleep(min(1, max(0, end - time.monotonic())))
    raise TimeoutError("readiness cutoff")


def archive(token: str, guild: int, thread: int, *, timeout: float = 12) -> None:
    body = request(token, "/v1/archive", {
        "guild_id": guild, "channel_id": thread, "include_children": False,
    }, timeout=timeout)
    counts = [body.get(field) for field in (
        "detached_count", "archived_count", "archive_unconfirmed_count",
    )]
    if any(type(value) is not int or value < 0 for value in counts) or sum(counts[1:]) != counts[0]:
        raise ValueError("invalid archive counts")
    if counts[2]:
        print(f"SDK archive unconfirmed: {counts[2]}")


def load_manifest(path: Path, guild: int) -> int:
    if not re.fullmatch(r"/tmp/horo-ai-live-[0-9]+\.json", str(path)):
        raise ValueError("invalid manifest path")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("invalid manifest permissions")
    body = json.loads(path.read_text())
    thread = body.get("thread") if isinstance(body, dict) else None
    if (
        not isinstance(body, dict) or set(body) != {"guild", "thread"}
        or type(body["guild"]) is not int or body["guild"] != guild or type(thread) is not int
        or not TEST_ID_BASE <= thread < TEST_ID_LIMIT
        or path.name != f"horo-ai-live-{thread}.json"
    ):
        raise ValueError("invalid test manifest")
    return thread


def cancel(token: str, payload: dict, guild: int, thread: int) -> None:
    # Cancel the socket, not a thread future whose HTTP request could continue.
    connection = http.client.HTTPConnection("codex", 8765, timeout=5)
    try:
        connection.request("POST", "/v1/chat", json.dumps(payload), {
            "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        })
        time.sleep(2)
    finally:
        if connection.sock is not None:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()
    end = time.monotonic() + 30
    while time.monotonic() < end:
        try:
            # An archive retry is idempotent; a chat retry would not be.
            archive(token, guild, thread, timeout=min(12, end - time.monotonic()))
            wait_ready(token, max(0, end - time.monotonic()))
            return
        except OSError:
            time.sleep(min(1, max(0, end - time.monotonic())))
    raise TimeoutError("cancel scope was not confirmed")


async def sdk_cancel() -> bool:
    # This client belongs only to this verification process, never to the daemon.
    import asyncio
    import logging
    import sys
    import tempfile

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 45
    work_deadline = deadline - 10
    archive_deadline = deadline - 5
    logging.disable(logging.CRITICAL)
    import openai_codex
    from openai_codex import AsyncCodex, ApprovalMode, Sandbox, TextInput
    from openai_codex.generated.v2_all import TurnCompletedNotification
    # The helper is copied to /tmp; load the source deployed in the formal image.
    sys.path.insert(0, "/app")
    from src.ai.bridge import _runtime_config
    from src.ai.runtime import PRIMARY_MODEL

    owned: set[asyncio.Task] = set()
    thread = handle = collector = interrupt_task = None
    passed = False

    def own(operation):
        task = asyncio.create_task(operation)
        owned.add(task)
        return task

    async def wait(task, until):
        remaining = until - loop.time()
        if remaining <= 0:
            raise TimeoutError("verification deadline")
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if not done:
            raise TimeoutError("verification deadline")
        return task.result()

    async def collect():
        stream = handle.stream()
        try:
            async for event in stream:
                payload = event.payload
                if (
                    event.method == "turn/completed"
                    and isinstance(payload, TurnCompletedNotification)
                    and payload.thread_id == handle.thread_id
                    and payload.turn.id == handle.id
                ):
                    return getattr(payload.turn.status, "value", payload.turn.status)
            return None
        finally:
            await stream.aclose()

    with tempfile.TemporaryDirectory(prefix="horo-sdk-cancel-", dir="/tmp") as workspace:
        codex = AsyncCodex(_runtime_config(workspace))
        try:
            if openai_codex.__version__ != "0.156.1":
                raise ValueError("SDK version mismatch")
            await wait(own(codex.__aenter__()), work_deadline)
            metadata = codex.metadata
            runtime = getattr(getattr(metadata, "serverInfo", None), "version", None)
            if not isinstance(runtime, str) or re.match(r"^0\.156\.1(?:\D|$)", runtime) is None:
                raise ValueError("runtime version mismatch")
            account = await wait(own(codex.account()), work_deadline)
            if account.account is None:
                raise ValueError("existing account required")
            thread = await wait(own(codex.thread_start(
                model=PRIMARY_MODEL, approval_mode=ApprovalMode.deny_all,
                sandbox=Sandbox.read_only, cwd=workspace,
                service_name="horo-dcb-verification",
            )), work_deadline)
            handle = await wait(own(thread.turn([TextInput(
                "請輸出 1 到 10000 的所有整數，每行一個，完成前不要使用工具。"
            )])), work_deadline)
            # A real handle is the accepted-turn acknowledgement; no chat is retried.
            collector = own(collect())
            await asyncio.sleep(0)
            interrupt_task = own(handle.interrupt())
            await wait(interrupt_task, work_deadline)
            passed = await wait(collector, work_deadline) == "interrupted"
        except Exception:
            passed = False
        finally:
            if handle is not None and collector is not None and not collector.done():
                try:
                    if interrupt_task is None:
                        interrupt_task = own(handle.interrupt())
                    await wait(interrupt_task, archive_deadline)
                    await wait(collector, archive_deadline)
                except Exception:
                    passed = False
            if thread is not None:
                try:
                    await wait(own(codex.thread_archive(thread.id)), archive_deadline)
                except Exception:
                    passed = False
            try:
                await wait(own(codex.close()), deadline)
            except Exception:
                passed = False
            pending = {task for task in owned if not task.done()}
            if pending:
                _, pending = await asyncio.wait(pending, timeout=max(0, deadline - loop.time()))
                if pending:
                    passed = False
            # Consume errors only after terminal collection or public SDK close.
            for task in owned:
                if task.done() and not task.cancelled():
                    task.exception()
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("ready", "smoke", "cancel", "cleanup", "sdk-cancel"))
    parser.add_argument("--parent-channel-id", type=int)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    started = time.monotonic()
    if args.mode == "sdk-cancel":
        if (
            os.name != "posix" or os.environ.get("CODEX_HOME") != "/app/codex"
            or not Path("/app/codex").is_dir()
            or args.parent_channel_id is not None or args.manifest is not None
        ):
            print("FAILED: formal Codex environment required")
            return 1
        import asyncio
        try:
            passed = asyncio.run(sdk_cancel())
        except Exception:
            passed = False
        if passed:
            print(f"PASS: sdk-cancel accepted-turn interrupted elapsed={time.monotonic() - started:.1f}s")
            return 0
        print("FAILED: sdk-cancel accepted-turn interruption or cleanup unconfirmed")
        return 1
    manifest = None
    token = os.environ.get("CODEX_BRIDGE_TOKEN", "")
    guild_raw = os.environ.get("CODEX_ALLOWED_GUILD_ID", "")
    if os.name != "posix" or not re.fullmatch(r"[0-9a-f]{64}", token) or not guild_raw.isdecimal() or int(guild_raw) <= 0:
        print("FAILED: formal Bot environment required")
        return 1
    guild = int(guild_raw)
    result = 0
    try:
        wait_ready(token)
        if args.mode == "ready":
            pass
        elif args.mode == "cleanup":
            if args.manifest is None:
                raise ValueError("manifest required")
            thread = load_manifest(args.manifest, guild)
            archive(token, guild, thread)
            args.manifest.unlink()
        else:
            if args.parent_channel_id is None or args.parent_channel_id <= 0 or args.manifest is not None:
                raise ValueError("observed test parent required")
            thread = TEST_ID_BASE + secrets.randbelow(TEST_ID_LIMIT - TEST_ID_BASE)
            manifest = Path(f"/tmp/horo-ai-live-{thread}.json")
            fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"guild": guild, "thread": thread}, handle)
            payload = {
                "conversation_key": f"guild:{guild}:thread:{thread}",
                "text": "請只回覆 OK。" if args.mode == "smoke" else "請輸出 1 到 10000 的所有整數，每行一個，完成前不要使用工具。",
                "images": [], "budget_ms": 10000 if args.mode == "smoke" else 30000,
                "parent_channel_id": args.parent_channel_id,
            }
            if args.mode == "smoke":
                body = request(token, "/v1/chat", payload, timeout=15)
                if not isinstance(body.get("reply"), str) or not body["reply"].strip():
                    raise ValueError("missing assistant reply")
            else:
                cancel(token, payload, guild, thread)
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        print(f"FAILED: {args.mode}")
        result = 1
    finally:
        if manifest is not None:
            try:
                wait_ready(token, 30)
                archive(token, guild, load_manifest(manifest, guild))
                manifest.unlink()
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                print("FAILED: test cleanup pending; use its private /tmp manifest")
                result = 1
    if result == 0:
        print(f"PASS: {args.mode} elapsed={time.monotonic() - started:.1f}s")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
