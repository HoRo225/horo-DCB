from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

from src.core.domain.records import DiscordEffect, OperationRecord
from src.voice.app.ports import DeleteClaim, GatewayError, StoreError
from src.voice.domain.rooms import RoomShape, RoomSpec, build_temp_voice_name

GUILD = 100


def shape(name: str = "語音", category_id: int | None = None) -> RoomShape:
    return RoomShape(name, category_id, (), 0, 64000, None, 1)


class FakeVoiceStore:
    def __init__(self) -> None:
        self.switch = False
        self.entry: int | None = None
        self.owners: dict[int, int] = {}
        self.effects: dict[str, DiscordEffect] = {}
        self.operations: dict[str, OperationRecord] = {}
        self.failing: set[str] = set()
        self.sealed = False

    def _write(self, name: str) -> None:
        if self.sealed:
            raise AssertionError(f"{name} wrote after the store was sealed")
        if name in self.failing:
            raise StoreError(name)

    def _effect(self, effect_id: str, **changes: object) -> None:
        self.effects[effect_id] = replace(self.effects[effect_id], **changes)

    def _operation(self, operation_id: str, **changes: object) -> None:
        self.operations[operation_id] = replace(self.operations[operation_id], **changes)

    def enabled(self) -> bool:
        return self.switch

    def set_enabled(self, value: bool, *, operation_id: str | None) -> bool:
        self._write("set_enabled")
        self.switch = value
        return True

    def entry_id(self) -> int | None:
        return self.entry

    def bind_entry(self, channel_id: int, *, operation_id: str | None) -> bool:
        self._write("bind_entry")
        self.entry = channel_id
        return True

    def clear_entry(self) -> None:
        self._write("clear_entry")
        self.entry = None

    def rooms(self) -> list[int]:
        return list(self.owners)

    def room_count(self) -> int:
        return len(self.owners)

    def is_room(self, channel_id: int) -> bool:
        return channel_id in self.owners

    def room_of(self, owner_id: int) -> int | None:
        return next((channel for channel, owner in self.owners.items() if owner == owner_id), None)

    def owner_of(self, channel_id: int) -> int | None:
        return self.owners.get(channel_id)

    def drop_room(self, channel_id: int) -> None:
        self._write("drop_room")
        self.owners.pop(channel_id, None)

    def creating(self, owner_id: int) -> bool:
        return any(
            effect.kind == "voice.create" and effect.status in {"pending", "unknown"}
            and effect.payload.get("owner_id") == owner_id
            for effect in self.effects.values()
        )

    def begin_create(self, operation_id: str, spec: RoomSpec) -> None:
        self._write("begin_create")
        inputs = spec.inputs()
        self.operations[operation_id] = OperationRecord(operation_id, "voice.create", inputs, "running", None, None)
        self.effects[operation_id] = DiscordEffect(
            operation_id, operation_id, "voice.create", "unknown", spec.entry_channel_id, None, None, inputs, None, 0, 0,
        )

    def reject_create(self, operation_id: str, spec: RoomSpec) -> None:
        self._write("reject_create")
        self._effect(operation_id, status="failed", error="creation_rejected")
        self._operation(operation_id, status="failed", result={"ok": False})

    def record_create(self, operation_id: str, spec: RoomSpec, channel_id: int) -> None:
        self._write("record_create")
        self.owners[channel_id] = spec.owner_id
        self._operation(operation_id, status="done", result={"ok": True, "channel_id": channel_id},
                        resource_id=str(channel_id))
        self._effect(operation_id, status="done")

    def compensate_create(self, operation_id: str, spec: RoomSpec, channel_id: int, *, deleted: bool) -> None:
        self._write("compensate_create")
        self._operation(operation_id, status="failed" if deleted else "running", resource_id=str(channel_id),
                        result={"ok": False, "channel_id": channel_id, "needs_reconciliation": not deleted})
        if deleted:
            self._effect(operation_id, status="failed", error="compensated")
        else:
            self._effect(operation_id, status="unknown", error="registration_and_compensation_failed")

    def effect(self, effect_id: str) -> DiscordEffect | None:
        return self.effects.get(effect_id)

    def operation(self, operation_id: str | None) -> OperationRecord | None:
        return self.operations.get(operation_id)

    def confirm_create(self, effect: DiscordEffect, channel_id: int, *, register: bool) -> None:
        self._write("confirm_create")
        if register:
            self.owners[channel_id] = effect.payload["owner_id"]
        self._operation(effect.operation_id, status="done", result={"ok": True, "channel_id": channel_id},
                        resource_id=str(channel_id))
        self._effect(effect.effect_id, status="done")

    def claim_delete(self, channel_id: int, guild_id: int, *, allow_unknown: bool) -> DeleteClaim:
        self._write("claim_delete")
        effect_id = f"voice.delete:{channel_id}"
        if effect_id not in self.effects:
            self.effects[effect_id] = DiscordEffect(
                effect_id, None, "voice.delete", "pending", channel_id, None, None,
                {"channel_id": channel_id, "guild_id": guild_id}, None, 0, 0,
            )
        status = self.effects[effect_id].status
        if status == "done":
            self.owners.pop(channel_id, None)
            return "done"
        if status == "skipped":
            return "skipped"
        if status in {"unknown", "failed"} and not allow_unknown:
            return "held"
        self._effect(effect_id, status="unknown")
        return "tracked"

    def fail_delete(self, channel_id: int, error: str) -> None:
        self._write("fail_delete")
        self._effect(f"voice.delete:{channel_id}", status="unknown", error=error)

    def finish_delete(self, channel_id: int, *, tracked: bool) -> None:
        self._write("finish_delete")
        self.owners.pop(channel_id, None)
        if tracked:
            self._effect(f"voice.delete:{channel_id}", status="done")

    def settle_delete(self, effect: DiscordEffect) -> None:
        self._write("settle_delete")
        self.owners.pop(effect.payload["channel_id"], None)
        self._effect(effect.effect_id, status="done")


@dataclass
class Member:
    id: int
    bot: bool = False
    channel: int | None = None
    name: str = "成員"


@dataclass
class Channel:
    id: int
    shape: RoomShape = field(default_factory=shape)
    denied: set[int] = field(default_factory=set)


class FakeGateway:
    def __init__(self) -> None:
        self.guild: int | None = GUILD
        self.connection = 0
        self.channels: dict[int, Channel] = {}
        self.members: dict[int, Member] = {}
        self.missing: tuple[str, ...] | None = ()
        self.fetched: dict[int, bool | GatewayError] = {}
        self.ids = itertools.count(1000)
        self.created: list[RoomSpec] = []
        self.deleted: list[int] = []
        self.moves: list[tuple[int, int]] = []
        self.notices: list[tuple[int, str]] = []
        self.failures: dict[str, BaseException] = {}
        self.during: dict[str, Callable[[], Awaitable[None] | None]] = {}

    def channel(self, channel_id: int | None = None, **values: object) -> Channel:
        channel = Channel(next(self.ids) if channel_id is None else channel_id, **values)
        self.channels[channel.id] = channel
        return channel

    def member(self, member_id: int, **values: object) -> Member:
        self.members[member_id] = Member(member_id, **values)
        return self.members[member_id]

    def reconnect(self) -> None:
        self.connection += 1

    async def _call(self, name: str) -> None:
        if (hook := self.during.get(name)) is not None:
            result = hook()
            if asyncio.iscoroutine(result):
                await result
        if (failure := self.failures.get(name)) is not None:
            raise failure

    def _voice(self, channel_id: int) -> Channel | None:
        return self.channels.get(channel_id) if self.guild is not None else None

    def _member(self, member_id: int) -> Member | None:
        return self.members.get(member_id) if self.guild is not None else None

    def guild_id(self) -> int | None:
        return self.guild

    def session(self) -> Callable[[], bool]:
        guild, connection = self.guild, self.connection
        return lambda: guild is not None and (self.guild, self.connection) == (guild, connection)

    def is_voice(self, channel_id: int) -> bool:
        return self._voice(channel_id) is not None

    def occupied(self, channel_id: int) -> bool:
        return self.is_voice(channel_id) and any(member.channel == channel_id for member in self.members.values())

    def humans(self, channel_id: int) -> tuple[int, ...]:
        return tuple(member.id for member in self.members.values() if member.channel == channel_id and not member.bot)

    def in_entry(self, member_id: int, entry_id: int) -> bool:
        member = self._member(member_id)
        return member is not None and member.channel == entry_id and self.can_join(member_id, entry_id)

    def can_join(self, member_id: int, channel_id: int) -> bool:
        channel = self._voice(channel_id)
        return self._member(member_id) is not None and channel is not None and member_id not in channel.denied

    def bot_missing(self, channel_id: int) -> tuple[str, ...] | None:
        return self.missing

    def category_id(self, channel_id: int) -> int | None:
        channel = self._voice(channel_id)
        return channel.shape.category_id if channel is not None else None

    def plan(self, member_id: int, entry_id: int) -> RoomSpec:
        member, entry = self.members[member_id], self.channels[entry_id]
        return RoomSpec(member_id, self.guild, entry_id, replace(
            entry.shape, name=build_temp_voice_name(member.name), overwrites=(),
        ))

    def shape(self, channel_id: int) -> RoomShape | None:
        channel = self._voice(channel_id)
        return channel.shape if channel is not None else None

    async def exists(self, channel_id: int) -> bool:
        await self._call("exists")
        return channel_id in self.channels

    async def create(self, spec: RoomSpec) -> int:
        self.created.append(spec)
        room = self.channel(shape=spec.shape)
        await self._call("create")
        return room.id

    async def move(self, member_id: int, channel_id: int) -> None:
        await self._call("move")
        self.moves.append((member_id, channel_id))
        self.members[member_id].channel = channel_id

    async def delete(self, channel_id: int) -> None:
        await self._call("delete")
        self.deleted.append(channel_id)
        self.channels.pop(channel_id, None)

    async def notify(self, channel_id: int, text: str) -> None:
        await self._call("notify")
        self.notices.append((channel_id, text))
