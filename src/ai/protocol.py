from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.ai.rate_limits import RATE_LIMIT_ERRORS as RATE_LIMIT_ERRORS
from src.ai.rate_limits import CodexRateLimits as CodexRateLimits
from src.ai.rate_limits import CodexRateWindow as CodexRateWindow
from src.ai.rate_limits import _rate_optional_int as _rate_optional_int
from src.ai.rate_limits import _rate_window as _rate_window
from src.ai.rate_limits import normalize_rate_limits as normalize_rate_limits
from src.ai.rate_limits import parse_rate_limits_payload as parse_rate_limits_payload

MAX_IMAGE_ATTACHMENTS = 4
MAX_PROMPT_CHARACTERS = 4000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 16 * 1024 * 1024
MAX_REPLY_IMAGES = 10
MEDIA_KINDS = frozenset({"image", "video", "lottie"})
MEDIA_HEADER_LIMIT = 1024
MEDIA_CHUNK_BYTES = 64 * 1024
SUPPORTED_IMAGE_TYPES = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "image/gif": {".gif"},
}
OUTPUT_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


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


ERROR_HTTP_STATUS = {
    "auth_required": 503,
    "busy": 429,
    "invalid_request": 400,
    "timeout": 504,
    "unauthorized": 401,
    "unavailable": 503,
    "model_capacity": 503,
    "usage_limit_or_unavailable": 429,
}
SAFE_ERROR_CODES = frozenset(ERROR_HTTP_STATUS)


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
    def __init__(self, code: object) -> None:
        self.code = code if isinstance(code, str) and code in SAFE_ERROR_CODES else "unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class CodexChatReply:
    text: str
    image_urls: tuple[str, ...] = ()


def safe_https_hostname(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    return parsed.hostname.casefold()


def normalize_reply_image_urls(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()

    result: list[str] = []
    seen: set[str] = set()
    for url in value:
        if not isinstance(url, str) or not url or any(char.isspace() for char in url):
            continue
        if safe_https_hostname(url) is None:
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
        if len(result) == MAX_REPLY_IMAGES:
            break
    return tuple(result)


def scope_matches(
    key: str,
    guild_id: int,
    channel_id: int | None = None,
    *,
    parent_channel_id: int | None = None,
    include_children: bool = False,
) -> bool:
    prefix = f"guild:{guild_id}:"
    if not key.startswith(prefix):
        return False
    if channel_id is None:
        return True
    if key.startswith(f"{prefix}channel:{channel_id}:user:"):
        return True
    if key == f"{prefix}thread:{channel_id}":
        return True
    return (
        include_children
        and key.startswith(f"{prefix}thread:")
        and (parent_channel_id is None or parent_channel_id == channel_id)
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
    protocol_version: int = 2
    ready: bool = False
    reason: str = "unavailable"
    status_fetched_at: int | None = None
    status_stale: bool = True


READY_REASONS = frozenset(
    {
        "ready",
        "initializing",
        "auth_required",
        "status_stale",
        "state_unavailable",
        "draining",
        "unavailable",
    }
)


@dataclass(frozen=True, slots=True)
class CodexArchiveResult:
    detached_count: int
    archived_count: int
    archive_unconfirmed_count: int


def parse_archive_result(raw: object) -> CodexArchiveResult:
    fields = {"detached_count", "archived_count", "archive_unconfirmed_count"}
    if (
        not isinstance(raw, dict)
        or set(raw) != fields
        or any(type(raw[field]) is not int or raw[field] < 0 for field in fields)
    ):
        raise ValueError("invalid archive result")
    return CodexArchiveResult(**raw)


EMPTY_CODEX_RUNTIME_STATUS = CodexRuntimeStatus(False, False, None, None, None, None, 0)


_THREAD_KEY = re.compile(
    r"^guild:[1-9][0-9]*:(?:thread:[1-9][0-9]*|channel:[1-9][0-9]*:user:[1-9][0-9]*)$"
)
_IMAGE_PREFIXES = {
    "data:image/jpeg;base64,": "image/jpeg",
    "data:image/png;base64,": "image/png",
    "data:image/webp;base64,": "image/webp",
}
_MAX_ENCODED_IMAGE_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4


@dataclass(frozen=True, slots=True)
class ChatPayload:
    conversation_key: str
    text: str
    images: tuple[str, ...]
    budget_ms: int = 120000
    parent_channel_id: int | None = None


def validate_chat_payload(value: object) -> ChatPayload:
    required = {"conversation_key", "text", "images"}
    optional = {"budget_ms", "parent_channel_id"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - optional
    ):
        raise CodexBridgeError("invalid_request")

    key = value["conversation_key"]
    text = value["text"]
    images = value["images"]
    budget_ms = value.get("budget_ms", 120000)
    parent_channel_id = value.get("parent_channel_id")
    if not valid_conversation_key(key):
        raise CodexBridgeError("invalid_request")
    if type(budget_ms) is not int or not 1 <= budget_ms <= 120000:
        raise CodexBridgeError("invalid_request")
    is_thread = isinstance(key, str) and ":thread:" in key
    if is_thread:
        if parent_channel_id is not None and (
            type(parent_channel_id) is not int or parent_channel_id <= 0
        ):
            raise CodexBridgeError("invalid_request")
        if parent_channel_id == int(key.rsplit(":", 1)[1]):
            raise CodexBridgeError("invalid_request")
    elif parent_channel_id is not None:
        raise CodexBridgeError("invalid_request")
    if not isinstance(text, str) or len(text) > MAX_PROMPT_CHARACTERS:
        raise CodexBridgeError("invalid_request")
    if not isinstance(images, list) or len(images) > MAX_IMAGE_ATTACHMENTS:
        raise CodexBridgeError("invalid_request")

    total_image_bytes = 0
    for image in images:
        if not isinstance(image, str):
            raise CodexBridgeError("invalid_request")
        match = next(
            (
                (prefix, content_type)
                for prefix, content_type in _IMAGE_PREFIXES.items()
                if image.startswith(prefix)
            ),
            None,
        )
        if match is None:
            raise CodexBridgeError("invalid_request")
        prefix, content_type = match
        encoded = image[len(prefix) :]
        if not encoded or len(encoded) > _MAX_ENCODED_IMAGE_CHARS:
            raise CodexBridgeError("invalid_request")
        try:
            data = base64.b64decode(encoded, validate=True)
        except binascii.Error, ValueError:
            raise CodexBridgeError("invalid_request") from None
        total_image_bytes += len(data)
        try:
            validate_image_bytes(content_type, data, total_image_bytes)
        except ImageAttachmentError:
            raise CodexBridgeError("invalid_request") from None
    if not text.strip() and not images:
        raise CodexBridgeError("invalid_request")
    return ChatPayload(key, text, tuple(images), budget_ms, parent_channel_id)


@dataclass(frozen=True, slots=True)
class ArchivePayload:
    guild_id: int
    channel_id: int | None = None
    include_children: bool = False


def validate_archive_payload(value: object) -> ArchivePayload:
    if not isinstance(value, dict) or set(value) - {"guild_id", "channel_id", "include_children"}:
        raise CodexBridgeError("invalid_request")
    guild_id = value.get("guild_id")
    channel_id = value.get("channel_id")
    include_children = value.get("include_children", False)
    if (
        type(guild_id) is not int
        or guild_id <= 0
        or (channel_id is not None and (type(channel_id) is not int or channel_id <= 0))
        or type(include_children) is not bool
        or (include_children and channel_id is None)
    ):
        raise CodexBridgeError("invalid_request")
    return ArchivePayload(guild_id, channel_id, include_children)


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
