from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

from src.admin.pages.common import (
    status_text,
    ChannelState,
    bind_channel,
    channel_select,
    footer_note,
    live_guild,
    no_guild_note,
    role_select,
)
from src.brand import BRAND_COLOUR, ICONS
from src.discord_utils import hub_link, interaction_operation_id
from src.games.hub import (
    bind_channel as bind_game_channel,
    channel_problem as game_channel_problem,
)

if TYPE_CHECKING:
    from src.admin.panel import AdminPanelView


def games_state(view: AdminPanelView) -> ChannelState:
    channel_id = view.games.channel_id
    if channel_id is None:
        return ChannelState(
            "需要處理",
            "尚未設定遊戲頻道",
            "尚未設定遊戲頻道",
            "請在下方選擇遊戲頻道；選擇後會自動發布遊戲大廳。",
        )
    channel = f"遊戲頻道 <#{channel_id}>"
    guild = live_guild(view)
    if guild is not None and (problem := game_channel_problem(guild, view.games)):
        return ChannelState("需要處理", channel, problem, problem)
    return ChannelState("狀態正常", channel, channel)


def render_games(view: AdminPanelView, note: tuple[str, str] | None = None) -> None:
    channel_id = view.games.channel_id
    state = games_state(view)
    role_ids = view.games.role_ids
    roles = " ".join(f"<@&{role_id}>" for role_id in role_ids) or "所有人"
    hub = view.games.hub_message
    children = view.header("遊戲大廳", "games")
    status = (
        "### 遊戲大廳狀態\n"
        f"{status_text(state.label)} · **{state.channel}**\n"
        f"**可用身分組**　{roles}\n"
        f"**常駐訊息**　{'已發布' if hub else '尚未發布'}"
    )
    children.append(
        discord.ui.Section(status, accessory=link[0]) if (link := hub_link(view._bot)) else discord.ui.TextDisplay(status)
    )
    children.extend(
        (
            discord.ui.ActionRow(
                channel_select(
                    view,
                    "game_channel",
                    "選擇遊戲頻道",
                    channel_id,
                    lambda i, channel: bind_channel(
                        view,
                        i,
                        lambda guild, current: bind_game_channel(guild, view.game_operations, guild.get_channel(channel.id) if channel else None,
                            operation_id=interaction_operation_id(i), still_current=lambda: current() and channel is not None and guild.get_channel(channel.id) is not None),
                        render_games,
                        "已設定遊戲頻道並發布遊戲大廳。",
                    ),
                )
            ),
            discord.ui.ActionRow(
                role_select(
                    view,
                    "game_role",
                    "選擇可用身分組（清空即所有人可用）",
                    role_ids,
                    lambda i, roles: handle_game_role_select(view, i, roles),
                )
            ),
            discord.ui.Separator(),
            *footer_note(note),
            view.actions(
                view.button(
                    "games_publish",
                    "重發遊戲大廳",
                    on_click=lambda i: view.run(i, lambda operation: _publish(view, i, operation)),
                    style=discord.ButtonStyle.primary
                    if not hub or state.label == "需要處理"
                    else discord.ButtonStyle.secondary,
                    disabled=channel_id is None,
                    emoji=ICONS["games"],
                )
            ),
        )
    )
    view.set_container(*children, colour=BRAND_COLOUR, note=note, problem=state.next_step)


async def handle_game_role_select(
    view: AdminPanelView, interaction: discord.Interaction, roles: tuple[discord.Role, ...]
) -> None:
    async def work(operation: int) -> Callable[[], None]:
        guild = live_guild(view)
        if guild is None or any(role.guild.id != view.guild_id or role.is_default() or guild.get_role(role.id) is None for role in roles):
            note = ("warn", "只能選擇目前伺服器的一般身分組。")
        else:
            view.games.set_roles((role.id for role in roles), operation_id=interaction_operation_id(interaction))
            note = (
                ("ok", f"已更新遊戲大廳可用身分組，共 {len(roles)} 個。")
                if roles
                else ("ok", "已清除可用身分組，所有人都可以使用遊戲大廳。")
            )
        return lambda: render_games(view, note)

    await view.run(interaction, work)


async def _publish(view: AdminPanelView, interaction: discord.Interaction, operation: int) -> Callable[[], None]:
    guild = live_guild(view)
    if guild is None:
        return lambda: render_games(view, no_guild_note())
    problem = await view.game_operations.publish_hub(guild, operation_id=interaction_operation_id(interaction),
                                                   still_current=lambda: view.is_current(operation) and live_guild(view) is guild)
    note = ("warn", problem) if problem else ("ok", "已重發遊戲大廳。")
    return lambda: render_games(view, note)

