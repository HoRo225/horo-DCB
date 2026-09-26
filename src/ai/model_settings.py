from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from src.state import read_json_state, write_json_atomic

DEFAULT_MODEL_SETTINGS_PATH = Path("/app/data/codex_model_settings.json")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("invalid model identifier")
    return value


@dataclass(frozen=True, slots=True)
class ModelChoice:
    model: str
    effort: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {"model": self.model, "effort": self.effort}


@dataclass(frozen=True, slots=True)
class ModelSettings:
    primary: ModelChoice
    fallback: ModelChoice | None

    def to_payload(self) -> dict[str, object]:
        return {
            "primary": self.primary.to_payload(),
            "fallback": self.fallback.to_payload() if self.fallback is not None else None,
        }


DEFAULT_MODEL_SETTINGS = ModelSettings(ModelChoice("gpt-5.6-luna"), ModelChoice("gpt-6-luna"))


def parse_model_choice(value: object) -> ModelChoice:
    if not isinstance(value, dict) or set(value) != {"model", "effort"}:
        raise ValueError("invalid model choice")
    effort = value["effort"]
    return ModelChoice(_identifier(value["model"]), None if effort is None else _identifier(effort))


def parse_model_settings(value: object) -> ModelSettings:
    if not isinstance(value, dict) or set(value) != {"primary", "fallback"}:
        raise ValueError("invalid model settings")
    return ModelSettings(
        parse_model_choice(value["primary"]),
        None if value["fallback"] is None else parse_model_choice(value["fallback"]),
    )


@dataclass(frozen=True, slots=True)
class ModelInfo:
    model: str
    display_name: str
    default_effort: str
    supported_efforts: tuple[str, ...]
    input_modalities: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "model": self.model,
            "display_name": self.display_name,
            "default_effort": self.default_effort,
            "supported_efforts": list(self.supported_efforts),
            "input_modalities": list(self.input_modalities),
        }


def parse_model_catalog(value: object) -> tuple[ModelInfo, ...]:
    if (
        not isinstance(value, dict)
        or set(value) != {"models", "fetched_at"}
        or type(value["fetched_at"]) is not int
        or value["fetched_at"] <= 0
        or not isinstance(value["models"], list)
        or not value["models"]
        or len(value["models"]) > 1000
    ):
        raise ValueError("invalid model catalog")
    result: list[ModelInfo] = []
    seen: set[str] = set()
    fields = {"model", "display_name", "default_effort", "supported_efforts", "input_modalities"}
    for raw in value["models"]:
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ValueError("invalid model catalog entry")
        model = _identifier(raw["model"])
        name = raw["display_name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or model in seen:
            raise ValueError("invalid model catalog name")
        efforts = raw["supported_efforts"]
        modalities = raw["input_modalities"]
        if (
            not isinstance(efforts, list)
            or len(efforts) > 25
            or not isinstance(modalities, list)
            or len(modalities) > 25
        ):
            raise ValueError("invalid model capabilities")
        supported = tuple(_identifier(item) for item in efforts)
        inputs = tuple(_identifier(item) for item in modalities)
        default = _identifier(raw["default_effort"])
        if (
            len(set(supported)) != len(supported)
            or len(set(inputs)) != len(inputs)
            or (supported and default not in supported)
        ):
            raise ValueError("inconsistent model capabilities")
        seen.add(model)
        result.append(ModelInfo(model, name, default, supported, inputs))
    return tuple(result)


class ModelSettingsStore:
    def __init__(self, path: Path | str = DEFAULT_MODEL_SETTINGS_PATH) -> None:
        self.path = Path(path)
        self.available = True
        self._settings = DEFAULT_MODEL_SETTINGS
        try:
            raw = read_json_state(self.path, 1)
            if set(raw) != {"version", "primary", "fallback"}:
                raise ValueError("invalid model settings state")
            self._settings = parse_model_settings(
                {key: raw[key] for key in ("primary", "fallback")}
            )
        except FileNotFoundError:
            pass
        except OSError, ValueError, TypeError:
            self.available = False
            logging.error("Codex 模型設定檔無法讀取；請由管理面板重新儲存設定。")

    def snapshot(self) -> ModelSettings:
        if not self.available:
            raise ValueError("model settings unavailable")
        return self._settings

    def save(self, settings: ModelSettings) -> None:
        checked = parse_model_settings(settings.to_payload())
        write_json_atomic(self.path, {"version": 1, **checked.to_payload()})
        self._settings = checked
        self.available = True
