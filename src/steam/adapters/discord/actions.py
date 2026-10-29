from __future__ import annotations

from collections.abc import Mapping

import discord

from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, WARN_COLOUR, card_title, info_card
from src.core.adapters.discord.channels import text_channel_problem
from src.core.adapters.discord.live import LiveGuild
from src.core.app.actions import ActionGroup, ActionSpec, Configure, Query, Step
from src.core.app.ports import Actor
from src.core.domain.actions import REGULAR_ROLES, TEXT_CHANNEL, Param, failure
from src.steam.adapters.discord.views import offer_summary
from src.steam.app.steam import Steam

CHANNEL_STALE = "操作權限或期限已改變，未更新 Steam 頻道。"
ROLES_STALE = "操作權限或期限已改變，未更新 Steam 身分組。"


def steam_actions(steam: Steam, live: LiveGuild) -> ActionGroup:
    def guild() -> discord.Guild:
        if (current := live.guild()) is None:
            raise LookupError("current guild is unavailable")
        return current

    async def set_enabled(step: Step, params: Mapping[str, object]) -> None:
        steam.set_enabled(params["enabled"], operation_id=step.operation_id)

    async def bind_channel(step: Step, params: Mapping[str, object]) -> Mapping[str, object] | None:
        current = guild()
        channel = current.get_channel(params["channel_id"])
        chosen = None if text_channel_problem(channel, current) else channel.id
        if problem := await steam.bind_channel(chosen, still_current=step.still_current, operation_id=step.operation_id):
            return failure("channel", problem)
        return None

    async def set_roles(step: Step, params: Mapping[str, object]) -> None:
        await steam.set_roles(params["ids"], still_current=step.still_current, operation_id=step.operation_id)

    async def offers(actor: Actor) -> Mapping[str, object]:
        result = await steam.fetch_current_offers()
        body =[offer_summary(offer) for offer in result.offers[:3]] if result else []
        failed = result is None or result.failed_app_count > 0
        if not body:
            body = [discord.ui.TextDisplay("目前無法取得 Steam 資料，請稍後再試。" if failed else "目前沒有符合條件的遊戲。")]
        elif failed:
            body.append(discord.ui.TextDisplay(
                f"{ICONS['warn']} 資料不完整：{result.failed_app_count} 款遊戲詳細資料無法取得，結果可能有缺漏。"))
        view = info_card(card_title(f"{ICONS['steam']} Steam 免費遊戲"), *body,
                         colour=WARN_COLOUR if failed else BRAND_COLOUR)
        return {"view": view, "attachments": []}

    return ActionGroup("steam", (
        ActionSpec("steam_enabled", {"enabled": Param("bool")}, admin=True, effect=Configure(set_enabled),
                   label=lambda params: "開啟 Steam 自動通知" if params["enabled"] else "關閉 Steam 自動通知"),
        ActionSpec("steam_channel", {"channel_id": Param("id", target=TEXT_CHANNEL)}, admin=True,
                   effect=Configure(bind_channel, stale=CHANNEL_STALE), label="設定 Steam 通知頻道"),
        ActionSpec("steam_roles", {"ids": Param("ids", target=REGULAR_ROLES)}, admin=True,
                   effect=Configure(set_roles, stale=ROLES_STALE), label="設定 Steam 通知身分組"),
        ActionSpec("steam_query", {}, admin=True, effect=Query("Steam 免費遊戲", offers), label="查詢 Steam 免費遊戲"),
    ))
