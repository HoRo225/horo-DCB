from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.core.domain.actions import MAX_ACTIONS, Catalogue, Plan

MAX_QUESTION = 1000


@dataclass(frozen=True, slots=True)
class Intent:
    kind: Literal["chat", "plan", "clarification"]
    plan: Plan | None = None
    question: str = ""


def parse_envelope(raw: object, catalogue: Catalogue) -> Intent:
    if not isinstance(raw, dict) or set(raw) != {"kind", "actions", "question"}:
        raise ValueError("invalid intent decision")
    kind, actions, question = raw["kind"], raw["actions"], raw["question"]
    if (kind not in ("chat", "plan", "clarification") or not isinstance(actions, list)
            or not isinstance(question, str) or len(question) > MAX_QUESTION):
        raise ValueError("invalid intent fields")
    if kind == "plan":
        if question:
            raise ValueError("invalid operation plan")
        return Intent(kind, catalogue.parse_plan(actions))
    if actions:
        raise ValueError(f"{kind} intent with actions")
    if kind == "chat":
        return Intent(kind)
    if not question.strip():
        raise ValueError("clarification without question")
    return Intent(kind, question=question)


def envelope_schema(catalogue: Catalogue) -> dict[str, object]:
    return {
        "title": "IntentEnvelope", "type": "object", "additionalProperties": False,
        "required": ["kind", "actions", "question"],
        "properties": {
            "kind": {"type": "string", "enum": ["chat", "plan", "clarification"]},
            "actions": {"type": "array", "items": {"anyOf": catalogue.choices()}, "maxItems": MAX_ACTIONS},
            "question": {"type": "string"},
        },
    }
