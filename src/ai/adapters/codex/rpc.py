from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiohttp

from src.core.app.supervisor import track

MAX_MESSAGE_BYTES = 64 * 1024 * 1024
HEARTBEAT_SECONDS = 20.0
HANDSHAKE_SECONDS = 30.0
REQUEST_SECONDS = 30.0
CLOSE_SECONDS = 3.0
RECONNECT_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)
OVERLOADED_DELAYS = (0.5, 1.0, 2.0)
OVERLOADED = -32001
METHOD_NOT_FOUND = -32601
OPT_OUT_NOTIFICATIONS = (
    "remoteControl/status/changed",
    "thread/tokenUsage/updated",
    "item/reasoning/textDelta",
    "turn/diff/updated",
    "turn/plan/updated",
    "item/plan/delta",
    "account/rateLimits/updated",
)
SERVER_REQUEST_REPLIES: dict[str, dict[str, object]] = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
    "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
    "item/tool/requestUserInput": {"answers": {}},
    "mcpServer/elicitation/request": {"action": "decline"},
    "item/tool/call": {"contentItems": [], "success": False},
}
_RUNTIME_VERSION = re.compile(r"^[^/\s]+/([0-9]+\.[0-9]+\.[0-9]+)")


class Phase(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    READY = "ready"
    DISCONNECTED = "disconnected"
    CLOSED = "closed"


class ConnectionLost(Exception):
    pass


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: object = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"{code}: {message}")

    @property
    def overloaded(self) -> bool:
        return self.code == OVERLOADED

    @property
    def thread_missing(self) -> bool:
        return self.message.startswith(("no rollout found for thread id ", "thread not found: "))


@dataclass(frozen=True, slots=True)
class Notification:
    method: str
    params: dict[str, Any]

    @property
    def server_request(self) -> bool:
        return self.method in SERVER_REQUEST_REPLIES


_LOST = object()


class ThreadEvents:
    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self._queue: asyncio.Queue[Notification | object] = asyncio.Queue()

    def push(self, event: Notification) -> None:
        self._queue.put_nowait(event)

    def lose(self) -> None:
        self._queue.put_nowait(_LOST)

    async def next(self) -> Notification:
        event = await self._queue.get()
        if event is _LOST:
            self._queue.put_nowait(_LOST)
            raise ConnectionLost
        return event


def runtime_version(user_agent: str | None) -> str | None:
    match = _RUNTIME_VERSION.match(user_agent or "")
    return match.group(1) if match else None


class AppServerClient:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        client_version: str,
        on_notification: Callable[[Notification], None] | None = None,
    ) -> None:
        self.url = url
        self._token = token
        self._client_version = client_version
        self._on_notification = on_notification
        self.phase = Phase.STOPPED
        self.generation = 0
        self.user_agent: str | None = None
        self.unresponsive = False
        self._ready = asyncio.Event()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._threads: dict[str, set[ThreadEvents]] = {}
        self._replies: set[asyncio.Task[None]] = set()

    @property
    def runtime_version(self) -> str | None:
        return runtime_version(self.user_agent)

    @property
    def ready(self) -> bool:
        return self.phase is Phase.READY

    def start(self) -> None:
        if self._supervisor is None and self.phase is Phase.STOPPED:
            self.phase = Phase.CONNECTING
            self._supervisor = asyncio.create_task(self._run())

    async def close(self) -> None:
        self.phase = Phase.CLOSED
        self._ready.clear()
        if self._supervisor is not None:
            self._supervisor.cancel()
            await asyncio.gather(self._supervisor, return_exceptions=True)
        self._fail_all()

    def reconnect(self) -> None:
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()

    async def wait_ready(self, timeout: float) -> bool:
        if self.phase is Phase.CLOSED:
            return False
        try:
            async with asyncio.timeout(max(0.0, timeout)):
                await self._ready.wait()
        except TimeoutError:
            return False
        return self.ready

    async def request(self, method: str, params: dict[str, object] | None = None, *,
                      timeout: float = REQUEST_SECONDS) -> Any:
        for delay in OVERLOADED_DELAYS:
            try:
                return await self._request(method, params, timeout)
            except RpcError as exc:
                if not exc.overloaded:
                    raise
            await asyncio.sleep(delay)
        return await self._request(method, params, timeout)

    @contextmanager
    def subscribe(self, thread_id: str) -> Iterator[ThreadEvents]:
        if not self.ready:
            raise ConnectionLost
        events = ThreadEvents(thread_id)
        self._threads.setdefault(thread_id, set()).add(events)
        try:
            yield events
        finally:
            subscribers = self._threads.get(thread_id)
            if subscribers is not None:
                subscribers.discard(events)
                if not subscribers:
                    del self._threads[thread_id]

    async def _request(self, method: str, params: dict[str, object] | None, timeout: float) -> Any:
        ws = self._ws
        if not self.ready or ws is None:
            raise ConnectionLost
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send(ws, {"id": request_id, "method": method, "params": params})
            try:
                async with asyncio.timeout(timeout):
                    return await future
            except TimeoutError:
                logging.error("Codex app-server did not answer %s in %.0fs; reconnecting.", method, timeout)
                self.unresponsive = True
                self.reconnect()
                raise
        finally:
            self._pending.pop(request_id, None)

    async def _send(self, ws: aiohttp.ClientWebSocketResponse, message: dict[str, object]) -> None:
        try:
            async with self._send_lock:
                await ws.send_str(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as exc:
            raise ConnectionLost from exc

    async def _run(self) -> None:
        attempt = 0
        async with aiohttp.ClientSession() as session:
            while self.phase is not Phase.CLOSED:
                self.phase = Phase.CONNECTING
                if await self._connect(session):
                    attempt = 0
                else:
                    attempt += 1
                if self.phase is Phase.CLOSED:
                    break
                self.phase = Phase.DISCONNECTED
                await asyncio.sleep(RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)])

    async def _connect(self, session: aiohttp.ClientSession) -> bool:
        try:
            ws = await session.ws_connect(
                self.url,
                headers={"Authorization": f"Bearer {self._token}"},
                heartbeat=HEARTBEAT_SECONDS,
                max_msg_size=MAX_MESSAGE_BYTES,
                timeout=aiohttp.ClientWSTimeout(ws_close=CLOSE_SECONDS),
            )
        except aiohttp.WSServerHandshakeError as exc:
            logging.error("Codex app-server rejected the WebSocket upgrade: HTTP %s.", exc.status)
            return False
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            logging.warning("Codex app-server connection failed: %s.", type(exc).__name__)
            return False
        self._ws = ws
        self._reader = asyncio.create_task(self._read(ws))
        established = False
        try:
            established = await self._handshake(ws)
            if established:
                await asyncio.wait({self._reader})
        finally:
            if not self._reader.done():
                self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._ws = None
            self._fail_all()
            await asyncio.shield(self._close_ws(ws))
        if established and self.phase is not Phase.CLOSED:
            logging.warning("Codex app-server connection lost; reconnecting.")
        return established

    async def _close_ws(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        try:
            async with asyncio.timeout(CLOSE_SECONDS):
                await ws.close()
        except (aiohttp.ClientError, ConnectionError, TimeoutError):
            pass

    async def _handshake(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        future = asyncio.get_running_loop().create_future()
        self._pending[0] = future
        try:
            await self._send(ws, {"id": 0, "method": "initialize", "params": {
                "clientInfo": {"name": "horo_dcb", "title": "horo-DCB", "version": self._client_version},
                "capabilities": {"experimentalApi": False, "optOutNotificationMethods": list(OPT_OUT_NOTIFICATIONS)},
            }})
            async with asyncio.timeout(HANDSHAKE_SECONDS):
                await asyncio.wait({future, self._reader}, return_when=asyncio.FIRST_COMPLETED)
            if not future.done():
                return False
            response = future.result()
            await self._send(ws, {"method": "initialized"})
        except TimeoutError:
            logging.error("Codex app-server did not finish the handshake in %.0fs.", HANDSHAKE_SECONDS)
            self.unresponsive = True
            return False
        except (RpcError, ConnectionLost) as exc:
            logging.error("Codex app-server handshake failed: %s.", exc)
            return False
        finally:
            self._pending.pop(0, None)
        user_agent = response.get("userAgent") if isinstance(response, dict) else None
        self.user_agent = user_agent if isinstance(user_agent, str) else None
        self.generation += 1
        self.phase = Phase.READY
        self._ready.set()
        logging.info("Codex app-server connected: %s.", self.runtime_version or "unknown version")
        return True

    def _fail_all(self) -> None:
        if self.phase is Phase.READY:
            self.phase = Phase.DISCONNECTED
        self._ready.clear()
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(ConnectionLost())
        threads, self._threads = self._threads, {}
        for subscribers in threads.values():
            for events in subscribers:
                events.lose()

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type is not aiohttp.WSMsgType.TEXT:
                if message.type is aiohttp.WSMsgType.ERROR:
                    break
                continue
            self.unresponsive = False
            try:
                payload = json.loads(message.data)
            except ValueError:
                logging.error("Codex app-server sent invalid JSON; reconnecting.")
                break
            if isinstance(payload, dict):
                self._dispatch(ws, payload)

    def _dispatch(self, ws: aiohttp.ClientWebSocketResponse, payload: dict[str, Any]) -> None:
        method = payload.get("method")
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}
        if "id" in payload and isinstance(method, str):
            track(self._replies, asyncio.create_task(self._answer(ws, payload["id"], method)), log=True)
            self._route(Notification(method, params))
        elif "id" in payload:
            future = self._pending.get(payload["id"]) if type(payload["id"]) is int else None
            if future is None or future.done():
                return
            error = payload.get("error")
            if isinstance(error, dict):
                code, text = error.get("code"), error.get("message")
                future.set_exception(RpcError(code if type(code) is int else 0,
                                              text if isinstance(text, str) else "", error.get("data")))
            else:
                future.set_result(payload.get("result"))
        elif isinstance(method, str):
            self._route(Notification(method, params))

    def _route(self, event: Notification) -> None:
        thread_id = event.params.get("threadId")
        if isinstance(thread_id, str):
            for events in tuple(self._threads.get(thread_id, ())):
                events.push(event)
        elif self._on_notification is not None and not event.server_request:
            try:
                self._on_notification(event)
            except Exception:
                logging.exception("Codex notification handler failed for %s.", event.method)

    async def _answer(self, ws: aiohttp.ClientWebSocketResponse, request_id: object, method: str) -> None:
        reply = SERVER_REQUEST_REPLIES.get(method)
        if reply is None:
            logging.warning("Codex app-server request %s rejected as unsupported.", method[:100])
            message = {"id": request_id, "error": {"code": METHOD_NOT_FOUND, "message": f"unsupported request `{method[:100]}`"}}
        else:
            logging.warning("Codex app-server request %s declined.", method)
            message = {"id": request_id, "result": reply}
        try:
            await self._send(ws, message)
        except ConnectionLost:
            pass
