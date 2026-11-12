import asyncio
import json
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from horo_dcb import tools
from tests.support.discord_fakes import CHANNEL_ID, GUILD_ID, Channel, attachment, http_error, member

ALICE = member(1, "Alice")
MiB = 1024 * 1024


def link(message_id: int, guild_id: int = GUILD_ID, channel_id: int = CHANNEL_ID, host: str = "discord.com") -> str:
    return f"https://{host}/channels/{guild_id}/{channel_id}/{message_id}"


def setup(count: int = 70, anchor_id: int = 60, http=None) -> tuple[Channel, tools.ToolContext]:
    channel = Channel()
    for message_id in range(1, count + 1):
        channel.post(message_id, ALICE, f"第 {message_id} 則")
    anchor = next(value for value in channel.messages if value.id == anchor_id)
    return channel, tools.ToolContext(channel.mock, anchor, http or Mock())


async def call(name: str, arguments, context: tools.ToolContext) -> tuple[tools.ToolResult, dict]:
    result = await tools.run(name, arguments, context)
    try:
        body = json.loads(result.text)
    except ValueError:
        body = {}
    return result, body


def ids(body: dict) -> list[int]:
    return [int(entry["id"]) for entry in body["messages"]]


@pytest.mark.parametrize("host", ["discord.com", "ptb.discord.com", "canary.discord.com", "discordapp.com"])
def test_message_links_resolve_to_the_message_id(host):
    _, context = setup(count=1, anchor_id=1)
    assert tools.target_id(f"看這則 {link(555, host=host)}", context) == 555


def test_plain_ids_are_accepted():
    _, context = setup(count=1, anchor_id=1)
    assert tools.target_id(" 123 ", context) == 123


@pytest.mark.parametrize("value", [link(5, channel_id=999), link(5, guild_id=999), "不是連結", None])
def test_other_channels_and_garbage_are_refused(value):
    _, context = setup(count=1, anchor_id=1)
    with pytest.raises(tools.ToolError):
        tools.target_id(value, context)


async def test_reading_without_arguments_starts_before_the_trigger():
    _, context = setup()
    result, body = await call("read_messages", {}, context)
    assert result.success
    assert ids(body) == list(range(40, 60))
    assert body["next_before"] == "40"
    assert result.note == "20 則較早的訊息"


async def test_reading_before_a_link_pages_backwards():
    _, context = setup()
    _, body = await call("read_messages", {"before": link(30), "limit": 5}, context)
    assert ids(body) == [25, 26, 27, 28, 29]
    assert body["next_before"] == "25"


async def test_reading_past_the_first_message_has_no_cursor():
    _, context = setup()
    _, body = await call("read_messages", {"before": "3"}, context)
    assert ids(body) == [1, 2]
    assert body["next_before"] is None


async def test_message_entries_carry_author_time_replies_and_image_markers():
    channel = Channel()
    channel.post(1, ALICE, "原文")
    channel.post(2, member(2, "Bob"), "x" * 1500, reply_to=1, attachments=(attachment(b"png", attachment_id=7), attachment(b"gif", "clip.GIF", None, attachment_id=8), attachment(b"", "notes.txt", "text/plain", attachment_id=9)))
    trigger = channel.post(3, ALICE, "問題")
    context = tools.ToolContext(channel.mock, trigger, Mock())
    _, body = await call("read_messages", {}, context)
    assert body["messages"][1] == {"id": "2", "author": "Bob", "time": "2026-11-10 12:02", "text": "x" * 1000 + "…（截斷）\n〔圖片〕〔圖片〕", "reply_to": "1"}


async def test_reading_stops_before_the_byte_budget():
    channel = Channel()
    for message_id in range(1, 21):
        channel.post(message_id, ALICE, "長" * 1000)
    trigger = channel.post(21, ALICE, "問題")
    context = tools.ToolContext(channel.mock, trigger, Mock())
    result, body = await call("read_messages", {}, context)
    assert len(result.text.encode()) <= tools.READ_BYTES
    assert ids(body) == list(range(21 - len(body["messages"]), 21))
    assert 0 < len(body["messages"]) < 20
    assert body["next_before"] == str(ids(body)[0])


async def test_reading_around_a_message_returns_both_sides():
    _, context = setup()
    result, body = await call("read_messages", {"around": link(30), "limit": 5}, context)
    assert ids(body) == [28, 29, 30, 31, 32]
    assert (body["next_before"], body["next_after"]) == ("28", "32")
    assert result.note == "指定訊息前後共 5 則"


async def test_reading_around_the_newest_message_has_no_newer_cursor():
    _, context = setup()
    _, body = await call("read_messages", {"around": "70", "limit": 5}, context)
    assert ids(body) == [68, 69, 70]
    assert (body["next_before"], body["next_after"]) == ("68", None)


async def test_reading_around_a_missing_message_fails():
    _, context = setup()
    result, _ = await call("read_messages", {"around": "999"}, context)
    assert not result.success
    assert result.note == "失敗，找不到訊息"


def search_hit(message_id: int, content: str, attachments: tuple[dict, ...] = ({"content_type": "image/png"},)) -> list[dict]:
    raw = {"id": str(message_id), "content": content, "timestamp": "2026-11-10T04:00:00+00:00", "author": {"global_name": "Alice", "username": "alice"}, "attachments": list(attachments), "message_reference": {"message_id": "1"}}
    return [{**raw, "id": "0", "content": "前文"}, {**raw, "hit": True}]


async def test_search_sends_the_channel_query_and_pages():
    http = Mock()
    http.request = AsyncMock(return_value={"total_results": 30, "messages": [search_hit(9, "紫色長頸鹿")]})
    _, context = setup(http=http)
    result, body = await call("search_messages", {"content": " 紫色長頸鹿 "}, context)
    route = http.request.await_args.args[0]
    assert (route.method, route.url.endswith(f"/guilds/{GUILD_ID}/messages/search")) == ("GET", True)
    assert http.request.await_args.kwargs["params"] == {"channel_id": str(CHANNEL_ID), "content": "紫色長頸鹿", "offset": 0, "limit": 25}
    assert body == {"total": 30, "messages": [{"id": "9", "author": "Alice", "time": "2026-11-10 12:00", "text": "紫色長頸鹿\n〔圖片〕", "reply_to": "1"}], "next_offset": 25}
    assert result.note == "紫色長頸鹿，找到 30 則"


async def test_search_marks_images_by_content_type_or_suffix():
    attachments = (
        {"filename": "upload", "content_type": "image/png; charset=binary"},
        {"filename": "clip.gif"},
        {"filename": "scan.JPG", "content_type": "application/octet-stream"},
        {"filename": "notes.txt", "content_type": "text/plain"},
    )
    http = Mock()
    http.request = AsyncMock(return_value={"total_results": 1, "messages": [search_hit(9, "紫色長頸鹿", attachments)]})
    _, context = setup(http=http)
    _, body = await call("search_messages", {"content": "紫色長頸鹿"}, context)
    assert [entry["text"] for entry in body["messages"]] == ["紫色長頸鹿\n〔圖片〕〔圖片〕〔圖片〕"]


async def test_search_has_no_next_page_on_the_last_page():
    http = Mock()
    http.request = AsyncMock(return_value={"total_results": 30, "messages": []})
    _, context = setup(http=http)
    _, body = await call("search_messages", {"content": "x", "offset": 25}, context)
    assert body["next_offset"] is None


async def test_search_has_no_next_page_at_the_offset_cap():
    http = Mock()
    http.request = AsyncMock(return_value={"total_results": 20000, "messages": []})
    _, context = setup(http=http)
    _, body = await call("search_messages", {"content": "x", "offset": 9975}, context)
    assert body["next_offset"] is None


async def test_search_retries_while_the_index_is_warming(monkeypatch):
    monkeypatch.setattr(tools, "SEARCH_RETRY_MAX", 0)
    http = Mock()
    http.request = AsyncMock(side_effect=[{"code": 110000, "retry_after": 3}, {"total_results": 0, "messages": []}])
    _, context = setup(http=http)
    result, body = await call("search_messages", {"content": "x"}, context)
    assert result.success
    assert body["total"] == 0
    assert http.request.await_count == 2


async def test_search_gives_up_after_three_index_errors(monkeypatch):
    monkeypatch.setattr(tools, "SEARCH_RETRY_MAX", 0)
    http = Mock()
    http.request = AsyncMock(return_value={"code": 110000})
    _, context = setup(http=http)
    result, _ = await call("search_messages", {"content": "x"}, context)
    assert (result.success, result.note) == (False, "失敗，搜尋索引未就緒")
    assert http.request.await_count == 3


async def test_search_needs_a_keyword():
    http = Mock()
    http.request = AsyncMock()
    _, context = setup(http=http)
    result, _ = await call("search_messages", {"content": "  "}, context)
    assert (result.success, result.note) == (False, "失敗，沒有關鍵字")
    http.request.assert_not_awaited()


def with_attachments(*files: Mock) -> tools.ToolContext:
    channel = Channel()
    channel.post(5, member(2, "Bob"), "圖", attachments=files)
    trigger = channel.post(6, ALICE, "問題")
    return tools.ToolContext(channel.mock, trigger, Mock())


async def test_viewing_images_returns_at_most_four():
    context = with_attachments(*(attachment(b"png", f"{index}.png", attachment_id=index) for index in range(1, 6)))
    result, body = await call("view_message_images", {"message": link(5)}, context)
    assert result.success
    assert result.images == ({"type": "image", "url": "data:image/png;base64,cG5n"},) * 4
    assert body["attached"] == ["1.png", "2.png", "3.png", "4.png"]
    assert body["skipped"] == [{"id": "5", "filename": "5.png", "reason": "超過一次 4 張的上限"}]
    assert result.note == "4 張圖片"


async def test_viewing_images_sends_each_file_as_is_under_its_own_type():
    context = with_attachments(
        attachment(b"hello", "clip.gif", None, attachment_id=1),
        attachment(b"hello", "upload", "image/webp; charset=binary", attachment_id=2),
        attachment(b"hello", "notes.txt", "text/plain", attachment_id=3),
    )
    result, body = await call("view_message_images", {"message": "5"}, context)
    assert result.images == (
        {"type": "image", "url": "data:image/gif;base64,aGVsbG8="},
        {"type": "image", "url": "data:image/webp;base64,aGVsbG8="},
    )
    assert body["attached"] == ["clip.gif", "upload"]
    assert body["skipped"] == []


async def test_viewing_images_skips_oversized_and_over_budget_files():
    declared_big = attachment(b"png", "declared-big.png", size=9 * MiB, attachment_id=1)
    context = with_attachments(
        declared_big,
        attachment(b"png", "ok.png", attachment_id=2),
        attachment(b"\0" * (9 * MiB), "really-big.png", size=1, attachment_id=3),
        attachment(b"\0" * (6 * MiB), "six.png", attachment_id=4),
        attachment(b"\0" * (2 * MiB), "over-total.png", size=1, attachment_id=5),
        attachment(b"png", "late.png", attachment_id=6),
    )
    result, body = await call("view_message_images", {"message": "5"}, context)
    declared_big.read.assert_not_awaited()
    assert body["attached"] == ["ok.png", "six.png"]
    assert len(result.images) == 2
    assert [(entry["filename"], entry["reason"]) for entry in body["skipped"]] == [
        ("declared-big.png", "單張超過 8 MiB"),
        ("really-big.png", "實際大小超過上限"),
        ("over-total.png", "實際大小超過上限"),
        ("late.png", "超過這次 16 MiB 的總量"),
    ]


async def test_viewing_images_skips_files_that_are_too_large_to_decode_without_downloading_them():
    huge = attachment(b"png", "huge.png", attachment_id=1, width=12000, height=12000)
    unmeasured = attachment(b"png", "unmeasured.png", attachment_id=2, width=None, height=None)
    context = with_attachments(huge, unmeasured, attachment(b"png", "ok.png", attachment_id=3, width=4000, height=3000))
    result, body = await call("view_message_images", {"message": "5"}, context)
    huge.read.assert_not_awaited()
    unmeasured.read.assert_not_awaited()
    assert body["attached"] == ["ok.png"]
    assert body["skipped"] == [
        {"id": "1", "filename": "huge.png", "reason": "尺寸超過 5000 萬像素或無法判定"},
        {"id": "2", "filename": "unmeasured.png", "reason": "尺寸超過 5000 萬像素或無法判定"},
    ]


async def test_message_without_images_is_a_failure():
    context = with_attachments(attachment(b"text", "notes.txt", "text/plain"))
    result, body = await call("view_message_images", {"message": "5"}, context)
    assert (result.success, result.note) == (False, "失敗，沒有圖片")
    assert result.images == ()
    assert body["error"] == "這則訊息沒有可查看的圖片"


async def test_viewing_a_missing_message_reports_not_found():
    context = with_attachments()
    result, _ = await call("view_message_images", {"message": "404"}, context)
    assert (result.success, result.note) == (False, "失敗，找不到訊息")


@pytest.mark.parametrize(("value", "note"), [("不是連結", "失敗，看不懂連結"), (link(5, channel_id=999), "失敗，連結在其他頻道")])
async def test_bad_links_are_reported_with_a_short_reason(value, note):
    context = with_attachments()
    result, _ = await call("view_message_images", {"message": value}, context)
    assert (result.success, result.note) == (False, note)


async def test_unknown_tool_is_refused():
    _, context = setup()
    result, _ = await call("delete_messages", {}, context)
    assert (result.success, result.note) == (False, "失敗，沒有這個工具")


async def test_slow_tool_times_out(monkeypatch):
    monkeypatch.setattr(tools, "TIMEOUT", 0.01)

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)

    http = Mock()
    http.request = AsyncMock(side_effect=slow)
    _, context = setup(http=http)
    result, _ = await call("search_messages", {"content": "x"}, context)
    assert (result.success, result.note) == (False, "失敗，逾時")


@pytest.mark.parametrize(
    ("error", "note"),
    [(http_error(discord.Forbidden, 403), "失敗，沒有權限"), (http_error(discord.HTTPException, 500), "失敗，Discord 錯誤")],
)
async def test_discord_errors_are_reported(error, note):
    http = Mock()
    http.request = AsyncMock(side_effect=error)
    _, context = setup(http=http)
    result, _ = await call("search_messages", {"content": "x"}, context)
    assert (result.success, result.note) == (False, note)


async def test_bad_arguments_are_reported():
    _, context = setup()
    result, _ = await call("read_messages", {"limit": "很多"}, context)
    assert (result.success, result.note) == (False, "失敗，參數錯誤")


async def test_non_object_arguments_fall_back_to_defaults():
    _, context = setup()
    result, body = await call("read_messages", ["unexpected"], context)
    assert result.success
    assert ids(body) == list(range(40, 60))
