import asyncio
import base64
import io
import re
import time
import warnings
from dataclasses import dataclass, field

import discord
from PIL import Image, UnidentifiedImageError

from . import log_event
from .codex import CodexError, ThreadState, TurnState


ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)


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


@dataclass
class ChatRound:
    message: discord.Message
    received_at: float
    task: asyncio.Task | None = None
    thread: ThreadState | None = None
    turn: TurnState | None = None
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
        if re.search(rf"<@!?{self.bot.user.id}>", message.content) is None or not any(user.id == self.bot.user.id for user in message.mentions):
            return
        settings = await self.bot.store.settings()
        if message.channel.id not in settings.ai_channel_ids or not settings.allowed_role_ids.intersection(role.id for role in message.author.roles):
            return
        if not self.accepting:
            return
        round_state = ChatRound(message, received_at)
        self.rounds[message.id] = round_state
        round_state.task = asyncio.create_task(self._serve(round_state))

    async def _inputs(self, message: discord.Message) -> list[dict]:
        inputs = [{"type": "text", "text": f"<觸發訊息>\n{message.content}\n</觸發訊息>", "text_elements": []}]
        for attachment in message.attachments:
            mime = (attachment.content_type or "").split(";")[0].lower()
            suffix = attachment.filename.rsplit(".", 1)[-1].lower()
            if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif") and suffix not in ("png", "jpg", "jpeg", "webp", "gif"):
                continue
            item = await asyncio.to_thread(image_input, await attachment.read())
            if item is not None:
                inputs.append(item)
        return inputs

    async def _serve(self, round_state: ChatRound) -> None:
        locked = False
        try:
            async with asyncio.timeout_at(round_state.received_at + 900):
                async with round_state.message.channel.typing():
                    account = await self.bot.codex.account()
                    if account.get("account") is None:
                        raise CodexError("not_logged_in")
                    inputs = await self._inputs(round_state.message)
                    settings = await self.bot.store.settings()
                    thread = await self.bot.codex.new_thread(settings.model)
                    round_state.thread = thread
                    await thread.lock.acquire()
                    locked = True
                    turn = TurnState(thread.id)
                    round_state.turn = turn
                    await self.bot.codex.start_turn(thread, turn, inputs, settings.model, settings.effort)
                    await self.bot.store.advance_cursor(thread.id, round_state.message.id)
                    answer = await self._answer(turn)
                    for offset in range(0, len(answer), 4000):
                        await self._send(round_state, answer[offset:offset + 4000])
                    await self.bot.store.record_messages([message.id for message in round_state.messages], thread.id)
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

    async def _answer(self, turn: TurnState) -> str:
        completed: dict[str, dict] = {}
        while True:
            method, params = await turn.events.get()
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

    async def _send(self, round_state: ChatRound, text: str) -> None:
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.TextDisplay(text))
        task = asyncio.create_task(round_state.message.channel.send(view=view, reference=round_state.message.to_reference(fail_if_not_exists=False), allowed_mentions=ALLOWED_MENTIONS, mention_author=False))
        round_state.pending_sends.add(task)
        message = await asyncio.shield(task)
        round_state.messages.append(message)
        round_state.pending_sends.discard(task)

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
        tasks = [value.task for value in tuple(self.rounds.values()) if value.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in tuple(self.drains):
            task.cancel()
        if self.drains:
            await asyncio.gather(*self.drains, return_exceptions=True)
