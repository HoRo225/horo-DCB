from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from src.admin.adapters.discord.pages.ai import AiReadings, ai_state, rate_period_label, rate_visible
from src.admin.adapters.discord.pages.common import IDLE_TIMEOUT_SECONDS, status_text
from src.admin.adapters.discord.pages.games import games_state
from src.admin.adapters.discord.pages.steam import steam_state
from src.admin.adapters.discord.pages.voice import voice_state
from src.core.adapters.discord.brand import ICONS

if TYPE_CHECKING:
    from src.admin.adapters.discord.panel import AdminPanelView


def rate_line(view: AdminPanelView) -> str:
    limits = view.state(AiReadings).rate_limits
    parts = [
        f"{rate_period_label(window.window_minutes, window.slot)} {max(0, 100 - window.used_percent):g}%"
        for window in limits.windows
    ]
    if limits.fetched_at is None or not parts:
        return "**額度**　暫時無法取得"
    return f"**額度剩餘**　{' · '.join(parts)}"


def render_overview(view: AdminPanelView) -> None:
    rows = tuple(
        (key, name, state.label, state.summary)
        for key, name, state in (
            ("ai", view.bot_name_md, ai_state(view)),
            ("voice", "臨時語音", voice_state(view)),
            ("steam", "Steam 免費遊戲", steam_state(view)),
            ("games", "遊戲大廳", games_state(view)),
        )
    )
    counts = [
        f"{count} 個{state}"
        for state in ("需要處理", "停用")
        if (count := sum(row[2] == state for row in rows))
    ]
    backed_up = view.services.last_backup()
    subtitle = " · ".join(
        (
            f"{len(rows)} 項功能",
            *(counts or ["狀態正常"]),
            "尚未備份" if backed_up is None else f"上次備份 <t:{backed_up}:R>",
            f"閒置 {IDLE_TIMEOUT_SECONDS / 60:.0f} 分鐘後自動關閉",
        )
    )
    children = view.header("管理控制台", "overview", subtitle=subtitle)
    if rate_visible(view):
        children.insert(1, discord.ui.TextDisplay(rate_line(view)))
    for key, name, label, detail in rows:
        detail = detail.removesuffix("。")
        children.append(
            discord.ui.Section(
                f"{status_text(label)} · **{ICONS[key]} {name}**\n{detail}",
                accessory=view.button(
                    f"manage_{key}",
                    "管理",
                    on_click=lambda interaction, key=key: view.open_page(interaction, key),
                    emoji=ICONS["manage"],
                ),
            )
        )
    pending = len(view.services.effects.pending())
    attention = "、".join(name for _, name, label, _ in rows if label == "需要處理")
    problems = [f"{attention}需要處理，請按「管理」查看"] if attention else []
    if pending:
        problems.append(f"有 {pending} 項送出結果待核對，請按「待處理項目」")
    children.append(
        view.actions(
            view.button(
                "manage_effects",
                f"待處理項目（{pending}）",
                on_click=lambda interaction: view.open_page(interaction, "effects"),
                style=discord.ButtonStyle.primary if pending else discord.ButtonStyle.secondary,
                disabled=not pending,
                emoji=ICONS["warn"],
            )
        )
    )
    view.set_container(*children, problem="；".join(problems))
