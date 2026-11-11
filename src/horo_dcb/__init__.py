import json
import traceback
from pathlib import Path


LOG_FIELDS = {"duration_ms", "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens", "usage_incomplete", "rpc_code"}


def log_event(code: str, error: BaseException | None = None, /, **metrics: int | float | bool | None) -> None:
    event = {"code": code}
    for key, value in metrics.items():
        if key in LOG_FIELDS and isinstance(value, (int, float, bool)):
            event[key] = value
    if error is not None:
        event["error_type"] = type(error).__name__
        event["locations"] = [
            {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(error.__traceback__)
        ]
    print(json.dumps(event, ensure_ascii=False), flush=True)
