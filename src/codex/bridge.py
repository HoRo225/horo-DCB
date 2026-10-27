from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.web_log import AccessLogger
from openai_codex import AsyncCodex, CodexConfig

from src.codex.protocol import (
    CLEANUP_SECONDS,
    ERROR_HTTP_STATUS,
    MAX_STREAM_FRAME_BYTES,
    PROTOCOL_VERSION,
    CodexBridgeError,
    completed_frame,
    error_frame,
    progress_frame,
    validate_archive_payload,
    validate_chat_payload,
)
from src.codex.runtime import CodexService
from src.codex.sdk import feature_config
from src.codex.thread_store import ThreadStore
from src.config import CODEX_BRIDGE_PORT, CODEX_HOME, CODEX_WORKSPACE, bridge_token_env


class _HealthAccessLogger(AccessLogger):
    def log(self, request, response, elapsed):
        if request.path == "/livez" and response.status == 200:
            return
        super().log(request, response, elapsed)


_TOKEN_KEY = web.AppKey("bridge_token", str)
_FEATURE_CONFIG = feature_config()
_CONFIG_OVERRIDES = (
    'web_search="live"',
    *(f"features.{name}={str(value).lower()}" for name, value in _FEATURE_CONFIG["features"].items()),
    "check_for_update_on_startup=false",
    'forced_login_method="chatgpt"',
    'cli_auth_credentials_store="file"',
    "agents.enabled=false",
    "mcp_servers={}",
    'shell_environment_policy.inherit="none"',
)


def _error(code: str) -> web.Response:
    return web.json_response({"error": code}, status=ERROR_HTTP_STATUS[code])


async def _payload(request: web.Request, validate: Any) -> Any:
    try:
        raw = await request.json()
    except Exception:
        return None
    try:
        return validate(raw)
    except CodexBridgeError:
        return None


def _authorized(request: web.Request) -> bool:
    expected = f"Bearer {request.app[_TOKEN_KEY]}"
    supplied = request.headers.get("Authorization", "")
    return supplied.isascii() and hmac.compare_digest(supplied, expected)


@web.middleware
async def _require_token(request: web.Request, handler: Any) -> web.StreamResponse:
    if (
        request.match_info.http_exception is None
        and request.path.startswith("/v1/")
        and not _authorized(request)
    ):
        return _error("unauthorized")
    return await handler(request)


def create_app(token: str, service: Any) -> web.Application:
    app = web.Application(client_max_size=72 * 1024 * 1024, middlewares=[_require_token])
    app[_TOKEN_KEY] = token

    async def live(_request: web.Request) -> web.Response:
        alive = service.live
        return web.json_response(
            {"status": "live" if alive else "draining"}, status=200 if alive else 503
        )

    async def runtime_status(request: web.Request) -> web.Response:
        try:
            return web.json_response(
                asdict(await service.status()) | {"protocol_version": PROTOCOL_VERSION}
            )
        except Exception:
            logging.exception("Codex bridge status request failed.")
            return _error("unavailable")

    async def rate_limits(request: web.Request) -> web.Response:
        try:
            return web.json_response(asdict(await service.rate_limits()))
        except Exception:
            logging.exception("Codex bridge rate-limit request failed.")
            return _error("unavailable")

    async def models(request: web.Request) -> web.Response:
        try:
            return web.json_response(await service.models())
        except Exception:
            logging.exception("Codex bridge model catalog request failed.")
            return _error("unavailable")

    async def chat(request: web.Request) -> web.StreamResponse:
        if (payload := await _payload(request, validate_chat_payload)) is None:
            return _error("invalid_request")
        request_id = request.headers.get("X-Request-Id", payload.request_id)
        if not re.fullmatch(r"(?:[0-9a-f]{8}|[0-9a-f-]{36})", request_id):
            request_id = "-"

        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        loop = asyncio.get_running_loop()
        deadline = loop.time() + payload.budget_ms / 1000 + CLEANUP_SECONDS
        latest: dict[str, object] | None = None
        changed = asyncio.Event()

        def progress(snapshot: dict[str, str]) -> None:
            nonlocal latest
            latest = progress_frame(snapshot["stage"], snapshot["summary"])
            changed.set()

        async def write(frame: dict[str, object]) -> None:
            data = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            if len(data) > MAX_STREAM_FRAME_BYTES:
                data = b'{"type":"error","error":"unavailable"}\n'
            async with asyncio.timeout_at(min(deadline, loop.time() + CLEANUP_SECONDS)):
                await response.write(data)

        async with asyncio.timeout_at(deadline):
            await response.prepare(request)
        common = dict(models=payload.models, on_progress=progress,
                      budget_ms=payload.budget_ms, parent_channel_id=payload.parent_channel_id,
                      request_id=request_id)
        if payload.mode == "chat":
            coroutine = service.chat(
                payload.conversation_key,
                payload.text,
                payload.images,
                **common,
            )
        else:
            coroutine = service.isolated(
                payload.conversation_key, payload.text, mode=payload.mode,
                context=payload.context, results=payload.results, **common,
            )
        owner = asyncio.create_task(coroutine)
        owner.add_done_callback(lambda _task: changed.set())
        try:
            await write(progress_frame("queued", ""))
            while not owner.done():
                async with asyncio.timeout_at(deadline):
                    await changed.wait()
                changed.clear()
                if owner.done():
                    break
                snapshot, latest = latest, None
                if snapshot is not None:
                    await write(snapshot)
            try:
                reply = owner.result()
            except CodexBridgeError as exc:
                await write(error_frame(exc.code))
            except asyncio.CancelledError:
                await write(error_frame("unavailable"))
            except Exception:
                logging.exception("Codex bridge chat request failed.")
                await write(error_frame("unavailable"))
            else:
                await write(completed_frame(reply))
            async with asyncio.timeout_at(min(deadline, loop.time() + CLEANUP_SECONDS)):
                await response.write_eof()
            return response
        finally:
            if not owner.done():
                owner.cancel()
            while not owner.done():
                try:
                    await asyncio.shield(owner)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not owner.cancelled():
                owner.exception()

    async def archive(request: web.Request) -> web.Response:
        if (scope := await _payload(request, validate_archive_payload)) is None:
            return _error("invalid_request")
        try:
            if scope.conversation_key is not None:
                result = await service.reset_conversation(scope.conversation_key)
            else:
                result = await service.archive_scope(
                    scope.guild_id, scope.channel_id, include_children=scope.include_children
                )
        except Exception:
            logging.exception("Codex bridge archive request failed.")
            return _error("unavailable")
        return web.json_response(asdict(result))

    app.router.add_get("/livez", live)
    app.router.add_get("/v1/status", runtime_status)
    app.router.add_get("/v1/rate-limits", rate_limits)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat", chat)
    app.router.add_post("/v1/archive", archive)
    return app


def _codex_home() -> Path:
    value = os.environ.get("CODEX_HOME", "").strip()
    if not value:
        raise RuntimeError(f"CODEX_HOME must be {CODEX_HOME}")
    path = Path(value).resolve()
    if path != CODEX_HOME or not path.is_dir():
        raise RuntimeError(f"CODEX_HOME must be {CODEX_HOME}")
    return path


def _runtime_config(workspace: str) -> CodexConfig:
    return CodexConfig(
        cwd=workspace,
        config_overrides=_CONFIG_OVERRIDES,
        client_name="horo_dcb",
        client_title="horo-DCB",
    )


def _runtime_app() -> web.Application:
    token = bridge_token_env()
    codex_home = _codex_home()
    workspace = Path(CODEX_WORKSPACE)
    if not workspace.is_dir():
        raise RuntimeError("Codex runtime directories are unavailable")

    codex = AsyncCodex(_runtime_config(str(workspace)))
    service = CodexService(
        codex,
        ThreadStore(codex_home / "horo_threads.json"),
    )
    app = create_app(token, service)

    async def lifetime(_app: web.Application):
        service.start()
        try:
            yield
        finally:
            await service.close()

    app.cleanup_ctx.append(lifetime)
    return app


async def _device_login() -> int:
    _codex_home()
    async with AsyncCodex(_runtime_config(CODEX_WORKSPACE)) as codex:
        account = await codex.account()
        if account.account is not None:
            print("Codex account is already authenticated.")
            return 0
        handle = await codex.login_chatgpt_device_code()
        print(handle.verification_url)
        print(handle.user_code)
        sys.stdout.flush()
        result = await handle.wait()
        if not result.success:
            print("Codex device login failed.", file=sys.stderr)
            return 1
    print("Codex device login succeeded.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="?", choices=("serve", "login"), default="serve")
    args = parser.parse_args()
    if args.command == "login":
        try:
            result = asyncio.run(_device_login())
        except Exception:
            print("Codex device login failed.", file=sys.stderr)
            result = 1
        raise SystemExit(result)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    web.run_app(
        _runtime_app(),
        host="0.0.0.0",
        port=CODEX_BRIDGE_PORT,
        handler_cancellation=True,
        shutdown_timeout=CLEANUP_SECONDS,
        access_log_class=_HealthAccessLogger,
    )


if __name__ == "__main__":
    main()
