from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.ai.adapters.discord.context import require_short
from src.ai.adapters.discord.help import help_view
from src.ai.adapters.discord.output import reply, reply_notice
from src.ai.app.ports import ModelError
from src.ai.domain.intent import Intent, envelope_schema, parse_envelope
from src.ai.domain.models import ModelSettings
from src.ai.domain.prompts import SUMMARY_INSTRUCTIONS, intent_instructions
from src.core.adapters.discord.receipts import receipt_view

if TYPE_CHECKING:
    from src.ai.adapters.discord.request import AiRequest

PUBLIC_FIELDS = frozenset({"ok", "code", "text", "resource_id", "message_url"})


async def route_intent(request: AiRequest, models: ModelSettings) -> bool:
    if not request.content.strip():
        return False
    require_short(request.content)
    services = request.services
    actions, model = services.actions, services.model
    prior = actions.prior(request.message.id)
    if prior is not None and not prior[0].actions:
        request.outcome = "operations"
        return True
    if prior is not None:
        intent, received = Intent("plan", prior[0]), prior[1]
    else:
        catalogue = actions.catalogue
        with request.timed("sdk"), request.timed("intent"):
            intent = await model.structured(
                request.key, {"direct_text": request.content, "context": request.context},
                instructions=intent_instructions(catalogue.hints()), schema=envelope_schema(catalogue),
                parse=lambda raw: parse_envelope(raw, catalogue), job=request.job, models=models,
                request_id=request.id,
            )
        received = request.context
    if intent.kind == "chat":
        return False
    await request.progress.stop()
    request.output_started = True
    if intent.kind == "clarification":
        if await request.can_send():
            await reply_notice(request.message, intent.question, tone=None)
        request.outcome = "clarification"
        return True
    if [action.name for action in intent.plan.actions] == ["help"]:
        if await request.can_send():
            await reply(request.message, view=help_view(services.games.channel_id, request.bot_name))
        request.outcome = "help"
        return True
    outcome = await actions.execute(request.source, intent.plan, received=received,
                                    deadline=request.job.work_deadline, grant=request.granted)
    results = outcome.results
    request.outcome = "operations"
    if not outcome.published:
        if await request.can_send():
            public = [{key: value for key, value in result.items() if key != "private_ref"} for result in results]
            await reply(request.message, view=receipt_view(public, name=request.bot_name))
        return True
    await request.progress.finish()
    if prior is None and request.job.current and await request.can_send():
        public = [{key: value for key, value in result.items() if key in PUBLIC_FIELDS} for result in results]
        try:
            with request.timed("sdk"), request.timed("summary"):
                summary = await model.summarize(request.key, public, instructions=SUMMARY_INSTRUCTIONS,
                                                job=request.job, models=models, request_id=request.id)
            if await request.can_send():
                await actions.explain(request.source, summary.text, grant=request.granted)
        except (ModelError, TimeoutError):
            logging.warning("AI result explanation unavailable; the confirmed receipt remains.")
    return True
