from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import aiohttp
import discord

from src.ai.access import CodexAccess, member_role_ids
from src.ai.client import CodexBridgeClient
from src.ai.images import (
    ImageAttachmentError,
    MediaBudget,
    read_image_attachments,
    read_message_media,
    select_image_attachments,
    select_message_media,
)
from src.ai.media_executor import MediaExecutor
from src.ai.message_context import build_codex_prompt as build_codex_prompt
from src.ai.message_context import clean_bot_mention as clean_bot_mention
from src.ai.message_context import get_referenced_message as get_referenced_message
from src.ai.message_context import message_mentions_bot as message_mentions_bot
from src.ai.message_context import visible_message_text as visible_message_text
from src.ai.output import AiProgress, build_ai_text_display_view
from src.ai.output import _send_native_ai_chunks as _send_native_ai_chunks
from src.ai.output import codex_error_text as codex_error_text
from src.ai.output import send_ai_answer as send_ai_answer
from src.ai.protocol import (
    MAX_IMAGE_ATTACHMENTS,
    MAX_PROMPT_CHARACTERS,
    CodexBridgeError,
    conversation_key,
)

_THREAD_CHANNEL_TYPES = {
    "public_thread",
    "private_thread",
    "news_thread",
}


def _codex_conversation_access_for_message(
    message: Any,
    access: CodexAccess,
    *,
    author: Any | None = None,
) -> tuple[str | None, str | None]:
    guild_id = getattr(getattr(message, "guild", None), "id", None)
    channel = getattr(message, "channel", None)
    channel_id = getattr(channel, "id", None)
    author = getattr(message, "author", None) if author is None else author
    user_id = getattr(author, "id", None)
    if not all(
        type(value) is int and value > 0
        for value in (
            guild_id,
            channel_id,
            user_id,
        )
    ):
        return None, "scope"

    is_thread = str(getattr(channel, "type", "")) in _THREAD_CHANNEL_TYPES
    allowed_channel_id = getattr(channel, "parent_id", None) if is_thread else channel_id
    if type(allowed_channel_id) is not int:
        return None, "scope"
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


def codex_conversation_key_for_message(
    message: Any,
    access: CodexAccess,
    *,
    author: Any | None = None,
) -> str | None:
    return _codex_conversation_access_for_message(
        message,
        access,
        author=author,
    )[0]


async def handle_message(
    message: discord.Message,
    *,
    bot_user_id: int | None,
    codex: CodexBridgeClient,
    access: CodexAccess,
    member_cache_enabled: bool,
    text_display_enabled: bool,
    media_executor: MediaExecutor,
) -> None:
    if message.author.bot or message.webhook_id is not None or bot_user_id is None:
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
    referenced_message = None
    if not mentions_bot:
        referenced_message = await get_referenced_message(message)
        if getattr(getattr(referenced_message, "author", None), "id", None) != bot_user_id:
            return

    key, denial_reason = _codex_conversation_access_for_message(message, access)
    if key is None:
        logging.info("AI access denied reason=%s", denial_reason)
        await message.reply(
            "Codex 目前未對此身分組或頻道開放。",
            mention_author=False,
        )
        return

    cleaned_content = clean_bot_mention(content, bot_user_id)
    if not codex.try_start_request(message.author.id):
        await message.reply(
            "請稍候幾秒再試。",
            mention_author=False,
        )
        return

    queued_at = time.monotonic()
    accepted_deadline = asyncio.get_running_loop().time() + codex.work_timeout_seconds
    queue_ms = images_ms = sdk_ms = discord_ms = 0.0
    outcome = "unavailable"
    output_started = False
    status_written = False
    generation = access.generation
    current_job = None

    async def can_send() -> bool:
        deadline = current_job.deadline if current_job is not None else accepted_deadline
        if (
            not access.is_current(generation)
            or (current_job is not None and not current_job.current)
            or asyncio.get_running_loop().time() >= deadline
        ):
            return False
        guild = message.guild
        author = guild.get_member(message.author.id) if member_cache_enabled else None
        if author is None:
            try:
                author = await asyncio.wait_for(guild.fetch_member(message.author.id), 2)
            except discord.HTTPException, aiohttp.ClientError, TimeoutError, AttributeError:
                return False
        return (
            access.is_current(generation)
            and (current_job is None or current_job.current)
            and asyncio.get_running_loop().time() < deadline
            and codex_conversation_key_for_message(message, access, author=author) == key
        )

    progress = AiProgress(can_send=can_send, deadline=accepted_deadline)

    async def send_error(exc: Exception, *, deadline: float | None = None) -> None:
        nonlocal outcome, discord_ms, status_written
        outcome = (
            exc.code
            if isinstance(exc, CodexBridgeError)
            else "timeout"
            if isinstance(exc, TimeoutError)
            else "invalid_request"
        )
        if output_started:
            return
        await progress.stop()
        if not await can_send():
            return
        error_text = (
            str(exc) if isinstance(exc, ImageAttachmentError) else codex_error_text(outcome)
        )
        budget = codex.cleanup_timeout_seconds
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            budget = min(budget, remaining)
        started = time.monotonic()
        try:
            async with asyncio.timeout(budget):
                if progress.message is not None:
                    await progress.message.edit(
                        view=build_ai_text_display_view(error_text),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    status_written = True
                else:
                    await message.reply(
                        error_text,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
        except discord.HTTPException, aiohttp.ClientError, TimeoutError:
            logging.error("Discord AI 狀態回覆送出失敗。")
        finally:
            discord_ms += (time.monotonic() - started) * 1000

    try:
        if text_display_enabled:
            await progress.start(message)
        async with codex.accepted_request(
            key,
            access=access,
            user_id=message.author.id,
            parent_channel_id=(
                message.channel.parent_id
                if str(getattr(message.channel, "type", "")) in _THREAD_CHANNEL_TYPES
                else None
            ),
            deadline=accepted_deadline,
        ) as job:
            current_job = job
            progress.update("preparing")
            assert job.started_at is not None
            queue_ms = (job.started_at - job.accepted_at) * 1000

            try:
                async with asyncio.timeout_at(job.deadline):
                    async with message.channel.typing():
                        async with asyncio.timeout_at(job.work_deadline):
                            if not await can_send():
                                raise CodexBridgeError("unauthorized")
                            if len(cleaned_content) > MAX_PROMPT_CHARACTERS:
                                raise ImageAttachmentError("問題最多 4,000 個字元，請縮短後再試。")
                            if (
                                mentions_bot
                                and referenced_message is None
                                and getattr(message, "reference", None) is not None
                            ):
                                referenced_message = await get_referenced_message(message)
                                if referenced_message is None:
                                    raise ImageAttachmentError(
                                        "目前無法讀取被回覆的訊息，請重新回覆或重新上傳內容。",
                                    )

                            referenced_context = referenced_message if mentions_bot else None
                            if referenced_context is not None:
                                image_attachments = select_image_attachments(
                                    [
                                        *getattr(referenced_context, "attachments", ()),
                                        *attachments,
                                    ]
                                )
                                media_messages = [referenced_context, message]
                            else:
                                image_attachments = select_image_attachments(attachments)
                                if not image_attachments and referenced_message is not None:
                                    image_attachments = select_image_attachments(
                                        list(getattr(referenced_message, "attachments", ()))
                                    )
                                media_messages = [message]

                            media_sources = select_message_media(media_messages)
                            if len(image_attachments) + len(media_sources) > MAX_IMAGE_ATTACHMENTS:
                                raise ImageAttachmentError(
                                    f"一次最多處理 {MAX_IMAGE_ATTACHMENTS} 張圖片，請減少圖片後再試。"
                                )
                            media_budget = MediaBudget()
                            started = time.monotonic()
                            try:
                                media_deadline = min(
                                    job.work_deadline,
                                    asyncio.get_running_loop().time() + codex.image_timeout_seconds,
                                )
                                async with asyncio.timeout_at(media_deadline):
                                    images = await read_image_attachments(
                                        image_attachments,
                                        budget=media_budget,
                                        executor=media_executor,
                                        deadline=media_deadline,
                                    )
                                    images += await read_message_media(
                                        media_sources,
                                        budget=media_budget,
                                        executor=media_executor,
                                        deadline=media_deadline,
                                    )
                            finally:
                                images_ms = (time.monotonic() - started) * 1000
                            referenced_text = visible_message_text(referenced_context)
                            if not cleaned_content and not images and not referenced_text:
                                raise ImageAttachmentError(
                                    "請輸入問題，或附上圖片、GIF、表情或貼圖。",
                                )
                            prompt = build_codex_prompt(cleaned_content, referenced_context)
                            if not prompt and images:
                                prompt = "請說明我提供的內容。"
                            if not await can_send():
                                raise CodexBridgeError("unauthorized")
                        started = time.monotonic()
                        try:
                            progress.update("generating")
                            reply = await codex.chat(
                                key, prompt, images, job=job, on_progress=progress.update
                            )
                        finally:
                            sdk_ms = (time.monotonic() - started) * 1000
                    await progress.stop()
                    output_started = True
                    started = time.monotonic()
                    try:
                        outcome = await send_ai_answer(
                            message,
                            reply.text,
                            image_urls=reply.image_urls,
                            text_display_enabled=text_display_enabled,
                            can_send=can_send,
                            progress_message=progress.message,
                        )
                    finally:
                        discord_ms = (time.monotonic() - started) * 1000
                    if not job.current:
                        outcome = "unauthorized"
            except (ImageAttachmentError, CodexBridgeError, TimeoutError) as exc:
                # Active error output still owns its key and cancellation registry.
                await send_error(exc, deadline=job.deadline)
    except asyncio.CancelledError:
        outcome = "cancelled"
    except CodexBridgeError as exc:
        # Rejected admission and expired queues report immediately, without requeueing.
        queue_ms = (time.monotonic() - queued_at) * 1000
        await send_error(exc, deadline=accepted_deadline)
    except aiohttp.ClientError, TimeoutError:
        # A failed initial Discord send may already exist; never replay an ambiguous send.
        outcome = "unavailable"
    finally:
        await progress.stop()
        if progress.message is not None and (
            outcome in {"cancelled", "unauthorized"} or (not output_started and not status_written)
        ):
            try:
                async with asyncio.timeout(3):
                    await progress.message.delete()
            except discord.HTTPException, aiohttp.ClientError, TimeoutError:
                logging.error("Discord AI 進度卡清理失敗。")
        logging.info(
            "AI request result=%s queue_ms=%.1f images_ms=%.1f sdk_ms=%.1f discord_ms=%.1f",
            outcome,
            queue_ms,
            images_ms,
            sdk_ms,
            discord_ms,
        )


async def handle_member_update(
    after: discord.Member,
    *,
    codex: CodexBridgeClient,
    access: CodexAccess,
) -> None:
    guild_id = getattr(getattr(after, "guild", None), "id", None)
    if type(guild_id) is int:
        if not access.role_ids.intersection(member_role_ids(after)):
            try:
                await codex.cancel_member(guild_id, after.id)
            except CodexBridgeError:
                logging.error("Codex revoked member cleanup failed.")


async def archive_scope(
    codex: CodexBridgeClient,
    guild_id: int,
    channel_id: int | None = None,
    *,
    include_children: bool = False,
) -> None:
    try:
        result = await codex.archive_scope(guild_id, channel_id, include_children=include_children)
        logging.info(
            "Codex scope detached=%d archived=%d unconfirmed=%d",
            result.detached_count,
            result.archived_count,
            result.archive_unconfirmed_count,
        )
    except Exception:
        logging.error("Codex scope archive failed.")
