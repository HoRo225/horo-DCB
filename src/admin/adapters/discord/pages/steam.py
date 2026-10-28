from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord
from src.core.adapters.discord.channels import interaction_operation_id, text_channel_problem

from src.admin.adapters.discord.pages.common import (
    status_text,
    bind_channel,
    channel_select,
    feature_button,
    footer_note,
    no_guild_note,
    role_select,
    spacer,
)
from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS
from src.steam import SteamConfigurationError, SteamFetchResult, SteamStatus

if TYPE_CHECKING:
    from src.admin.adapters.discord.panel import AdminPanelView


MAX_STEAM_OFFERS_SHOWN = 3


@dataclass(slots=True)
class SteamQuery:
    result: SteamFetchResult | None = None
    error: str | None = None
    fetched_at: int | None = None


@dataclass(frozen=True, slots=True)
class _SteamState:
    status: SteamStatus
    label: str
    summary: str
    channel: str
    role_status: str
    next_step: str | None

    @property
    def poll(self) -> str:
        if self.label == "停用":
            return "已停止自動檢查"
        return f"每 {int(self.status.poll_interval_seconds // 60)} 分鐘檢查"


def steam_notification_problem(view: AdminPanelView, status: SteamStatus) -> str | None:
    if not view.services.steam.enabled:
        return None
    guild = view.services.live.guild()
    if guild is None:
        return None
    if status.channel_id is None:
        return None
    return view.services.steam.notification_problem()


def steam_state(view: AdminPanelView) -> _SteamState:
    status = view.services.steam.status()
    problem = steam_notification_problem(view, status)
    if not view.services.steam.enabled:
        label, detail = "停用", "自動通知未開啟"
        channel, role_status, next_step = "手動查詢仍可使用", "身分組通知未開啟", None
    else:
        service_problem = status.last_error or (
            "背景檢查已停止，請重新開啟 Steam 自動通知。" if not status.running
            else "背景檢查正在啟動，尚未完成首次檢查。" if status.last_success is None
            else f"有 {status.pending_count} 則通知待處理。" if status.pending_count else None
        )
        label = "需要處理" if status.channel_id is None or problem or service_problem else "狀態正常"
        detail = "通知頻道尚未綁定" if status.channel_id is None else problem or service_problem or ""
        channel = f"通知 <#{status.channel_id}>" if status.channel_id else "尚未綁定通知頻道"
        role_status = (
            "通知身分組 " + " ".join(f"<@&{role_id}>" for role_id in status.role_ids)
            if status.role_ids
            else "未設定通知身分組"
        )
        next_step = problem or (
            "請在下方選擇通知頻道。"
            if status.channel_id is None
            else service_problem
        )
    summary = detail or f"{channel} · {status.active_app_count} 款活動中"
    return _SteamState(status, label, summary, channel, role_status, next_step)


def render_steam(view: AdminPanelView, note: tuple[str, str] | None = None) -> None:
    query = view.state(SteamQuery)
    result = query.result
    error = query.error
    steam = steam_state(view)
    status = steam.status
    checked = f"上次成功檢查 <t:{status.last_success}:R>" if status.last_success else "尚無成功檢查紀錄"
    children = view.header("Steam 免費遊戲", "steam")
    children.append(
        discord.ui.Section(
            "### 通知狀態\n"
            f"{status_text(steam.label)} · **{status.active_app_count} 款活動中**"
            f" · {steam.poll}\n"
            f"{steam.channel}\n"
            f"{steam.role_status}\n"
            f"{checked} · {status.pending_count} 則通知待處理",
            accessory=feature_button(
                view,
                "steam_free_games",
                "Steam 自動通知",
                owner=view.services.steam,
                render=render_steam,
            ),
        )
    )
    children.append(
        discord.ui.ActionRow(
            channel_select(
                view,
                "steam_channel",
                "選擇 Steam 通知頻道",
                status.channel_id,
                lambda i, channel: bind_channel(
                    view,
                    i,
                    lambda guild, current: view.services.steam.bind_channel(
                        None if text_channel_problem(channel, guild) else channel.id,
                        still_current=current, operation_id=interaction_operation_id(i),
                    ),
                    render_steam,
                    "已綁定 Steam 免費遊戲通知頻道。",
                ),
            )
        )
    )
    children.append(
        discord.ui.ActionRow(
            role_select(
                view,
                "steam_role",
                "選擇通知身分組（清空即取消）",
                status.role_ids,
                lambda i, roles: handle_steam_role_select(view, i, roles),
                disabled=not view.services.steam.enabled,
            )
        )
    )

    children.append(spacer())
    if error is not None:
        children.append(
            discord.ui.TextDisplay(f"### 查詢結果\n{discord.utils.escape_markdown(error)}")
        )
    elif result is None:
        children.append(
            discord.ui.TextDisplay(
                "### 查詢結果\n按「重新查詢」取得目前符合條件的限時免費遊戲。"
            )
        )
    else:
        offers = result.offers[:MAX_STEAM_OFFERS_SHOWN]
        query_time = f"\n**查詢時間**　<t:{query.fetched_at}:T>"
        if result.failed_app_count > 0:
            children.append(
                discord.ui.TextDisplay(
                    "### 查詢結果"
                    f"{query_time}\n"
                    f"資料不完整：{result.failed_app_count} 款遊戲詳細資料無法取得；"
                    "請稍後再查詢。"
                )
            )
        elif not offers:
            children.append(
                discord.ui.TextDisplay(
                    f"### 查詢結果{query_time}\n目前沒有符合條件的限時免費遊戲。"
                )
            )
        else:
            children.append(discord.ui.TextDisplay(f"### 查詢結果{query_time}"))
        if offers:
            children.extend(view.services.offer_summary(offer) for offer in offers)
            if len(result.offers) > len(offers):
                children.append(
                    discord.ui.TextDisplay(
                        f"只顯示前 {len(offers)} 款，共 {len(result.offers)} 款。"
                    )
                )

    children.extend(
        (
            discord.ui.Separator(),
            *footer_note(note),
            view.actions(
                view.button(
                    "steam_query",
                    "重新查詢",
                    on_click=lambda i: query_steam(view, i),
                    style=discord.ButtonStyle.primary,
                    emoji=ICONS["query"],
                ),
                view.button(
                    "steam_effects",
                    f"待處理項目（{len(view.services.effects.pending())}）",
                    on_click=lambda i: view.open_page(i, "effects"),
                    emoji=ICONS["warn"],
                ),
            ),
        )
    )
    view.set_container(*children, colour=BRAND_COLOUR, note=note, problem=steam.next_step)


async def handle_steam_role_select(
    view: AdminPanelView,
    interaction: discord.Interaction,
    roles: tuple[discord.Role, ...],
) -> None:
    async def work(operation: int) -> Callable[[], None]:
        guild = view.services.live.guild()
        had_roles = bool(view.services.steam.status().role_ids)
        if not view.services.steam.enabled:
            note = ("info", "Steam 自動通知未開啟，未修改身分組設定。")
        elif roles and guild is None:
            note = no_guild_note()
        else:
            try:
                await view.services.steam.set_roles(
                    [role.id for role in roles],
                    still_current=lambda: view.is_current(operation) and view.services.live.guild() is guild,
                    operation_id=interaction_operation_id(interaction),
                )
            except SteamConfigurationError as exc:
                note = ("warn", str(exc))
            else:
                if roles:
                    note = ("ok", f"已更新 Steam 免費遊戲通知身分組，共 {len(roles)} 個。")
                elif had_roles:
                    note = ("ok", "已取消 Steam 免費遊戲通知身分組。")
                else:
                    note = ("info", "目前沒有設定 Steam 通知身分組。")
        return lambda: render_steam(view, note)

    await view.run(interaction, work)


async def query_steam(view: AdminPanelView, interaction: discord.Interaction) -> None:
    async def work(_operation: int) -> Callable[[], None]:
        try:
            result = await view.services.steam.fetch_current_offers()
        except Exception:
            logging.exception("Admin panel Steam free games query failed.")
            result = None

        def render() -> None:
            query = view.state(SteamQuery)
            query.result = result
            query.error = None if result is not None else "目前無法取得 Steam 資料。"
            query.fetched_at = int(time.time()) if result is not None else None
            render_steam(view)

        return render

    await view.run(interaction, work)
