import pytest

from tests.support import chat_harness
from tests.support.discord_fakes import Channel, member

harness = chat_harness.harness


def plain(harness):
    return harness.post(harness.person(), "大家好")


def from_a_bot(harness):
    return harness.post(member(5, "別的機器人", (chat_harness.ROLE,), bot=True), "嗨", mention=True)


def elsewhere(harness):
    return harness.post(harness.person(), "嗨", mention=True, channel=Channel(201, bot=harness.bot))


def without_role(harness):
    return harness.post(harness.person(role_ids=(999,)), "嗨", mention=True)


def reply_to_a_person(harness):
    other = harness.post(harness.person(2, "路人"), "我說了一句話")
    return harness.post(harness.person(), "回你", reply_to=other.id)


@pytest.mark.parametrize("build", [plain, from_a_bot, elsewhere, without_role, reply_to_a_person])
async def test_messages_that_do_not_address_the_bot_are_ignored(harness, build):
    message = build(harness)
    await harness.chat.receive(message)
    assert harness.chat.rounds == {}
    assert [value["method"] for value in harness.server.received if value.get("method") not in ("initialize", "initialized")] == []
    assert harness.channel.sent == []
