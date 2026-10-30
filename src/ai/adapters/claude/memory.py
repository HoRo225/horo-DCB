from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from src.ai.domain.conversation import conversation_user


@dataclass(slots=True)
class Conversation:
    key: str
    user_id: int | None
    parent_channel_id: int | None
    used_at: float
    turns: deque[tuple[str, str]]


class ConversationMemory:
    def __init__(
        self, *, max_turns: int = 20, max_chars: int = 24_000, idle_seconds: float = 86_400,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_turns = max_turns
        self._max_chars = max_chars
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._conversations: dict[str, Conversation] = {}

    def __len__(self) -> int:
        return len(self._conversations)

    def history(self, key: str) -> list[dict[str, str]]:
        self.sweep()
        conversation = self._conversations.get(key)
        kept: list[tuple[str, str]] = []
        total = 0
        for user, assistant in reversed(conversation.turns if conversation is not None else ()):
            total += len(user) + len(assistant)
            if total > self._max_chars:
                break
            kept.append((user, assistant))
        return [
            message
            for user, assistant in reversed(kept)
            for message in ({"role": "user", "content": user}, {"role": "assistant", "content": assistant})
        ]

    def append(self, key: str, user: str, assistant: str, *, parent_channel_id: int | None = None) -> None:
        self.sweep()
        now = self._clock()
        conversation = self._conversations.get(key)
        if conversation is None:
            conversation = Conversation(key, conversation_user(key), parent_channel_id, now,
                                        deque(maxlen=self._max_turns))
            self._conversations[key] = conversation
        conversation.used_at = now
        conversation.turns.append((user, assistant))

    def forget_where(self, predicate: Callable[[Conversation], bool]) -> int:
        doomed = [key for key, conversation in self._conversations.items() if predicate(conversation)]
        for key in doomed:
            del self._conversations[key]
        return len(doomed)

    def sweep(self) -> int:
        cutoff = self._clock() - self._idle_seconds
        return self.forget_where(lambda conversation: conversation.used_at <= cutoff)
