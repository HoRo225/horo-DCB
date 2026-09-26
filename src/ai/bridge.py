from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import hmac
import logging
import os
from pathlib import Path
import sys
from typing import Any

from aiohttp import web
from aiohttp.web_log import AccessLogger
from openai_codex import AsyncCodex, CodexConfig

from src.ai.protocol import (
    CodexBridgeError, ERROR_HTTP_STATUS, valid_bridge_token,
    validate_archive_payload, validate_chat_payload,
)
from src.ai.runtime import CODEX_WORKSPACE, CodexService
from src.ai.thread_store import ThreadStore


class _HealthAccessLogger(AccessLogger):
    def log(self, request, response, elapsed):
        if request.path in ("/livez", "/readyz", "/healthz") and response.status == 200:
            return
        super().log(request, response, elapsed)


_TOKEN_KEY = web.AppKey("bridge_token", str)
_CONFIG_OVERRIDES = (
    'web_search="live"',
    "features.standalone_web_search=true",
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


def _error(code: str) -> web.Response:
    if code not in ERROR_HTTP_STATUS:
        code = "unavailable"
    return web.json_response({"error": code}, status=ERROR_HTTP_STATUS[code])


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
            ready = status.get("ready") is True
        except Exception:
            ready = False
        if ready != last_ready:
            logging.info("Codex health status=%s", "ready" if ready else "not_ready")
            last_ready = ready
        return web.json_response(
            {"status": "ready" if ready else "not_ready"},
            status=200 if ready else 503,
        )

    async def live(_request: web.Request) -> web.Response:
        alive = service.live
        return web.json_response({"status": "live" if alive else "draining"}, status=200 if alive else 503)

    async def runtime_status(request: web.Request) -> web.Response:
        if not _authorized(request):
            return _error("unauthorized")
        try:
            return web.json_response(await service.status())
        except Exception:
            return _error("unavailable")

    async def rate_limits(request: web.Request) -> web.Response:
        if not _authorized(request):
            return _error("unauthorized")
        try:
            return web.json_response(asdict(await service.rate_limits()))
        except Exception:
            logging.error("Codex bridge rate-limit request failed.")
            return _error("unavailable")

    async def chat(request: web.Request) -> web.Response:
        if not _authorized(request):
            return _error("unauthorized")
        try:
            raw_payload = await request.json()
        except Exception:
            return _error("invalid_request")
        try:
            payload = validate_chat_payload(raw_payload)
            reply = await service.chat(
                payload.conversation_key,
                payload.text,
                payload.images,
                budget_ms=payload.budget_ms,
                parent_channel_id=payload.parent_channel_id,
            )
            body: dict[str, object] = {"reply": reply.text}
            if reply.image_urls:
                body["image_urls"] = list(reply.image_urls)
            return web.json_response(body)
        except CodexBridgeError as exc:
            return _error(exc.code)
        except Exception:
            logging.error("Codex bridge chat request failed.")
            return _error("unavailable")

    async def archive(request: web.Request) -> web.Response:
        if not _authorized(request):
            return _error("unauthorized")
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid_request")
        try:
            scope = validate_archive_payload(payload)
        except CodexBridgeError as exc:
            return _error(exc.code)
        try:
            result = await service.archive_scope(scope.guild_id, scope.channel_id, include_children=scope.include_children)
        except Exception:
            logging.error("Codex bridge archive request failed.")
            return _error("unavailable")
        return web.json_response(asdict(result))

    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", health)
    app.router.add_get("/livez", live)
    app.router.add_get("/v1/status", runtime_status)
    app.router.add_get("/v1/rate-limits", rate_limits)
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
        _runtime_app(), host="0.0.0.0", port=8765,
        handler_cancellation=True, shutdown_timeout=5,
        access_log_class=_HealthAccessLogger,
    )


if __name__ == "__main__":
    main()
