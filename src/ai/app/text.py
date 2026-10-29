from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Callable, Mapping

from src.ai.app.ports import TextModel
from src.ai.app.settings import AiSettings
from src.ai.domain.conversation import MAX_ACTIVE_KEYS, WORK_TIMEOUT_SECONDS, conversation_key
from src.ai.domain.formatting import clean_line
from src.ai.domain.prompts import ONCE_INSTRUCTIONS, ROUND_LINE, persona_prompt

REMARK_SECONDS = 45
LINE_EFFORT = "low"
LINE_LIMIT = 60


class AiText:
    def __init__(self, settings: AiSettings, model: TextModel, bot_name: Callable[[], str]) -> None:
        self._settings = settings
        self._model = model
        self._bot_name = bot_name
        self._once_users = itertools.count(1)

    async def round_line(self, channel_id: int, facts: Mapping[str, object]) -> str | None:
        settings, model = self._settings, self._model
        active, waiting = model.request_counts()
        started = time.monotonic()
        result, reply = "cancelled", None

        async def request(key: str, prompt: str) -> str:
            async with model.accepted_request(
                key,
                deadline=asyncio.get_running_loop().time() + WORK_TIMEOUT_SECONDS,
                still_current=lambda: settings.enabled,
            ) as job:
                return (await model.generate_once(
                    key, prompt, instructions=ONCE_INSTRUCTIONS, job=job, models=settings.models, effort=LINE_EFFORT,
                )).text

        try:
            if not settings.enabled:
                result = "off"
            elif waiting or active >= MAX_ACTIVE_KEYS - 1:
                result = "busy"
            elif model.quota_full():
                result = "quota"
            else:
                key = conversation_key(settings.guild_id, channel_id, next(self._once_users), is_thread=False)
                prompt = persona_prompt(self._bot_name(), ROUND_LINE, dict(facts))
                try:
                    async with asyncio.timeout(REMARK_SECONDS):
                        reply = await asyncio.create_task(request(key, prompt))
                    result = "ok"
                except TimeoutError:
                    result = "timeout"
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        raise
                    result = "failed"
                except Exception:
                    logging.exception("Game AI line request for %s failed.", key)
                    result = "failed"
        finally:
            logging.info("Game AI line effort=%s result=%s ms=%d", LINE_EFFORT, result, (time.monotonic() - started) * 1000)
        return clean_line(next((line for line in (reply or "").splitlines() if line.strip()), None), LINE_LIMIT)
