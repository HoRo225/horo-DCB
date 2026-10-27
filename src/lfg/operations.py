from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from uuid import uuid4
from types import SimpleNamespace

import aiohttp
import discord

from src.brand import PENDING_CHECK, send_notice
from src.cards import T3_FACES, fetch_avatar, render, t3
from src.discord_utils import avatar_url, fetch_current_member
from src.lfg import views
from src.lfg.store import LfgCard
from src.operations import OperationStore
from src.state import transaction

LFG_SECONDS = 2 * 60 * 60
NOTICE_TONES = {"expired": "deadline", "member": "info"}


class LfgOperations:
    def __init__(self, bot) -> None:
        self.bot, self.store = bot, bot.lfg
        self.records = OperationStore(self.store.db)
        self._lock = asyncio.Lock()
        self._active_tasks: set[asyncio.Task] = set()

    def pending_tasks(self) -> set[asyncio.Task]:
        return set(self._active_tasks)

    @staticmethod
    def _actor(source):
        return source.user if hasattr(source, "user") else source.author

    @staticmethod
    def _guild_id(source):
        return getattr(source, "guild_id", None) or source.guild.id

    @staticmethod
    def _channel_id(source):
        return getattr(source, "channel_id", None) or source.channel.id

    def _live_source(self, source):
        guild = self.bot.get_guild(self._guild_id(source))
        if guild is None:
            raise ValueError("current guild cache is unavailable")
        channel = guild.get_channel_or_thread(self._channel_id(source))
        if channel is None:
            raise ValueError("current source channel is unavailable")
        actor = self._actor(source)
        live = SimpleNamespace(id=source.id, user=actor, author=actor, guild=guild, guild_id=guild.id,
                               channel=channel, channel_id=channel.id)
        if hasattr(source, "response"):
            live.response, live.followup, live.message = source.response, source.followup, source.message
            live.edit_original_response = source.edit_original_response
            live.delete_original_response = source.delete_original_response
        return live

    @staticmethod
    def _id(source, operation_id):
        return operation_id or (f"interaction:{source.id}" if isinstance(source.id, int) else uuid4().hex)

    @staticmethod
    def _failure(code, text):
        return {"ok": False, "code": code, "text": text}

    def _inputs(self, source, **kwargs):
        guild_id, channel_id = self._guild_id(source), self._channel_id(source)
        return {"actor_id": self._actor(source).id,
                "guild_id": guild_id if isinstance(guild_id, int) else None,
                "channel_id": channel_id if isinstance(channel_id, int) else None, **kwargs}

    async def _permission(self, source, target_channel_id):
        if reason := self.bot.unavailable_reason(self._guild_id(source)):
            return reason
        guild = self.bot.get_guild(self._guild_id(source))
        if guild is None:
            return "目前無法確認最新的伺服器狀態，請稍後再試。"
        for attempt in range(2):
            actor = guild.get_member(self._actor(source).id) if hasattr(source, "response") else None
            if actor is None:
                try:
                    actor = await guild.fetch_member(self._actor(source).id)
                except discord.HTTPException:
                    return "無法確認你目前的伺服器身分，請稍後再試。"
            channel = guild.get_channel_or_thread(target_channel_id)
            if channel is None or not channel.permissions_for(actor).view_channel:
                return "你目前無法查看這個揪團所在的頻道。"
            if (isinstance(channel, discord.Thread) and channel.is_private()
                    and not channel.permissions_for(actor).manage_threads):
                try:
                    await channel.fetch_member(actor.id)
                except discord.HTTPException:
                    return "你目前無法查看這個揪團所在的私人討論串。"
            if self.bot.get_guild(guild.id) is guild:
                source.guild, source.user, source.author = guild, actor, actor
                source.channel = guild.get_channel_or_thread(self._channel_id(source))
                return None if source.channel is not None else "目前無法確認最新的頻道狀態。"
            guild = self.bot.get_guild(guild.id)
            if guild is None:
                return "目前無法確認最新的伺服器狀態，請稍後再試。"
        return "伺服器狀態正在更新，請稍後再試。"

    def _replay(self, operation_id, kind, inputs):
        try:
            return self.records.replay(operation_id, kind, inputs)
        except RuntimeError:
            return self.records.get(operation_id)

    def _remember(self, operation_id, kind, inputs, result):
        with transaction(self.store.db):
            self.records.complete(operation_id, kind, inputs, result)
        return result

    async def _withdraw(self, source, message=None) -> bool:
        try:
            if hasattr(source, "response"):
                await source.delete_original_response()
            else:
                await message.delete()
        except discord.NotFound:
            pass
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError):
            logging.exception("LFG reply could not be deleted.")
            return False
        return True

    async def _reject(self, source, result):
        if hasattr(source, "response"):
            await self._withdraw(source)
            await send_notice(source, result["text"])
        return result

    async def _initial(self, source):
        if reason := self.bot.unavailable_reason(self._guild_id(source)):
            result = self._failure("forbidden", reason)
            if hasattr(source, "response"):
                await send_notice(source, reason)
            return result
        if hasattr(source, "response") and not source.response.is_done():
            await source.response.defer()
        return None

    async def create(self, source, game: str, size: int, note: str | None = None, operation_id: str | None = None, *, still_current=None) -> dict:
        task = asyncio.current_task()
        self._active_tasks.add(task)
        try:
            return await self._create(source, game, size, note, self._id(source, operation_id), still_current)
        finally:
            self._active_tasks.discard(task)

    async def _create(self, source, game, size, note, operation_id, still_current):
        if result := await self._initial(source):
            return result
        inputs = self._inputs(source, game=game, size=size, note=note)
        async with self._lock:
            try:
                source = self._live_source(source)
            except ValueError:
                return await self._reject(source, self._failure("forbidden", "目前無法確認最新的伺服器或頻道狀態。"))
            if still_current is not None and not still_current():
                return self._failure("forbidden", "你目前沒有執行這個 AI 操作的權限。")
            if problem := await self._permission(source, self._channel_id(source)):
                return await self._reject(source, self._failure("forbidden", problem))
            if still_current is not None and not still_current():
                return self._failure("forbidden", "你目前沒有執行這個 AI 操作的權限。")
            if prior := self._replay(operation_id, "lfg_create", inputs):
                return prior.result
            if (not isinstance(game, str) or not 1 <= len(game.strip()) <= 50
                    or not isinstance(size, int) or isinstance(size, bool) or not 2 <= size <= 10
                    or (note is not None and (not isinstance(note, str) or not 1 <= len(note.strip()) <= 100))):
                return await self._reject(source, self._remember(
                    operation_id, "lfg_create", inputs,
                    self._failure("parameters", "遊戲名稱限 1–50 字、人數 2–10 人、備註限 1–100 字。")))
            card = LfgCard(0, self._channel_id(source), self._actor(source).id, game.strip(), size,
                           note.strip() if note else None, [self._actor(source).id], int(time.time()) + LFG_SECONDS)
            effect_id = f"{operation_id}:lfg-create"
            unknown = self._failure("unknown", f"揪團發送{PENDING_CHECK}")
            with transaction(self.store.db):
                self.records.create_effect(effect_id, "lfg_create", card.channel_id, dataclasses.asdict(card),
                                           operation_id=operation_id)
                self.records.update_effect(effect_id, "unknown")
                self.records.complete(operation_id, "lfg_create", inputs, unknown, status="running")
            try:
                view = views.card_view(card, avatar=self._actor(source).display_avatar.url)
                message = (await source.edit_original_response(view=view) if hasattr(source, "response")
                           else await source.channel.send(view=view))
            except (discord.HTTPException, TimeoutError, aiohttp.ClientError):
                logging.exception("LFG creation result is unknown; not resending.")
                return await self._reject(source, unknown)
            card.message_id = message.id
            problem = await self._permission(source, card.channel_id)
            if not problem and still_current is not None and not still_current():
                problem = "你目前沒有執行這個 AI 操作的權限。"
            if problem:
                removed = await self._withdraw(source, message)
                with transaction(self.store.db):
                    if removed:
                        result = self._failure("forbidden", problem)
                        self.records.complete(operation_id, "lfg_create", inputs, result)
                    else:
                        result = self._failure("unknown", f"{problem} 揪團尚未啟用，{PENDING_CHECK}")
                    self.records.update_effect(effect_id, "skipped" if removed else "unknown",
                                               message_id=message.id, error=problem)
                if hasattr(source, "response"):
                    await send_notice(source, result["text"])
                return result
            game = discord.utils.escape_markdown(card.game)
            result = {"ok": True, "code": "done", "text": f"已建立揪團「{game}」（{len(card.members)} / {card.size} 人）。",
                      "resource_id": str(card.message_id),
                      "message_url": f"https://discord.com/channels/{self._guild_id(source)}/{card.channel_id}/{card.message_id}"}
            with transaction(self.store.db):
                self.store._add(card)
                self.records.complete(operation_id, "lfg_create", inputs, result, resource_id=str(card.message_id))
                self.records.update_effect(effect_id, "done", message_id=message.id)
            cog = self.bot.get_cog("LfgCog")
            if cog is not None:
                cog.schedule(card, new=True)
            return result

    async def execute(self, source, action: str, message_id: int, operation_id: str | None = None, *, still_current=None) -> dict:
        task = asyncio.current_task()
        self._active_tasks.add(task)
        try:
            return await self._execute(source, action, message_id, self._id(source, operation_id), still_current)
        finally:
            self._active_tasks.discard(task)

    async def _execute(self, source, action, message_id, operation_id, still_current):
        if result := await self._initial(source):
            return result
        inputs = self._inputs(source, action=action, message_id=message_id)
        departed = None
        async with self._lock:
            try:
                source = self._live_source(source)
            except ValueError:
                return self._failure("forbidden", "目前無法確認最新的伺服器或頻道狀態。")
            if still_current is not None and not still_current():
                return self._failure("forbidden", "你目前沒有執行這個 AI 操作的權限。")
            card = self.store.get(message_id)
            effect = self.records.get_effect(f"{operation_id}:lfg-edit")
            target = card.channel_id if card else effect.channel_id if effect else self._channel_id(source)
            if problem := await self._permission(source, target):
                result = self._failure("forbidden", problem)
                if hasattr(source, "response"):
                    await send_notice(source, problem)
                return result
            if still_current is not None and not still_current():
                return self._failure("forbidden", "你目前沒有執行這個 AI 操作的權限。")
            if prior := self._replay(operation_id, "lfg_action", inputs):
                return prior.result
            ended, failure = None, None
            actor_id = self._actor(source).id
            if card is None:
                failure = self._failure("ended", "這個揪團已經結束了。")
            elif card.expires_at <= time.time():
                ended = views.ENDED
                failure = self._failure("expired", "這個揪團已過期，請用 /揪團 重新開一團。")
            elif action == "join":
                if actor_id in card.members:
                    failure = self._failure("member", "你已經在隊伍裡了。")
                elif len(card.members) >= card.size:
                    failure = self._failure("full", "這個揪團已經額滿了。")
                else:
                    card.members.append(actor_id)
                    if len(card.members) == card.size:
                        ended = views.DEPARTED
            elif action == "leave":
                if actor_id == card.host_id:
                    failure = self._failure("host", "發起人請按「取消揪團」。")
                elif actor_id not in card.members:
                    failure = self._failure("member", "你不在這個隊伍裡。")
                else:
                    card.members.remove(actor_id)
            elif action in {"go", "cancel"}:
                if actor_id != card.host_id:
                    failure = self._failure("host", "只有發起人可以按「出發」或「取消揪團」。")
                else:
                    ended = views.DEPARTED if action == "go" else views.CANCELLED
            else:
                failure = self._failure("action", "不支援這個揪團動作。")
            if failure and ended is None:
                result = self._remember(operation_id, "lfg_action", inputs, failure)
            else:
                game = discord.utils.escape_markdown(card.game)
                text = {"join": f"已加入「{game}」（{len(card.members)} / {card.size} 人）。", "leave": f"已退出「{game}」。",
                        "go": f"揪團「{game}」已出發。", "cancel": f"揪團「{game}」已取消。"}.get(action)
                result = failure or {"ok": True, "code": "done", "text": text, "resource_id": str(message_id),
                                     "message_url": f"https://discord.com/channels/{self._guild_id(source)}/{card.channel_id}/{message_id}"}
                with transaction(self.store.db):
                    if ended is not None:
                        self.store._finish(message_id)
                    else:
                        self.store._set_members(message_id, card.members)
                    self.records.complete(operation_id, "lfg_action", inputs, result, resource_id=str(message_id))
                    self._presentation_effect(operation_id, card, ended)
                    if ended == views.DEPARTED:
                        self.records.create_effect(f"{operation_id}:lfg-depart", "lfg_depart", card.channel_id,
                                                   dataclasses.asdict(card), operation_id=operation_id)
                        self.records.update_effect(f"{operation_id}:lfg-depart", "unknown")
                await self._show(source, card, ended, operation_id)
                if ended == views.DEPARTED:
                    departed = card
            if hasattr(source, "response") and not result["ok"]:
                await send_notice(source, result["text"], NOTICE_TONES.get(result["code"], "warn"))
        if departed:
            await self._depart(source, departed, operation_id, party=True)
        return result

    def _presentation_effect(self, operation_id, card, ended):
        self.records.create_effect(f"{operation_id}:lfg-edit", "lfg_edit", card.channel_id,
                                   {"card": dataclasses.asdict(card), "ended": ended}, operation_id=operation_id)

    def _status(self, effect_id, status, **kwargs):
        with transaction(self.store.db):
            self.records.update_effect(effect_id, status, **kwargs)

    async def cheer(self, message_id: int, line: str) -> None:
        async with self._lock:
            card = self.store.get(message_id)
            if card is None or card.expires_at <= time.time() or len(card.members) >= card.size:
                return
            with transaction(self.store.db):
                self.store._set_cheer(message_id, line)
            card.cheer = line
            guild = self.bot.get_guild(getattr(self.bot, "guild_id", None))
            try:
                await self.bot.get_partial_messageable(card.channel_id).get_partial_message(message_id).edit(
                    view=views.card_view(card, avatar=avatar_url(guild, card.host_id)))
            except discord.HTTPException:
                logging.warning("Rana's LFG cheer for %s could not be shown.", message_id)

    async def _show(self, source, card, ended, operation_id):
        effect_id = f"{operation_id}:lfg-edit"
        self._status(effect_id, "unknown")
        try:
            view = views.card_view(card, ended, avatar_url(source.guild, card.host_id))
            if hasattr(source, "response") and source.message.id == card.message_id:
                await source.edit_original_response(view=view)
            else:
                await self.bot.get_partial_messageable(card.channel_id).get_partial_message(card.message_id).edit(view=view)
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError):
            logging.exception("LFG card %s could not show its committed result.", card.message_id)
            return
        self._status(effect_id, "done", message_id=card.message_id)

    async def _depart(self, source, card, operation_id, *, party=False, room_id=None):
        if party:
            try:
                room = await self.bot.temp_voice.open_party(source.guild, card.game, card.message_id)
                room_id = room.id if room else None
            except Exception:
                logging.exception("LFG party voice room could not be opened.")
        effect_id = f"{operation_id}:lfg-depart"
        self._status(effect_id, "unknown")
        channel = source.channel if self._channel_id(source) == card.channel_id else self.bot.get_partial_messageable(card.channel_id)
        reference = (source.message.to_reference(fail_if_not_exists=False) if hasattr(source, "response")
                     and source.message.id == card.message_id else discord.MessageReference(
                         message_id=card.message_id, channel_id=card.channel_id, guild_id=self._guild_id(source), fail_if_not_exists=False))
        sub = views.departure_sub(card)
        room_url = f"https://discord.com/channels/{self._guild_id(source)}/{room_id}" if room_id else None
        try:
            guild = source.guild
            members = [guild.get_member(user_id) if guild else None for user_id in card.members[:T3_FACES]]
            faces = list(await asyncio.gather(*(fetch_avatar(m.display_avatar) for m in members if m is not None)))
            faces += [None] * (len(card.members) - len(faces))
            files = [await render(t3, f"{card.game} 出發！", sub, faces, "rocket", "depart.png")]
        except Exception:
            logging.exception("LFG departure image could not be drawn; posting it as text.")
            files = []
        try:
            message = await channel.send(view=views.departure_view(card, sub, room_url, bool(files)), files=files, reference=reference,
                                         allowed_mentions=discord.AllowedMentions(users=[discord.Object(i) for i in card.members]))
        except (discord.HTTPException, TimeoutError, aiohttp.ClientError):
            logging.exception("LFG departure announcement result is unknown; not resending.")
            return
        self._status(effect_id, "done", message_id=message.id if isinstance(message.id, int) else None)

    async def expire(self, message_id):
        async with self._lock:
            card = self.store.get(message_id)
            if card is None or card.expires_at > time.time():
                return
            operation_id = f"lfg-expire:{message_id}"
            with transaction(self.store.db):
                self.store._finish(message_id)
                self._presentation_effect(operation_id, card, views.ENDED)
            source = SimpleNamespace(guild=self.bot.get_guild(getattr(self.bot, "guild_id", None)))
            await self._show(source, card, views.ENDED, operation_id)

    async def retry_effect(self, effect_id: str, *, allow_unknown: bool = False, still_current=None) -> bool:
        async with self._lock:
            effect = self.records.get_effect(effect_id)
            if effect is None or effect.kind not in {"lfg_depart", "lfg_edit"}:
                return False
            if (allow_unknown and still_current is None) or (still_current is not None and not still_current()):
                return False
            if effect.status != "pending":
                if not allow_unknown or effect.status not in {"unknown", "failed"}:
                    return False
                with transaction(self.store.db):
                    self.records.update_effect(effect_id, "pending", retry=True)
            guild = self.bot.get_guild(self.bot.guild_id)
            source = SimpleNamespace(guild=guild, guild_id=self.bot.guild_id, channel_id=effect.channel_id,
                                     channel=self.bot.get_partial_messageable(effect.channel_id))
            if effect.kind == "lfg_depart":
                card = LfgCard(**effect.payload)
                room = self.store.db.execute("SELECT channel_id FROM voice_rooms WHERE owner_id = ?", (card.message_id,)).fetchone()
                await self._depart(source, card, effect.operation_id, room_id=room[0] if room else None)
            else:
                original = LfgCard(**effect.payload["card"])
                current = self.store.get(original.message_id)
                ended = None if current else tuple(effect.payload["ended"]) if effect.payload["ended"] else views.ENDED
                await self._show(source, current or original, ended, effect.operation_id or effect_id.removesuffix(":lfg-edit"))
        return self.records.get_effect(effect_id).status == "done"

    async def list_view(self, guild_id: int, user_id: int) -> discord.ui.LayoutView:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            raise ValueError("current guild cache is unavailable")
        actor = await guild.fetch_member(user_id)
        candidates = []
        for card in self.store.cards():
            channel = guild.get_channel_or_thread(card.channel_id)
            if channel is not None and channel.permissions_for(actor).view_channel:
                membership_checked = False
                if isinstance(channel, discord.Thread) and channel.is_private() and not channel.permissions_for(actor).manage_threads:
                    try:
                        await channel.fetch_member(actor.id)
                    except discord.HTTPException:
                        continue
                    membership_checked = True
                candidates.append((card, channel, membership_checked))
        guild, actor = await fetch_current_member(self.bot, guild_id, user_id)
        visible = []
        for card, checked_channel, membership_checked in candidates:
            channel = guild.get_channel_or_thread(card.channel_id)
            if channel is None or not channel.permissions_for(actor).view_channel:
                continue
            if (isinstance(channel, discord.Thread) and channel.is_private()
                and not channel.permissions_for(actor).manage_threads
                and (channel is not checked_channel or not membership_checked)):
                continue
            if self.store.get(card.message_id) is not None:
                visible.append(card)
        return views.list_view(visible, guild_id)
