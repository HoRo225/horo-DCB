from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

MAX_ACTIONS = 4
MAX_IDS = 25
MAX_TEXT = 500
MAX_ID = 2**63 - 1

type TargetKind = Literal["text_channel", "voice_channel", "text_channels", "roles", "quoted"]
type ParamKind = Literal["id", "ids", "bool", "text", "enum", "object"]


def failure(code: str, text: str) -> dict[str, object]:
    return {"ok": False, "code": code, "text": text}


def _is_id(value: object) -> bool:
    return type(value) is int and 0 < value <= MAX_ID


@dataclass(frozen=True, slots=True)
class Target:
    kind: TargetKind
    unusable: tuple[str, str] = ("", "")


@dataclass(frozen=True, slots=True)
class Param:
    kind: ParamKind
    required: bool = True
    choices: tuple[str, ...] = ()
    schema: Mapping[str, object] | None = None
    accepts: Callable[[object], bool] | None = None
    target: Target | None = None

    def valid(self, value: object) -> bool:
        match self.kind:
            case "id":
                shaped = _is_id(value)
            case "ids":
                shaped = (isinstance(value, list) and len(value) <= MAX_IDS and all(map(_is_id, value))
                          and len(set(value)) == len(value))
            case "bool":
                shaped = type(value) is bool
            case "text":
                shaped = isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_TEXT
            case "enum":
                shaped = value in self.choices
            case "object":
                shaped = True
        return shaped and (self.accepts is None or self.accepts(value))

    def json_schema(self) -> dict[str, object]:
        match self.kind:
            case "id":
                shape: dict[str, object] = {"type": "integer", "minimum": 1}
            case "ids":
                shape = {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": MAX_IDS}
            case "bool":
                shape = {"type": "boolean"}
            case "text":
                shape = {"type": "string"}
            case "enum":
                shape = {"type": "string", "enum": list(self.choices)}
            case "object":
                shape = dict(self.schema or {})
        return shape if self.required else {"anyOf": [shape, {"type": "null"}]}


@dataclass(frozen=True, slots=True)
class Action:
    name: str
    params: Mapping[str, object]

    def to_payload(self) -> dict[str, object]:
        return {"name": self.name, "params": dict(self.params)}


@dataclass(frozen=True, slots=True)
class Plan:
    actions: tuple[Action, ...]

    def to_payload(self) -> list[dict[str, object]]:
        return [action.to_payload() for action in self.actions]


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    params: Mapping[str, Param]
    exclusive: str | None = None


class Catalogue:
    def __init__(self, entries: Sequence[Entry], hints: Sequence[str]) -> None:
        self._entries = {entry.name: entry for entry in entries}
        self._hints = tuple(hints)

    def hints(self) -> tuple[str, ...]:
        return self._hints

    def parse_action(self, raw: object) -> Action:
        if not isinstance(raw, dict) or set(raw) != {"name", "params"}:
            raise ValueError("invalid action")
        name, params = raw["name"], raw["params"]
        if not isinstance(name, str) or name not in self._entries or not isinstance(params, dict):
            raise ValueError("unknown action")
        spec = self._entries[name].params
        if set(params) - set(spec):
            raise ValueError("unknown action parameters")
        params = {key: value for key, value in params.items() if value is not None or spec[key].required}
        if any(param.required and key not in params for key, param in spec.items()):
            raise ValueError("invalid action parameters")
        if any(not spec[key].valid(value) for key, value in params.items()):
            raise ValueError("invalid action parameter type")
        return Action(name, params)

    def parse_plan(self, raw: object) -> Plan:
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ACTIONS:
            raise ValueError("invalid operation plan")
        actions = tuple(self.parse_action(item) for item in raw)
        exclusive = Counter(group for action in actions if (group := self._entries[action.name].exclusive) is not None)
        if any(count > 1 for count in exclusive.values()):
            raise ValueError("one exclusive action per message")
        return Plan(actions)

    def choices(self) -> list[dict[str, object]]:
        choices = []
        for name, entry in self._entries.items():
            properties = {key: param.json_schema() for key, param in entry.params.items()}
            choices.append({
                "type": "object", "properties": {
                    "name": {"type": "string", "enum": [name]},
                    "params": {"type": "object", "properties": properties, "required": list(properties),
                               "additionalProperties": False},
                }, "required": ["name", "params"], "additionalProperties": False,
            })
        return choices


TEXT_CHANNEL = Target("text_channel", ("這項設定必須使用目前伺服器的文字頻道，未執行任何操作。", "指定文字頻道目前不可用，未執行這項操作。"))
REGULAR_ROLES = Target("roles", ("指定的頻道或身分組不存在，未執行任何操作。", "只能選擇一般身分組，未執行這項操作。"))
