import pytest

from horo_dcb.chat import route
from horo_dcb.store import Entry

ASKER = 1
OTHER = 2


@pytest.mark.parametrize(
    ("bot_reply", "active_asker", "entry", "latest", "busy", "expected"),
    [
        (False, None, None, None, False, "new"),
        (False, ASKER, Entry("t", "a"), "a", True, "new"),
        (True, ASKER, Entry("t", None), None, True, "steer"),
        (True, OTHER, Entry("t", None), None, True, "queue"),
        (True, None, None, None, False, "new"),
        (True, None, None, None, True, "new"),
        (True, None, Entry("t", None), "a", False, "continue"),
        (True, None, Entry("t", None), "a", True, "queue"),
        (True, None, Entry("t", "a"), None, False, "continue"),
        (True, None, Entry("t", "a"), None, True, "queue"),
        (True, None, Entry("t", "a"), "a", False, "continue"),
        (True, None, Entry("t", "a"), "a", True, "fork"),
        (True, None, Entry("t", "a"), "b", False, "fork"),
        (True, None, Entry("t", "a"), "b", True, "fork"),
    ],
    ids=[
        "mention-only",
        "mention-while-other-round-active",
        "asker-replies-to-active-round",
        "other-user-replies-to-active-round",
        "unknown-bot-message",
        "unknown-bot-message-busy",
        "legacy-entry-idle",
        "legacy-entry-busy",
        "thread-without-latest-idle",
        "thread-without-latest-busy",
        "latest-answer-idle",
        "latest-answer-busy",
        "older-answer-idle",
        "older-answer-busy",
    ],
)
def test_route(bot_reply, active_asker, entry, latest, busy, expected):
    assert route(author=ASKER, bot_reply=bot_reply, active_asker=active_asker, entry=entry, latest=latest, busy=busy) == expected
