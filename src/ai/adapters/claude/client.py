from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import aiohttp

API_VERSION = "2023-06-01"
CONNECT_SECONDS = 10.0
READ_SECONDS = 120.0
ERROR_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "api_error": 500,
    "timeout_error": 504,
    "overloaded_error": 529,
}


class ClaudeHttpError(Exception):
    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(f"{status} {error_type}: {message}")
        self.status = status
        self.error_type = error_type
        self.message = message


class ClaudeConnectionError(Exception):
    pass


def http_error(status: int, body: bytes) -> ClaudeHttpError:
    text = body.decode("utf-8", "replace")
    try:
        error = json.loads(text).get("error")
    except ValueError, AttributeError:
        error = None
    if isinstance(error, dict):
        return ClaudeHttpError(status, str(error.get("type") or "http_error"), str(error.get("message") or "")[:300])
    return ClaudeHttpError(status, "http_error", text.strip()[:300])


def stream_error(event: dict[str, Any]) -> ClaudeHttpError:
    error = event.get("error")
    error = error if isinstance(error, dict) else {}
    error_type = str(error.get("type") or "api_error")
    return ClaudeHttpError(ERROR_STATUS.get(error_type, 500), error_type, str(error.get("message") or "")[:300])


class SseDecoder:
    def __init__(self) -> None:
        self._partial: list[bytes] = []
        self._data: list[bytes] = []

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        *lines, rest = chunk.split(b"\n")
        if lines:
            lines[0] = b"".join((*self._partial, lines[0]))
            self._partial = []
        self._partial.append(rest)
        events = []
        for raw in lines:
            line = raw.removesuffix(b"\r")
            if not line:
                if self._data:
                    events.append(self._decode(b"\n".join(self._data)))
                    self._data = []
            elif line.startswith(b"data:"):
                self._data.append(line[5:].removeprefix(b" "))
        return events

    @staticmethod
    def _decode(data: bytes) -> dict[str, Any]:
        try:
            event = json.loads(data)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            raise ClaudeHttpError(502, "api_error", "invalid stream event")
        return event


class ClaudeClient:
    def __init__(self, url: str, password: str) -> None:
        self._url = url.rstrip("/")
        self._password = password
        self._session: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=CONNECT_SECONDS, sock_read=READ_SECONDS)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def version(self) -> str:
        try:
            async with self._http().get(f"{self._url}/api/version") as response:
                body = await response.read()
        except aiohttp.ClientError as exc:
            raise ClaudeConnectionError(str(exc) or type(exc).__name__) from None
        if response.status != 200:
            raise http_error(response.status, body)
        lines = body.decode("utf-8", "replace").strip().splitlines()
        if not lines:
            raise ClaudeHttpError(502, "api_error", "empty version")
        return lines[0].strip()

    async def stream_message(self, body: dict[str, Any], *, on_event: Callable[[dict[str, Any]], None]) -> None:
        headers = {"x-api-key": self._password, "anthropic-version": API_VERSION, "accept": "text/event-stream"}
        try:
            async with self._http().post(f"{self._url}/code/v1/messages", json=body, headers=headers) as response:
                if not 200 <= response.status < 300:
                    raise http_error(response.status, await response.read())
                decoder = SseDecoder()
                async for chunk in response.content.iter_any():
                    for event in decoder.feed(chunk):
                        if event.get("type") == "error":
                            raise stream_error(event)
                        on_event(event)
                        if event.get("type") == "message_stop":
                            return
        except aiohttp.ClientError as exc:
            raise ClaudeConnectionError(str(exc) or type(exc).__name__) from None
        raise ClaudeConnectionError("stream ended before message_stop")
