from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import discord

GUILD_ID = 100
CHANNEL_ID = 200
EPOCH = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)


def http_error(kind: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return kind(Mock(status=status, reason=""), "")


def member(user_id: int = 1, name: str = "測試者", role_ids: tuple[int, ...] = (), bot: bool = False) -> Mock:
    value = Mock(spec=discord.Member)
    value.id = user_id
    value.display_name = name
    value.bot = bot
    value.roles = [Mock(id=role_id) for role_id in role_ids]
    return value


def attachment(data: bytes = b"", filename: str = "image.png", content_type: str | None = "image/png", size: int | None = None, attachment_id: int = 1) -> Mock:
    value = Mock(spec=discord.Attachment)
    value.id = attachment_id
    value.filename = filename
    value.content_type = content_type
    value.size = len(data) if size is None else size
    value.read = AsyncMock(return_value=data)
    return value


class Channel:
    def __init__(self, channel_id: int = CHANNEL_ID, guild_id: int = GUILD_ID) -> None:
        self.messages: list[Mock] = []
        self.sent: list[Mock] = []
        self.mock = Mock(spec=discord.TextChannel)
        self.mock.id = channel_id
        self.mock.guild = Mock(id=guild_id)
        self.mock.history = Mock(side_effect=self.history)
        self.mock.fetch_message = AsyncMock(side_effect=self.fetch_message)

    def add(self, message: Mock) -> Mock:
        message.channel = self.mock
        self.messages.append(message)
        self.messages.sort(key=lambda value: value.id)
        return message

    def post(self, message_id: int, author: Mock, content: str = "", **options) -> Mock:
        return self.add(message(message_id, author, content, **options))

    async def history(self, *, limit: int = 100, before=None, after=None, around=None, oldest_first: bool | None = None):
        ordered = sorted(self.messages, key=lambda value: value.id)
        if around is not None:
            older = [value for value in ordered if value.id < around.id][-(limit // 2):] if limit // 2 else []
            newer = [value for value in ordered if value.id > around.id][: limit - limit // 2 - 1]
            target = [value for value in ordered if value.id == around.id]
            chosen = [*older, *target, *newer]
            chosen.reverse()
        else:
            if oldest_first is None:
                oldest_first = after is not None
            if oldest_first:
                chosen = [value for value in ordered if (after is None or value.id > after.id) and (before is None or value.id < before.id)][:limit]
            else:
                below = [value for value in reversed(ordered) if before is None or value.id < before.id][:limit]
                chosen = [value for value in below if after is None or value.id > after.id]
        for value in chosen:
            yield value

    async def fetch_message(self, message_id: int) -> Mock:
        for value in self.messages:
            if value.id == message_id:
                return value
        raise http_error(discord.NotFound, 404)


def message(message_id: int, author: Mock, content: str = "", *, attachments: tuple[Mock, ...] = (), reply_to: int | None = None, components: tuple = (), mentions: tuple[Mock, ...] = ()) -> Mock:
    value = Mock(spec=discord.Message)
    value.id = message_id
    value.author = author
    value.content = content
    value.clean_content = content
    value.components = list(components)
    value.attachments = list(attachments)
    value.mentions = list(mentions)
    value.created_at = EPOCH + timedelta(minutes=message_id)
    value.reference = discord.MessageReference(message_id=reply_to, channel_id=CHANNEL_ID, guild_id=GUILD_ID) if reply_to is not None else None
    value.type = discord.MessageType.reply if reply_to is not None else discord.MessageType.default
    value.to_reference = Mock(side_effect=lambda fail_if_not_exists=True: discord.MessageReference(message_id=message_id, channel_id=CHANNEL_ID, guild_id=GUILD_ID, fail_if_not_exists=fail_if_not_exists))
    value.delete = AsyncMock()
    value.edit = AsyncMock()
    value.add_reaction = AsyncMock()
    return value
