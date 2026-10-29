from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import discord

from src.admin.adapters.discord.pages.common import PendingConfirm
from src.ai import ModelError, RuntimeStatus
from src.core.adapters.discord.brand import ICONS

if TYPE_CHECKING:
    from src.admin.adapters.discord.panel import AdminPanelView

type Note = tuple[str, str] | None
type Redraw = Callable[[Note], None]
type Reload = Callable[[int], Awaitable[bool]]


def follow_logins(view: AdminPanelView, redraw: Redraw) -> Callable[[], None]:
    def finished(success: bool) -> None:
        note = ("ok", "已登入 Codex。") if success else ("warn", "登入未完成，請重新按「登入」。")
        view.spawn(view.refresh("ai", lambda: redraw(note)))

    return view.services.logins.subscribe(finished)


def account_items(
    view: AdminPanelView, status: RuntimeStatus, redraw: Redraw, reload: Reload,
) -> tuple[discord.ui.Item, ...]:
    if status.authenticated:
        logout = PendingConfirm(
            "確認登出 Codex", "登出後 AI 功能會停止，直到再次登入。", "確認登出",
            lambda interaction: _logout(view, interaction, redraw, reload),
        )
        return (discord.ui.Section(
            "### Codex 帳號\n已登入 ChatGPT 帳號。",
            accessory=view.button("logout", "登出", on_click=lambda interaction: view.ask(interaction, logout),
                                  emoji=ICONS["leave"]),
        ),)
    if status.reason != "auth_required":
        return ()
    if (pending := view.services.logins.pending) is not None:
        login = pending.login
        return (
            discord.ui.TextDisplay(
                "### 登入 Codex\n"
                f"1. 開啟 {login.verification_url}\n"
                f"2. 輸入代碼 `{login.user_code}`\n"
                f"完成後這裡會自動更新；代碼將在 <t:{pending.expires_at}:R> 失效。"
            ),
            discord.ui.ActionRow(
                view.button("login_cancel", "取消登入", on_click=lambda interaction: _cancel_login(view, interaction, redraw),
                            emoji=ICONS["cancel"]),
            ),
        )
    return (discord.ui.Section(
        "### Codex 帳號\n尚未登入。按「登入」取得裝置碼，再用 ChatGPT 帳號完成登入。",
        accessory=view.button("login", "登入", on_click=lambda interaction: _start_login(view, interaction, redraw),
                              style=discord.ButtonStyle.primary, emoji=ICONS["access"]),
    ),)


async def _start_login(view: AdminPanelView, interaction: discord.Interaction, redraw: Redraw) -> None:
    async def work(operation: int) -> Callable[[], None]:
        try:
            await view.services.logins.start()
        except ModelError:
            return lambda: redraw(("warn", "目前無法開始登入，請稍後再試。"))
        return lambda: redraw(None)

    await view.run(interaction, work)


async def _cancel_login(view: AdminPanelView, interaction: discord.Interaction, redraw: Redraw) -> None:
    async def work(operation: int) -> Callable[[], None]:
        await view.services.logins.cancel()
        return lambda: redraw(("info", "已取消登入。"))

    await view.run(interaction, work)


async def _logout(view: AdminPanelView, interaction: discord.Interaction, redraw: Redraw, reload: Reload) -> None:
    async def work(operation: int) -> Callable[[], None] | None:
        try:
            await view.services.codex.logout()
        except ModelError:
            note = ("warn", "登出失敗，請稍後再試。")
        else:
            note = ("ok", "已登出 Codex。")
        if not await reload(operation):
            return None
        return lambda: redraw(note)

    await view.run(interaction, work)
