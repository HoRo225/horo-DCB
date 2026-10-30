from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelChoice:
    model: str

    def to_payload(self) -> dict[str, object]:
        return {"model": self.model}


@dataclass(frozen=True, slots=True)
class ModelSettings:
    primary: ModelChoice
    fallback: ModelChoice | None

    def to_payload(self) -> dict[str, object]:
        return {
            "primary": self.primary.to_payload(),
            "fallback": self.fallback.to_payload() if self.fallback is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ModelInfo:
    model: str
    display_name: str


MODEL_CATALOG: tuple[ModelInfo, ...] = (
    ModelInfo("claude-haiku-5-5", "Claude Haiku 5.5"),
    ModelInfo("claude-haiku-5-5-thinking", "Claude Haiku 5.5（思考）"),
    ModelInfo("claude-sonnet-5-5", "Claude Sonnet 5.5"),
    ModelInfo("claude-sonnet-5-5-thinking", "Claude Sonnet 5.5（思考）"),
)
DEFAULT_MODEL_SETTINGS = ModelSettings(ModelChoice("claude-haiku-5-5"), ModelChoice("claude-sonnet-5-5"))


def find_model(catalog: Iterable[ModelInfo], model: str | None) -> ModelInfo | None:
    if model is None:
        return None
    return next((item for item in catalog if item.model == model), None)


def parse_model_choice(value: object) -> ModelChoice:
    if not isinstance(value, dict) or set(value) != {"model"} or find_model(MODEL_CATALOG, value["model"]) is None:
        raise ValueError("invalid model choice")
    return ModelChoice(value["model"])


def parse_model_settings(value: object) -> ModelSettings:
    if not isinstance(value, dict) or set(value) != {"primary", "fallback"}:
        raise ValueError("invalid model settings")
    return ModelSettings(
        parse_model_choice(value["primary"]),
        None if value["fallback"] is None else parse_model_choice(value["fallback"]),
    )
