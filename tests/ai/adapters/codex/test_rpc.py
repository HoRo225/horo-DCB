import asyncio
import unittest
from unittest.mock import patch

from src.ai.adapters.codex.rpc import (
    OPT_OUT_NOTIFICATIONS,
    SERVER_REQUEST_REPLIES,
    AppServerClient,
    ConnectionLost,
    Notification,
    Phase,
    RpcError,
)
from tests.support.codex_server import FakeAppServer, FakeRpcError


class RpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        for patcher in (patch("src.ai.adapters.codex.rpc.RECONNECT_DELAYS", (0.01,)), patch("src.ai.adapters.codex.rpc.OVERLOADED_DELAYS", (0.01, 0.01))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = FakeAppServer()
        await self.server.start()
        self.addAsyncCleanup(self.server.close)
        self.global_events: list[Notification] = []
        self.client = self.connect(self.server.token)

    def connect(self, token: str) -> AppServerClient:
        client = AppServerClient(self.server.url, token, client_version="9.9.9", on_notification=self.global_events.append)
        client.start()
        self.addAsyncCleanup(client.close)
        return client

    async def ready(self, client: AppServerClient | None = None) -> AppServerClient:
        client = client or self.client
        self.assertTrue(await client.wait_ready(5))
        return client

    async def test_handshake_initializes_then_notifies(self) -> None:
        await self.ready()
        await self.server.wait_for(lambda: "initialized" in self.server.notifications)
        method, params = self.server.calls[0]
        self.assertEqual(method, "initialize")
        self.assertEqual(params["clientInfo"], {"name": "horo_dcb", "title": "horo-DCB", "version": "9.9.9"})
        self.assertEqual(params["capabilities"], {"experimentalApi": False, "optOutNotificationMethods": list(OPT_OUT_NOTIFICATIONS)})
        self.assertEqual(self.client.runtime_version, "0.165.0")
        self.assertEqual(self.client.generation, 1)
        self.assertFalse(any("jsonrpc" in frame for frame in self.server.frames))
        self.assertEqual(self.server.rejected_upgrades, [])

    async def test_wrong_token_never_becomes_ready(self) -> None:
        with self.assertLogs(level="ERROR") as logs:
            client = self.connect("wrong")
            self.assertFalse(await client.wait_ready(0.3))
            self.assertNotEqual(client.phase, Phase.READY)
            with self.assertRaises(ConnectionLost):
                await client.request("account/read", {})
            await client.close()
        self.assertIn(401, self.server.rejected_upgrades)
        self.assertIn("HTTP 401", logs.output[0])

    async def test_errors_carry_code_message_and_thread_state(self) -> None:
        await self.ready()
        with self.assertRaises(RpcError) as caught:
            await self.client.request("thread/resume", {"threadId": "gone"})
        self.assertEqual(caught.exception.code, -32600)
        self.assertTrue(caught.exception.thread_missing)
        with self.assertRaises(RpcError) as caught:
            await self.client.request("turn/start", {"threadId": "gone", "input": []})
        self.assertTrue(caught.exception.thread_missing)

        async def shutting_down(ws, params):
            raise FakeRpcError(-32600, "server is shutting down", {"reason": "serverShuttingDown"})
        self.server.handlers["account/read"] = shutting_down
        with self.assertRaises(RpcError) as caught:
            await self.client.request("account/read", {})
        self.assertEqual(caught.exception.data, {"reason": "serverShuttingDown"})
        self.assertFalse(caught.exception.thread_missing)

    async def test_overloaded_requests_retry_with_backoff(self) -> None:
        await self.ready()
        attempts = []

        async def busy(ws, params):
            attempts.append(params)
            if len(attempts) < 3:
                raise FakeRpcError(-32001, "server overloaded")
            return {"account": None}
        self.server.handlers["account/read"] = busy
        self.assertEqual(await self.client.request("account/read", {}), {"account": None})
        self.assertEqual(len(attempts), 3)

    async def test_server_requests_are_always_declined(self) -> None:
        await self.ready()
        for method, reply in SERVER_REQUEST_REPLIES.items():
            with self.subTest(method):
                answer = await self.server.ask(method, {"threadId": "t", "turnId": "u"})
                self.assertEqual(answer["result"], reply)
        answer = await self.server.ask("account/chatgptAuthTokens/refresh", {})
        self.assertEqual(answer["error"]["code"], -32601)
        self.assertNotIn("result", answer)

    async def test_notifications_route_by_thread_and_server_requests_reach_the_turn(self) -> None:
        await self.ready()
        with self.client.subscribe("t1") as events, self.client.subscribe("t2") as other:
            await self.server.broadcast("item/started", {"threadId": "t1", "turnId": "u", "item": {"type": "reasoning"}})
            await self.server.broadcast("account/updated", {"authMode": None})
            self.assertEqual((await asyncio.wait_for(events.next(), 2)).method, "item/started")
            await self.server.ask("item/fileChange/requestApproval", {"threadId": "t1", "turnId": "u"})
            request = await asyncio.wait_for(events.next(), 2)
            self.assertTrue(request.server_request)
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(other.next(), 0.05)
        self.assertEqual([event.method for event in self.global_events], ["account/updated"])

    async def test_disconnect_fails_requests_and_turns_then_reinitializes(self) -> None:
        await self.ready()
        self.server.ignore.add("model/list")
        with self.client.subscribe("t1") as events:
            pending = asyncio.create_task(self.client.request("model/list", {}))
            await self.server.wait_for(lambda: "model/list" in self.server.methods())
            await self.server.drop()
            with self.assertRaises(ConnectionLost):
                await asyncio.wait_for(pending, 2)
            with self.assertRaises(ConnectionLost):
                await asyncio.wait_for(events.next(), 2)
        await self.server.wait_for(lambda: self.server.methods().count("initialize") == 2)
        await self.ready()
        self.assertEqual(self.client.generation, 2)
        self.server.ignore.clear()
        self.assertEqual((await self.client.request("model/list", {}))["data"][0]["id"], "gpt-test")

    async def test_unanswered_request_marks_unresponsive_and_reconnects(self) -> None:
        await self.ready()
        self.server.ignore.add("account/read")
        with self.assertRaises(TimeoutError):
            await self.client.request("account/read", {}, timeout=0.2)
        self.assertTrue(self.client.unresponsive)
        await self.server.wait_for(lambda: self.server.methods().count("initialize") == 2)
        await self.ready()
        self.assertFalse(self.client.unresponsive)

    async def test_close_stops_reconnecting(self) -> None:
        await self.ready()
        await self.client.close()
        self.assertEqual(self.client.phase, Phase.CLOSED)
        with self.assertRaises(ConnectionLost):
            await self.client.request("account/read", {})
        await asyncio.sleep(0.05)
        self.assertEqual(self.server.methods().count("initialize"), 1)
        self.assertFalse(await self.client.wait_ready(0.1))


if __name__ == "__main__":
    unittest.main()
