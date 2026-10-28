from __future__ import annotations

import asyncio
import base64
import os
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
import discord

from src.ai.domain.conversation import (
    MAX_IMAGE_ATTACHMENTS,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_TOTAL_BYTES,
    safe_https_hostname,
)
from src.ai.frames import extract_frames

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}
_CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_~]+:([1-9][0-9]*)>")
_LABELS = {"video": "影片", "lottie": "Lottie 貼圖"}
MEDIA_CHUNK_BYTES = 64 * 1024
SUPPORTED_IMAGE_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
}


def image_signature_matches(content_type: str, data: bytes) -> bool:
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if content_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    return False


class ImageAttachmentError(ValueError):
    pass


def validate_image_size(size: object, total_size: int) -> None:
    if type(size) is not int or size < 0:
        raise ImageAttachmentError("目前無法確認檔案大小，請重新上傳後再試。")
    if size > MAX_IMAGE_BYTES:
        raise ImageAttachmentError(f"單一檔案最多 {MAX_IMAGE_BYTES // 2**20} MB。")
    if total_size > MAX_IMAGE_TOTAL_BYTES:
        raise ImageAttachmentError(f"本次媒體總大小最多 {MAX_IMAGE_TOTAL_BYTES // 2**20} MB。")


MediaItem = tuple[str, tuple[str, ...]]


@dataclass(slots=True)
class MediaBudget:
    source_bytes: int = 0
    output_bytes: int = 0
    images_left: int = MAX_IMAGE_ATTACHMENTS
    skipped: int = 0
    broken: int = 0
    timed_out: int = 0

    def add_source(self, size: int) -> None:
        validate_image_size(size, self.source_bytes + size)
        self.source_bytes += size

    def data_url(self, content_type: str, data: bytes) -> str:
        validate_image_size(len(data), self.output_bytes + len(data))
        self.output_bytes += len(data)
        self.images_left -= 1
        return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def _attachment_kind(attachment: Any) -> str | None:
    content_type = getattr(attachment, "content_type", None)
    filename = getattr(attachment, "filename", "")
    extension = os.path.splitext(filename)[1].lower() if isinstance(filename, str) else ""
    if isinstance(content_type, str) and content_type.startswith("video/"):
        return "video"
    is_image_like = (
        isinstance(content_type, str) and content_type.startswith("image/")
    ) or extension in IMAGE_EXTENSIONS
    if not is_image_like:
        return None
    if (
        content_type not in SUPPORTED_IMAGE_TYPES
        or extension not in SUPPORTED_IMAGE_TYPES[content_type]
    ):
        return "broken"
    return "media"


def _is_safe_discord_url(url: str) -> bool:
    host = safe_https_hostname(url)
    return host is not None and (
        host in {"cdn.discordapp.com", "media.discordapp.net"}
        or host.endswith(".discordapp.com")
        or host.endswith(".discordapp.net")
    )


def _discord_media_url(value: Any) -> str | None:
    for name in ("url", "proxy_url"):
        url = getattr(value, name, None)
        if isinstance(url, str) and _is_safe_discord_url(url):
            return url
    return None


def select_message_media(
    messages: list[Any],
    *,
    seen: set[str] | None = None,
) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    seen = set() if seen is None else seen

    def add(kind: str, url: str | None, key: str | None = None) -> None:
        if url is None or not _is_safe_discord_url(url):
            return
        identity = key or url
        if identity not in seen:
            seen.add(identity)
            selected.append((kind, url))

    def visit_components(components: object) -> None:
        if not isinstance(components, (list, tuple)):
            return
        for component in components:
            if isinstance(component, discord.components.MediaGalleryComponent):
                for item in component.items:
                    media = getattr(item, "media", None)
                    attachment_id = getattr(media, "attachment_id", None)
                    add(
                        "media",
                        _discord_media_url(media),
                        f"attachment:{attachment_id}" if type(attachment_id) is int else None,
                    )
            elif isinstance(component, discord.Container):
                visit_components(getattr(component, "children", ()))

    for message in messages:
        for attachment in getattr(message, "attachments", ()):
            kind = _attachment_kind(attachment)
            if kind is not None:
                add(kind, _discord_media_url(attachment), f"attachment:{attachment.id}")

        content = getattr(message, "content", "")
        if isinstance(content, str):
            for match in _CUSTOM_EMOJI.finditer(content):
                emoji = discord.PartialEmoji.from_str(match.group(0))
                add("media", str(emoji.url), f"emoji:{emoji.id}")

        for embed in getattr(message, "embeds", ()):
            video = _discord_media_url(getattr(embed, "video", None))
            if video is not None:
                add("video", video)
            else:
                add(
                    "media",
                    _discord_media_url(getattr(embed, "image", None))
                    or _discord_media_url(getattr(embed, "thumbnail", None)),
                )

        for sticker in getattr(message, "stickers", ()):
            format_name = getattr(getattr(sticker, "format", None), "name", "").casefold()
            raw_url = getattr(sticker, "url", None)
            sticker_id = getattr(sticker, "id", None)
            add(
                "lottie" if format_name == "lottie" else "media",
                str(raw_url) if raw_url is not None else None,
                f"sticker:{sticker_id}" if sticker_id is not None else None,
            )

        visit_components(getattr(message, "components", ()))
    return selected


def _image_type(data: bytes) -> str | None:
    return next(
        (kind for kind in SUPPORTED_IMAGE_TYPES if image_signature_matches(kind, data)), None
    )


def _needs_frames(content_type: str, data: bytes) -> bool:
    if content_type == "image/gif":
        return True
    if content_type == "image/png":
        actl, idat = data.find(b"acTL"), data.find(b"IDAT")
        return actl != -1 and (idat == -1 or actl < idat)
    return (
        content_type == "image/webp"
        and len(data) > 20
        and data[12:16] == b"VP8X"
        and bool(data[20] & 0x02)
    )


async def _download_discord_media(
    session: aiohttp.ClientSession, url: str, *, total_before: int
) -> bytes | None:
    if not _is_safe_discord_url(url):
        raise ImageAttachmentError("不支援從這個網址讀取媒體。")
    try:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200:
                return None
            if response.content_length is not None:
                validate_image_size(response.content_length, total_before + response.content_length)
            data = bytearray()
            async for chunk in response.content.iter_chunked(MEDIA_CHUNK_BYTES):
                data.extend(chunk)
                validate_image_size(len(data), total_before + len(data))
            return bytes(data)
    except aiohttp.ClientError, OSError:
        return None


async def read_message_media(
    sources: list[tuple[str, str]], *, budget: MediaBudget, deadline: float | None = None
) -> list[MediaItem]:
    result: list[MediaItem] = []
    if not sources:
        return result

    timeout = aiohttp.ClientTimeout(total=15, connect=3, sock_read=10)
    index = 0
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session, asyncio.timeout_at(deadline):
            for index, (kind, url) in enumerate(sources):
                if kind == "broken":
                    budget.broken += 1
                    continue
                if budget.images_left <= 0:
                    budget.skipped += 1
                    continue
                data = await _download_discord_media(session, url, total_before=budget.source_bytes)
                if data is None:
                    budget.broken += 1
                    continue
                budget.add_source(len(data))
                content_type = _image_type(data)
                if kind == "media" and content_type is None:
                    budget.broken += 1
                    continue
                if kind == "media" and not _needs_frames(content_type, data):
                    result.append(("靜態圖", (budget.data_url(content_type, data),)))
                    continue
                frames = await extract_frames("image" if kind == "media" else kind, data)
                if not frames:
                    budget.broken += 1
                    continue
                result.append(
                    (
                        _LABELS.get(kind, "動畫"),
                        tuple(
                            budget.data_url("image/jpeg", frame)
                            for frame in frames[: budget.images_left]
                        ),
                    )
                )
    except TimeoutError:
        budget.timed_out += len(sources) - index
    return result


def describe_media(items: list[MediaItem], skipped: int, broken: int, timed_out: int) -> str:
    parts = []
    start = 1
    for label, images in items:
        end = start + len(images) - 1
        if label != "靜態圖":
            span = f"圖 {start}" if start == end else f"圖 {start}–{end}"
            parts.append(f"{span}：{label}依時間順序取樣的 {len(images)} 格")
        start = end + 1
    if skipped:
        parts.append(f"另有 {skipped} 個媒體因數量上限未讀取")
    if broken:
        parts.append(f"另有 {broken} 個媒體無法讀取")
    if timed_out:
        parts.append(f"另有 {timed_out} 個媒體逾時未讀取")
    return f"\n（{'；'.join(parts)}。）" if parts else ""
