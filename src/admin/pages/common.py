from __future__ import annotations

import time
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from src.brand import ICONS, note_text
from src.discord_utils import interaction_operation_id

if TYPE_CHECKING:
    from src.admin.panel import AdminPanelView

STATUS_DOTS = {"狀態正常": "dotok", "需要處理": "dotwarn", "停用": "dotoff"}


def status_text(label: str) -> str:
    return f"{ICONS[STATUS_DOTS[label]]} {label}"
IDLE_TIMEOUT_SECONDS = 600.0
TOGGLE_REPEAT_SECONDS = 1.5


def no_guild_note() -> tuple[str, str]:
    return ("warn", "無法取得目前伺服器。")


@dataclass(frozen=True, slots=True)
class ChannelState:
    label: str
    channel: str
    summary: str
    next_step: str | None = None


def footer_note(note: tuple[str, str] | None) -> tuple[discord.ui.Item, ...]:
    if not note:
        return ()
    tone, text = note
    return (discord.ui.TextDisplay(note_text((tone, discord.utils.escape_markdown(text)))),)


def live_guild(view: AdminPanelView) -> discord.Guild | None:
    guild = view._bot.get_guild(view.guild_id)
    if guild is None or guild.id != view.guild_id or guild.unavailable:
        return None
    return guild


def defaults(
    make: Callable[[discord.abc.Snowflake], discord.SelectDefaultValue], ids: Iterable[int | None]
) -> list[discord.SelectDefaultValue]:
    return [make(discord.Object(id=entity_id)) for entity_id in ids if entity_id is not None]


def channel_select(
    view: AdminPanelView,
    key: str,
    placeholder: str,
    current_id: int | None,
    on_pick: Callable[[discord.Interaction, object | None], Awaitable[None]],
    channel_type: discord.ChannelType = discord.ChannelType.text,
) -> discord.ui.ChannelSelect:
    item = view.control(
        ("select", key),
        lambda: discord.ui.ChannelSelect(
            channel_types=[channel_type], placeholder=placeholder, min_values=1, max_values=1
        ),
        lambda item, interaction: on_pick(
            interaction, item.values[0].resolve() if item.values else None
        ),
    )
    item.default_values = defaults(discord.SelectDefaultValue.from_channel, [current_id])
    return item


async def bind_channel(
    view: AdminPanelView,
    interaction: discord.Interaction,
    bind: Callable[[discord.Guild, Callable[[], bool]], Awaitable[str | None]],
    render: Callable[[AdminPanelView, tuple[str, str]], None],
    ok: str,
) -> None:
    async def work(operation: int) -> Callable[[], None]:
        guild = live_guild(view)
        if guild is None:
            note = no_guild_note()
        elif problem := await bind(guild, lambda: view.is_current(operation) and live_guild(view) is guild):
            note = ("warn", problem)
        else:
            note = ("ok", ok)
        return lambda: render(view, note)

    await view.run(interaction, work)


def role_select(
    view: AdminPanelView,
    key: str,
    placeholder: str,
    role_ids: Iterable[int],
    on_pick: Callable[[discord.Interaction, tuple[discord.Role, ...]], Awaitable[None]],
    *,
    disabled: bool = False,
) -> discord.ui.RoleSelect:
    item = view.control(
        ("select", key),
        lambda: discord.ui.RoleSelect(min_values=0, max_values=25),
        lambda item, interaction: on_pick(interaction, tuple(item.values)),
    )
    item.disabled = disabled
    item.placeholder = placeholder
    item.default_values = defaults(discord.SelectDefaultValue.from_role, sorted(role_ids))
    return item


def spacer() -> discord.ui.Separator:
    return discord.ui.Separator(visible=False)


def _join(verb: str, label: str) -> str:
    return f"{verb} {label}" if label[:1].isascii() else f"{verb}{label}"


def feature_button(
    view: AdminPanelView,
    key: str,
    label: str,
    *,
    owner: object,
    attr: str = "enabled",
    render: Callable[[AdminPanelView, tuple[str, str]], None],
    after: Callable[
        [AdminPanelView, discord.Interaction, int, bool], Awaitable[tuple[str, str] | None]
    ]
    | None = None,
) -> discord.ui.Button:
    enabled = getattr(owner, attr)

    async def toggle(interaction: discord.Interaction) -> None:
        now = time.monotonic()
        if now - view.toggled_at.get(key, float("-inf")) < TOGGLE_REPEAT_SECONDS:
            await view.acknowledge(interaction)
            return
        view.toggled_at[key] = now

        async def work(operation: int) -> Callable[[], None]:
            enabled = not getattr(owner, attr)
            if not view.is_current(operation) or not interaction.permissions.administrator:
                return lambda: render(view, ("warn", "操作已過期或權限已變更。"))
            setter = owner.set_display if attr == "text_display" else owner.set_enabled
            result = setter(enabled, operation_id=interaction_operation_id(interaction))
            if inspect.isawaitable(result):
                await result
            name = label.format(name=view.bot_name)
            note = ("ok", f"{_join('已開啟' if enabled else '已關閉', name)}。")
            if after is not None and (extra := await after(view, interaction, operation, enabled)):
                note = (extra[0], f"{note[1]} {extra[1]}")
            return lambda: render(view, note)

        await view.run(interaction, work)

    return view.button(
        f"toggle_{key}",
        _join("關閉" if enabled else "開啟", label.format(name=view.bot_name)),
        on_click=toggle,
        style=discord.ButtonStyle.secondary if enabled else discord.ButtonStyle.primary,
        emoji=ICONS["settings"],
    )
