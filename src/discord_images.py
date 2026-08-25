from __future__ import annotations

import base64
import os
from typing import Any

import discord

MAX_IMAGE_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 16 * 1024 * 1024
SUPPORTED_IMAGE_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}


class ImageAttachmentError(ValueError):
    pass


def _image_signature_matches(content_type: str, data: bytes) -> bool:
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


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
        if (
            content_type not in SUPPORTED_IMAGE_TYPES
            or extension not in SUPPORTED_IMAGE_TYPES[content_type]
        ):
            raise ImageAttachmentError("目前只支援 JPEG、PNG 與 WebP 圖片。")
        selected.append(attachment)

    if len(selected) > MAX_IMAGE_ATTACHMENTS:
        raise ImageAttachmentError(f"一次最多處理 {MAX_IMAGE_ATTACHMENTS} 張圖片。")

    total_size = 0
    for attachment in selected:
        size = getattr(attachment, "size", None)
        if type(size) is not int or size < 0:
            raise ImageAttachmentError("目前無法確認圖片大小，請重新上傳後再試。")
        if size > MAX_IMAGE_BYTES:
            raise ImageAttachmentError("單張圖片最多 8 MB。")
        total_size += size
    if total_size > MAX_IMAGE_TOTAL_BYTES:
        raise ImageAttachmentError("本次圖片總大小最多 16 MB。")
    return selected


async def read_image_attachments(attachments: list[Any]) -> tuple[str, ...]:
    data_urls = []
    total_bytes = 0
    for attachment in attachments:
        try:
            data = await attachment.read()
        except (discord.HTTPException, OSError) as exc:
            raise ImageAttachmentError(
                "目前無法讀取這張圖片，請重新上傳後再試。"
            ) from exc

        content_type = attachment.content_type
        if len(data) > MAX_IMAGE_BYTES:
            raise ImageAttachmentError("單張圖片最多 8 MB。")
        total_bytes += len(data)
        if total_bytes > MAX_IMAGE_TOTAL_BYTES:
            raise ImageAttachmentError("本次圖片總大小最多 16 MB。")
        if not _image_signature_matches(content_type, data):
            raise ImageAttachmentError("圖片格式驗證失敗，請重新上傳 JPEG、PNG 或 WebP。")

        encoded = base64.b64encode(data).decode("ascii")
        data_urls.append(f"data:{content_type};base64,{encoded}")
    return tuple(data_urls)
