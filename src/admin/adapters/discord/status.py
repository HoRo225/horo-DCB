from __future__ import annotations

from collections.abc import Callable, Mapping

import discord

from src.core.adapters.discord.brand import ICONS, card_title, info_card
from src.core.app.actions import ActionGroup, ActionSpec, Query
from src.core.app.ports import Actor


def admin_actions(
    ai_enabled: Callable[[], bool], voice_enabled: Callable[[], bool], steam_running: Callable[[], bool],
    pending: Callable[[], int], lobby_problem: Callable[[], str | None],
) -> ActionGroup:
    def lobby() -> str:
        problem = lobby_problem()
        return f"需要處理（{problem.removesuffix('。')}）" if problem else "正常"

    async def status(actor: Actor) -> Mapping[str, object]:
        return {"view": info_card(card_title(f"{ICONS['status']} 系統狀態"), discord.ui.TextDisplay(
                    f"AI：{'開啟' if ai_enabled() else '停用'}\n"
                    f"語音：{'開啟' if voice_enabled() else '停用'}\n"
                    f"Steam：{'執行中' if steam_running() else '已停止'}\n"
                    f"遊戲大廳：{lobby()}\n"
                    f"待處理：{pending()} 項")), "attachments": []}

    return ActionGroup("admin", (ActionSpec("admin_status", {}, admin=True, effect=Query("管理狀態", status), label="查詢管理狀態"),))
