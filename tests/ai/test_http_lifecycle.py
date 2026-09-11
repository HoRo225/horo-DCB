import asyncio
from dataclasses import asdict
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import discord
from aiohttp import ClientSession, TCPConnector
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.web_log import AccessLogger

import src.ai.bridge as bridge

from src.bot import HoroBot
from src.ai.discord import codex_error_text
from src.ai.protocol import BridgeRequestError
from src.ai.runtime import CodexService, ThreadStore
from src.ai.bridge import create_app
from src.ai.client import CodexBridgeClient
from src.ai.protocol import CodexBridgeError
from tests.support.access import configured_access

from tests.support.ai import settle, stop_tasks, ControlledCodex, StatusService, Typing


class SafeHttpLifecycleTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = StatusService()
        self.token = "a" * 64
        self.http = TestClient(TestServer(create_app(self.token, self.service)))
        await self.http.start_server()
        self.client = CodexBridgeClient(str(self.http.make_url("")).rstrip("/"), self.token)

    async def asyncTearDown(self):
        await asyncio.wait_for(self.client.close(), 1)
        await asyncio.wait_for(self.http.close(), 1)

    async def test_non_ascii_authorization_returns_fixed_401(self):
        response = await self.http.get("/v1/status", headers={"Authorization": "Bearer 非 ASCII"})
        self.assertEqual(response.status, 401)
        self.assertEqual(await response.json(), {"error": "unauthorized"})

    async def test_busy_survives_server_and_client_safe_code_filters(self):
        self.service.error = BridgeRequestError("busy", 429)
        with self.assertRaises(CodexBridgeError) as caught:
            await self.client.chat("guild:1:thread:2", "one", ())
        self.assertEqual(caught.exception.code, "busy")
        self.assertNotEqual(codex_error_text("busy"), codex_error_text("unavailable"))

    async def test_missing_optional_status_fields_default_safely(self):
        status = asdict(await self.client.get_runtime_status())
        self.assertEqual(status.get("active_requests"), 0)
        self.assertEqual(status.get("queued_requests"), 0)
        self.assertIsNone(status.get("last_error"))

    async def test_optional_status_fields_reject_unsafe_counters_and_errors(self):
        self.service.status_data.update({
            "active_requests": True, "queued_requests": -1,
            "last_error": "private-account@example.com raw rpc data",
        })
        status = asdict(await self.client.get_runtime_status())
        self.assertEqual(status.get("active_requests"), 0)
        self.assertEqual(status.get("queued_requests"), 0)
        self.assertNotIn("private-account", repr(status))
        self.assertNotIn("raw rpc", repr(status))
        self.service.status_data.update({"active_requests": 2, "queued_requests": 4, "last_error": "busy"})
        status = asdict(await self.client.get_runtime_status())
        self.assertEqual((status.get("active_requests"), status.get("queued_requests")), (2, 4))
        self.assertEqual(status.get("last_error"), "busy")


class RuntimeHttpSafetyTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def launch_options(app):
        # Exercise the serving entry point, then use its actual aiohttp options
        # against loopback HTTP. No assertion merely checks a keyword exists.
        with patch.object(bridge, "_runtime_app", return_value=app), patch.object(
            bridge.web, "run_app", Mock(),
        ) as launch, patch.object(sys, "argv", ["codex-bridge", "serve"]):
            bridge.main()
        return launch.call_args.kwargs

    async def test_http_disconnect_cancels_and_interrupts_only_the_accepted_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            codex = ControlledCodex()
            codex.gates["run"] = asyncio.Event()
            service = CodexService(codex, ThreadStore(Path(directory) / "threads.json"), timeout_seconds=5)
            exits = []
            service.fatal_exit = exits.append
            app = create_app("a" * 64, service)
            options = self.launch_options(app)
            runner = bridge.web.AppRunner(app, handler_cancellation=options.get("handler_cancellation", False))
            await runner.setup()
            await bridge.web.TCPSite(runner, "127.0.0.1", 0).start()
            base_url = f"http://127.0.0.1:{runner.addresses[0][1]}"
            http = ClientSession()
            request = asyncio.create_task(http.post(base_url + "/v1/chat", json={
                "conversation_key": "guild:1:thread:2",
                "text": "private prompt", "images": [],
            }, headers={"Authorization": "Bearer " + "a" * 64}))
            try:
                await asyncio.wait_for(codex.entered["run"].wait(), 0.3)
                request.cancel()
                await asyncio.wait_for(asyncio.gather(request, return_exceptions=True), 0.3)
                try:
                    await asyncio.wait_for(codex.entered["interrupt"].wait(), 0.3)
                except TimeoutError:
                    self.fail("HTTP disconnect did not cancel the SDK turn")
                await settle()
                self.assertEqual(codex.interrupted, ["thread-1"])
                self.assertEqual(len(codex.started), 1)
                self.assertEqual(exits, [])
                status = await asyncio.wait_for(service.status(), 0.3)
                self.assertEqual(status["active_requests"], 0)
                self.assertEqual(status["queued_requests"], 0)
            finally:
                codex.gates["run"].set()
                await stop_tasks([request])
                await asyncio.wait_for(http.close(), 1)
                await asyncio.wait_for(runner.cleanup(), 1)

    async def test_bot_real_http_bridge_and_sdk_keep_shared_thread_output_serialized(self):
        with tempfile.TemporaryDirectory() as directory:
            codex = ControlledCodex()
            store = ThreadStore(Path(directory) / "threads.json")
            service = CodexService(codex, store, timeout_seconds=1)
            exits = []
            service.fatal_exit = exits.append
            http = TestClient(TestServer(create_app("a" * 64, service)))
            await http.start_server()
            client = CodexBridgeClient(str(http.make_url("")).rstrip("/"), "a" * 64, cooldown_seconds=0)
            access = configured_access(True, 10, channel_ids=(20,), role_ids=())
            access.set_roles(10, frozenset({70}))
            bot = HoroBot(client, access, SimpleNamespace(), SimpleNamespace(close=AsyncMock()),
                          SimpleNamespace(close=AsyncMock()), ai_text_display_enabled=False)
            bot._connection.user = SimpleNamespace(id=99)
            members = {}
            guild = SimpleNamespace(id=10, get_member=members.get)
            guild.fetch_member = AsyncMock(side_effect=members.get)
            output_entered = asyncio.Event()
            output_release = asyncio.Event()
            delivered = []
            tasks = []
            def message(user_id):
                author = SimpleNamespace(id=user_id, display_name="Member", bot=False,
                                         roles=[SimpleNamespace(id=70)])
                members[user_id] = author
                async def reply(content=None, **_kwargs):
                    if user_id == 30:
                        output_entered.set()
                        await output_release.wait()
                    delivered.append(content)
                return SimpleNamespace(
                    author=author, guild=guild, webhook_id=None,
                    channel=SimpleNamespace(id=21, parent_id=20, type=discord.ChannelType.public_thread,
                                            typing=Typing, send=AsyncMock()),
                    content="<@99> hello", mentions=[SimpleNamespace(id=99)],
                    attachments=[], reference=None, reply=reply,
                )
            try:
                first = asyncio.create_task(bot.on_message(message(30)))
                tasks.append(first)
                await asyncio.wait_for(output_entered.wait(), 1)
                second = asyncio.create_task(bot.on_message(message(31)))
                tasks.append(second)
                await settle()
                self.assertEqual(codex.runs, ["thread-1"])
                status = await asyncio.wait_for(client.get_runtime_status(), 1)
                self.assertEqual((status.active_requests, status.queued_requests), (0, 0))
                self.assertEqual((status.bot_active_requests, status.bot_queued_requests), (1, 1))
                output_release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 1)
                self.assertEqual(delivered, ["answer", "answer"])
                self.assertEqual(exits, [])
                self.assertEqual(codex.runs, ["thread-1", "thread-1"])
                self.assertEqual(len(codex.started), 1)
                self.assertEqual([entry[0] for entry in codex.resumed], ["thread-1"])
                self.assertEqual(store.get("guild:10:thread:21"), "thread-1")
                status = await asyncio.wait_for(client.get_runtime_status(), 1)
                self.assertEqual((status.bot_active_requests, status.bot_queued_requests), (0, 0))
            finally:
                output_release.set()
                await stop_tasks(tasks)
                await asyncio.wait_for(bot.close(), 1)
                await asyncio.wait_for(http.close(), 1)

    async def test_successful_health_probes_are_quiet_but_failure_and_recovery_are_logged(self):
        service = StatusService()
        app = create_app("a" * 64, service)
        options = self.launch_options(app)
        logger = logging.getLogger("horo.tests.health.access")
        # TestServer discards constructor runner options and forces cancellation.
        # Use the same native runner path as serve so main's options reach HTTP.
        runner = bridge.web.AppRunner(
            app, access_log=logger,
            access_log_class=options.get("access_log_class", AccessLogger),
        )
        await runner.setup()
        await bridge.web.TCPSite(runner, "127.0.0.1", 0).start()
        base_url = f"http://127.0.0.1:{runner.addresses[0][1]}"
        http = ClientSession(connector=TCPConnector(force_close=True))
        async def probe(path="/healthz"):
            response = await http.get(base_url + path)
            await response.read()
            await settle()
            return response
        try:
            with self.assertLogs(level=logging.INFO) as logs:
                first = await probe()
                self.assertEqual(first.status, 200)
                initial = len(logs.output)
                await probe()
                self.assertEqual(len(logs.output), initial, "unchanged successful health probe was logged")
                service.status_data["available"] = False
                failed = await probe()
                self.assertEqual(failed.status, 503)
                self.assertGreater(len(logs.output), initial)
                self.assertIn("503", "\n".join(logs.output))
                failure_logs = len(logs.output)
                service.status_data["available"] = True
                recovered = await probe()
                self.assertEqual(recovered.status, 200)
                self.assertGreater(len(logs.output), failure_logs, "health recovery was hidden")
                recovered_logs = len(logs.output)
                await probe()
                self.assertEqual(len(logs.output), recovered_logs)
                unauthorized = await probe("/v1/status")
                self.assertEqual(unauthorized.status, 401)
                self.assertGreater(len(logs.output), recovered_logs)
                self.assertNotIn("private", "\n".join(logs.output))
        finally:
            await asyncio.wait_for(http.close(), 1)
            await asyncio.wait_for(runner.cleanup(), 1)


if __name__ == "__main__":
    unittest.main()
