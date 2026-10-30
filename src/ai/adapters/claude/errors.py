from __future__ import annotations

from src.ai.adapters.claude.client import ClaudeConnectionError, ClaudeHttpError
from src.ai.app.ports import ModelError


def retryable(exc: Exception) -> bool:
    return isinstance(exc, ClaudeConnectionError) or isinstance(exc, ClaudeHttpError) and (
        exc.status == 429 or exc.status >= 500
    )


def normalize_error(exc: Exception) -> ModelError:
    if isinstance(exc, ClaudeHttpError):
        if exc.status in (401, 403):
            return ModelError("auth")
        if retryable(exc):
            return ModelError("upstream")
        return ModelError("invalid_request")
    if isinstance(exc, TimeoutError):
        return ModelError("timeout")
    return ModelError("unavailable")
