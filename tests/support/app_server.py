import asyncio
import inspect
import itertools
import json
from collections.abc import Awaitable, Callable

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

MODEL = {
    "id": "gpt-test",
    "displayName": "GPT Test",
    "description": "測試模型",
    "isDefault": True,
    "defaultReasoningEffort": "medium",
    "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "medium"}, {"reasoningEffort": "high"}],
}


class Reject:
    def __init__(self, code: int = -32600, message: str = "rejected") -> None:
        self.code = code
        self.message = message


class Hold:
    pass


Handler = Callable[[dict], object | Awaitable[object]]


class FakeAppServer:
    def __init__(self) -> None:
        self.received: list[dict] = []
        self.handlers: dict[str, Handler] = {}
        self.after: dict[str, Callable[[dict, dict], Awaitable[None]]] = {}
        self.connections = 0
        self.connected = asyncio.Event()
        self.socket: web.WebSocketResponse | None = None
        self.replies: dict[int, asyncio.Future] = {}
        self.server_ids = itertools.count(100)
        self.threads = itertools.count(1)
        self.turns = itertools.count(1)
        self.thread_reads: dict[str, dict] = {}
        self.server: TestServer | None = None

    async def start(self) -> str:
        app = web.Application()
        app.router.add_get("/", self.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("/"))

    async def close(self) -> None:
        if self.socket is not None:
            await self.socket.close()
        if self.server is not None:
            await self.server.close()

    def requests(self, method: str) -> list[dict]:
        return [message.get("params", {}) for message in self.received if message.get("method") == method and "id" in message]

    def notifications(self, method: str) -> list[dict]:
        return [message for message in self.received if message.get("method") == method and "id" not in message]

    def responses(self) -> list[dict]:
        return [message for message in self.received if "method" not in message]

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse(max_msg_size=0)
        await socket.prepare(request)
        self.socket = socket
        self.connections += 1
        self.connected.set()
        async for frame in socket:
            if frame.type != WSMsgType.TEXT:
                break
            message = json.loads(frame.data)
            self.received.append(message)
            if "method" in message and "id" in message:
                asyncio.create_task(self.answer(socket, message))
            elif "method" not in message and message.get("id") in self.replies:
                self.replies.pop(message["id"]).set_result(message)
        self.connected.clear()
        return socket

    async def answer(self, socket: web.WebSocketResponse, message: dict) -> None:
        method = message["method"]
        params = message.get("params", {})
        handler = self.handlers.get(method) or getattr(self, "default_" + method.replace("/", "_"), None)
        result = handler(params) if handler is not None else {}
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Hold):
            return
        if isinstance(result, Reject):
            await socket.send_json({"id": message["id"], "error": {"code": result.code, "message": result.message}})
            return
        await socket.send_json({"id": message["id"], "result": result})
        follow = self.after.get(method)
        if follow is not None:
            await follow(params, result)

    async def notify(self, method: str, params: dict) -> None:
        assert self.socket is not None
        await self.socket.send_json({"method": method, "params": params})

    async def send(self, message: dict) -> None:
        assert self.socket is not None
        await self.socket.send_json(message)

    async def call(self, method: str, params: dict, request_id: int | None = None) -> asyncio.Future:
        request_id = next(self.server_ids) if request_id is None else request_id
        reply = asyncio.get_running_loop().create_future()
        self.replies[request_id] = reply
        await self.send({"id": request_id, "method": method, "params": params})
        return reply

    async def drop(self) -> None:
        assert self.socket is not None
        await self.socket.close()

    def default_initialize(self, params: dict) -> dict:
        return {"userAgent": "fake", "platformFamily": "unix", "platformOs": "linux"}

    def default_account_read(self, params: dict) -> dict:
        return {"account": {"type": "chatgpt", "planType": "plus"}, "requiresOpenaiAuth": True}

    def default_account_rateLimits_read(self, params: dict) -> dict:
        return {"rateLimits": {}}

    def default_model_list(self, params: dict) -> dict:
        return {"data": [MODEL], "nextCursor": None}

    def default_thread_start(self, params: dict) -> dict:
        return {"thread": {"id": f"thread-{next(self.threads)}"}}

    def default_thread_fork(self, params: dict) -> dict:
        return {"thread": {"id": f"thread-{next(self.threads)}", "forkedFromId": params["threadId"]}}

    def default_thread_resume(self, params: dict) -> dict:
        return {"thread": {"id": params["threadId"], "status": {"type": "idle"}, "turns": []}}

    def default_thread_read(self, params: dict) -> dict:
        return {"thread": self.thread_reads.get(params["threadId"], {"id": params["threadId"], "status": {"type": "idle"}, "turns": []})}

    def default_turn_start(self, params: dict) -> dict:
        return {"turn": {"id": f"turn-{next(self.turns)}", "status": "inProgress", "items": []}}
