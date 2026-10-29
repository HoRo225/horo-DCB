from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.ai import ArchiveResult, ModelError, ModelReply
from src.ai.domain.intent import Intent
from src.core.domain.actions import Catalogue


class MemoryAiStore:
    def __init__(
        self, *, enabled: bool = True, channel_ids: tuple[int, ...] = (),
        role_ids: tuple[int, ...] = (), models: dict[str, object] | None = None,
    ) -> None:
        self.values: dict[str, object] = {
            "enabled": enabled, "channels": sorted(channel_ids),
            "roles": sorted(role_ids), "models": models,
        }
        self.saved: list[tuple[str, object, str | None]] = []
        self.failure: Exception | None = None
        self.tipped: set[int] = set()

    def _get(self, name: str) -> object:
        if self.failure is not None:
            raise self.failure
        return self.values[name]

    def _save(self, name: str, value: object, operation_id: str | None) -> bool:
        changed = self.values[name] != value
        self.values[name] = value
        self.saved.append((name, value, operation_id))
        return changed

    def enabled(self) -> bool:
        return self._get("enabled")

    def set_enabled(self, value: bool, *, operation_id: str | None) -> bool:
        return self._save("enabled", value, operation_id)

    def channel_ids(self) -> list[int]:
        return list(self._get("channels"))

    def role_ids(self) -> list[int]:
        return list(self._get("roles"))

    def set_allowlist(self, ids: list[int], *, roles: bool, operation_id: str | None) -> bool:
        return self._save("roles" if roles else "channels", list(ids), operation_id)

    def models(self) -> dict[str, object] | None:
        return self._get("models")

    def save_models(self, payload: dict[str, object], *, operation_id: str | None) -> bool:
        return self._save("models", payload, operation_id)

    def claim_tip(self, user_id: int) -> bool:
        first = user_id not in self.tipped
        self.tipped.add(user_id)
        return first


class FakeModel:
    def __init__(self, *, allow: bool = True, admission_error: Exception | None = None,
                 work_seconds: float = 5.0, deadline_seconds: float = 5.0) -> None:
        self.start = AsyncMock()
        self.try_start_request = MagicMock(return_value=allow)
        self.request_counts = MagicMock(return_value=(0, 0))
        self.quota_full = MagicMock(return_value=False)
        self.get_models = AsyncMock(return_value=())
        self.chat = AsyncMock(return_value=ModelReply("最終答案"))
        self.structured = AsyncMock(return_value=Intent("chat"))
        self.summarize = AsyncMock(return_value=ModelReply("操作已完成。"))
        self.generate_once = AsyncMock(return_value=ModelReply("最終答案"))
        self.cancel = AsyncMock()
        self.cancel_revoked = AsyncMock()
        self.reset_conversation = AsyncMock(return_value=ArchiveResult(1, 1, 0))
        self.archive_scope = AsyncMock(return_value=ArchiveResult(0, 0, 0))
        self.admission_error = admission_error
        self.work_seconds = work_seconds
        self.deadline_seconds = deadline_seconds
        self.accepted: list[str] = []
        self.job: SimpleNamespace | None = None

    @contextlib.asynccontextmanager
    async def accepted_request(self, key, *, deadline, still_current, user_id=None, parent_channel_id=None):
        if self.admission_error is not None:
            raise self.admission_error
        if not still_current():
            raise ModelError("revoked")
        self.accepted.append(key)
        now = asyncio.get_running_loop().time()
        self.job = SimpleNamespace(
            key=key, user_id=user_id, parent_channel_id=parent_channel_id, accepted_at=now, started_at=now,
            deadline=min(deadline, now + self.deadline_seconds), work_deadline=min(deadline, now + self.work_seconds),
            current=True,
        )
        yield self.job


class FakeActions:
    def __init__(self) -> None:
        self.catalogue = Catalogue([], [])
        self.context = MagicMock(return_value={})
        self.needs_confirmation = MagicMock(return_value=False)
        self.execute = AsyncMock()
        self.explain = AsyncMock()


class FakeMedia:
    def __init__(self) -> None:
        self.read = AsyncMock(side_effect=self.decode)

    @staticmethod
    async def decode(groups, *, deadline):
        return [[("靜態圖", (f"img:{url}",)) for _kind, url in group] for group in groups], 0, 0, 0


class Games:
    def __init__(self, channel_id: int | None = None) -> None:
        self.channel_id = channel_id
