from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel
from openai_codex.models import UnknownNotification
from openai_codex.generated.v2_all import ThreadUnsubscribeResponse

from src.codex.model_settings import ModelInfo
from src.codex.contracts import ACTION_PARAMETERS, GAMES, MAX_ACTIONS
from src.codex.rate_limits import CodexRateLimits, normalize_rate_limits


COMMON_DISABLED_FEATURES = dict.fromkeys((
    "apps", "browser_use", "computer_use", "goals", "hooks", "image_generation",
    "memories", "multi_agent", "plugins", "remote_plugin", "shell_snapshot",
    "shell_tool", "skill_search", "sleep_tool", "tool_suggest", "unified_exec",
), False)
_ISOLATED_NOTIFICATIONS = frozenset({
    "thread/started", "thread/closed", "thread/status/changed", "thread/name/updated", "thread/settings/updated", "thread/tokenUsage/updated",
    "account/updated", "account/rateLimits/updated", "serverRequest/resolved",
    "warning", "configWarning", "remoteControl/status/changed",
    "turn/started", "turn/completed", "turn/moderationMetadata", "error", "item/started", "item/completed",
    "item/agentMessage/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta",
    "item/reasoning/summaryPartAdded",
})


@dataclass(eq=False, slots=True)
class CallbackTicket:
    thread_id: str | None = None
    violated: bool = False
    reason: str | None = None


class CallbackGuard:
    def __init__(self, codex: Any) -> None:
        self._lock = threading.Lock()
        self._tickets: set[CallbackTicket] = set()
        client = getattr(getattr(codex, "_client", None), "_sync", None)
        original = getattr(client, "_approval_handler", None)
        coerce = getattr(client, "_coerce_notification", None)
        self.available = callable(original) and callable(coerce)
        if not self.available:
            return

        def observe(method: str, params: dict[str, object] | None) -> dict[str, object]:
            self._mark_violation(params, method)
            if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                return {"decision": "decline"}
            return {}

        def observe_notification(method: str, params: object) -> object:
            notification = coerce(method, params)
            if method not in _ISOLATED_NOTIFICATIONS or isinstance(getattr(notification, "payload", None), UnknownNotification):
                self._mark_violation(params, method)
            elif method in ("item/started", "item/completed"):
                item = getattr(notification.payload.item, "root", notification.payload.item)
                if getattr(item, "type", None) not in ("userMessage", "agentMessage", "reasoning"):
                    self._mark_violation(params, method)
            return notification

        try:
            client._approval_handler = observe
            client._coerce_notification = observe_notification
        except (AttributeError, TypeError):
            self.available = False

    def _mark_violation(self, params: object, method: str) -> None:
        thread_id = params.get("threadId", params.get("thread_id")) if isinstance(params, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            thread_id = None
        with self._lock:
            for ticket in self._tickets:
                if thread_id is None or ticket.thread_id == thread_id:
                    ticket.violated = True
                    if ticket.reason is None:
                        ticket.reason = method[:100]

    def begin(self) -> CallbackTicket:
        if not self.available:
            raise RuntimeError("SDK callback observer unavailable")
        ticket = CallbackTicket()
        with self._lock:
            self._tickets.add(ticket)
        return ticket

    def end(self, ticket: CallbackTicket) -> None:
        with self._lock:
            self._tickets.discard(ticket)


def feature_config(*, isolated: bool = False) -> dict[str, object]:
    return {
        "web_search": "disabled" if isolated else "live",
        "features": {
            **COMMON_DISABLED_FEATURES,
            "standalone_web_search": not isolated,
            "view_image": not isolated,
            "code_mode_host": not isolated,
        },
    }


class _IntentEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, title="IntentEnvelope")
    kind: str
    actions: list[dict[str, object]]
    question: str


def intent_schema() -> dict[str, object]:
    schema = _IntentEnvelope.model_json_schema()
    schema["properties"]["kind"] = {"type": "string", "enum": ["chat", "plan", "clarification"]}
    choices = []
    primitive = {
        "id": {"type": "integer", "minimum": 1},
        "bool": {"type": "boolean"}, "text": {"type": "string"},
        "ids": {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": 25},
        "game": {"type": "string", "enum": list(GAMES)},
        "resolution": {"type": "string", "enum": ["confirmed", "retry", "skip"]},
    }
    model_choice = {"type": "object", "properties": {"model": {"type": "string"}, "effort": {"type": ["string", "null"]}}, "required": ["model", "effort"], "additionalProperties": False}
    primitive["choice"] = model_choice
    primitive["nullable_choice"] = {"anyOf": [model_choice, {"type": "null"}]}
    for name, params in ACTION_PARAMETERS.items():
        properties = {key: primitive[kind] if required else {"anyOf": [primitive[kind], {"type": "null"}]} for key, (kind, required) in params.items()}
        choices.append({
            "type": "object", "properties": {
                "name": {"type": "string", "enum": [name]},
                "params": {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False},
            }, "required": ["name", "params"], "additionalProperties": False,
        })
    schema["properties"]["actions"] = {"type": "array", "items": {"anyOf": choices}, "maxItems": MAX_ACTIONS}
    return schema


async def request[T](codex: Any, method: str, response_model: type[T], *, params: dict[str, object] | None = None) -> T:
    return await codex._client.request(method, params, response_model=response_model)


async def unsubscribe_thread(codex: Any, thread_id: str) -> str:
    response = await request(codex, "thread/unsubscribe", ThreadUnsubscribeResponse, params={"threadId": thread_id})
    return str(getattr(response.status, "value", response.status))


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


async def read_models(codex: Any) -> dict[str, object]:
    response = await codex.models(include_hidden=False)
    return {
        "models": [
            ModelInfo(
                model=item.model,
                display_name=item.display_name,
                default_effort=_value(item.default_reasoning_effort),
                supported_efforts=tuple(
                    _value(option.reasoning_effort) for option in item.supported_reasoning_efforts
                ),
                input_modalities=tuple(_value(value) for value in (item.input_modalities or ())),
            ).to_payload()
            for item in response.data
        ],
        "fetched_at": int(time.time()),
    }


async def read_rate_limits(codex: Any) -> CodexRateLimits:
    response = await request(codex, "account/rateLimits/read", RootModel[dict[str, Any]])
    return normalize_rate_limits(response.root, fetched_at=int(time.time()))
