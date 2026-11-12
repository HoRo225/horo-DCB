import asyncio

from horo_dcb import tools
from horo_dcb.chat import LINK_HINT, STEER_LEAD
from horo_dcb.store import Entry
from tests.support import chat_harness
from tests.support.chat_harness import context_text, data_url, default_answer, input_text, picture
from tests.support.codex_messages import agent, completed, delta, recorded_answer, recorded_turn, started, turn_completed
from tests.support.discord_fakes import attachment, card_of
from tests.support.waiting import until

harness = chat_harness.harness
WAITING_CARD = (0x80848E, ["⏳ - **等待中**"])


def streaming(hold: asyncio.Event, first: str, rest: str):
    return lambda thread, turn: [
        started(thread, turn, agent("a")),
        delta(thread, turn, "a", first),
        hold,
        delta(thread, turn, "a", rest),
        completed(thread, turn, agent("a", first + rest)),
        turn_completed(thread, turn, items=[agent("a", first + rest)]),
    ]


async def first_answer(harness, asker):
    trigger = await harness.ask(asker, "第一問", mention=True)
    await harness.settle()
    return trigger, harness.answers(trigger)[0]


async def test_a_mention_starts_a_thread_and_records_the_answer(harness):
    harness.script(lambda thread, turn: recorded_turn("answer", thread, turn))
    trigger = await harness.ask(harness.person(), "一週有幾天？", mention=True)
    await harness.settle()
    start = harness.server.requests("thread/start")
    assert len(start) == 1
    assert start[0]["dynamicTools"] == tools.SPEC
    assert start[0]["developerInstructions"] == chat_harness.INSTRUCTIONS
    assert harness.server.requests("turn/start")[0]["threadId"] == "thread-1"
    [reply] = harness.answers(trigger)
    assert harness.text(reply) == recorded_answer("answer")
    assert harness.store.entry_for(reply.id) == Entry("thread-1", "turn-1")
    assert harness.store.latest_turn("thread-1") == "turn-1"


async def test_replying_to_the_latest_answer_continues_with_newer_messages_only(harness):
    asker, other = harness.person(), harness.person(2, "路人")
    harness.post(other, "很久以前的話")
    _, reply = await first_answer(harness, asker)
    harness.post(other, "中間插進來的話")
    trigger = await harness.ask(asker, "第二問", reply_to=reply.id)
    await harness.settle()
    assert len(harness.server.requests("thread/start")) == 1
    assert harness.server.requests("thread/fork") == []
    first, second = harness.server.requests("turn/start")
    assert second["threadId"] == first["threadId"]
    assert "第二問" in input_text(second)
    context = context_text(second)
    assert "中間插進來的話" in context
    assert "很久以前的話" not in context
    assert "第一問" not in context
    assert harness.answer_text(trigger) == default_answer("turn-2")
    assert harness.store.latest_turn("thread-1") == "turn-2"


async def test_replying_to_an_older_answer_forks_without_the_source_thread(harness):
    asker, other = harness.person(), harness.person(2, "路人")
    _, old = await first_answer(harness, asker)
    second = await harness.ask(asker, "第二問", reply_to=old.id)
    await harness.settle()
    harness.post(other, "路人的話")
    trigger = await harness.ask(asker, "第三問", reply_to=old.id)
    await harness.settle()
    [fork] = harness.server.requests("thread/fork")
    assert (fork["threadId"], fork["lastTurnId"]) == ("thread-1", "turn-1")
    params = harness.server.requests("turn/start")[-1]
    assert params["threadId"] == "thread-2"
    context = context_text(params)
    assert "路人的話" in context
    assert "第二問" not in context
    assert default_answer("turn-2") not in context
    assert harness.store.entry_for(harness.answers(trigger)[0].id) == Entry("thread-2", "turn-3")
    assert harness.answer_text(second) == default_answer("turn-2")


async def test_the_asker_steers_a_streaming_answer(harness):
    hold = asyncio.Event()
    asker = harness.person()
    harness.script(streaming(hold, "夜市有很多", "三句話版本"))
    trigger = await harness.ask(asker, "介紹夜市", mention=True)
    await until(lambda: harness.answers(trigger) and "夜市有很多" in harness.text(harness.answers(trigger)[0]))
    reply = harness.answers(trigger)[0]
    steer = await harness.ask(asker, "改成只用三句話", reply_to=reply.id)
    await until(lambda: steer.id not in harness.chat.rounds)
    hold.set()
    await harness.settle()
    [params] = harness.server.requests("turn/steer")
    assert (params["threadId"], params["expectedTurnId"]) == ("thread-1", "turn-1")
    assert input_text(params).startswith(STEER_LEAD)
    assert "改成只用三句話" in input_text(params)
    steer.add_reaction.assert_awaited_once_with("✅")
    assert harness.answers(steer) == []
    assert harness.channel.sent == [reply]
    assert len(harness.server.requests("turn/start")) == 1


async def test_another_user_waits_for_the_streaming_answer(harness):
    hold = asyncio.Event()
    harness.script(streaming(hold, "第一段", "第二段"))
    first = await harness.ask(harness.person(), "介紹夜市", mention=True)
    await until(lambda: harness.answers(first) and "第一段" in harness.text(harness.answers(first)[0]))
    reply = harness.answers(first)[0]
    queued = await harness.ask(harness.person(2, "第二位"), "我也想問", reply_to=reply.id)
    await until(lambda: harness.answers(queued))
    waiting = harness.answers(queued)[0]
    assert card_of(waiting.views[-1]) == WAITING_CARD
    assert len(harness.server.requests("turn/start")) == 1
    hold.set()
    await harness.settle()
    assert harness.answers(queued) == [waiting]
    assert card_of(waiting.views[0]) == WAITING_CARD
    assert harness.text(waiting) == default_answer("turn-2")
    assert harness.text(reply) == "第一段第二段"
    assert harness.log.index(("turn/completed", "turn-1")) < harness.log.index(("turn/start", "turn-2"))
    assert [params["threadId"] for params in harness.server.requests("turn/start")] == ["thread-1", "thread-1"]


async def test_replying_to_the_latest_answer_while_its_thread_is_busy_forks(harness):
    hold = asyncio.Event()
    _, latest = await first_answer(harness, harness.person())
    harness.script(streaming(hold, "第一段", "第二段"))
    busy = await harness.ask(harness.person(), "第二問", reply_to=latest.id)
    await until(lambda: ("turn/start", "turn-2") in harness.log)
    forked = await harness.ask(harness.person(2, "第二位"), "第三問", reply_to=latest.id)
    await until(lambda: forked.id not in harness.chat.rounds)
    [fork] = harness.server.requests("thread/fork")
    assert (fork["threadId"], fork["lastTurnId"]) == ("thread-1", "turn-1")
    assert harness.answer_text(forked) == default_answer("turn-3")
    hold.set()
    await harness.settle()
    assert harness.answer_text(busy) == "第一段第二段"


async def test_images_are_labelled_and_follow_their_labels(harness):
    asker = harness.person()
    red, green, blue = b"red", b"green", b"blue"
    harness.post(asker, "先看這張", attachments=(picture(red, 1),))
    await harness.ask(asker, "這兩張呢", mention=True, attachments=(picture(green, 2), picture(blue, 3)))
    await harness.settle()
    [params] = harness.server.requests("turn/start")
    assert "〔圖片 1〕" in context_text(params)
    assert "〔圖片 2〕〔圖片 3〕" in input_text(params)
    assert params["input"][1:] == [
        {"type": "text", "text": "〔圖片 1〕", "text_elements": []},
        {"type": "image", "url": data_url(red)},
        {"type": "text", "text": "〔圖片 2〕", "text_elements": []},
        {"type": "image", "url": data_url(green)},
        {"type": "text", "text": "〔圖片 3〕", "text_elements": []},
        {"type": "image", "url": data_url(blue)},
    ]


async def test_an_image_with_too_many_pixels_is_left_out(harness):
    asker = harness.person()
    huge = attachment(b"huge", "huge.png", attachment_id=1, width=12000, height=12000)
    await harness.ask(asker, "看這兩張", mention=True, attachments=(huge, picture(b"small", 2)))
    await harness.settle()
    [params] = harness.server.requests("turn/start")
    huge.read.assert_not_awaited()
    assert "〔圖片 2〕" not in input_text(params)
    assert params["input"][1:] == [
        {"type": "text", "text": "〔圖片 1〕", "text_elements": []},
        {"type": "image", "url": data_url(b"small")},
    ]


async def test_a_reply_quotes_the_answer_when_messages_sit_between(harness):
    asker = harness.person()
    _, reply = await first_answer(harness, asker)
    harness.post(harness.person(2, "路人"), "插話")
    await harness.ask(asker, "第二問", reply_to=reply.id)
    await harness.settle()
    text = input_text(harness.server.requests("turn/start")[-1])
    assert "（回覆 樂奈" in text
    assert default_answer("turn-1") in text


async def test_a_gapless_continue_has_no_quote(harness):
    asker = harness.person()
    _, reply = await first_answer(harness, asker)
    await harness.ask(asker, "第二問", reply_to=reply.id)
    await harness.settle()
    text = input_text(harness.server.requests("turn/start")[-1])
    assert "第二問" in text
    assert "（回覆" not in text
    assert default_answer("turn-1") not in text


async def test_a_discord_message_link_adds_the_link_hint(harness):
    await harness.ask(harness.person(), "這則在說什麼 https://discord.com/channels/100/200/5", mention=True)
    await harness.settle()
    await harness.ask(harness.person(2, "第二位"), "沒有連結", mention=True)
    await harness.settle()
    linked, plain = harness.server.requests("turn/start")
    assert LINK_HINT in input_text(linked)
    assert LINK_HINT not in input_text(plain)
