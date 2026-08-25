from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from src.admin_panel import AdminPanelView
from src.agent_tools import AgentTools, ToolContext
from src.ai_client import AIClient, AIClientError, AIRuntimeStatus
from src.chat import ChatManager, ChatReply
from src.calendar_events import CalendarManager, CalendarUserError
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
from src.semantic_memory import MemoryScope, MemoryVerification, SemanticMemory
from src.steam_free_games import SteamFreeGamesNotifier
from src.temp_voice import TempVoiceManager

AI_TEXT_DISPLAY_ENABLED = True
TEMP_VOICE_ENABLED = True
STEAM_FREE_GAMES_ENABLED = True


def clean_bot_mention(content: str, bot_user_id: int) -> str:
    return (
        content.replace(f"<@{bot_user_id}>", "")
        .replace(f"<@!{bot_user_id}>", "")
        .strip()
    )


def message_mentions_bot(message: Any, bot_user_id: int) -> bool:
    return any(getattr(user, "id", None) == bot_user_id for user in message.mentions)


def resolved_reply_targets_bot(message: Any, bot_user_id: int) -> bool:
    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None)
    author = getattr(resolved, "author", None)
    return getattr(author, "id", None) == bot_user_id


def build_tool_context(message: Any) -> ToolContext:
    guild_name = getattr(getattr(message, "guild", None), "name", None)
    if not isinstance(guild_name, str):
        guild_name = None

    channel = message.channel
    channel_name = getattr(channel, "name", None)
    if not isinstance(channel_name, str) or not channel_name:
        channel_name = "direct-message"

    channel_type = getattr(channel, "type", None)
    return ToolContext(
        guild_name=guild_name,
        channel_name=channel_name,
        channel_type=str(channel_type) if channel_type is not None else "unknown",
    )


def is_semantic_memory_channel(message: Any) -> bool:
    if getattr(message, "guild", None) is None:
        return False
    channel_type = str(getattr(getattr(message, "channel", None), "type", ""))
    return channel_type in {
        "text",
        "news",
        "public_thread",
        "private_thread",
        "news_thread",
    }


def build_memory_scope(message: Any) -> MemoryScope | None:
    if not is_semantic_memory_channel(message):
        return None
    channel = message.channel

    async def verify_message(message_id: int) -> MemoryVerification:
        try:
            current = await channel.fetch_message(message_id)
        except discord.NotFound:
            return MemoryVerification("deleted")
        except (discord.Forbidden, discord.HTTPException):
            return MemoryVerification("unavailable")
        if current.author.bot or current.webhook_id is not None:
            return MemoryVerification("deleted")
        return MemoryVerification(
            "current",
            current.content,
            current.author.display_name,
        )

    return MemoryScope(channel_id=channel.id, verify_message=verify_message)


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
        return await message.channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def reply_targets_bot(message: Any, bot_user_id: int) -> bool:
    referenced = await get_referenced_message(message)
    return getattr(getattr(referenced, "author", None), "id", None) == bot_user_id


async def should_trigger_ai(message: Any, bot_user_id: int) -> bool:
    if message_mentions_bot(message, bot_user_id):
        return True
    return await reply_targets_bot(message, bot_user_id)


async def _send_native_ai_chunks(
    message: discord.Message,
    chunks: list[str],
    *,
    reply_first: bool,
    first_view: discord.ui.View | None = None,
) -> list[str]:
    sent: list[str] = []
    if not chunks:
        return sent

    try:
        start = 0
        if reply_first:
            reply_kwargs: dict[str, Any] = {
                "mention_author": False,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if first_view is not None:
                reply_kwargs["view"] = first_view
            await message.reply(chunks[0], **reply_kwargs)
            sent.append(chunks[0])
            start = 1

        for chunk in chunks[start:]:
            await message.channel.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            sent.append(chunk)
    except discord.HTTPException:
        logging.exception("Discord AI 回覆送出失敗。")

    return sent


class HoroBot(discord.Client):
    def __init__(
        self,
        chat: ChatManager,
        temp_voice: TempVoiceManager,
        steam_free_games: SteamFreeGamesNotifier,
        semantic_memory: SemanticMemory | None = None,
        calendar: CalendarManager | None = None,
        *,
        ai_text_display_enabled: bool = AI_TEXT_DISPLAY_ENABLED,
        temp_voice_enabled: bool = TEMP_VOICE_ENABLED,
        steam_free_games_enabled: bool = STEAM_FREE_GAMES_ENABLED,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guild_scheduled_events = True
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.chat = chat
        self.temp_voice = temp_voice
        self.steam_free_games = steam_free_games
        self.semantic_memory = semantic_memory
        self.calendar = calendar
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
            try:
                ai_status = await self.chat.ai_client.get_runtime_status()
            except Exception:
                logging.exception("管理控制台讀取 9Router 狀態失敗。")
                ai_status = AIRuntimeStatus(None, None, False, None)
            await interaction.response.send_message(
                view=AdminPanelView(
                    user_id=interaction.user.id,
                    guild_id=interaction.guild.id,
                    chat=self.chat,
                    ai_status=ai_status,
                    temp_voice=self.temp_voice,
                    steam_free_games=self.steam_free_games,
                    temp_voice_enabled=self.temp_voice_enabled,
                    steam_free_games_enabled=self.steam_free_games_enabled,
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        if self.calendar is not None:
            calendar_group = app_commands.Group(name="行事曆", description="管理 Discord 行事曆看板")

            async def require_calendar_admin(
                interaction: discord.Interaction,
            ) -> bool:
                if interaction.guild is not None and interaction.permissions.administrator:
                    return True
                await interaction.response.send_message(
                    "此操作僅限伺服器管理員使用。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return False

            @calendar_group.command(name="綁定", description="將行事曆看板綁定到文字頻道")
            @app_commands.describe(channel="顯示持久行事曆看板的文字頻道")
            async def bind_calendar(
                interaction: discord.Interaction,
                channel: discord.TextChannel,
            ) -> None:
                if not await require_calendar_admin(interaction):
                    return
                assert interaction.guild is not None
                await interaction.response.defer(ephemeral=True, thinking=True)
                try:
                    binding = await self.calendar.bind(
                        interaction.guild,
                        channel,
                        actor_id=interaction.user.id,
                    )
                except CalendarUserError as exc:
                    await interaction.followup.send(
                        str(exc),
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                await interaction.followup.send(
                    f"已將行事曆看板綁定到 <#{binding.channel_id}>。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            @calendar_group.command(name="解除綁定", description="解除目前的行事曆看板")
            async def unbind_calendar(interaction: discord.Interaction) -> None:
                if not await require_calendar_admin(interaction):
                    return
                assert interaction.guild is not None
                await interaction.response.defer(ephemeral=True, thinking=True)
                try:
                    removed = await self.calendar.unbind(
                        interaction.guild,
                        actor_id=interaction.user.id,
                    )
                except CalendarUserError as exc:
                    await interaction.followup.send(
                        str(exc),
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                await interaction.followup.send(
                    "已解除行事曆看板。" if removed else "此伺服器目前沒有綁定行事曆看板。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            @calendar_group.command(name="重新整理", description="重新整理目前的行事曆看板")
            async def refresh_calendar(interaction: discord.Interaction) -> None:
                if not await require_calendar_admin(interaction):
                    return
                assert interaction.guild is not None
                await interaction.response.defer(ephemeral=True, thinking=True)
                ok = await self.calendar.refresh_guild(interaction.guild)
                await interaction.followup.send(
                    "行事曆看板已重新整理。" if ok else "行事曆看板目前無法重新整理。",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )

            self.tree.add_command(calendar_group)

    async def setup_hook(self) -> None:
        await self.chat.start()
        if self.semantic_memory is not None:
            await self.semantic_memory.start()
        if self.calendar is not None:
            self.add_view(self.calendar.persistent_board_view())
            await self.calendar.start(self)
        if getattr(self, "steam_free_games_enabled", True):
            self.steam_free_games.start(self)
        try:
            await self.tree.sync()
        except discord.HTTPException:
            logging.exception("Discord Slash Command 同步失敗；Bot 其他功能繼續啟動。")

    async def close(self) -> None:
        await self.steam_free_games.close()
        if self.calendar is not None:
            await self.calendar.close()
        if self.semantic_memory is not None:
            await self.semantic_memory.close()
        await self.chat.close()
        await super().close()

    async def on_ready(self) -> None:
        logging.info("Discord Bot 已登入：%s", self.user)
        if self.calendar is not None:
            for guild in self.guilds:
                if self.calendar.has_binding(guild.id):
                    await self.calendar.refresh_guild(guild)
        if getattr(self, "temp_voice_enabled", True):
            await self.temp_voice.reconcile(self.guilds)

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if getattr(self, "temp_voice_enabled", True):
            await self.temp_voice.handle_voice_state_update(member, before, after)

    async def on_scheduled_event_create(self, event: discord.ScheduledEvent) -> None:
        calendar = getattr(self, "calendar", None)
        if calendar is None:
            return
        guild = self.get_guild(event.guild_id)
        if guild is not None and calendar.has_binding(guild.id):
            await calendar.refresh_guild(guild)

    async def on_scheduled_event_update(
        self,
        before: discord.ScheduledEvent,
        after: discord.ScheduledEvent,
    ) -> None:
        calendar = getattr(self, "calendar", None)
        if calendar is None:
            return
        guild = self.get_guild(after.guild_id)
        if guild is not None and calendar.has_binding(guild.id):
            await calendar.refresh_guild(guild)

    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent) -> None:
        calendar = getattr(self, "calendar", None)
        if calendar is None:
            return
        guild = self.get_guild(event.guild_id)
        if guild is not None and calendar.has_binding(guild.id):
            await calendar.refresh_guild(guild)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        calendar = getattr(self, "calendar", None)
        if calendar is not None:
            calendar.handle_channel_delete(channel.guild.id, channel.id)
        if getattr(self, "temp_voice_enabled", True):
            try:
                await self.temp_voice.handle_channel_delete(channel)
            except Exception:
                logging.error("Temp voice channel delete handling failed.")

        forget_channel = getattr(getattr(self, "chat", None), "forget_channel", None)
        if callable(forget_channel):
            forget_channel(channel.id)

        semantic_memory = getattr(self, "semantic_memory", None)
        if semantic_memory is not None:
            try:
                await semantic_memory.delete_channel(channel.id)
            except Exception:
                logging.error("Semantic memory channel cleanup failed.")

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        calendar = getattr(self, "calendar", None)
        if payload.guild_id is not None and calendar is not None:
            await calendar.handle_board_message_delete(
                payload.guild_id,
                payload.channel_id,
                payload.message_id,
            )
        semantic_memory = getattr(self, "semantic_memory", None)
        if payload.guild_id is None or semantic_memory is None:
            return
        try:
            await semantic_memory.delete_message(payload.message_id)
        except Exception:
            logging.error("Semantic memory message delete failed.")

    async def on_raw_bulk_message_delete(
        self,
        payload: discord.RawBulkMessageDeleteEvent,
    ) -> None:
        semantic_memory = getattr(self, "semantic_memory", None)
        if payload.guild_id is None or semantic_memory is None:
            return
        try:
            await semantic_memory.delete_messages(payload.message_ids)
        except Exception:
            logging.error("Semantic memory bulk delete failed.")

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        semantic_memory = getattr(self, "semantic_memory", None)
        if payload.guild_id is None or semantic_memory is None:
            return
        try:
            if not await semantic_memory.contains_message(payload.message_id):
                return
            channel = self.get_channel(payload.channel_id)
            if channel is None or not hasattr(channel, "fetch_message"):
                return
            try:
                current = await channel.fetch_message(payload.message_id)
            except discord.NotFound:
                await semantic_memory.delete_message(payload.message_id)
                return
            except (discord.Forbidden, discord.HTTPException):
                return
            if current.author.bot or current.webhook_id is not None:
                await semantic_memory.delete_message(payload.message_id)
                return
            if not is_semantic_memory_channel(current):
                await semantic_memory.delete_message(payload.message_id)
                return
            await semantic_memory.update_existing_message(
                message_id=current.id,
                guild_id=current.guild.id,
                channel_id=current.channel.id,
                author_name=current.author.display_name,
                content=current.content,
            )
        except Exception:
            logging.error("Semantic memory message edit failed.")

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent) -> None:
        forget_channel = getattr(getattr(self, "chat", None), "forget_channel", None)
        if callable(forget_channel):
            forget_channel(payload.thread_id)

        semantic_memory = getattr(self, "semantic_memory", None)
        if semantic_memory is not None:
            try:
                await semantic_memory.delete_channel(payload.thread_id)
            except Exception:
                logging.error("Semantic memory thread cleanup failed.")

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        calendar = getattr(self, "calendar", None)
        if calendar is not None:
            calendar.delete_guild(guild.id)
        forget_channel = getattr(getattr(self, "chat", None), "forget_channel", None)
        if callable(forget_channel):
            for channel in (*getattr(guild, "channels", ()), *getattr(guild, "threads", ())):
                channel_id = getattr(channel, "id", None)
                if isinstance(channel_id, int):
                    forget_channel(channel_id)

        semantic_memory = getattr(self, "semantic_memory", None)
        if semantic_memory is not None:
            try:
                await semantic_memory.delete_guild(guild.id)
            except Exception:
                logging.error("Semantic memory guild cleanup failed.")

    async def _send_ai_answer(self, message: discord.Message, reply: ChatReply) -> None:
        answer = reply.content
        if not answer:
            return

        sent_chunks: list[str]
        calendar = getattr(self, "calendar", None)
        if (
            reply.calendar_draft is not None
            and calendar is not None
            and message.guild is not None
        ):
            confirmation_text = (
                f"{calendar.draft_summary(reply.calendar_draft)}\n\n"
                f"{answer.strip()}"
            )
            confirmation_view = calendar.confirmation_view(
                reply.calendar_draft,
                user_id=message.author.id,
                guild_id=message.guild.id,
            )
            confirmation_chunks = split_discord_message(confirmation_text)
            sent_chunks = await _send_native_ai_chunks(
                message,
                confirmation_chunks,
                reply_first=True,
                first_view=confirmation_view,
            )
            if not sent_chunks:
                fallback_text = (
                    f"{confirmation_text}\n\n"
                    "⚠️ 確認按鈕無法顯示，本次草稿未執行；請重新提出行事曆需求。"
                )
                sent_chunks = await _send_native_ai_chunks(
                    message,
                    split_discord_message(fallback_text),
                    reply_first=True,
                )
            if sent_chunks:
                self.chat.record_assistant_message(
                    message.channel.id,
                    "".join(sent_chunks),
                )
            return

        text_display_enabled = getattr(
            self,
            "ai_text_display_enabled",
            AI_TEXT_DISPLAY_ENABLED,
        )
        if not text_display_enabled:
            sent_chunks = await _send_native_ai_chunks(
                message,
                split_discord_message(answer),
                reply_first=True,
            )
        else:
            display_chunks = split_discord_text_display(answer)
            sent_chunks = []
            try:
                for index, chunk in enumerate(display_chunks):
                    view = build_ai_text_display_view(
                        chunk, reply.images if index == 0 else ()
                    )
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
                logging.exception(
                    "Discord AI TextDisplay 回覆送出失敗，改用原生文字。"
                )
                if sent_chunks:
                    remaining = "".join(display_chunks[len(sent_chunks) :])
                    sent_chunks.extend(
                        await _send_native_ai_chunks(
                            message,
                            split_discord_message(remaining),
                            reply_first=False,
                        )
                    )
                else:
                    sent_chunks = await _send_native_ai_chunks(
                        message,
                        split_discord_message(answer),
                        reply_first=True,
                    )

        if sent_chunks:
            self.chat.record_assistant_message(
                message.channel.id, "".join(sent_chunks)
            )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id is not None:
            return
        if self.user is None:
            return

        content = message.content.strip()
        attachments = list(message.attachments)
        if not content and not attachments:
            return

        semantic_memory = getattr(self, "semantic_memory", None)
        if semantic_memory is not None and content and is_semantic_memory_channel(message):
            try:
                created_at_value = getattr(message, "created_at", None)
                created_at = (
                    int(created_at_value.timestamp())
                    if created_at_value is not None
                    else None
                )
                await semantic_memory.capture_message(
                    message_id=message.id,
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    author_name=message.author.display_name,
                    content=content,
                    created_at=created_at,
                )
            except Exception:
                logging.error("Semantic memory message capture failed.")

        bot_user_id = self.user.id
        cleaned_content = clean_bot_mention(content, bot_user_id)
        referenced_message = None
        triggered_by_mention = message_mentions_bot(message, bot_user_id)
        if not triggered_by_mention:
            referenced_message = await get_referenced_message(message)
            if getattr(getattr(referenced_message, "author", None), "id", None) != bot_user_id:
                if content:
                    self.chat.record_user_message(
                        message.channel.id,
                        message.author.display_name,
                        cleaned_content or content,
                    )
                return

        try:
            image_attachments = select_image_attachments(attachments)
        except ImageAttachmentError as exc:
            if content:
                self.chat.record_user_message(
                    message.channel.id,
                    message.author.display_name,
                    cleaned_content or content,
                )
            await message.reply(
                str(exc),
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        image_marker = None
        if image_attachments:
            image_marker = f"[附帶 {len(image_attachments)} 張圖片]"
        else:
            if referenced_message is None and getattr(message, "reference", None) is not None:
                referenced_message = await get_referenced_message(message)
            referenced_attachments = list(
                getattr(referenced_message, "attachments", ()) if referenced_message is not None else ()
            )
            try:
                image_attachments = select_image_attachments(referenced_attachments)
            except ImageAttachmentError as exc:
                if content:
                    self.chat.record_user_message(
                        message.channel.id,
                        message.author.display_name,
                        cleaned_content or content,
                    )
                await message.reply(
                    str(exc),
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            if image_attachments:
                image_marker = f"[引用訊息含 {len(image_attachments)} 張圖片]"

        if not content and not image_attachments:
            await message.reply(
                "目前只支援 JPEG、PNG 與 WebP 圖片。",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        history_content = cleaned_content or content
        if image_marker is not None:
            history_content = f"{image_marker} {history_content}".strip()
        self.chat.record_user_message(
            message.channel.id,
            message.author.display_name,
            history_content,
        )
        history_snapshot = self.chat.snapshot_history(message.channel.id)

        if not self.chat.try_start_request(message.author.id):
            await message.reply(
                "請稍候幾秒再試。",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        calendar_scope = None
        calendar = getattr(self, "calendar", None)
        if (
            calendar is not None
            and message.guild is not None
            and calendar.has_binding(message.guild.id)
        ):
            permissions = getattr(message.author, "guild_permissions", None)
            can_manage_events = bool(
                permissions is not None
                and (
                    getattr(permissions, "administrator", False)
                    or getattr(permissions, "manage_events", False)
                )
            )
            calendar_scope = calendar.make_scope(
                message.guild.id,
                message.author.id,
                can_manage_events=can_manage_events,
            )

        async with self.chat.channel_lock(message.channel.id):
            try:
                async with message.channel.typing():
                    image_data_urls = await read_image_attachments(image_attachments)
                    generate_kwargs: dict[str, Any] = {
                        "image_data_urls": image_data_urls,
                        "memory_scope": (
                            build_memory_scope(message)
                            if semantic_memory is not None
                            else None
                        ),
                        "history_snapshot": history_snapshot,
                    }
                    if calendar_scope is not None:
                        generate_kwargs["calendar_scope"] = calendar_scope
                    answer = await self.chat.generate_reply(
                        message.channel.id,
                        build_tool_context(message),
                        **generate_kwargs,
                    )
            except ImageAttachmentError as exc:
                logging.error("Discord 圖片附件處理失敗。")
                await message.reply(
                    str(exc),
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            except AIClientError as exc:
                logging.error("AI 呼叫失敗：%s", exc)
                await message.reply(
                    "AI 服務暫時無法回覆，請稍後再試。",
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            await self._send_ai_answer(message, answer)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = AppConfig.from_env()
    discord_credential = config.discord_token
    search_provider = config.web_search_provider
    image_search_provider = config.image_search_provider
    fetch_provider = config.web_fetch_provider
    embedding_model = config.embedding_model
    embedding_dimensions = config.embedding_dimensions
    semantic_memory_enabled = config.semantic_memory_enabled

    ai_client = AIClient(
        config.ninerouter_url,
        config.ninerouter_api_key,
        config.ninerouter_model,
    )
    semantic_memory = None
    if semantic_memory_enabled:
        semantic_memory = SemanticMemory(
            ai_client,
            embedding_model=embedding_model,
            embedding_dimensions=embedding_dimensions,
        )
    temp_voice = TempVoiceManager()
    steam_free_games = SteamFreeGamesNotifier()
    calendar = CalendarManager()
    agent_tools = AgentTools(
        steam_free_games,
        ai_client,
        semantic_memory=semantic_memory,
        calendar=calendar,
        search_provider=search_provider,
        image_search_provider=image_search_provider,
        fetch_provider=fetch_provider,
    )
    chat = ChatManager(ai_client, agent_tools)
    HoroBot(
        chat,
        temp_voice,
        steam_free_games,
        semantic_memory=semantic_memory,
        calendar=calendar,
        ai_text_display_enabled=config.ai_text_display_enabled,
        temp_voice_enabled=config.temp_voice_enabled,
        steam_free_games_enabled=config.steam_free_games_enabled,
    ).run(
        discord_credential,
        log_handler=None,
    )


if __name__ == "__main__":
    main()
