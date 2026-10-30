from __future__ import annotations

import logging
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
from src.admin.adapters.discord.ports import AiRuntime
from src.ai import MAX_AI_ALLOWED_CHANNELS, RuntimeStatus
from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS
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
    status: RuntimeStatus = RuntimeStatus()


def connection_text(status: RuntimeStatus) -> str:
    if status.ready:
        return f"clewdr 已連線（{discord.utils.escape_markdown(status.version or '版本未提供')}）"
    return "clewdr 無法連線"


async def load_ai_status(model: AiRuntime) -> RuntimeStatus:
    try:
        return await model.get_runtime_status()
    except Exception:
        logging.exception("Admin panel failed to read AI status.")
        return RuntimeStatus()


async def load_ai(view: AdminPanelView, operation: int) -> bool:
    if not view.is_current(operation):
        logging.info("Admin panel skipped AI status update before read.")
        return False
    status = await load_ai_status(view.services.model)
    if not view.is_current(operation):
        logging.info("Admin panel skipped AI status update after read.")
        return False
    view.state(AiReadings).status = status
    return True


def ai_stale_allowlist_counts(view: AdminPanelView) -> tuple[int | None, int | None]:
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


def ai_stale_detail(
    counts: tuple[int | None, int | None],
) -> str | None:
    stale_channels, stale_roles = counts
    details = []
    if stale_channels:
        details.append(f"{stale_channels} 個白名單頻道已不存在")
    if stale_roles:
        details.append(f"{stale_roles} 個白名單身分組已不存在")
    return f"{'；'.join(details)}，請重新選擇。" if details else None


def ai_channel_permission_detail(view: AdminPanelView) -> str | None:
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
    status = view.state(AiReadings).status
    stale_counts = ai_stale_allowlist_counts(view)
    permission_detail = ai_channel_permission_detail(view)
    allowlist_detail = ai_stale_detail(stale_counts) or permission_detail
    next_step = None
    if not access.enabled:
        label, detail = "停用", "未開啟"
    elif not access.channel_ids:
        label, detail = "需要處理", "白名單頻道尚未設定"
    elif not access.configured:
        label, detail = "需要處理", "白名單身分組尚未設定"
    elif allowlist_detail is not None:
        label, detail = "需要處理", allowlist_detail
    elif not status.ready:
        label, detail = "需要處理", "clewdr 無法連線"
        next_step = "clewdr 無法連線，請檢查容器與 secrets/clewdr_password。"
    else:
        label, detail = "狀態正常", ""
    summary = detail or connection_text(status)
    return _AiState(label, summary, next_step or detail, stale_counts, permission_detail)


def render_ai(view: AdminPanelView, note: tuple[str, str] | None = None) -> None:
    ai = ai_state(view)
    status = view.state(AiReadings).status
    children = view.header(view.bot_name_md, "ai")
    children.append(
        feature_section(
            view,
            "### 目前狀態",
            f"{status_text(ai.label)} · **{connection_text(status)}**",
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
                f"{view.services.ai_error(code, view.bot_name)}\n**錯誤代碼**　`{code}`"
            )
        )
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


def ai_channel_select(view: AdminPanelView, channel_ids: frozenset[int]) -> discord.ui.ChannelSelect:
    item = view.control(
        ("select", "ai_channel"),
        lambda: discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=MAX_AI_ALLOWED_CHANNELS,
        ),
        lambda item, interaction: handle_ai_allowlist(
            view, interaction, tuple(value.resolve() for value in item.values), roles=False
        ),
    )
    item.placeholder = view.bot_text("選擇", "白名單文字頻道（可複選）", markdown=False)
    item.default_values = defaults(discord.SelectDefaultValue.from_channel, sorted(channel_ids))
    return item


async def handle_ai_allowlist(
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


async def pick_ai_roles(
    view: AdminPanelView, interaction: discord.Interaction, roles: tuple[discord.Role, ...]
) -> None:
    if roles or not view.services.ai.role_ids:
        await handle_ai_allowlist(view, interaction, roles, roles=True)
        return
    await view.ask(interaction, PendingConfirm(
        view.bot_text("確認清空", "白名單身分組"),
        view.bot_text("清空後沒有任何人可以使用", "，直到重新選擇身分組。"),
        "確認清空",
        lambda i: handle_ai_allowlist(view, i, (), roles=True),
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
            discord.ui.ActionRow(ai_channel_select(view, channel_ids)),
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
                    "ai_role",
                    "選擇白名單身分組（清空即暫停）",
                    role_ids,
                    lambda i, roles: pick_ai_roles(view, i, roles),
                    disabled=not channel_ids,
                )
            ),
            discord.ui.Separator(),
            *footer_note(note),
            view.actions(),
        )
    )
    view.set_container(*children, colour=BRAND_COLOUR, note=note)

