from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord

from src.ai.access import CodexAccess, member_role_ids
from src.ai.client import CodexBridgeClient
from src.ai.images import ImageAttachmentError, read_image_attachments, select_image_attachments
from src.ai.output import build_ai_text_display_view, split_discord_message, split_discord_text_display
from src.ai.protocol import CodexBridgeError, conversation_key


def clean_bot_mention(content: str, bot_user_id: int) -> str:
    return (
        content.replace(f"<@{bot_user_id}>", "")
        .replace(f"<@!{bot_user_id}>", "")
        .strip()
    )


def message_mentions_bot(message: Any, bot_user_id: int) -> bool:
    return any(getattr(user, "id", None) == bot_user_id for user in message.mentions)


_THREAD_CHANNEL_TYPES = {
    "public_thread",
    "private_thread",
    "news_thread",
}


def codex_conversation_key_for_message(
    message: Any,
    access: CodexAccess,
    *,
    author: Any | None = None,
) -> str | None:
    guild_id = getattr(getattr(message, "guild", None), "id", None)
    channel = getattr(message, "channel", None)
    channel_id = getattr(channel, "id", None)
    author = getattr(message, "author", None) if author is None else author
    user_id = getattr(author, "id", None)
    if not all(type(value) is int and value > 0 for value in (
        guild_id,
        channel_id,
        user_id,
    )):
        return None

    is_thread = str(getattr(channel, "type", "")) in _THREAD_CHANNEL_TYPES
    allowed_channel_id = (
        getattr(channel, "parent_id", None) if is_thread else channel_id
    )
    if type(allowed_channel_id) is not int or not access.allows(
        guild_id,
        allowed_channel_id,
        member_role_ids(author),
    ):
        return None
    return conversation_key(
        guild_id,
        channel_id,
        user_id,
        is_thread=is_thread,
    )


def codex_error_text(code: str) -> str:
    if code == "busy":
        return "AI 目前忙碌，請稍後再試。"
    if code == "unauthorized":
        return "Codex 目前未對此身分組或頻道開放。"
    if code == "auth_required":
        return "AI 尚未完成登入，請聯絡管理員。"
    if code == "timeout":
        return "AI 回覆逾時，請稍後再試。"
    if code == "usage_limit_or_unavailable":
        return "Codex 額度已用盡或服務暫時無法使用，請稍後再試。"
    return "AI 服務暫時無法回覆，請稍後再試。"


async def get_referenced_message(message: Any) -> Any | None:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None

    reference_channel_id = getattr(reference, "channel_id", None)
    current_channel_id = getattr(getattr(message, "channel", None), "id", None)
    if (
        reference_channel_id is not None
        and current_channel_id is not None
        and reference_channel_id != current_channel_id
    ):
        return None

    resolved = getattr(reference, "resolved", None)
    if resolved is not None and (
        getattr(resolved, "author", None) is not None
        or hasattr(resolved, "attachments")
    ):
        return resolved

    message_id = getattr(reference, "message_id", None)
    if message_id is None:
        return None

    try:
        return await asyncio.wait_for(message.channel.fetch_message(message_id), 2)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, TimeoutError):
        return None


async def _send_native_ai_chunks(
    message: discord.Message,
    chunks: list[str],
    *,
    reply_first: bool,
    can_send: Any = None,
) -> str:
    if not chunks:
        return "unavailable"

    try:
        start = 0
        if reply_first:
            if can_send is not None and not await can_send():
                return "unauthorized"
            await message.reply(
                chunks[0],
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            start = 1

        for chunk in chunks[start:]:
            if can_send is not None and not await can_send():
                return "unauthorized"
            await message.channel.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except discord.HTTPException:
        logging.error("Discord AI 回覆送出失敗。")
        return "unavailable"
    return "success"


async def send_ai_answer(
    message: discord.Message, answer: str, *,
    text_display_enabled: bool = True, can_send: Any = None,
) -> str:
    if not text_display_enabled:
        return await _send_native_ai_chunks(
            message,
            split_discord_message(answer),
            reply_first=True,
            can_send=can_send,
        )

    display_chunks = split_discord_text_display(answer)
    sent_chunks: list[str] = []
    try:
        for index, chunk in enumerate(display_chunks):
            if can_send is not None and not await can_send():
                return "unauthorized"
            view = build_ai_text_display_view(chunk)
            if index == 0:
                await message.reply(
                    view=view,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await message.channel.send(
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            sent_chunks.append(chunk)
    except discord.HTTPException:
        logging.error("Discord AI TextDisplay 回覆送出失敗，改用原生文字。")
        remaining = "".join(display_chunks[len(sent_chunks) :])
        return await _send_native_ai_chunks(
            message,
            split_discord_message(remaining or answer),
            reply_first=not sent_chunks,
            can_send=can_send,
        )
    return "success" if sent_chunks else "unavailable"


async def handle_message(
    message: discord.Message, *, bot_user_id: int | None,
    codex: CodexBridgeClient, access: CodexAccess,
    member_cache_enabled: bool, text_display_enabled: bool,
) -> None:
    if message.author.bot or message.webhook_id is not None or bot_user_id is None:
        return

    content = message.content.strip()
    attachments = list(message.attachments)
    if not content and not attachments:
        return

    referenced_message = None
    if not message_mentions_bot(message, bot_user_id):
        referenced_message = await get_referenced_message(message)
        if getattr(getattr(referenced_message, "author", None), "id", None) != bot_user_id:
            return

    key = codex_conversation_key_for_message(message, access)
    if key is None:
        await message.reply(
            "Codex 目前未對此身分組或頻道開放。",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    cleaned_content = clean_bot_mention(content, bot_user_id)
    if not codex.try_start_request(message.author.id):
        await message.reply(
            "請稍候幾秒再試。", mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    queued_at = time.monotonic()
    queue_ms = images_ms = sdk_ms = discord_ms = 0.0
    outcome = "unavailable"
    output_started = False

    async def send_error(exc: Exception, *, deadline: float | None = None) -> None:
        nonlocal outcome, discord_ms
        outcome = (
            exc.code if isinstance(exc, CodexBridgeError)
            else "timeout" if isinstance(exc, TimeoutError) else "invalid_request"
        )
        if output_started:
            return
        error_text = str(exc) if isinstance(exc, ImageAttachmentError) else codex_error_text(outcome)
        budget = min(5.0, codex.cleanup_timeout_seconds)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                budget = min(budget, remaining)
        started = time.monotonic()
        try:
            async with asyncio.timeout(budget):
                await message.reply(
                    error_text, mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (discord.HTTPException, TimeoutError):
            logging.error("Discord AI 狀態回覆送出失敗。")
        finally:
            discord_ms += (time.monotonic() - started) * 1000

    try:
        async with codex.accepted_request(
            key, access=access, user_id=message.author.id,
        ) as job:
            queue_ms = ((job.started_at or time.monotonic()) - job.accepted_at) * 1000

            try:
                async with asyncio.timeout_at(job.deadline):
                    async def can_send() -> bool:
                        if not job.current:
                            return False
                        guild = message.guild
                        author = None
                        if member_cache_enabled:
                            author = guild.get_member(message.author.id)
                        if author is None:
                            try:
                                author = await asyncio.wait_for(guild.fetch_member(message.author.id), 2)
                            except (discord.HTTPException, TimeoutError, AttributeError):
                                return False
                        if author is None:
                            return False
                        return job.current and codex_conversation_key_for_message(
                            message, access, author=author,
                        ) == key

                    if not await can_send():
                        raise CodexBridgeError("unauthorized")
                    image_attachments = select_image_attachments(attachments)
                    if not image_attachments:
                        if referenced_message is None and getattr(message, "reference", None) is not None:
                            referenced_message = await get_referenced_message(message)
                        image_attachments = select_image_attachments(list(
                            getattr(referenced_message, "attachments", ())
                            if referenced_message is not None else ()
                        ))
                    if not cleaned_content and not image_attachments:
                        await message.reply(
                            "請輸入問題，或附上 JPEG、PNG、WebP 圖片。",
                            mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
                        )
                        outcome = "invalid_request"
                        return
                    async with message.channel.typing():
                        started = time.monotonic()
                        try:
                            async with asyncio.timeout(codex.image_timeout_seconds):
                                images = await read_image_attachments(image_attachments)
                        finally:
                            images_ms = (time.monotonic() - started) * 1000
                        if not await can_send():
                            raise CodexBridgeError("unauthorized")
                        started = time.monotonic()
                        try:
                            answer = await codex.chat(key, cleaned_content, images)
                        finally:
                            sdk_ms = (time.monotonic() - started) * 1000
                    output_started = True
                    started = time.monotonic()
                    try:
                        outcome = await send_ai_answer(
                            message, answer, text_display_enabled=text_display_enabled, can_send=can_send,
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
        await send_error(exc)
    finally:
        logging.info(
            "AI request result=%s queue_ms=%.1f images_ms=%.1f sdk_ms=%.1f discord_ms=%.1f",
            outcome, queue_ms, images_ms, sdk_ms, discord_ms,
        )


async def handle_member_update(
    after: discord.Member, *, codex: CodexBridgeClient, access: CodexAccess,
) -> None:
    guild_id = getattr(getattr(after, "guild", None), "id", None)
    if type(guild_id) is int:
        if not access.role_ids.intersection(member_role_ids(after)):
            try:
                await codex.cancel_member(guild_id, after.id)
            except CodexBridgeError:
                logging.error("Codex revoked member cleanup failed.")


async def archive_scope(
    codex: CodexBridgeClient, guild_id: int, channel_id: int | None = None,
) -> None:
    try:
        await codex.archive_scope(guild_id, channel_id)
    except Exception:
        logging.error("Codex scope archive failed.")
