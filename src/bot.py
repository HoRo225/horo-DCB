from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import discord
from discord import app_commands

from src.admin_panel import AdminPanelView
from src.calendar_events import CalendarManager
from src.codex_bridge_client import (
    DEFAULT_CODEX_ACCESS_STATE_PATH,
    CodexAccess,
    CodexBridgeClient,
    CodexBridgeError,
    CodexRuntimeStatus,
    conversation_key,
)
from src.config import AppConfig
from src.discord_images import (
    ImageAttachmentError,
    read_image_attachments,
    select_image_attachments,
)
from src.discord_output import (
    build_ai_text_display_view,
    split_discord_message,
    split_discord_text_display,
)
from src.server_activity import ServerActivityMonitor
from src.steam_free_games import SteamFreeGamesNotifier
from src.temp_voice import TempVoiceManager

AI_TEXT_DISPLAY_ENABLED = True
TEMP_VOICE_ENABLED = False
STEAM_FREE_GAMES_ENABLED = False


def clean_bot_mention(content: str, bot_user_id: int) -> str:
    return (
        content.replace(f"<@{bot_user_id}>", "")
        .replace(f"<@!{bot_user_id}>", "")
        .strip()
    )


def message_mentions_bot(message: Any, bot_user_id: int) -> bool:
    return any(getattr(user, "id", None) == bot_user_id for user in message.mentions)


_THREAD_CHANNEL_TYPES = {
    "public_thread",
    "private_thread",
    "news_thread",
}


def codex_conversation_key_for_message(
    message: Any,
    access: CodexAccess,
    *,
    author: Any | None = None,
) -> str | None:
    guild_id = getattr(getattr(message, "guild", None), "id", None)
    channel = getattr(message, "channel", None)
    channel_id = getattr(channel, "id", None)
    author = getattr(message, "author", None) if author is None else author
    user_id = getattr(author, "id", None)
    if not all(type(value) is int and value > 0 for value in (
        guild_id,
        channel_id,
        user_id,
    )):
        return None

    is_thread = str(getattr(channel, "type", "")) in _THREAD_CHANNEL_TYPES
    allowed_channel_id = (
        getattr(channel, "parent_id", None) if is_thread else channel_id
    )
    role_ids = frozenset(
        role_id
        for role in getattr(author, "roles", ())
        if type(role_id := getattr(role, "id", None)) is int and role_id > 0
    )
    if type(allowed_channel_id) is not int or not access.allows(
        guild_id,
        allowed_channel_id,
        user_id,
        role_ids,
    ):
        return None
    return conversation_key(
        guild_id,
        channel_id,
        user_id,
        is_thread=is_thread,
    )


def codex_error_text(code: str) -> str:
    if code == "busy":
        return "AI 目前忙碌，請稍後再試。"
    if code == "unauthorized":
        return "Codex 目前未對此身分組或頻道開放。"
    if code == "auth_required":
        return "AI 尚未完成登入，請聯絡管理員。"
    if code == "timeout":
        return "AI 回覆逾時，請稍後再試。"
    if code == "usage_limit_or_unavailable":
        return "Codex 額度已用盡或服務暫時無法使用，請稍後再試。"
    return "AI 服務暫時無法回覆，請稍後再試。"




async def get_referenced_message(message: Any) -> Any | None:
    reference = getattr(message, "reference", None)
    if reference is None:
        return None

    reference_channel_id = getattr(reference, "channel_id", None)
    current_channel_id = getattr(getattr(message, "channel", None), "id", None)
    if (
        reference_channel_id is not None
        and current_channel_id is not None
        and reference_channel_id != current_channel_id
    ):
        return None

    resolved = getattr(reference, "resolved", None)
    if resolved is not None and (
        getattr(resolved, "author", None) is not None
        or hasattr(resolved, "attachments")
    ):
        return resolved

    message_id = getattr(reference, "message_id", None)
    if message_id is None:
        return None

    try:
        return await asyncio.wait_for(message.channel.fetch_message(message_id), 2)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException, TimeoutError):
        return None


async def _send_native_ai_chunks(
    message: discord.Message,
    chunks: list[str],
    *,
    reply_first: bool,
    can_send: Any = None,
) -> str:
    if not chunks:
        return "unavailable"

    try:
        start = 0
        if reply_first:
            if can_send is not None and not await can_send():
                return "unauthorized"
            await message.reply(
                chunks[0],
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            start = 1

        for chunk in chunks[start:]:
            if can_send is not None and not await can_send():
                return "unauthorized"
            await message.channel.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    except discord.HTTPException:
        logging.error("Discord AI 回覆送出失敗。")
        return "unavailable"
    return "success"


class HoroBot(discord.Client):
    def __init__(
        self,
        codex: CodexBridgeClient,
        codex_access: CodexAccess,
        temp_voice: TempVoiceManager,
        steam_free_games: SteamFreeGamesNotifier,
        calendar: CalendarManager,
        server_activity: ServerActivityMonitor | None = None,
        *,
        ai_text_display_enabled: bool = AI_TEXT_DISPLAY_ENABLED,
        temp_voice_enabled: bool = TEMP_VOICE_ENABLED,
        steam_free_games_enabled: bool = STEAM_FREE_GAMES_ENABLED,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = server_activity is not None
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.codex = codex
        self.codex_access = codex_access
        self.temp_voice = temp_voice
        self.steam_free_games = steam_free_games
        self.calendar = calendar
        self.server_activity = server_activity
        self.ai_text_display_enabled = ai_text_display_enabled
        self.temp_voice_enabled = temp_voice_enabled
        self.steam_free_games_enabled = steam_free_games_enabled
        self.tree = app_commands.CommandTree(self)

        @self.tree.command(name="控制台", description="開啟管理控制台")
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        async def control_panel(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not interaction.permissions.administrator:
                await interaction.response.send_message(
                    "此指令僅限伺服器管理員使用。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.defer(ephemeral=True)
            try:
                codex_status = await self.codex.get_runtime_status()
            except Exception:
                logging.exception("管理控制台讀取 Codex 狀態失敗。")
                codex_status = CodexRuntimeStatus(
                    False,
                    False,
                    None,
                    None,
                    None,
                    None,
                    0,
                )
            await interaction.edit_original_response(
                view=AdminPanelView(
                    user_id=interaction.user.id,
                    guild_id=interaction.guild.id,
                    codex_client=self.codex,
                    user_role_ids=frozenset(
                        role_id
                        for role in getattr(interaction.user, "roles", ())
                        if type(role_id := getattr(role, "id", None)) is int
                        and role_id > 0
                    ),
                    codex_access=self.codex_access,
                    codex_status=codex_status,
                    temp_voice=self.temp_voice,
                    steam_free_games=self.steam_free_games,
                    server_activity=self.server_activity,
                    temp_voice_enabled=self.temp_voice_enabled,
                    steam_free_games_enabled=self.steam_free_games_enabled,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )

        @self.tree.command(name="行事曆", description="開啟行事曆管理")
        @app_commands.guild_only()
        @app_commands.default_permissions(administrator=True)
        async def calendar_panel(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not interaction.permissions.administrator:
                await interaction.response.send_message(
                    "此指令僅限伺服器管理員使用。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            await interaction.response.send_message(
                self.calendar.admin_panel_text(interaction.guild),
                view=self.calendar.admin_view(
                    user_id=interaction.user.id,
                    guild_id=interaction.guild.id,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def setup_hook(self) -> None:
        await self.codex.start()
        if self.server_activity is not None:
            try:
                await self.server_activity.start()
            except Exception:
                logging.error("Server activity event handling failed.")
        self.add_view(self.calendar.persistent_board_view())
        await self.calendar.start(self)
        if self.steam_free_games_enabled:
            self.steam_free_games.start(self)
        try:
            await self.tree.sync()
        except discord.HTTPException:
            logging.exception("Discord Slash Command 同步失敗；Bot 其他功能繼續啟動。")

    async def close(self) -> None:
        async def close_service(service: Any) -> None:
            try:
                await service.close()
            except Exception:
                logging.error("Bot service shutdown failed.")

        try:
            await close_service(self.steam_free_games)
            await close_service(self.calendar)
            await close_service(self.codex)
            if self.server_activity is not None:
                await close_service(self.server_activity)
        finally:
            await super().close()

    def _record_server_activity(self, method_name: str, *args: Any) -> None:
        monitor = self.server_activity
        if monitor is None:
            return
        try:
            getattr(monitor, method_name)(*args)
        except Exception:
            logging.error("Server activity event handling failed.")

    async def on_ready(self) -> None:
        logging.info("Discord Bot 已登入：%s", self.user)
        for guild in self.guilds:
            if self.calendar.has_binding(guild.id):
                await self.calendar.refresh_guild(guild)
        if self.temp_voice_enabled:
            await self.temp_voice.reconcile(self.guilds)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        server_activity = self.server_activity
        if server_activity is not None:
            try:
                await server_activity.enable_guild(guild.id)
            except Exception:
                logging.error("Server activity guild enable failed.")

        if not self.temp_voice_enabled:
            return
        try:
            await self.temp_voice.reconcile([guild], prune_absent=False)
        except Exception:
            logging.error("Temp voice guild join handling failed.")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        HoroBot._record_server_activity(self, "record_voice", member, before, after)
        if self.temp_voice_enabled:
            await self.temp_voice.handle_voice_state_update(member, before, after)

    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        guild = self.get_guild(event.guild_id)
        if guild is not None and self.calendar.has_binding(guild.id):
            await self.calendar.refresh_guild(guild)

    async def on_scheduled_event_update(
        self,
        before: discord.ScheduledEvent,
        after: discord.ScheduledEvent,
    ) -> None:
        guild = self.get_guild(after.guild_id)
        if guild is not None and self.calendar.has_binding(guild.id):
            await self.calendar.refresh_guild(guild)

    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        guild = self.get_guild(event.guild_id)
        if guild is not None and self.calendar.has_binding(guild.id):
            await self.calendar.refresh_guild(guild)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        self.calendar.handle_channel_delete(channel.guild.id, channel.id)
        if self.temp_voice_enabled:
            try:
                await self.temp_voice.handle_channel_delete(channel)
            except Exception:
                logging.error("Temp voice channel delete handling failed.")
        try:
            await self.codex.archive_scope(channel.guild.id, channel.id)
        except Exception:
            logging.error("Codex channel archive failed.")

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        HoroBot._record_server_activity(self, "record_message", "message_delete", payload)
        if payload.guild_id is not None:
            await self.calendar.handle_board_message_delete(
                payload.guild_id,
                payload.channel_id,
                payload.message_id,
            )

    async def on_raw_bulk_message_delete(
        self,
        payload: discord.RawBulkMessageDeleteEvent,
    ) -> None:
        HoroBot._record_server_activity(self, "record_bulk_message_delete", payload)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        HoroBot._record_server_activity(self, "record_message", "message_edit", payload)

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        HoroBot._record_server_activity(self, "record_thread", "thread_delete", payload)
        if type(payload.guild_id) is int:
            try:
                await self.codex.archive_scope(payload.guild_id, payload.thread_id)
            except Exception:
                logging.error("Codex thread archive failed.")

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        server_activity = self.server_activity
        if server_activity is not None:
            try:
                await server_activity.delete_guild(guild.id)
            except Exception:
                logging.error("Server activity guild cleanup failed.")

        try:
            self.calendar.delete_guild(guild.id)
        except Exception:
            logging.error("Calendar guild cleanup failed.")

        if self.temp_voice_enabled:
            try:
                await self.temp_voice.delete_guild(guild.id)
            except Exception:
                logging.error("Temp voice guild cleanup failed.")

        try:
            await self.codex.archive_scope(guild.id)
        except Exception:
            logging.error("Codex guild archive failed.")

    async def _send_ai_answer(self, message: discord.Message, answer: str, *, can_send: Any = None) -> str:
        if not self.ai_text_display_enabled:
            return await _send_native_ai_chunks(
                message,
                split_discord_message(answer),
                reply_first=True,
                can_send=can_send,
            )

        display_chunks = split_discord_text_display(answer)
        sent_chunks: list[str] = []
        try:
            for index, chunk in enumerate(display_chunks):
                if can_send is not None and not await can_send():
                    return "unauthorized"
                view = build_ai_text_display_view(chunk)
                if index == 0:
                    await message.reply(
                        view=view,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await message.channel.send(
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                sent_chunks.append(chunk)
        except discord.HTTPException:
            logging.error("Discord AI TextDisplay 回覆送出失敗，改用原生文字。")
            remaining = "".join(display_chunks[len(sent_chunks) :])
            return await _send_native_ai_chunks(
                message,
                split_discord_message(remaining or answer),
                reply_first=not sent_chunks,
                can_send=can_send,
            )
        return "success" if sent_chunks else "unavailable"

    async def on_message(self, message: discord.Message) -> None:
        if (
            getattr(message, "guild", None) is not None
            and not getattr(message.author, "bot", False)
            and getattr(message, "webhook_id", None) is None
        ):
            HoroBot._record_server_activity(
                self,
                "record_message",
                "message_create",
                message,
            )
        if message.author.bot or message.webhook_id is not None or self.user is None:
            return

        content = message.content.strip()
        attachments = list(message.attachments)
        if not content and not attachments:
            return

        bot_user_id = self.user.id
        referenced_message = None
        if not message_mentions_bot(message, bot_user_id):
            referenced_message = await get_referenced_message(message)
            if getattr(getattr(referenced_message, "author", None), "id", None) != bot_user_id:
                return

        key = codex_conversation_key_for_message(message, self.codex_access)
        if key is None:
            await message.reply(
                "Codex 目前未對此身分組或頻道開放。",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        cleaned_content = clean_bot_mention(content, bot_user_id)
        if not self.codex.try_start_request(message.author.id):
            await message.reply(
                "請稍候幾秒再試。", mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        queued_at = time.monotonic()
        queue_ms = images_ms = sdk_ms = discord_ms = 0.0
        outcome = "unavailable"
        output_started = False

        async def send_error(exc: Exception, *, deadline: float | None = None) -> None:
            nonlocal outcome, discord_ms
            outcome = (
                exc.code if isinstance(exc, CodexBridgeError)
                else "timeout" if isinstance(exc, TimeoutError) else "invalid_request"
            )
            if output_started:
                return
            error_text = str(exc) if isinstance(exc, ImageAttachmentError) else codex_error_text(outcome)
            budget = min(5.0, self.codex.cleanup_timeout_seconds)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    budget = min(budget, remaining)
            started = time.monotonic()
            try:
                async with asyncio.timeout(budget):
                    await message.reply(
                        error_text, mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except (discord.HTTPException, TimeoutError):
                logging.error("Discord AI 狀態回覆送出失敗。")
            finally:
                discord_ms += (time.monotonic() - started) * 1000

        try:
            async with self.codex.accepted_request(
                key, access=self.codex_access, user_id=message.author.id,
            ) as job:
                queue_ms = ((job.started_at or time.monotonic()) - job.accepted_at) * 1000

                try:
                    async with asyncio.timeout_at(job.deadline):
                        async def can_send() -> bool:
                            if not job.current:
                                return False
                            author = message.author
                            if self.codex_access.mode == "roles":
                                guild = message.guild
                                author = None
                                if getattr(getattr(self, "intents", None), "members", False):
                                    author = guild.get_member(message.author.id)
                                if author is None:
                                    try:
                                        author = await asyncio.wait_for(guild.fetch_member(message.author.id), 2)
                                    except (discord.HTTPException, TimeoutError, AttributeError):
                                        return False
                                if author is None:
                                    return False
                            return job.current and codex_conversation_key_for_message(
                                message, self.codex_access, author=author,
                            ) == key

                        if not await can_send():
                            raise CodexBridgeError("unauthorized")
                        image_attachments = select_image_attachments(attachments)
                        if not image_attachments:
                            if referenced_message is None and getattr(message, "reference", None) is not None:
                                referenced_message = await get_referenced_message(message)
                            image_attachments = select_image_attachments(list(
                                getattr(referenced_message, "attachments", ())
                                if referenced_message is not None else ()
                            ))
                        if not cleaned_content and not image_attachments:
                            await message.reply(
                                "請輸入問題，或附上 JPEG、PNG、WebP 圖片。",
                                mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
                            )
                            outcome = "invalid_request"
                            return
                        async with message.channel.typing():
                            started = time.monotonic()
                            try:
                                async with asyncio.timeout(self.codex.image_timeout_seconds):
                                    images = await read_image_attachments(image_attachments)
                            finally:
                                images_ms = (time.monotonic() - started) * 1000
                            if not await can_send():
                                raise CodexBridgeError("unauthorized")
                            started = time.monotonic()
                            try:
                                answer = await self.codex.chat(key, message.author.display_name, cleaned_content, images)
                            finally:
                                sdk_ms = (time.monotonic() - started) * 1000
                        output_started = True
                        started = time.monotonic()
                        try:
                            outcome = await self._send_ai_answer(message, answer, can_send=can_send)
                        finally:
                            discord_ms = (time.monotonic() - started) * 1000
                        if not job.current:
                            outcome = "unauthorized"
                except (ImageAttachmentError, CodexBridgeError, TimeoutError) as exc:
                    # Active error output still owns its key and cancellation registry.
                    await send_error(exc, deadline=job.deadline)
        except asyncio.CancelledError:
            outcome = "cancelled"
        except CodexBridgeError as exc:
            # Rejected admission and expired queues report immediately, without requeueing.
            queue_ms = (time.monotonic() - queued_at) * 1000
            await send_error(exc)
        finally:
            logging.info(
                "AI request result=%s queue_ms=%.1f images_ms=%.1f sdk_ms=%.1f discord_ms=%.1f",
                outcome, queue_ms, images_ms, sdk_ms, discord_ms,
            )

    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        HoroBot._record_server_activity(self, "record_audit", entry)

    async def on_member_join(self, member: discord.Member) -> None:
        HoroBot._record_server_activity(self, "record_member", "member_join", member)

    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent) -> None:
        HoroBot._record_server_activity(self, "record_raw_member_remove", payload)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        HoroBot._record_server_activity(self, "record_member", "member_update", before, after)
        guild_id = getattr(getattr(after, "guild", None), "id", None)
        if type(guild_id) is int and self.codex_access.mode == "roles":
            roles = {role.id for role in after.roles}
            if not self.codex_access.role_ids.intersection(roles):
                try:
                    await self.codex.cancel_member(guild_id, after.id)
                except CodexBridgeError:
                    logging.error("Codex revoked member cleanup failed.")

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        HoroBot._record_server_activity(self, "record_reaction", "reaction_add", payload)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        HoroBot._record_server_activity(self, "record_reaction", "reaction_remove", payload)

    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent) -> None:
        HoroBot._record_server_activity(self, "record_reaction", "reaction_clear", payload)

    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent) -> None:
        HoroBot._record_server_activity(self, "record_reaction", "reaction_clear_emoji", payload)

    async def on_raw_poll_vote_add(self, payload: discord.RawPollVoteActionEvent) -> None:
        HoroBot._record_server_activity(self, "record_poll_vote", "poll_vote_add", payload)

    async def on_raw_poll_vote_remove(self, payload: discord.RawPollVoteActionEvent) -> None:
        HoroBot._record_server_activity(self, "record_poll_vote", "poll_vote_remove", payload)

    async def on_thread_create(self, thread: discord.Thread) -> None:
        HoroBot._record_server_activity(self, "record_thread", "thread_create", thread)

    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        HoroBot._record_server_activity(self, "record_thread", "thread_update", after)

    async def on_scheduled_event_user_add(self, event: discord.ScheduledEvent, user: discord.User) -> None:
        HoroBot._record_server_activity(self, "record_scheduled_subscriber", "scheduled_event_user_add", event, user)

    async def on_scheduled_event_user_remove(self, event: discord.ScheduledEvent, user: discord.User) -> None:
        HoroBot._record_server_activity(self, "record_scheduled_subscriber", "scheduled_event_user_remove", event, user)

    async def on_automod_action(self, execution: discord.AutoModAction) -> None:
        HoroBot._record_server_activity(self, "record_automod", execution)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = AppConfig.from_env()
    codex = CodexBridgeClient(
        "http://codex:8765",
        config.codex_bridge_token,
    )
    codex_access = CodexAccess(
        config.codex_enabled,
        config.codex_allowed_guild_id,
        config.codex_allowed_channel_id,
        config.codex_allowed_user_ids,
        state_path=DEFAULT_CODEX_ACCESS_STATE_PATH,
    )
    server_activity = (
        ServerActivityMonitor() if config.server_activity_enabled else None
    )
    temp_voice = TempVoiceManager()
    steam_free_games = SteamFreeGamesNotifier()
    calendar = CalendarManager()
    HoroBot(
        codex,
        codex_access,
        temp_voice,
        steam_free_games,
        calendar=calendar,
        server_activity=server_activity,
        ai_text_display_enabled=config.ai_text_display_enabled,
        temp_voice_enabled=config.temp_voice_enabled,
        steam_free_games_enabled=config.steam_free_games_enabled,
    ).run(
        config.discord_token,
        log_handler=None,
    )


if __name__ == "__main__":
    main()
