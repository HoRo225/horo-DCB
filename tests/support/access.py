from src.ai.access import CodexAccess


def configured_access(enabled=True, guild_id=10, *, channel_ids=(20,), role_ids=(70,)):
    access = CodexAccess(enabled, guild_id)
    access.set_channels(guild_id, frozenset(channel_ids))
    if role_ids:
        access.set_roles(guild_id, frozenset(role_ids))
    return access
