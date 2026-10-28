from __future__ import annotations

import copy
import asyncio
import logging
import time
from types import SimpleNamespace
from uuid import uuid4

import discord

from src.core.adapters.discord import channels
from src.core.adapters.discord.brand import PENDING_CHECK, note_text
from src.games import access, rana, tables
from src.games.blackjack import Round
from src.games.hub import publish_hub
from src.games.table_runtime import delete_message
from src.games.tables import LIVE_TABLES, BlackjackTableView, SeatView, SlotTableView
from src.core.adapters.sqlite.operations import OperationStore
from src.core.adapters.sqlite.db import transaction

SEAT_GONE = "這張賭桌已失效，這局沒有結算；請從遊戲大廳重新開桌。"


class GameOperations:
    def __init__(self, bot) -> None:
        self.bot = bot
        self.store = bot.games
        self.records = OperationStore(self.store.db)
        self._active_tasks: set[asyncio.Task] = set()

    def pending_tasks(self) -> set[asyncio.Task]:
        return set(self._active_tasks)

    async def publish_hub(self, guild: discord.Guild, *, operation_id: str | None = None, still_current=None) -> str | None:
        task = asyncio.current_task()
        self._active_tasks.add(task)
        try:
            return await publish_hub(guild, self.store, operation_id=operation_id, still_current=still_current)
        finally:
            self._active_tasks.discard(task)

    async def close_idle(self, table: SeatView) -> None:
        async with table._lock:
            if table.closed or LIVE_TABLES.get(table.message.id) is not table:
                return
            store, user_id = table.store, table.player_id
            operation_id = f"games.idle:{table.message.id}"
            game = None
            if isinstance(table, BlackjackTableView) and not table.between_rounds:
                game = copy.deepcopy(table.game)
                while not game.done:
                    game.apply("stand")
            with transaction(store.db):
                if game is None:
                    self._create_presentation(table, operation_id, "delete")
                store._close_table(user_id)
            table.state_token = store.advance_state(user_id)
            table.closed = True
            if game is None:
                await self._delete(table, operation_id)
            else:
                table.game = game
                table.notice = note_text(("warn", f"閒置 {tables.IDLE_SECONDS // 60} 分鐘，已代你停牌結算，賭桌關閉。"))
                table.render()
                if links := access.hub_link(self.bot):
                    table.add_item(discord.ui.ActionRow(*links))
                try:
                    await table.message.edit(view=table)
                except discord.HTTPException:
                    logging.warning("Idle game table %s could not be updated.", table.message.id)
            LIVE_TABLES.pop(table.message.id, None)

    def table(self, user_id: int) -> SeatView | None:
        posted = self.store.table_of(user_id)
        return LIVE_TABLES.get(posted[1]) if posted else None

    def snapshot(self, user_id: int) -> dict:
        return {**self._snapshot(self.table(user_id)), "state_token": self.store.state_token(user_id),
                "busy": self.store.player_lock(user_id).locked()}

    @staticmethod
    def _snapshot(table: SeatView | None, *, candidate=None, token: str | None = None) -> dict:
        if table is None or table.closed:
            return {"state_token": None, "game": None, "legal_actions": ["open_table"]}
        value = {"state_token": token or table.state_token, "game": table.key}
        if isinstance(table, BlackjackTableView):
            game = candidate if candidate is not None else table.game
            value["phase"] = game.phase if game else "between_rounds"
            value["legal_actions"] = list(game.legal_actions()) if game and not game.done else ["deal", "leave_table"]
            if game:
                visible = game.dealer if game.done else game.dealer[:1]
                value["dealer_cards"] = [f"{card.rank}{card.suit}" for card in visible]
                value["player_cards"] = [f"{card.rank}{card.suit}" for card in game.player]
                if game.done:
                    value["outcome"] = game.result()
        else:
            value.update(phase="between_rounds", legal_actions=["deal", "again", "leave_table"])
            if table.multiplier is not None:
                value.update(result=table.result, summary=table.summary, multiplier=table.multiplier)
        value["legal_actions"].append("open_table")
        return value

    async def _context(self, source, table: SeatView | None, *, refresh: bool = True):
        bot = self.bot
        actor = source.user if hasattr(source, "user") else source.author
        guild_id = getattr(source, "guild_id", None) or source.guild.id
        channel_id = getattr(source, "channel_id", None) or source.channel.id
        if refresh:
            try:
                guild, actor = await channels.fetch_current_member(bot, guild_id, actor.id)
            except ValueError as exc:
                return None, str(exc)
            except discord.HTTPException:
                return None, "無法確認你目前的遊戲權限，請稍後再試。"
        else:
            guild = bot.get_guild(guild_id)
            if guild is None:
                return None, "目前無法確認最新的伺服器狀態，請稍後再試。"
            actor = guild.get_member(actor.id)
            if actor is None:
                return None, "目前無法確認你的伺服器身分，請稍後再試。"
        channel = guild.get_channel_or_thread(channel_id)
        if channel is None:
            return None, "目前無法確認最新的遊戲頻道，請稍後再試。"
        permissions = channel.permissions_for(actor)
        if not permissions.view_channel or not permissions.send_messages:
            return None, "你目前無法在這個頻道操作賭桌。"
        context = SimpleNamespace(client=bot, user=actor, guild=guild, channel=channel,
                                  guild_id=guild_id, channel_id=channel_id)
        if table is not None and actor.id != table.player_id:
            return context, "只有開桌的玩家可以操作這張賭桌。"
        return context, access.play_problem(context)

    @staticmethod
    def _failure(code: str, text: str) -> dict:
        return {"ok": False, "code": code, "text": text}

    def _remember(self, operation_id: str, inputs: dict, result: dict, *, status: str = "done") -> dict:
        with transaction(self.store.db):
            self.records.complete(operation_id, "game_action", inputs, result, status=status)
        return result

    async def execute(
        self, source, action: str, *, game: str | None = None, expected_token: str | None = None,
        operation_id: str | None = None, table: SeatView | None = None, ui: bool = False, still_current=None,
    ) -> dict:
        task = asyncio.current_task()
        self._active_tasks.add(task)
        try:
            return await self._execute(source, action, game=game, expected_token=expected_token,
                                       operation_id=operation_id, table=table, ui=ui, still_current=still_current)
        finally:
            self._active_tasks.discard(task)

    async def _execute(self, source, action, *, game, expected_token, operation_id, table, ui, still_current):
        actor = source.user if hasattr(source, "user") else source.author
        user_id = table.player_id if table is not None else actor.id
        lock = self.store.player_lock(user_id)
        if lock.locked():
            if not ui:
                return self._failure("busy", "你的賭桌正在處理上一個動作，請稍候。")
            if not source.response.is_done():
                await source.response.defer()
        if not await tables.take_turn(lock, tables.BUSY_SECONDS):
            result = self._failure("busy", tables.BUSY_TEXT)
            if ui:
                await tables.send_notice(source, result["text"])
            return result
        try:
            if ui and isinstance(source, discord.Interaction) and not source.response.is_done():
                await source.response.defer()
            context, problem = await self._context(source, table, refresh=not ui)
            if not problem and still_current is not None and not still_current():
                problem = "你目前沒有執行這個 AI 操作的權限。"
            if problem:
                result = self._failure("forbidden", problem)
                if ui:
                    await tables.send_notice(source, problem, items=access.hub_link(self.bot))
                return result
            operation_id = operation_id or (f"interaction:{source.id}" if isinstance(source.id, int) else uuid4().hex)
            inputs = {"actor_id": user_id, "guild_id": context.guild_id if isinstance(context.guild_id, int) else None,
                      "channel_id": context.channel_id if isinstance(context.channel_id, int) else None,
                      "action": action, "game": game, "state_token": expected_token}
            try:
                prior = self.records.replay(operation_id, "game_action", inputs)
            except RuntimeError:
                prior = self.records.get(operation_id)
            if prior is not None:
                if ui and not source.response.is_done():
                    await source.response.defer()
                return prior.result
            table = table or self.table(user_id)
            if table is not None:
                table.touched = time.monotonic()
            current_token = self.store.state_token(user_id)
            if table is not None and self.store.table_of(user_id) is None:
                table.closed = True
                result = self._remember(operation_id, inputs, self._failure("seat_gone", SEAT_GONE))
            elif expected_token != current_token:
                result = self._remember(operation_id, inputs, self._failure("stale", "賭桌狀態已經改變，請查看目前畫面再操作。"))
            elif action == "open_table":
                return await self._open(source, context, table, game, operation_id, inputs, ui, still_current)
            elif table is None or table.closed:
                result = self._remember(operation_id, inputs, self._failure("no_table", "請先從遊戲大廳開桌。"))
            else:
                result = self._mutate(table, action, operation_id, inputs)
                if result["ok"] and ("outcome" in result or "multiplier" in result):
                    self._spawn_line(context, table, result)
            if ui:
                if result["ok"]:
                    if table.closed:
                        if not source.response.is_done():
                            await source.response.defer()
                        await self._delete(table, operation_id)
                    else:
                        await self._present(table, source, operation_id)
                else:
                    await tables.send_notice(source, result["text"], "error" if result["code"] == "seat_gone" else "warn",
                                             items=access.hub_link(self.bot)
                                             if result["code"] in {"seat_gone", "no_table"} else ())
            elif result["ok"] and table is not None:
                if table.closed:
                    await self._delete(table, operation_id)
                else:
                    try:
                        await self._present(table, None, operation_id)
                    except discord.HTTPException:
                        logging.exception("Committed game result could not be shown; it will not be played again.")
            return result
        finally:
            lock.release()

    def _mutate(self, table, action, operation_id, inputs) -> dict:
        candidate, slot_state = None, None
        token = uuid4().hex
        failure = None
        if action == "leave_table":
            if not table.between_rounds:
                failure = self._failure("illegal", "這局尚未結束，請先完成牌局。")
        elif action in {"deal", "again"}:
            if not table.between_rounds:
                failure = self._failure("illegal", "這局尚未結束，請先完成牌局。")
            elif isinstance(table, BlackjackTableView):
                candidate = Round(tables.shoe())
            else:
                previous_frames = table.frames
                multiplier, result, summary = table.play()
                slot_state = (multiplier, result, summary, table.frames)
                table.frames = previous_frames
        elif not isinstance(table, BlackjackTableView) or table.between_rounds or action not in table.game.legal_actions():
            failure = self._failure("illegal", "這張賭桌目前不能進行這個動作。")
        else:
            candidate = copy.deepcopy(table.game)
            candidate.apply(action)
        if failure is not None:
            return self._remember(operation_id, inputs, failure)
        result = {"ok": True, "code": "done", "text": "已離桌。" if action == "leave_table" else "遊戲動作已完成。"}
        result.update(self._snapshot(None if action == "leave_table" else table, candidate=candidate, token=token))
        if action == "leave_table":
            result["state_token"] = token
        if slot_state is not None:
            result.update(result=slot_state[1], summary=slot_state[2], multiplier=slot_state[0])
        if table.message is not None:
            result["resource_id"] = str(table.message.id)
            result["message_url"] = getattr(table.message, "jump_url", None)
            if not isinstance(result["message_url"], str):
                result.pop("message_url")
        with transaction(self.store.db):
            gone = self.store.table_of(table.player_id) is None
            if not gone:
                if action == "leave_table":
                    self.store._close_table(table.player_id)
                self.records.complete(operation_id, "game_action", inputs, result, resource_id=result.get("resource_id"))
                self._create_presentation(table, operation_id, "delete" if action == "leave_table" else "edit")
        if gone:
            table.closed = True
            return self._remember(operation_id, inputs, self._failure("seat_gone", SEAT_GONE))
        self.store.advance_state(table.player_id, token)
        table.state_token, table.notice, table.taunt = token, None, None
        if table.line_task:
            table.line_task.cancel()
        if action == "leave_table":
            table.closed = True
            if table.message is not None:
                LIVE_TABLES.pop(table.message.id, None)
        elif isinstance(table, BlackjackTableView):
            table.game = candidate
        else:
            table.multiplier, table.result, table.summary, table.frames = slot_state
        table.render()
        return result

    def _spawn_line(self, context, table: SeatView, result: dict) -> None:
        data = {"遊戲": table.title, "玩家暱稱": context.user.display_name}
        if isinstance(table, BlackjackTableView):
            data["結果"] = tables.OUTCOMES[result["outcome"]]
        else:
            data["倍數"] = result["multiplier"]
        table.line_task = rana.spawn(self._line(context, table, table.state_token, data))

    async def _line(self, context, table: SeatView, token: str, data: dict) -> None:
        if not rana.ready(context.client):
            return
        if not (text := await rana.round_line(context.client, context.channel_id, data)):
            return
        async with table._lock:
            if table.closed or table.state_token != token or table.message is None:
                return
            table.taunt = text
            table.render()
            try:
                await table.message.edit(view=table)
            except discord.HTTPException:
                logging.warning("Rana's line on a %s round could not be shown.", table.title)

    def _create_presentation(self, table: SeatView, operation_id: str, action: str) -> None:
        posted = self.store.table_of(table.player_id)
        message_id = table.message.id if table.message is not None else None
        channel_id = posted[0] if posted else getattr(table.message, "channel", SimpleNamespace(id=None)).id
        if isinstance(channel_id, int) and isinstance(message_id, int):
            self.records.create_effect(f"{operation_id}:table-{action}", f"game_table_{action}", channel_id,
                                       {"user_id": table.player_id, "message_id": message_id}, operation_id=operation_id)

    def _effect_status(self, effect_id: str, status: str, **kwargs) -> None:
        if self.records.get_effect(effect_id) is not None:
            with transaction(self.store.db):
                self.records.update_effect(effect_id, status, **kwargs)

    async def _present(self, table: SeatView, interaction, operation_id: str) -> None:
        effect_id = f"{operation_id}:table-edit"
        self._effect_status(effect_id, "unknown")
        if interaction is not None:
            if isinstance(table, SlotTableView):
                await table.reveal(interaction)
            else:
                if interaction.response.is_done():
                    await interaction.edit_original_response(view=table)
                else:
                    await interaction.response.edit_message(view=table)
        elif table.message is not None:
            await table.message.edit(view=table)
        self._effect_status(effect_id, "done")

    async def _delete(self, table: SeatView, operation_id: str) -> None:
        if table.message is None:
            return
        effect_id = f"{operation_id}:table-delete"
        self._effect_status(effect_id, "unknown")
        try:
            await table.message.delete()
        except discord.NotFound:
            pass
        except discord.HTTPException:
            logging.warning("Game table %s could not be deleted.", table.message.id)
            return
        self._effect_status(effect_id, "done")

    async def _open(self, source, context, previous, game, operation_id, inputs, ui, still_current) -> dict:
        if game not in tables.TABLES:
            result = self._remember(operation_id, inputs, self._failure("game", "請指定要開 21 點或拉霸賭桌。"))
            if ui:
                await tables.send_notice(source, result["text"])
            return result
        for orphan in self.records.effects(status=("pending", "unknown"), kind="game_table_open"):
            if orphan.payload.get("user_id") != context.user.id:
                continue
            if orphan.message_id is not None:
                await delete_message(self.bot, orphan.channel_id, orphan.message_id)
            with transaction(self.store.db):
                self.records.update_effect(orphan.effect_id, "skipped", error="superseded_by_player")
                if (receipt := self.records.get(orphan.operation_id)) is not None and receipt.status == "running":
                    self.records.complete(receipt.operation_id, receipt.kind, receipt.inputs,
                                          self._failure("superseded", "玩家已重新開桌，這次開桌已作廢。"), status="failed")
        new = tables.TABLES[game](self.store, context.user.id, avatar=context.user.display_avatar.url)
        new.state_token = uuid4().hex
        new.render()
        effect_id = f"{operation_id}:table-send"
        uncertain = self._failure("unknown", f"賭桌發送{PENDING_CHECK}")
        with transaction(self.store.db):
            self.records.create_effect(effect_id, "game_table_open", context.channel_id,
                                       {"user_id": context.user.id, "game": game,
                                        "source_message_id": source.id}, operation_id=operation_id)
            self.records.update_effect(effect_id, "unknown")
            self.records.complete(operation_id, "game_action", inputs, uncertain, status="running")
        opening_token = self.store.advance_state(context.user.id)
        if previous is not None:
            previous.state_token = opening_token
            previous.render()
        try:
            new.message = await context.channel.send(view=new)
        except discord.HTTPException:
            logging.exception("Game table opening result is unknown; keeping the previous table.")
            if ui:
                await tables.send_notice(source, uncertain["text"])
            return uncertain
        _, problem = await self._context(source, previous, refresh=not ui)
        if not problem and still_current is not None and not still_current():
            problem = "你目前沒有執行這個 AI 操作的權限。"
        if problem:
            await delete_message(self.bot, context.channel_id, new.message.id)
            result = self._failure("forbidden", problem)
            with transaction(self.store.db):
                self.records.update_effect(effect_id, "skipped", message_id=new.message.id, error=problem)
                self.records.complete(operation_id, "game_action", inputs, result, status="failed")
            if ui:
                await tables.send_notice(source, problem, items=access.hub_link(self.bot))
            return result
        result = {"ok": True, "code": "done", "text": "賭桌已開啟。", **self._snapshot(new),
                  "resource_id": str(new.message.id)}
        if isinstance(getattr(new.message, "jump_url", None), str):
            result["message_url"] = new.message.jump_url
        replaced = self.store.table_of(context.user.id)
        with transaction(self.store.db):
            self.store.replace_table(context.user.id, game, context.channel_id, new.message.id)
            self.records.complete(operation_id, "game_action", inputs, result, resource_id=str(new.message.id))
            self.records.update_effect(effect_id, "done", message_id=new.message.id)
            if previous is not None:
                self._create_presentation(previous, operation_id, "delete")
        if previous is not None:
            previous.closed = True
        self.store.advance_state(context.user.id, new.state_token)
        if replaced is not None:
            LIVE_TABLES.pop(replaced[1], None)
        LIVE_TABLES[new.message.id] = new
        if replaced is not None:
            if previous is not None and previous.message is not None:
                await self._delete(previous, operation_id)
            else:
                await delete_message(self.bot, *replaced[:2])
        return result
