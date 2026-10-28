from __future__ import annotations

from src.core.app.effects import EffectKind, MessageProof


def game_effect_kinds() -> tuple[EffectKind, ...]:
    return (
        EffectKind("game_hub_publish", "發布遊戲大廳", proof=MessageProof()),
        EffectKind("game_table_open", "開啟賭桌"),
        EffectKind("game_table_edit", "更新賭桌", proof=MessageProof(edited=True, same_message=True)),
        EffectKind("game_table_delete", "收起賭桌"),
    )
