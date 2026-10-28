from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence

import discord

from src.core.adapters.discord.brand import ICONS, card_title, rana_says, tone_colour
from src.core.adapters.discord.live import LiveGuild
from src.core.adapters.discord.private import TTL_SECONDS, PrivateResult, PrivateResults
from src.core.adapters.sqlite.db import transaction
from src.core.adapters.sqlite.operations import OperationStore
from src.core.app.actions import ActionRegistry, ActionSpec, Query
from src.core.app.effects import EffectKind, MessageProof, delivered
from src.core.app.ports import Source

RECEIPT = "action.receipt"
REMEMBERED = 200


def receipt_view(
    results: Sequence[Mapping[str, object]], explanation: str = "", *, buttons: Sequence[discord.ui.Item] = (),
) -> discord.ui.LayoutView:
    lines = [f"{ICONS['ok']} **完成**　{item['text']}" if item.get("ok") else f"{ICONS['error']} **未完成**　{item['text']}"
             for item in results]
    note = f"按鈕 {TTL_SECONDS // 60} 分鐘內有效，只有你打得開" if buttons else ""
    items = [card_title(f"{ICONS['ai']} 操作結果", note), discord.ui.TextDisplay("\n".join(lines))]
    if explanation:
        items.append(discord.ui.TextDisplay(rana_says(explanation[:1500])))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*items, accent_colour=tone_colour("ok" if all(item.get("ok") for item in results) else "warn")))
    if buttons:
        view.add_item(discord.ui.ActionRow(*buttons))
    return view


def _reference(channel: object | None, message_id: int) -> discord.MessageReference | None:
    guild = getattr(channel, "guild", None)
    if guild is None or not channel.permissions_for(guild.me).read_message_history:
        return None
    return discord.MessageReference(message_id=message_id, channel_id=channel.id, fail_if_not_exists=False)


class DiscordReceipts:
    def __init__(self, operations: OperationStore, live: LiveGuild, client: discord.Client, registry: ActionRegistry) -> None:
        self._operations = operations
        self._live = live
        self._client = client
        self._registry = registry
        self.private_results = PrivateResults(self._authorize, self._render, self._label)
        self._sent: OrderedDict[int, object | None] = OrderedDict()
        self._lock = asyncio.Lock()

    def effect_kind(self) -> EffectKind:
        return EffectKind(RECEIPT, "AI 操作結果卡", proof=MessageProof(), retry=self._retry)

    async def _retry(self, effect_id: str, still_current: Callable[[], bool]) -> str | None:
        return delivered(await self.resend(effect_id, allow_unknown=True, still_current=still_current))

    def private(self, source: Source, spec: ActionSpec, params: Mapping[str, object]) -> str:
        return self.private_results.issue(source, spec.name, params)

    def _view(self, results: Sequence[Mapping[str, object]], explanation: str = "") -> discord.ui.LayoutView:
        buttons = self.private_results.make_buttons(item["private_ref"] for item in results if "private_ref" in item)
        return receipt_view(results, explanation, buttons=buttons)

    def _bind(self, results: Sequence[Mapping[str, object]], message_id: int) -> None:
        for item in results:
            if "private_ref" in item:
                self.private_results.bind_message(item["private_ref"], message_id)

    async def publish(self, source: Source, request_id: str, results: Sequence[Mapping[str, object]]) -> bool:
        async with self._lock:
            return await self._publish(source, request_id, results)

    async def _publish(self, source: Source, request_id: str, results: Sequence[Mapping[str, object]]) -> bool:
        live, operations = self._live, self._operations
        if source.guild_id != live.guild_id or live.send_problem(source.channel_id, source.actor_id):
            return False
        channel = live.channel(source.channel_id)
        effect_id = f"request:{request_id}:receipt"
        if (existing := operations.get_effect(effect_id)) is not None:
            self._sent[source.message_id] = (
                self._client.get_partial_messageable(existing.channel_id).get_partial_message(existing.message_id)
                if existing.message_id else None
            )
            return True
        nonce = uuid.uuid4().hex[:24]
        with transaction(operations.db):
            operations.create_effect(effect_id, RECEIPT, channel.id,
                                     {"results": results, "source_message_id": source.message_id}, nonce=nonce)
            operations.update_effect(effect_id, "unknown")
        self._sent[source.message_id] = None
        while len(self._sent) > REMEMBERED:
            self._sent.popitem(last=False)
        try:
            message = await channel.send(view=self._view(results), nonce=nonce,
                                         reference=_reference(channel, source.message_id),
                                         allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logging.exception("Action receipt delivery is unknown; not resending.")
            return True
        self._sent[source.message_id] = message
        self._bind(results, message.id)
        with transaction(operations.db):
            operations.update_effect(effect_id, "done", message_id=message.id)
        return True

    async def explain(self, source: Source, text: str, still_current: Callable[[], bool]) -> None:
        async with self._lock:
            message = self._sent.get(source.message_id)
            if message is None or not still_current():
                return
            request = self._operations.get_request(source.message_id)
            effect = self._operations.get_effect(f"request:{request.request_id}:receipt") if request is not None else None
            if effect is None:
                return
            try:
                await message.edit(view=self._view(effect.payload["results"], text),
                                   allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logging.exception("Action receipt explanation could not be updated.")

    async def resend(self, effect_id: str, *, allow_unknown: bool = False,
                     still_current: Callable[[], bool] | None = None) -> bool:
        async with self._lock:
            return await self._resend(effect_id, allow_unknown, still_current)

    async def _resend(self, effect_id: str, allow_unknown: bool, still_current: Callable[[], bool] | None) -> bool:
        operations = self._operations
        effect = operations.get_effect(effect_id)
        if effect is None or effect.kind != RECEIPT:
            return False
        if (allow_unknown and still_current is None) or (still_current is not None and not still_current()):
            return False
        with transaction(operations.db):
            if effect.status != "pending":
                if not allow_unknown or effect.status not in {"unknown", "failed"}:
                    return False
                operations.update_effect(effect_id, "pending", retry=True)
            operations.update_effect(effect_id, "unknown")
        results = [item.copy() for item in effect.payload["results"]]
        for item in results:
            if (token := item.get("private_ref")) and not self.private_results.can_bind(token):
                item.pop("private_ref")
        source_message_id = effect.payload["source_message_id"]
        try:
            message = await self._client.get_partial_messageable(effect.channel_id).send(
                view=self._view(results), nonce=effect.nonce,
                reference=_reference(self._client.get_channel(effect.channel_id), source_message_id),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logging.exception("Explicit receipt resend is still unconfirmed.")
            return False
        self._bind(results, message.id)
        with transaction(operations.db):
            operations.update_effect(effect_id, "done", message_id=message.id)
        self._sent[source_message_id] = message
        return True

    def _query(self, kind: str) -> tuple[ActionSpec, Query] | None:
        spec = self._registry.spec(kind)
        return (spec, spec.effect) if spec is not None and isinstance(spec.effect, Query) else None

    def _label(self, kind: str) -> str:
        return self._query(kind)[1].label

    async def _authorize(self, entry: PrivateResult, interaction: discord.Interaction) -> str | None:
        if (query := self._query(entry.kind)) is None:
            return "這份私人結果已失效，請重新查詢。"
        actor = await self._live.fresh_actor(interaction.channel_id, interaction.user.id)
        if actor is None:
            return "無法確認目前的伺服器身分。"
        if problem := self._live.act_problem(interaction.guild_id, interaction.channel_id, actor):
            return problem
        if query[0].admin and not actor.admin:
            return "此操作僅限伺服器管理員使用。"
        return None

    async def _render(self, entry: PrivateResult, interaction: discord.Interaction) -> Mapping[str, object]:
        query = self._query(entry.kind)
        actor = self._live.actor(interaction.channel_id, interaction.user.id)
        if query is None or actor is None or interaction.guild_id != self._live.guild_id:
            raise LookupError("private result source is unavailable")
        return await query[1].render(actor)
