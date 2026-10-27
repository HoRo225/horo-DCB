from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from src.config import is_positive_int

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


DEFAULT_MODEL_SETTINGS = ModelSettings(ModelChoice("gpt-6-luna"), ModelChoice("gpt-5.6-luna"))


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


def find_model(catalog: Iterable[ModelInfo], model: str | None) -> ModelInfo | None:
    if model is None:
        return None
    return next((item for item in catalog if item.model == model), None)


def resolve_model_settings(settings: ModelSettings, catalog: Iterable[ModelInfo]) -> ModelSettings:
    catalog = tuple(catalog)

    def resolve(choice: ModelChoice) -> ModelChoice:
        info = find_model(catalog, choice.model)
        if info is None:
            raise ValueError("model unavailable")
        effort = choice.effort or info.default_effort
        if info.supported_efforts and effort not in info.supported_efforts:
            raise ValueError("unsupported reasoning effort")
        return ModelChoice(choice.model, effort)

    return ModelSettings(resolve(settings.primary), resolve(settings.fallback) if settings.fallback else None)


def with_effort(settings: ModelSettings, effort: str, catalog: Iterable[ModelInfo]) -> ModelSettings:
    catalog = tuple(catalog)

    def choose(choice: ModelChoice) -> ModelChoice:
        info = find_model(catalog, choice.model)
        return ModelChoice(choice.model, effort if info and effort in info.supported_efforts else None)

    return ModelSettings(choose(settings.primary), choose(settings.fallback) if settings.fallback else None)


def parse_model_catalog(value: object) -> tuple[ModelInfo, ...]:
    if (
        not isinstance(value, dict)
        or set(value) != {"models", "fetched_at"}
        or not is_positive_int(value["fetched_at"])
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
