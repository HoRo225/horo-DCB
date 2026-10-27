from __future__ import annotations

import json
from dataclasses import dataclass

from src.codex.model_settings import parse_model_settings

MAX_ACTIONS = 4
GAME_ACTIONS = frozenset({"open_table", "deal", "again", "hit", "stand", "leave_table"})
GAMES = ("blackjack", "slots")

ACTION_PARAMETERS: dict[str, dict[str, tuple[str, bool]]] = {
    name: {} for name in (
        "deal", "again", "hit", "stand", "leave_table",
        "voice_sync", "admin_status", "publish_hub", "steam_query", "help",
    )
}
ACTION_PARAMETERS.update({
    "open_table": {"game": ("game", True)},
    **{name: {"enabled": ("bool", True)}
       for name in ("ai_enabled", "ai_display", "voice_enabled", "steam_enabled")},
    **{name: {"ids": ("ids", True)}
       for name in ("ai_channels", "ai_roles", "games_roles", "steam_roles")},
    **{name: {"channel_id": ("id", True)}
       for name in ("games_channel", "voice_entry", "steam_channel")},
    "ai_models": {"primary": ("choice", True), "fallback": ("nullable_choice", True)},
    "pending_resolution": {"effect_id": ("text", True), "resolution": ("resolution", True), "message_id": ("id", False)},
})
ACTION_NAMES = frozenset(ACTION_PARAMETERS)


@dataclass(frozen=True, slots=True)
class Action:
    name: str
    params: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return {"name": self.name, "params": self.params.copy()}


@dataclass(frozen=True, slots=True)
class IntentDecision:
    kind: str
    actions: tuple[Action, ...] = ()
    question: str = ""

    def to_payload(self) -> dict[str, object]:
        return {"kind": self.kind, "actions": [action.to_payload() for action in self.actions], "question": self.question}


def _valid_parameter(kind: str, value: object) -> bool:
    if kind == "id":
        return type(value) is int and 0 < value <= 2**63 - 1
    if kind == "bool":
        return type(value) is bool
    if kind == "ids":
        return isinstance(value, list) and len(value) <= 25 and all(_valid_parameter("id", item) for item in value) and len(set(value)) == len(value)
    if kind == "text":
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 500
    if kind == "game":
        return value in GAMES
    if kind == "resolution":
        return value in ("confirmed", "retry", "skip")
    if kind in {"choice", "nullable_choice"}:
        if value is None:
            return kind == "nullable_choice"
        try:
            parse_model_settings({"primary": value, "fallback": None})
            return True
        except (TypeError, ValueError):
            return False
    return False


def parse_action(raw: object) -> Action:
    if not isinstance(raw, dict) or set(raw) != {"name", "params"}:
        raise ValueError("invalid action")
    name, params = raw["name"], raw["params"]
    if not isinstance(name, str) or name not in ACTION_PARAMETERS or not isinstance(params, dict):
        raise ValueError("unknown action")
    spec = ACTION_PARAMETERS[name]
    if set(params) - set(spec):
        raise ValueError("unknown action parameters")
    params = {key: value for key, value in params.items() if value is not None or (key in spec and spec[key][1])}
    if set(params) - set(spec) or any(required and key not in params for key, (_, required) in spec.items()):
        raise ValueError("invalid action parameters")
    if any(not _valid_parameter(spec[key][0], value) for key, value in params.items()):
        raise ValueError("invalid action parameter type")
    return Action(name, params)


def parse_intent_decision(raw: object) -> IntentDecision:
    if not isinstance(raw, dict) or set(raw) != {"kind", "actions", "question"}:
        raise ValueError("invalid intent decision")
    kind, actions, question = raw["kind"], raw["actions"], raw["question"]
    if kind not in ("chat", "plan", "clarification") or not isinstance(actions, list) or not isinstance(question, str) or len(question) > 1000:
        raise ValueError("invalid intent fields")
    if kind == "plan":
        if not 1 <= len(actions) <= MAX_ACTIONS or question:
            raise ValueError("invalid operation plan")
        parsed = tuple(parse_action(action) for action in actions)
        if sum(action.name in GAME_ACTIONS for action in parsed) > 1:
            raise ValueError("one game action per message")
        return IntentDecision(kind, parsed)
    if actions:
        raise ValueError(f"{kind} intent with actions")
    if kind == "chat":
        return IntentDecision(kind)
    if not question.strip():
        raise ValueError("clarification without question")
    return IntentDecision(kind, question=question)


def parse_intent_json(text: str) -> IntentDecision:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result = dict(items)
        if len(result) != len(items):
            raise ValueError("duplicate intent field")
        return result
    return parse_intent_decision(json.loads(text, object_pairs_hook=pairs))
