from __future__ import annotations

from urllib.parse import urlsplit

MAX_IMAGE_ATTACHMENTS = 12
MAX_PROMPT_CHARACTERS = 4000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_TOTAL_BYTES = 50 * 1024 * 1024
MAX_ACTIVE_KEYS = 4
MAX_CHAT_BUDGET_MS = 300_000
WORK_TIMEOUT_SECONDS = MAX_CHAT_BUDGET_MS / 1000 + 30
CLEANUP_SECONDS = 5.0


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


def conversation_user(key: str) -> int | None:
    _channel, separator, user = key.rpartition(":user:")
    return int(user) if separator and user.isdigit() else None


def scope_matches(
    key: str,
    guild_id: int,
    channel_id: int | None = None,
    *,
    parent_channel_id: int | None = None,
    include_children: bool = False,
) -> bool:
    prefix = f"guild:{guild_id}:"
    if not key.startswith(prefix):
        return False
    if channel_id is None:
        return True
    if key.startswith(f"{prefix}channel:{channel_id}:user:"):
        return True
    if key == f"{prefix}thread:{channel_id}":
        return True
    return (
        include_children
        and key.startswith(f"{prefix}thread:")
        and (parent_channel_id is None or parent_channel_id == channel_id)
    )


def safe_https_hostname(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        return None
    return parsed.hostname.casefold()


def is_discord_media_url(url: str) -> bool:
    host = safe_https_hostname(url)
    return host is not None and (
        host in {"cdn.discordapp.com", "media.discordapp.net"}
        or host.endswith(".discordapp.com")
        or host.endswith(".discordapp.net")
    )

