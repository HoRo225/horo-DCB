import json
from functools import cache
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "codex" / "recording.jsonl"
USAGE_KEYS = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")


@cache
def recording() -> tuple[dict, ...]:
    return tuple(json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines())


def rewrite(value, mapping: dict[str, str]):
    if isinstance(value, dict):
        return {key: rewrite(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite(item, mapping) for item in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def recorded_turn(scenario: str, thread_id: str, turn_id: str) -> list[dict]:
    rows = [row for row in recording() if row["scenario"] == scenario]
    start = next(index for index, row in enumerate(rows) if row["direction"] == "client" and row["message"].get("method") == "turn/start")
    request = rows[start]["message"]
    response = next(row["message"] for row in rows[start:] if row["direction"] == "server" and row["message"].get("id") == request["id"] and "method" not in row["message"])
    mapping = {request["params"]["threadId"]: thread_id, response["result"]["turn"]["id"]: turn_id}
    messages = []
    for row in rows[start + 1:]:
        if row["direction"] != "server" or "method" not in row["message"]:
            continue
        messages.append(rewrite(row["message"], mapping))
        if row["message"]["method"] == "turn/completed":
            break
    return messages


def recorded_answer(scenario: str) -> str:
    rows = [row for row in recording() if row["scenario"] == scenario]
    completed = next(row["message"] for row in rows if row["message"].get("method") == "turn/completed")
    return "\n\n".join(item["text"] for item in completed["params"]["turn"]["items"] if item["type"] == "agentMessage")


def note(method: str, thread_id: str, turn_id: str, **params) -> dict:
    return {"method": method, "params": {"threadId": thread_id, "turnId": turn_id, **params}}


def agent(item_id: str, text: str = "", phase: str | None = "final_answer") -> dict:
    return {"type": "agentMessage", "id": item_id, "text": text, "phase": phase}


def reasoning(item_id: str, summary: list[str] | None = None) -> dict:
    return {"type": "reasoning", "id": item_id, "summary": summary or [], "content": []}


def web_search(item_id: str, action: dict) -> dict:
    return {"type": "webSearch", "id": item_id, "query": "", "action": action}


def tool_item(item_id: str, tool: str = "read_messages", success: bool | None = None) -> dict:
    return {"type": "dynamicToolCall", "id": item_id, "namespace": "discord", "tool": tool, "arguments": {}, "success": success}


def started(thread_id: str, turn_id: str, item: dict) -> dict:
    return note("item/started", thread_id, turn_id, item=item)


def completed(thread_id: str, turn_id: str, item: dict) -> dict:
    return note("item/completed", thread_id, turn_id, item=item)


def delta(thread_id: str, turn_id: str, item_id: str, text: str) -> dict:
    return note("item/agentMessage/delta", thread_id, turn_id, itemId=item_id, delta=text)


def summary_delta(thread_id: str, turn_id: str, item_id: str, text: str, index: int = 0) -> dict:
    return note("item/reasoning/summaryTextDelta", thread_id, turn_id, itemId=item_id, summaryIndex=index, delta=text)


def usage(thread_id: str, turn_id: str, **counts: int) -> dict:
    return note("thread/tokenUsage/updated", thread_id, turn_id, tokenUsage={"total": {key: counts.get(key, 0) for key in USAGE_KEYS}})


def turn_completed(thread_id: str, turn_id: str, status: str = "completed", items: list[dict] | None = None) -> dict:
    return {"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": status, "items": items or []}}}


def answer(thread_id: str, turn_id: str, text: str, item_id: str = "answer") -> list[dict]:
    return [
        started(thread_id, turn_id, agent(item_id)),
        delta(thread_id, turn_id, item_id, text),
        completed(thread_id, turn_id, agent(item_id, text)),
        turn_completed(thread_id, turn_id, items=[agent(item_id, text)]),
    ]
