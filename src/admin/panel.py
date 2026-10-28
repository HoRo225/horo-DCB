from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord

from src.admin.pages.ai import load_ai, render_ai, render_ai_access, render_ai_tech
from src.admin.pages.ai_models import load_models, render_ai_models
from src.admin.pages.common import IDLE_TIMEOUT_SECONDS
from src.admin.pages.games import render_games
from src.admin.pages.effects import render_effects
from src.admin.pages.overview import render_overview
from src.admin.pages.steam import render_steam
from src.admin.pages.voice import render_voice
from src.admin.ports import CodexAccount
from src.admin.sessions import PanelSession
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.discord.brand import (
    BRAND_COLOUR,
    ICONS,
    NEUTRAL_COLOUR,
    WARN_COLOUR,
    ErrorNotice,
    bot_avatar,
    card_title,
    notice_view,
    send_notice,
    tint,
    tone_colour,
)

if TYPE_CHECKING:
    from src.bot import HoroBot

QUICK_REPLY_SECONDS = 1.5

@dataclass(frozen=True)
class Page:
    key: str
    description: str
    emoji: str
    render: Callable[[AdminPanelView], None]
    load: Callable[[AdminPanelView, int], Awaitable[bool]] | None = None


PAGES = {
    page.key: page
    for page in (
        Page("overview", "控制台首頁", "overview", render_overview, load_ai),
        Page("ai", "狀態與額度", "ai", render_ai, load_ai),
        Page("ai_access", "頻道與身分組白名單", "access", render_ai_access),
        Page("ai_models", "主模型、備援與推理強度", "models", render_ai_models, load_models),
        Page("ai_tech", "版本、工作與安全邊界", "tech", render_ai_tech, load_ai),
        Page("voice", "入口頻道與同步狀態", "voice", render_voice),
        Page("steam", "通知設定與手動查詢", "steam", render_steam),
        Page("games", "頻道與身分組", "games", render_games),
        Page("effects", "核對通知與資源的待處理結果", "warn", render_effects),
    )
}
AI_TABS = {
    "ai": ("狀態", "status"),
    "ai_access": ("權限", "access"),
    "ai_models": ("模型", "models"),
    "ai_tech": ("技術", "tech"),
}
NAV_PAGES = {
    "ai": "{name}",
    "voice": "臨時語音",
    "steam": "Steam 免費遊戲",
    "games": "遊戲大廳",
    "effects": "待處理項目",
}


class AdminPanelView(ErrorNotice, discord.ui.LayoutView):
    def __init__(
        self, bot: HoroBot, live: LiveGuild, session: PanelSession, interaction: discord.Interaction
    ) -> None:
        super().__init__(timeout=IDLE_TIMEOUT_SECONDS)
        self.page = "overview"
        self.user_id = interaction.user.id
        self._bot = bot
        self.live = live
        self.guild = interaction.guild
        self.guild_id = self.guild.id
        self.codex_client: CodexAccount = bot.codex
        self.ai_settings = bot.ai_settings
        self.access_service = bot.access_service
        self.temp_voice = bot.temp_voice
        self.steam = bot.steam
        self.games = bot.games
        self.game_operations = bot.game_operations
        self.effects = bot.effects
        self.toggled_at: dict[str, float] = {}
        self._states: dict[type, object] = {}
        self._panel_registry = bot.admin_panels
        self._panel_session = session
        self._interaction: discord.Interaction | None = None
        self._retirement_task: asyncio.Task[None] | None = None
        self._idle_closed = False
        self._operation = 0
        self._edit_lock = asyncio.Lock()
        self._control_cache: dict[tuple[object, ...], discord.ui.Item] = {}
        self._desired_children: tuple[discord.ui.Item, ...] | None = None
        self._published_children: tuple[discord.ui.Item, ...] | None = None

    async def open_panel(self, interaction: discord.Interaction) -> None:
        published = False
        opened = False
        try:
            await interaction.response.defer(ephemeral=True)
            self.bind_interaction(interaction)
            self._panel_registry.attach_view(self._panel_session, self)
            home = PAGES["overview"]
            if not self.alive or not await home.load(self, self._begin_operation()):
                return await self._open_failed(interaction)
            home.render(self)
            async with self._edit_lock:
                if not self.alive:
                    return await self._open_failed(interaction)
                published = True
                await interaction.edit_original_response(view=self)
                self._published_children = tuple(self.children)
            opened = self.alive
        finally:
            if not opened:
                self._remove_registry()
                if published:
                    self.close_stale_message()

    async def _open_failed(self, interaction: discord.Interaction) -> None:
        try:
            await interaction.edit_original_response(
                view=notice_view("控制台未能開啟，請重新輸入 /控制台。", "warn")
            )
        except discord.HTTPException:
            logging.exception("Failed to send admin panel open-failure notice.")

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        tone = "warn"
        if interaction.user.id != self.user_id or interaction.guild_id != self.guild_id:
            denial = "只有開啟這個控制台的人可以操作。"
        elif not self.alive:
            denial, tone = "控制台已過期，請重新輸入 /控制台。", "deadline"
        elif not interaction.permissions.administrator or not self._authorized():
            denial = "你已不是伺服器管理員，無法操作控制台。"
        else:
            return True
        if not interaction.response.is_done():
            await send_notice(interaction, denial, tone)
        return False

    def state[T](self, kind: type[T]) -> T:
        if (found := self._states.get(kind)) is None:
            found = self._states[kind] = kind()
        return found

    def control[ItemT: discord.ui.Item](
        self,
        key: tuple[object, ...],
        create: Callable[[], ItemT],
        callback: Callable[[ItemT, discord.Interaction], Awaitable[None]],
    ) -> ItemT:
        item = self._control_cache.get(key)
        if item is None:
            item = create()

            async def dispatch(interaction: discord.Interaction) -> None:
                await callback(item, interaction)

            item.callback = dispatch
            self._control_cache[key] = item
        return item

    def button(
        self,
        key: str,
        label: str,
        *,
        on_click: Callable[[discord.Interaction], Awaitable[None]],
        style: discord.ButtonStyle = discord.ButtonStyle.secondary,
        disabled: bool = False,
        emoji: str | None = None,
    ) -> discord.ui.Button:
        item = self.control(
            ("button", key), discord.ui.Button, lambda _item, interaction: on_click(interaction)
        )
        item.label, item.style, item.disabled, item.emoji = label, style, disabled, emoji and tint(emoji, style)
        return item

    async def open_page(self, interaction: discord.Interaction, key: str) -> None:
        if key == self.page and self._layout_is_published():
            await self.acknowledge(interaction)
        else:
            await self.show(interaction, PAGES[key])

    async def refresh(self, page: str, render: Callable[[], None]) -> None:
        interaction = self._interaction
        if interaction is None or self.page != page:
            return
        operation = self._begin_operation()
        if not self.is_current(operation):
            return
        if (load := PAGES[page].load) is not None and not await load(self, operation):
            return
        render()
        await self._edit_original(interaction, operation)

    async def show(self, interaction: discord.Interaction, page: Page, *, load: bool = True) -> None:
        async def work(operation: int) -> Callable[[], None] | None:
            if load and page.load is not None and not await page.load(self, operation):
                return None
            return lambda: page.render(self)

        await self.run(interaction, work)

    def set_items(self, *items: discord.ui.Item) -> None:
        self.clear_items()
        for item in items:
            self.add_item(item)
        self._desired_children = tuple(self.children)

    def set_container(
        self,
        *children: discord.ui.Item,
        colour: discord.Colour = BRAND_COLOUR,
        note: tuple[str, str] | None = None,
        problem: str | None = None,
    ) -> None:
        items: list[discord.ui.Item] = []
        if problem:
            problem += "" if problem.endswith("。") else "。"
            items.append(
                discord.ui.Container(
                    discord.ui.TextDisplay(f"**{ICONS['warn']} 需要處理**\n{problem}"),
                    accent_colour=WARN_COLOUR,
                )
            )
        colour = tone_colour(note[0], colour) if note else colour
        items.append(discord.ui.Container(*children, *self._page_menu(), accent_colour=colour))
        self.set_items(*items)

    def _page_menu(self) -> tuple[discord.ui.ActionRow, ...]:
        if self.page not in PAGES:
            return ()
        here = "ai" if self.page in AI_TABS else self.page
        menu = self.control(
            ("select", "page_menu"),
            lambda: discord.ui.Select(placeholder="前往其他頁面…", min_values=1, max_values=1),
            lambda item, interaction: self.open_page(interaction, item.values[0]),
        )
        menu.options = [
            discord.SelectOption(
                label=label.format(name=self.bot_name),
                value=key,
                emoji=ICONS[PAGES[key].emoji],
                description=PAGES[key].description,
            )
            for key, label in NAV_PAGES.items()
            if key != here
        ]
        return (discord.ui.ActionRow(menu),)

    @property
    def bot_name(self) -> str:
        return self.guild.me.display_name

    @property
    def bot_name_md(self) -> str:
        return discord.utils.escape_markdown(self.bot_name)

    def bot_text(self, before: str = "", after: str = "", *, markdown: bool = True) -> str:
        name = self.bot_name_md if markdown else self.bot_name
        left = " " if before[-1:].isalnum() and name[:1].isascii() else ""
        right = " " if after[:1].isalnum() and name[-1:].isascii() else ""
        return f"{before}{left}{name}{right}{after}"

    def header(
        self,
        title: str,
        page: str,
        subtitle: str = "",
    ) -> list[discord.ui.Item]:
        self.page = page
        info = PAGES[page]
        heading = f"{ICONS[info.emoji]} {title}".strip()
        subtitle = subtitle or info.description
        if page == "overview":
            top = card_title(heading, subtitle, thumbnail=bot_avatar())
        else:
            back = self.button(
                "back",
                "返回總覽",
                on_click=lambda interaction: self.show(interaction, PAGES["overview"], load=False),
                emoji=ICONS["back"],
            )
            top = card_title(heading, subtitle, accessory=back)
        children: list[discord.ui.Item] = [top]
        if page in AI_TABS:
            children.append(
                discord.ui.ActionRow(
                    *(
                        self.button(
                            f"tab_{key}",
                            label,
                            on_click=lambda interaction, key=key: self.open_page(interaction, key),
                            style=discord.ButtonStyle.primary
                            if key == page
                            else discord.ButtonStyle.secondary,
                            emoji=ICONS[icon],
                        )
                        for key, (label, icon) in AI_TABS.items()
                    )
                )
            )
        children.append(discord.ui.Separator(spacing=discord.SeparatorSpacing.large))
        return children

    def actions(self, *buttons: discord.ui.Button, refresh: bool = True) -> discord.ui.ActionRow:
        return discord.ui.ActionRow(
            *buttons,
            *(
                [
                    self.button(
                        "refresh",
                        "重新整理",
                        on_click=lambda interaction: self.show(interaction, PAGES[self.page]),
                        emoji=ICONS["refresh"],
                    )
                ]
                if refresh
                else []
            ),
        )

    def spawn[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        return self._panel_registry.spawn(self._panel_session, coro)

    @property
    def interaction_user(self) -> discord.User | discord.Member | None:
        return self._interaction.user if self._interaction is not None else None

    @property
    def alive(self) -> bool:
        return self._panel_registry.is_current(self._panel_session)

    def _remove_registry(self) -> None:
        self._panel_registry.retire(self._panel_session)

    async def on_timeout(self) -> None:
        self._idle_closed = True
        self._remove_registry()

    def retire_session(self) -> None:
        self._operation += 1
        self.stop()
        current = asyncio.current_task()
        for task in tuple(self._panel_session.tasks):
            if (
                task is not current
                and task is not self._retirement_task
                and not task.done()
                and not task.cancelling()
            ):
                task.cancel()
        self.close_stale_message()

    def bind_interaction(self, interaction: discord.Interaction) -> None:
        if self._interaction is None or interaction.id > self._interaction.id:
            self._interaction = interaction

    def close_stale_message(self) -> None:
        if self._retirement_task is not None:
            return
        self._retirement_task = self.spawn(self._close_stale_message())

    async def _close_stale_message(self) -> None:
        async with self._edit_lock:
            interaction = self._interaction
            if interaction is None:
                return
            if not self._idle_closed:
                try:
                    async with asyncio.timeout(3):
                        await interaction.delete_original_response()
                    return
                except discord.NotFound:
                    return
                except Exception:
                    logging.exception("Admin panel message deletion failed.")
            try:
                self._render_closed()
                async with asyncio.timeout(3):
                    await interaction.edit_original_response(view=self)
            except discord.NotFound:
                return
            except Exception:
                logging.exception("Admin panel retirement notification failed.")

    def _render_closed(self) -> None:
        self.page = "closed"
        subtitle = f"閒置 {IDLE_TIMEOUT_SECONDS / 60:.0f} 分鐘，已自動關閉" if self._idle_closed else "已關閉"
        self.set_container(
            card_title(f"{ICONS['closed']} 管理控制台", subtitle),
            discord.ui.TextDisplay("重新輸入 /控制台 即可再開啟。"),
            colour=NEUTRAL_COLOUR,
        )
        self.stop()

    def _begin_operation(self) -> int:
        self._operation += 1
        return self._operation

    async def acknowledge(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if self.alive:
            self.bind_interaction(interaction)

    async def run(
        self,
        interaction: discord.Interaction,
        work: Callable[[int], Awaitable[Callable[[], None] | None]],
    ) -> None:
        try:
            operation = self._begin_operation()
            if not self.is_current(operation):
                return
            async def authorized_work():
                if not self.is_current(operation):
                    return None
                return await work(operation)

            working = self._panel_registry.mutate(authorized_work())
            await asyncio.wait({working}, timeout=QUICK_REPLY_SECONDS)
            if not working.done() or self._edit_lock.locked():
                await self.acknowledge(interaction)
            render = await asyncio.shield(working)
            if not self.is_current(operation):
                return
            if render is None:
                await send_notice(interaction, "畫面未能更新，請按「重新整理」再試一次。")
                return
            render()
            await self._edit_original(interaction, operation)
        finally:
            if not interaction.response.is_done():
                await self.acknowledge(interaction)

    def is_current(self, operation: int) -> bool:
        return operation == self._operation and self.alive and self._authorized()

    def _authorized(self) -> bool:
        guild = self.live.guild()
        if guild is None or not self.live.admin(self.user_id):
            return False
        self.guild = guild
        return True

    async def _edit_original(
        self,
        interaction: discord.Interaction,
        operation: int,
    ) -> None:
        if not self.is_current(operation):
            return
        await self._panel_registry.run_child(
            self._panel_session, self._publish_edit(interaction, operation)
        )

    def _layout_is_published(self) -> bool:
        return (
            self._desired_children is not None
            and self._desired_children == self._published_children
        )

    async def _publish_edit(
        self,
        interaction: discord.Interaction,
        operation: int,
    ) -> None:
        if not interaction.response.is_done() and self._edit_lock.locked():
            await self.acknowledge(interaction)
        async with self._edit_lock:
            if not self.is_current(operation):
                return
            desired = self._desired_children
            sent_children = tuple(self.children)
            try:
                if interaction.response.is_done():
                    await interaction.edit_original_response(view=self)
                else:
                    await interaction.response.edit_message(view=self)
            except Exception, asyncio.CancelledError:
                restore = self._published_children
                if self._desired_children is not desired and self._desired_children is not None:
                    restore = self._desired_children
                if restore is None:
                    restore = sent_children
                self.clear_items()
                for child in restore:
                    self.add_item(child)
                raise
            else:
                self._published_children = sent_children
                if self.alive:
                    self.bind_interaction(interaction)
            finally:
                if not self.alive:
                    self.close_stale_message()
