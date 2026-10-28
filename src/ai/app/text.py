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
    def __init__(self, settings: AiSettings, model: TextModel) -> None:
        self._settings = settings
        self._model = model
        self._once_users = itertools.count(1)

    async def ask(
        self, key: str, prompt: str, still_current: Callable[[], bool], *, effort: str | None = None,
    ) -> str | None:
        if not still_current():
            return None
        model = self._model

        async def request() -> str:
            models = self._settings.models
            async with model.accepted_request(
                key,
                deadline=asyncio.get_running_loop().time() + WORK_TIMEOUT_SECONDS,
                still_current=still_current,
            ) as job:
                return (await model.generate_once(
                    key, prompt, instructions=ONCE_INSTRUCTIONS, job=job, models=models, effort=effort,
                )).text

        try:
            return await asyncio.create_task(request())
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            logging.info("AI request for %s was cancelled.", key)
        except Exception:
            logging.exception("AI request for %s failed.", key)
        return None

    async def ask_once(self, channel_id: int, prompt: str, *, timeout: float, effort: str, kind: str) -> str | None:
        settings, model = self._settings, self._model
        active, waiting = model.request_counts()
        started = time.monotonic()
        result = "cancelled"
        try:
            if not settings.enabled:
                result = "off"
                return None
            if waiting or active >= MAX_ACTIVE_KEYS - 1:
                result = "busy"
                return None
            if model.quota_full():
                result = "quota"
                return None
            key = conversation_key(settings.guild_id, channel_id, next(self._once_users), is_thread=False)
            try:
                async with asyncio.timeout(timeout):
                    reply = await self.ask(key, prompt, lambda: settings.enabled, effort=effort)
            except TimeoutError:
                result = "timeout"
                return None
            result = "failed" if reply is None else "ok"
            return reply
        finally:
            logging.info("Game AI %s effort=%s result=%s ms=%d", kind, effort, result, (time.monotonic() - started) * 1000)

    async def round_line(self, channel_id: int, facts: Mapping[str, object]) -> str | None:
        if not self._settings.enabled or self._model.quota_full():
            return None
        prompt = persona_prompt(ROUND_LINE, dict(facts))
        reply = await self.ask_once(channel_id, prompt, timeout=REMARK_SECONDS, effort=LINE_EFFORT, kind="line")
        return clean_line(next((line for line in (reply or "").splitlines() if line.strip()), None), LINE_LIMIT)
