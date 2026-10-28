from __future__ import annotations

from typing import Any

from src.ai.background import ask_once, clean_reply, persona_prompt

REMARK_SECONDS = 45
LINE_EFFORT = "low"


def ready(bot: Any) -> bool:
    return bot.ai_settings.enabled and not bot.codex.quota_full()


async def round_line(bot: Any, channel_id: int, data: dict[str, Any]) -> str | None:
    prompt = persona_prompt("下面是成員剛和你玩完的一局遊戲。用繁體中文回一句 30 字內的台詞（得意、調侃或不甘心都行），"
                            "只寫一行，不要標題。", data)
    reply = await ask_once(bot, channel_id, prompt, timeout=REMARK_SECONDS, effort=LINE_EFFORT, kind="line")
    return clean_reply(next((line for line in (reply or "").splitlines() if line.strip()), None), 60)
