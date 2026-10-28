from __future__ import annotations

from typing import TYPE_CHECKING

import discord

from src.admin.pages.ai import AiReadings, ai_state, rate_period_label, rate_visible
from src.admin.pages.common import IDLE_TIMEOUT_SECONDS, status_text
from src.admin.pages.games import games_state
from src.admin.pages.steam import steam_state
from src.admin.pages.voice import voice_state
from src.core.adapters.discord.brand import ICONS, bar

if TYPE_CHECKING:
    from src.admin.panel import AdminPanelView


def rate_bars(view: AdminPanelView) -> str:
    limits = view.state(AiReadings).rate_limits
    lines = []
    for window in limits.windows:
        remaining = max(0, 100 - window.used_percent)
        label = rate_period_label(window.window_minutes, window.slot)
        lines.append(f"**{label}**　{bar(remaining / 100)} 剩餘 {remaining:g}%")
    if limits.fetched_at is None or not lines:
        return "額度暫時無法取得"
    return "\n".join(lines)


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
    subtitle = " · ".join(
        (
            f"{len(rows)} 項功能",
            *(counts or ["狀態正常"]),
            f"閒置 {IDLE_TIMEOUT_SECONDS / 60:.0f} 分鐘後自動關閉",
        )
    )
    children = view.header("管理控制台", "overview", subtitle=subtitle)
    if rate_visible(view):
        children.insert(1, discord.ui.TextDisplay(rate_bars(view)))
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
    pending = len(view.effects.pending())
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
