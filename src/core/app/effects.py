from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from src.core.domain.records import DiscordEffect

OTHER = "其他項目"
MANUAL_CLEANUP = "請先核對並人工清理未啟用資源，再略過原項目；不會恢復舊指令。"
UNDELIVERED = "已處理送出要求；尚未確認送達，請重新整理查看狀態。"

type StillCurrent = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class MessageProof:
    edited: bool = False
    same_message: bool = False
    check: Callable[[DiscordEffect, object], str | None] | None = None


@dataclass(frozen=True, slots=True)
class ResourceProof:
    confirm: Callable[[str, int, StillCurrent], Awaitable[str | None]]
    prompt: str


@dataclass(frozen=True, slots=True)
class EffectKind:
    name: str
    label: str
    proof: MessageProof | ResourceProof | None = None
    retry: Callable[[str, StillCurrent], Awaitable[str | None]] | None = None
    settle: Callable[[DiscordEffect], None] | None = None
    unconfirmable: str = MANUAL_CLEANUP


@dataclass(frozen=True, slots=True)
class PendingEffect:
    effect: DiscordEffect
    kind: EffectKind

    @property
    def confirmable(self) -> bool:
        return self.kind.proof is not None and self.effect.status != "failed"

    @property
    def retryable(self) -> bool:
        return self.kind.retry is not None


def delivered(sent: bool) -> str | None:
    return None if sent else UNDELIVERED


class EffectRegistry:
    def __init__(self) -> None:
        self._kinds: dict[str, EffectKind] = {}

    def register(self, *kinds: EffectKind) -> None:
        for kind in kinds:
            if kind.name in self._kinds:
                raise ValueError(f"effect kind {kind.name!r} is already registered")
            self._kinds[kind.name] = kind

    def kind(self, name: str) -> EffectKind:
        return self._kinds.get(name) or EffectKind(name, OTHER)
