from __future__ import annotations

from collections.abc import Mapping

import discord

from src.core.adapters.discord.channels import text_channel_problem
from src.core.adapters.discord.live import LiveGuild
from src.core.app.actions import ActionGroup, ActionSpec, Configure, Network, Play as PlayEffect, Step
from src.core.app.ports import Actor
from src.core.domain.actions import REGULAR_ROLES, TEXT_CHANNEL, Param, failure
from src.games.app.lobby import Lobby
from src.games.app.play import Move, Play
from src.games.domain.catalog import GAMES

HINT = "open_table 使用 blackjack（21 點）或 slots（拉霸），牌局不下注；deal 開新局、again 再來一局、hit 叫牌、stand 停牌、leave_table 離桌。"
BUSY = "收到訊息時賭桌仍在處理上一個動作，請等完成後再要求下一局。"


def game_actions(play: Play, lobby: Lobby, live: LiveGuild) -> tuple[ActionGroup, ActionGroup]:
    def act(name: str) -> PlayEffect:
        async def run(step: Step, params: Mapping[str, object]) -> Mapping[str, object]:
            source = step.source
            return await play.execute(Move(
                name, step.actor.user_id, source.guild_id, source.channel_id, source.message_id,
                step.received["game"].get("state_token"), game=params.get("game"), operation_id=step.operation_id,
                still_current=step.still_current,
            ))
        return PlayEffect(run)

    def available(actor: Actor, channel_id: int) -> str | None:
        return lobby.problem(live.guild_id, channel_id, actor.role_ids)

    def snapshot(user_id: int | None) -> Mapping[str, object]:
        return play.snapshot(user_id) if user_id is not None else {"state_token": None, "busy": True}

    def guild() -> discord.Guild:
        if (current := live.guild()) is None:
            raise LookupError("current guild is unavailable")
        return current

    async def set_roles(step: Step, params: Mapping[str, object]) -> None:
        lobby.set_roles(params["ids"], operation_id=step.operation_id)

    async def set_channel(step: Step, params: Mapping[str, object]) -> Mapping[str, object] | None:
        current = guild()
        channel = current.get_channel(params["channel_id"])
        if problem := text_channel_problem(channel, current):
            return failure("channel", problem)
        if problem := await lobby.bind_channel(channel.id, operation_id=step.operation_id, still_current=step.still_current):
            return failure("publish", problem)
        return None

    async def publish(step: Step, params: Mapping[str, object]) -> Mapping[str, object] | None:
        guild()
        if problem := await lobby.publish_hub(operation_id=step.operation_id, still_current=step.still_current):
            return failure("publish", problem)
        return None

    table = ActionGroup("game", (
        ActionSpec("open_table", {"game": Param("enum", choices=tuple(GAMES))}, admin=False, effect=act("open_table")),
        *(ActionSpec(name, {}, admin=False, effect=act(name)) for name in ("deal", "again", "hit", "stand", "leave_table")),
    ), hint=HINT, exclusive=True, available=available, snapshot=snapshot, busy=BUSY)
    settings = ActionGroup("games", (
        ActionSpec("games_roles", {"ids": Param("ids", target=REGULAR_ROLES)}, admin=True, effect=Configure(set_roles)),
        ActionSpec("games_channel", {"channel_id": Param("id", target=TEXT_CHANNEL)}, admin=True, effect=Network(set_channel)),
        ActionSpec("publish_hub", {}, admin=True, effect=Network(publish)),
    ))
    return table, settings
