from __future__ import annotations

import time
from typing import Any

from openai_codex.generated.v2_all import ModelListParams, ModelListResponse

from src.ai.model_settings import ModelChoice, ModelInfo
from src.ai.protocol import CodexBridgeError


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


async def read_models(codex: Any) -> dict[str, object]:
    models: dict[str, ModelInfo] = {}
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        params = ModelListParams(cursor=cursor, limit=100, include_hidden=False)
        response = await codex._client.request(
            "model/list",
            params.model_dump(mode="json", by_alias=True, exclude_none=True),
            response_model=ModelListResponse,
        )
        for item in response.data:
            if item.hidden:
                continue
            info = ModelInfo(
                model=item.model,
                display_name=item.display_name,
                default_effort=_value(item.default_reasoning_effort),
                supported_efforts=tuple(
                    _value(option.reasoning_effort) for option in item.supported_reasoning_efforts
                ),
                input_modalities=tuple(_value(value) for value in (item.input_modalities or ())),
            )
            if info.model in models and models[info.model] != info:
                raise ValueError("conflicting model capabilities")
            models[info.model] = info
        cursor = response.next_cursor
        if cursor is None:
            break
        if cursor in seen:
            raise ValueError("repeated model cursor")
        seen.add(cursor)
    if not models:
        raise ValueError("empty model catalog")
    return {
        "models": [info.to_payload() for info in models.values()],
        "fetched_at": int(time.time()),
    }


def resolve_choice(
    choice: ModelChoice, catalog: tuple[ModelInfo, ...], *, images: bool
) -> ModelChoice:
    info = next((item for item in catalog if item.model == choice.model), None)
    if info is None:
        raise CodexBridgeError("model_configuration_invalid")
    effort = choice.effort if choice.effort is not None else info.default_effort
    if effort not in info.supported_efforts:
        raise CodexBridgeError("model_configuration_invalid")
    if "text" not in info.input_modalities or (images and "image" not in info.input_modalities):
        raise CodexBridgeError("unsupported_input")
    return ModelChoice(choice.model, effort)
