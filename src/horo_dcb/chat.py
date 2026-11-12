import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import aiohttp
import discord

from . import log_event, tools
from .codex import CodexError, ThreadState, ToolCall, TurnState, choose_model
from .content import exceeds_pixels, image_input, image_mime, message_text, stamp
from .render import ALLOWED_MENTIONS, Failure, FailureReason, Renderer, render_view
from .store import Entry

if TYPE_CHECKING:
    from .bot import HoroBot


TOOL_IMAGE_LABEL = "〔工具附上的圖片〕"
LINK_HINT = "（這則訊息含有 Discord 訊息連結。連結指向的訊息可能不在上面的頻道訊息裡，上面附的圖片也不屬於它。請先用 discord 工具讀取或查看連結指向的訊息，再回答。）"
STEER_LEAD = "原提問者在你回答途中送出以下補充。請立刻依照補充內容重新回答這次的問題，補充優先於原本的要求："
MAX_CONTEXT_BYTES = 3800
ROUND_TIMEOUT = 900

CODEX_FAILURES = {"not_logged_in": FailureReason.SIGNED_OUT, "empty_answer": FailureReason.EMPTY}

Action = Literal["new", "continue", "queue", "steer", "fork"]
Entered = Literal["new", "continue", "queue", "steered", "fork"]


def failure_reason(error: Exception) -> FailureReason | None:
    if isinstance(error, CodexError):
        return CODEX_FAILURES.get(error.code, FailureReason.DISCONNECTED)
    if isinstance(error, discord.NotFound):
        return None
    return FailureReason.ERROR


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
    def __init__(self, bot: HoroBot) -> None:
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
            mime = image_mime(attachment.content_type, attachment.filename)
            if mime is None or exceeds_pixels(attachment.width, attachment.height):
                continue
            try:
                data = await attachment.read()
            except (discord.HTTPException, aiohttp.ClientError, TimeoutError):
                continue
            items.append(await asyncio.to_thread(image_input, mime, data))
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
            if message is trigger and tools.LINK.search(content):
                content = f"{content}\n{LINK_HINT}"
            if message is trigger and replied is not None:
                content = f"（回覆 {replied.author.display_name}　{stamp(replied.created_at)} 的訊息：{message_text(replied)}）\n{content}"
            if content or message is trigger:
                blocks.append(f"{message.author.display_name}　{stamp(message.created_at)}\n{content}")
        text = f"{STEER_LEAD}\n<補充訊息>\n{blocks[-1]}\n</補充訊息>" if steer else f"<觸發訊息>\n{blocks[-1]}\n</觸發訊息>"
        inputs = [{"type": "text", "text": text, "text_elements": []}]
        for number, item in enumerate((item for items in images for item in items), 1):
            inputs += [{"type": "text", "text": f"〔圖片 {number}〕", "text_elements": []}, item]
        return inputs, [part for block in blocks[:-1] for part in context_parts(block)]

    async def _serve(self, round_state: ChatRound) -> None:
        try:
            async with asyncio.timeout_at(round_state.received_at + ROUND_TIMEOUT):
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
                turn_id = await self.bot.codex.start_turn(thread, turn, inputs, model_id, effort, context, round_state.message.id)  # ty: ignore[invalid-argument-type]
                await self.bot.store.begin_turn(thread.id, turn_id, [message.id for message in round_state.messages], round_state.message.id)
                round_state.settled.set()
                await self._follow(round_state, turn, renderer)
                await renderer.flush()
                await self.bot.store.set_latest_turn(thread.id, turn_id)
        except asyncio.CancelledError:
            log_event("turn_stopped")
            await self._abort(round_state, FailureReason.RESTARTING)
        except TimeoutError:
            log_event("turn_timeout")
            await self._abort(round_state, FailureReason.TIMEOUT)
        except Exception as error:
            metrics = {"rpc_code": error.rpc_code} if isinstance(error, CodexError) and error.rpc_code is not None else {}
            log_event(error.code if isinstance(error, CodexError) else "turn_error", error, **metrics)
            await self._abort(round_state, failure_reason(error))
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
                thread = await self.bot.codex.fork_thread(entry.thread, entry.turn, model_id)  # ty: ignore[invalid-argument-type]
                await self.bot.store.add_thread(thread.id, referenced.id)
                round_state.source = entry.thread
                self._join(round_state, thread)
                return "fork"
            if action != "steer" or active is None:
                return self._join(round_state, active.thread if active is not None else self.bot.codex.thread_state(entry.thread))  # ty: ignore[invalid-argument-type]
            async with active.steering:
                inputs, _ = await self._inputs([], round_state.message, None, steer=True)
                await active.settled.wait()
                turn = self._steerable(active)
                if turn is None:
                    continue
                if not await self.bot.codex.steer(turn, inputs, round_state.message.id):
                    return self._join(round_state, active.thread)  # ty: ignore[invalid-argument-type]
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

    def _steerable(self, round_state: ChatRound) -> TurnState | None:
        turn = round_state.turn
        return turn if self._live(round_state) and turn is not None and turn.id is not None else None

    def _leave(self, round_state: ChatRound) -> None:
        line = self.lines.get(round_state.thread.id) if round_state.thread is not None else None
        if line is None or round_state not in line:
            return
        turn = round_state.turn
        if line[0] is not round_state:
            line.remove(round_state)
        elif turn is not None and not turn.terminal.is_set():
            task = asyncio.create_task(self._drain(round_state, turn))
            self.drains.add(task)
            task.add_done_callback(self.drains.discard)
        else:
            self._release(round_state)

    def _release(self, round_state: ChatRound) -> None:
        line = self.lines[round_state.thread.id]
        line.popleft()
        self.bot.codex.end_turn(round_state.thread.id)
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
        context = tools.ToolContext(round_state.message.channel, round_state.message, self.bot.http)  # ty: ignore[invalid-argument-type]
        try:
            result = await tools.run(call.tool, call.arguments, context)
            if result.images:
                inputs = [{"type": "text", "text": TOOL_IMAGE_LABEL, "text_elements": []}, *result.images]
                if not await self.bot.codex.steer(turn, inputs, f"tool-{call.call_id}"):
                    result = tools.ToolResult(False, "圖片無法附加到這次回答", "失敗，無法附加圖片")
        except Exception as error:
            log_event("tool_error", error)
            result = tools.ToolResult(False, "工具執行失敗", "失敗，發生錯誤")
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

    async def _abort(self, round_state: ChatRound, reason: FailureReason | None) -> None:
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
        if reason is None:
            return
        message = round_state.message
        try:
            await message.channel.send(view=render_view(Failure(reason)), reference=message.to_reference(fail_if_not_exists=False), allowed_mentions=ALLOWED_MENTIONS, mention_author=False)
        except Exception as error:
            log_event("failure_card_error", error)

    async def _drain(self, round_state: ChatRound, turn: TurnState) -> None:
        try:
            await self.bot.codex.wait_terminal(turn)
        finally:
            if turn.terminal.is_set():
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
        tasks = [value.task for value in rounds if value.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tuple(self.drains):
            task.cancel()
        if self.drains:
            await asyncio.gather(*self.drains, return_exceptions=True)
