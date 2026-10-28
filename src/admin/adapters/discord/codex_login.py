from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import discord

from src.admin.adapters.discord.pages.common import PendingConfirm
from src.ai import DeviceLogin, ModelError, RuntimeStatus
from src.core.adapters.discord.brand import ICONS

if TYPE_CHECKING:
    from src.admin.adapters.discord.panel import AdminPanelView

type Note = tuple[str, str] | None
type Redraw = Callable[[Note], None]
type Reload = Callable[[int], Awaitable[bool]]


@dataclass(slots=True)
class CodexLogin:
    login: DeviceLogin | None = None
    task: asyncio.Task[None] | None = None


def account_items(
    view: AdminPanelView, status: RuntimeStatus, redraw: Redraw, reload: Reload,
) -> tuple[discord.ui.Item, ...]:
    flow = view.state(CodexLogin)
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
    if (login := flow.login) is not None:
        return (
            discord.ui.TextDisplay(
                "### 登入 Codex\n"
                f"1. 開啟 {login.verification_url}\n"
                f"2. 輸入代碼 `{login.user_code}`\n"
                "完成後這裡會自動更新；代碼約 15 分鐘後失效。"
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
        flow = view.state(CodexLogin)
        try:
            login = await view.services.codex.login_start()
        except ModelError:
            return lambda: redraw(("warn", "目前無法開始登入，請稍後再試。"))
        if flow.task is not None:
            flow.task.cancel()
        flow.login = login
        flow.task = view.spawn(_watch_login(view, login, redraw))
        return lambda: redraw(None)

    await view.run(interaction, work)


async def _watch_login(view: AdminPanelView, login: DeviceLogin, redraw: Redraw) -> None:
    flow = view.state(CodexLogin)
    client = view.services.codex
    try:
        success = await client.wait_login(login.login_id)
    except TimeoutError:
        success = None
    except asyncio.CancelledError:
        await asyncio.shield(client.login_cancel(login.login_id))
        raise
    finally:
        if flow.login is login:
            flow.login, flow.task = None, None
    if success is None:
        await client.login_cancel(login.login_id)
    note = ("ok", "已登入 Codex。") if success else ("warn", "登入未完成，請重新按「登入」。")
    await view.refresh("ai", lambda: redraw(note))


async def _cancel_login(view: AdminPanelView, interaction: discord.Interaction, redraw: Redraw) -> None:
    async def work(operation: int) -> Callable[[], None]:
        flow = view.state(CodexLogin)
        task, flow.login, flow.task = flow.task, None, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
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
