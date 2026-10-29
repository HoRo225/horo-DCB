from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord

from src.ai.adapters.discord.context import (
    allowed_channel_id,
    conversation_access,
    get_referenced_message,
    quote,
    reply_chain,
    require_short,
    select_media,
)
from src.ai.adapters.discord.errors import codex_error_text
from src.ai.adapters.discord.intent import route_intent
from src.ai.adapters.discord.output import AiProgress, reply_notice, send_ai_answer
from src.ai.adapters.media.images import ImageAttachmentError, MediaItem, MediaReader, describe_media
from src.ai.app.ports import ActionRunner, GamesChannel, ModelError, NewcomerTips, StoreError, TextModel
from src.ai.app.settings import AiSettings
from src.ai.domain.conversation import CLEANUP_SECONDS, WORK_TIMEOUT_SECONDS
from src.ai.domain.prompts import chat_instructions, chat_prompt
from src.core.adapters.discord.brand import ICONS
from src.core.adapters.discord.live import LiveGuild, can_send as member_can_send, message_source
from src.core.app.ports import Actor
from src.core.app.supervisor import TaskGroup

CONTEXT_TIMEOUT_SECONDS = 30.0
RESTARTED_NOTICE = "已開始新的對話（先前的無法接續）"
NEWCOMER_TIP = "第一次來？跟我說「說明」看看我能做什麼"


@dataclass(frozen=True, slots=True)
class AiServices:
    live: LiveGuild
    settings: AiSettings
    model: TextModel
    actions: ActionRunner
    games: GamesChannel
    media: MediaReader
    cleanups: TaskGroup
    tips: NewcomerTips


def _image_count(items: list[MediaItem]) -> int:
    return sum(len(images) for _label, images in items)


async def _keep_typing(channel: Any) -> None:
    with contextlib.suppress(discord.HTTPException, aiohttp.ClientError, TimeoutError):
        async with channel.typing():
            await asyncio.Event().wait()


class AiRequest:
    def __init__(
        self,
        services: AiServices,
        message: discord.Message,
        *,
        key: str,
        content: str,
        mentions_bot: bool,
        referenced_message: Any,
        bot_user_id: int,
        bot_name: str,
        received_context: dict[str, object] | None = None,
    ) -> None:
        self.services = services
        self.message = message
        self.key = key
        self.content = content
        self.mentions_bot = mentions_bot
        self.referenced_message = referenced_message
        self.bot_user_id = bot_user_id
        self.bot_name = bot_name
        self.id = secrets.token_hex(4)
        self.queued_at = time.monotonic()
        self.accepted_deadline = asyncio.get_running_loop().time() + WORK_TIMEOUT_SECONDS
        self.ms = {"queue": 0.0, "images": 0.0, "sdk": 0.0, "intent": 0.0, "chat": 0.0, "summary": 0.0, "discord": 0.0}
        self.outcome = "unavailable"
        self.output_started = False
        settings = services.settings
        self.generation = settings.generation
        self.allowed_channel_id = allowed_channel_id(message.channel)
        self.channel_generation = settings.channel_generation(self.allowed_channel_id)
        self.source = message_source(message)
        self.context = received_context if received_context is not None else services.actions.context(self.source)
        self.job: Any = None
        self.progress = AiProgress(
            can_send=self.can_send, deadline=self.accepted_deadline, name=bot_name, cleanups=services.cleanups,
        )

    @contextlib.contextmanager
    def timed(self, name: str) -> Iterator[None]:
        started = time.monotonic()
        try:
            yield
        finally:
            self.ms[name] += (time.monotonic() - started) * 1000

    async def can_send(self) -> bool:
        job, live = self.job, self.services.live
        deadline = job.deadline if job is not None else self.accepted_deadline
        if not self.current() or asyncio.get_running_loop().time() >= deadline:
            return False
        author = await live.find_member(self.message.author.id)
        channel = live.channel(self.message.channel.id)
        if author is None or channel is None or not member_can_send(channel, author):
            return False
        return (
            self.current()
            and asyncio.get_running_loop().time() < deadline
            and conversation_access(self.message, self.services.settings, author=author)[0] == self.key
        )

    def current(self) -> bool:
        return self.services.settings.is_current(self.generation, self.allowed_channel_id, self.channel_generation)

    def granted(self, actor: Actor) -> bool:
        return self.services.settings.denial_reason(
            self.message.guild.id, self.allowed_channel_id, actor.role_ids,
        ) is None

    async def send_error(self, exc: Exception, *, deadline: float) -> None:
        self.outcome = (
            exc.code
            if isinstance(exc, ModelError)
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
            with self.timed("discord"):
                async with asyncio.timeout(min(CLEANUP_SECONDS, remaining)):
                    if not await self.progress.fail(error_text):
                        await reply_notice(self.message, error_text)
        except discord.HTTPException, aiohttp.ClientError, TimeoutError:
            logging.error("Failed to send Discord AI status reply.")

    async def _prepare(self) -> tuple[str, tuple[str, ...]]:
        message = self.message
        if not await self.can_send():
            raise ModelError("revoked")
        require_short(self.content)
        deadline = min(self.job.work_deadline, asyncio.get_running_loop().time() + CONTEXT_TIMEOUT_SECONDS)
        nearest = self.referenced_message
        if self.mentions_bot and nearest is None and getattr(message, "reference", None) is not None:
            nearest = await get_referenced_message(message)
            if nearest is None:
                raise ImageAttachmentError(
                    "目前無法讀取被回覆的訊息，請重新回覆或重新上傳內容。",
                )
        chain = [] if nearest is None else await reply_chain(nearest, deadline=deadline)
        quotes = [quote(item, bot_user_id=self.bot_user_id, bot_name=self.bot_name) for item in chain]
        with self.timed("images"):
            media_by_message, skipped, broken, timed_out = await self.services.media.read(
                select_media([message] if nearest is None else [nearest, message]), deadline=deadline,
            )
        media_items = [item for items in media_by_message for item in items]
        images = tuple(url for _label, urls in media_items for url in urls)
        if not self.content and not images and not any(item.text for item in quotes):
            raise ImageAttachmentError(
                "目前無法讀取這些媒體，請重新上傳後再試。"
                if broken or timed_out
                else "沒有收到問題內容。請在訊息中輸入問題，或附上圖片、影片、表情或貼圖。"
            )
        prompt = chat_prompt(
            self.content,
            quotes,
            referenced_image_count=0 if nearest is None else _image_count(media_by_message[0]),
            current_image_count=_image_count(media_by_message[-1]),
            media_note=describe_media(media_items, skipped, broken, timed_out),
        )
        if not await self.can_send():
            raise ModelError("revoked")
        return prompt, images

    def _notes(self, restarted: bool) -> list[str]:
        notes = [RESTARTED_NOTICE] * restarted
        try:
            first = self.services.tips.claim_tip(self.message.author.id)
        except StoreError:
            logging.warning("AI newcomer tip state unavailable; answering without the tip.")
            first = False
        return [*notes, NEWCOMER_TIP] if first else notes

    async def run(self) -> None:
        message, model, progress = self.message, self.services.model, self.progress
        try:
            await progress.start(message)
            try:
                models = self.services.settings.models
            except StoreError, ValueError:
                raise ModelError("model_configuration_invalid") from None
            async with model.accepted_request(
                self.key,
                still_current=self.current,
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
                            if await route_intent(self, models):
                                return
                            async with asyncio.timeout_at(job.work_deadline):
                                prompt, images = await self._prepare()
                            with self.timed("sdk"), self.timed("chat"):
                                reply = await model.chat(
                                    self.key,
                                    prompt,
                                    images,
                                    instructions=chat_instructions(self.bot_name),
                                    job=job,
                                    models=models,
                                    on_progress=progress.update,
                                    request_id=self.id,
                                )
                        finally:
                            typing.cancel()
                        await progress.stop()
                        with self.timed("discord"):
                            notes = self._notes(reply.restarted)
                            answer = f"-# {ICONS['info']} {' · '.join(notes)}\n{reply.text}" if notes else reply.text
                            self.output_started = True
                            self.outcome = await send_ai_answer(
                                message,
                                answer,
                                image_urls=reply.image_urls,
                                can_send=self.can_send,
                            )
                        if not job.current:
                            self.outcome = "revoked"
                except (ImageAttachmentError, ModelError, TimeoutError) as exc:
                    await self.send_error(exc, deadline=job.deadline)
        except asyncio.CancelledError:
            self.outcome = "cancelled"
        except ModelError as exc:
            self.ms["queue"] = (time.monotonic() - self.queued_at) * 1000
            await self.send_error(exc, deadline=self.accepted_deadline)
        except discord.HTTPException, aiohttp.ClientError, TimeoutError:
            self.outcome = "unavailable"
        except Exception:
            logging.exception("AI request id=%s failed unexpectedly.", self.id)
            await self.send_error(ModelError("unavailable"), deadline=self.accepted_deadline)
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
