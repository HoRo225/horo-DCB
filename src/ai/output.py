from __future__ import annotations

import logging
from typing import Any

import aiohttp
import discord

DISCORD_MESSAGE_LIMIT = 2_000
MAX_DISCORD_RESPONSE_CHUNKS = 8
DISCORD_TEXT_DISPLAY_LIMIT = 4_000
MAX_TEXT_DISPLAY_RESPONSE_CHUNKS = 4
AI_RESPONSE_TRUNCATION_NOTICE = "\n\n（回覆過長，已截斷。）"


def _find_natural_split(text: str, max_chars: int) -> int:
    if len(text) <= max_chars:
        return len(text)
    if max_chars <= 0:
        return 0

    minimum = max_chars // 2
    window = text[:max_chars]
    for separator in ("\n\n", "\n", " "):
        index = window.rfind(separator, minimum)
        if index != -1:
            return index + len(separator)
    return max_chars


def _update_code_fence_language(text: str, language: str | None) -> str | None:
    current = language
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("```"):
            continue
        if current is not None:
            if stripped == "```":
                current = None
            continue

        label = stripped[3:].strip()
        if label and len(label) <= 32 and all(char.isalnum() or char in "+-_.#" for char in label):
            current = label
        else:
            current = ""
    return current


def _code_fence_prefix(language: str | None) -> str:
    if language is None:
        return ""
    return f"```{language}\n"


def _split_discord_markdown(
    text: str,
    *,
    limit: int,
    max_chunks: int,
) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    language: str | None = None

    while remaining and len(chunks) < max_chunks:
        prefix = _code_fence_prefix(language)
        final_language = _update_code_fence_language(remaining, language)
        final_suffix = "\n```" if final_language is not None else ""
        if len(prefix) + len(remaining) + len(final_suffix) <= limit:
            chunks.append(prefix + remaining + final_suffix)
            break

        is_last_chunk = len(chunks) == max_chunks - 1
        if is_last_chunk:
            notice = AI_RESPONSE_TRUNCATION_NOTICE
            raw_budget = max(0, limit - len(prefix) - len(notice))
            split_at = _find_natural_split(remaining, raw_budget)
            segment = remaining[:split_at]
            next_language = _update_code_fence_language(segment, language)
            suffix = "\n```" if next_language is not None else ""
            if len(prefix) + len(segment) + len(suffix) + len(notice) > limit:
                raw_budget = max(0, raw_budget - len(suffix))
                split_at = _find_natural_split(remaining, raw_budget)
                segment = remaining[:split_at]
                next_language = _update_code_fence_language(segment, language)
                suffix = "\n```" if next_language is not None else ""
            chunks.append(prefix + segment + suffix + notice)
            break

        raw_budget = limit - len(prefix)
        split_at = _find_natural_split(remaining, raw_budget)
        segment = remaining[:split_at]
        next_language = _update_code_fence_language(segment, language)
        suffix = "\n```" if next_language is not None else ""

        if len(prefix) + len(segment) + len(suffix) > limit:
            raw_budget = limit - len(prefix) - len(suffix)
            split_at = _find_natural_split(remaining, raw_budget)
            segment = remaining[:split_at]
            next_language = _update_code_fence_language(segment, language)
            suffix = "\n```" if next_language is not None else ""

        chunks.append(prefix + segment + suffix)
        remaining = remaining[split_at:]
        language = next_language

    return chunks


def split_discord_message(
    text: str,
    *,
    max_chunks: int = MAX_DISCORD_RESPONSE_CHUNKS,
) -> list[str]:
    return _split_discord_markdown(
        text,
        limit=DISCORD_MESSAGE_LIMIT,
        max_chunks=max_chunks,
    )


def split_discord_text_display(text: str) -> list[str]:
    return _split_discord_markdown(
        text,
        limit=DISCORD_TEXT_DISPLAY_LIMIT,
        max_chunks=MAX_TEXT_DISPLAY_RESPONSE_CHUNKS,
    )


def build_ai_native_image_links(image_urls: tuple[str, ...]) -> str:
    urls = list(image_urls)
    if not urls:
        return ""

    for omitted in range(len(urls) + 1):
        suffix = f"\n\n（另有 {omitted} 個圖片連結因訊息長度限制省略。）" if omitted else ""
        content = "\n".join(("圖片連結：", *urls)) + suffix
        if len(content) <= DISCORD_MESSAGE_LIMIT:
            return content
        # Drop longest links first; ties keep earlier results.
        urls.remove(max(reversed(urls), key=len))
    raise AssertionError("image link message cannot fit Discord limit")


def build_ai_text_display_view(
    content: str,
    *,
    image_urls: tuple[str, ...] = (),
) -> discord.ui.LayoutView:
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.TextDisplay(content))
    if image_urls:
        gallery = discord.ui.MediaGallery()
        for url in image_urls:
            gallery.add_item(media=url)
        view.add_item(gallery)
    return view


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
    except discord.HTTPException, aiohttp.ClientError:
        logging.error("Discord AI 回覆送出失敗。")
        return "unavailable"
    return "success" if sent_any else "unavailable"


async def send_ai_answer(
    message: discord.Message,
    answer: str,
    *,
    image_urls: tuple[str, ...] = (),
    text_display_enabled: bool,
    can_send: Any,
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
                chunk,
                image_urls=image_urls if index == 0 else (),
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
        remaining = answer if not sent_count else "\n".join(display_chunks[sent_count:])
        return await _send_native_ai_chunks(
            message,
            split_discord_message(
                remaining,
                max_chunks=MAX_DISCORD_RESPONSE_CHUNKS - bool(image_urls if not sent_count else ()),
            ),
            reply_first=not sent_count,
            image_urls=image_urls if not sent_count else (),
            can_send=can_send,
        )
    return "success" if sent_count else "unavailable"
