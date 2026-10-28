from __future__ import annotations

import logging

from src.ai.app.ports import AiStore, ModelError, TextModel
from src.ai.domain.models import DEFAULT_MODEL_SETTINGS, ModelSettings, parse_model_settings
from src.core.domain.ids import is_positive_int

MAX_CODEX_ALLOWED_CHANNELS = 25


def valid_allowlist_ids(values: object, *, minimum: int) -> bool:
    return (
        isinstance(values, frozenset)
        and minimum <= len(values) <= MAX_CODEX_ALLOWED_CHANNELS
        and all(map(is_positive_int, values))
    )


class AiSettings:
    def __init__(self, store: AiStore, model: TextModel, guild_id: int) -> None:
        self._store = store
        self._model = model
        self.guild_id = guild_id
        self.generation = 0
        self._channel_generations: dict[int, int] = {}
        self._suspended_channels: set[int] = set()

    @property
    def enabled(self) -> bool:
        return self._store.enabled()

    async def set_enabled(self, value: bool, *, operation_id: str | None = None) -> bool:
        if type(value) is not bool:
            raise ValueError("invalid AI switch")
        before = self.enabled
        if not self._store.set_enabled(value, operation_id=operation_id) or not before or value:
            return True
        self.generation += 1
        try:
            await self._model.cancel(self.guild_id)
        except ModelError:
            logging.exception("AI was disabled but requests in flight could not be stopped.")
            return False
        return True

    @property
    def channel_ids(self) -> frozenset[int]:
        return frozenset(self._store.channel_ids())

    @property
    def role_ids(self) -> frozenset[int]:
        return frozenset(self._store.role_ids())

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
        raw = self._store.models()
        return DEFAULT_MODEL_SETTINGS if raw is None else parse_model_settings(raw)

    def save_models(self, settings: ModelSettings, *, operation_id: str | None = None) -> bool:
        payload = parse_model_settings(settings.to_payload()).to_payload()
        return self._store.save_models(payload, operation_id=operation_id)

    @property
    def configured(self) -> bool:
        return bool(self.channel_ids and self.role_ids)

    def validate_change(self, guild_id: int, selected: frozenset[int], *, roles: bool) -> None:
        if (
            type(guild_id) is not int
            or guild_id != self.guild_id
            or not valid_allowlist_ids(selected, minimum=0 if roles else 1)
            or (roles and (not self.channel_ids or guild_id in selected))
        ):
            raise ValueError(f"invalid Codex allowlist {'roles' if roles else 'channels'}")

    def set_allowlist(
        self, guild_id: int, selected: frozenset[int], *, roles: bool, operation_id: str | None = None
    ) -> bool:
        return self._store.set_allowlist(sorted(selected), roles=roles, operation_id=operation_id)

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
