import json

from horo_dcb import log_event


def emitted(capsys) -> dict:
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_only_whitelisted_numeric_metrics_are_logged(capsys):
    log_event("turn_usage", duration_ms=12, total_tokens=5, usage_incomplete=False, rpc_code=None, secret=7, output_tokens="9")
    assert emitted(capsys) == {"code": "turn_usage", "duration_ms": 12, "total_tokens": 5, "usage_incomplete": False}


def test_errors_are_logged_as_locations_without_the_message(capsys):
    try:
        raise ValueError("讀不到的使用者內容")
    except ValueError as error:
        raised_at = error.__traceback__.tb_lineno
        log_event("turn_error", error)
    event = emitted(capsys)
    assert event == {"code": "turn_error", "locations": [{"file": "test_log_event.py", "line": raised_at, "function": "test_errors_are_logged_as_locations_without_the_message"}]}
