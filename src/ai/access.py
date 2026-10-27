from __future__ import annotations

import sqlite3

from src.codex.model_settings import DEFAULT_MODEL_SETTINGS, ModelSettings, parse_model_settings
from src.config import is_positive_int
from src.state import setting
from src.settings_ops import save_settings

MAX_CODEX_ALLOWED_CHANNELS = 25


def valid_allowlist_ids(values: object, *, minimum: int) -> bool:
    return (
        isinstance(values, frozenset)
        and minimum <= len(values) <= MAX_CODEX_ALLOWED_CHANNELS
        and all(map(is_positive_int, values))
    )


class AiSettings:
    def __init__(self, db: sqlite3.Connection, guild_id: int) -> None:
        self._db = db
        self.guild_id = guild_id
        self.generation = 0
        self._channel_generations: dict[int, int] = {}
        self._suspended_channels: set[int] = set()

    @property
    def enabled(self) -> bool:
        return setting(self._db, "feature.ai")

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self.set_enabled(value)

    def set_enabled(self, value: bool, *, operation_id: str | None = None) -> bool:
        if type(value) is not bool:
            raise ValueError("invalid AI switch")
        before = self.enabled
        changed = save_settings(self._db, operation_id, "ai.enabled", {"enabled": value}, {"feature.ai": value})
        if changed and before and not value:
            self.generation += 1
        return changed

    @property
    def channel_ids(self) -> frozenset[int]:
        return frozenset(setting(self._db, "ai.channel_ids"))

    @property
    def role_ids(self) -> frozenset[int]:
        return frozenset(setting(self._db, "ai.role_ids"))

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
        raw = setting(self._db, "ai.models")
        return DEFAULT_MODEL_SETTINGS if raw is None else parse_model_settings(raw)

    def save_models(self, settings: ModelSettings, *, operation_id: str | None = None) -> bool:
        payload = parse_model_settings(settings.to_payload()).to_payload()
        return save_settings(self._db, operation_id, "ai.models", payload, {"ai.models": payload})

    @property
    def text_display(self) -> bool:
        return setting(self._db, "feature.ai_text_display")

    @text_display.setter
    def text_display(self, value: bool) -> None:
        self.set_display(value)

    def set_display(self, value: bool, *, operation_id: str | None = None) -> bool:
        if type(value) is not bool:
            raise ValueError("invalid AI display switch")
        return save_settings(self._db, operation_id, "ai.display", {"enabled": value}, {"feature.ai_text_display": value})

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
        key = "ai.role_ids" if roles else "ai.channel_ids"
        return save_settings(self._db, operation_id, key, {"ids": sorted(selected)}, {key: sorted(selected)})

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
