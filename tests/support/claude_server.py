from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from aiohttp import web

PASSWORD = "fake-password"
VERSION = "v0.13.6"


@dataclass(frozen=True, slots=True)
class Reply:
    status: int = 200
    events: tuple[dict[str, Any], ...] = ()
    body: object = None
    hang: bool = False
    chunk: int = 0


def block(index: int, start: dict[str, Any], *deltas: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index, "content_block": start},
        *({"type": "content_block_delta", "index": index, "delta": delta} for delta in deltas),
        {"type": "content_block_stop", "index": index},
    ]


def answer(
    *texts: str, thinking: tuple[str, ...] | None = None, search: bool = False, stop_reason: str = "end_turn",
    extras: tuple[dict[str, Any], ...] = (), hang: bool = False, chunk: int = 0,
) -> Reply:
    events: list[dict[str, Any]] = [{"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant", "model": "claude-fake", "content": [],
        "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 1},
    }}]
    if thinking is not None:
        events += block(len(events), {"type": "thinking", "thinking": ""},
                        *({"type": "thinking_delta", "thinking": piece} for piece in thinking),
                        {"type": "signature_delta", "signature": "sig"})
    if search:
        events += block(len(events), {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {}},
                        {"type": "input_json_delta", "partial_json": '{"query": "今日新聞"}'})
        events += block(len(events), {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [
            {"type": "web_search_result", "url": "https://example.com", "title": "新聞", "encrypted_content": "x" * 70_000},
        ]})
    for text in texts:
        events += block(len(events), {"type": "text", "text": ""}, {"type": "text_delta", "text": text},
                        {"type": "citations_delta", "citation": {"type": "web_search_result_location"}})
    events += [{"type": "ping"}, *extras]
    if not hang:
        events += [{"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 2}},
                   {"type": "message_stop"}]
    return Reply(events=tuple(events), hang=hang, chunk=chunk)


def failure(status: int, error_type: str, message: str = "fake failure") -> Reply:
    return Reply(status=status, body={"type": "error", "error": {"type": error_type, "message": message}})


def stream_failure(error_type: str, *texts: str) -> Reply:
    events = answer(*texts, hang=True).events
    return Reply(events=(*events, {"type": "error", "error": {"type": error_type, "message": "fake stream failure"}}))


class FakeClewdr:
    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.requests: list[dict[str, Any]] = []
        self.version_failures = 0
        self.version_calls = 0
        self.disconnected = asyncio.Event()
        self.url = ""
        self._runner: web.AppRunner | None = None

    def bodies(self, field: str) -> list[object]:
        return [body.get(field) for body in self.requests]

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/api/version", self._version)
        app.router.add_post("/code/v1/messages", self._messages)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{self._runner.addresses[0][1]}"
        return self.url

    async def close(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    async def _version(self, request: web.Request) -> web.Response:
        self.version_calls += 1
        if self.version_failures:
            self.version_failures -= 1
            return web.Response(status=503, text="starting")
        return web.Response(text=f"{VERSION}\nbuilt for tests\n")

    async def _messages(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("x-api-key") != PASSWORD or request.headers.get("anthropic-version") != "2023-06-01":
            return web.json_response(failure(401, "authentication_error", "invalid api key").body, status=401)
        self.requests.append(await request.json())
        reply = self.replies.pop(0) if self.replies else answer("收到")
        if reply.status != 200:
            return web.json_response(reply.body, status=reply.status)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        payload = b"".join(
            f"event: {event['type']}\r\ndata: {json.dumps(event, ensure_ascii=False)}\r\n\r\n".encode() for event in reply.events
        )
        size = reply.chunk or len(payload) or 1
        try:
            for start in range(0, len(payload), size):
                await response.write(payload[start:start + size])
                await asyncio.sleep(0)
            while reply.hang:
                await response.write(b'event: ping\ndata: {"type": "ping"}\n\n')
                await asyncio.sleep(0.02)
        except ConnectionResetError:
            self.disconnected.set()
            return response
        except asyncio.CancelledError:
            self.disconnected.set()
            raise
        return response
