from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import math
import re
from urllib.parse import urlsplit


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


EMPTY_CODEX_RUNTIME_STATUS = CodexRuntimeStatus(False, False, None, None, None, None, 0)


RATE_LIMIT_ERRORS = frozenset({
    "unavailable", "timeout", "auth_required", "invalid_response",
})


@dataclass(frozen=True, slots=True)
class CodexRateWindow:
    slot: str
    used_percent: int | float
    window_minutes: int | None
    resets_at: int | None


@dataclass(frozen=True, slots=True)
class CodexRateLimits:
    fetched_at: int | None = None
    windows: tuple[CodexRateWindow, ...] = ()
    error: str | None = "unavailable"


def _rate_optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 < value <= 253402300799:
        raise ValueError("invalid rate-limit integer")
    return value


def _rate_window(slot: str, value: object, *, upstream: bool) -> CodexRateWindow:
    if not isinstance(value, dict) or slot not in ("primary", "secondary"):
        raise ValueError("invalid rate-limit window")
    used = value.get("usedPercent" if upstream else "used_percent")
    if type(used) not in (int, float) or not 0 <= used <= 100:
        raise ValueError("invalid rate-limit percentage")
    if isinstance(used, float) and not math.isfinite(used):
        raise ValueError("invalid rate-limit percentage")
    minutes = _rate_optional_int(value.get(
        "windowDurationMins" if upstream else "window_minutes"
    ))
    reset = _rate_optional_int(value.get("resetsAt" if upstream else "resets_at"))
    return CodexRateWindow(slot, used, minutes, reset)


def normalize_rate_limits(raw: object, *, fetched_at: int) -> CodexRateLimits:
    if not isinstance(raw, dict):
        raise ValueError("invalid rate-limit response")
    timestamp = _rate_optional_int(fetched_at)
    if timestamp is None:
        raise ValueError("missing rate-limit timestamp")
    buckets = raw.get("rateLimitsByLimitId")
    if buckets is not None and not isinstance(buckets, dict):
        raise ValueError("invalid rate-limit buckets")
    if isinstance(buckets, dict) and "codex" in buckets:
        snapshot = buckets["codex"]
        if not isinstance(snapshot, dict) or snapshot.get("limitId") not in (None, "codex"):
            raise ValueError("invalid codex rate-limit bucket")
    else:
        snapshot = raw.get("rateLimits")
        if not isinstance(snapshot, dict):
            raise ValueError("missing rate-limit snapshot")
        if snapshot.get("limitId") not in (None, "codex"):
            return CodexRateLimits(timestamp, (), None)
    windows = tuple(
        _rate_window(slot, snapshot[slot], upstream=True)
        for slot in ("primary", "secondary")
        if snapshot.get(slot) is not None
    )
    return CodexRateLimits(timestamp, windows, None)


def parse_rate_limits_payload(raw: object) -> CodexRateLimits:
    if not isinstance(raw, dict) or set(raw) != {"fetched_at", "windows", "error"}:
        raise ValueError("invalid rate-limit payload")
    error = raw.get("error")
    windows = raw.get("windows")
    if not isinstance(windows, list) or len(windows) > 2:
        raise ValueError("invalid rate-limit windows")
    if error is not None:
        if not isinstance(error, str) or error not in RATE_LIMIT_ERRORS:
            raise ValueError("invalid rate-limit error")
        if windows or raw.get("fetched_at") is not None:
            raise ValueError("error payload contains current values")
        return CodexRateLimits(error=error)
    timestamp = _rate_optional_int(raw.get("fetched_at"))
    if timestamp is None:
        raise ValueError("missing rate-limit timestamp")
    result = tuple(
        _rate_window(
            value.get("slot") if isinstance(value, dict) else "",
            value,
            upstream=False,
        )
        for value in windows
    )
    if len({window.slot for window in result}) != len(result):
        raise ValueError("duplicate rate-limit slot")
    return CodexRateLimits(timestamp, result, None)


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


def validate_chat_payload(value: object) -> ChatPayload:
    if not isinstance(value, dict) or set(value) != {
        "conversation_key",
        "text",
        "images",
    }:
        raise CodexBridgeError("invalid_request")

    key = value["conversation_key"]
    text = value["text"]
    images = value["images"]
    if not valid_conversation_key(key):
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
        except (binascii.Error, ValueError):
            raise CodexBridgeError("invalid_request") from None
        total_image_bytes += len(data)
        try:
            validate_image_bytes(content_type, data, total_image_bytes)
        except ImageAttachmentError:
            raise CodexBridgeError("invalid_request") from None
    if not text.strip() and not images:
        raise CodexBridgeError("invalid_request")
    return ChatPayload(key, text, tuple(images))


def validate_archive_payload(value: object) -> tuple[int, int | None]:
    if not isinstance(value, dict) or set(value) - {"guild_id", "channel_id"}:
        raise CodexBridgeError("invalid_request")
    guild_id = value.get("guild_id")
    channel_id = value.get("channel_id")
    if type(guild_id) is not int or guild_id <= 0 or (
        channel_id is not None and (type(channel_id) is not int or channel_id <= 0)
    ):
        raise CodexBridgeError("invalid_request")
    return guild_id, channel_id


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
