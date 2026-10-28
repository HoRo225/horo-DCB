from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

MAX_IMAGE_ATTACHMENTS = 12
MAX_PROMPT_CHARACTERS = 4000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 50 * 1024 * 1024
MAX_REPLY_IMAGES = 10
MAX_PROGRESS_SUMMARY_CHARACTERS = 2000
MAX_CHAT_BUDGET_MS = 300_000
WORK_TIMEOUT_SECONDS = MAX_CHAT_BUDGET_MS / 1000 + 30
CLEANUP_SECONDS = 5.0


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


class CodexError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CodexChatReply:
    text: str
    image_urls: tuple[str, ...] = ()
    restarted: bool = False


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


type StatusReason = Literal["ready", "connecting", "auth_required", "unresponsive", "unavailable", "draining"]


@dataclass(frozen=True, slots=True)
class CodexRuntimeStatus:
    reason: StatusReason = "unavailable"
    authenticated: bool = False
    plan: str | None = None
    runtime_version: str | None = None
    thread_count: int = 0
    last_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.reason == "ready"


@dataclass(frozen=True, slots=True)
class CodexArchiveResult:
    detached_count: int
    archived_count: int
    archive_unconfirmed_count: int


def truncate_progress_summary(summary: str) -> str:
    if len(summary) <= MAX_PROGRESS_SUMMARY_CHARACTERS:
        return summary
    return "…" + summary[-(MAX_PROGRESS_SUMMARY_CHARACTERS - 1) :]
