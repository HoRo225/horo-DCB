from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import discord

from src.core.adapters.discord.brand import ICONS, NEUTRAL_COLOUR, WARN_COLOUR, card_title, reported, send_notice, tint
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.discord.receipts import receipt_view
from src.core.app.actions import ActionEngine, Grant, Line
from src.core.app.ports import Source
from src.core.app.supervisor import TaskGroup
from src.core.domain.actions import Plan

TTL_SECONDS = 300
RUN_SECONDS = 60.0
LIMIT = 200
NOT_YOURS = "只有提出這個操作的人可以確認。"

type Choice = Literal["yes", "no"]


@dataclass(frozen=True, slots=True)
class Pending:
    source: Source
    plan: Plan
    received: Mapping[str, object]
    grant: Grant
    expires_at: float


def _card(heading: str, subtitle: str, lines: Sequence[Line], colour: discord.Colour,
          buttons: Sequence[discord.ui.Item]) -> discord.ui.LayoutView:
    body = "\n".join(f"{ICONS['warn']} **{label}**　{warning}" if warning else f"{ICONS['next']} {label}"
                     for label, warning in lines)
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(card_title(heading, subtitle), discord.ui.TextDisplay(body), accent_colour=colour))
    if buttons:
        view.add_item(discord.ui.ActionRow(*buttons))
    return view


def confirm_view(lines: Sequence[Line], buttons: Sequence[discord.ui.Item]) -> discord.ui.LayoutView:
    return _card(f"{ICONS['ai']} 確認操作", f"按「確認」後才會執行 · {TTL_SECONDS // 60} 分鐘內有效，只有提出的人可以按",
                 lines, WARN_COLOUR, buttons)


def cancelled_view(lines: Sequence[Line]) -> discord.ui.LayoutView:
    return _card(f"{ICONS['cancel']} 已取消", "未執行任何操作", lines, NEUTRAL_COLOUR, ())


def confirmation_item(confirmations: Confirmations) -> type[discord.ui.DynamicItem[discord.ui.Button]]:
    class ConfirmationButton(discord.ui.DynamicItem[discord.ui.Button],
                             template=r"horo:confirm:(?P<token>[A-Za-z0-9_-]+):(?P<choice>yes|no)"):
        def __init__(self, token: str, choice: Choice) -> None:
            style = discord.ButtonStyle.danger if choice == "yes" else discord.ButtonStyle.secondary
            super().__init__(discord.ui.Button(
                label="確認" if choice == "yes" else "取消", style=style,
                emoji=tint(ICONS["ok" if choice == "yes" else "cancel"], style),
                custom_id=f"horo:confirm:{token}:{choice}",
            ))
            self.token, self.choice = token, choice

        @classmethod
        async def from_custom_id(cls, interaction, item, match, /):
            return cls(match["token"], match["choice"])

        @reported
        async def callback(self, interaction: discord.Interaction) -> None:
            await confirmations.choose(self.token, self.choice, interaction)

    return ConfirmationButton


class Confirmations:
    def __init__(self, engine: ActionEngine, live: LiveGuild, tasks: TaskGroup) -> None:
        self._engine = engine
        self._live = live
        self._tasks = tasks
        self._pending: OrderedDict[str, Pending] = OrderedDict()
        self.item = confirmation_item(self)

    def _prune(self) -> None:
        now = time.monotonic()
        while self._pending and next(iter(self._pending.values())).expires_at <= now:
            self._pending.popitem(last=False)

    def ask(self, source: Source, plan: Plan, *, received: Mapping[str, object], grant: Grant) -> discord.ui.LayoutView:
        self._prune()
        while len(self._pending) >= LIMIT:
            self._pending.popitem(last=False)
        token = secrets.token_urlsafe(24)
        self._pending[token] = Pending(source, plan, received, grant, time.monotonic() + TTL_SECONDS)
        return confirm_view(self._engine.review(plan), (self.item(token, "yes"), self.item(token, "no")))

    async def choose(self, token: str, choice: Choice, interaction: discord.Interaction) -> None:
        self._prune()
        entry = self._pending.get(token)
        if entry is None:
            return
        if interaction.user.id != entry.source.actor_id:
            await send_notice(interaction, NOT_YOURS)
            return
        del self._pending[token]
        if choice == "no":
            await interaction.response.edit_message(view=cancelled_view(self._engine.review(entry.plan)))
            return
        await interaction.response.defer()
        self._tasks.spawn(self._run(entry))
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            logging.exception("Confirmation card could not be deleted; the confirmed actions still run.")

    async def _run(self, entry: Pending) -> None:
        source, live = entry.source, self._live
        outcome = await self._engine.execute(
            source, entry.plan, received=entry.received,
            deadline=asyncio.get_running_loop().time() + RUN_SECONDS, grant=entry.grant,
        )
        if outcome.published or live.send_problem(source.channel_id, source.actor_id):
            return
        public = [{key: value for key, value in result.items() if key != "private_ref"} for result in outcome.results]
        try:
            await live.channel(source.channel_id).send(
                view=receipt_view(public, name=live.bot_name()), allowed_mentions=discord.AllowedMentions.none(),
                reference=discord.MessageReference(message_id=source.message_id, channel_id=source.channel_id,
                                                   fail_if_not_exists=False),
            )
        except discord.HTTPException:
            logging.exception("Confirmed action results could not be posted.")
