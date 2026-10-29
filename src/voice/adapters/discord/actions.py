from __future__ import annotations

from collections.abc import Mapping

import discord

from src.core.adapters.discord.live import LiveGuild
from src.core.app.actions import ActionGroup, ActionSpec, Configure, Network, Step
from src.core.domain.actions import Param, Target, failure
from src.voice.adapters.discord.gateway import voice_channel_id
from src.voice.app.voice import Voice

STALE = "操作權限或期限已改變，未更新語音入口。"
VOICE_CHANNEL = Target("voice_channel", ("語音入口必須是目前伺服器的語音頻道，未執行任何操作。", "指定語音入口目前不可用，未執行這項操作。"))


def voice_actions(voice: Voice, live: LiveGuild) -> ActionGroup:
    def guild() -> discord.Guild:
        if (current := live.guild()) is None:
            raise LookupError("current guild is unavailable")
        return current

    async def set_enabled(step: Step, params: Mapping[str, object]) -> Mapping[str, object] | None:
        if problem := await voice.set_enabled(params["enabled"], operation_id=step.operation_id):
            return {"ok": True, "code": "done", "text": f"已開啟臨時語音；{problem}"}
        return None

    async def bind_entry(step: Step, params: Mapping[str, object]) -> Mapping[str, object] | None:
        channel = guild().get_channel(params["channel_id"])
        if problem := await voice.bind_entry(voice_channel_id(channel), still_current=step.still_current,
                                             operation_id=step.operation_id):
            return failure("channel", problem)
        return None

    async def sync(step: Step, params: Mapping[str, object]) -> None:
        guild()
        await voice.reconcile(still_current=step.still_current)

    return ActionGroup("voice", (
        ActionSpec("voice_enabled", {"enabled": Param("bool")}, admin=True, effect=Configure(set_enabled),
                   label=lambda params: "開啟臨時語音" if params["enabled"] else "關閉臨時語音"),
        ActionSpec("voice_entry", {"channel_id": Param("id", target=VOICE_CHANNEL)}, admin=True,
                   effect=Configure(bind_entry, stale=STALE), label="設定臨時語音入口"),
        ActionSpec("voice_sync", {}, admin=True, effect=Network(sync), label="重新同步臨時語音"),
    ))
