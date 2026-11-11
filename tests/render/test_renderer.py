import asyncio

import pytest

from tests.support import chat_harness
from tests.support.chat_harness import server_request
from tests.support.codex_messages import agent, answer, completed, delta, note, reasoning, recorded_answer, recorded_turn, started, tool_item, turn_completed, web_search
from tests.support.discord_fakes import card_of
from tests.support.waiting import until

harness = chat_harness.harness


PURPLE = 0x7F77DD


def timer(trigger) -> str:
    return f"-# 開始於 <t:{int(trigger.created_at.timestamp())}:R>"


def frame_of(harness, trigger):
    replies = harness.answers(trigger)
    return card_of(replies[0].views[-1]) if replies else None


async def test_the_thinking_frame_becomes_the_answer_in_one_message(harness):
    harness.script(lambda thread, turn: recorded_turn("answer", thread, turn))
    trigger = await harness.ask(harness.person(), "一週有幾天？", mention=True)
    await harness.settle()
    assert len(harness.channel.sent) == 1
    reply = harness.channel.sent[0]
    assert card_of(reply.views[0]) == (PURPLE, ["💭 - **思考中**", "---", timer(trigger)])
    assert harness.answers(trigger) == [reply]
    assert harness.text(reply) == recorded_answer("answer")


@pytest.mark.parametrize("line", ["long line " * 10, "長" * 90], ids=["ascii", "multibyte"])
async def test_a_long_answer_is_split_across_messages_in_order(harness, line):
    text = "".join(f"第 {index} 段。{line}\n" for index in range(60))
    harness.script(lambda thread, turn: answer(thread, turn, text))
    trigger = await harness.ask(harness.person(), "寫長一點", mention=True)
    await harness.settle()
    replies = harness.answers(trigger)
    assert len(replies) >= 2
    assert all(len(harness.text(value)) <= 4000 and len(harness.text(value).encode()) <= 9000 for value in replies)
    assert harness.answer_text(trigger) == text
    assert [value.id for value in replies] == sorted(value.id for value in replies)


async def test_later_chunks_of_an_uninterrupted_answer_are_not_replies(harness):
    harness.script(lambda thread, turn: answer(thread, turn, "長" * 5000))
    trigger = await harness.ask(harness.person(), "寫長一點", mention=True)
    await harness.settle()
    first, second = harness.channel.live()
    assert first.reference.message_id == trigger.id
    assert second.reference is None
    assert harness.answers(trigger) == [first, second]


async def test_a_chunk_after_someone_else_spoke_replies_to_the_chunk_before_it(harness):
    hold = asyncio.Event()
    harness.script(lambda thread, turn: [hold, *answer(thread, turn, "長" * 5000)])
    trigger = await harness.ask(harness.person(), "寫長一點", mention=True)
    await until(lambda: harness.answers(trigger))
    harness.post(harness.person(2, "路人"), "插話")
    hold.set()
    await harness.settle()
    first, second = harness.channel.live()
    assert first.reference.message_id == trigger.id
    assert second.reference.message_id == first.id
    assert harness.answer_text(trigger) == "長" * 5000


async def test_the_frame_shows_the_status_and_only_the_last_three_steps(harness):
    searching, hold = asyncio.Event(), asyncio.Event()
    harness.script(lambda thread, turn: [
        started(thread, turn, agent("c1", phase="commentary")),
        completed(thread, turn, agent("c1", "我先查天氣", phase="commentary")),
        started(thread, turn, reasoning("r1")),
        completed(thread, turn, reasoning("r1", ["**規劃回答**\n先查天氣再整理"])),
        started(thread, turn, web_search("s1", {"type": "search", "query": "台北天氣"})),
        searching,
        completed(thread, turn, web_search("s1", {"type": "search", "query": "台北天氣"})),
        started(thread, turn, web_search("s2", {"type": "openPage", "url": "https://weather.example.com/taipei"})),
        completed(thread, turn, web_search("s2", {"type": "openPage", "url": "https://weather.example.com/taipei"})),
        note("model/rerouted", thread, turn, fromModel="gpt-5.1", toModel="gpt-5.1-mini"),
        hold,
        *answer(thread, turn, "晴天"),
    ])
    trigger = await harness.ask(harness.person(), "台北天氣？", mention=True)
    await until(lambda: frame_of(harness, trigger) == (PURPLE, ["🔍 - **搜尋中**\n-# `說明` 我先查天氣\n-# `思考` 規劃回答\n`搜尋` 台北天氣", "---", timer(trigger)]))
    searching.set()
    await until(lambda: frame_of(harness, trigger) == (PURPLE, ["💭 - **思考中**\n-# `搜尋` 台北天氣\n-# `瀏覽` weather.example.com\n`模型` gpt-5.1 改用 gpt-5.1-mini", "---", timer(trigger)]))
    hold.set()
    await harness.settle()
    assert harness.answer_text(trigger) == "晴天"


async def test_the_frame_shows_a_tool_call_pending_then_its_note(harness):
    pending, noted = asyncio.Event(), asyncio.Event()
    harness.script(lambda thread, turn: [
        started(thread, turn, tool_item("call-1")),
        pending,
        server_request("item/tool/call", threadId=thread, turnId=turn, callId="call-1", tool="read_messages", arguments={}),
        completed(thread, turn, tool_item("call-1", success=True)),
        noted,
        *answer(thread, turn, "讀完了"),
    ])
    harness.post(harness.person(2, "路人"), "早先的訊息")
    trigger = await harness.ask(harness.person(), "讀一下頻道", mention=True)
    await until(lambda: frame_of(harness, trigger) == (PURPLE, ["📖 - **讀取中**\n`讀取` 頻道訊息", "---", timer(trigger)]))
    pending.set()
    await until(lambda: frame_of(harness, trigger) == (PURPLE, ["💭 - **思考中**\n`讀取` 1 則較早的訊息", "---", timer(trigger)]))
    noted.set()
    await harness.settle()
    assert harness.answer_text(trigger) == "讀完了"


async def test_no_send_or_edit_can_ping_everyone_or_roles(harness):
    hold = asyncio.Event()
    text = "@everyone <@&300> " + "長" * 5000
    harness.script(lambda thread, turn: [started(thread, turn, reasoning("r1")), completed(thread, turn, reasoning("r1", ["**想想**"])), hold, *answer(thread, turn, text)])
    trigger = await harness.ask(harness.person(), "大聲一點", mention=True)
    await until(lambda: harness.answers(trigger) and "想想" in harness.text(harness.answers(trigger)[0]))
    hold.set()
    await harness.settle()
    assert len(harness.answers(trigger)) >= 2
    sends = [value.send_options["allowed_mentions"] for value in harness.channel.sent]
    edits = [call.kwargs["allowed_mentions"] for value in harness.channel.sent for call in value.edit.call_args_list]
    assert edits
    assert all(mentions.everyone is False and mentions.roles is False for mentions in [*sends, *edits])


async def test_an_abandoned_stream_item_leaves_only_the_retried_answer(harness):
    hold = asyncio.Event()
    harness.script(lambda thread, turn: [
        started(thread, turn, agent("first")),
        delta(thread, turn, "first", "半截的舊回答" * 800),
        hold,
        started(thread, turn, agent("second")),
        delta(thread, turn, "second", "重新產生的完整回答"),
        completed(thread, turn, agent("second", "重新產生的完整回答")),
        turn_completed(thread, turn, items=[agent("second", "重新產生的完整回答")]),
    ])
    trigger = await harness.ask(harness.person(), "說個故事", mention=True)
    await until(lambda: len(harness.answers(trigger)) == 2)
    abandoned = harness.answers(trigger)[1]
    hold.set()
    await harness.settle()
    assert [harness.text(value) for value in harness.answers(trigger)] == ["重新產生的完整回答"]
    assert abandoned.id in harness.channel.deleted
    assert harness.store.entry_for(abandoned.id) is None
