import asyncio
import json
import re
import sys

import aiohttp

from horo_dcb import tools

SOCKET = "/run/codex/app-server.sock"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
ID_KEYS = {"id", "threadId", "turnId", "itemId", "callId", "lastTurnId", "expectedTurnId", "clientUserMessageId", "parentThreadId", "forkedFromId"}
REDACTED_KEYS = ("path", "cwd", "userAgent", "codexHome", "instructions", "serverName")
TOOL_RESULT = json.dumps({"messages": [], "next_before": None})


class Recorder:
    def __init__(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        self.websocket = websocket
        self.aliases: dict[str, str] = {}
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.silent: set[int] = set()
        self.events: asyncio.Queue = asyncio.Queue()
        self.scenario = "setup"
        self.reader = asyncio.create_task(self.read())

    def alias(self, value: str) -> str:
        if value not in self.aliases:
            self.aliases[value] = f"id-{len(self.aliases) + 1}"
        return self.aliases[value]

    def scrub(self, value, key: str | None = None):
        if isinstance(value, dict):
            return {name: self.scrub(item, name) for name, item in value.items()}
        if isinstance(value, list):
            return [self.scrub(item) for item in value]
        if isinstance(value, str):
            if key is not None and any(part.lower() in key.lower() for part in REDACTED_KEYS):
                return "<redacted>"
            if key in ID_KEYS:
                return self.alias(value)
            return UUID.sub(lambda match: self.alias(match.group(0)), value)
        return value

    def write(self, direction: str, message: dict) -> None:
        if str(message.get("method", "")).startswith("account/"):
            return
        print(json.dumps({"scenario": self.scenario, "direction": direction, "message": self.scrub(message)}, ensure_ascii=False), flush=True)

    async def send(self, message: dict) -> None:
        self.write("client", message)
        await self.websocket.send_json(message)

    async def read(self) -> None:
        async for frame in self.websocket:
            if frame.type != aiohttp.WSMsgType.TEXT:
                break
            message = frame.json()
            if "method" not in message and "id" in message:
                if message["id"] in self.pending:
                    if message["id"] not in self.silent:
                        self.write("server", message)
                    self.pending.pop(message["id"]).set_result(message)
                continue
            self.write("server", message)
            await self.events.put(message)

    async def request(self, method: str, params: dict | None = None, record: bool = True) -> dict:
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        message = {"id": self.next_id, "method": method, "params": params or {}}
        self.pending[self.next_id] = future
        if record:
            await self.send(message)
        else:
            self.silent.add(self.next_id)
            await self.websocket.send_json(message)
        response = await asyncio.wait_for(future, 60)
        if "error" in response:
            raise RuntimeError(f"{method} failed: {response['error']}")
        return response["result"]

    async def turn(self, thread_id: str, text: str, model: str) -> None:
        params = {"threadId": thread_id, "input": [{"type": "text", "text": text, "text_elements": []}], "model": model, "effort": "low", "summary": "auto", "clientUserMessageId": "1"}
        result = await self.request("turn/start", params)
        turn_id = result["turn"]["id"]
        while True:
            message = await asyncio.wait_for(self.events.get(), 180)
            if message.get("method") == "item/tool/call" and "id" in message:
                await self.send({"id": message["id"], "result": {"contentItems": [{"type": "inputText", "text": TOOL_RESULT}], "success": True}})
            if message.get("method") == "turn/completed" and message["params"]["turn"]["id"] == turn_id:
                return


async def main() -> None:
    connector = aiohttp.UnixConnector(path=SOCKET)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.ws_connect("http://localhost/", max_msg_size=0) as websocket:
            recorder = Recorder(websocket)
            await recorder.request("initialize", {"clientInfo": {"name": "horo_dcb_recorder", "title": "horo-DCB recorder", "version": "0.1.0"}, "capabilities": {"experimentalApi": True, "requestAttestation": False}})
            await websocket.send_json({"method": "initialized"})
            models = (await recorder.request("model/list", {"includeHidden": False, "cursor": None}, record=False))["data"]
            model = next((value["id"] for value in models if value.get("isDefault")), models[0]["id"])
            created: list[str] = []
            try:
                recorder.scenario = "answer"
                answer = (await recorder.request("thread/start", {"ephemeral": False, "approvalPolicy": "never", "sandbox": "read-only", "model": model}))["thread"]["id"]
                created.append(answer)
                await recorder.turn(answer, "用一句話回答：一週有幾天？", model)
                turns = (await recorder.request("thread/read", {"threadId": answer, "includeTurns": True}, record=False))["thread"]["turns"]

                recorder.scenario = "tool"
                tool = (await recorder.request("thread/start", {"ephemeral": False, "approvalPolicy": "never", "sandbox": "read-only", "model": model, "dynamicTools": tools.SPEC}))["thread"]["id"]
                created.append(tool)
                await recorder.turn(tool, "請先呼叫 discord 的 read_messages 工具讀取最近的訊息，再用一句話說你讀到幾則訊息。", model)

                recorder.scenario = "fork"
                fork = (await recorder.request("thread/fork", {"threadId": answer, "lastTurnId": turns[-1]["id"], "ephemeral": False, "approvalPolicy": "never", "sandbox": "read-only", "excludeTurns": True, "model": model}))["thread"]["id"]
                created.insert(0, fork)
                await recorder.turn(fork, "用一句話回答：我剛剛問了你什麼？", model)
            finally:
                recorder.scenario = "cleanup"
                for thread_id in created:
                    try:
                        await recorder.request("thread/delete", {"threadId": thread_id}, record=False)
                    except Exception as error:
                        print(f"cleanup failed: {error}", file=sys.stderr)
                recorder.reader.cancel()


if __name__ == "__main__":
    asyncio.run(main())
