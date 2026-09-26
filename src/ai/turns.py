from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from openai_codex import RetryLimitExceededError, ServerBusyError

from src.ai.protocol import CodexBridgeError, normalize_reply_image_urls

_IMAGE_RESULT_REF = re.compile(r"^turn[0-9]+image[0-9]+$")
_MARKDOWN_IMAGE_URL = re.compile(r"!\[[^\]]*\]\((https://[^\s)]+)\)")


def _result_image_url(result: object) -> str | None:
    if not isinstance(result, dict):
        return None

    payload = result
    nested = result.get("image_result")
    if isinstance(nested, dict):
        payload = nested
    elif result.get("type") != "image_result":
        ref_id = result.get("ref_id")
        if not isinstance(ref_id, str) or _IMAGE_RESULT_REF.fullmatch(ref_id) is None:
            return None

    for name in ("image_url", "url"):
        url = payload.get(name)
        if isinstance(url, str):
            return url
    return None


def _extract_reply_image_urls(
    items: object,
    reply: str,
) -> tuple[str, ...]:
    if not isinstance(items, (list, tuple)):
        return ()

    urls = _MARKDOWN_IMAGE_URL.findall(reply)
    for item in items:
        raw = getattr(item, "root", item)
        if getattr(raw, "type", None) != "webSearch":
            continue
        results = getattr(raw, "results", None)
        if not isinstance(results, list):
            continue
        for result in results:
            url = _result_image_url(result)
            if url is not None:
                urls.append(url)
    return normalize_reply_image_urls(urls)


@dataclass
class TurnResult:
    terminal: bool = False
    completed: bool = False
    failed: bool = False
    output_seen: bool = False
    unknown: bool = False
    error: object = None
    text: str = ""
    final_answer: bool = False
    items: list[Any] = field(default_factory=list)


def normalize_error(exc: Exception) -> CodexBridgeError:
    details = f"{exc} {getattr(exc, 'data', '')}".casefold()
    if any(
        marker in details
        for marker in (
            "authentication",
            "chatgpt login",
            "invalid_grant",
            "login required",
            "not logged in",
            "refresh token",
            "unauthorized",
        )
    ):
        return CodexBridgeError("auth_required")
    if isinstance(exc, (ServerBusyError, RetryLimitExceededError)) or any(
        marker in details
        for marker in (
            "at capacity",
            "server overloaded",
            "server_overloaded",
            "serveroverloaded",
        )
    ):
        return CodexBridgeError("model_capacity")
    if any(
        marker in details
        for marker in (
            "credits",
            "quota",
            "rate limit",
            "rate_limit",
            "too many requests",
            "usage limit",
            "usage_limit",
            "usagelimit",
        )
    ):
        return CodexBridgeError("usage_limit_or_unavailable")
    return CodexBridgeError("unavailable")


def record_item(result: TurnResult, item: Any, *, completed: bool) -> None:
    raw = getattr(item, "root", item)
    if getattr(raw, "type", None) == "agentMessage":
        text = getattr(raw, "text", "")
        if text:
            result.output_seen = True
        phase = getattr(getattr(raw, "phase", None), "value", getattr(raw, "phase", None))
        if (
            completed
            and text
            and (phase == "final_answer" or (phase is None and not result.final_answer))
        ):
            result.text = text
            result.final_answer = phase == "final_answer"
    if completed:
        result.items.append(item)


async def collect_turn(handle: Any) -> TurnResult:
    result = TurnResult()
    stream = handle.stream()
    try:
        async for event in stream:
            payload = event.payload
            method = event.method
            if getattr(payload, "thread_id", handle.thread_id) != handle.thread_id:
                continue
            turn_id = getattr(payload, "turn_id", None)
            if method == "turn/completed":
                turn = payload.turn
                if turn.id != handle.id:
                    continue
                status = getattr(turn.status, "value", turn.status)
                if status not in ("completed", "failed", "interrupted"):
                    result.unknown = True
                    continue
                result.terminal = True
                result.completed = status == "completed"
                result.failed = status == "failed"
                result.error = turn.error
                for item in getattr(turn, "items", ()):
                    record_item(result, item, completed=True)
                break
            if turn_id is not None and turn_id != handle.id:
                continue
            if method == "item/agentMessage/delta":
                if payload.delta:
                    result.output_seen = True
            elif method in ("item/started", "item/completed"):
                record_item(result, payload.item, completed=method == "item/completed")
            elif method not in (
                "turn/started",
                "item/reasoning/textDelta",
                "item/reasoning/summaryTextDelta",
                "item/reasoning/summaryPartAdded",
                "thread/tokenUsage/updated",
            ):
                # Unknown notifications cannot establish absence of assistant output.
                result.unknown = True
    finally:
        await stream.aclose()
    return result
