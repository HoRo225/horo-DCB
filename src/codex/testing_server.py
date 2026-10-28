from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp import WSMsgType, web

TOKEN = "fake-token"
MODELS = [
    {
        "id": "gpt-test", "model": "gpt-test", "displayName": "GPT Test", "description": "", "hidden": False,
        "isDefault": True, "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [{"reasoningEffort": effort, "description": ""} for effort in ("low", "medium", "high")],
        "inputModalities": ["text", "image"],
    },
    {
        "id": "gpt-backup", "model": "gpt-backup", "displayName": "GPT Backup", "description": "", "hidden": False,
        "isDefault": False, "defaultReasoningEffort": "low",
        "supportedReasoningEfforts": [{"reasoningEffort": "low", "description": ""}],
        "inputModalities": ["text"],
    },
]
RATE_LIMITS = {"rateLimits": {"limitId": "codex", "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1900000000}, "secondary": None}}

type Script = Callable[["FakeAppServer", web.WebSocketResponse, str, str, dict[str, Any]], Awaitable[None]]


class FakeRpcError(Exception):
    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def answer(*messages: tuple[str | None, str], status: str = "completed", error: dict[str, object] | None = None,
           items: tuple[dict[str, object], ...] = (), delay: float = 0) -> Script:
    async def script(server: FakeAppServer, ws: web.WebSocketResponse, thread_id: str, turn_id: str,
                     params: dict[str, Any]) -> None:
        scope = {"threadId": thread_id, "turnId": turn_id}
        await server.send(ws, {"method": "turn/started", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "inProgress"}}})
        if delay:
            await asyncio.sleep(delay)
        for item in items:
            await server.send(ws, {"method": "item/completed", "params": {**scope, "item": item}})
        for index, (phase, text) in enumerate(messages):
            item = {"type": "agentMessage", "id": f"{turn_id}-{index}", "text": text, "phase": phase}
            await server.send(ws, {"method": "item/completed", "params": {**scope, "item": item}})
        turn = {"id": turn_id, "status": status, "items": [], "error": error}
        await server.send(ws, {"method": "turn/completed", "params": {"threadId": thread_id, "turn": turn}})
    return script


async def silent(server: FakeAppServer, ws: web.WebSocketResponse, thread_id: str, turn_id: str,
                 params: dict[str, Any]) -> None:
    await asyncio.Event().wait()


class FakeAppServer:
    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.calls: list[tuple[str, Any]] = []
        self.frames: list[dict[str, Any]] = []
        self.notifications: list[str] = []
        self.replies: dict[object, dict[str, Any]] = {}
        self.rejected_upgrades: list[int] = []
        self.account: dict[str, object] | None = {"type": "chatgpt", "email": "horo@example.com", "planType": "plus"}
        self.models = MODELS
        self.script: Script = answer(("final_answer", "收到"))
        self.handlers: dict[str, Callable[[web.WebSocketResponse, Any], Awaitable[object]]] = {
            "initialize": self._initialize, "account/read": self._account_read,
            "model/list": self._model_list, "account/rateLimits/read": self._rate_limits,
            "thread/start": self._thread_start, "thread/resume": self._thread_resume,
            "thread/unsubscribe": self._thread_unsubscribe, "thread/archive": self._thread_archive,
            "turn/start": self._turn_start, "turn/interrupt": self._turn_interrupt,
            "account/login/start": self._login_start, "account/login/cancel": self._login_cancel,
            "account/logout": self._logout,
        }
        self.threads: dict[str, bool] = {}
        self.archived: set[str] = set()
        self.subscribed: dict[web.WebSocketResponse, set[str]] = {}
        self.turns: dict[str, asyncio.Task[None]] = {}
        self.connections: list[web.WebSocketResponse] = []
        self.ignore: set[str] = set()
        self._ids = itertools.count(1)
        self._server_ids = itertools.count(1000)
        self._waiters: dict[object, asyncio.Future[dict[str, Any]]] = {}
        self._changed = asyncio.Condition()
        self._runner: web.AppRunner | None = None
        self.url = ""

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/", self._socket)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def close(self) -> None:
        for task in self.turns.values():
            task.cancel()
        await self.drop()
        if self._runner is not None:
            await self._runner.cleanup()

    async def drop(self) -> None:
        for ws in tuple(self.connections):
            await ws.close()

    def methods(self) -> list[str]:
        return [method for method, _params in self.calls]

    def params(self, method: str) -> list[Any]:
        return [params for name, params in self.calls if name == method]

    async def wait_for(self, predicate: Callable[[], bool], timeout: float = 5) -> None:
        async with asyncio.timeout(timeout):
            async with self._changed:
                await self._changed.wait_for(predicate)

    async def send(self, ws: web.WebSocketResponse, message: dict[str, object]) -> None:
        if not ws.closed:
            await ws.send_str(json.dumps(message, ensure_ascii=False))

    async def broadcast(self, method: str, params: dict[str, object]) -> None:
        for ws in tuple(self.connections):
            await self.send(ws, {"method": method, "params": params})

    async def ask(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        request_id = next(self._server_ids)
        future = asyncio.get_running_loop().create_future()
        self._waiters[request_id] = future
        await self.send(self.connections[-1], {"id": request_id, "method": method, "params": params})
        return await asyncio.wait_for(future, 5)

    async def _socket(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            self.rejected_upgrades.append(401)
            return web.Response(status=401)
        if "Origin" in request.headers:
            self.rejected_upgrades.append(403)
            return web.Response(status=403)
        ws = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)
        self.connections.append(ws)
        self.subscribed[ws] = set()
        await self._notify_changed()
        try:
            async for message in ws:
                if message.type is WSMsgType.TEXT:
                    asyncio.create_task(self._handle(ws, json.loads(message.data)))
        finally:
            self.connections.remove(ws)
            self.subscribed.pop(ws, None)
            await self._notify_changed()
        return ws

    async def _notify_changed(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    async def _handle(self, ws: web.WebSocketResponse, message: dict[str, Any]) -> None:
        self.frames.append(message)
        method = message.get("method")
        if method is None:
            future = self._waiters.pop(message.get("id"), None)
            self.replies[message.get("id")] = message
            if future is not None and not future.done():
                future.set_result(message)
            return
        if "id" not in message:
            self.notifications.append(method)
            await self._notify_changed()
            return
        self.calls.append((method, message.get("params")))
        await self._notify_changed()
        if method in self.ignore:
            return
        handler = self.handlers.get(method)
        try:
            if handler is None:
                raise FakeRpcError(-32600, f"Invalid request: unknown variant `{method}`")
            result = await handler(ws, message.get("params"))
        except FakeRpcError as exc:
            error: dict[str, object] = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            await self.send(ws, {"id": message["id"], "error": error})
        else:
            await self.send(ws, {"id": message["id"], "result": result})

    async def _initialize(self, ws: web.WebSocketResponse, params: Any) -> object:
        return {"userAgent": "horo_dcb/0.165.0 (fake)", "codexHome": "/app/codex", "platformFamily": "unix", "platformOs": "linux"}

    async def _account_read(self, ws: web.WebSocketResponse, params: Any) -> object:
        return {"account": self.account, "requiresOpenaiAuth": True}

    async def _model_list(self, ws: web.WebSocketResponse, params: Any) -> object:
        return {"data": self.models, "nextCursor": None}

    async def _rate_limits(self, ws: web.WebSocketResponse, params: Any) -> object:
        if self.account is None:
            raise FakeRpcError(-32600, "codex account authentication required to read rate limits")
        return RATE_LIMITS

    async def _thread_start(self, ws: web.WebSocketResponse, params: Any) -> object:
        thread_id = f"thread-{next(self._ids)}"
        self.threads[thread_id] = bool(params.get("ephemeral"))
        self.subscribed[ws].add(thread_id)
        thread = {"id": thread_id, "ephemeral": self.threads[thread_id], "status": {"type": "idle"}}
        await self.send(ws, {"method": "thread/started", "params": {"thread": thread}})
        return {"thread": thread, "model": params.get("model")}

    def _known(self, thread_id: object) -> str:
        if thread_id not in self.threads or self.threads[thread_id] or thread_id in self.archived:
            raise FakeRpcError(-32600, f"no rollout found for thread id {thread_id}")
        return thread_id

    async def _thread_resume(self, ws: web.WebSocketResponse, params: Any) -> object:
        thread_id = self._known(params["threadId"])
        self.subscribed[ws].add(thread_id)
        return {"thread": {"id": thread_id, "status": {"type": "idle"}}}

    async def _thread_unsubscribe(self, ws: web.WebSocketResponse, params: Any) -> object:
        self.subscribed[ws].discard(params["threadId"])
        return {"status": "unsubscribed"}

    async def _thread_archive(self, ws: web.WebSocketResponse, params: Any) -> object:
        self.archived.add(self._known(params["threadId"]))
        return {}

    async def _turn_start(self, ws: web.WebSocketResponse, params: Any) -> object:
        thread_id = params["threadId"]
        if thread_id not in self.subscribed[ws]:
            raise FakeRpcError(-32600, f"thread not found: {thread_id}")
        turn_id = f"turn-{next(self._ids)}"
        self.turns[turn_id] = asyncio.create_task(self.script(self, ws, thread_id, turn_id, params))
        return {"turn": {"id": turn_id, "status": "inProgress", "items": [], "error": None}}

    async def _turn_interrupt(self, ws: web.WebSocketResponse, params: Any) -> object:
        task = self.turns.get(params["turnId"])
        if task is None or task.done():
            raise FakeRpcError(-32600, "no active turn to interrupt")
        task.cancel()
        turn = {"id": params["turnId"], "status": "interrupted", "items": [], "error": None}
        await self.send(ws, {"method": "turn/completed", "params": {"threadId": params["threadId"], "turn": turn}})
        return {}

    async def _login_start(self, ws: web.WebSocketResponse, params: Any) -> object:
        return {"type": "chatgptDeviceCode", "loginId": "login-1",
                "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "ABCD-1234"}

    async def _login_cancel(self, ws: web.WebSocketResponse, params: Any) -> object:
        if params.get("loginId") != "login-1":
            raise FakeRpcError(-32600, f"invalid login id: {params.get('loginId')}")
        return {"status": "canceled"}

    async def _logout(self, ws: web.WebSocketResponse, params: Any) -> object:
        self.account = None
        await self.broadcast("account/updated", {"authMode": None, "planType": None})
        return {}
