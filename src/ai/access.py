from __future__ import annotations

import sqlite3

from src.ai.domain.models import DEFAULT_MODEL_SETTINGS, ModelSettings, parse_model_settings
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.settings import SettingsStore
from src.core.domain.ids import is_positive_int
from src.core.domain.settings import Setting

MAX_CODEX_ALLOWED_CHANNELS = 25
AI_ENABLED = Setting("feature.ai", False)
AI_TEXT_DISPLAY = Setting("feature.ai_text_display", True)
AI_CHANNEL_IDS: Setting[list[int]] = Setting("ai.channel_ids", [])
AI_ROLE_IDS: Setting[list[int]] = Setting("ai.role_ids", [])
AI_MODELS: Setting[dict[str, object] | None] = Setting("ai.models", None)


def valid_allowlist_ids(values: object, *, minimum: int) -> bool:
    return (
        isinstance(values, frozenset)
        and minimum <= len(values) <= MAX_CODEX_ALLOWED_CHANNELS
        and all(map(is_positive_int, values))
    )


class AiSettings:
    def __init__(self, db: sqlite3.Connection, guild_id: int) -> None:
        self._settings = SettingsStore(db, OperationStore(db))
        self.guild_id = guild_id
        self.generation = 0
        self._channel_generations: dict[int, int] = {}
        self._suspended_channels: set[int] = set()

    @property
    def enabled(self) -> bool:
        return self._settings.get(AI_ENABLED)

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.set_enabled(value)

    def set_enabled(self, value: bool, *, operation_id: str | None = None) -> bool:
        if type(value) is not bool:
            raise ValueError("invalid AI switch")
        before = self.enabled
        changed = self._settings.save("ai.enabled", {"enabled": value}, {AI_ENABLED: value}, operation_id=operation_id)
        if changed and before and not value:
            self.generation += 1
        return changed

    @property
    def channel_ids(self) -> frozenset[int]:
        return frozenset(self._settings.get(AI_CHANNEL_IDS))

    @property
    def role_ids(self) -> frozenset[int]:
        return frozenset(self._settings.get(AI_ROLE_IDS))

    def denial_reason(
        self,
        guild_id: int | None,
        channel_id: int | None,
        role_ids: frozenset[int] = frozenset(),
    ) -> str | None:
        if not self.enabled:
            return "disabled"
        if channel_id in self._suspended_channels:
            return "suspended"
        if guild_id != self.guild_id:
            return "guild"
        if channel_id not in self.channel_ids:
            return "channel"
        if not self.role_ids & role_ids:
            return "role"
        return None

    @property
    def models(self) -> ModelSettings:
        raw = self._settings.get(AI_MODELS)
        return DEFAULT_MODEL_SETTINGS if raw is None else parse_model_settings(raw)

    def save_models(self, settings: ModelSettings, *, operation_id: str | None = None) -> bool:
        payload = parse_model_settings(settings.to_payload()).to_payload()
        return self._settings.save("ai.models", payload, {AI_MODELS: payload}, operation_id=operation_id)

    @property
    def text_display(self) -> bool:
        return self._settings.get(AI_TEXT_DISPLAY)

    @text_display.setter
    def text_display(self, value: bool) -> None:
        self.set_display(value)

    def set_display(self, value: bool, *, operation_id: str | None = None) -> bool:
        if type(value) is not bool:
            raise ValueError("invalid AI display switch")
        return self._settings.save("ai.display", {"enabled": value}, {AI_TEXT_DISPLAY: value}, operation_id=operation_id)

    @property
    def configured(self) -> bool:
        return bool(self.channel_ids and self.role_ids)

    def validate_change(self, guild_id: int, selected: frozenset[int], *, roles: bool) -> None:
        if (
            not self.enabled
            or type(guild_id) is not int
            or guild_id != self.guild_id
            or not valid_allowlist_ids(selected, minimum=0 if roles else 1)
            or (roles and (not self.channel_ids or guild_id in selected))
        ):
            raise ValueError(f"invalid Codex allowlist {'roles' if roles else 'channels'}")

    def set_allowlist(
        self, guild_id: int, selected: frozenset[int], *, roles: bool, operation_id: str | None = None
    ) -> bool:
        setting = AI_ROLE_IDS if roles else AI_CHANNEL_IDS
        return self._settings.save(
            setting.key, {"ids": sorted(selected)}, {setting: sorted(selected)}, operation_id=operation_id,
        )

    def channel_generation(self, channel_id: int) -> int:
        return self._channel_generations.get(channel_id, 0)

    def is_current(self, generation: int, channel_id: int | None = None, channel_generation: int = 0) -> bool:
        return (
            generation == self.generation and channel_id not in self._suspended_channels
            and (channel_id is None or self.channel_generation(channel_id) == channel_generation)
        )

    def suspend(self, channel_ids: frozenset[int]) -> None:
        for channel_id in channel_ids - self._suspended_channels:
            self._channel_generations[channel_id] = self.channel_generation(channel_id) + 1
        self._suspended_channels.update(channel_ids)

    def resume(self, channel_ids: frozenset[int]) -> None:
        self._suspended_channels.difference_update(channel_ids)
