import asyncio
import base64
import io
import re
import time
import warnings
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from urllib.parse import urlsplit

import aiohttp
import discord
from PIL import Image, UnidentifiedImageError

from . import log_event
from .codex import CodexError, ThreadState, TurnState


ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)
TAIPEI = timezone(timedelta(hours=8))
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
IMAGE_SUFFIXES = ("png", "jpg", "jpeg", "webp", "gif")


def image_input(data: bytes) -> dict | None:
    try:
        with warnings.catch_warnings(record=True):
            with Image.open(io.BytesIO(data)) as image:
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}.get(image.format)
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
    def __init__(self, round_state: "ChatRound", previous: "MessageSlot | None", failure: asyncio.Future) -> None:
        self.round_state = round_state
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
                    await self.message.delete()
                    round_state.messages.remove(self.message)
                self.sent.set()
                self.synced.set()
                return
            if self.message is None:
                task = asyncio.create_task(round_state.message.channel.send(view=render_view(content), reference=round_state.message.to_reference(fail_if_not_exists=False), allowed_mentions=ALLOWED_MENTIONS, mention_author=False))
                round_state.pending_sends.add(task)
                self.message = await asyncio.shield(task)
                round_state.messages.append(self.message)
                round_state.pending_sends.discard(task)
                self.sent.set()
            else:
                await self.message.edit(view=render_view(content), allowed_mentions=ALLOWED_MENTIONS)
            self.last_edit = time.monotonic()
            if self.desired == content:
                self.synced.set()


class Renderer:
    def __init__(self, round_state: "ChatRound") -> None:
        self.round_state = round_state
        self.started_at = int(round_state.message.created_at.timestamp())
        self.failure = asyncio.get_running_loop().create_future()
        self.slots: list[MessageSlot] = []
        self.retired: list[MessageSlot] = []
        self.timeline: list[tuple] = []
        self.agents: dict[str, dict] = {}
        self.reasoning: dict[str, dict[int, str]] = {}
        self.searches: dict[str, dict] = {}

    def _mark(self, key: tuple) -> None:
        if key not in self.timeline:
            self.timeline.append(key)

    def handle(self, method: str, params: dict) -> None:
        item = params.get("item") or {}
        kind = item.get("type")
        if method in ("item/started", "item/completed") and kind == "agentMessage":
            agent = self.agents.setdefault(item["id"], {"phase": None, "text": ""})
            agent["phase"] = item.get("phase")
            if method == "item/completed":
                agent["text"] = item.get("text") or ""
            self._mark(("agent", item["id"]))
        elif method in ("item/started", "item/completed") and kind == "reasoning":
            parts = self.reasoning.setdefault(item["id"], {})
            if method == "item/completed":
                for index, text in enumerate(item.get("summary") or ()):
                    parts[index] = text
                    self._mark(("reasoning", item["id"], index))
        elif method in ("item/started", "item/completed") and kind == "webSearch":
            self.searches[item["id"]] = {"text": search_text(item), "done": method == "item/completed"}
            self._mark(("search", item["id"]))
        elif method == "item/agentMessage/delta":
            self.agents.setdefault(params["itemId"], {"phase": None, "text": ""})["text"] += params.get("delta", "")
            self._mark(("agent", params["itemId"]))
        elif method in ("item/reasoning/summaryPartAdded", "item/reasoning/summaryTextDelta"):
            parts = self.reasoning.setdefault(params["itemId"], {})
            index = params.get("summaryIndex", 0)
            parts[index] = parts.get(index, "") + params.get("delta", "")
            self._mark(("reasoning", params["itemId"], index))
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

    def _answer_text(self) -> str:
        answers = self._answer_keys()
        return "\n\n".join(self.agents[key[1]]["text"] for key in self.timeline if key[0] == "agent" and key[1] in answers)

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
        searching = any(not search["done"] for search in self.searches.values())
        header = f"{'🔍 **搜尋中**' if searching else '💭 **思考中**'} · <t:{self.started_at}:R>"
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

    def refresh(self, answer: str | None = None) -> None:
        chunks = answer_chunks(self._answer_text() if answer is None else answer)
        contents = [("answer", chunk) for chunk in chunks] or [("frame", self._frame())]
        while len(self.slots) < len(contents):
            self.slots.append(MessageSlot(self.round_state, self.slots[-1] if self.slots else None, self.failure))
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


@dataclass
class ChatRound:
    message: discord.Message
    received_at: float
    referenced: discord.Message | None = None
    task: asyncio.Task | None = None
    thread: ThreadState | None = None
    turn: TurnState | None = None
    renderer: Renderer | None = None
    cleaning: bool = False
    messages: list[discord.Message] = field(default_factory=list)
    pending_sends: set[asyncio.Task] = field(default_factory=set)


class Chat:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.accepting = True
        self.rounds: dict[int, ChatRound] = {}
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
        cursor = await self.bot.store.cursor(thread_id) if thread_id is not None else None
        after = discord.Object(cursor) if cursor is not None else None
        messages = [message async for message in round_state.message.channel.history(limit=100, before=round_state.message, after=after, oldest_first=False)]
        messages.reverse()
        if thread_id is not None:
            return [message for message in messages if message.author.id != self.bot.user.id or await self.bot.store.thread_for(message.id) != thread_id]
        referenced = round_state.referenced
        if referenced is not None and all(message.id != referenced.id for message in messages):
            messages.insert(0, referenced)
        return messages

    async def _images(self, message: discord.Message) -> list[dict]:
        items = []
        for attachment in message.attachments:
            mime = (attachment.content_type or "").split(";")[0].lower()
            suffix = attachment.filename.rsplit(".", 1)[-1].lower()
            if mime not in IMAGE_TYPES and suffix not in IMAGE_SUFFIXES:
                continue
            try:
                item = await asyncio.to_thread(image_input, await attachment.read())
            except (discord.HTTPException, aiohttp.ClientError, TimeoutError):
                continue
            if item is not None:
                items.append(item)
        return items

    async def _inputs(self, channel_messages: list[discord.Message], trigger: discord.Message, replied: discord.Message | None) -> list[dict]:
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
        inputs = []
        if len(blocks) > 1:
            inputs.append({"type": "text", "text": "<頻道訊息>\n" + "\n\n".join(blocks[:-1]) + "\n</頻道訊息>", "text_elements": []})
        inputs.append({"type": "text", "text": f"<觸發訊息>\n{blocks[-1]}\n</觸發訊息>", "text_elements": []})
        inputs.extend(item for items in images for item in items)
        return inputs

    async def _serve(self, round_state: ChatRound) -> None:
        locked = False
        try:
            async with asyncio.timeout_at(round_state.received_at + 900):
                async with round_state.message.channel.typing():
                    account = await self.bot.codex.account()
                    if account.get("account") is None:
                        raise CodexError("not_logged_in")
                    settings = await self.bot.store.settings()
                    thread_id = await self.bot.store.thread_for(round_state.referenced.id) if round_state.referenced is not None else None
                    thread = self.bot.codex.thread_state(thread_id) if thread_id is not None else await self.bot.codex.new_thread(settings.model)
                    round_state.thread = thread
                    await thread.lock.acquire()
                    locked = True
                    renderer = Renderer(round_state)
                    round_state.renderer = renderer
                    renderer.refresh()
                    if thread_id is not None:
                        await renderer.guard(self.bot.codex.load_thread(thread_id))
                    inputs = await renderer.guard(self._inputs(await renderer.guard(self._channel_messages(round_state, thread_id)), round_state.message, round_state.referenced if thread_id is None else None))
                    turn = TurnState(thread.id)
                    round_state.turn = turn
                    await self.bot.codex.start_turn(thread, turn, inputs, settings.model, settings.effort)
                    await self.bot.store.advance_cursor(thread.id, round_state.message.id)
                    answer = await self._answer(turn, renderer)
                    renderer.refresh(answer)
                    await renderer.flush()
                    await self.bot.store.record_messages([slot.message.id for slot in renderer.slots], thread.id)
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
            if round_state.renderer is not None:
                await round_state.renderer.close()
            if round_state.turn is not None:
                self._log_usage(round_state)
            if locked:
                if round_state.turn is not None and not round_state.turn.terminal.is_set():
                    task = asyncio.create_task(self._drain(round_state.thread, round_state.turn))
                    self.drains.add(task)
                    task.add_done_callback(self.drains.discard)
                else:
                    round_state.thread.active = None
                    round_state.thread.lock.release()
            self.rounds.pop(round_state.message.id, None)

    async def _answer(self, turn: TurnState, renderer: Renderer) -> str:
        completed: dict[str, dict] = {}
        while True:
            method, params = await renderer.guard(turn.events.get())
            renderer.handle(method, params)
            if method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    completed[item["id"]] = item
            elif method == "turn/completed":
                if params["turn"]["status"] != "completed":
                    raise CodexError("turn_failed")
                for item in params["turn"].get("items", []):
                    if item.get("type") == "agentMessage":
                        completed[item["id"]] = item
                items = list(completed.values())
                answers = [item["text"] for item in items if item.get("phase") == "final_answer"]
                if answers:
                    answer = "\n\n".join(answers)
                elif items and items[-1].get("phase") is None:
                    answer = items[-1].get("text", "")
                else:
                    answer = ""
                if not answer.strip():
                    raise CodexError("empty_answer")
                return answer
            elif method in ("error", "client/disconnected", "client/recovered", "client/unsupportedRequest"):
                raise CodexError("turn_interrupted")

    async def _delete_messages(self, round_state: ChatRound) -> None:
        for message in tuple(round_state.messages):
            try:
                await message.delete()
                round_state.messages.remove(message)
            except discord.NotFound:
                round_state.messages.remove(message)
            except Exception as error:
                log_event("message_cleanup_error", error)

    async def _abort(self, round_state: ChatRound) -> None:
        round_state.cleaning = True
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

    async def _drain(self, thread: ThreadState, turn: TurnState) -> None:
        try:
            await self.bot.codex.wait_terminal(turn)
        finally:
            if turn.terminal.is_set():
                thread.active = None
                thread.lock.release()

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
