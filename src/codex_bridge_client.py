from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CodexAccess:
    enabled: bool
    guild_id: int | None
    channel_id: int | None
    user_ids: frozenset[int]

    def allows(
        self,
        guild_id: int | None,
        channel_id: int | None,
        user_id: int,
    ) -> bool:
        return (
            self.enabled
            and guild_id == self.guild_id
            and channel_id == self.channel_id
            and user_id in self.user_ids
        )


def conversation_key(
    guild_id: int,
    channel_id: int,
    user_id: int,
    *,
    is_thread: bool,
) -> str:
    if is_thread:
        return f"guild:{guild_id}:thread:{channel_id}"
    return f"guild:{guild_id}:channel:{channel_id}:user:{user_id}"
