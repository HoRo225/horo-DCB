import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, PropertyMock

import discord
from discord.components import _component_factory

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


def attachment(data: bytes = b"", filename: str = "image.png", content_type: str | None = "image/png", size: int | None = None, attachment_id: int = 1, width: int | None = 1, height: int | None = 1) -> Mock:
    value = Mock(spec=discord.Attachment)
    value.id = attachment_id
    value.width = width
    value.height = height
    value.filename = filename
    value.content_type = content_type
    value.size = len(data) if size is None else size
    value.read = AsyncMock(return_value=data)
    return value


def components_of(view: discord.ui.LayoutView) -> list:
    return [component for component in (_component_factory(payload) for payload in view.to_components()) if component is not None]


def view_text(view: discord.ui.LayoutView) -> str:
    def walk(payloads: list[dict]) -> list[str]:
        texts = []
        for payload in payloads:
            if payload.get("type") == 10:
                texts.append(payload["content"])
            texts.extend(walk(payload.get("components", [])))
        return texts

    return "\n".join(walk(view.to_components()))


def card_of(view: discord.ui.LayoutView) -> tuple[int | None, list[str]]:
    [payload] = view.to_components()
    if payload["type"] != 17:
        return None, [payload["content"]]
    return payload.get("accent_color"), ["---" if child["type"] == 14 else child["content"] for child in payload["components"]]


class Channel:
    def __init__(self, channel_id: int = CHANNEL_ID, guild_id: int = GUILD_ID, bot: Mock | None = None, first_sent_id: int = 10_000) -> None:
        self.messages: list[Mock] = []
        self.sent: list[Mock] = []
        self.deleted: list[int] = []
        self.bot = bot or member(999, "樂奈", bot=True)
        self.next_sent_id = first_sent_id
        self.send_gate: asyncio.Event | None = None
        self.mock = Mock(spec=discord.TextChannel)
        self.mock.id = channel_id
        self.mock.guild = Mock(id=guild_id)
        self.mock.last_message_id = None
        self.mock.history = Mock(side_effect=self.history)
        self.mock.fetch_message = AsyncMock(side_effect=self.fetch_message)
        self.mock.send = AsyncMock(side_effect=self.send)

    async def send(self, *, view: discord.ui.LayoutView, reference: discord.MessageReference | None = None, **options) -> Mock:
        if self.send_gate is not None:
            await self.send_gate.wait()
        self.next_sent_id += 1
        sent = self.add(message(self.next_sent_id, self.bot, components=tuple(components_of(view)), reply_to=reference.message_id if reference is not None else None))
        sent.views = [view]
        sent.send_options = options
        sent.edit = AsyncMock(side_effect=lambda *, view, **edit_options: self.edit(sent, view))
        sent.delete = AsyncMock(side_effect=lambda: self.delete(sent))
        self.sent.append(sent)
        return sent

    def edit(self, sent: Mock, view: discord.ui.LayoutView) -> None:
        if sent.id in self.deleted:
            raise http_error(discord.NotFound, 404)
        sent.views.append(view)
        sent.components = components_of(view)

    def delete(self, sent: Mock) -> None:
        if sent.id in self.deleted:
            raise http_error(discord.NotFound, 404)
        self.deleted.append(sent.id)
        self.messages.remove(sent)

    def live(self) -> list[Mock]:
        return [value for value in self.sent if value.id not in self.deleted]

    def texts(self) -> list[str]:
        return [view_text(value.views[-1]) for value in self.live()]

    def add(self, message: Mock) -> Mock:
        message.channel = self.mock
        self.mock.last_message_id = max(message.id, self.mock.last_message_id or 0)
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


class Guild:
    def __init__(self, guild_id: int = GUILD_ID, first_channel_id: int = 5000) -> None:
        self.me = member(999, "樂奈", bot=True)
        self.channels: dict[int, Mock] = {}
        self.people: dict[int, Mock] = {}
        self.voice: dict[int, int] = {}
        self.pending: list[tuple[int, int]] = []
        self.moves: list[tuple[int, int]] = []
        self.created: list[Mock] = []
        self.deleted: list[int] = []
        self.next_channel_id = first_channel_id
        self.mock = Mock(spec=discord.Guild)
        self.mock.id = guild_id
        self.mock.me = self.me
        self.mock.get_channel = Mock(side_effect=self.channels.get)
        self.mock.create_voice_channel = AsyncMock(side_effect=self.create)

    def voice_channel(self, channel_id: int, name: str = "語音", *, category: Mock | None = None, bitrate: int = 64000, user_limit: int = 0, overwrites: dict | None = None) -> Mock:
        value = Mock(spec=discord.VoiceChannel)
        value.id = channel_id
        value.name = name
        value.guild = self.mock
        value.category = category
        value.bitrate = bitrate
        value.user_limit = user_limit
        value.overwrites = dict(overwrites or {})
        value.edits = []
        type(value).members = PropertyMock(side_effect=lambda: [self.people[person] for person, channel in self.voice.items() if channel == channel_id])
        value.edit = AsyncMock(side_effect=lambda **options: self.edit(value, options))
        value.delete = AsyncMock(side_effect=lambda **options: self.delete(value))
        self.channels[channel_id] = value
        return value

    def join(self, person: Mock, channel: Mock) -> Mock:
        self.people[person.id] = person
        self.voice[person.id] = channel.id
        person.guild = self.mock
        person.move_to = AsyncMock(side_effect=lambda target, **options: self.move(person, target))
        return person

    def leave(self, person: Mock) -> None:
        self.voice.pop(person.id, None)

    def move(self, person: Mock, target: Mock) -> None:
        self.moves.append((person.id, target.id))
        self.pending.append((person.id, target.id))

    def deliver(self) -> None:
        for person, channel in self.pending:
            if channel in self.channels:
                self.voice[person] = channel
        self.pending.clear()

    async def create(self, name: str, **options) -> Mock:
        self.next_channel_id += 1
        value = self.voice_channel(self.next_channel_id, name, category=options.get("category"), bitrate=options.get("bitrate", 64000), user_limit=options.get("user_limit", 0), overwrites=options.get("overwrites"))
        self.created.append(value)
        return value

    def edit(self, channel: Mock, options: dict) -> None:
        channel.edits.append(options)
        for key, value in options.items():
            setattr(channel, key, dict(value) if key == "overwrites" else value)

    def delete(self, channel: Mock) -> None:
        self.deleted.append(channel.id)
        del self.channels[channel.id]
        self.voice = {person: where for person, where in self.voice.items() if where != channel.id}


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
