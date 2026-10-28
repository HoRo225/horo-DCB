from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import discord

from src.core.adapters.discord.channels import text_channel_problem
from src.core.adapters.discord.live import LiveGuild
from src.core.app.actions import ActionGroup, ActionSpec, Configure, Network, Play, Step
from src.core.app.ports import Actor
from src.core.domain.actions import REGULAR_ROLES, TEXT_CHANNEL, Param, failure
from src.games.access import roles_play_problem
from src.games.hub import bind_channel
from src.games.operations import GameOperations
from src.games.tables import TABLES

HINT = "open_table 使用 blackjack（21 點）或 slots（拉霸），牌局不下注；deal 開新局、again 再來一局、hit 叫牌、stand 停牌、leave_table 離桌。"
BUSY = "收到訊息時賭桌仍在處理上一個動作，請等完成後再要求下一局。"


def game_actions(operations: GameOperations, live: LiveGuild) -> tuple[ActionGroup, ActionGroup]:
    store = operations.store

    def play(name: str) -> Play:
        async def run(step: Step, params: Mapping[str, object]) -> Mapping[str, object]:
            source = SimpleNamespace(id=step.source.message_id, user=SimpleNamespace(id=step.actor.user_id),
                                     guild_id=step.source.guild_id, channel_id=step.source.channel_id)
            return await operations.execute(source, name, game=params.get("game"),
                                            expected_token=step.received["game"].get("state_token"),
                                            operation_id=step.operation_id, still_current=step.still_current)
        return Play(run)

    def available(actor: Actor, channel_id: int) -> str | None:
        return roles_play_problem(live, store, live.guild_id, channel_id, actor.role_ids)

    def snapshot(user_id: int | None) -> Mapping[str, object]:
        return operations.snapshot(user_id) if user_id is not None else {"state_token": None, "busy": True}

    def guild() -> discord.Guild:
        if (current := live.guild()) is None:
            raise LookupError("current guild is unavailable")
        return current

    async def set_roles(step: Step, params: Mapping[str, object]) -> None:
        store.set_roles(params["ids"], operation_id=step.operation_id)

    async def set_channel(step: Step, params: Mapping[str, object]) -> Mapping[str, object] | None:
        current = guild()
        channel = current.get_channel(params["channel_id"])
        if problem := text_channel_problem(channel, current):
            return failure("channel", problem)
        if problem := await bind_channel(current, operations, channel, operation_id=step.operation_id,
                                         still_current=step.still_current):
            return failure("publish", problem)
        return None

    async def publish(step: Step, params: Mapping[str, object]) -> Mapping[str, object] | None:
        if problem := await operations.publish_hub(guild(), operation_id=step.operation_id, still_current=step.still_current):
            return failure("publish", problem)
        return None

    table = ActionGroup("game", (
        ActionSpec("open_table", {"game": Param("enum", choices=tuple(TABLES))},
                   admin=False, effect=play("open_table")),
        *(ActionSpec(name, {}, admin=False, effect=play(name)) for name in ("deal", "again", "hit", "stand", "leave_table")),
    ), hint=HINT, exclusive=True, available=available, snapshot=snapshot, busy=BUSY)
    settings = ActionGroup("games", (
        ActionSpec("games_roles", {"ids": Param("ids", target=REGULAR_ROLES)}, admin=True, effect=Configure(set_roles)),
        ActionSpec("games_channel", {"channel_id": Param("id", target=TEXT_CHANNEL)}, admin=True, effect=Network(set_channel)),
        ActionSpec("publish_hub", {}, admin=True, effect=Network(publish)),
    ))
    return table, settings

