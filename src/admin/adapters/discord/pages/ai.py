from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from src.admin.adapters.discord.pages.common import (
    PendingConfirm,
    status_text,
    defaults,
    feature_section,
    footer_note,
    mentions,
    role_select,
    spacer,
)
from src.admin.adapters.discord.codex_login import account_items
from src.admin.adapters.discord.ports import CodexAccount
from src.ai import MAX_CODEX_ALLOWED_CHANNELS, RateLimits, RuntimeStatus
from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, bar
from src.core.adapters.discord.channels import (
    VIEW_AND_SEND,
    is_text_channel,
    member_role_ids,
    missing_channel_permissions,
    interaction_operation_id,
)

if TYPE_CHECKING:
    from src.admin.adapters.discord.panel import AdminPanelView


@dataclass(frozen=True, slots=True)
class _AiState:
    label: str
    summary: str
    next_step: str
    stale_counts: tuple[int | None, int | None]
    permission_detail: str | None


@dataclass(slots=True)
class AiReadings:
    codex_status: RuntimeStatus = RuntimeStatus()
    rate_limits: RateLimits = RateLimits()


def ai_display(view: AdminPanelView) -> tuple[str, str]:
    status = view.state(AiReadings).codex_status
    plan = status.plan.replace("_", " ").title() if status.plan else "未提供"
    runtime = (
        discord.utils.escape_markdown(status.runtime_version)
        if status.runtime_version
        else "未提供"
    )
    return plan, runtime


def rate_visible(view: AdminPanelView) -> bool:
    return view.services.ai.enabled


async def load_codex_status(client: CodexAccount) -> RuntimeStatus:
    try:
        return await client.get_runtime_status()
    except Exception:
        logging.exception("Admin panel failed to read Codex status.")
        return RuntimeStatus()


async def load_codex_rate_limits(client: CodexAccount) -> RateLimits:
    try:
        return await client.get_rate_limits()
    except Exception:
        logging.exception("Admin panel failed to read Codex rate limits.")
        return RateLimits(error="unavailable")


async def load_ai(view: AdminPanelView, operation: int) -> bool:
    if not view.is_current(operation):
        logging.info("Admin panel skipped AI status update before read.")
        return False
    status = view.spawn(load_codex_status(view.services.codex))
    limits = view.spawn(load_codex_rate_limits(view.services.codex)) if rate_visible(view) else None
    await asyncio.gather(*(task for task in (status, limits) if task is not None))
    if not view.is_current(operation):
        logging.info("Admin panel skipped AI status update after read.")
        return False
    readings = view.state(AiReadings)
    readings.codex_status = status.result()
    if limits is not None:
        readings.rate_limits = limits.result()
    return True


def rate_period_label(minutes: int | None, slot: str) -> str:
    if minutes == 300:
        return "5 小時"
    if minutes == 10080:
        return "每週"
    if minutes == 43200:
        return "30 天"
    if minutes is None:
        return f"週期未提供（{slot}）"
    return f"{minutes} 分鐘"


def rate_text(view: AdminPanelView) -> str:
    lines = ["### 帳號額度"]
    limits = view.state(AiReadings).rate_limits
    if limits.fetched_at is None:
        lines.extend(("**暫時無法取得**", "上游未提供可驗證的額度快照"))
    else:
        for window in limits.windows:
            remaining = max(0, 100 - window.used_percent)
            label = rate_period_label(window.window_minutes, window.slot)
            lines.append(f"**{label}**　{bar(remaining / 100)} 剩餘 {remaining:g}%")
            if window.resets_at is None:
                lines.append("**重設時間**　未提供")
            elif window.resets_at <= time.time():
                lines.append(f"**重設時間**　<t:{window.resets_at}:f> · 等待上游更新")
            else:
                lines.append(f"**重設時間**　<t:{window.resets_at}:f>")
        lines.append(f"**額度快照**　<t:{limits.fetched_at}:T>")
    return "\n".join(lines)


def rate_item(view: AdminPanelView) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(rate_text(view))


def codex_stale_allowlist_counts(view: AdminPanelView) -> tuple[int | None, int | None]:
    access = view.services.ai
    if not access.enabled:
        return None, None
    guild = view.services.live.guild()
    if guild is None:
        return None, None

    def stale_count(ids: frozenset[int], getter: Callable[[int], object | None]) -> int:
        return sum(getter(entity_id) is None for entity_id in ids)

    return (
        stale_count(access.channel_ids, guild.get_channel),
        stale_count(access.role_ids, guild.get_role),
    )


def codex_stale_detail(
    counts: tuple[int | None, int | None],
) -> str | None:
    stale_channels, stale_roles = counts
    details = []
    if stale_channels:
        details.append(f"{stale_channels} 個白名單頻道已不存在")
    if stale_roles:
        details.append(f"{stale_roles} 個白名單身分組已不存在")
    return f"{'；'.join(details)}，請重新選擇。" if details else None


def codex_channel_permission_detail(view: AdminPanelView) -> str | None:
    access = view.services.ai
    if not access.enabled or not access.channel_ids:
        return None
    guild = view.services.live.guild()
    if guild is None:
        return None
    bot_member = guild.me
    if bot_member is None:
        return None

    unusable = 0
    for channel_id in access.channel_ids:
        channel = guild.get_channel(channel_id)
        if not is_text_channel(channel):
            unusable += 1
            continue
        if missing_channel_permissions(
            channel,
            bot_member,
            VIEW_AND_SEND,
        ):
            unusable += 1
    if unusable:
        return (
            view.bot_text(f"{unusable} 個", "白名單頻道不存在、類型不支援或缺少 Bot 權限，")
            + "請重新選擇頻道或調整權限。"
        )
    return None


def ai_state(view: AdminPanelView) -> _AiState:
    access = view.services.ai
    status = view.state(AiReadings).codex_status
    stale_counts = codex_stale_allowlist_counts(view)
    permission_detail = codex_channel_permission_detail(view)
    allowlist_detail = codex_stale_detail(stale_counts) or permission_detail
    next_step = None
    if not access.enabled:
        label, detail = "停用", "未開啟"
    elif not access.channel_ids:
        label, detail = "需要處理", "白名單頻道尚未設定"
    elif not access.configured:
        label, detail = "需要處理", "白名單身分組尚未設定"
    elif allowlist_detail is not None:
        label, detail = "需要處理", allowlist_detail
    elif status.reason == "unresponsive":
        label, detail = "需要處理", "codex 無回應"
        next_step = "codex 無回應，請手動重啟 codex 容器後再重新整理。"
    elif status.reason == "unavailable":
        label, detail = "需要處理", "codex 無法連線"
        next_step = "請確認 codex 服務正在執行且連線 token 設定正確，再重新整理。"
    elif not status.ready:
        label, detail = (
            "需要處理",
            {
                "connecting": view.bot_text(after="服務連線中"),
                "auth_required": "Codex 尚未登入",
                "draining": view.bot_text(after="服務正在收尾"),
            }.get(status.reason, view.bot_text(after="服務暫時不可用")),
        )
    else:
        label, detail = "狀態正常", ""
    authenticated = "已登入" if status.authenticated else "未登入"
    summary = detail or f"{ai_display(view)[0]} · {authenticated}"
    return _AiState(label, summary, next_step or detail, stale_counts, permission_detail)


def render_ai(view: AdminPanelView, note: tuple[str, str] | None = None) -> None:
    ai = ai_state(view)
    status = view.state(AiReadings).codex_status
    plan = ai_display(view)[0]
    authenticated = "已登入" if status.authenticated else "未登入"
    children = view.header(view.bot_name_md, "ai")
    children.append(
        feature_section(
            view,
            "### 目前狀態",
            f"{status_text(ai.label)} · **{plan} · {authenticated}**",
            "ai",
            "{name}",
            owner=view.services.ai,
            render=render_ai,
            after=_after_ai_toggle,
            confirm_off=lambda view: (
                view.bot_text("確認關閉"),
                view.bot_text("關閉後會立即中斷所有進行中的", "請求，成員也無法再使用，直到重新開啟。"),
            ),
        )
    )
    if code := status.last_error:
        children.append(
            discord.ui.TextDisplay(
                f"### {ICONS['warn']} 最近錯誤\n"
                f"{view.services.codex_error(code, view.bot_name)}\n**錯誤代碼**　`{code}`"
            )
        )
    children.extend((spacer(), *account_items(
        view, status, lambda note: render_ai(view, note), lambda operation: load_ai(view, operation),
    )))
    if rate_visible(view):
        children.extend((spacer(), rate_item(view)))
    children.extend(
        (
            discord.ui.Separator(),
            *footer_note(note),
            view.actions(),
        )
    )
    view.set_container(
        *children,
        colour=BRAND_COLOUR,
        note=note,
        problem=ai.next_step if ai.label == "需要處理" else None,
    )


async def _after_ai_toggle(
    view: AdminPanelView, _interaction: discord.Interaction, operation: int, enabled: bool, stopped: object
) -> tuple[str, str] | None:
    note = None if stopped else ("warn", "部分進行中的請求未能即時停止。")
    if not await load_ai(view, operation):
        logging.warning("AI status refresh after toggle failed; showing previous status.")
    return note


def access_note(result: str, roles: bool, count: int) -> tuple[str, str]:
    label = "身分組" if roles else "頻道"
    if result == "unchanged":
        return "info", f"目前已設定 {count} 個白名單{label}。"
    return "ok", f"已更新 {count} 個白名單{label}。"


def codex_channel_select(view: AdminPanelView, channel_ids: frozenset[int]) -> discord.ui.ChannelSelect:
    item = view.control(
        ("select", "codex_channel"),
        lambda: discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=MAX_CODEX_ALLOWED_CHANNELS,
        ),
        lambda item, interaction: handle_codex_allowlist(
            view, interaction, tuple(value.resolve() for value in item.values), roles=False
        ),
    )
    item.placeholder = view.bot_text("選擇", "白名單文字頻道（可複選）", markdown=False)
    item.default_values = defaults(discord.SelectDefaultValue.from_channel, sorted(channel_ids))
    return item


async def handle_codex_allowlist(
    view: AdminPanelView,
    interaction: discord.Interaction,
    items: tuple[object | None, ...],
    *,
    roles: bool,
) -> None:
    async def work(operation: int) -> Callable[[], None]:
        guild_id = interaction.guild_id
        rejected = ("warn", f"只能選擇目前伺服器的一般{'身分組' if roles else '文字頻道'}。")
        if guild_id != view.guild_id or any(
            getattr(getattr(item, "guild", None), "id", None) != guild_id
            or (item.is_default() if roles else not is_text_channel(item))
            for item in items
        ):
            return lambda: render_ai_access(view, rejected)
        selected = frozenset(getattr(item, "id", None) for item in items)
        guild = view.services.live.guild()
        def current() -> bool:
            if not view.is_current(operation) or guild is None or view.services.live.guild() is not guild:
                return False
            return all(
                (guild.get_role(item_id) is not None) if roles else is_text_channel(guild.get_channel(item_id))
                for item_id in selected
            )

        if not current():
            return lambda: render_ai_access(view, rejected)
        try:
            result = await view.services.access.change(
                guild_id,
                selected,
                roles=roles,
                still_current=current,
                operation_id=interaction_operation_id(interaction),
            )
        except ValueError:
            return lambda: render_ai_access(view, rejected)
        note = access_note(result, roles, len(selected))
        if roles and not selected and result == "updated":
            note = (
                "ok",
                view.bot_text("已清除白名單身分組，目前沒有人可以使用", "。", markdown=False),
            )
        return lambda: render_ai_access(view, note)

    await view.run(interaction, work)


async def pick_codex_roles(
    view: AdminPanelView, interaction: discord.Interaction, roles: tuple[discord.Role, ...]
) -> None:
    if roles or not view.services.ai.role_ids:
        await handle_codex_allowlist(view, interaction, roles, roles=True)
        return
    await view.ask(interaction, PendingConfirm(
        view.bot_text("確認清空", "白名單身分組"),
        view.bot_text("清空後沒有任何人可以使用", "，直到重新選擇身分組。"),
        "確認清空",
        lambda i: handle_codex_allowlist(view, i, (), roles=True),
    ))


def render_ai_access(view: AdminPanelView, note: tuple[str, str] | None = None) -> None:
    ai = ai_state(view)
    stale_channel_count, stale_role_count = ai.stale_counts
    permission_detail = ai.permission_detail
    channel_ids = view.services.ai.channel_ids
    role_ids = view.services.ai.role_ids
    if not channel_ids:
        channel_detail = view.bot_text("請選擇可使用", "的文字頻道")
    else:
        channel_detail = view.bot_text("僅所選頻道及其討論串可使用")
    role_detail = "擁有任一所選身分組即可使用" if role_ids else "請選擇白名單身分組；目前尚未開放"
    if stale_channel_count:
        channel_detail += f"；{stale_channel_count} 個白名單頻道已不存在，請重新選擇"
    if permission_detail:
        channel_detail += f"；{permission_detail}"
    if stale_role_count:
        role_detail += f"；{stale_role_count} 個白名單身分組已不存在，請重新選擇"
    current_allowed = (
        view.services.ai.denial_reason(
            view.guild_id, next(iter(channel_ids), None), member_role_ids(view.interaction_user)
        )
        is None
    )
    children = view.header(view.bot_text(after="使用權限"), "ai_access")
    children.extend(
        (
            discord.ui.TextDisplay(
                f"### 白名單頻道 · {len(channel_ids)} 個\n"
                + (f"{mentions(view, channel_ids)}\n" if channel_ids else "")
                + channel_detail
            ),
            discord.ui.ActionRow(codex_channel_select(view, channel_ids)),
            spacer(),
            discord.ui.TextDisplay(
                f"### 白名單身分組 · {len(role_ids)} 個\n"
                + (f"{mentions(view, role_ids, role=True)}\n" if role_ids else "")
                + f"{role_detail} · 目前操作者："
                f"{'已允許' if current_allowed else '未允許'}"
            ),
            discord.ui.ActionRow(
                role_select(
                    view,
                    "codex_role",
                    "選擇白名單身分組（清空即暫停）",
                    role_ids,
                    lambda i, roles: pick_codex_roles(view, i, roles),
                    disabled=not channel_ids,
                )
            ),
            discord.ui.Separator(),
            *footer_note(note),
            view.actions(),
        )
    )
    view.set_container(*children, colour=BRAND_COLOUR, note=note)


def render_ai_tech(view: AdminPanelView) -> None:
    plan, runtime = ai_display(view)
    status = view.state(AiReadings).codex_status
    bot_active, bot_queued = view.services.codex.request_counts()
    children = view.header(view.bot_text(after="技術資訊"), "ai_tech")
    children.extend(
        (
            discord.ui.TextDisplay(
                f"### 執行環境\n**{plan} · Runtime {runtime}**\nWeb Search Live"
            ),
            spacer(),
            discord.ui.TextDisplay(
                "### 工作與對話\n"
                f"**持久討論串**　{status.thread_count} 條\n"
                f"**請求**　執行 {bot_active} · 等待 {bot_queued}"
            ),
            spacer(),
            discord.ui.TextDisplay("### 安全邊界\n唯讀；指令列、MCP、應用程式、外掛、子代理與全域記憶都停用"),
            discord.ui.Separator(),
            view.actions(),
        )
    )
    view.set_container(*children, colour=BRAND_COLOUR)
