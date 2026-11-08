import asyncio
import contextlib
from dataclasses import dataclass, field
from enum import Enum

import aiohttp

from . import log_event


class CodexError(Exception):
    def __init__(self, code: str, rpc_code: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.rpc_code = rpc_code


class TurnPhase(Enum):
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    TERMINAL = "terminal"


def choose_model(models: list[dict], model: str | None, effort: str | None) -> tuple[dict | None, str | None]:
    chosen = next((value for value in models if value["id"] == model), None) or next((value for value in models if value.get("isDefault")), None) or (models[0] if models else None)
    if chosen is None:
        return None, effort
    efforts = [value["reasoningEffort"] for value in chosen.get("supportedReasoningEfforts", [])]
    return chosen, effort if effort in efforts else chosen.get("defaultReasoningEffort")


@dataclass
class TurnState:
    thread_id: str
    id: str | None = None
    phase: TurnPhase = TurnPhase.STARTING
    status: str | None = None
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    usage_before: dict | None = None
    usage_after: dict | None = None

    def finish(self, status: str) -> None:
        self.status = status
        self.phase = TurnPhase.TERMINAL
        self.terminal.set()


@dataclass
class ThreadState:
    id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: TurnState | None = None
    token_total: dict | None = None


class CodexClient:
    def __init__(self, socket_path: str = "/run/codex/app-server.sock") -> None:
        self.socket_path = socket_path
        self.ready = asyncio.Event()
        self._closing = False
        self._runner: asyncio.Task | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self.threads: dict[str, ThreadState] = {}
        self.loaded: set[str] = set()

    async def start(self) -> None:
        self._runner = asyncio.create_task(self._connect_loop())
        await self.ready.wait()

    async def close(self) -> None:
        self._closing = True
        self.ready.set()
        if self._runner is not None:
            self._runner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._runner

    def subscribe(self) -> asyncio.Queue:
        queue = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def thread_state(self, thread_id: str) -> ThreadState:
        return self.threads.setdefault(thread_id, ThreadState(thread_id))

    async def _connect_loop(self) -> None:
        delay = 1
        while not self._closing:
            reader = None
            try:
                connector = aiohttp.UnixConnector(path=self.socket_path)
                timeout = aiohttp.ClientTimeout(total=None, connect=10)
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    async with session.ws_connect("http://localhost/", max_msg_size=0, heartbeat=30) as websocket:
                        self._ws = websocket
                        self.loaded.clear()
                        reader = asyncio.create_task(self._read(websocket))
                        await self._request("initialize", {"clientInfo": {"name": "horo_dcb", "title": "horo-DCB", "version": "0.1.0"}, "capabilities": {"experimentalApi": True, "requestAttestation": False}}, wait_ready=False)
                        await websocket.send_json({"method": "initialized"})
                        self.ready.set()
                        self._publish("client/connected", {})
                        log_event("codex_connected")
                        delay = 1
                        await self._recover_threads()
                        await reader
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_event("codex_connection_error", error)
            finally:
                self.ready.clear()
                self._ws = None
                self.loaded.clear()
                if reader is not None:
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await reader
                for future in tuple(self._pending.values()):
                    if not future.done():
                        future.set_exception(CodexError("connection_lost"))
                for thread in self.threads.values():
                    if thread.active is not None and not thread.active.terminal.is_set():
                        thread.active.phase = TurnPhase.CANCELLING
                        thread.active.events.put_nowait(("client/disconnected", {}))
                self._publish("client/disconnected", {})
            if not self._closing:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10)

    async def _read(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async for frame in websocket:
                if frame.type == aiohttp.WSMsgType.TEXT:
                    message = frame.json()
                    if not isinstance(message, dict):
                        raise CodexError("protocol_error")
                    if "method" not in message and "id" in message:
                        future = self._pending.get(message["id"])
                        if future is not None and not future.done():
                            if "error" in message:
                                code = message["error"].get("code")
                                future.set_exception(CodexError("rpc_rejected", code if type(code) is int else None))
                            else:
                                future.set_result(message.get("result", {}))
                    elif "id" in message:
                        await websocket.send_json({"id": message["id"], "error": {"code": -32601, "message": "Client operations are disabled"}})
                        params = message.get("params", {})
                        self._dispatch("client/unsupportedRequest", params)
                    else:
                        self._dispatch(message.get("method", ""), message.get("params", {}))
                elif frame.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_exception(CodexError("connection_lost"))

    def _publish(self, method: str, params: dict) -> None:
        for queue in self._subscribers:
            queue.put_nowait((method, params))

    def _dispatch(self, method: str, params: dict) -> None:
        self._publish(method, params)
        thread_id = params.get("threadId")
        thread = self.threads.get(thread_id)
        if thread is None:
            return
        turn = thread.active
        turn_data = params.get("turn", {})
        turn_id = params.get("turnId") or turn_data.get("id")
        if turn is None or (turn.id is not None and turn_id is not None and turn.id != turn_id):
            return
        if turn.id is None and turn_id is not None:
            turn.id = turn_id
        if method == "thread/tokenUsage/updated":
            total = params.get("tokenUsage", {}).get("total")
            if isinstance(total, dict):
                thread.token_total = total.copy()
                turn.usage_after = total.copy()
        elif method == "turn/started" and turn.phase == TurnPhase.STARTING:
            turn.phase = TurnPhase.RUNNING
        elif method == "turn/completed":
            status = turn_data.get("status")
            if status in ("completed", "failed", "interrupted"):
                turn.finish(status)
        turn.events.put_nowait((method, params))

    async def _request(self, method: str, params: dict | None = None, *, wait_ready: bool = True) -> dict:
        if wait_ready:
            await self.ready.wait()
        if self._closing or self._ws is None:
            raise CodexError("connection_lost")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        try:
            async with asyncio.timeout(30):
                await self._ws.send_json(message)
                return await future
        except TimeoutError:
            raise CodexError("rpc_timeout") from None
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def account(self) -> dict:
        return await self._request("account/read", {"refreshToken": False})

    async def rate_limits(self) -> dict:
        return await self._request("account/rateLimits/read")

    async def login(self) -> dict:
        return await self._request("account/login/start", {"type": "chatgptDeviceCode"})

    async def cancel_login(self, login_id: str) -> None:
        await self._request("account/login/cancel", {"loginId": login_id})

    async def logout(self) -> None:
        await self._request("account/logout")

    async def models(self) -> list[dict]:
        result = []
        cursor = None
        while True:
            page = await self._request("model/list", {"includeHidden": False, "cursor": cursor})
            result.extend(page["data"])
            cursor = page.get("nextCursor")
            if not cursor:
                return result

    async def new_thread(self, model: str | None = None, instructions: str | None = None) -> ThreadState:
        params = {"ephemeral": False, "approvalPolicy": "never", "sandbox": "read-only"}
        if model is not None:
            params["model"] = model
        if instructions:
            params["developerInstructions"] = instructions
        result = await self._request("thread/start", params)
        thread = self.thread_state(result["thread"]["id"])
        thread.token_total = {key: 0 for key in ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")}
        self.loaded.add(thread.id)
        return thread

    async def resume_thread(self, thread_id: str) -> ThreadState:
        await self._request("thread/resume", {"threadId": thread_id, "approvalPolicy": "never", "sandbox": "read-only"})
        self.loaded.add(thread_id)
        return self.thread_state(thread_id)

    async def load_thread(self, thread_id: str) -> ThreadState:
        if thread_id in self.loaded:
            return self.thread_state(thread_id)
        return await self.resume_thread(thread_id)

    async def start_turn(self, thread: ThreadState, turn: TurnState, inputs: list[dict], model: str | None = None, effort: str | None = None, summary: str = "auto", context: str | None = None) -> None:
        if thread.active is not None:
            raise CodexError("thread_busy")
        thread.active = turn
        turn.usage_before = thread.token_total.copy() if thread.token_total is not None else None
        params = {"threadId": thread.id, "input": inputs, "summary": summary}
        if model is not None:
            params["model"] = model
        if effort is not None:
            params["effort"] = effort
        if context:
            params["additionalContext"] = {"discord_channel": {"kind": "untrusted", "name": "discord_channel", "value": context}}
        try:
            result = await self._request("turn/start", params)
        except CodexError as error:
            if error.code == "rpc_rejected":
                turn.finish("failed")
            raise
        turn.id = result["turn"]["id"]
        if turn.phase == TurnPhase.STARTING:
            turn.phase = TurnPhase.RUNNING

    async def interrupt(self, turn: TurnState) -> None:
        if turn.terminal.is_set():
            return
        turn.phase = TurnPhase.CANCELLING
        if turn.id is not None and self.ready.is_set():
            try:
                await self._request("turn/interrupt", {"threadId": turn.thread_id, "turnId": turn.id})
            except Exception as error:
                log_event("codex_interrupt_error", error)
        elif self.ready.is_set():
            try:
                await self._recover_turn(turn, wait_ready=True)
            except Exception as error:
                log_event("codex_recovery_error", error)

    async def wait_terminal(self, turn: TurnState) -> None:
        while not turn.terminal.is_set():
            try:
                async with asyncio.timeout(10):
                    await turn.terminal.wait()
            except TimeoutError:
                if self.ready.is_set():
                    try:
                        await self._recover_turn(turn, wait_ready=True)
                    except Exception as error:
                        log_event("codex_recovery_error", error)

    async def _recover_threads(self) -> None:
        pending = [thread.active for thread in tuple(self.threads.values()) if thread.active is not None and not thread.active.terminal.is_set()]
        for turn in pending:
            if not turn.terminal.is_set():
                try:
                    await self._recover_turn(turn, wait_ready=False)
                except Exception as error:
                    log_event("codex_recovery_error", error)

    async def _recover_turn(self, turn: TurnState, *, wait_ready: bool) -> None:
        result = await self._request("thread/read", {"threadId": turn.thread_id, "includeTurns": True}, wait_ready=wait_ready)
        thread_data = result["thread"]
        if thread_data["status"]["type"] in ("notLoaded", "active"):
            result = await self._request("thread/resume", {"threadId": turn.thread_id, "approvalPolicy": "never", "sandbox": "read-only"}, wait_ready=wait_ready)
            self.loaded.add(turn.thread_id)
            thread_data = result["thread"]
        for candidate in thread_data.get("turns", []):
            if candidate["id"] == turn.id and candidate["status"] in ("completed", "failed", "interrupted"):
                turn.finish(candidate["status"])
                turn.events.put_nowait(("client/recovered", {}))
                return
            if turn.id is None and candidate["status"] == "inProgress":
                turn.id = candidate["id"]
        if thread_data["status"]["type"] == "idle":
            turn.finish("interrupted")
            turn.events.put_nowait(("client/recovered", {}))
        elif turn.id is not None and thread_data["status"]["type"] == "active":
            await self._request("turn/interrupt", {"threadId": turn.thread_id, "turnId": turn.id}, wait_ready=wait_ready)
