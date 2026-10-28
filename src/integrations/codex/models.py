from __future__ import annotations

from src.ai import ModelInfo, model_identifier


def parse_model_list(entries: object) -> tuple[ModelInfo, ...]:
    if not isinstance(entries, list) or not entries or len(entries) > 1000:
        raise ValueError("invalid model catalog")
    result: list[ModelInfo] = []
    seen: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise ValueError("invalid model catalog entry")
        model = model_identifier(raw.get("model"))
        name = raw.get("displayName")
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or model in seen:
            raise ValueError("invalid model catalog name")
        efforts = raw.get("supportedReasoningEfforts")
        modalities = raw.get("inputModalities") or []
        if (
            not isinstance(efforts, list)
            or len(efforts) > 25
            or not isinstance(modalities, list)
            or len(modalities) > 25
            or any(not isinstance(option, dict) for option in efforts)
        ):
            raise ValueError("invalid model capabilities")
        supported = tuple(model_identifier(option.get("reasoningEffort")) for option in efforts)
        inputs = tuple(model_identifier(item) for item in modalities)
        default = model_identifier(raw.get("defaultReasoningEffort"))
        if (
            len(set(supported)) != len(supported)
            or len(set(inputs)) != len(inputs)
            or (supported and default not in supported)
        ):
            raise ValueError("inconsistent model capabilities")
        seen.add(model)
        result.append(ModelInfo(model, name, default, supported, inputs))
    return tuple(result)
