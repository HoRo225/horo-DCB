from __future__ import annotations

import discord

from src.core.adapters.discord.live import name_text

_ERROR_TEXT = {
    "busy": "{name} 目前忙碌，請稍後再試。",
    "not_allowed": "{name} 目前沒有在這個頻道或你的身分組開放。",
    "not_allowed_channel": "這個頻道不能找{name}聊天，請到 {channels} 再 @{name}。",
    "not_allowed_role": "你目前沒有使用{name} AI 的身分組，需要的話請找管理員。",
    "timeout": "{name} 回覆逾時，請稍後再試。",
    "model_configuration_invalid": "AI 模型設定目前無法使用，請聯絡管理員檢查模型。",
    "auth": "{name} 的 AI 帳號登入已失效，請聯絡管理員重新登入 clewdr。",
    "upstream": "AI 服務暫時無法回應，請稍後再試。",
    "refusal": "這個問題 AI 無法回答。",
}


def ai_error_text(code: str, name: str, *, channels: str = "") -> str:
    text = _ERROR_TEXT.get(code, "{name} 暫時無法回覆，請稍後再試。")
    return name_text(text.format(name="{name}", channels=channels), discord.utils.escape_markdown(name))
