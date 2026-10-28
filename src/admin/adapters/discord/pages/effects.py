from __future__ import annotations

from dataclasses import dataclass

import discord

from src.core.adapters.discord.brand import ICONS, ErrorNotice, send_notice
from src.admin.adapters.discord.pages.common import footer_note

STATES = {"pending": "待送出", "unknown": "結果不明", "failed": "送出失敗"}


@dataclass
class EffectPick:
    effect_id: str | None = None
    confirming: str | None = None


class ConfirmDelivery(ErrorNotice, discord.ui.Modal, title="核對已送達"):
    def __init__(self, view, effect_id: str) -> None:
        super().__init__()
        self.panel, self.effect_id = view, effect_id
        self.resource = discord.ui.TextInput(min_length=1, max_length=100)
        self.add_item(discord.ui.Label(text="訊息連結或 ID", description="對訊息按右鍵 →「複製訊息連結」；語音房請複製頻道連結",
                                       component=self.resource))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.panel.interaction_check(interaction):
            return
        if self.panel.page != "effects" or self.panel.state(EffectPick).effect_id != self.effect_id:
            await send_notice(interaction, "選取項目已改變，請重新開啟核對視窗。")
            return
        raw = str(self.resource).strip().rstrip("/").rsplit("/", 1)[-1]
        if not raw.isascii() or not raw.isdecimal() or not 0 < int(raw) <= 2**63 - 1:
            await send_notice(interaction, "請輸入正整數 ID。")
            return
        await resolve(self.panel, interaction, "confirmed", self.effect_id, int(raw))


async def resolve(view, interaction, resolution, effect_id, message_id=None):
    async def work(operation):
        problem = await view.services.effects.resolve(
            effect_id, resolution, message_id=message_id, actor_id=interaction.user.id,
            still_current=lambda: view.is_current(operation) and view.page == "effects" and view.state(EffectPick).effect_id == effect_id,
        )
        note = ("warn", problem) if problem else ("ok", "已處理；僅變更通知結果，不重做原本的功能操作。")
        return lambda: render_effects(view, note)
    await view.run(interaction, work)


def channel_name(view, channel_id: int | None) -> str:
    channel = view.guild.get_channel_or_thread(channel_id) if channel_id else None
    return f"#{channel.name}" if channel is not None else f"頻道 ID {channel_id}"


def render_effects(view, note=None):
    pending = view.services.effects.pending()
    picked = view.state(EffectPick)
    if view.page != "effects":
        picked.confirming = None
    if picked.effect_id not in {item.effect.effect_id for item in pending}:
        picked.effect_id, picked.confirming = (pending[0].effect.effect_id if pending else None), None
    children = view.header("待處理項目", "effects")
    buttons = []
    if not pending:
        children.append(discord.ui.TextDisplay("目前沒有待處理的送出結果。"))
    else:
        more = f"\n還有 {len(pending) - 25} 項，處理完前面的會自動遞補。" if len(pending) > 25 else ""
        children.append(discord.ui.TextDisplay(f"**{len(pending)} 項待處理**\n結果不明表示可能已送達；查核前不會自動重送。{more}"))
        select = view.control(("select", "effect"), lambda: discord.ui.Select(), lambda item, interaction: pick(view, item, interaction))
        select.options = [discord.SelectOption(label=f"{item.kind.label} · {STATES.get(item.effect.status, '待處理')}",
                                               value=item.effect.effect_id, description=channel_name(view, item.effect.channel_id)[:100],
                                               default=item.effect.effect_id == picked.effect_id) for item in pending[:25]]
        children.append(discord.ui.ActionRow(select))
        chosen = next(item for item in pending if item.effect.effect_id == picked.effect_id)
        effect, can_confirm, can_retry = chosen.effect, chosen.confirmable, chosen.retryable
        children.append(discord.ui.TextDisplay(
            f"**{chosen.kind.label}** · {STATES.get(effect.status, '待處理')}\n"
            f"**原頻道**　<#{effect.channel_id}> · <t:{effect.updated_at}:f>"
            + ("" if can_confirm or can_retry else "\n這類項目不能核對或重送，請到原頻道確認並清理後按略過。")))
        async def confirm(interaction):
            await interaction.response.send_modal(ConfirmDelivery(view, view.state(EffectPick).effect_id))
        danger = discord.ButtonStyle.danger
        if picked.confirming:
            label, icon = {"retry": ("確定要重送？無法復原", "sync"), "skip": ("確定要略過此次？無法復原", "next")}[picked.confirming]
            buttons = [
                view.button("effect_sure", label, on_click=lambda i: sure(view, i), style=danger, emoji=ICONS[icon]),
                view.button("effect_unsure", "取消", on_click=lambda i: ask(view, i, None), emoji=ICONS["cancel"]),
            ]
        else:
            if can_confirm:
                buttons.append(view.button("effect_confirm", "核對後標記已送達", on_click=confirm, emoji=ICONS["ok"]))
            if can_retry:
                buttons.append(view.button("effect_retry", "已查核未送達，重送", on_click=lambda i: ask(view, i, "retry"),
                                           style=danger, emoji=ICONS["sync"]))
            buttons.append(view.button("effect_skip", "略過此次", on_click=lambda i: ask(view, i, "skip"), style=danger,
                                       emoji=ICONS["next"]))
    children.extend((discord.ui.Separator(), *footer_note(note), view.actions(*buttons)))
    view.set_container(*children, note=note)


async def pick(view, item, interaction):
    view.state(EffectPick).effect_id = item.values[0]
    await ask(view, interaction, None)


async def ask(view, interaction, action):
    view.state(EffectPick).confirming = action
    await view.run(interaction, lambda operation: _render(view))


async def sure(view, interaction):
    picked = view.state(EffectPick)
    action, picked.confirming = picked.confirming, None
    if action is None:
        return await ask(view, interaction, None)
    await resolve(view, interaction, action, picked.effect_id)


async def _render(view):
    return lambda: render_effects(view)
