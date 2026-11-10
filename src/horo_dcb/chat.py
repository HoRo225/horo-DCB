import asyncio
import base64
import io
import re
import time
import warnings
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

import aiohttp
import discord
from PIL import Image, UnidentifiedImageError

from . import log_event, tools
from .codex import CodexError, ThreadState, ToolCall, TurnState, choose_model
from .store import Entry, Store


ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
TAIPEI = timezone(timedelta(hours=8))
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
TOOL_IMAGE_LABEL = "〔工具附上的圖片〕"
TOOL_ICONS = {"read_messages": "📖", "search_messages": "🔎", "view_message_images": "🖼️"}
TOOL_PENDING = {"read_messages": "讀取頻道訊息", "search_messages": "搜尋頻道訊息", "view_message_images": "查看圖片"}
WAITING_TEXT = "⏳ 等上一則回答完成後接著回答"
STEER_LEAD = "原提問者在你回答途中送出以下補充。請立刻依照補充內容重新回答這次的問題，補充優先於原本的要求："

Action = Literal["new", "continue", "queue", "steer", "fork"]
Entered = Literal["new", "continue", "queue", "steered", "fork"]


def route(*, author: int, bot_reply: bool, active_asker: int | None, entry: Entry | None, latest: str | None, busy: bool) -> Action:
    if not bot_reply:
        return "new"
    if active_asker is not None:
        return "steer" if active_asker == author else "queue"
    if entry is None:
        return "new"
    if entry.turn is None or latest is None:
        return "queue" if busy else "continue"
    return "continue" if entry.turn == latest and not busy else "fork"


def image_input(data: bytes) -> dict | None:
    try:
        with warnings.catch_warnings(record=True):
            with Image.open(io.BytesIO(data)) as image:
                mime = IMAGE_TYPES.get(image.format)
                if mime is None or (image.format == "GIF" and getattr(image, "is_animated", False)):
                    return None
        return {"type": "image", "url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def component_texts(components) -> list[str]:
    texts = []
    for component in components:
        if isinstance(component, discord.TextDisplay):
            texts.append(component.content)
        texts.extend(component_texts(getattr(component, "children", ())))
    return texts


def message_text(message: discord.Message) -> str:
    return "\n".join(text for text in [message.clean_content, *component_texts(message.components)] if text.strip())


MAX_CHUNK_CHARS = 4000
MAX_CHUNK_BYTES = 9000


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


MAX_CONTEXT_BYTES = 3800


def context_parts(text: str) -> list[str]:
    parts = []
    current = ""
    size = 0
    for char in text:
        width = len(char.encode())
        if size + width > MAX_CONTEXT_BYTES:
            parts.append(current)
            current = "（續）\n"
            size = len(current.encode())
        current += char
        size += width
    parts.append(current)
    return parts


FRAME_COLOUR = discord.Colour(0x7F77DD)
MAX_FRAME_CHARS = 1500


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
        if not task.cancelled() and task.exception() is not None and not self.failure.done():
            self.failure.set_exception(task.exception())

    async def _run(self) -> None:
        if self.previous is not None:
            await self.previous.sent.wait()
        round_state = self.round_state
        while True:
            await self.dirty.wait()
            delay = self.last_edit + 1 - time.monotonic()
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


@dataclass(eq=False)
class ChatRound:
    message: discord.Message
    received_at: float
    referenced: discord.Message | None = None
    source: str | None = None
    task: asyncio.Task | None = None
    thread: ThreadState | None = None
    turn: TurnState | None = None
    renderer: Renderer | None = None
    cleaning: bool = False
    messages: list[discord.Message] = field(default_factory=list)
    pending_sends: set[asyncio.Task] = field(default_factory=set)
    tool_tasks: set[asyncio.Task] = field(default_factory=set)
    admitted: asyncio.Event = field(default_factory=asyncio.Event)
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    steering: asyncio.Lock = field(default_factory=asyncio.Lock)


class Chat:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.accepting = True
        self.rounds: dict[int, ChatRound] = {}
        self.lines: dict[str, deque[ChatRound]] = {}
        self.drains: set[asyncio.Task] = set()

    async def receive(self, message: discord.Message) -> None:
        received_at = time.monotonic()
        if not self.accepting or self.bot.user is None or message.author.bot or not isinstance(message.channel, discord.TextChannel) or not isinstance(message.author, discord.Member):
            return
        mentioned = re.search(rf"<@!?{self.bot.user.id}>", message.content) is not None and any(user.id == self.bot.user.id for user in message.mentions)
        reply = message.type is discord.MessageType.reply and message.reference is not None
        if not mentioned and not reply:
            return
        settings = await self.bot.store.settings()
        if message.channel.id not in settings.ai_channel_ids or not settings.allowed_role_ids.intersection(role.id for role in message.author.roles):
            return
        referenced = await self._referenced(message) if reply else None
        if not mentioned and (referenced is None or referenced.author.id != self.bot.user.id):
            return
        if not self.accepting:
            return
        round_state = ChatRound(message, received_at, referenced)
        self.rounds[message.id] = round_state
        round_state.task = asyncio.create_task(self._serve(round_state))

    async def _referenced(self, message: discord.Message) -> discord.Message | None:
        if isinstance(message.reference.resolved, discord.Message):
            return message.reference.resolved
        if message.reference.message_id is None:
            return None
        try:
            return await message.channel.fetch_message(message.reference.message_id)
        except discord.HTTPException:
            return None

    async def _channel_messages(self, round_state: ChatRound, thread_id: str | None) -> list[discord.Message]:
        cursor = self.bot.store.cursor(thread_id) if thread_id is not None else None
        after = discord.Object(cursor) if cursor is not None else None
        messages = [message async for message in round_state.message.channel.history(limit=100, before=round_state.message, after=after, oldest_first=False)]
        messages.reverse()
        if round_state.source is not None:
            def in_source(message_id: int | None) -> bool:
                return message_id is not None and (entry := self.bot.store.entry_for(message_id)) is not None and entry.thread == round_state.source

            prompts = {message.reference.message_id for message in messages if message.reference is not None and message.author.id == self.bot.user.id and in_source(message.id)}
            return [message for message in messages if not in_source(message.id) and message.id not in prompts and not (message.reference is not None and in_source(message.reference.message_id))]
        if thread_id is not None:
            return [message for message in messages if (entry := self.bot.store.entry_for(message.id)) is None or entry.thread != thread_id]
        referenced = round_state.referenced
        if referenced is not None and all(message.id != referenced.id for message in messages):
            messages.insert(0, referenced)
        return messages

    async def _images(self, message: discord.Message) -> list[dict]:
        items = []
        for attachment in message.attachments:
            if not tools.is_image(attachment):
                continue
            try:
                item = await asyncio.to_thread(image_input, await attachment.read())
            except (discord.HTTPException, aiohttp.ClientError, TimeoutError):
                continue
            if item is not None:
                items.append(item)
        return items

    async def _inputs(self, channel_messages: list[discord.Message], trigger: discord.Message, replied: discord.Message | None, steer: bool = False) -> tuple[list[dict], list[str]]:
        entries = [*channel_messages, trigger]
        images = await asyncio.gather(*(self._images(message) for message in entries))
        blocks = []
        count = 0
        for message, items in zip(entries, images):
            labels = "".join(f"〔圖片 {count + index}〕" for index in range(1, len(items) + 1))
            count += len(items)
            content = "\n".join(part for part in (message_text(message), labels) if part)
            if message is trigger and replied is not None:
                content = f"（回覆 {replied.author.display_name}　{replied.created_at.astimezone(TAIPEI):%Y-%m-%d %H:%M} 的訊息：{message_text(replied)}）\n{content}"
            if content or message is trigger:
                blocks.append(f"{message.author.display_name}　{message.created_at.astimezone(TAIPEI):%Y-%m-%d %H:%M}\n{content}")
        text = f"{STEER_LEAD}\n<補充訊息>\n{blocks[-1]}\n</補充訊息>" if steer else f"<觸發訊息>\n{blocks[-1]}\n</觸發訊息>"
        inputs = [{"type": "text", "text": text, "text_elements": []}]
        for number, item in enumerate((item for items in images for item in items), 1):
            inputs += [{"type": "text", "text": f"〔圖片 {number}〕", "text_elements": []}, item]
        return inputs, [part for block in blocks[:-1] for part in context_parts(block)]

    async def _serve(self, round_state: ChatRound) -> None:
        try:
            async with asyncio.timeout_at(round_state.received_at + 900):
                account = await self.bot.codex.account()
                if account.get("account") is None:
                    raise CodexError("not_logged_in")
                settings = await self.bot.store.settings()
                try:
                    models = await self.bot.codex.models()
                except CodexError as error:
                    log_event("codex_models_error", error)
                    models = []
                model, effort = choose_model(models, settings.model, settings.effort)
                model_id = model["id"] if model is not None else settings.model
                entered = await self._enter(round_state, model_id, settings.instructions)
                if entered == "steered":
                    return
                fresh = entered == "new"
                thread = round_state.thread
                renderer = Renderer(round_state, self.bot.store, entered == "queue")
                round_state.renderer = renderer
                renderer.refresh()
                if entered == "queue":
                    await renderer.guard(round_state.admitted.wait())
                    renderer.waiting = False
                    renderer.refresh()
                if not fresh:
                    await renderer.guard(self.bot.codex.load_thread(thread.id))
                channel_messages = await renderer.guard(self._channel_messages(round_state, None if fresh else thread.id))
                inputs, context = await renderer.guard(self._inputs(channel_messages, round_state.message, round_state.referenced if fresh or channel_messages else None))
                turn = TurnState(thread.id)
                round_state.turn = turn
                await self.bot.codex.start_turn(thread, turn, inputs, model_id, effort, context, round_state.message.id)
                await self.bot.store.begin_turn(thread.id, turn.id, [message.id for message in round_state.messages], round_state.message.id)
                round_state.settled.set()
                await self._follow(round_state, turn, renderer)
                await renderer.flush()
                await self.bot.store.set_latest_turn(thread.id, turn.id)
        except asyncio.CancelledError:
            log_event("turn_stopped")
            await self._abort(round_state)
        except TimeoutError:
            log_event("turn_timeout")
            await self._abort(round_state)
        except Exception as error:
            metrics = {"rpc_code": error.rpc_code} if isinstance(error, CodexError) and error.rpc_code is not None else {}
            log_event(error.code if isinstance(error, CodexError) else "turn_error", error, **metrics)
            await self._abort(round_state)
        finally:
            round_state.settled.set()
            self._stop_tools(round_state)
            if round_state.renderer is not None:
                await round_state.renderer.close()
            if round_state.turn is not None:
                self._log_usage(round_state)
            self._leave(round_state)
            self.rounds.pop(round_state.message.id, None)

    async def _enter(self, round_state: ChatRound, model_id: str | None, instructions: str | None) -> Entered:
        referenced = round_state.referenced
        bot_reply = referenced is not None and referenced.author.id == self.bot.user.id
        while True:
            entry = self.bot.store.entry_for(referenced.id) if bot_reply else None
            active = self._active_round(referenced.id) if bot_reply else None
            action = route(
                author=round_state.message.author.id,
                bot_reply=bot_reply,
                active_asker=active.message.author.id if active is not None else None,
                entry=entry,
                latest=self.bot.store.latest_turn(entry.thread) if entry is not None else None,
                busy=entry is not None and entry.thread in self.lines,
            )
            if action == "new":
                self._join(round_state, await self.bot.codex.new_thread(model_id, instructions, tools.SPEC))
                await self.bot.store.add_thread(round_state.thread.id, round_state.message.id)
                return "new"
            if action == "fork":
                thread = await self.bot.codex.fork_thread(entry.thread, entry.turn, model_id)
                await self.bot.store.add_thread(thread.id, referenced.id)
                round_state.source = entry.thread
                self._join(round_state, thread)
                return "fork"
            if action != "steer":
                return self._join(round_state, active.thread if active is not None else self.bot.codex.thread_state(entry.thread))
            async with active.steering:
                inputs, _ = await self._inputs([], round_state.message, None, steer=True)
                await active.settled.wait()
                if not self._steerable(active):
                    continue
                turn = active.turn
                if not await self.bot.codex.steer(turn, inputs, round_state.message.id):
                    return self._join(round_state, active.thread)
                await self.bot.store.record_message(round_state.message.id, turn.thread_id, turn.id)
            try:
                await round_state.message.add_reaction("✅")
            except discord.HTTPException as error:
                log_event("steer_reaction_error", error)
            return "steered"

    def _join(self, round_state: ChatRound, thread: ThreadState) -> Literal["continue", "queue"]:
        round_state.thread = thread
        line = self.lines.get(thread.id)
        if line is None:
            self.lines[thread.id] = deque([round_state])
            return "continue"
        line.append(round_state)
        return "queue"

    def _live(self, round_state: ChatRound) -> bool:
        line = self.lines.get(round_state.thread.id)
        return line is not None and line[0] is round_state and not round_state.cleaning and (round_state.turn is None or not round_state.turn.terminal.is_set())

    def _active_round(self, message_id: int) -> ChatRound | None:
        return next((line[0] for line in self.lines.values() if self._live(line[0]) and any(message.id == message_id for message in line[0].messages)), None)

    def _steerable(self, round_state: ChatRound) -> bool:
        return self._live(round_state) and round_state.turn is not None and round_state.turn.id is not None

    def _leave(self, round_state: ChatRound) -> None:
        line = self.lines.get(round_state.thread.id) if round_state.thread is not None else None
        if line is None or round_state not in line:
            return
        if line[0] is not round_state:
            line.remove(round_state)
        elif round_state.turn is not None and not round_state.turn.terminal.is_set():
            task = asyncio.create_task(self._drain(round_state))
            self.drains.add(task)
            task.add_done_callback(self.drains.discard)
        else:
            self._release(round_state)

    def _release(self, round_state: ChatRound) -> None:
        line = self.lines[round_state.thread.id]
        line.popleft()
        round_state.thread.active = None
        if line:
            line[0].admitted.set()
        else:
            del self.lines[round_state.thread.id]

    async def _follow(self, round_state: ChatRound, turn: TurnState, renderer: Renderer) -> None:
        while True:
            method, params = await renderer.guard(turn.events.get())
            if method == "client/toolCall":
                task = asyncio.create_task(self._run_tool(round_state, turn, renderer, params))
                round_state.tool_tasks.add(task)
                task.add_done_callback(round_state.tool_tasks.discard)
                continue
            renderer.handle(method, params)
            if method == "turn/completed":
                if params["turn"]["status"] != "completed":
                    raise CodexError("turn_failed")
                if not renderer.answer_text().strip():
                    raise CodexError("empty_answer")
                return
            if method in ("error", "client/disconnected", "client/recovered", "client/unsupportedRequest"):
                raise CodexError("turn_interrupted")

    async def _run_tool(self, round_state: ChatRound, turn: TurnState, renderer: Renderer, call: ToolCall) -> None:
        context = tools.ToolContext(round_state.message.channel, round_state.message, self.bot.http, message_text, image_input)
        try:
            result = await tools.run(call.tool, call.arguments, context)
            if result.images:
                inputs = [{"type": "text", "text": TOOL_IMAGE_LABEL, "text_elements": []}, *result.images]
                if not await self.bot.codex.steer(turn, inputs, f"tool-{call.call_id}"):
                    result = tools.ToolResult(False, "圖片無法附加到這次回答", "查看圖片失敗：無法附加圖片")
        except Exception as error:
            log_event("tool_error", error)
            result = tools.ToolResult(False, "工具執行失敗", "工具執行失敗")
        renderer.tool_notes[call.call_id] = result.note
        await self.bot.codex.respond(call, result.success, result.text)

    def _stop_tools(self, round_state: ChatRound) -> None:
        if round_state.turn is not None:
            round_state.turn.accepts_tools = False
        for task in tuple(round_state.tool_tasks):
            task.cancel()

    async def _delete_messages(self, round_state: ChatRound) -> None:
        gone = []
        for message in tuple(round_state.messages):
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except Exception as error:
                log_event("message_cleanup_error", error)
                continue
            round_state.messages.remove(message)
            gone.append(message.id)
        if gone:
            try:
                await self.bot.store.forget_messages(gone)
            except Exception as error:
                log_event("message_forget_error", error)

    async def _abort(self, round_state: ChatRound) -> None:
        round_state.cleaning = True
        self._stop_tools(round_state)
        if round_state.renderer is not None:
            await round_state.renderer.close()
        cleanup = [self._delete_messages(round_state)]
        if round_state.turn is not None:
            cleanup.append(self.bot.codex.interrupt(round_state.turn))
        await asyncio.gather(*cleanup)
        if round_state.pending_sends:
            tasks = tuple(round_state.pending_sends)
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, discord.Message) and result not in round_state.messages:
                    round_state.messages.append(result)
            round_state.pending_sends.clear()
            await self._delete_messages(round_state)

    async def _drain(self, round_state: ChatRound) -> None:
        try:
            await self.bot.codex.wait_terminal(round_state.turn)
        finally:
            if round_state.turn.terminal.is_set():
                self._release(round_state)

    def _log_usage(self, round_state: ChatRound) -> None:
        turn = round_state.turn
        before, after = turn.usage_before, turn.usage_after
        metrics = {"duration_ms": round((time.monotonic() - round_state.received_at) * 1000), "usage_incomplete": True}
        names = {"inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens", "outputTokens": "output_tokens", "reasoningOutputTokens": "reasoning_output_tokens", "totalTokens": "total_tokens"}
        if before is not None and after is not None and all(type(before.get(key)) is int and type(after.get(key)) is int and after[key] >= before[key] for key in names):
            metrics.update({label: after[key] - before[key] for key, label in names.items()})
            metrics["usage_incomplete"] = False
        log_event("turn_usage", **metrics)

    async def shutdown(self) -> None:
        self.accepting = False
        rounds = [value for value in tuple(self.rounds.values()) if value.task is not None]
        for value in rounds:
            if not value.cleaning:
                value.task.cancel()
        tasks = [value.task for value in rounds]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tuple(self.drains):
            task.cancel()
        if self.drains:
            await asyncio.gather(*self.drains, return_exceptions=True)
