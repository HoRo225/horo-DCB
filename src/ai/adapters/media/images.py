from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from src.ai.adapters.media.frames import Frames
from src.ai.domain.conversation import (
    MAX_IMAGE_ATTACHMENTS,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_TOTAL_BYTES,
    is_discord_media_url,
)

MEDIA_CHUNK_BYTES = 64 * 1024
STILL_AS_IS_BYTES = 2**20
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15, connect=3, sock_read=10)
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


def first_frame_url(url: str) -> str:
    scheme, host, path, query, fragment = urlsplit(url)
    return urlunsplit((scheme, host, path, f"{query}&format=jpeg" if query else "format=jpeg", fragment))


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


async def download_discord_media(
    session: aiohttp.ClientSession, url: str, *, total_before: int
) -> bytes | None:
    if not is_discord_media_url(url):
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


type Fetch = Callable[[str, int], Awaitable[bytes | None]]


async def read_message_media(
    sources: list[tuple[str, str]], *, budget: MediaBudget, frames: Frames, fetch: Fetch,
    deadline: float | None = None,
) -> list[MediaItem]:
    result: list[MediaItem] = []
    if not sources:
        return result

    index = 0
    try:
        async with asyncio.timeout_at(deadline):
            for index, (kind, url) in enumerate(sources):
                if kind == "broken":
                    budget.broken += 1
                    continue
                if budget.images_left <= 0:
                    budget.skipped += 1
                    continue
                data = await fetch(first_frame_url(url) if kind == "video" else url, budget.source_bytes)
                if data is None:
                    budget.broken += 1
                    continue
                budget.add_source(len(data))
                content_type = _image_type(data)
                if content_type is None:
                    budget.broken += 1
                    continue
                if kind == "video" or not _needs_frames(content_type, data):
                    label = "影片" if kind == "video" else "靜態圖"
                    if len(data) > STILL_AS_IS_BYTES:
                        data, content_type = await frames.shrink(data), "image/jpeg"
                        if data is None:
                            budget.broken += 1
                            continue
                    result.append((label, (budget.data_url(content_type, data),)))
                    continue
                sampled = await frames.extract(data)
                if not sampled:
                    budget.broken += 1
                    continue
                result.append(
                    (
                        "動畫",
                        tuple(
                            budget.data_url("image/jpeg", frame)
                            for frame in sampled[: budget.images_left]
                        ),
                    )
                )
    except TimeoutError:
        budget.timed_out += len(sources) - index
    return result


type MediaRead = tuple[list[list[MediaItem]], int, int, int]


class MediaReader:
    def __init__(self, frames: Frames) -> None:
        self._frames = frames

    async def read(self, groups: list[list[tuple[str, str]]], *, deadline: float) -> MediaRead:
        budget = MediaBudget()
        if not any(groups):
            return [[] for _sources in groups], 0, 0, 0
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            async def fetch(url: str, total_before: int) -> bytes | None:
                return await download_discord_media(session, url, total_before=total_before)

            items = [
                await read_message_media(sources, budget=budget, frames=self._frames, fetch=fetch, deadline=deadline)
                for sources in groups
            ]
        return items, budget.skipped, budget.broken, budget.timed_out


def describe_media(items: list[MediaItem], skipped: int, broken: int, timed_out: int) -> str:
    parts = []
    start = 1
    for label, images in items:
        end = start + len(images) - 1
        if label != "靜態圖":
            span = f"圖 {start}" if start == end else f"圖 {start}–{end}"
            parts.append(f"{span}：影片的第一格" if label == "影片" else f"{span}：{label}依時間順序取樣的 {len(images)} 格")
        start = end + 1
    if skipped:
        parts.append(f"另有 {skipped} 個媒體因數量上限未讀取")
    if broken:
        parts.append(f"另有 {broken} 個媒體無法讀取")
    if timed_out:
        parts.append(f"另有 {timed_out} 個媒體逾時未讀取")
    return f"\n（{'；'.join(parts)}。）" if parts else ""
