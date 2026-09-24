from __future__ import annotations

import base64
from dataclasses import dataclass
import os
import re
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import discord
from src.ai.media_executor import MediaExecutor
from src.ai.protocol import (
    ImageAttachmentError, MAX_IMAGE_ATTACHMENTS,
    validate_image_bytes, validate_image_size,
)

SUPPORTED_IMAGE_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
_CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_~]+:([1-9][0-9]*)>")


@dataclass(slots=True)
class MediaBudget:
    source_bytes: int = 0
    output_bytes: int = 0

    def add_source(self, size: int) -> None:
        validate_image_size(size, self.source_bytes + size)
        self.source_bytes += size

    def add_output(self, size: int) -> None:
        try:
            validate_image_size(size, self.output_bytes + size)
        except ImageAttachmentError as exc:
            raise ImageAttachmentError(
                f"轉換後的圖片超過大小限制：{exc} 請減少圖片或縮小尺寸後再試。"
            ) from exc
        self.output_bytes += size


def select_image_attachments(attachments: list[Any]) -> list[Any]:
    selected = []
    for attachment in attachments:
        content_type = getattr(attachment, "content_type", None)
        filename = getattr(attachment, "filename", "")
        extension = os.path.splitext(filename)[1].lower() if isinstance(filename, str) else ""
        is_image_like = (
            isinstance(content_type, str) and content_type.startswith("image/")
        ) or extension in IMAGE_EXTENSIONS
        if not is_image_like:
            continue
        if content_type not in SUPPORTED_IMAGE_TYPES or extension not in SUPPORTED_IMAGE_TYPES[content_type]:
            raise ImageAttachmentError("目前只支援 JPEG、PNG、WebP 與 GIF 圖片。")
        selected.append(attachment)

    if len(selected) > MAX_IMAGE_ATTACHMENTS:
        raise ImageAttachmentError(f"一次最多處理 {MAX_IMAGE_ATTACHMENTS} 張圖片。")
    total_size = 0
    for attachment in selected:
        size = getattr(attachment, "size", None)
        validate_image_size(size, 0)
        total_size += size
    validate_image_size(0, total_size)
    return selected


def _data_url(content_type: str, data: bytes, budget: MediaBudget) -> str:
    budget.add_output(len(data))
    validate_image_bytes(content_type, data, budget.output_bytes)
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


async def read_image_attachments(
    attachments: list[Any], *, executor: MediaExecutor, deadline: float,
    budget: MediaBudget,
) -> tuple[str, ...]:
    data_urls = []
    for attachment in attachments:
        try:
            data = await attachment.read()
        except (discord.HTTPException, aiohttp.ClientError, OSError) as exc:
            raise ImageAttachmentError("目前無法讀取這張圖片，請重新上傳後再試。") from exc
        budget.add_source(len(data))
        content_type, normalized = await executor.decode(
            "image", data, getattr(attachment, "content_type", None),
            deadline=deadline,
        )
        data_urls.append(_data_url(content_type, normalized, budget))
    return tuple(data_urls)


def _is_safe_discord_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").casefold()
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and (
            host in {"cdn.discordapp.com", "media.discordapp.net"}
            or host.endswith(".discordapp.com")
            or host.endswith(".discordapp.net")
        )
    )


def _discord_media_url(value: Any) -> str | None:
    for name in ("proxy_url", "url"):
        url = getattr(value, name, None)
        if isinstance(url, str) and _is_safe_discord_url(url):
            return url
    return None


def select_message_media(messages: list[Any]) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(kind: str, url: str | None, key: str | None = None) -> None:
        if url is None or not _is_safe_discord_url(url):
            return
        identity = key or url
        if identity not in seen:
            seen.add(identity)
            selected.append((kind, url))

    for message in messages:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            for match in _CUSTOM_EMOJI.finditer(content):
                emoji = discord.PartialEmoji.from_str(match.group(0))
                add("image", str(emoji.url), f"emoji:{emoji.id}")

        for embed in getattr(message, "embeds", ()):
            video = _discord_media_url(getattr(embed, "video", None))
            image = _discord_media_url(getattr(embed, "image", None))
            thumbnail = _discord_media_url(getattr(embed, "thumbnail", None))
            if video is not None:
                add("video", video)
            elif image is not None:
                add("image", image)
            else:
                add("image", thumbnail)

        for sticker in getattr(message, "stickers", ()):
            raw_url = getattr(sticker, "url", None)
            url = str(raw_url) if raw_url is not None else None
            sticker_format = getattr(sticker, "format", None)
            kind = "lottie" if getattr(sticker_format, "name", "").casefold() == "lottie" else "image"
            sticker_id = getattr(sticker, "id", None)
            add(kind, url, f"sticker:{sticker_id}" if sticker_id is not None else None)
    return selected


async def _download_discord_media(
    session: aiohttp.ClientSession, url: str, *, total_before: int
) -> tuple[bytes, str | None]:
    if not _is_safe_discord_url(url):
        raise ImageAttachmentError("不支援從這個網址讀取媒體。")
    try:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                raise ImageAttachmentError("目前無法讀取這個 Discord 媒體。")
            if response.content_length is not None:
                validate_image_size(
                    response.content_length, total_before + response.content_length
                )
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                validate_image_size(len(data), total_before + len(data))
            return bytes(data), response.headers.get("Content-Type")
    except ImageAttachmentError:
        raise
    except (aiohttp.ClientError, OSError) as exc:
        raise ImageAttachmentError("目前無法讀取這個 Discord 媒體。") from exc


async def read_message_media(
    messages: list[Any], *, remaining: int, budget: MediaBudget,
    executor: MediaExecutor, deadline: float,
) -> tuple[str, ...]:
    sources = select_message_media(messages)
    if remaining < 0 or len(sources) > remaining:
        raise ImageAttachmentError(f"一次最多處理 {MAX_IMAGE_ATTACHMENTS} 張圖片。")
    if not sources:
        return ()

    result = []
    timeout = aiohttp.ClientTimeout(total=6, connect=3, sock_read=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for kind, url in sources:
            data, content_type = await _download_discord_media(
                session, url, total_before=budget.source_bytes
            )
            budget.add_source(len(data))
            normalized_type, normalized = await executor.decode(
                kind, data, content_type, deadline=deadline,
            )
            result.append(_data_url(normalized_type, normalized, budget))
    return tuple(result)
