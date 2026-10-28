from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from src.core.domain.json import json_value

CHANNEL_NAME_PREFIX = "▍"
CHANNEL_NAME_SUFFIX = " 的語音-🔊"
CHANNEL_NAME_LIMIT = 100
REQUIRED_PERMISSIONS = (
    ("view_channel", "檢視頻道"),
    ("connect", "連線"),
    ("manage_channels", "管理頻道"),
    ("manage_roles", "管理身分組"),
    ("move_members", "移動成員"),
    ("mute_members", "靜音成員"),
    ("deafen_members", "拒聽成員"),
)
OWNER_PERMISSIONS = ("view_channel", "connect", "manage_channels", "move_members", "mute_members", "deafen_members")
RECORDED = frozenset({
    "guild_id", "owner_id", "name", "category_id", "overwrites", "user_limit", "bitrate", "rtc_region",
    "video_quality_mode",
})


@dataclass(frozen=True, slots=True)
class VoiceStatus:
    entry_channel_id: int | None
    room_count: int


@dataclass(frozen=True, slots=True, order=True)
class Overwrite:
    target_type: Literal["member", "role"]
    target_id: int
    allow: int
    deny: int

    def value(self) -> dict[str, object]:
        return {"target_id": self.target_id, "target_type": self.target_type, "allow": self.allow, "deny": self.deny}


@dataclass(frozen=True, slots=True)
class RoomShape:
    name: str
    category_id: int | None
    overwrites: tuple[Overwrite, ...]
    user_limit: int
    bitrate: int
    rtc_region: str | None
    video_quality_mode: int

    def values(self) -> dict[str, object]:
        return {
            "name": self.name, "category_id": self.category_id,
            "overwrites": [overwrite.value() for overwrite in sorted(self.overwrites)],
            "user_limit": self.user_limit, "bitrate": self.bitrate, "rtc_region": self.rtc_region,
            "video_quality_mode": self.video_quality_mode,
        }

    def matches(self, recorded: Mapping[str, object]) -> bool:
        return all(json_value(value) == json_value(recorded[key]) for key, value in self.values().items())


@dataclass(frozen=True, slots=True)
class RoomSpec:
    owner_id: int
    guild_id: int
    entry_channel_id: int
    shape: RoomShape

    def inputs(self) -> dict[str, object]:
        shape = self.shape.values()
        return {
            "owner_id": self.owner_id, "guild_id": self.guild_id, "entry_channel_id": self.entry_channel_id,
            "category_id": shape["category_id"], "name": shape["name"], "overwrites": shape["overwrites"],
            "user_limit": shape["user_limit"], "bitrate": shape["bitrate"], "rtc_region": shape["rtc_region"],
            "video_quality_mode": shape["video_quality_mode"],
        }


def build_temp_voice_name(display_name: str) -> str:
    collapsed = " ".join(display_name.split())
    cleaned = "".join(character for character in collapsed if character.isprintable()) or "使用者"
    available = CHANNEL_NAME_LIMIT - len(CHANNEL_NAME_PREFIX) - len(CHANNEL_NAME_SUFFIX)
    return f"{CHANNEL_NAME_PREFIX}{cleaned[:available]}{CHANNEL_NAME_SUFFIX}"


def bot_problem(missing: tuple[str, ...] | None) -> str | None:
    if missing is None:
        return "目前無法確認 Bot 的伺服器成員狀態，請稍後重新整理。"
    return f"Bot 缺少權限：{'、'.join(missing)}。" if missing else None


def creation_notice(member_id: int, problem: str) -> str:
    return f"<@{member_id}>，無法建立你的語音房：{problem}請通知管理員。"
