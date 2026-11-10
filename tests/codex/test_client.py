import asyncio

import pytest

from horo_dcb import codex as codex_module
from horo_dcb.codex import CodexError, ToolCall, TurnState
from tests.support.app_server import Hold, Reject
from tests.support.codex_messages import delta, recorded_answer, recorded_turn
from tests.support.waiting import events_until, until

TEXT = [{"type": "text", "text": "嗨", "text_elements": []}]


async def replay(server, messages: list[dict]) -> None:
    for message in messages:
        if "id" in message:
            await server.call(message["method"], message["params"], message["id"])
        else:
            await server.send(message)


async def active_turn(codex, **thread_options):
    thread = await codex.new_thread(**thread_options)
    turn = TurnState(thread.id)
    await codex.start_turn(thread, turn, TEXT, None, None, [], 1)
    return thread, turn


async def test_handshake_identifies_the_bot(app_server, codex):
    initialize = app_server.requests("initialize")[0]
    assert initialize["clientInfo"]["name"] == "horo_dcb"
    assert initialize["capabilities"]["experimentalApi"] is True
    await until(lambda: app_server.notifications("initialized"))


async def test_rpc_errors_carry_the_server_code(app_server, codex):
    app_server.handlers["account/read"] = lambda params: Reject(-32001)
    with pytest.raises(CodexError) as raised:
        await codex.account()
    assert (raised.value.code, raised.value.rpc_code) == ("rpc_rejected", -32001)


async def test_unanswered_requests_time_out(app_server, codex, monkeypatch):
    monkeypatch.setattr(codex_module, "REQUEST_TIMEOUT", 0.05)
    app_server.handlers["account/read"] = lambda params: Hold()
    with pytest.raises(CodexError) as raised:
        await codex.account()
    assert raised.value.code == "rpc_timeout"


async def test_models_follow_every_page(app_server, codex):
    pages = {None: {"data": [{"id": "a"}], "nextCursor": "next"}, "next": {"data": [{"id": "b"}], "nextCursor": None}}
    app_server.handlers["model/list"] = lambda params: pages[params["cursor"]]
    assert [model["id"] for model in await codex.models()] == ["a", "b"]
    assert [params["cursor"] for params in app_server.requests("model/list")] == [None, "next"]


async def test_new_threads_are_persistent_and_read_only(app_server, codex):
    tools = [{"type": "namespace", "name": "discord", "tools": []}]
    thread = await codex.new_thread("gpt-test", "喵", tools)
    await codex.new_thread()
    full, bare = app_server.requests("thread/start")
    base = {"ephemeral": False, "approvalPolicy": "never", "sandbox": "read-only"}
    assert full == base | {"dynamicTools": tools, "model": "gpt-test", "developerInstructions": "喵"}
    assert bare == base
    assert thread.id == "thread-1"


async def test_forks_start_after_the_chosen_turn(app_server, codex):
    thread = await codex.fork_thread("source", "turn-9", "gpt-test")
    assert app_server.requests("thread/fork") == [{"threadId": "source", "lastTurnId": "turn-9", "ephemeral": False, "approvalPolicy": "never", "sandbox": "read-only", "excludeTurns": True, "model": "gpt-test"}]
    await codex.load_thread(thread.id)
    assert app_server.requests("thread/resume") == []


async def test_old_threads_are_resumed_once_per_connection(app_server, codex):
    await codex.load_thread("old")
    await codex.load_thread("old")
    assert len(app_server.requests("thread/resume")) == 1
    await app_server.drop()
    await until(lambda: app_server.connections == 2 and len(app_server.notifications("initialized")) == 2)
    await codex.load_thread("old")
    assert [params["threadId"] for params in app_server.requests("thread/resume")] == ["old", "old"]


async def test_turns_carry_inputs_context_model_and_trigger(app_server, codex):
    thread = await codex.new_thread()
    turn = TurnState(thread.id)
    await codex.start_turn(thread, turn, TEXT, "gpt-test", "high", ["第一則", "第二則"], 555)
    assert app_server.requests("turn/start") == [{
        "threadId": thread.id,
        "input": TEXT,
        "summary": "auto",
        "clientUserMessageId": "555",
        "model": "gpt-test",
        "effort": "high",
        "additionalContext": {
            "discord_00000": {"kind": "untrusted", "name": "discord_channel", "value": "第一則"},
            "discord_00001": {"kind": "untrusted", "name": "discord_channel", "value": "第二則"},
        },
    }]
    assert turn.id == "turn-1"


async def test_a_thread_runs_one_turn_at_a_time(app_server, codex):
    thread, _ = await active_turn(codex)
    with pytest.raises(CodexError) as raised:
        await codex.start_turn(thread, TurnState(thread.id), TEXT, None, None, [], 2)
    assert raised.value.code == "thread_busy"
    assert len(app_server.requests("turn/start")) == 1


async def test_a_rejected_turn_is_already_over(app_server, codex):
    app_server.handlers["turn/start"] = lambda params: Reject()
    thread = await codex.new_thread()
    turn = TurnState(thread.id)
    with pytest.raises(CodexError):
        await codex.start_turn(thread, turn, TEXT, None, None, [], 1)
    assert turn.terminal.is_set()


async def test_a_recorded_answer_reaches_the_turn(app_server, codex):
    messages = []

    async def play(params: dict, result: dict) -> None:
        messages.extend(recorded_turn("answer", params["threadId"], result["turn"]["id"]))
        await replay(app_server, messages)

    app_server.after["turn/start"] = play
    _, turn = await active_turn(codex)
    events = await events_until(turn.events, "turn/completed")
    assert "".join(params["delta"] for method, params in events if method == "item/agentMessage/delta") == recorded_answer("answer")
    assert turn.terminal.is_set()
    total = next(message["params"]["tokenUsage"]["total"] for message in messages if message["method"] == "thread/tokenUsage/updated")
    assert turn.usage_after == total
    assert turn.usage_before == {"inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0, "reasoningOutputTokens": 0, "totalTokens": 0}


async def test_events_of_other_turns_are_not_delivered(app_server, codex):
    thread, turn = await active_turn(codex)
    await app_server.send(delta(thread.id, "turn-other", "x", "別人的"))
    await app_server.send(delta("thread-other", turn.id, "x", "別的對話"))
    await app_server.send(delta(thread.id, turn.id, "x", "自己的"))
    method, params = await asyncio.wait_for(turn.events.get(), 3)
    assert (method, params["delta"]) == ("item/agentMessage/delta", "自己的")


async def test_a_recorded_tool_call_is_handed_to_the_turn(app_server, codex):
    async def play(params: dict, result: dict) -> None:
        await replay(app_server, recorded_turn("tool", params["threadId"], result["turn"]["id"]))

    app_server.after["turn/start"] = play
    _, turn = await active_turn(codex, tools=[{"type": "namespace", "name": "discord", "tools": []}])
    events = await events_until(turn.events, "client/toolCall")
    call = events[-1][1]
    assert isinstance(call, ToolCall)
    assert (call.tool, call.arguments, call.turn_id) == ("read_messages", {}, turn.id)
    assert await codex.respond(call, True, "讀到 0 則")
    assert not await codex.respond(call, True, "重複")
    await until(lambda: app_server.responses())
    assert app_server.responses() == [{"id": 0, "result": {"contentItems": [{"type": "inputText", "text": "讀到 0 則"}], "success": True}}]


async def test_tool_calls_for_unknown_turns_are_refused(app_server, codex):
    reply = await app_server.call("item/tool/call", {"threadId": "nobody", "turnId": "x", "callId": "c", "tool": "read_messages", "arguments": {}})
    response = await asyncio.wait_for(reply, 3)
    assert response["result"]["success"] is False


async def test_tool_calls_for_an_older_turn_of_the_thread_are_refused(app_server, codex):
    thread, turn = await active_turn(codex)
    reply = await app_server.call("item/tool/call", {"threadId": thread.id, "turnId": "turn-old", "callId": "c", "tool": "read_messages", "arguments": {}})
    response = await asyncio.wait_for(reply, 3)
    assert response["result"]["success"] is False
    assert turn.events.empty()


async def test_tool_calls_after_the_answer_stopped_are_refused(app_server, codex):
    thread, turn = await active_turn(codex)
    turn.accepts_tools = False
    reply = await app_server.call("item/tool/call", {"threadId": thread.id, "turnId": turn.id, "callId": "c", "tool": "read_messages", "arguments": {}})
    response = await asyncio.wait_for(reply, 3)
    assert response["result"]["success"] is False
    assert turn.events.empty()


async def test_interrupt_refuses_waiting_tool_calls_first(app_server, codex):
    thread, turn = await active_turn(codex)
    reply = await app_server.call("item/tool/call", {"threadId": thread.id, "turnId": turn.id, "callId": "c", "tool": "read_messages", "arguments": {}})
    await events_until(turn.events, "client/toolCall")
    await codex.interrupt(turn)
    assert (await asyncio.wait_for(reply, 3))["result"]["success"] is False
    assert app_server.requests("turn/interrupt") == [{"threadId": thread.id, "turnId": turn.id}]
    refusal = next(index for index, message in enumerate(app_server.received) if "method" not in message)
    interrupt = next(index for index, message in enumerate(app_server.received) if message.get("method") == "turn/interrupt")
    assert refusal < interrupt


async def test_unsupported_server_requests_are_declined(app_server, codex):
    thread, turn = await active_turn(codex)
    reply = await app_server.call("item/commandExecution/requestApproval", {"threadId": thread.id, "turnId": turn.id})
    assert (await asyncio.wait_for(reply, 3))["error"]["code"] == -32601
    events = await events_until(turn.events, "client/unsupportedRequest")
    assert events[-1][0] == "client/unsupportedRequest"


async def test_disconnect_fails_waiting_requests_and_tells_the_turn(app_server, codex):
    _, turn = await active_turn(codex)
    app_server.handlers["account/read"] = lambda params: Hold()
    pending = asyncio.create_task(codex.account())
    await until(lambda: app_server.requests("account/read"))
    await app_server.drop()
    with pytest.raises(CodexError) as raised:
        await pending
    assert raised.value.code == "connection_lost"
    await events_until(turn.events, "client/disconnected")


async def test_reconnect_recovers_a_turn_that_finished_offline(app_server, codex):
    thread, turn = await active_turn(codex)
    app_server.thread_reads[thread.id] = {"id": thread.id, "status": {"type": "idle"}, "turns": [{"id": turn.id, "status": "completed"}]}
    await app_server.drop()
    events = await events_until(turn.events, "client/recovered")
    assert ("client/disconnected", {}) in events
    assert turn.terminal.is_set()
    assert len(app_server.requests("initialize")) == 2


async def test_reconnect_trusts_the_turn_list_over_the_thread_state(app_server, codex):
    thread, turn = await active_turn(codex)
    app_server.thread_reads[thread.id] = {"id": thread.id, "status": {"type": "systemError"}, "turns": [{"id": turn.id, "status": "failed"}]}
    await app_server.drop()
    await events_until(turn.events, "client/recovered")
    assert turn.terminal.is_set()


async def test_reconnect_interrupts_a_turn_still_running(app_server, codex):
    thread, turn = await active_turn(codex)
    running = {"id": thread.id, "status": {"type": "active"}, "turns": [{"id": turn.id, "status": "inProgress"}]}
    app_server.thread_reads[thread.id] = running
    app_server.handlers["thread/resume"] = lambda params: {"thread": running}
    await app_server.drop()
    await until(lambda: app_server.requests("turn/interrupt"))
    assert app_server.requests("turn/interrupt") == [{"threadId": thread.id, "turnId": turn.id}]
    assert not turn.terminal.is_set()


async def test_waiting_for_the_end_polls_the_thread(app_server, codex):
    thread, turn = await active_turn(codex)
    app_server.thread_reads[thread.id] = {"id": thread.id, "status": {"type": "idle"}, "turns": [{"id": turn.id, "status": "interrupted"}]}
    await asyncio.wait_for(codex.wait_terminal(turn), 3)
    assert turn.terminal.is_set()
    assert app_server.requests("thread/read")


async def test_steer_targets_the_running_turn(app_server, codex):
    thread, turn = await active_turn(codex)
    assert await codex.steer(turn, TEXT, 77)
    assert app_server.requests("turn/steer") == [{"threadId": thread.id, "expectedTurnId": turn.id, "input": TEXT, "clientUserMessageId": "77"}]


async def test_a_rejected_steer_reports_false(app_server, codex):
    _, turn = await active_turn(codex)
    app_server.handlers["turn/steer"] = lambda params: Reject()
    assert not await codex.steer(turn, TEXT, 77)


async def test_a_steer_that_times_out_raises(app_server, codex, monkeypatch):
    _, turn = await active_turn(codex)
    monkeypatch.setattr(codex_module, "REQUEST_TIMEOUT", 0.05)
    app_server.handlers["turn/steer"] = lambda params: Hold()
    with pytest.raises(CodexError):
        await codex.steer(turn, TEXT, 77)
