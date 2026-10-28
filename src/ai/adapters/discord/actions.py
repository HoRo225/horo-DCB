from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping

from src.ai.app.access import AiAccess
from src.ai.app.ports import ModelError
from src.ai.app.settings import AiSettings
from src.ai.domain.models import ModelInfo, parse_model_settings, resolve_model_settings
from src.core.app.actions import ActionGroup, ActionSpec, Configure, Step
from src.core.app.ports import Source
from src.core.domain.actions import REGULAR_ROLES, Param, Target

STALE = "操作權限或期限已改變，未更新 AI 設定。"
TEXT_CHANNELS = Target("text_channels", ("AI 允許清單必須使用可見的文字頻道，未執行任何操作。", "指定 AI 頻道目前不可用，未執行這項操作。"))
MODEL_CHOICE = {"type": "object", "properties": {"model": {"type": "string"}, "effort": {"type": ["string", "null"]}},
                "required": ["model", "effort"], "additionalProperties": False}


def _model_choice(value: object) -> bool:
    try:
        parse_model_settings({"primary": value, "fallback": None})
    except TypeError, ValueError:
        return False
    return True


def ai_actions(
    settings: AiSettings, access: AiAccess, models: Callable[[], Awaitable[Iterable[ModelInfo]]],
) -> ActionGroup:
    async def set_enabled(step: Step, params: Mapping[str, object]) -> None:
        await settings.set_enabled(params["enabled"], operation_id=step.operation_id)

    async def check_models(source: Source, params: Mapping[str, object]) -> None:
        try:
            resolve_model_settings(parse_model_settings(params), await models())
        except ModelError as exc:
            raise ValueError("model catalogue is unavailable") from exc

    async def save_models(step: Step, params: Mapping[str, object]) -> None:
        settings.save_models(parse_model_settings(params), operation_id=step.operation_id)

    def allowlist(roles: bool) -> ActionSpec:
        async def check(source: Source, params: Mapping[str, object]) -> None:
            settings.validate_change(source.guild_id, frozenset(params["ids"]), roles=roles)

        async def change(step: Step, params: Mapping[str, object]) -> None:
            await access.change(step.source.guild_id, frozenset(params["ids"]), roles=roles,
                                still_current=step.still_current, operation_id=step.operation_id)

        return ActionSpec("ai_roles" if roles else "ai_channels",
                          {"ids": Param("ids", target=REGULAR_ROLES if roles else TEXT_CHANNELS)},
                          admin=True, effect=Configure(change, stale=STALE), validate=check)

    return ActionGroup("ai", (
        ActionSpec("ai_enabled", {"enabled": Param("bool")}, admin=True, effect=Configure(set_enabled)),
        ActionSpec("ai_models", {
            "primary": Param("object", schema=MODEL_CHOICE, accepts=lambda value: value is not None and _model_choice(value)),
            "fallback": Param("object", schema={"anyOf": [MODEL_CHOICE, {"type": "null"}]},
                              accepts=lambda value: value is None or _model_choice(value)),
        }, admin=True, effect=Configure(save_models), validate=check_models),
        allowlist(roles=False),
        allowlist(roles=True),
    ))
