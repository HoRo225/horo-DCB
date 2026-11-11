import asyncio
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import discord

from .store import Store

if TYPE_CHECKING:
    from .chat import ChatRound


ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
MAX_CHUNK_CHARS = 4000
MAX_CHUNK_BYTES = 9000
FRAME_STEPS = 3
EDIT_INTERVAL = 1


class Status(Enum):
    WAITING = ("⏳", "等待中", 0x80848E)
    THINKING = ("💭", "思考中", 0x7F77DD)
    SEARCHING = ("🔍", "搜尋中", 0x7F77DD)
    READING = ("📖", "讀取中", 0x7F77DD)
    FAILED = ("❌", "回答失敗", 0xF23F43)

    def __init__(self, icon: str, title: str, colour: int) -> None:
        self.icon = icon
        self.title = title
        self.colour = colour

    def line(self) -> str:
        return f"{self.icon} - **{self.title}**"


class FailureReason(Enum):
    TIMEOUT = "逾時，請再問一次"
    DISCONNECTED = "連線中斷，請再問一次"
    SIGNED_OUT = "機器人尚未登入，請通知擁有者"
    EMPTY = "沒有產生回答，請再問一次"
    RESTARTING = "機器人重新啟動，請再問一次"
    ERROR = "發生錯誤，請再問一次"


@dataclass(frozen=True)
class Step:
    label: str
    text: str

    def line(self) -> str:
        return f"`{self.label}` {discord.utils.escape_markdown(' '.join(self.text.split()))}"


@dataclass(frozen=True)
class Waiting:
    pass


@dataclass(frozen=True)
class Frame:
    status: Status
    steps: tuple[Step, ...]
    started_at: int


@dataclass(frozen=True)
class Answer:
    text: str


@dataclass(frozen=True)
class Failure:
    reason: FailureReason


Content = Waiting | Frame | Answer | Failure
TOOL_STEPS = {"read_messages": Step("讀取", "頻道訊息"), "search_messages": Step("查詢", "頻道訊息"), "view_message_images": Step("看圖", "訊息圖片")}


def answer_chunks(text: str) -> list[str]:
    chunks = []
    while text:
        end = 0
        size = 0
        while end < len(text) and end < MAX_CHUNK_CHARS:
            size += len(text[end].encode())
            if size > MAX_CHUNK_BYTES:
                break
            end += 1
        if end < len(text):
            newline = text.rfind("\n", 0, end)
            if newline >= end // 2:
                end = newline + 1
        if text[:end].strip():
            chunks.append(text[:end])
        text = text[end:]
    return chunks


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summary_title(text: str) -> str:
    text = text.strip()
    match = re.match(r"\*\*(.+?)\*\*", text, re.S)
    return shorten(match.group(1) if match else text.split("\n", 1)[0], 100)


def search_text(item: dict) -> Step:
    action = item.get("action") or {}
    kind = action.get("type")
    if kind == "openPage" and action.get("url"):
        return Step("瀏覽", urlsplit(action["url"]).hostname or action["url"])
    if kind == "findInPage" and action.get("pattern"):
        return Step("尋找", shorten(action["pattern"], 80))
    query = action.get("query") or next(iter(action.get("queries") or ()), None) or item.get("query")
    return Step("搜尋", shorten(query, 80) if query else "網路")


def card(status: Status, *items: discord.ui.Item) -> discord.ui.Container:
    return discord.ui.Container(*items, accent_colour=status.colour)


def render_view(content: Content) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    match content:
        case Waiting():
            view.add_item(card(Status.WAITING, discord.ui.TextDisplay(Status.WAITING.line())))
        case Frame(status=status, steps=steps, started_at=started_at):
            lines = [status.line(), *(f"-# {step.line()}" for step in steps[:-1]), *(step.line() for step in steps[-1:])]
            view.add_item(card(status, discord.ui.TextDisplay("\n".join(lines)), discord.ui.Separator(), discord.ui.TextDisplay(f"-# 開始於 <t:{started_at}:R>")))
        case Answer(text=text):
            view.add_item(discord.ui.TextDisplay(text))
        case Failure(reason=reason):
            view.add_item(card(Status.FAILED, discord.ui.TextDisplay(f"{Status.FAILED.line()}\n`原因` {reason.value}")))
    return view


class MessageSlot:
    def __init__(self, round_state: "ChatRound", store: Store, previous: "MessageSlot | None", failure: asyncio.Future) -> None:
        self.round_state = round_state
        self.store = store
        self.previous = previous
        self.failure = failure
        self.message: discord.Message | None = None
        self.sent = asyncio.Event()
        self.synced = asyncio.Event()
        self.dirty = asyncio.Event()
        self.desired: Content | None = None
        self.last_edit = 0.0
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._done)

    def show(self, content: Content | None) -> None:
        if content != self.desired:
            self.desired = content
            self.synced.clear()
            self.dirty.set()

    def _reference(self) -> discord.MessageReference | None:
        above = self.previous.message if self.previous is not None else None
        if above is None:
            return self.round_state.message.to_reference(fail_if_not_exists=False)
        if self.round_state.message.channel.last_message_id == above.id:
            return None
        return above.to_reference(fail_if_not_exists=False)

    def _done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None and not self.failure.done():
            self.failure.set_exception(error)

    async def _run(self) -> None:
        if self.previous is not None:
            await self.previous.sent.wait()
        round_state = self.round_state
        while True:
            await self.dirty.wait()
            delay = self.last_edit + EDIT_INTERVAL - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.dirty.clear()
            content = self.desired
            if content is None:
                if self.message is not None:
                    try:
                        await self.message.delete()
                    except discord.NotFound:
                        pass
                    round_state.messages.remove(self.message)
                    await self.store.forget_messages([self.message.id])
                self.sent.set()
                self.synced.set()
                return
            self.last_edit = time.monotonic()
            if self.message is None:
                task = asyncio.create_task(round_state.message.channel.send(view=render_view(content), reference=self._reference(), allowed_mentions=ALLOWED_MENTIONS, mention_author=False))  # ty: ignore[no-matching-overload]
                round_state.pending_sends.add(task)
                sent: discord.Message = await asyncio.shield(task)
                self.message = sent
                round_state.messages.append(sent)
                round_state.pending_sends.discard(task)
                await self.store.record_message(sent.id, round_state.thread.id, round_state.turn.id if round_state.turn is not None else None)
                self.sent.set()
            else:
                await self.message.edit(view=render_view(content), allowed_mentions=ALLOWED_MENTIONS)
            if self.desired == content:
                self.synced.set()


class Renderer:
    def __init__(self, round_state: "ChatRound", store: Store, waiting: bool) -> None:
        self.round_state = round_state
        self.store = store
        self.waiting = waiting
        self.started_at = int(round_state.message.created_at.timestamp())
        self.failure = asyncio.get_running_loop().create_future()
        self.slots: list[MessageSlot] = []
        self.retired: list[MessageSlot] = []
        self.timeline: list[tuple] = []
        self.agents: dict[str, dict] = {}
        self.reasoning: dict[str, dict[int, str]] = {}
        self.searches: dict[str, dict] = {}
        self.tool_items: dict[str, dict] = {}
        self.tool_notes: dict[str, str] = {}
        self.open: set[str] = set()

    def _track(self, method: str, item_id: str) -> None:
        if method == "item/completed":
            self.open.discard(item_id)
        elif item_id not in self.open:
            for stale in self.open:
                self.agents.pop(stale, None)
                self.reasoning.pop(stale, None)
                self.searches.pop(stale, None)
            self.timeline = [key for key in self.timeline if key[0] == "reroute" or key[1] not in self.open]
            self.open = {item_id}

    def _mark(self, key: tuple) -> None:
        if key not in self.timeline:
            self.timeline.append(key)

    def _agent(self, item: dict, completed: bool) -> None:
        agent = self.agents.setdefault(item["id"], {"phase": None, "text": ""})
        agent["phase"] = item.get("phase")
        if completed:
            agent["text"] = item.get("text") or ""
        self._mark(("agent", item["id"]))

    def handle(self, method: str, params: dict) -> None:
        item = params.get("item") or {}
        kind = item.get("type")
        if method in ("item/started", "item/completed") and kind in ("agentMessage", "reasoning", "webSearch"):
            self._track(method, item["id"])
        if method in ("item/started", "item/completed") and kind == "agentMessage":
            self._agent(item, method == "item/completed")
        elif method == "turn/completed":
            for value in params["turn"].get("items", []):
                if value.get("type") == "agentMessage":
                    self._agent(value, True)
        elif method in ("item/started", "item/completed") and kind == "reasoning":
            parts = self.reasoning.setdefault(item["id"], {})
            if method == "item/completed":
                for index, text in enumerate(item.get("summary") or ()):
                    parts[index] = text
                    self._mark(("reasoning", item["id"], index))
        elif method in ("item/started", "item/completed") and kind == "webSearch":
            self.searches[item["id"]] = {"step": search_text(item), "done": method == "item/completed"}
            self._mark(("search", item["id"]))
        elif method in ("item/started", "item/completed") and kind == "dynamicToolCall":
            self.tool_items[item["id"]] = {"tool": item.get("tool") or "", "done": method == "item/completed", "success": item.get("success")}
            self._mark(("tool", item["id"]))
        elif method == "item/agentMessage/delta":
            self.agents.setdefault(params["itemId"], {"phase": None, "text": ""})["text"] += params.get("delta", "")
            self._mark(("agent", params["itemId"]))
        elif method in ("item/reasoning/summaryPartAdded", "item/reasoning/summaryTextDelta"):
            parts = self.reasoning.setdefault(params["itemId"], {})
            index = params.get("summaryIndex", 0)
            parts[index] = parts.get(index, "") + params.get("delta", "")
            self._mark(("reasoning", params["itemId"], index))
        elif method == "model/rerouted":
            self._mark(("reroute", params.get("fromModel") or "", params.get("toModel") or ""))
        else:
            return
        self.refresh()

    def _answer_keys(self) -> set[str]:
        keys = [key[1] for key in self.timeline if key[0] == "agent"]
        finals = {key for key in keys if self.agents[key]["phase"] == "final_answer"}
        if finals:
            return finals
        if keys and self.agents[keys[-1]]["phase"] is None:
            return {keys[-1]}
        return set()

    def answer_text(self) -> str:
        answers = self._answer_keys()
        return "\n\n".join(self.agents[key[1]]["text"] for key in self.timeline if key[0] == "agent" and key[1] in answers)

    def _tool_step(self, call_id: str) -> Step:
        tool = self.tool_items[call_id]
        pending = TOOL_STEPS.get(tool["tool"], Step("工具", tool["tool"]))
        if not tool["done"]:
            return pending
        return Step(pending.label, self.tool_notes.get(call_id) or (pending.text if tool["success"] else "失敗，發生錯誤"))

    def _frame(self) -> Frame:
        answers = self._answer_keys()
        steps = []
        for key in self.timeline:
            if key[0] == "agent" and key[1] not in answers and self.agents[key[1]]["text"].strip():
                steps.append(Step("說明", shorten(self.agents[key[1]]["text"], 100)))
            elif key[0] == "reasoning" and self.reasoning[key[1]].get(key[2], "").strip():
                steps.append(Step("思考", summary_title(self.reasoning[key[1]][key[2]])))
            elif key[0] == "search":
                steps.append(self.searches[key[1]]["step"])
            elif key[0] == "tool":
                steps.append(self._tool_step(key[1]))
            elif key[0] == "reroute":
                steps.append(Step("模型", f"{key[1]} 改用 {key[2]}"))
        if any(not tool["done"] for tool in self.tool_items.values()):
            status = Status.READING
        elif any(not search["done"] for search in self.searches.values()):
            status = Status.SEARCHING
        else:
            status = Status.THINKING
        return Frame(status, tuple(steps[-FRAME_STEPS:]), self.started_at)

    def refresh(self) -> None:
        contents: list[Content]
        if self.waiting:
            contents = [Waiting()]
        else:
            contents = [Answer(chunk) for chunk in answer_chunks(self.answer_text())] or [self._frame()]
        while len(self.slots) < len(contents):
            self.slots.append(MessageSlot(self.round_state, self.store, self.slots[-1] if self.slots else None, self.failure))
        for slot, content in zip(self.slots, contents):
            slot.show(content)
        for slot in self.slots[len(contents):]:
            slot.show(None)
            self.retired.append(slot)
        del self.slots[len(contents):]

    async def guard(self, awaitable):
        task = asyncio.ensure_future(awaitable)
        try:
            await asyncio.wait({task, self.failure}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            if not task.done():
                task.cancel()
        if self.failure.done():
            self.failure.result()
        return task.result()

    async def flush(self) -> None:
        await self.guard(asyncio.gather(*(slot.synced.wait() for slot in [*self.slots, *self.retired])))

    async def close(self) -> None:
        tasks = [slot.task for slot in [*self.slots, *self.retired]]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.failure.done() and not self.failure.cancelled():
            self.failure.exception()
