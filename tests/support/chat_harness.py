import asyncio
import base64
import io
from collections import deque
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

from horo_dcb import render as render_module
from horo_dcb.chat import Chat
from horo_dcb.store import Store
from tests.support.app_server import FakeAppServer
from tests.support.codex_messages import answer
from tests.support.discord_fakes import CHANNEL_ID, Channel, attachment, member, view_text
from tests.support.waiting import until

ROLE = 300
INSTRUCTIONS = "回答要簡短"

Step = dict | asyncio.Event | Callable[[], Awaitable[object]]
Script = Callable[[str, str], list[Step]]


def default_answer(turn_id: str) -> str:
    return f"{turn_id} 的回答"


def png(colour: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), colour).save(buffer, "PNG")
    return buffer.getvalue()


def data_url(data: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def picture(data: bytes, attachment_id: int) -> Mock:
    return attachment(data, f"{attachment_id}.png", "image/png", attachment_id=attachment_id)


def server_request(method: str, **params) -> dict:
    return {"id": 0, "method": method, "params": params}


def input_text(params: dict) -> str:
    return params["input"][0]["text"]


def context_text(params: dict) -> str:
    return "\n".join(value["value"] for value in params.get("additionalContext", {}).values())


class ChatHarness:
    def __init__(self, server: FakeAppServer, codex, store: Store) -> None:
        self.server = server
        self.store = store
        self.channel = Channel()
        self.bot = self.channel.bot
        self.chat = Chat(SimpleNamespace(user=self.bot, store=store, codex=codex, http=Mock()))
        self.scripts: deque[Script] = deque()
        self.log: list[tuple[str, str]] = []
        self.replies: list[dict] = []
        self.plays: set[asyncio.Task] = set()
        self.start_reply: Callable[[dict], object] = server.default_turn_start
        server.handlers["turn/start"] = self._start
        server.after["turn/start"] = self._play

    def person(self, user_id: int = 1, name: str = "提問者", role_ids: tuple[int, ...] = (ROLE,)) -> Mock:
        return member(user_id, name, role_ids)

    def post(self, author: Mock, text: str = "", *, reply_to: int | None = None, mention: bool = False, attachments: tuple[Mock, ...] = (), channel: Channel | None = None) -> Mock:
        self.channel.next_sent_id += 1
        content = f"<@{self.bot.id}> {text}" if mention else text
        return (channel or self.channel).post(self.channel.next_sent_id, author, content, reply_to=reply_to, mentions=(self.bot,) if mention else (), attachments=attachments)

    async def ask(self, author: Mock, text: str, **options) -> Mock:
        message = self.post(author, text, **options)
        await self.chat.receive(message)
        return message

    def script(self, build: Script) -> None:
        self.scripts.append(build)

    async def settle(self) -> None:
        await until(lambda: not self.chat.rounds and not self.chat.drains)

    def answers(self, trigger: Mock) -> list[Mock]:
        return [value for value in self.channel.live() if value.reference is not None and value.reference.message_id == trigger.id]

    def text(self, message: Mock) -> str:
        return view_text(message.views[-1])

    def answer_text(self, trigger: Mock) -> str:
        return "".join(self.text(value) for value in self.answers(trigger))

    def _start(self, params: dict) -> object:
        result = self.start_reply(params)
        if isinstance(result, dict):
            self.log.append(("turn/start", result["turn"]["id"]))
        return result

    async def _play(self, params: dict, result: dict) -> None:
        task = asyncio.current_task()
        self.plays.add(task)
        try:
            build = self.scripts.popleft() if self.scripts else lambda thread, turn: answer(thread, turn, default_answer(turn))
            for step in build(params["threadId"], result["turn"]["id"]):
                if isinstance(step, asyncio.Event):
                    await step.wait()
                elif callable(step):
                    await step()
                elif "id" in step:
                    reply = await self.server.call(step["method"], step["params"])
                    self.replies.append(await reply)
                else:
                    if step["method"] == "turn/completed":
                        self.log.append(("turn/completed", step["params"]["turn"]["id"]))
                    await self.server.send(step)
        finally:
            self.plays.discard(task)


@pytest.fixture
async def harness(app_server, codex, tmp_path, monkeypatch):
    monkeypatch.setattr(render_module, "EDIT_INTERVAL", 0)
    store = Store(tmp_path)
    await store.open()
    await store.update_settings(ai_channel_ids=[CHANNEL_ID], allowed_role_ids=[ROLE], instructions=INSTRUCTIONS)
    value = ChatHarness(app_server, codex, store)
    yield value
    async with asyncio.timeout(5):
        await value.chat.shutdown()
    plays = tuple(value.plays)
    for task in plays:
        task.cancel()
    await asyncio.gather(*plays, return_exceptions=True)
