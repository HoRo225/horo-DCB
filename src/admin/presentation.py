from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from src.discord_utils import is_text_channel, missing_channel_permissions
from src.steam.notifier import (
    SteamGuildStatus,
)
from src.steam.provider import SteamFetchResult
from src.steam.views import offer_items
from src.voice.manager import TempVoiceGuildStatus

if TYPE_CHECKING:
    from src.admin.panel import AdminPanelView

MAX_STEAM_OFFERS_SHOWN = 5


STATUS_DOTS = {"狀態正常": "🟢", "需要處理": "🟠", "停用": "⚫"}


@dataclass(frozen=True, slots=True)
class _VoiceState:
    status: TempVoiceGuildStatus
    problem: str | None
    label: str
    detail: str
    entry: str
    next_step: str | None


@dataclass(frozen=True, slots=True)
class _SteamState:
    status: SteamGuildStatus
    problem: str | None
    label: str
    detail: str
    channel: str
    role_status: str
    next_step: str | None


@dataclass(frozen=True, slots=True)
class _AiState:
    label: str
    detail: str
    allowlist_detail: str | None
    stale_counts: tuple[int | None, int | None]
    permission_detail: str | None


def _footer_note(note: str | None) -> tuple[discord.ui.Item, ...]:
    if not note:
        return ()
    return (discord.ui.TextDisplay(f"-# {discord.utils.escape_markdown(note)}"),)


def detail(title: str, message: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(f"## {title}\n-# {message}")


def ai_display(view: AdminPanelView) -> tuple[str, str, str]:
    plan = view.codex_status.plan.replace("_", " ").title() if view.codex_status.plan else "Unknown"
    runtime = (
        discord.utils.escape_markdown(view.codex_status.runtime_version)
        if view.codex_status.runtime_version
        else "Unknown"
    )
    search = view.codex_status.web_search.title() if view.codex_status.web_search else "Unknown"
    return plan, runtime, search


def rate_visible(view: AdminPanelView) -> bool:
    return view.codex_access.enabled and view.codex_access.guild_id == view.guild_id


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


def rate_text(view: AdminPanelView, *, stopped: bool = False, compact: bool = False) -> str:
    lines = ["## 帳號額度" if compact else "## Codex 帳號額度"]
    limits = view.codex_rate_limits
    if limits.fetched_at is None:
        lines.extend(("**暫時無法取得**", "-# 上游未提供可驗證的額度快照"))
    else:
        durations = set()
        for window in limits.windows:
            durations.add(window.window_minutes)
            used = window.used_percent
            remaining = max(0, 100 - used)
            label = rate_period_label(window.window_minutes, window.slot)
            heading = label if window.window_minutes is None else f"{label}額度"
            if compact:
                lines.append(f"**{label} · 剩餘 {remaining:g}%**")
            else:
                lines.append(f"**{heading}**　剩餘 {remaining:g}% · 已用 {used:g}%")
            if window.resets_at is None:
                lines.append("-# 重設時間未提供")
            elif window.resets_at <= time.time():
                lines.append(f"-# 重設時間 <t:{window.resets_at}:f> · 等待上游更新")
            else:
                lines.append(f"-# 重設時間 <t:{window.resets_at}:f>")
        if not compact:
            missing = []
            if 300 not in durations:
                missing.append("5 小時")
            if 10080 not in durations:
                missing.append("每週")
            if missing:
                lines.append(f"-# {'／'.join(missing)}額度：上游未提供")
        lines.append(f"-# {'額度快照' if compact else '資料取得'} <t:{limits.fetched_at}:T>")
    if stopped:
        lines.append("-# 自動更新已停止，請重新開啟 /控制台")
    elif view._rate_cutoff_at is not None and not compact:
        lines.append(f"-# 每 30 秒嘗試更新 · 自動更新至 <t:{int(view._rate_cutoff_at)}:T>")
    return "\n".join(lines)


def voice_entry_problem(
    view,
    status: TempVoiceGuildStatus,
    guild: discord.Guild | None = None,
) -> str | None:
    if not view.temp_voice_enabled:
        return None
    if guild is None:
        interaction = view._rate_interaction
        guild = interaction.guild if interaction is not None else None
    if guild is None:
        return None
    if guild.id != view.guild_id:
        return None

    if not status.state_available or status.parent_channel_id is None:
        return None
    return view.temp_voice.entry_problem(guild)


def codex_stale_allowlist_counts(view: AdminPanelView) -> tuple[int | None, int | None]:
    access = view.codex_access
    if not access.enabled or access.guild_id != view.guild_id or not access.state_available:
        return None, None
    interaction = view._rate_interaction
    guild = getattr(interaction, "guild", None) if interaction is not None else None
    if getattr(guild, "id", None) != view.guild_id or getattr(guild, "unavailable", False):
        return None, None

    def stale_count(ids: frozenset[int], getter_name: str) -> int | None:
        if not ids:
            return 0
        getter = getattr(guild, getter_name, None)
        if not callable(getter):
            return None
        return sum(getter(entity_id) is None for entity_id in ids)

    return (
        stale_count(access.channel_ids, "get_channel"),
        stale_count(access.role_ids, "get_role"),
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
    access = view.codex_access
    if (
        not access.enabled
        or access.guild_id != view.guild_id
        or not access.state_available
        or not access.channel_ids
    ):
        return None
    interaction = view._rate_interaction
    guild = getattr(interaction, "guild", None) if interaction is not None else None
    if getattr(guild, "id", None) != view.guild_id or getattr(guild, "unavailable", False):
        return None
    get_channel = getattr(guild, "get_channel", None)
    bot_member = getattr(guild, "me", None)
    if not callable(get_channel) or bot_member is None:
        return None

    unusable = 0
    for channel_id in access.channel_ids:
        channel = get_channel(channel_id)
        if not is_text_channel(channel):
            unusable += 1
            continue
        permissions_for = getattr(channel, "permissions_for", None)
        if not callable(permissions_for):
            return None
        if missing_channel_permissions(
            channel,
            bot_member,
            (("view_channel", "View Channel"), ("send_messages", "Send Messages")),
        ):
            unusable += 1
    if unusable:
        return (
            f"{unusable} 個 AI 白名單頻道不存在、類型不支援或缺少 Bot 權限，"
            "請重新選擇頻道或調整權限。"
        )
    return None


def ai_state(view: AdminPanelView) -> _AiState:
    access = view.codex_access
    configured_guild = access.enabled and access.guild_id == view.guild_id
    stale_counts = codex_stale_allowlist_counts(view)
    permission_detail = codex_channel_permission_detail(view)
    allowlist_detail = codex_stale_detail(stale_counts) or permission_detail
    if not access.enabled:
        label, detail = "停用", "AI 對話目前依設定停用"
    elif not configured_guild:
        label, detail = "停用", "此伺服器不在 AI 白名單"
    elif not access.state_available:
        label, detail = "需要處理", "白名單狀態檔不可用"
    elif not access.channel_ids:
        label, detail = "需要處理", "白名單頻道尚未設定"
    elif not access.configured:
        label, detail = "需要處理", "白名單身分組尚未設定"
    elif allowlist_detail is not None:
        label, detail = "需要處理", allowlist_detail
    elif not view.codex_status.available and (
        view.codex_status.protocol_version != 3 or view.codex_status.reason in {"", "unavailable"}
    ):
        label, detail = "需要處理", "Codex bridge 無法連線"
    elif not view.codex_status.ready:
        label, detail = (
            "需要處理",
            {
                "initializing": "AI 服務初始化中",
                "auth_required": "Codex 尚未登入",
                "status_stale": "AI 帳號狀態已過期，請稍後重新整理",
                "state_unavailable": "AI 對話狀態檔不可用",
                "draining": "AI 服務正在收尾",
                "unavailable": "AI 服務暫時不可用",
            }.get(view.codex_status.reason, "AI 服務暫時不可用"),
        )
    else:
        label, detail = "狀態正常", ""
    return _AiState(label, detail, allowlist_detail, stale_counts, permission_detail)


def codex_summary(state: _AiState) -> str | None:
    detail = state.allowlist_detail
    if detail is None:
        return None
    return f"## 🟠 AI 助手\n**需要處理**\n-# {detail}"


def voice_state(view: AdminPanelView, guild: discord.Guild | None = None) -> _VoiceState:
    status = view.temp_voice.get_guild_status(view.guild_id)
    problem = voice_entry_problem(view, status, guild)
    if not view.temp_voice_enabled:
        label, detail = "停用", "依設定停用"
        entry, next_step = "不會建立、同步或管理語音頻道", None
    elif not status.state_available:
        label, detail = "需要處理", "狀態檔不可用，臨時語音功能已停用"
        entry = "入口無法讀取"
        next_step = "請檢查並修復私有狀態檔後重新啟動 Bot；目前無法同步。"
    elif status.parent_channel_id is None:
        label, detail = "需要處理", "入口頻道尚未綁定"
        entry = "尚未綁定入口"
        next_step = "請確認 Bot 有管理頻道權限，再按「重新同步」尋找或建立入口。"
    elif problem is not None:
        label, detail = "需要處理", problem
        entry, next_step = "入口不可用", problem
    else:
        label, detail = "狀態正常", ""
        entry, next_step = f"入口 <#{status.parent_channel_id}>", None
    return _VoiceState(status, problem, label, detail, entry, next_step)


def voice_summary(state: _VoiceState) -> str:
    status = state.status
    if state.label == "停用":
        return "## ⚫ 臨時語音\n**依設定停用**\n-# 不會建立、同步或管理語音頻道"
    if not status.state_available:
        return "## 🟠 臨時語音\n**需要處理**\n-# 入口無法讀取 · 追蹤暫停"
    detail = f"-# {state.entry} · {state.label}"
    if state.problem is not None:
        detail += f" · {state.problem}"
    return (
        f"## {STATUS_DOTS[state.label]} 臨時語音\n"
        f"**{status.tracked_child_count} 個頻道追蹤中**\n"
        f"{detail}"
    )


def steam_notification_problem(view: AdminPanelView, status: SteamGuildStatus) -> str | None:
    if not view.steam_free_games_enabled:
        return None
    interaction = view._rate_interaction
    guild = getattr(interaction, "guild", None) if interaction is not None else None
    if getattr(guild, "id", None) != view.guild_id or getattr(guild, "unavailable", False):
        return None
    if not status.state_available or status.channel_id is None:
        return None
    return view.steam_free_games.notification_problem(guild)


def steam_state(view: AdminPanelView) -> _SteamState:
    status = view.steam_free_games.get_guild_status(view.guild_id)
    problem = steam_notification_problem(view, status)
    if not view.steam_free_games_enabled:
        label, detail = "停用", "自動通知依設定停用"
        channel, role_status, next_step = "手動查詢仍可使用", "身分組通知依設定停用", None
    elif not status.state_available:
        label, detail = "需要處理", "狀態檔不可用，通知功能已停用"
        channel, role_status = "通知頻道無法讀取", "身分組設定不可用"
        next_step = "請檢查並修復私有狀態檔後重新啟動 Bot；目前無法修改通知設定。"
    else:
        label = "需要處理" if status.channel_id is None or problem else "狀態正常"
        detail = "通知頻道尚未綁定" if status.channel_id is None else problem or ""
        channel = f"通知 <#{status.channel_id}>" if status.channel_id else "尚未綁定通知頻道"
        role_status = (
            "通知身分組 " + " ".join(f"<@&{role_id}>" for role_id in status.role_ids)
            if status.role_ids
            else "未設定通知身分組"
        )
        next_step = problem or (
            "請確認 Bot 有管理頻道、檢視與傳送訊息權限；既有流程會尋找或建立通知頻道。"
            if status.channel_id is None
            else None
        )
    return _SteamState(status, problem, label, detail, channel, role_status, next_step)


def steam_summary(state: _SteamState) -> str:
    status = state.status
    if state.label == "停用":
        return "## ⚫ Steam 免費遊戲\n**自動通知依設定停用**\n-# 手動查詢仍可使用"
    if not status.state_available:
        return "## 🟠 Steam 免費遊戲\n**需要處理**\n-# 通知頻道無法讀取"
    detail = f"-# {state.channel} · 每 {int(status.poll_interval_seconds // 60)} 分鐘檢查 · {state.label}"
    if state.problem is not None:
        detail += f" · {state.problem}"
    return (
        f"## {STATUS_DOTS[state.label]} Steam 免費遊戲\n"
        f"**{status.active_app_count} 款活動中**\n"
        f"{detail}"
    )


def render_overview(view: AdminPanelView) -> None:
    voice = voice_state(view)
    steam = steam_state(view)
    ai = ai_state(view)
    voice_status, voice_problem = voice.status, voice.problem
    steam_status, steam_problem = steam.status, steam.problem
    ai_allowlist_detail = ai.allowlist_detail

    ai_access_enabled = view.codex_access.enabled and view.codex_access.guild_id == view.guild_id

    ai_shortcut = voice_shortcut = steam_shortcut = None
    if ai_access_enabled and (
        not view.codex_access.state_available
        or not view.codex_access.channel_ids
        or not view.codex_access.configured
        or ai_allowlist_detail is not None
    ):
        ai_shortcut = view._button("ai_access", "前往設定")
    elif ai_access_enabled and (
        not view.codex_status.available or not view.codex_status.authenticated
    ):
        ai_shortcut = view._button("ai", "查看狀態")
    if view.temp_voice_enabled and (
        not voice_status.state_available
        or voice_status.parent_channel_id is None
        or voice_problem is not None
    ):
        voice_shortcut = view._button("voice", "前往設定")
    if view.steam_free_games_enabled and (
        not steam_status.state_available
        or steam_status.channel_id is None
        or steam_problem is not None
    ):
        steam_shortcut = view._button("steam", "前往設定")

    authenticated = "已登入" if view.codex_status.authenticated else "未登入"
    calendar_label, calendar_detail = calendar_status(view)
    rows = (
        ("AI 助手", ai.label, ai.detail or f"{ai_display(view)[0]} · {authenticated}", ai_shortcut),
        (
            "臨時語音",
            voice.label,
            voice.detail or f"{voice.entry} · {voice_status.tracked_child_count} 個頻道追蹤中",
            voice_shortcut,
        ),
        (
            "Steam 免費遊戲",
            steam.label,
            steam.detail or f"{steam.channel} · {steam_status.active_app_count} 款活動中",
            steam_shortcut,
        ),
    )
    rows += (("行事曆", calendar_label, calendar_detail, view._button("calendar", "前往設定")),)
    issues = sum(label == "需要處理" for _, label, _, _ in rows)
    subtitle = f"{len(rows)} 個模組 · " + (f"{issues} 個需要處理" if issues else "狀態正常")
    children = view._header("管理控制台", "overview", subtitle=subtitle)
    for name, label, detail, shortcut in rows:
        text = f"{STATUS_DOTS[label]} **{name}**\n-# {detail}"
        children.append(
            discord.ui.Section(text, accessory=shortcut)
            if shortcut is not None
            else discord.ui.TextDisplay(text)
        )
    if rate_visible(view):
        children.append(view._rate_item(compact=True))
    children.extend((discord.ui.Separator(), view._actions()))
    view._set_container(*children)


def render_ai(view: AdminPanelView, note: str | None = None) -> None:
    ai = ai_state(view)
    plan = ai_display(view)[0]
    authenticated = "已登入" if view.codex_status.authenticated else "未登入"
    children = view._header("AI 助手", "ai", "ai")
    if view.codex_status.last_error:
        children.append(
            discord.ui.TextDisplay(
                f"## ⚠ 最近錯誤\n{discord.utils.escape_markdown(view.codex_status.last_error)}"
            )
        )
    children.append(
        discord.ui.TextDisplay(f"{STATUS_DOTS[ai.label]} **{plan} · {authenticated} · {ai.label}**")
    )
    if rate_visible(view):
        children.append(view._rate_item(compact=True))
    if not view.codex_status.available or not view.codex_status.ready:
        next_step = ai.detail
        if next_step == "Codex bridge 無法連線":
            next_step = "請確認 AI 服務正在執行且連線設定正確，再重新整理。"
        children.append(detail("下一步", next_step))
    children.extend((discord.ui.Separator(), *_footer_note(note), view._actions()))
    view._set_container(*children)


def render_ai_access(view: AdminPanelView, note: str | None = None) -> None:
    ai = ai_state(view)
    configured_guild = view.codex_access.guild_id == view.guild_id
    stale_channel_count, stale_role_count = ai.stale_counts
    permission_detail = ai.permission_detail
    channel_ids = (
        view.codex_access.channel_ids if view.codex_access.state_available else frozenset()
    )
    role_ids = view.codex_access.role_ids if view.codex_access.state_available else frozenset()
    if not configured_guild:
        channel_detail = "此伺服器不在 AI 白名單"
    elif not view.codex_access.enabled:
        channel_detail = "AI 對話目前依設定停用"
    elif not view.codex_access.state_available:
        channel_detail = "狀態檔不可用；重新選擇可修復"
    elif not channel_ids:
        channel_detail = "請選擇可使用 AI 的文字頻道"
    else:
        channel_detail = "僅所選頻道及其 Thread 可使用 AI"
    role_detail = "擁有任一所選身分組即可使用" if role_ids else "請選擇白名單身分組；目前尚未開放"
    if stale_channel_count:
        channel_detail += f"；{stale_channel_count} 個白名單頻道已不存在，請重新選擇"
    if permission_detail:
        channel_detail += f"；{permission_detail}"
    if stale_role_count:
        role_detail += f"；{stale_role_count} 個白名單身分組已不存在，請重新選擇"
    current_allowed = view.codex_access.allows(
        view.guild_id,
        next(iter(channel_ids), None),
        view.user_role_ids,
    )
    children = view._header("AI 使用權限", "ai_access", "ai")
    children.extend(
        (
            discord.ui.TextDisplay(f"## 白名單頻道 · {len(channel_ids)} 個\n-# {channel_detail}"),
            discord.ui.ActionRow(
                view._codex_channel_select(
                    disabled=not view.codex_access.enabled or not configured_guild,
                    channel_ids=channel_ids,
                )
            ),
            discord.ui.TextDisplay(
                f"## 白名單身分組 · {len(role_ids)} 個\n"
                f"-# {role_detail} · 目前操作者："
                f"{'已允許' if current_allowed else '未允許'}"
            ),
            discord.ui.ActionRow(
                view._codex_role_select(
                    disabled=not view.codex_access.enabled
                    or not configured_guild
                    or not view.codex_access.state_available
                    or not channel_ids,
                    role_ids=role_ids,
                )
            ),
            discord.ui.Separator(),
            *_footer_note(note),
            view._actions(),
        )
    )
    view._set_container(*children)


def render_ai_models(view: AdminPanelView) -> None:
    children = view._header("AI 模型設定", "ai_models", "ai")
    children.append(discord.ui.TextDisplay("-# 全 Bot 共用；儲存後只影響後續接納的請求。"))
    if not view.codex_client.model_settings.available:
        children.append(
            discord.ui.TextDisplay("⚠ 模型設定檔無法讀取；以下為預設草稿，請確認並明確儲存以修復。")
        )
    last_page = max(0, (len(view.model_catalog) - 1) // 24)
    for side, label in (("primary", "主模型"), ("fallback", "備援模型")):
        choice = getattr(view.model_draft, side) if view.model_draft is not None else None
        info = next(
            (item for item in view.model_catalog if choice and item.model == choice.model), None
        )
        value = discord.utils.escape_markdown(choice.model) if choice is not None else "停用"
        effort = choice.effort if choice and choice.effort else "模型預設"
        warning = (
            " · ⚠ 模型已失效"
            if choice is not None and info is None and view.model_catalog_available
            else ""
        )
        page = view.model_pages[side]
        page_text = f"\n-# 模型選單第 {page + 1}／{last_page + 1} 頁" if last_page else ""
        children.extend(
            (
                discord.ui.TextDisplay(f"## {label}\n**{value}** · {effort}{warning}{page_text}"),
                discord.ui.ActionRow(view._model_setting_select(f"{side}_model")),
                discord.ui.ActionRow(view._model_setting_select(f"{side}_effort")),
            )
        )
    children.extend((discord.ui.Separator(), *_footer_note(view.model_notice)))
    if last_page:
        children.append(
            discord.ui.ActionRow(
                view._button(
                    "model_primary_prev", "主模型上一頁", disabled=view.model_pages["primary"] == 0
                ),
                view._button(
                    "model_primary_next",
                    "主模型下一頁",
                    disabled=view.model_pages["primary"] >= last_page,
                ),
                view._button(
                    "model_fallback_prev", "備援上一頁", disabled=view.model_pages["fallback"] == 0
                ),
                view._button(
                    "model_fallback_next",
                    "備援下一頁",
                    disabled=view.model_pages["fallback"] >= last_page,
                ),
            )
        )
    children.append(
        view._actions(
            view._button(
                "model_save",
                "儲存設定",
                style=discord.ButtonStyle.primary,
                disabled=not view.model_catalog_available,
            )
        )
    )
    view._set_container(*children)


def render_ai_tech(view: AdminPanelView) -> None:
    plan, runtime, search = ai_display(view)
    sdk = view.codex_status.sdk_version or "Unknown"
    children = view._header("AI 技術資訊", "ai_tech", "ai")
    children.append(
        discord.ui.TextDisplay(
            "## 執行環境\n"
            f"**{plan} · Runtime {runtime} · SDK {sdk}**\n"
            f"-# Web Search {search}\n"
            "## 工作與對話\n"
            f"**持久 Thread**　{view.codex_status.thread_count} 條\n"
            f"**Bot**　執行 {view.codex_status.bot_active_requests} · 等待 {view.codex_status.bot_queued_requests}\n"
            f"**Bridge**　執行 {view.codex_status.active_requests} · 等待 {view.codex_status.queued_requests}"
        )
    )
    if rate_visible(view):
        children.append(view._rate_item())
    children.extend(
        (
            discord.ui.TextDisplay(
                "## 安全邊界\n**Read-only · Deny-all**\n"
                "-# Shell、MCP、Apps、Subagents 與全域 Memories 均停用"
            ),
            discord.ui.Separator(),
            view._actions(),
        )
    )
    view._set_container(*children)


def render_modules(view: AdminPanelView) -> None:
    summary = codex_summary(ai_state(view))
    voice = voice_state(view)
    steam = steam_state(view)
    calendar_label, calendar_detail = calendar_status(view)
    children = view._header("功能模組", "modules", "modules")
    if summary is not None:
        children.append(discord.ui.TextDisplay(summary))
    children.extend(
        (
            discord.ui.TextDisplay(voice_summary(voice)),
            discord.ui.TextDisplay(steam_summary(steam)),
            discord.ui.Section(
                f"## 📅 行事曆\n**{calendar_label}**\n-# {calendar_detail}",
                accessory=view._button("calendar", "前往設定"),
            ),
            discord.ui.Separator(),
            view._actions(),
        )
    )
    view._set_container(*children)


def render_voice(
    view: AdminPanelView, note: str | None = None, *, voice: _VoiceState | None = None
) -> None:
    voice = voice or voice_state(view)
    status = voice.status
    children = view._header("臨時語音", "voice", "modules")
    children.append(
        discord.ui.TextDisplay(
            "## 目前狀態\n"
            f"{STATUS_DOTS[voice.label]} **{status.tracked_child_count} 個臨時語音頻道**\n"
            f"-# {voice.entry} · {voice.label}"
        )
    )
    if voice.next_step:
        children.append(detail("下一步", voice.next_step))
    children.extend((discord.ui.Separator(), *_footer_note(note)))
    children.append(
        view._actions(
            view._button(
                "voice_sync",
                "重新同步",
                style=discord.ButtonStyle.primary,
                disabled=not view.temp_voice_enabled or not status.state_available,
            ),
        )
    )
    view._set_container(*children)


def render_steam(
    view: AdminPanelView,
    result: SteamFetchResult | None = None,
    *,
    error: str | None = None,
    notice: str | None = None,
) -> None:
    if error is not None:
        view._steam_result = None
        view._steam_error = error
        view._steam_fetched_at = None
    elif result is not None:
        view._steam_result = result
        view._steam_error = None
        view._steam_fetched_at = int(time.time())

    result = view._steam_result
    error = view._steam_error
    steam = steam_state(view)
    status = steam.status
    role_controls_disabled = not view.steam_free_games_enabled or not status.state_available
    children = view._header("Steam 限時免費", "steam", "modules")
    children.append(
        discord.ui.TextDisplay(
            "## 通知狀態\n"
            f"{STATUS_DOTS[steam.label]} **{status.active_app_count} 款活動中**\n"
            f"-# {steam.channel} · {steam.role_status} · 每 {int(status.poll_interval_seconds // 60)} 分鐘檢查 · {steam.label}"
        )
    )
    children.append(
        discord.ui.ActionRow(
            view._steam_role_select(
                disabled=role_controls_disabled,
                configured=bool(status.role_ids),
            )
        )
    )

    if steam.next_step:
        children.append(detail("下一步", steam.next_step))

    if error is not None:
        children.append(
            discord.ui.TextDisplay(f"## 查詢結果\n-# {discord.utils.escape_markdown(error)}")
        )
    elif result is None:
        children.append(
            discord.ui.TextDisplay("## 查詢結果\n-# 按「重新查詢」取得目前符合條件的限時免費遊戲。")
        )
    else:
        offers = result.offers[:MAX_STEAM_OFFERS_SHOWN]
        query_time = (
            f"\n-# 查詢時間 <t:{view._steam_fetched_at}:T>"
            if view._steam_fetched_at is not None
            else ""
        )
        if result.failed_app_count > 0:
            children.append(
                discord.ui.TextDisplay(
                    "## 查詢結果"
                    f"{query_time}\n"
                    f"-# 資料不完整：{result.failed_app_count} 款遊戲詳細資料無法取得；"
                    "請稍後再查詢。"
                )
            )
        elif not offers:
            children.append(
                discord.ui.TextDisplay(
                    f"## 查詢結果{query_time}\n-# 目前沒有符合條件的限時免費遊戲。"
                )
            )
        else:
            children.append(discord.ui.TextDisplay(f"## 查詢結果{query_time}"))
        if offers:
            children.extend(offer_items(offer, compact=True)[0] for offer in offers)
            if len(result.offers) > len(offers):
                children.append(
                    discord.ui.TextDisplay(
                        f"-# 只顯示前 {len(offers)} 款，共 {len(result.offers)} 款。"
                    )
                )

    children.extend(
        (
            discord.ui.Separator(),
            *_footer_note(notice),
            view._actions(
                view._button("steam_query", "重新查詢", style=discord.ButtonStyle.primary),
                view._button(
                    "steam_role_clear",
                    "取消身分組通知",
                    disabled=role_controls_disabled or not status.role_ids,
                ),
            ),
        )
    )
    view._set_container(*children)


def calendar_status(view: AdminPanelView) -> tuple[str, str]:
    manager = view.calendar
    if not manager.state_available:
        return "需要處理", "行事曆狀態目前不可用，請檢查儲存狀態並重新啟動 Bot。"
    binding = manager.get_binding(view.guild_id)
    if binding is None:
        return "需要處理", "尚未綁定；選擇文字頻道後按「套用綁定」。"
    if not manager.binding_channel_is_valid(view.guild):
        return "需要處理", f"<#{binding.channel_id}> 已不存在或不是一般文字頻道，請重新綁定或解除。"
    return "狀態正常", f"看板 <#{binding.channel_id}> · 隨活動與日期自動更新"


def render_calendar(view: AdminPanelView, note: str | None = None) -> None:
    manager = view.calendar
    available = manager.state_available
    if note is not None:
        view.calendar_notice = note
    if not available:
        view.calendar_unbind_target = None
    binding = manager.get_binding(view.guild_id) if available else None
    valid = available and manager.binding_channel_is_valid(view.guild)
    label, text = calendar_status(view)
    children = view._header("行事曆", "calendar", "modules", "設定公開看板的位置與更新狀態")
    children.append(discord.ui.TextDisplay(f"{STATUS_DOTS[label]} **{label}**\n-# {text}"))
    channel = view.pending_calendar_channel
    if channel is not None:
        children.append(discord.ui.TextDisplay(f"**待套用**　<#{channel.id}>"))
    view._calendar_channel_control.disabled = not available
    children.append(discord.ui.ActionRow(view._calendar_channel_control))
    if view.calendar_unbind_target is not None:
        children.append(
            discord.ui.TextDisplay(
                "### 解除綁定？\n-# 看板訊息將被移除；既有 Discord 活動不會刪除。"
            )
        )
        buttons = (
            view._button("calendar_unbind_confirm", "確認解除", style=discord.ButtonStyle.danger),
            view._button("calendar_unbind_cancel", "取消"),
        )
    else:
        buttons = (
            view._button(
                "calendar_apply",
                "套用綁定",
                style=discord.ButtonStyle.primary,
                disabled=not available or channel is None,
            ),
            view._button("calendar_unbind", "解除綁定", disabled=not available or binding is None),
        )
        if valid and binding is not None:
            children.append(
                discord.ui.ActionRow(
                    discord.ui.Button(
                        label="開啟看板",
                        style=discord.ButtonStyle.link,
                        url=f"https://discord.com/channels/{view.guild_id}/{binding.channel_id}/{binding.message_id}",
                    )
                )
            )
    children.extend((discord.ui.Separator(), *_footer_note(view.calendar_notice)))
    actions = view._actions(*buttons)
    view._button("refresh", "重新整理", disabled=not available or not valid)
    children.append(actions)
    view._set_container(*children)
