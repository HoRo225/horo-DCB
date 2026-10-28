from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from src.ai import ModelError, normalize_reply_image_urls
from src.integrations.codex.rpc import RpcError, ThreadEvents

_IMAGE_RESULT_REF = re.compile(r"^turn[0-9]+image[0-9]+$")
_MARKDOWN_IMAGE_URL = re.compile(r"!\[[^\]]*\]\((https://[^\s)]+)\)")
_CITATION = re.compile("[^]*")
ISOLATED_ITEMS = frozenset({"userMessage", "agentMessage", "reasoning"})
TERMINAL = frozenset({"completed", "failed", "interrupted"})
MAX_PROGRESS_SUMMARY_CHARACTERS = 2000


def truncate_progress_summary(summary: str) -> str:
    if len(summary) <= MAX_PROGRESS_SUMMARY_CHARACTERS:
        return summary
    return "…" + summary[-(MAX_PROGRESS_SUMMARY_CHARACTERS - 1) :]


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


def extract_reply_image_urls(web_searches: list[dict[str, Any]], reply: str) -> tuple[str, ...]:
    urls = _MARKDOWN_IMAGE_URL.findall(reply)
    for item in web_searches:
        results = item.get("results")
        if isinstance(results, list):
            urls.extend(url for result in results if (url := _result_image_url(result)) is not None)
    return normalize_reply_image_urls(urls)


@dataclass
class TurnResult:
    status: Literal["completed", "failed", "interrupted"] | None = None
    output_seen: bool = False
    untrusted: str | None = None
    error: object = None
    text: str = ""
    final_answer: bool = False
    web_searches: list[dict[str, Any]] = field(default_factory=list)


def normalize_error(error: object) -> ModelError:
    if isinstance(error, ModelError):
        return error
    info = None
    if isinstance(error, RpcError):
        if error.overloaded:
            return ModelError("model_capacity")
        details = f"{error.message} {error.data}"
    elif isinstance(error, dict):
        info = error.get("codexErrorInfo")
        details = f"{error.get('message')} {error.get('additionalDetails')}"
    else:
        details = str(error)
    if info is not None:
        name = (info if isinstance(info, str) else json.dumps(info)).replace("_", "").casefold()
        if "serveroverloaded" in name:
            return ModelError("model_capacity")
        if any(marker in name for marker in ("usagelimitexceeded", "ratelimitexceeded")):
            return ModelError("usage_limit_or_unavailable")
        if "modelnotfound" in name:
            return ModelError("model_configuration_invalid")
        if "unauthorized" in name or re.search(r"statuscode\W+401\b", name):
            return ModelError("auth_required")
        return ModelError("unavailable")
    details = details.casefold()
    if any(marker in details for marker in (
        "authentication", "chatgpt login", "invalid_grant", "login required", "not logged in", "refresh token", "unauthorized",
    )):
        return ModelError("auth_required")
    if any(marker in details for marker in ("at capacity", "server overloaded", "server_overloaded", "serveroverloaded")):
        return ModelError("model_capacity")
    if any(marker in details for marker in (
        "credits", "quota", "rate limit", "rate_limit", "too many requests", "usage limit", "usage_limit", "usagelimit",
    )):
        return ModelError("usage_limit_or_unavailable")
    return ModelError("unavailable")


def _record_message(result: TurnResult, item: dict[str, Any]) -> None:
    text = item.get("text")
    if not isinstance(text, str) or not text:
        return
    result.output_seen = True
    phase = item.get("phase")
    final = phase == "final_answer"
    if final or (phase != "commentary" and not result.final_answer):
        result.text = _CITATION.sub("", text)
        result.final_answer = final


async def collect_turn(
    events: ThreadEvents,
    turn_id: str,
    on_progress: Callable[[str, str], None] | None = None,
    upstream: asyncio.Event | None = None,
    *,
    isolated: bool = False,
) -> TurnResult:
    result = TurnResult()
    summary_parts: dict[tuple[object, object], str] = {}

    def progress(stage: str, text: str = "") -> None:
        if on_progress is not None:
            on_progress(stage, truncate_progress_summary(text))

    def distrust(reason: str) -> None:
        result.untrusted = result.untrusted or reason[:100]

    while True:
        event = await events.next()
        params = event.params
        if event.method == "turn/completed":
            turn = params.get("turn")
            if not isinstance(turn, dict) or turn.get("id") != turn_id:
                continue
            status = turn.get("status")
            if status not in TERMINAL:
                distrust(f"turn status {status}")
                continue
            result.status = status
            result.error = turn.get("error") or result.error
            return result
        if params.get("turnId", turn_id) != turn_id:
            continue
        item = params.get("item")
        if upstream is not None and (
            event.method == "error"
            or event.method.startswith(("item/agentMessage/", "item/reasoning/"))
            or (isinstance(item, dict) and item.get("type") != "userMessage")
        ):
            upstream.set()
        if event.server_request:
            distrust(f"request {event.method}")
            continue
        if event.method == "item/agentMessage/delta":
            if params.get("delta"):
                result.output_seen = True
        elif event.method == "error":
            result.error = params.get("error")
        elif event.method == "item/reasoning/summaryTextDelta":
            key = (params.get("itemId"), params.get("summaryIndex"))
            part = truncate_progress_summary(summary_parts.get(key, "") + str(params.get("delta") or ""))
            summary_parts[key] = part
            progress("generating", part)
        elif event.method in ("item/started", "item/completed"):
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            completed = event.method == "item/completed"
            if isolated and kind not in ISOLATED_ITEMS:
                distrust(f"item {kind}")
            if kind == "agentMessage":
                if completed:
                    _record_message(result, item)
                progress("generating")
            elif kind == "reasoning" and completed:
                parts = [part for part in item.get("summary") or () if isinstance(part, str) and part.strip()]
                if parts:
                    progress("generating", parts[-1])
            elif kind == "webSearch":
                progress("generating" if completed else "searching")
                if completed:
                    result.web_searches.append(item)
