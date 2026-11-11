import asyncio
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import discord

from .store import Store

if TYPE_CHECKING:
    from .chat import ChatRound


ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
TOOL_ICONS = {"read_messages": "📖", "search_messages": "🔎", "view_message_images": "🖼️"}
TOOL_PENDING = {"read_messages": "讀取頻道訊息", "search_messages": "搜尋頻道訊息", "view_message_images": "查看圖片"}
WAITING_TEXT = "⏳ 等上一則回答完成後接著回答"
MAX_CHUNK_CHARS = 4000
MAX_CHUNK_BYTES = 9000
FRAME_COLOUR = discord.Colour(0x7F77DD)
MAX_FRAME_CHARS = 1500
EDIT_INTERVAL = 1


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


def search_text(item: dict) -> str:
    action = item.get("action") or {}
    kind = action.get("type")
    if kind == "openPage" and action.get("url"):
        return f"瀏覽 {urlsplit(action['url']).hostname or action['url']}"
    if kind == "findInPage" and action.get("pattern"):
        return f"在網頁中尋找「{shorten(action['pattern'], 80)}」"
    query = action.get("query") or next(iter(action.get("queries") or ()), None) or item.get("query")
    return f"搜尋「{shorten(query, 80)}」" if query else "搜尋網路"


def render_view(content: tuple[str, str]) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    kind, text = content
    if kind == "frame":
        view.add_item(discord.ui.Container(discord.ui.TextDisplay(text), accent_colour=FRAME_COLOUR))
    else:
        view.add_item(discord.ui.TextDisplay(text))
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
        self.desired: tuple[str, str] | None = None
        self.last_edit = 0.0
        self.task = asyncio.create_task(self._run())
        self.task.add_done_callback(self._done)

    def show(self, content: tuple[str, str] | None) -> None:
        if content != self.desired:
            self.desired = content
            self.synced.clear()
            self.dirty.set()

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
                task = asyncio.create_task(round_state.message.channel.send(view=render_view(content), reference=round_state.message.to_reference(fail_if_not_exists=False), allowed_mentions=ALLOWED_MENTIONS, mention_author=False))
                round_state.pending_sends.add(task)
                self.message = await asyncio.shield(task)
                round_state.messages.append(self.message)
                round_state.pending_sends.discard(task)
                await self.store.record_message(self.message.id, round_state.thread.id, round_state.turn.id if round_state.turn is not None else None)
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
            self.searches[item["id"]] = {"text": search_text(item), "done": method == "item/completed"}
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

    def _tool_entry(self, call_id: str) -> tuple[str, str]:
        tool = self.tool_items[call_id]
        icon = TOOL_ICONS.get(tool["tool"], "🧰")
        label = TOOL_PENDING.get(tool["tool"], "使用工具")
        if not tool["done"]:
            return icon, f"{label}中"
        return icon, self.tool_notes.get(call_id) or (label if tool["success"] else f"{label}失敗")

    def _frame(self) -> str:
        answers = self._answer_keys()
        entries = []
        for key in self.timeline:
            if key[0] == "agent" and key[1] not in answers and self.agents[key[1]]["text"].strip():
                entries.append(("💬", shorten(self.agents[key[1]]["text"], 200)))
            elif key[0] == "reasoning" and self.reasoning[key[1]].get(key[2], "").strip():
                entries.append(("💭", summary_title(self.reasoning[key[1]][key[2]])))
            elif key[0] == "search":
                entries.append(("🔍", self.searches[key[1]]["text"]))
            elif key[0] == "tool":
                entries.append(self._tool_entry(key[1]))
            elif key[0] == "reroute":
                entries.append(("🔀", f"模型已由 {key[1]} 改用 {key[2]}"))
        if any(not tool["done"] for tool in self.tool_items.values()):
            status = "📖 **查看頻道中**"
        elif any(not search["done"] for search in self.searches.values()):
            status = "🔍 **搜尋中**"
        else:
            status = "💭 **思考中**"
        header = f"{status} · <t:{self.started_at}:R>"
        lines = []
        size = len(header)
        for index, (icon, text) in enumerate(reversed(entries)):
            text = discord.utils.escape_markdown(text)
            line = f"{icon} **{text}**" if index == 0 else f"{icon} {text}"
            if size + len(line) + 1 > MAX_FRAME_CHARS:
                lines.append("…")
                break
            lines.append(line)
            size += len(line) + 1
        return "\n".join([header, *reversed(lines)])

    def refresh(self) -> None:
        if self.waiting:
            contents = [("frame", WAITING_TEXT)]
        else:
            contents = [("answer", chunk) for chunk in answer_chunks(self.answer_text())] or [("frame", self._frame())]
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
