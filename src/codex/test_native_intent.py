from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox, TextInput
from openai_codex.generated.v2_all import ReasoningEffort, ThreadLoadedListResponse

from src.codex.bridge import _CONFIG_OVERRIDES
from src.codex.contracts import parse_intent_json
from src.codex.sdk import CallbackGuard, feature_config, intent_schema, request, unsubscribe_thread
from src.codex.turns import collect_turn


class NativeIntentTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_cli_isolates_planner_and_restores_chat_tools(self) -> None:
        requests = []

        async def respond(request):
            body = await request.json()
            requests.append(body)
            answer = '{"kind":"chat","actions":[],"question":""}'
            message = {"type": "message", "id": f"msg-{len(requests)}", "role": "assistant",
                       "phase": "final_answer", "status": "completed",
                       "content": [{"type": "output_text", "text": answer, "annotations": []}]}
            response = {"id": f"resp-{len(requests)}", "object": "response", "status": "completed",
                        "output": [message], "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}}
            events = [
                {"type": "response.created", "response": response | {"status": "in_progress", "output": []}},
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.completed", "response": response},
            ]
            return web.Response(text="".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events), content_type="text/event-stream")

        app = web.Application()
        app.router.add_post("/v1/responses", respond)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name) / "codex-home"
        home.mkdir()
        overrides = _CONFIG_OVERRIDES + (
            'model_provider="fixture"', 'model="gpt-5.3-codex"',
            'model_providers.fixture.name="Local regression fixture"',
            f'model_providers.fixture.base_url="http://127.0.0.1:{port}/v1"',
            'model_providers.fixture.wire_api="responses"',
            "model_providers.fixture.requires_openai_auth=false",
        )
        codex = AsyncCodex(CodexConfig(cwd=directory.name, env={"CODEX_HOME": str(home)}, config_overrides=overrides))
        guard = CallbackGuard(codex)
        self.assertTrue(guard.available)
        async with asyncio.timeout(20), codex:
            process = codex._client._sync._proc
            self.addCleanup(lambda: [stream.close() for stream in (process.stdout, process.stderr) if stream])
            isolated = await codex.thread_start(model="gpt-5.3-codex", ephemeral=True, cwd=directory.name,
                                                approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only,
                                                config=feature_config(isolated=True), base_instructions="Return the JSON schema, no tools.")
            ticket = guard.begin()
            ticket.thread_id = isolated.id
            try:
                handle = await isolated.turn([TextInput("你好")], model="gpt-5.3-codex", effort=ReasoningEffort.low, output_schema=intent_schema())
                result = await collect_turn(handle, isolated=True)
                self.assertFalse(result.unknown)
                self.assertEqual(parse_intent_json(result.text).kind, "chat")
                self.assertFalse(ticket.violated, ticket.reason)
            finally:
                guard.end(ticket)
            loaded = await request(codex, "thread/loaded/list", ThreadLoadedListResponse, params={})
            self.assertIn(isolated.id, loaded.data)
            self.assertEqual(await unsubscribe_thread(codex, isolated.id), "unsubscribed")
            self.assertEqual(await unsubscribe_thread(codex, isolated.id), "notSubscribed")
            normal = await codex.thread_start(model="gpt-5.3-codex", cwd=directory.name,
                                              approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only,
                                              config=feature_config())
            handle = await normal.turn([TextInput("hello")], model="gpt-5.3-codex", effort=ReasoningEffort.low)
            self.assertTrue((await collect_turn(handle)).completed)
            for callback in (False, True):
                injected = guard.begin()
                injected.thread_id = isolated.id
                if callback:
                    self.assertEqual(codex._client._sync._approval_handler("unknown/serverRequest", {}), {})
                else:
                    codex._client._sync._coerce_notification("unknown/globalNotification", {})
                self.assertTrue(injected.violated)
                guard.end(injected)
        self.assertEqual(len(requests), 2)
        self.assertEqual({tool.get("name", tool.get("type")) for tool in requests[0].get("tools", [])}, {"request_user_input"})
        planner_tools = json.dumps(requests[0].get("tools", []))
        normal_tools = json.dumps(requests[1].get("tools", []))
        self.assertNotIn("web_search", planner_tools)
        self.assertNotIn("view_image", planner_tools)
        self.assertNotIn('"name": "exec"', planner_tools)
        self.assertIn("web_search", normal_tools)
        self.assertIn("view_image", normal_tools)
        self.assertFalse((home / "auth.json").exists())
