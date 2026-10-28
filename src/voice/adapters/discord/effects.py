from __future__ import annotations

from src.core.app.effects import EffectKind, ResourceProof
from src.voice.app.voice import Voice


def voice_effect_kinds(voice: Voice) -> tuple[EffectKind, ...]:
    return (
        EffectKind("voice.create", "建立臨時語音房", proof=ResourceProof(voice.confirm, "請提供已核對的語音頻道 ID。")),
        EffectKind("voice.delete", "刪除臨時語音房", retry=voice.retry,
                   unconfirmable="刪除操作請使用重試清理，系統會核對既有頻道 ID。"),
    )
