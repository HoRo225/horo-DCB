import asyncio
import json

import pytest

from horo_dcb.chat import TOOL_IMAGE_LABEL, WAITING_TEXT
from tests.support import chat_harness
from tests.support.app_server import Reject
from tests.support.chat_harness import data_url, default_answer, picture, png, server_request
from tests.support.codex_messages import agent, answer, completed, delta, note, recorded_answer, recorded_turn, started, tool_item, turn_completed
from tests.support.waiting import until

harness = chat_harness.harness


def partial(thread, turn):
    return [started(thread, turn, agent("a")), delta(thread, turn, "a", "半截")]


def assert_cleaned_up(harness):
    assert harness.channel.sent
    assert harness.channel.live() == []
    assert all(harness.store.entry_for(value.id) is None for value in harness.channel.sent)


@pytest.mark.parametrize("ending", [
    lambda thread, turn: [*partial(thread, turn), turn_completed(thread, turn, "failed")],
    lambda thread, turn: [turn_completed(thread, turn)],
], ids=["failed", "empty"])
async def test_a_turn_that_ends_without_an_answer_is_cleaned_up(harness, ending):
    harness.script(ending)
    await harness.ask(harness.person(), "嗨", mention=True)
    await harness.settle()
    assert_cleaned_up(harness)
    assert harness.store.latest_turn("thread-1") is None


@pytest.mark.parametrize("ending", [
    lambda thread, turn: [*partial(thread, turn), note("error", thread, turn, error={"message": "stream disconnected"}, willRetry=False)],
    lambda thread, turn: [*partial(thread, turn), server_request("item/commandExecution/requestApproval", threadId=thread, turnId=turn, itemId="cmd")],
], ids=["error", "unsupported-request"])
async def test_a_running_turn_that_breaks_is_interrupted_and_cleaned_up(harness, ending):
    harness.script(ending)
    await harness.ask(harness.person(), "嗨", mention=True)
    await harness.settle()
    assert_cleaned_up(harness)
    assert harness.server.requests("turn/interrupt") == [{"threadId": "thread-1", "turnId": "turn-1"}]
    assert harness.store.latest_turn("thread-1") is None


async def test_a_disconnect_mid_answer_is_cleaned_up(harness):
    harness.script(lambda thread, turn: [*partial(thread, turn), harness.server.drop])
    await harness.ask(harness.person(), "嗨", mention=True)
    await harness.settle()
    assert_cleaned_up(harness)
    assert harness.server.connections == 2


async def test_a_message_still_being_sent_when_the_round_aborts_is_deleted(harness):
    harness.channel.send_gate = asyncio.Event()
    harness.script(lambda thread, turn: [note("error", thread, turn, error={"message": "boom"}, willRetry=False)])
    await harness.ask(harness.person(), "嗨", mention=True)
    await until(lambda: harness.server.requests("turn/interrupt"))
    assert harness.channel.sent == []
    harness.channel.send_gate.set()
    await harness.settle()
    assert len(harness.channel.sent) == 1
    assert_cleaned_up(harness)


async def test_a_frame_deleted_by_a_moderator_stops_the_round(harness):
    resume, never = asyncio.Event(), asyncio.Event()
    harness.script(lambda thread, turn: [*partial(thread, turn), resume, delta(thread, turn, "a", "後半"), never])
    trigger = await harness.ask(harness.person(), "嗨", mention=True)
    await until(lambda: harness.answers(trigger) and harness.text(harness.answers(trigger)[0]) == "半截")
    frame = harness.answers(trigger)[0]
    harness.channel.delete(frame)
    resume.set()
    await harness.settle()
    assert harness.server.requests("turn/interrupt") == [{"threadId": "thread-1", "turnId": "turn-1"}]
    assert harness.store.entry_for(frame.id) is None
    assert harness.channel.live() == []


async def test_the_cursor_advances_only_after_turn_start_succeeds(harness):
    asker = harness.person()
    first = await harness.ask(asker, "第一問", mention=True)
    await harness.settle()
    assert harness.store.cursor("thread-1") == first.id
    harness.start_reply = lambda params: Reject()
    await harness.ask(asker, "第二問", reply_to=harness.answers(first)[0].id)
    await harness.settle()
    assert len(harness.server.requests("turn/start")) == 2
    assert harness.store.cursor("thread-1") == first.id
    assert len(harness.channel.live()) == 1


async def test_a_recorded_tool_call_reads_the_channel_and_answers(harness):
    harness.script(lambda thread, turn: recorded_turn("tool", thread, turn))
    harness.post(harness.person(2, "路人"), "早先的訊息")
    trigger = await harness.ask(harness.person(), "讀一下頻道", mention=True)
    await harness.settle()
    [reply] = harness.replies
    assert reply["result"]["success"] is True
    read = json.loads(reply["result"]["contentItems"][0]["text"])
    assert [value["text"] for value in read["messages"]] == ["早先的訊息"]
    assert harness.answer_text(trigger) == recorded_answer("tool")


def viewing(target: int):
    return lambda thread, turn: [
        started(thread, turn, tool_item("call-1", "view_message_images")),
        server_request("item/tool/call", threadId=thread, turnId=turn, callId="call-1", tool="view_message_images", arguments={"message": str(target)}),
        completed(thread, turn, tool_item("call-1", "view_message_images", success=True)),
        *answer(thread, turn, "看到紅色"),
    ]


async def test_tool_images_are_steered_in_before_the_tool_answers(harness):
    red = png("red")
    shown = harness.post(harness.person(2, "路人"), "紅色的圖", attachments=(picture(red, 7),))
    harness.script(viewing(shown.id))
    await harness.ask(harness.person(), "那張圖是什麼顏色", mention=True)
    await harness.settle()
    [steer] = harness.server.requests("turn/steer")
    assert (steer["threadId"], steer["expectedTurnId"]) == ("thread-1", "turn-1")
    assert steer["input"] == [{"type": "text", "text": TOOL_IMAGE_LABEL, "text_elements": []}, {"type": "image", "url": data_url(red)}]
    [reply] = harness.replies
    assert reply["result"]["success"] is True
    received = harness.server.received
    steered = next(index for index, value in enumerate(received) if value.get("method") == "turn/steer")
    responded = next(index for index, value in enumerate(received) if value.get("id") == reply["id"] and "method" not in value)
    assert steered < responded


async def test_a_rejected_image_steer_fails_the_tool(harness):
    harness.server.handlers["turn/steer"] = lambda params: Reject()
    shown = harness.post(harness.person(2, "路人"), "紅色的圖", attachments=(picture(png("red"), 7),))
    harness.script(viewing(shown.id))
    await harness.ask(harness.person(), "那張圖是什麼顏色", mention=True)
    await harness.settle()
    [reply] = harness.replies
    assert reply["result"]["success"] is False


async def test_a_cancelled_queued_round_does_not_admit_the_next_one_early(harness):
    hold = asyncio.Event()
    harness.script(lambda thread, turn: [started(thread, turn, agent("a")), delta(thread, turn, "a", "第一段"), hold, completed(thread, turn, agent("a", "第一段")), turn_completed(thread, turn, items=[agent("a", "第一段")])])
    first = await harness.ask(harness.person(), "第一問", mention=True)
    await until(lambda: harness.answers(first) and harness.text(harness.answers(first)[0]) == "第一段")
    reply = harness.answers(first)[0]
    leaving = await harness.ask(harness.person(2, "第二位"), "我先問", reply_to=reply.id)
    staying = await harness.ask(harness.person(3, "第三位"), "我也問", reply_to=reply.id)
    await until(lambda: harness.answers(leaving) and harness.answers(staying))
    harness.chat.rounds[leaving.id].task.cancel()
    await until(lambda: leaving.id not in harness.chat.rounds)
    assert harness.answers(leaving) == []
    hold.set()
    await harness.settle()
    assert harness.text(reply) == "第一段"
    assert harness.answer_text(staying) == default_answer("turn-2")
    assert harness.log.index(("turn/completed", "turn-1")) < harness.log.index(("turn/start", "turn-2"))
    assert len(harness.server.requests("turn/start")) == 2


async def test_an_aborted_head_blocks_the_queue_until_its_turn_ends(harness):
    hold = asyncio.Event()
    running = {"id": "thread-1", "status": {"type": "active"}, "turns": [{"id": "turn-1", "status": "inProgress"}]}
    harness.server.thread_reads["thread-1"] = running
    harness.server.handlers["thread/resume"] = lambda params: {"thread": running}
    harness.script(lambda thread, turn: [*partial(thread, turn), hold, note("error", thread, turn, error={"message": "boom"}, willRetry=False)])
    first = await harness.ask(harness.person(), "第一問", mention=True)
    await until(lambda: harness.answers(first) and harness.text(harness.answers(first)[0]) == "半截")
    queued = await harness.ask(harness.person(2, "第二位"), "我也問", reply_to=harness.answers(first)[0].id)
    await until(lambda: harness.answers(queued))
    hold.set()
    await until(lambda: first.id not in harness.chat.rounds)
    polls = len(harness.server.requests("thread/read"))
    await until(lambda: len(harness.server.requests("thread/read")) >= polls + 3)
    assert harness.text(harness.answers(queued)[0]) == WAITING_TEXT
    assert len(harness.server.requests("turn/start")) == 1
    await harness.server.send(turn_completed("thread-1", "turn-1", "interrupted"))
    await harness.settle()
    assert harness.answers(first) == []
    assert harness.answer_text(queued) == default_answer("turn-2")


async def test_shutdown_stops_running_rounds_and_removes_their_messages(harness):
    hold = asyncio.Event()
    harness.script(lambda thread, turn: [*partial(thread, turn), hold])
    trigger = await harness.ask(harness.person(), "嗨", mention=True)
    await until(lambda: harness.answers(trigger) and harness.text(harness.answers(trigger)[0]) == "半截")
    await asyncio.wait_for(harness.chat.shutdown(), 3)
    assert harness.chat.rounds == {}
    assert_cleaned_up(harness)
    assert harness.server.requests("turn/interrupt") == [{"threadId": "thread-1", "turnId": "turn-1"}]
    late = await harness.ask(harness.person(), "還在嗎", mention=True)
    assert late.id not in harness.chat.rounds
    assert len(harness.server.requests("turn/start")) == 1
