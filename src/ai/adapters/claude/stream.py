from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

type Progress = Callable[[str, str], None]

BLOCK_STAGES = {
    "thinking": "thinking",
    "redacted_thinking": "thinking",
    "server_tool_use": "searching",
    "text": "generating",
}


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(slots=True)
class StreamCollector:
    on_progress: Progress | None = None
    stop_reason: str | None = None
    _text: list[str] = field(default_factory=list)
    _thinking: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self._text).strip()

    def _progress(self, stage: str, summary: str = "") -> None:
        if self.on_progress is not None:
            self.on_progress(stage, summary)

    def __call__(self, event: dict[str, Any]) -> None:
        match event.get("type"):
            case "content_block_start":
                if stage := BLOCK_STAGES.get(_dict(event.get("content_block")).get("type")):
                    self._thinking.clear()
                    self._progress(stage)
            case "content_block_delta":
                delta = _dict(event.get("delta"))
                match delta.get("type"):
                    case "text_delta":
                        self._text.append(str(delta.get("text") or ""))
                    case "thinking_delta" if thinking := str(delta.get("thinking") or ""):
                        self._thinking.append(thinking)
                        self._progress("thinking", "".join(self._thinking))
            case "message_delta":
                reason = _dict(event.get("delta")).get("stop_reason")
                self.stop_reason = reason if isinstance(reason, str) else self.stop_reason
