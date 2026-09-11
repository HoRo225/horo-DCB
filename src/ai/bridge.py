from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import os
from pathlib import Path
import stat
import sys
from typing import Any

from aiohttp import web
from aiohttp.web_log import AccessLogger
from openai_codex import AsyncCodex, CodexConfig

from src.ai.protocol import BridgeRequestError, SAFE_ERROR_CODES, valid_bridge_token, validate_chat_payload
from src.ai.runtime import CodexService, ThreadStore


class _HealthAccessLogger(AccessLogger):
    def log(self, request, response, elapsed):
        if request.path == "/healthz" and response.status == 200:
            return
        super().log(request, response, elapsed)


_TOKEN_KEY = web.AppKey("bridge_token", str)
_BASE_INSTRUCTIONS_FILENAME = "base_instructions.txt"
_MAX_BASE_INSTRUCTIONS_BYTES = 16 * 1024
_CONFIG_OVERRIDES = (
    'web_search="live"',
    'check_for_update_on_startup=false',
    'forced_login_method="chatgpt"',
    'cli_auth_credentials_store="file"',
    "agents.enabled=false",
    "features.apps=false",
    "features.goals=false",
    "features.hooks=false",
    "features.memories=false",
    "features.multi_agent=false",
    "features.remote_plugin=false",
    "features.shell_snapshot=false",
    "features.shell_tool=false",
    "features.unified_exec=false",
    "mcp_servers={}",
    'shell_environment_policy.inherit="none"',
)


def _error(code: str, status: int) -> web.Response:
    if code not in SAFE_ERROR_CODES:
        code, status = "unavailable", 503
    return web.json_response({"error": code}, status=status)


def _authorized(request: web.Request) -> bool:
    expected = f"Bearer {request.app[_TOKEN_KEY]}"
    supplied = request.headers.get("Authorization", "")
    return supplied.isascii() and hmac.compare_digest(supplied, expected)


def create_app(token: str, service: Any) -> web.Application:
    app = web.Application(client_max_size=24 * 1024 * 1024)
    app[_TOKEN_KEY] = token
    last_ready: bool | None = None

    async def health(_request: web.Request) -> web.Response:
        nonlocal last_ready
        try:
            status = await service.status()
            ready = (
                status.get("available") is True
                and status.get("authenticated") is True
            )
        except Exception:
            ready = False
        if ready != last_ready:
            logging.info("Codex health status=%s", "ready" if ready else "not_ready")
            last_ready = ready
        return web.json_response(
            {"status": "ready" if ready else "not_ready"},
            status=200 if ready else 503,
        )

    async def runtime_status(request: web.Request) -> web.Response:
        if not _authorized(request):
            return _error("unauthorized", 401)
        try:
            return web.json_response(await service.status())
        except Exception:
            return _error("unavailable", 503)

    async def chat(request: web.Request) -> web.Response:
        if not _authorized(request):
            return _error("unauthorized", 401)
        try:
            raw_payload = await request.json()
        except Exception:
            return _error("invalid_request", 400)
        try:
            payload = validate_chat_payload(raw_payload)
            reply = await service.chat(
                payload.conversation_key,
                payload.text,
                payload.images,
            )
            return web.json_response({"reply": reply})
        except BridgeRequestError as exc:
            return _error(exc.code, exc.status)
        except Exception:
            logging.error("Codex bridge chat request failed.")
            return _error("unavailable", 503)

    async def archive(request: web.Request) -> web.Response:
        if not _authorized(request):
            return _error("unauthorized", 401)
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid_request", 400)
        if not isinstance(payload, dict) or set(payload) - {
            "guild_id",
            "channel_id",
        }:
            return _error("invalid_request", 400)
        guild_id = payload.get("guild_id")
        channel_id = payload.get("channel_id")
        if type(guild_id) is not int or guild_id <= 0:
            return _error("invalid_request", 400)
        if channel_id is not None and (
            type(channel_id) is not int or channel_id <= 0
        ):
            return _error("invalid_request", 400)
        try:
            await service.archive_scope(guild_id, channel_id)
        except Exception:
            logging.error("Codex bridge archive request failed.")
            return _error("unavailable", 503)
        return web.json_response({})

    app.router.add_get("/healthz", health)
    app.router.add_get("/v1/status", runtime_status)
    app.router.add_post("/v1/chat", chat)
    app.router.add_post("/v1/archive", archive)
    return app


def _codex_home() -> Path:
    value = os.environ.get("CODEX_HOME", "").strip()
    if not value:
        raise RuntimeError("CODEX_HOME must be /app/codex")
    path = Path(value).resolve()
    if path != Path("/app/codex") or not path.is_dir():
        raise RuntimeError("CODEX_HOME must be /app/codex")
    return path


def _base_instructions(codex_home: Path) -> str:
    path = codex_home / _BASE_INSTRUCTIONS_FILENAME
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o777 != 0o600
            or not 0 < metadata.st_size <= _MAX_BASE_INSTRUCTIONS_BYTES
        ):
            raise OSError
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        raise RuntimeError(
            "Codex base instructions must be a non-empty UTF-8 0600 regular file"
        ) from None
    if not value:
        raise RuntimeError(
            "Codex base instructions must be a non-empty UTF-8 0600 regular file"
        )
    return value


def _runtime_config(workspace: str) -> CodexConfig:
    return CodexConfig(
        cwd=workspace,
        config_overrides=_CONFIG_OVERRIDES,
        client_name="horo_dcb",
        client_title="horo-DCB",
    )


def _runtime_app() -> web.Application:
    token = os.environ.get("CODEX_BRIDGE_TOKEN", "").strip()
    if not valid_bridge_token(token):
        raise RuntimeError("CODEX_BRIDGE_TOKEN must be 64 lowercase hex characters")
    codex_home = _codex_home()
    base_instructions = _base_instructions(codex_home)
    workspace = Path("/app/codex-workspace")
    if not workspace.is_dir():
        raise RuntimeError("Codex runtime directories are unavailable")

    codex = AsyncCodex(_runtime_config(str(workspace)))
    service = CodexService(
        codex,
        ThreadStore(codex_home / "horo_threads.json"),
        base_instructions=base_instructions,
    )
    app = create_app(token, service)

    async def lifetime(_app: web.Application):
        await service.initialize()
        try:
            yield
        finally:
            await service.close()

    app.cleanup_ctx.append(lifetime)
    return app


async def _device_login() -> int:
    _codex_home()
    workspace = "/app/codex-workspace"
    async with AsyncCodex(_runtime_config(workspace)) as codex:
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
        _runtime_app(), host="0.0.0.0", port=8765,
        handler_cancellation=True, shutdown_timeout=5,
        access_log_class=_HealthAccessLogger,
    )
