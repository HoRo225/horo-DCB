from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.codex.model_settings import ModelSettings, parse_model_settings
from src.codex.contracts import IntentDecision, parse_intent_decision
from src.config import is_positive_int

MAX_IMAGE_ATTACHMENTS = 12
MAX_PROMPT_CHARACTERS = 4000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 50 * 1024 * 1024
MAX_REPLY_IMAGES = 10
PROTOCOL_VERSION = 4
MAX_STREAM_FRAME_BYTES = 1024 * 1024
MAX_PROGRESS_SUMMARY_CHARACTERS = 2000
MAX_CHAT_BUDGET_MS = 300_000
WORK_TIMEOUT_SECONDS = MAX_CHAT_BUDGET_MS / 1000 + 30
CLEANUP_SECONDS = 5.0
PROGRESS_STAGES = frozenset({"queued", "generating", "searching", "fallback"})


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
    "stalled": 503,
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
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def wire_error(code: object) -> CodexBridgeError:
    known = isinstance(code, str) and code in SAFE_ERROR_CODES
    return CodexBridgeError(code if known else "unavailable")


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


@dataclass(frozen=True, slots=True)
class CodexRuntimeStatus:
    available: bool
    authenticated: bool
    plan: str | None
    sdk_version: str | None
    runtime_version: str | None
    thread_count: int
    active_requests: int = 0
    queued_requests: int = 0
    last_error: str | None = None
    ready: bool = False
    reason: str = "unavailable"
    intent_available: bool = False
    intent_reason: str = "unavailable"


READY_REASONS = frozenset(
    {
        "ready",
        "initializing",
        "auth_required",
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
        or raw["archived_count"] + raw["archive_unconfirmed_count"] > raw["detached_count"]
    ):
        raise ValueError("invalid archive result")
    return CodexArchiveResult(**raw)


EMPTY_CODEX_RUNTIME_STATUS = CodexRuntimeStatus(False, False, None, None, None, 0)


def parse_runtime_status(body: dict[str, object]) -> CodexRuntimeStatus:
    try:
        version = body.get("protocol_version")
        if type(version) is not int or version != PROTOCOL_VERSION:
            raise ValueError("invalid protocol version")
        if any(type(body[field]) is not bool for field in ("available", "authenticated")):
            raise ValueError("invalid status flag")
        if any(
            type(body[field]) is not int or body[field] < 0
            for field in ("thread_count", "active_requests", "queued_requests")
        ):
            raise ValueError("invalid status counts")
        if (
            type(body["ready"]) is not bool
            or body["reason"] not in READY_REASONS
            or body["ready"] != (body["reason"] == "ready")
            or (body["ready"] and (not body["available"] or not body["authenticated"]))
        ):
            raise ValueError("invalid readiness status")
        last_error = body.get("last_error")
        intent_available = body.get("intent_available", False)
        intent_reason = body.get("intent_reason", "unavailable")
        if (type(intent_available) is not bool or not isinstance(intent_reason, str)
            or intent_reason not in READY_REASONS | {"sdk_callback_observer_unavailable"}
            or (intent_available and (intent_reason != "ready" or not body["ready"]))):
            raise ValueError("invalid intent readiness")

        def optional_text(field: str) -> str | None:
            return body[field] if isinstance(body[field], str) else None

        return CodexRuntimeStatus(
            available=body["available"],
            authenticated=body["authenticated"],
            plan=optional_text("plan"),
            sdk_version=optional_text("sdk_version"),
            runtime_version=optional_text("runtime_version"),
            thread_count=body["thread_count"],
            active_requests=body["active_requests"],
            queued_requests=body["queued_requests"],
            last_error=(
                last_error
                if isinstance(last_error, str) and last_error in SAFE_ERROR_CODES
                else None
            ),
            ready=body["ready"],
            reason=body["reason"],
            intent_available=intent_available,
            intent_reason=intent_reason,
        )
    except (KeyError, TypeError) as exc:
        raise ValueError("invalid runtime status") from exc


def truncate_progress_summary(summary: str) -> str:
    if len(summary) <= MAX_PROGRESS_SUMMARY_CHARACTERS:
        return summary
    return "…" + summary[-(MAX_PROGRESS_SUMMARY_CHARACTERS - 1) :]


def progress_frame(stage: str, summary: str) -> dict[str, object]:
    return {
        "type": "progress",
        "stage": stage,
        "summary": truncate_progress_summary(summary),
    }


def error_frame(code: str) -> dict[str, object]:
    return {"type": "error", "error": code}


def completed_frame(reply: CodexChatReply | IntentDecision) -> dict[str, object]:
    if isinstance(reply, IntentDecision):
        return {"type": "intent", "decision": reply.to_payload()}
    return {
        "type": "completed",
        "reply": reply.text,
        "image_urls": list(reply.image_urls),
        "restarted": reply.restarted,
    }


def parse_stream_frame(event: object) -> tuple[str, str] | CodexChatReply | IntentDecision:
    if not isinstance(event, dict):
        raise CodexBridgeError("unavailable")
    event_type = event.get("type")
    if event_type == "intent":
        try:
            if set(event) != {"type", "decision"}:
                raise ValueError("invalid intent frame")
            return parse_intent_decision(event["decision"])
        except (ValueError, TypeError):
            raise CodexBridgeError("unavailable") from None
    if event_type == "progress":
        stage, summary = event.get("stage"), event.get("summary")
        if (
            not isinstance(stage, str)
            or stage not in PROGRESS_STAGES
            or not isinstance(summary, str)
            or len(summary) > MAX_PROGRESS_SUMMARY_CHARACTERS
        ):
            raise CodexBridgeError("unavailable")
        return stage, summary
    if event_type == "error":
        raise wire_error(event.get("error"))
    if event_type == "completed":
        reply = event.get("reply")
        restarted = event.get("restarted", False)
        if not isinstance(reply, str) or not reply or type(restarted) is not bool:
            raise CodexBridgeError("unavailable")
        return CodexChatReply(
            reply, normalize_reply_image_urls(event.get("image_urls", ())), restarted
        )
    raise CodexBridgeError("unavailable")


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
    models: ModelSettings
    budget_ms: int = MAX_CHAT_BUDGET_MS
    parent_channel_id: int | None = None
    mode: str = "chat"
    context: dict[str, object] | None = None
    results: tuple[dict[str, object], ...] = ()
    request_id: str = "-"


def validate_chat_payload(value: object) -> ChatPayload:
    required = {"conversation_key", "text", "images", "models"}
    optional = {"budget_ms", "parent_channel_id", "mode", "context", "results", "request_id", "protocol_version"}
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - optional
    ):
        raise CodexBridgeError("invalid_request")

    key = value["conversation_key"]
    text = value["text"]
    images = value["images"]
    try:
        models = parse_model_settings(value["models"])
    except ValueError, TypeError:
        raise CodexBridgeError("invalid_request") from None
    budget_ms = value.get("budget_ms", MAX_CHAT_BUDGET_MS)
    parent_channel_id = value.get("parent_channel_id")
    mode = value.get("mode", "chat")
    context = value.get("context")
    results = value.get("results", [])
    request_id = value.get("request_id", "-")
    if value.get("protocol_version", PROTOCOL_VERSION) != PROTOCOL_VERSION or type(value.get("protocol_version", PROTOCOL_VERSION)) is not int:
        raise CodexBridgeError("invalid_request")
    if mode not in ("chat", "intent", "summary", "once") or not isinstance(request_id, str) or len(request_id) > 64:
        raise CodexBridgeError("invalid_request")
    if context is not None and (mode != "intent" or not isinstance(context, dict)):
        raise CodexBridgeError("invalid_request")
    if isinstance(context, dict) and set(context) & {"history", "references", "quoted_text", "images", "media", "web_content"}:
        raise CodexBridgeError("invalid_request")
    if not isinstance(results, list) or len(results) > 4 or any(not isinstance(item, dict) for item in results) or (results and mode != "summary"):
        raise CodexBridgeError("invalid_request")
    try:
        if len(json.dumps(context, ensure_ascii=False, allow_nan=False)) > 32_000 or len(json.dumps(results, ensure_ascii=False, allow_nan=False)) > 32_000:
            raise ValueError("oversized context")
    except (TypeError, ValueError):
        raise CodexBridgeError("invalid_request") from None
    if mode != "chat" and images:
        raise CodexBridgeError("invalid_request")
    if mode == "intent" and context is None:
        raise CodexBridgeError("invalid_request")
    if not valid_conversation_key(key):
        raise CodexBridgeError("invalid_request")
    if type(budget_ms) is not int or not 1 <= budget_ms <= MAX_CHAT_BUDGET_MS:
        raise CodexBridgeError("invalid_request")
    if not valid_parent_channel(key, parent_channel_id):
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
        if (
            len(data) > MAX_IMAGE_BYTES
            or total_image_bytes > MAX_IMAGE_TOTAL_BYTES
            or not image_signature_matches(content_type, data)
        ):
            raise CodexBridgeError("invalid_request")
    if not text.strip() and not images and not (mode == "summary" and results):
        raise CodexBridgeError("invalid_request")
    return ChatPayload(key, text, tuple(images), models, budget_ms, parent_channel_id, mode, context, tuple(results), request_id)


@dataclass(frozen=True, slots=True)
class ArchivePayload:
    guild_id: int | None = None
    channel_id: int | None = None
    include_children: bool = False
    conversation_key: str | None = None


def validate_archive_payload(value: object) -> ArchivePayload:
    if isinstance(value, dict) and set(value) == {"conversation_key"}:
        if not valid_conversation_key(value["conversation_key"]):
            raise CodexBridgeError("invalid_request")
        return ArchivePayload(conversation_key=value["conversation_key"])
    if not isinstance(value, dict) or set(value) - {"guild_id", "channel_id", "include_children"}:
        raise CodexBridgeError("invalid_request")
    guild_id = value.get("guild_id")
    channel_id = value.get("channel_id")
    include_children = value.get("include_children", False)
    if (
        not is_positive_int(guild_id)
        or (channel_id is not None and not is_positive_int(channel_id))
        or type(include_children) is not bool
        or (include_children and channel_id is None)
    ):
        raise CodexBridgeError("invalid_request")
    return ArchivePayload(guild_id, channel_id, include_children)


def valid_conversation_key(key: object) -> bool:
    return isinstance(key, str) and _THREAD_KEY.fullmatch(key) is not None


def valid_parent_channel(key: str, parent_channel_id: object) -> bool:
    return parent_channel_id is None or (
        is_positive_int(parent_channel_id)
        and ":thread:" in key
        and parent_channel_id != int(key.rsplit(":", 1)[1])
    )

