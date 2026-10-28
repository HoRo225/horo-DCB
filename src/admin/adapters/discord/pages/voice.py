from __future__ import annotations
from src.core.adapters.discord.channels import interaction_operation_id

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from src.admin.adapters.discord.pages.common import (
    status_text,
    channel_select,
    feature_button,
    footer_note,
    no_guild_note,
)
from src.core.adapters.discord.brand import ICONS
from src.core.adapters.discord.channels import is_voice_channel
from src.voice import VoiceStatus

if TYPE_CHECKING:
    from src.admin.adapters.discord.panel import AdminPanelView


@dataclass(frozen=True, slots=True)
class _VoiceState:
    status: VoiceStatus
    problem: str | None
    label: str
    detail: str
    entry: str
    next_step: str | None

    @property
    def summary(self) -> str:
        return self.detail or f"{self.entry} · {self.status.room_count} 個追蹤中"


def voice_entry_problem(view, status: VoiceStatus) -> str | None:
    if not view.services.voice.enabled:
        return None
    guild = view.services.live.guild()
    if guild is None:
        return None

    if status.entry_channel_id is None:
        return None
    return view.services.voice.entry_problem()


def voice_state(view: AdminPanelView) -> _VoiceState:
    status = view.services.voice.status()
    problem = voice_entry_problem(view, status)
    if not view.services.voice.enabled:
        label, detail = "停用", "未開啟"
        entry, next_step = "不會建立新房；既有空房仍會清理", None
    elif status.entry_channel_id is None:
        label, detail = "需要處理", "入口頻道尚未綁定"
        entry = "尚未綁定入口"
        next_step = "請在下方選擇語音入口頻道。"
    elif problem is not None:
        label, detail = "需要處理", problem
        entry, next_step = "入口不可用", problem
    else:
        label, detail = "狀態正常", ""
        entry, next_step = f"入口 <#{status.entry_channel_id}>", None
    return _VoiceState(status, problem, label, detail, entry, next_step)


def render_voice(view: AdminPanelView, note: tuple[str, str] | None = None) -> None:
    voice = voice_state(view)
    status = voice.status
    children = view.header("臨時語音", "voice")
    children.append(
        discord.ui.Section(
            "### 目前狀態\n"
            f"{status_text(voice.label)} · **{status.room_count} 個臨時語音頻道**\n"
            f"{voice.entry}",
            accessory=feature_button(
                view,
                "temp_voice",
                "臨時語音",
                owner=view.services.voice,
                render=render_voice,
                after=_after_voice_toggle,
            ),
        )
    )
    children.append(
        discord.ui.ActionRow(
            channel_select(
                view,
                "voice_entry",
                "選擇臨時語音入口頻道",
                status.entry_channel_id,
                lambda i, channel: handle_voice_entry_select(view, i, channel),
                discord.ChannelType.voice,
            )
        )
    )
    children.extend((discord.ui.Separator(), *footer_note(note)))
    children.append(
        view.actions(
            view.button(
                "voice_sync",
                "重新同步",
                on_click=lambda i: view.run(i, lambda operation: _voice_sync(view, operation)),
                style=discord.ButtonStyle.primary if voice.next_step else discord.ButtonStyle.secondary,
                disabled=not view.services.voice.enabled,
                emoji=ICONS["sync"],
            ),
        )
    )
    view.set_container(*children, note=note, problem=voice.next_step)


async def _after_voice_toggle(
    view: AdminPanelView, interaction: discord.Interaction, operation: int, enabled: bool, _changed: object
) -> tuple[str, str] | None:
    if not enabled or (guild := view.services.live.guild()) is None:
        return None
    try:
        await view.services.voice.reconcile(
            still_current=lambda: view.is_current(operation) and view.services.live.guild() is guild,
        )
    except Exception:
        logging.exception("Admin panel temp voice sync after enabling failed.")
        return ("warn", "入口同步失敗，請按「重新同步」。")
    return None


async def handle_voice_entry_select(
    view: AdminPanelView, interaction: discord.Interaction, channel: object | None
) -> None:
    if view.page != "voice":
        await view.acknowledge(interaction)
        return

    async def work(operation: int) -> Callable[[], None]:
        guild = view.services.live.guild()
        if guild is None:
            return lambda: render_voice(view, no_guild_note())
        chosen = channel.id if is_voice_channel(channel) and channel.guild.id == guild.id else None
        problem = await view.services.voice.bind_entry(chosen, still_current=lambda: view.is_current(operation) and view.services.live.guild() is guild, operation_id=interaction_operation_id(interaction))
        if problem is not None:
            return lambda: render_voice(view, ("warn", problem))
        if not view.services.voice.enabled:
            return lambda: render_voice(view, ("ok", "已綁定入口；開啟臨時語音後生效。"))
        return await _voice_sync(view, operation)

    await view.run(interaction, work)


async def _voice_sync(view: AdminPanelView, operation: int) -> Callable[[], None]:
    if not view.services.voice.enabled:
        return lambda: render_voice(view, ("info", "臨時語音未開啟，未執行同步。"))
    guild = view.services.live.guild()
    if guild is None:
        return lambda: render_voice(view, no_guild_note())
    try:
        await view.services.voice.reconcile(
            still_current=lambda: view.is_current(operation) and view.services.live.guild() is guild,
        )
    except Exception:
        logging.exception("Admin panel temp voice resync failed.")
        return lambda: render_voice(view, ("warn", "重新同步失敗，請查看 Bot 紀錄。"))
    voice = voice_state(view)
    status, problem = voice.status, voice.problem
    if status.entry_channel_id is None or problem is not None:
        detail = voice.next_step or voice.detail
        return lambda: render_voice(view, ("warn", f"同步未完成；{detail}"))
    return lambda: render_voice(view, ("ok", "入口檢查通過，已執行同步流程。"))
