from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import sqlite3
import time
from collections.abc import Iterator
from typing import Any

import aiohttp
import discord

from src.ai.access import AiSettings
from src.codex.service import CodexService
from src.codex.contracts import parse_intent_decision
from src.ai.formatting import codex_error_text
from src.ai.images import (
    ImageAttachmentError,
    MediaBudget,
    MediaItem,
    describe_media,
    read_message_media,
    select_message_media,
)
from src.ai.message_context import (
    build_codex_prompt,
    clean_bot_mention,
    get_referenced_message,
    message_mentions_bot,
    visible_message_text,
)
from src.ai.output import AiProgress, reply, reply_notice, send_ai_answer
from src.codex.protocol import (
    CLEANUP_SECONDS,
    MAX_PROMPT_CHARACTERS,
    WORK_TIMEOUT_SECONDS,
    CodexError,
    conversation_key,
)
from src.ai.query_views import help_view
from src.brand import ICONS
from src.discord_utils import member_role_ids

IMAGE_TIMEOUT_SECONDS = 30.0
RESTARTED_NOTICE = "已開始新的對話（先前的無法接續）"
HELP_WORDS = frozenset({"說明", "你會什麼", "你會什麼？", "你會什麼?"})
NEWCOMER_TIP = "第一次來？跟我說「說明」看看我能做什麼"
TIPPED: set[int] = set()


def _codex_conversation_access_for_message(
    message: Any,
    access: AiSettings,
    *,
    author: Any | None = None,
) -> tuple[str | None, str | None]:
    guild_id = message.guild.id
    channel = message.channel
    channel_id = channel.id
    author = message.author if author is None else author
    user_id = author.id
    is_thread = isinstance(channel, discord.Thread)
    allowed_channel_id = channel.parent_id if is_thread else channel_id
    reason = access.denial_reason(
        guild_id,
        allowed_channel_id,
        member_role_ids(author),
    )
    if reason is not None:
        return None, reason
    return (
        conversation_key(
            guild_id,
            channel_id,
            user_id,
            is_thread=is_thread,
        ),
        None,
    )


def _select_media(messages: list[Any]) -> list[list[tuple[str, str]]]:
    seen: set[str] = set()
    return [select_message_media([message], seen=seen) for message in messages]


async def _read_media(
    groups: list[list[tuple[str, str]]], *, deadline: float
) -> tuple[list[list[MediaItem]], int, int, int]:
    budget = MediaBudget()
    items = [
        await read_message_media(sources, budget=budget, deadline=deadline) for sources in groups
    ]
    return items, budget.skipped, budget.broken, budget.timed_out


def _image_count(items: list[MediaItem]) -> int:
    return sum(len(images) for _label, images in items)


async def _keep_typing(channel: Any) -> None:
    with contextlib.suppress(discord.HTTPException, aiohttp.ClientError, TimeoutError):
        async with channel.typing():
            await asyncio.Event().wait()


async def handle_message(
    message: discord.Message,
    *,
    bot_user_id: int,
    codex: CodexService,
    access: AiSettings,
    actions: Any = None,
    bot: Any = None,
) -> None:
    if message.author.bot or message.webhook_id is not None:
        return

    content = message.content.strip()
    attachments = list(message.attachments)
    if (
        not content
        and not attachments
        and not getattr(message, "embeds", ())
        and not getattr(message, "stickers", ())
    ):
        return

    mentions_bot = message_mentions_bot(message, bot_user_id)
    if not mentions_bot and message.reference is None:
        return
    if not access.enabled:
        return
    key, denial_reason = _codex_conversation_access_for_message(message, access)
    if key is None and (
        not mentions_bot
        or (denial_reason == "channel" and clean_bot_mention(content, bot_user_id) == content)
    ):
        return
    text_display_enabled = access.text_display
    received_context = actions.snapshot(message) if actions is not None and key is not None else None
    referenced_message = None
    if not mentions_bot:
        referenced_message = await get_referenced_message(message)
        if getattr(getattr(referenced_message, "author", None), "id", None) != bot_user_id:
            return
    bot_name = message.guild.me.display_name

    if key is None:
        logging.info("AI access denied reason=%s", denial_reason)
        code = {"suspended": "busy", "channel": "not_allowed_channel", "role": "not_allowed_role"}.get(denial_reason, "not_allowed")
        channels = "、".join(f"<#{channel_id}>" for channel_id in sorted(access.channel_ids)[:5])
        if code == "not_allowed_channel" and not channels:
            code = "not_allowed"
        await reply_notice(message, codex_error_text(code, bot_name, channels=channels), text_display_enabled)
        return

    cleaned_content = clean_bot_mention(content, bot_user_id)
    if bot is not None and cleaned_content in HELP_WORDS:
        try:
            await reply(message, view=help_view(bot.games.channel_id))
        except discord.HTTPException:
            logging.exception("Failed to send the help card.")
        return
    replay = actions is not None and actions.records.get_request(message.id) is not None
    if not replay and not codex.try_start_request(message.author.id):
        await reply_notice(message, "你發問得太快了，請稍候幾秒再試。", text_display_enabled)
        return

    await AiRequest(
        message,
        key=key,
        content=cleaned_content,
        mentions_bot=mentions_bot,
        referenced_message=referenced_message,
        bot_name=bot_name,
        codex=codex,
        access=access,
        text_display_enabled=text_display_enabled,
        actions=actions,
        received_context=received_context,
        bot=bot,
    ).run()


class AiRequest:
    def __init__(
        self,
        message: discord.Message,
        *,
        key: str,
        content: str,
        mentions_bot: bool,
        referenced_message: Any,
        bot_name: str,
        codex: CodexService,
        access: AiSettings,
        text_display_enabled: bool,
        actions: Any = None,
        received_context: dict | None = None,
        bot: Any = None,
    ) -> None:
        self.message = message
        self.bot = bot
        self.key = key
        self.content = content
        self.mentions_bot = mentions_bot
        self.referenced_message = referenced_message
        self.bot_name = bot_name
        self.codex = codex
        self.access = access
        self.text_display_enabled = text_display_enabled
        self.id = secrets.token_hex(4)
        self.queued_at = time.monotonic()
        self.accepted_deadline = asyncio.get_running_loop().time() + WORK_TIMEOUT_SECONDS
        self.ms = {"queue": 0.0, "images": 0.0, "sdk": 0.0, "intent": 0.0, "chat": 0.0, "summary": 0.0, "discord": 0.0}
        self.outcome = "unavailable"
        self.output_started = False
        self.generation = access.generation
        self.allowed_channel_id = message.channel.parent_id if isinstance(message.channel, discord.Thread) else message.channel.id
        self.channel_generation = access.channel_generation(self.allowed_channel_id)
        self.actions = actions
        self.context = received_context if received_context is not None else actions.snapshot(message) if actions is not None else {}
        self.job: Any = None
        self.progress = AiProgress(
            can_send=self.can_send, deadline=self.accepted_deadline, name=bot_name
        )

    @contextlib.contextmanager
    def _timed(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.ms[name] += (time.monotonic() - started) * 1000

    async def can_send(self) -> bool:
        job, access = self.job, self.access
        deadline = job.deadline if job is not None else self.accepted_deadline
        if (
            not self._current()
            or asyncio.get_running_loop().time() >= deadline
        ):
            return False
        guild = self.actions.bot.get_guild(self.message.guild.id) if self.actions is not None else self.message.guild
        if guild is None:
            return False
        author = guild.get_member(self.message.author.id)
        if author is None:
            try:
                author = await asyncio.wait_for(guild.fetch_member(self.message.author.id), 2)
            except discord.HTTPException, aiohttp.ClientError, TimeoutError, AttributeError:
                return False
        channel = guild.get_channel_or_thread(self.message.channel.id) if self.actions is not None else self.message.channel
        if channel is None:
            return False
        if hasattr(channel, "permissions_for"):
            try:
                permissions = channel.permissions_for(author)
            except discord.ClientException:
                return False
            if not permissions.view_channel or not (permissions.send_messages_in_threads if isinstance(channel, discord.Thread) else permissions.send_messages):
                return False
        return (
            self._current()
            and asyncio.get_running_loop().time() < deadline
            and _codex_conversation_access_for_message(self.message, access, author=author)[0]
            == self.key
        )

    def _current(self) -> bool:
        return self.access.is_current(self.generation, self.allowed_channel_id, self.channel_generation)

    async def _intent_route(self, models) -> bool:
        if self.actions is None or not self.content.strip():
            return False
        if len(self.content) > MAX_PROMPT_CHARACTERS:
            raise ImageAttachmentError(f"問題最多 {MAX_PROMPT_CHARACTERS:,} 個字元，請縮短後再試。")
        prior = self.actions.records.get_request(self.message.id)
        if prior is not None:
            decision = parse_intent_decision(prior.plan["decision"])
            game_snapshot = prior.plan.get("game")
        else:
            with self._timed("sdk"), self._timed("intent"):
                decision = await self.codex.intent(self.key, self.content, self.context,
                                                  job=self.job, models=models, request_id=self.id)
            game_snapshot = self.context.get("game")
        if decision.kind == "chat":
            return False
        await self.progress.stop()
        self.output_started = True
        if decision.kind == "clarification":
            if await self.can_send():
                await reply_notice(self.message, decision.question, self.text_display_enabled, tone=None)
            self.outcome = "clarification"
            return True
        if self.bot is not None and [action.name for action in decision.actions] == ["help"]:
            if await self.can_send():
                await reply(self.message, view=help_view(self.bot.games.channel_id))
            self.outcome = "help"
            return True
        results = await self.actions.execute_plan(self.message, decision, game_snapshot=game_snapshot,
                                                 deadline=self.job.work_deadline)
        self.outcome = "operations"
        if not self.actions.was_sent(self.message.id):
            if await self.can_send():
                if self.text_display_enabled:
                    public = [{key: value for key, value in result.items() if key != "private_ref"} for result in results]
                    await reply(self.message, view=self.actions._receipt_view(public))
                else:
                    await reply_notice(self.message, "\n".join(result["text"] for result in results), self.text_display_enabled)
            return True
        await self.progress.finish()
        if prior is None and self.job.current and await self.can_send():
            public = [{key: value for key, value in result.items() if key in {"ok", "code", "text", "resource_id", "message_url"}}
                      for result in results]
            try:
                with self._timed("sdk"), self._timed("summary"):
                    summary = await self.codex.summarize(self.key, public, job=self.job, models=models, request_id=self.id)
                if await self.can_send():
                    await self.actions.update_receipt(self.message, summary.text)
            except (CodexError, TimeoutError):
                logging.warning("AI result explanation unavailable; the confirmed receipt remains.")
        return True

    async def send_error(self, exc: Exception, *, deadline: float) -> None:
        self.outcome = (
            exc.code
            if isinstance(exc, CodexError)
            else "timeout"
            if isinstance(exc, TimeoutError)
            else "invalid_request"
        )
        if self.output_started:
            return
        await self.progress.stop()
        if not await self.can_send():
            return
        error_text = (
            str(exc)
            if isinstance(exc, ImageAttachmentError)
            else codex_error_text(self.outcome, self.bot_name)
        )
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        try:
            with self._timed("discord"):
                async with asyncio.timeout(min(CLEANUP_SECONDS, remaining)):
                    if not await self.progress.fail(error_text):
                        await reply_notice(self.message, error_text, self.text_display_enabled)
        except discord.HTTPException, aiohttp.ClientError, TimeoutError:
            logging.error("Failed to send Discord AI status reply.")

    async def _prepare(self) -> tuple[str, tuple[str, ...]]:
        message = self.message
        if not await self.can_send():
            raise CodexError("revoked")
        if len(self.content) > MAX_PROMPT_CHARACTERS:
            raise ImageAttachmentError(f"問題最多 {MAX_PROMPT_CHARACTERS:,} 個字元，請縮短後再試。")
        if (
            self.mentions_bot
            and self.referenced_message is None
            and getattr(message, "reference", None) is not None
        ):
            self.referenced_message = await get_referenced_message(message)
            if self.referenced_message is None:
                raise ImageAttachmentError(
                    "目前無法讀取被回覆的訊息，請重新回覆或重新上傳內容。",
                )

        referenced = self.referenced_message
        media_groups = _select_media([message] if referenced is None else [referenced, message])
        with self._timed("images"):
            media_by_message, skipped, broken, timed_out = await _read_media(
                media_groups,
                deadline=min(
                    self.job.work_deadline,
                    asyncio.get_running_loop().time() + IMAGE_TIMEOUT_SECONDS,
                ),
            )
        media_items = [item for items in media_by_message for item in items]
        images = tuple(url for _label, urls in media_items for url in urls)
        if not self.content and not images and not visible_message_text(referenced):
            raise ImageAttachmentError(
                "目前無法讀取這些媒體，請重新上傳後再試。"
                if broken or timed_out
                else "沒有收到問題內容。請在訊息中輸入問題，或附上圖片、影片、表情或貼圖。"
            )
        prompt = build_codex_prompt(
            self.content,
            referenced,
            referenced_image_count=(
                0 if referenced is None else _image_count(media_by_message[0])
            ),
            current_image_count=_image_count(media_by_message[-1]),
            media_note=describe_media(media_items, skipped, broken, timed_out),
        )
        if not await self.can_send():
            raise CodexError("revoked")
        return prompt, images

    async def run(self) -> None:
        message, codex, progress = self.message, self.codex, self.progress
        try:
            if self.text_display_enabled:
                await progress.start(message)
            try:
                models = self.access.models
            except sqlite3.Error, ValueError:
                raise CodexError("model_configuration_invalid") from None
            async with codex.accepted_request(
                    self.key,
                still_current=self._current,
                user_id=message.author.id,
                parent_channel_id=(
                    message.channel.parent_id
                    if isinstance(message.channel, discord.Thread)
                    else None
                ),
                deadline=self.accepted_deadline,
            ) as job:
                self.job = job
                assert job.started_at is not None
                self.ms["queue"] = (job.started_at - job.accepted_at) * 1000

                try:
                    async with asyncio.timeout_at(job.deadline):
                        typing = asyncio.create_task(_keep_typing(message.channel))
                        try:
                            if await self._intent_route(models):
                                return
                            async with asyncio.timeout_at(job.work_deadline):
                                prompt, images = await self._prepare()
                            with self._timed("sdk"), self._timed("chat"):
                                reply = await codex.chat(
                                    self.key,
                                    prompt,
                                    images,
                                    job=job,
                                    models=models,
                                    on_progress=progress.update,
                                    request_id=self.id,
                                )
                        finally:
                            typing.cancel()
                        await progress.stop()
                        with self._timed("discord"):
                            notes = [RESTARTED_NOTICE] * reply.restarted
                            user_id = message.author.id
                            if self.bot is not None and user_id not in TIPPED:
                                TIPPED.add(user_id)
                                notes.append(NEWCOMER_TIP)
                            answer = f"-# {ICONS['info']} {' · '.join(notes)}\n{reply.text}" if notes else reply.text
                            self.output_started = True
                            self.outcome = await send_ai_answer(
                                message,
                                answer,
                                image_urls=reply.image_urls,
                                text_display_enabled=self.text_display_enabled,
                                can_send=self.can_send,
                            )
                        if not job.current:
                            self.outcome = "revoked"
                except (ImageAttachmentError, CodexError, TimeoutError) as exc:
                    await self.send_error(exc, deadline=job.deadline)
        except asyncio.CancelledError:
            self.outcome = "cancelled"
        except CodexError as exc:
            self.ms["queue"] = (time.monotonic() - self.queued_at) * 1000
            await self.send_error(exc, deadline=self.accepted_deadline)
        except discord.HTTPException, aiohttp.ClientError, TimeoutError:
            self.outcome = "unavailable"
        except Exception:
            logging.exception("AI request id=%s failed unexpectedly.", self.id)
            await self.send_error(CodexError("unavailable"), deadline=self.accepted_deadline)
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        await self.progress.finish()
        logging.info(
            "AI request id=%s result=%s %s",
            self.id,
            self.outcome,
            " ".join(f"{name}_ms={ms:.1f}" for name, ms in self.ms.items()),
        )
