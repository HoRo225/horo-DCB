from __future__ import annotations

import logging

import discord

from src.ai.adapters.discord.context import (
    clean_bot_mention,
    conversation_access,
    get_referenced_message,
    message_mentions_bot,
)
from src.ai.adapters.discord.errors import ai_error_text
from src.ai.adapters.discord.help import HELP_WORDS, help_view
from src.ai.adapters.discord.output import reply, reply_notice
from src.ai.adapters.discord.request import AiRequest, AiServices
from src.ai.adapters.media.images import MediaReader
from src.ai.app.ports import GamesChannel, NewcomerTips, TextModel
from src.ai.app.settings import AiSettings
from src.core.adapters.discord.live import LiveGuild
from src.core.app.supervisor import TaskGroup

THROTTLED = "你發問得太快了，請稍候幾秒再試。"
DENIAL_CODES = {"suspended": "busy", "channel": "not_allowed_channel", "role": "not_allowed_role"}


class AiMessages:
    def __init__(
        self, live: LiveGuild, settings: AiSettings, model: TextModel, games: GamesChannel, media: MediaReader, *,
        tips: NewcomerTips, cleanups: TaskGroup,
    ) -> None:
        self._services = AiServices(live, settings, model, media, cleanups, tips)
        self._games = games

    async def handle(self, message: discord.Message, bot_user_id: int) -> None:
        if message.author.bot or message.webhook_id is not None:
            return
        content = message.content.strip()
        if (
            not content
            and not message.attachments
            and not getattr(message, "embeds", ())
            and not getattr(message, "stickers", ())
        ):
            return
        mentions_bot = message_mentions_bot(message, bot_user_id)
        if not mentions_bot and message.reference is None:
            return
        services = self._services
        settings = services.settings
        if not settings.enabled:
            return
        key, denial_reason = conversation_access(message, settings)
        if key is None and (
            not mentions_bot
            or (denial_reason == "channel" and clean_bot_mention(content, bot_user_id) == content)
        ):
            return
        referenced_message = None
        if not mentions_bot:
            referenced_message = await get_referenced_message(message)
            if getattr(getattr(referenced_message, "author", None), "id", None) != bot_user_id:
                return
        bot_name = services.live.bot_name()

        if key is None:
            logging.info("AI access denied reason=%s", denial_reason)
            code = DENIAL_CODES.get(denial_reason, "not_allowed")
            channels = "、".join(f"<#{channel_id}>" for channel_id in sorted(settings.channel_ids)[:5])
            if code == "not_allowed_channel" and not channels:
                code = "not_allowed"
            await reply_notice(message, ai_error_text(code, bot_name, channels=channels))
            return

        cleaned_content = clean_bot_mention(content, bot_user_id)
        if cleaned_content in HELP_WORDS:
            try:
                await reply(message, view=help_view(self._games.channel_id, bot_name))
            except discord.HTTPException:
                logging.exception("Failed to send the help card.")
            return
        if not services.model.try_start_request(message.author.id):
            await reply_notice(message, THROTTLED)
            return

        await AiRequest(
            services,
            message,
            key=key,
            content=cleaned_content,
            mentions_bot=mentions_bot,
            referenced_message=referenced_message,
            bot_user_id=bot_user_id,
            bot_name=bot_name,
        ).run()
