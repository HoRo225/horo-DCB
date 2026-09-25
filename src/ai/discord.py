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
    ImageAttachmentError, MediaBudget, read_image_attachments, read_message_media,
    select_image_attachments, select_message_media,
)
from src.ai.media_executor import MediaExecutor
from src.ai.output import (
    MAX_DISCORD_RESPONSE_CHUNKS,
    build_ai_native_image_links,
    build_ai_text_display_view,
    split_discord_message,
    split_discord_text_display,
)
from src.ai.protocol import (
    CodexBridgeError, MAX_IMAGE_ATTACHMENTS, MAX_PROMPT_CHARACTERS,
    conversation_key,
)


def clean_bot_mention(content: str, bot_user_id: int) -> str:
    return (
        content.replace(f"<@{bot_user_id}>", "")
        .replace(f"<@!{bot_user_id}>", "")
        .strip()
    )


def message_mentions_bot(message: Any, bot_user_id: int) -> bool:
    return any(getattr(user, "id", None) == bot_user_id for user in message.mentions)


def visible_message_text(message: Any) -> str:
    parts: list[str] = []

    def add(value: object) -> None:
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

    def visit(components: object) -> None:
        if not isinstance(components, (list, tuple)):
            return
        for component in components:
            if isinstance(component, discord.TextDisplay):
                add(component.content)
            elif isinstance(
                component,
                (discord.Container, discord.components.SectionComponent),
            ):
                visit(getattr(component, "children", ()))

    add(getattr(message, "content", ""))
    visit(getattr(message, "components", ()))
    return "\n".join(parts)


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
    if not all(type(value) is int and value > 0 for value in (
        guild_id,
        channel_id,
        user_id,
    )):
        return None, "scope"

    is_thread = str(getattr(channel, "type", "")) in _THREAD_CHANNEL_TYPES
    allowed_channel_id = (
        getattr(channel, "parent_id", None) if is_thread else channel_id
    )
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
        message, access, author=author,
    )[0]


def codex_error_text(code: str) -> str:
    if code == "busy":
        return "AI 目前忙碌，請稍後再試。"
    if code == "unauthorized":
        return "Codex 目前未對此身分組或頻道開放。"
    if code == "auth_required":
        return "AI 尚未完成登入，請聯絡管理員。"
    if code == "timeout":
        return "AI 回覆逾時，請稍後再試。"
    if code == "model_capacity":
        return "目前模型滿載，請稍後再試。"
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
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, aiohttp.ClientError, TimeoutError):
        return None


def build_codex_prompt(question: str, referenced_message: Any | None) -> str:
    if referenced_message is None:
        return question
    referenced = visible_message_text(referenced_message)
    question = question or "請說明我回覆的內容。"
    prefix = "【被回覆的訊息】\n"
    middle = "\n\n【本次問題】\n"
    note = (
        "\n\n（圖片輸入若存在，順序為被回覆訊息在前、本次訊息在後；"
        "動畫圖片由左到右依時間順序取樣。）"
    )
    question = question[: max(
        0, MAX_PROMPT_CHARACTERS - len(prefix) - len(middle) - len(note),
    )]
    available = max(
        0,
        MAX_PROMPT_CHARACTERS - len(prefix) - len(middle)
        - len(question) - len(note),
    )
    referenced = referenced[:available]
    return f"{prefix}{referenced}{middle}{question}{note}"


async def _send_native_ai_chunks(
    message: discord.Message,
    chunks: list[str],
    *,
    reply_first: bool,
    image_urls: tuple[str, ...] = (),
    can_send: Any,
) -> str:
    link_message = build_ai_native_image_links(image_urls)
    if not chunks and not link_message:
        return "unavailable"

    try:
        start = 0
        sent_any = False
        if reply_first and chunks:
            if not await can_send():
                return "unauthorized"
            await message.reply(
                chunks[0],
                mention_author=False,
            )
            start = 1
            sent_any = True

        for chunk in chunks[start:]:
            if not await can_send():
                return "unauthorized"
            await message.channel.send(
                chunk,
            )
            sent_any = True

        if link_message:
            if not await can_send():
                return "unauthorized"
            if not sent_any and reply_first:
                await message.reply(
                    link_message,
                    mention_author=False,
                )
            else:
                await message.channel.send(
                    link_message,
                )
            sent_any = True
    except (discord.HTTPException, aiohttp.ClientError):
        logging.error("Discord AI 回覆送出失敗。")
        return "unavailable"
    return "success" if sent_any else "unavailable"


async def send_ai_answer(
    message: discord.Message, answer: str, *,
    image_urls: tuple[str, ...] = (),
    text_display_enabled: bool, can_send: Any,
) -> str:
    if not text_display_enabled:
        return await _send_native_ai_chunks(
            message,
            split_discord_message(
                answer,
                max_chunks=MAX_DISCORD_RESPONSE_CHUNKS - bool(image_urls),
            ),
            reply_first=True,
            image_urls=image_urls,
            can_send=can_send,
        )

    display_chunks = split_discord_text_display(answer)
    sent_count = 0
    try:
        for index, chunk in enumerate(display_chunks):
            if not await can_send():
                return "unauthorized"
            view = build_ai_text_display_view(
                chunk, image_urls=image_urls if index == 0 else (),
            )
            if index == 0:
                await message.reply(
                    view=view,
                    mention_author=False,
                )
            else:
                await message.channel.send(
                    view=view,
                )
            sent_count += 1
    except aiohttp.ClientError:
        logging.error("Discord AI TextDisplay 回覆送出失敗。")
        return "unavailable"
    except discord.HTTPException as exc:
        if exc.status in {403, 404}:
            logging.error("Discord AI TextDisplay 回覆無法送達。")
            return "unavailable"
        logging.error("Discord AI TextDisplay 回覆送出失敗，改用原生文字。")
        remaining = (
            answer
            if not sent_count
            else "\n".join(display_chunks[sent_count:])
        )
        return await _send_native_ai_chunks(
            message,
            split_discord_message(
                remaining,
                max_chunks=MAX_DISCORD_RESPONSE_CHUNKS
                - bool(image_urls if not sent_count else ()),
            ),
            reply_first=not sent_count,
            image_urls=image_urls if not sent_count else (),
            can_send=can_send,
        )
    return "success" if sent_count else "unavailable"


async def handle_message(
    message: discord.Message, *, bot_user_id: int | None,
    codex: CodexBridgeClient, access: CodexAccess,
    member_cache_enabled: bool, text_display_enabled: bool,
    media_executor: MediaExecutor,
) -> None:
    if message.author.bot or message.webhook_id is not None or bot_user_id is None:
        return

    content = message.content.strip()
    attachments = list(message.attachments)
    if (
        not content and not attachments
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
            "請稍候幾秒再試。", mention_author=False,
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
        budget = codex.cleanup_timeout_seconds
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                budget = min(budget, remaining)
        started = time.monotonic()
        try:
            async with asyncio.timeout(budget):
                await message.reply(
                    error_text, mention_author=False,
                )
        except (discord.HTTPException, TimeoutError):
            logging.error("Discord AI 狀態回覆送出失敗。")
        finally:
            discord_ms += (time.monotonic() - started) * 1000

    try:
        async with codex.accepted_request(
            key, access=access, user_id=message.author.id,
        ) as job:
            assert job.started_at is not None
            queue_ms = (job.started_at - job.accepted_at) * 1000

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
                            except (discord.HTTPException, aiohttp.ClientError, TimeoutError, AttributeError):
                                return False
                        if author is None:
                            return False
                        return job.current and codex_conversation_key_for_message(
                            message, access, author=author,
                        ) == key

                    if not await can_send():
                        raise CodexBridgeError("unauthorized")
                    if len(cleaned_content) > MAX_PROMPT_CHARACTERS:
                        await message.reply(
                            "問題最多 4,000 個字元，請縮短後再試。",
                            mention_author=False,
                        )
                        outcome = "invalid_request"
                        return
                    if (
                        mentions_bot and referenced_message is None
                        and getattr(message, "reference", None) is not None
                    ):
                        referenced_message = await get_referenced_message(message)
                        if referenced_message is None:
                            await message.reply(
                                "目前無法讀取被回覆的訊息，請重新回覆或重新上傳內容。",
                                mention_author=False,
                            )
                            outcome = "invalid_request"
                            return

                    referenced_context = referenced_message if mentions_bot else None
                    if referenced_context is not None:
                        image_attachments = select_image_attachments([
                            *getattr(referenced_context, "attachments", ()),
                            *attachments,
                        ])
                        media_messages = [referenced_context, message]
                    else:
                        image_attachments = select_image_attachments(attachments)
                        if not image_attachments and referenced_message is not None:
                            image_attachments = select_image_attachments(list(
                                getattr(referenced_message, "attachments", ())
                            ))
                        media_messages = [message]

                    media_sources = select_message_media(media_messages)
                    if len(image_attachments) + len(media_sources) > MAX_IMAGE_ATTACHMENTS:
                        raise ImageAttachmentError(f"一次最多處理 {MAX_IMAGE_ATTACHMENTS} 張圖片，請減少圖片後再試。")
                    media_budget = MediaBudget()
                    async with message.channel.typing():
                        started = time.monotonic()
                        try:
                            media_deadline = min(
                                job.deadline,
                                asyncio.get_running_loop().time()
                                + codex.image_timeout_seconds,
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
                            await message.reply(
                                "請輸入問題，或附上圖片、GIF、表情或貼圖。",
                                mention_author=False,
                            )
                            outcome = "invalid_request"
                            return
                        prompt = build_codex_prompt(cleaned_content, referenced_context)
                        if not prompt and images:
                            prompt = "請說明我提供的內容。"
                        if not await can_send():
                            raise CodexBridgeError("unauthorized")
                        started = time.monotonic()
                        try:
                            reply = await codex.chat(key, prompt, images, job=job)
                        finally:
                            sdk_ms = (time.monotonic() - started) * 1000
                    output_started = True
                    started = time.monotonic()
                    try:
                        outcome = await send_ai_answer(
                            message, reply.text, image_urls=reply.image_urls,
                            text_display_enabled=text_display_enabled, can_send=can_send,
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
