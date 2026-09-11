from __future__ import annotations

import base64
import os
from typing import Any

import discord

from src.ai.protocol import (
    ImageAttachmentError, MAX_IMAGE_ATTACHMENTS,
    validate_image_bytes, validate_image_size,
)

SUPPORTED_IMAGE_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}


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
        validate_image_size(size, 0)
        total_size += size
    validate_image_size(0, total_size)
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
        total_bytes += len(data)
        validate_image_bytes(content_type, data, total_bytes)

        encoded = base64.b64encode(data).decode("ascii")
        data_urls.append(f"data:{content_type};base64,{encoded}")
    return tuple(data_urls)
