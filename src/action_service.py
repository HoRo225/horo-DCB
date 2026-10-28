from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from collections import OrderedDict
from types import SimpleNamespace

import discord

from src.codex.contracts import ACTION_NAMES, GAME_ACTIONS, IntentDecision, parse_intent_decision
from src.codex.model_settings import parse_model_settings, resolve_model_settings
from src.codex.protocol import CodexError
from src.ai.private_results import TTL_SECONDS, PrivateResults
from src.ai.query_views import help_view, info_card
from src.core.adapters.discord.brand import BRAND_COLOUR, ICONS, WARN_COLOUR, card_title, rana_says, tone_colour
from src.core.adapters.discord.channels import fetch_current_member, member_role_ids, text_channel_problem, is_voice_channel, is_text_channel
from src.games.access import play_problem
from src.games.hub import bind_channel
from src.operations import OperationStore, json_value
from src.state import transaction
from src.core.app.supervisor import cancel_all, track, wait_until

ADMIN_ACTIONS = frozenset({"admin_status", "voice_sync", "publish_hub",
    "steam_query", "ai_enabled", "ai_display", "voice_enabled", "steam_enabled", "ai_channels", "ai_roles",
    "games_roles", "steam_roles", "games_channel", "voice_entry", "steam_channel",
    "ai_models", "pending_resolution"})
QUERY_LABELS = {"admin_status": "管理狀態", "steam_query": "Steam 免費遊戲", "help": "功能說明"}
SAFE_KEYS = frozenset({"ok", "code", "text", "resource_id", "message_url", "private_ref"})


def _static_action_problem(action, explicit, content="") -> str | None:
    params = action.params
    if "channel_id" in params and params["channel_id"] not in explicit["channels"]:
        return "請在本則訊息明確提及設定頻道，未執行任何操作。"
    if action.name in {"ai_channels", "ai_roles", "games_roles", "steam_roles"}:
        target = "channels" if action.name == "ai_channels" else "roles"
        if any(identifier not in explicit[target] for identifier in params["ids"]):
            return "請在本則訊息明確提及設定的頻道或身分組，未執行任何操作。"
    if action.name == "pending_resolution" and params["effect_id"] not in content:
        return "請在本則訊息貼上要處理的項目 ID，未執行任何操作。"
    return None


class ActionService:
    def __init__(self, bot, db) -> None:
        self.bot, self.db = bot, db
        self.records = OperationStore(db)
        self.games = bot.game_operations
        self.private_results = PrivateResults(self._authorize_private, self._render_private, QUERY_LABELS)
        self._tasks: set[asyncio.Task] = set()
        self._running: dict[int, asyncio.Task] = {}
        self._stop_remaining: dict[int, asyncio.Event] = {}
        self._receipts: OrderedDict[int, object | None] = OrderedDict()
        self._closed = False
        self._receipt_lock = asyncio.Lock()

    @staticmethod
    def _actor(source):
        return source.user if hasattr(source, "user") else source.author

    @staticmethod
    def _allowed_channel(source):
        return source.channel.parent_id if isinstance(source.channel, discord.Thread) else source.channel.id

    def _live_source(self, source, actor=None):
        original = getattr(source, "original_source", source)
        guild = self.bot.get_guild(original.guild.id)
        if guild is None:
            raise ValueError("current guild cache is unavailable")
        channel = guild.get_channel_or_thread(original.channel.id)
        if channel is None:
            raise ValueError("current source channel is unavailable")
        actor = actor or guild.get_member(self._actor(original).id)
        if actor is None or (isinstance(actor, discord.Member) and actor.guild is not guild):
            raise ValueError("current member cache is unavailable")
        live = SimpleNamespace(
            original_source=original, client=self.bot, user=actor, author=actor, id=original.id,
            guild=guild, guild_id=guild.id, channel=channel, channel_id=channel.id,
            content=getattr(original, "content", ""),
            mentions=getattr(original, "mentions", ()), channel_mentions=getattr(original, "channel_mentions", ()),
            role_mentions=getattr(original, "role_mentions", ()),
        )
        if isinstance(original, discord.Interaction) or hasattr(original, "response"):
            live.response, live.followup, live.message = original.response, original.followup, original.message
            live.edit_original_response = original.edit_original_response
        return live

    def _problem(self, source, member, *, admin=False, ai=True):
        try:
            source = self._live_source(source, member)
        except ValueError:
            return "目前無法確認最新的伺服器或頻道狀態，請稍後再試。"
        if reason := self.bot.unavailable_reason(source.guild.id):
            return reason
        permissions = source.channel.permissions_for(member)
        can_send = permissions.send_messages_in_threads if isinstance(source.channel, discord.Thread) else permissions.send_messages
        if not permissions.view_channel or not can_send:
            return "你目前無法在這個頻道執行操作。"
        if admin and not member.guild_permissions.administrator:
            return "此操作僅限伺服器管理員使用。"
        if ai and self.bot.ai_settings.denial_reason(source.guild.id, self._allowed_channel(source), member_role_ids(member)):
            return "你目前沒有執行 AI 操作的權限。"
        return None

    def _still_current(self, source, *, admin=False):
        try:
            source = self._live_source(source)
        except ValueError:
            return False
        return self._problem(source, self._actor(source), admin=admin) is None

    async def _fresh(self, source):
        original = getattr(source, "original_source", source)
        _, member = await fetch_current_member(self.bot, original.guild.id, self._actor(original).id)
        return member

    def _explicit_targets(self, source):
        content = getattr(source, "content", "")
        literals = {int(value) for value in re.findall(r"(?<!\d)[1-9]\d{0,18}(?!\d)", content)}
        chosen = {"channels": {source.channel.id}, "roles": set()}
        for category, mention_attr in (("channels", "channel_mentions"), ("roles", "role_mentions")):
            chosen[category].update(item.id for item in getattr(source, mention_attr, ()))
            candidates = {"channels": source.guild.channels, "roles": source.guild.roles}[category]
            by_name = {}
            for item in candidates:
                if item.id in literals:
                    chosen[category].add(item.id)
                name = getattr(item, "name", None)
                if isinstance(name, str) and 2 <= len(name) <= 100 and name in content:
                    by_name.setdefault(name, set()).add(item.id)
            chosen[category].update(next(iter(ids)) for ids in by_name.values() if len(ids) == 1)
        return chosen

    def snapshot(self, source) -> dict:
        try:
            source = self._live_source(source)
        except ValueError:
            return {"allowed_actions": [], "game": {"state_token": None, "busy": True},
                    "channels": [], "roles": []}
        actor, guild = self._actor(source), source.guild
        member = guild.get_member(actor.id) or actor
        admin = bool(member.guild_permissions.administrator)
        explicit = self._explicit_targets(source)
        names = ACTION_NAMES if admin else ACTION_NAMES - ADMIN_ACTIONS
        if play_problem(SimpleNamespace(client=self.bot, guild_id=guild.id, channel_id=source.channel.id, user=member)):
            names = names - GAME_ACTIONS
        return {"actor_id": actor.id, "guild_id": guild.id, "channel_id": source.channel.id,
                "allowed_actions": sorted(names), "game": self.games.snapshot(actor.id),
                "channels": [{"id": item.id, "name": item.name} for item in guild.channels
                             if item.id in explicit["channels"] and item.permissions_for(member).view_channel][:25],
                "roles": [{"id": item.id, "name": item.name} for item in guild.roles
                          if item.id in explicit["roles"] and not item.is_default()][:25]}

    async def execute_plan(self, source, decision, *, game_snapshot=None, deadline=None) -> list[dict]:
        if self._closed:
            return [self._failure("stopped", "Bot 正在重新啟動，未接納新的操作。")]
        try:
            raw = decision.to_payload() if isinstance(decision, IntentDecision) else decision
            decision = parse_intent_decision(json.loads(json_value(raw)))
            game_snapshot = json.loads(json_value(game_snapshot))
            if decision.kind != "plan":
                raise ValueError("not a plan")
            if any(action.name in GAME_ACTIONS for action in decision.actions) and not isinstance(game_snapshot, dict):
                raise ValueError("missing receive-time game snapshot")
        except (ValueError, TypeError):
            return [self._failure("invalid_plan", "操作清單不完整或包含多個牌局動作，請拆開並明確指定。")]
        if any(action.name in GAME_ACTIONS for action in decision.actions) and game_snapshot.get("busy"):
            return [self._failure("busy", "收到訊息時賭桌仍在處理上一個動作，請等完成後再要求下一局。")]
        if (task := self._running.get(source.id)) is None:
            stop_remaining = self._stop_remaining[source.id] = asyncio.Event()
            task = track(self._tasks, asyncio.create_task(self._run_plan(source, decision, game_snapshot, deadline, stop_remaining)), log=True)
            self._running[source.id] = task
            def finished(owner):
                self._running.pop(source.id, None)
                self._stop_remaining.pop(source.id, None)
            task.add_done_callback(finished)
        else:
            stop_remaining = self._stop_remaining[source.id]
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop_remaining.set()
            raise

    @staticmethod
    def _failure(code, text):
        return {"ok": False, "code": code, "text": text}

    @staticmethod
    def _safe(result):
        return {key: value for key, value in result.items() if key in SAFE_KEYS}

    async def _preflight(self, source, decision):
        actor = await self._fresh(source)
        source = self._live_source(source, actor)
        explicit = self._explicit_targets(source)
        for action in decision.actions:
            if problem := self._problem(source, actor, admin=action.name in ADMIN_ACTIONS):
                return problem
            if action.name in GAME_ACTIONS and (problem := play_problem(SimpleNamespace(
                client=self.bot, guild_id=source.guild.id, channel_id=source.channel.id, user=actor
            ))):
                return problem
            if problem := _static_action_problem(action, explicit, source.content):
                return problem
            params = action.params
            if action.name == "ai_models":
                resolve_model_settings(parse_model_settings(params), await self.bot.codex.get_models())
            if "channel_id" in params:
                channel = source.guild.get_channel_or_thread(params["channel_id"])
                if channel is None or not channel.permissions_for(actor).view_channel:
                    return "指定頻道不存在或目前無法查看，未執行任何操作。"
                if action.name in {"games_channel", "steam_channel"} and text_channel_problem(channel, source.guild):
                    return "這項設定必須使用目前伺服器的文字頻道，未執行任何操作。"
                if action.name == "voice_entry" and not is_voice_channel(channel):
                    return "語音入口必須是目前伺服器的語音頻道，未執行任何操作。"
            if action.name in {"ai_channels", "ai_roles", "games_roles", "steam_roles"}:
                role_action = action.name != "ai_channels"
                for identifier in params["ids"]:
                    item = source.guild.get_role(identifier) if role_action else source.guild.get_channel(identifier)
                    if item is None or (role_action and item.is_default()):
                        return "指定的頻道或身分組不存在，未執行任何操作。"
                    if not role_action and (not is_text_channel(item) or not item.permissions_for(actor).view_channel):
                        return "AI 允許清單必須使用可見的文字頻道，未執行任何操作。"
                if action.name in {"ai_channels", "ai_roles"}:
                    self.bot.ai_settings.validate_change(source.guild.id, frozenset(params["ids"]), roles=role_action)
        return None

    def _previous_results(self, request, decision):
        results = []
        for index, action in enumerate(decision.actions):
            operation_id = f"{request.request_id}:{index}"
            public = self.records.get(f"{operation_id}:public")
            owner = self.records.get(operation_id)
            if public:
                results.append(self._safe(public.result))
            elif owner and owner.status == "done":
                results.append({"ok": True, "code": "recorded", "text": "這項操作已有完成紀錄，請重新查詢目前狀態。"})
            elif owner:
                results.append(self._failure("unknown", "這項操作的結果待查；不會自動重試。"))
            else:
                results.append(self._failure("not_executed", "這項操作未執行；請用新訊息重新要求。"))
        return results

    def _step_target_problem(self, source, action):
        source = self._live_source(source, self._actor(source))
        params, guild, actor = action.params, source.guild, self._actor(source)
        if "channel_id" in params:
            channel = guild.get_channel_or_thread(params["channel_id"])
            if channel is None or not channel.permissions_for(actor).view_channel:
                return "指定頻道已不存在或無法查看，未執行這項操作。"
            if action.name in {"games_channel", "steam_channel"} and text_channel_problem(channel, guild):
                return "指定文字頻道目前不可用，未執行這項操作。"
            if action.name == "voice_entry" and not is_voice_channel(channel):
                return "指定語音入口目前不可用，未執行這項操作。"
        if action.name in {"ai_channels", "ai_roles", "games_roles", "steam_roles"}:
            for identifier in params["ids"]:
                item = guild.get_channel(identifier) if action.name == "ai_channels" else guild.get_role(identifier)
                if item is None:
                    return "指定頻道或身分組已不存在，未執行這項操作。"
                if action.name == "ai_channels":
                    if not is_text_channel(item) or not item.permissions_for(actor).view_channel:
                        return "指定 AI 頻道目前不可用，未執行這項操作。"
                elif item.is_default():
                    return "只能選擇一般身分組，未執行這項操作。"
        return None

    async def _run_plan(self, source, decision, game_snapshot, deadline, stop_remaining):
        existing = self.records.get_request(source.id)
        if existing is not None:
            if (existing.actor_id, existing.guild_id, existing.channel_id, existing.plan["decision"]) != (
                self._actor(source).id, source.guild.id, source.channel.id, decision.to_payload()
            ):
                return [self._failure("source", "這則訊息已綁定另一份操作清單，未執行任何操作。")]
            results = self._previous_results(existing, decision)
            await self._publish(source, existing.request_id, results)
            return results
        try:
            if problem := await self._preflight(source, decision):
                return [self._failure("forbidden", problem)]
        except (ValueError, discord.HTTPException, CodexError):
            return [self._failure("invalid_plan", "無法確認完整操作清單，未執行任何操作。")]
        request = self.records.register_request(source.id, self._actor(source).id, source.guild.id, source.channel.id,
                                                {"decision": decision.to_payload(), "game": game_snapshot})
        decision = parse_intent_decision(request.plan["decision"])
        game_snapshot = request.plan["game"]
        results = []
        for index, action in enumerate(decision.actions):
            operation_id = f"{request.request_id}:{index}"
            try:
                if stop_remaining.is_set():
                    result = self._failure("not_executed", "原要求已中止，這項操作未執行。")
                elif deadline is not None and asyncio.get_running_loop().time() >= deadline:
                    result = self._failure("deadline", "操作期限已到，這項操作未執行。")
                else:
                    actor = await self._fresh(source)
                    live_source = self._live_source(source, actor)
                    if stop_remaining.is_set():
                        result = self._failure("not_executed", "原要求已中止，這項操作未執行。")
                    elif problem := self._problem(live_source, actor, admin=action.name in ADMIN_ACTIONS):
                        result = self._failure("forbidden", problem)
                    elif deadline is not None and asyncio.get_running_loop().time() >= deadline:
                        result = self._failure("deadline", "操作期限已到，這項操作未執行。")
                    elif action.name in GAME_ACTIONS and (problem := play_problem(SimpleNamespace(
                        client=self.bot, guild_id=source.guild.id, channel_id=source.channel.id, user=actor
                    ))):
                        result = self._failure("forbidden", problem)
                    elif problem := self._step_target_problem(live_source, action):
                        result = self._failure("target", problem)
                    else:
                        result = self._safe(await self._execute(live_source, action, operation_id, game_snapshot, deadline))
                with transaction(self.db):
                    self.records.complete(f"{operation_id}:public", "action.public", action.to_payload(), result)
            except asyncio.CancelledError:
                stop_remaining.set()
                result = self._failure("interrupted", "操作已停止，已提交的結果保留；未完成項目不會自動接續。")
            except Exception:
                logging.exception("Action %s failed; remaining actions will not run.", action.name)
                owner = self.records.get(operation_id)
                result = self._failure("unknown" if owner and owner.status == "running" else "failed",
                                       "這項操作未能確認完成，請查看私人結果或控制台待處理項目。")
            results.append(result)
            if not result.get("ok"):
                results.extend(self._failure("not_executed", "前面的操作未完成，這項操作未執行。")
                               for remaining in decision.actions[index + 1:])
                break
        with transaction(self.db):
            for index, result in enumerate(results):
                public_id = f"{request.request_id}:{index}:public"
                if self.records.get(public_id) is None:
                    self.records.complete(public_id, "action.public", decision.actions[index].to_payload(), result)
        status = "interrupted" if stop_remaining.is_set() else "done" if all(item.get("ok") for item in results) else "failed"
        self.records.finish_request(request.request_id, status)
        await self._publish(source, request.request_id, results)
        return results

    def _private(self, source, kind, params=None, *, text):
        return {"ok": True, "code": "done", "text": text,
                "private_ref": self.private_results.issue(source, kind, params)}

    def _record_query(self, operation_id, action, result):
        with transaction(self.db):
            self.records.complete(operation_id, "action.query", action.to_payload(), result)
        return result

    async def _execute(self, source, action, operation_id, game_snapshot, deadline=None):
        name, params = action.name, action.params
        still_current = lambda: self._still_current(source, admin=name in ADMIN_ACTIONS) and (
            deadline is None or asyncio.get_running_loop().time() < deadline
        ) and self._step_target_problem(source, action) is None
        if name in GAME_ACTIONS:
            return await self.games.execute(source, name, game=params.get("game"),
                                            expected_token=game_snapshot.get("state_token"), operation_id=operation_id,
                                            still_current=still_current)
        if name in QUERY_LABELS:
            return self._record_query(operation_id, action, self._private(source, name, params, text=f"已查詢{QUERY_LABELS[name]}。"))
        return await self._settings(source, action, operation_id, still_current)

    async def _settings(self, source, action, operation_id, still_current):
        name, params, guild = action.name, action.params, source.guild
        if name == "ai_enabled":
            was_enabled = self.bot.ai_settings.enabled
            if self.bot.ai_settings.set_enabled(params["enabled"], operation_id=operation_id) and was_enabled and not params["enabled"]:
                try:
                    await self.bot.codex.cancel(guild.id)
                except CodexError:
                    logging.exception("AI action disabled AI but could not stop requests in flight.")
        elif name == "ai_display":
            self.bot.ai_settings.set_display(params["enabled"], operation_id=operation_id)
        elif name == "ai_models":
            self.bot.ai_settings.save_models(parse_model_settings(params), operation_id=operation_id)
        elif name in {"ai_channels", "ai_roles"}:
            await self.bot.access_service.change(guild.id, frozenset(params["ids"]), roles=name == "ai_roles",
                                                 still_current=still_current, operation_id=operation_id)
            if self.records.get(operation_id) is None:
                return self._failure("stale", "操作權限或期限已改變，未更新 AI 設定。")
        elif name == "voice_enabled":
            self.bot.temp_voice.set_enabled(params["enabled"], operation_id=operation_id)
        elif name == "steam_enabled":
            self.bot.steam_free_games.set_enabled(params["enabled"], self.bot, operation_id=operation_id)
        elif name == "games_roles":
            self.bot.games.set_roles(params["ids"], operation_id=operation_id)
        elif name == "voice_entry":
            problem = await self.bot.temp_voice.bind_entry(guild, guild.get_channel(params["channel_id"]),
                                                          still_current=still_current, operation_id=operation_id)
            if problem:
                return self._failure("channel", problem)
            if self.records.get(operation_id) is None:
                return self._failure("stale", "操作權限或期限已改變，未更新語音入口。")
        elif name == "steam_channel":
            problem = await self.bot.steam_free_games.bind_channel(guild, guild.get_channel(params["channel_id"]),
                                                                  still_current=still_current, operation_id=operation_id)
            if problem:
                return self._failure("channel", problem)
            if self.records.get(operation_id) is None:
                return self._failure("stale", "操作權限或期限已改變，未更新 Steam 頻道。")
        elif name == "steam_roles":
            await self.bot.steam_free_games.set_notification_roles(guild, [guild.get_role(identifier) for identifier in params["ids"]],
                                                                   still_current=still_current, operation_id=operation_id)
            if self.records.get(operation_id) is None:
                return self._failure("stale", "操作權限或期限已改變，未更新 Steam 身分組。")
        else:
            with transaction(self.db):
                self.records.complete(operation_id, "action.network", action.to_payload(),
                                      self._failure("unknown", "操作結果待查，不會自動重試。"), status="running")
            if name == "voice_sync":
                await self.bot.temp_voice.reconcile(guild, still_current=still_current)
            elif name in {"games_channel", "publish_hub"}:
                if name == "games_channel":
                    if problem := text_channel_problem(guild.get_channel(params["channel_id"]), guild):
                        return self._network_failed(operation_id, action, self._failure("channel", problem))
                    problem = await bind_channel(guild, self.games, guild.get_channel(params["channel_id"]),
                                                 operation_id=operation_id, still_current=still_current)
                else:
                    problem = await self.games.publish_hub(guild, operation_id=operation_id, still_current=still_current)
                if problem:
                    return self._network_failed(operation_id, action, self._failure("publish", problem))
            elif name == "pending_resolution":
                result = await self.bot.effects.resolve(params["effect_id"], params["resolution"],
                                                       message_id=params.get("message_id"), still_current=still_current,
                                                       actor_id=self._actor(source).id)
                if result is not None:
                    return self._network_failed(operation_id, action, self._failure("effect", result))
            else:
                raise ValueError("unsupported action")
            with transaction(self.db):
                self.records.complete(operation_id, "action.network", action.to_payload(), {"ok": True})
        return {"ok": True, "code": "done", "text": "管理操作已完成。"}

    def _network_failed(self, operation_id, action, failure):
        with transaction(self.db):
            self.records.complete(operation_id, "action.network", action.to_payload(), failure, status="failed")
        return failure

    @staticmethod
    def _source_reference(channel, message_id):
        guild = getattr(channel, "guild", None)
        if guild is None or not channel.permissions_for(guild.me).read_message_history:
            return None
        return discord.MessageReference(message_id=message_id, channel_id=channel.id, fail_if_not_exists=False)

    def _receipt_view(self, results, explanation=""):
        lines = [f"{ICONS['ok']} **完成**　{item['text']}" if item.get("ok") else f"{ICONS['error']} **未完成**　{item['text']}"
                 for item in results]
        buttons = self.private_results.make_buttons(item["private_ref"] for item in results if "private_ref" in item)
        note = f"按鈕 {TTL_SECONDS // 60} 分鐘內有效，只有你打得開" if buttons else ""
        items = [card_title(f"{ICONS['ai']} 操作結果", note), discord.ui.TextDisplay("\n".join(lines))]
        if explanation:
            items.append(discord.ui.TextDisplay(rana_says(explanation[:1500])))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*items, accent_colour=tone_colour("ok" if all(item.get("ok") for item in results) else "warn")))
        if buttons:
            view.add_item(discord.ui.ActionRow(*buttons))
        return view

    async def _publish(self, source, request_id, results):
        async with self._receipt_lock:
            await self._publish_locked(source, request_id, results)

    async def _publish_locked(self, source, request_id, results):
        try:
            source = self._live_source(source)
        except ValueError:
            return
        if self.bot.unavailable_reason(source.guild.id):
            return
        actor = source.guild.get_member(self._actor(source).id)
        if actor is None or self._problem(source, actor, ai=False):
            return
        effect_id = f"request:{request_id}:receipt"
        existing = self.records.get_effect(effect_id)
        if existing is not None:
            self._receipts[source.id] = self.bot.get_partial_messageable(existing.channel_id).get_partial_message(existing.message_id) if existing.message_id else None
            return
        nonce = uuid.uuid4().hex[:24]
        with transaction(self.db):
            self.records.create_effect(effect_id, "action.receipt", source.channel.id, {"results": results, "source_message_id": source.id}, nonce=nonce)
            self.records.update_effect(effect_id, "unknown")
        self._receipts[source.id] = None
        while len(self._receipts) > 200:
            self._receipts.popitem(last=False)
        try:
            message = await source.channel.send(view=self._receipt_view(results), nonce=nonce,
                                                 reference=self._source_reference(source.channel, source.id),
                                                 allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logging.exception("Action receipt delivery is unknown; not resending.")
            return
        self._receipts[source.id] = message
        for item in results:
            if "private_ref" in item:
                self.private_results.bind_message(item["private_ref"], message.id)
        with transaction(self.db):
            self.records.update_effect(effect_id, "done", message_id=message.id)

    def was_sent(self, message_id: int) -> bool:
        return message_id in self._receipts

    async def update_receipt(self, source, explanation: str) -> None:
        async with self._receipt_lock:
            await self._update_receipt_locked(source, explanation)

    async def _update_receipt_locked(self, source, explanation):
        message = self._receipts.get(source.id)
        if message is not None and self._still_current(source):
            request = self.records.get_request(source.id)
            effect = self.records.get_effect(f"request:{request.request_id}:receipt") if request is not None else None
            if effect is None:
                return
            results = effect.payload["results"]
            try:
                await message.edit(view=self._receipt_view(results, explanation), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logging.exception("Action receipt explanation could not be updated.")

    async def retry_effect(self, effect_id: str, *, allow_unknown: bool = False, still_current=None) -> bool:
        async with self._receipt_lock:
            return await self._retry_effect_locked(effect_id, allow_unknown=allow_unknown, still_current=still_current)

    async def _retry_effect_locked(self, effect_id, *, allow_unknown, still_current):
        effect = self.records.get_effect(effect_id)
        if effect is None or effect.kind != "action.receipt":
            return False
        if (allow_unknown and still_current is None) or (still_current is not None and not still_current()):
            return False
        with transaction(self.db):
            if effect.status != "pending":
                if not allow_unknown or effect.status not in {"unknown", "failed"}:
                    return False
                self.records.update_effect(effect_id, "pending", retry=True)
            self.records.update_effect(effect_id, "unknown")
        results = [item.copy() for item in effect.payload["results"]]
        for item in results:
            if token := item.get("private_ref"):
                if not self.private_results.can_bind(token):
                    item.pop("private_ref")
        try:
            message = await self.bot.get_partial_messageable(effect.channel_id).send(
                view=self._receipt_view(results), nonce=effect.nonce,
                reference=self._source_reference(self.bot.get_channel(effect.channel_id), effect.payload["source_message_id"]),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            logging.exception("Explicit receipt resend is still unconfirmed.")
            return False
        for result in results:
            if "private_ref" in result:
                self.private_results.bind_message(result["private_ref"], message.id)
        with transaction(self.db):
            self.records.update_effect(effect_id, "done", message_id=message.id)
        self._receipts[effect.payload["source_message_id"]] = message
        return True

    async def _authorize_private(self, entry, interaction):
        if entry.kind not in QUERY_LABELS:
            return "這份私人結果已失效，請重新查詢。"
        try:
            actor = await self._fresh(interaction)
            interaction = self._live_source(interaction, actor)
        except (discord.HTTPException, ValueError):
            return "無法確認目前的伺服器身分。"
        if problem := self._problem(interaction, actor, admin=entry.kind in {"admin_status", "steam_query"}, ai=False):
            return problem
        return None

    async def _render_private(self, entry, interaction):
        self._live_source(interaction)
        if entry.kind == "help":
            return {"view": help_view(self.bot.games.channel_id), "attachments": []}
        if entry.kind == "steam_query":
            from src.steam.views import offer_summary
            result = await self.bot.steam_free_games.fetch_current_offers()
            view = discord.ui.LayoutView(timeout=None)
            body = [offer_summary(offer) for offer in result.offers[:3]] if result else []
            failed = result is None or result.failed_app_count > 0
            if not body:
                body = [discord.ui.TextDisplay("目前無法取得 Steam 資料，請稍後再試。" if failed else "目前沒有符合條件的遊戲。")]
            elif failed:
                body.append(discord.ui.TextDisplay(
                    f"{ICONS['warn']} 資料不完整：{result.failed_app_count} 款遊戲詳細資料無法取得，結果可能有缺漏。"))
            view.add_item(discord.ui.Container(card_title(f"{ICONS['steam']} Steam 免費遊戲"), *body,
                                               accent_colour=WARN_COLOUR if failed else BRAND_COLOUR))
            return {"view": view, "attachments": []}
        status = self.bot.steam_free_games.status()
        voice = self.bot.temp_voice.status()
        return {"view": info_card(card_title(f"{ICONS['status']} 系統狀態"), discord.ui.TextDisplay(
                    f"AI：{'開啟' if self.bot.ai_settings.enabled else '停用'}\n"
                    f"語音：{'開啟' if self.bot.temp_voice.enabled else '停用'}\n"
                    f"Steam：{'執行中' if status.running else '已停止'}\n"
                    f"待處理：{len(self.bot.effects.pending())} 項")), "attachments": []}

    def stop_new_work(self) -> None:
        self._closed = True

    async def close(self, *, deadline: float) -> None:
        self.stop_new_work()
        pending = await wait_until(self._tasks, deadline)
        cancel_all(pending)
        if pending:
            await asyncio.sleep(0)
