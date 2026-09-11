from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import re


MAX_IMAGE_ATTACHMENTS = 4
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 16 * 1024 * 1024


def image_signature_matches(content_type: str, data: bytes) -> bool:
    if content_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if content_type == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


SAFE_ERROR_CODES = {
    "auth_required",
    "busy",
    "invalid_request",
    "timeout",
    "unauthorized",
    "unavailable",
    "usage_limit_or_unavailable",
}


def conversation_key(
    guild_id: int,
    channel_id: int,
    user_id: int,
    *,
    is_thread: bool,
) -> str:
    if is_thread:
        return f"guild:{guild_id}:thread:{channel_id}"
    return f"guild:{guild_id}:channel:{channel_id}:user:{user_id}"


class CodexBridgeError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in SAFE_ERROR_CODES else "unavailable"
        super().__init__(self.code)


def scope_matches(key: str, guild_id: int, channel_id: int | None = None) -> bool:
    prefix = f"guild:{guild_id}:"
    return key.startswith(prefix) and (
        channel_id is None
        or key == f"{prefix}thread:{channel_id}"
        or key.startswith(f"{prefix}channel:{channel_id}:user:")
    )


@dataclass(frozen=True, slots=True)
class CodexRuntimeStatus:
    available: bool
    authenticated: bool
    plan: str | None
    sdk_version: str | None
    runtime_version: str | None
    web_search: str | None
    thread_count: int
    active_requests: int = 0
    queued_requests: int = 0
    last_error: str | None = None
    bot_active_requests: int = 0
    bot_queued_requests: int = 0


_THREAD_KEY = re.compile(
    r"^guild:[1-9][0-9]*:(?:thread:[1-9][0-9]*|channel:[1-9][0-9]*:user:[1-9][0-9]*)$"
)
_IMAGE_PREFIXES = {
    "data:image/jpeg;base64,": "image/jpeg",
    "data:image/png;base64,": "image/png",
    "data:image/webp;base64,": "image/webp",
}
_MAX_ENCODED_IMAGE_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4


class BridgeRequestError(RuntimeError):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class ChatPayload:
    conversation_key: str
    text: str
    images: tuple[str, ...]


def validate_chat_payload(value: object) -> ChatPayload:
    if not isinstance(value, dict) or set(value) != {
        "conversation_key",
        "text",
        "images",
    }:
        raise BridgeRequestError("invalid_request")

    key = value["conversation_key"]
    text = value["text"]
    images = value["images"]
    if not valid_conversation_key(key):
        raise BridgeRequestError("invalid_request")
    if not isinstance(text, str) or len(text) > 4000:
        raise BridgeRequestError("invalid_request")
    if not isinstance(images, list) or len(images) > MAX_IMAGE_ATTACHMENTS:
        raise BridgeRequestError("invalid_request")

    total_image_bytes = 0
    for image in images:
        if not isinstance(image, str):
            raise BridgeRequestError("invalid_request")
        match = next(
            (
                (prefix, content_type)
                for prefix, content_type in _IMAGE_PREFIXES.items()
                if image.startswith(prefix)
            ),
            None,
        )
        if match is None:
            raise BridgeRequestError("invalid_request")
        prefix, content_type = match
        encoded = image[len(prefix) :]
        if not encoded or len(encoded) > _MAX_ENCODED_IMAGE_CHARS:
            raise BridgeRequestError("invalid_request")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise BridgeRequestError("invalid_request") from None
        total_image_bytes += len(data)
        try:
            validate_image_bytes(content_type, data, total_image_bytes)
        except ImageAttachmentError:
            raise BridgeRequestError("invalid_request") from None
    if not text.strip() and not images:
        raise BridgeRequestError("invalid_request")
    return ChatPayload(key, text, tuple(images))


def valid_conversation_key(key: object) -> bool:
    return isinstance(key, str) and _THREAD_KEY.fullmatch(key) is not None


def valid_bridge_token(token: str) -> bool:
    return len(token) == 64 and all(character in "0123456789abcdef" for character in token)


class ImageAttachmentError(ValueError):
    pass


def validate_image_size(size: object, total_size: int) -> None:
    if type(size) is not int or size < 0:
        raise ImageAttachmentError("目前無法確認圖片大小，請重新上傳後再試。")
    if size > MAX_IMAGE_BYTES:
        raise ImageAttachmentError("單張圖片最多 8 MB。")
    if total_size > MAX_IMAGE_TOTAL_BYTES:
        raise ImageAttachmentError("本次圖片總大小最多 16 MB。")


def validate_image_bytes(content_type: str, data: bytes, total_size: int) -> None:
    validate_image_size(len(data), total_size)
    if not image_signature_matches(content_type, data):
        raise ImageAttachmentError("圖片格式驗證失敗，請重新上傳 JPEG、PNG 或 WebP。")
