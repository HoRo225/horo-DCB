import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call, patch

import discord

from src.agent_tools import AgentTools, ResearchContext, ToolContext
from src.ai_client import AIResponse, AIToolCall
from src.calendar_events import (
    BOARD_BROWSE_CUSTOM_ID,
    BOARD_CREATE_CUSTOM_ID,
    BOARD_EDIT_CUSTOM_ID,
    BOARD_REFRESH_CUSTOM_ID,
    CALENDAR_TZ,
    CalendarAdminView,
    CalendarBinding,
    CalendarDraft,
    CalendarDraftCreateModal,
    CalendarEditPickerView,
    CalendarManager,
    CalendarScope,
    CalendarUserError,
    build_calendar_event_input,
    parse_calendar_datetime,
)
from src.bot import HoroBot
from src.chat import ChatManager, ChatReply


class FakeMessage:
    def __init__(self, message_id=100):
        self.id = message_id
        self.edits = []
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    async def delete(self):
        self.deleted = True


class FakePartialMessage:
    def __init__(self, channel, message_id):
        self.channel = channel
        self.id = message_id

    def _message(self):
        message = self.channel.messages.get(self.id)
        if message is None:
            raise discord.NotFound(
                SimpleNamespace(status=404, reason="Not Found"),
                "missing",
            )
        return message

    async def edit(self, **kwargs):
        await self._message().edit(**kwargs)

    async def delete(self):
        await self._message().delete()


class FakeTextChannel:
    type = discord.ChannelType.text

    def __init__(self, channel_id=10):
        self.id = channel_id
        self.guild = None
        self.sent = []
        self.messages = {}
        self.permissions = SimpleNamespace(
            view_channel=True,
            read_message_history=True,
            send_messages=True,
        )
        self.next_message_id = 100
        self.fetch_message_calls = 0

    def permissions_for(self, _member):
        return self.permissions

    async def send(self, **kwargs):
        message = FakeMessage(self.next_message_id)
        self.next_message_id += 1
        self.sent.append(kwargs)
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id):
        return FakePartialMessage(self, message_id)

    async def fetch_message(self, message_id):
        self.fetch_message_calls += 1
        raise AssertionError("Calendar cache-first flow must not fetch board messages before edit/delete")


class FakeGuild:
    def __init__(self, guild_id=1, name="Test Guild"):
        self.id = guild_id
        self.name = name
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(
                manage_events=True,
                create_events=True,
            )
        )
        self.channels = {}
        self.events = []
        self.created = []
        self.event_by_id = {}
        self.fetch_events_calls = 0
        self.fetch_event_calls = 0

    @property
    def scheduled_events(self):
        return tuple(self.events)

    def add_channel(self, channel):
        channel.guild = self
        self.channels[channel.id] = channel

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_scheduled_event(self, event_id):
        return self.event_by_id.get(event_id) or next(
            (event for event in self.events if event.id == event_id),
            None,
        )

    async def fetch_scheduled_events(self):
        self.fetch_events_calls += 1
        raise AssertionError("Calendar cache-first flow must not fetch scheduled event lists")

    async def fetch_scheduled_event(self, event_id):
        self.fetch_event_calls += 1
        raise AssertionError("Calendar cache-first flow must not fetch individual scheduled events")

    async def create_scheduled_event(self, **kwargs):
        self.created.append(kwargs)
        event = make_event(
            event_id=900,
            name=kwargs["name"],
            start=kwargs["start_time"],
            end=kwargs["end_time"],
            location=kwargs["location"],
            description=kwargs.get("description"),
        )
        event.url = "https://discord.com/events/1/900"
        return event


def make_event(
    *,
    event_id=1,
    name="團練",
    start=None,
    end=None,
    location="Discord",
    description=None,
    entity_type=discord.EntityType.external,
    status=discord.EventStatus.scheduled,
):
    start = start or datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
    end = end or datetime(2026, 8, 25, 14, 0, tzinfo=timezone.utc)
    return SimpleNamespace(
        id=event_id,
        name=name,
        start_time=start,
        end_time=end,
        location=location,
        description=description,
        entity_type=entity_type,
        status=status,
        channel=None,
        url=f"https://discord.com/events/1/{event_id}",
    )


class CalendarInputTest(unittest.TestCase):
    def test_parse_calendar_datetime_is_utc_plus_8(self):
        value = parse_calendar_datetime("2026-09-05 20:30")
        self.assertEqual(value.utcoffset().total_seconds(), 8 * 3600)
        self.assertEqual(value.strftime("%Y-%m-%d %H:%M"), "2026-09-05 20:30")

    def test_invalid_calendar_datetime_is_rejected(self):
        for value in (
            "2026-02-30 20:00",
            "09/05/2026 20:00",
            "2026-09-05 25:00",
            "2026-9-5 8:00",
        ):
            with self.subTest(value=value), self.assertRaises(CalendarUserError):
                parse_calendar_datetime(value)

    def test_event_input_rejects_past_and_invalid_duration(self):
        now = datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ)
        with self.assertRaises(CalendarUserError):
            build_calendar_event_input(
                name="團練",
                start="2026-08-24 17:00",
                duration_minutes=60,
                location="Discord",
                now=now,
            )
        with self.assertRaises(CalendarUserError):
            build_calendar_event_input(
                name="團練",
                start="2026-08-25 20:00",
                duration_minutes=True,
                location="Discord",
                now=now,
            )

    def test_event_input_applies_duration_and_bounds(self):
        now = datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ)
        result = build_calendar_event_input(
            name=" 團練 ",
            start="2026-08-25 20:00",
            duration_minutes=120,
            location=" Discord ",
            description=" 測試 ",
            now=now,
        )
        self.assertEqual(result.name, "團練")
        self.assertEqual(result.location, "Discord")
        self.assertEqual(result.description, "測試")
        self.assertEqual(result.duration_minutes, 120)


class CalendarStateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "calendar_board.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_boolean_ids_fail_closed_without_partial_state(self):
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "guilds": [
                        {"guild_id": 1, "channel_id": 10, "message_id": 20},
                        {"guild_id": True, "channel_id": 11, "message_id": 21},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertLogs(level="ERROR"):
            manager = CalendarManager(self.state_path)
        self.assertFalse(manager.state_available)
        self.assertIsNone(manager.get_binding(1))

    def test_persisted_binding_is_versioned_and_mode_600(self):
        manager = CalendarManager(self.state_path)
        manager._commit_bindings({1: CalendarBinding(1, 10, 20)})
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["guilds"][0]["message_id"], 20)
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)


class CalendarBoardTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.state_path = Path(self.temp_dir.name) / "calendar_board.json"
        self.manager = CalendarManager(self.state_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_board_renders_current_month_marker_and_upcoming_link(self):
        now = datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ)
        event = make_event(
            start=datetime(2026, 8, 24, 13, 0, tzinfo=timezone.utc),
            end=datetime(2026, 8, 24, 15, 0, tzinfo=timezone.utc),
        )
        text = self.manager.render_board_text("Guild *Name*", [event], now=now)
        self.assertIn("2026 年 8 月", text)
        self.assertIn("24•", text)
        self.assertIn("Guild \\*Name\\*", text)
        self.assertIn("https://discord.com/events/1/1", text)

    def test_persistent_board_has_stable_custom_ids(self):
        view = self.manager.persistent_board_view()
        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())
        payload_text = repr(view.to_components())
        for custom_id in (
            BOARD_CREATE_CUSTOM_ID,
            BOARD_EDIT_CUSTOM_ID,
            BOARD_BROWSE_CUSTOM_ID,
            BOARD_REFRESH_CUSTOM_ID,
        ):
            self.assertIn(custom_id, payload_text)

    def test_next_midnight_uses_utc_plus_8(self):
        now = datetime(2026, 8, 24, 23, 30, tzinfo=CALENDAR_TZ)
        self.assertEqual(self.manager.seconds_until_next_midnight(now), 30 * 60)

    async def test_midnight_loop_isolates_guild_refresh_errors(self):
        first_guild = FakeGuild(1)
        second_guild = FakeGuild(2)
        self.manager._bindings = {
            1: CalendarBinding(1, 10, 20),
            2: CalendarBinding(2, 11, 21),
        }
        self.manager._client = SimpleNamespace(
            wait_until_ready=AsyncMock(),
            get_guild=lambda guild_id: {1: first_guild, 2: second_guild}.get(guild_id),
        )
        secret = "midnight-secret"
        self.manager.refresh_guild = AsyncMock(
            side_effect=[RuntimeError(secret), True]
        )

        with (
            patch(
                "src.calendar_events.asyncio.sleep",
                new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
            ) as sleep,
            self.assertLogs(level="ERROR") as captured,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.manager._run_midnight_loop()

        self.manager.refresh_guild.assert_has_awaits(
            [call(first_guild), call(second_guild)]
        )
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(captured.output, ["ERROR:root:行事曆午夜重新整理失敗。"])
        self.assertNotIn(secret, "\n".join(captured.output))

    async def test_midnight_guard_logs_task_error_without_detail(self):
        secret = "task-secret"
        self.manager._run_midnight_loop = AsyncMock(side_effect=RuntimeError(secret))

        with self.assertLogs(level="ERROR") as captured:
            await self.manager._guard_midnight_loop()

        self.assertEqual(captured.output, ["ERROR:root:行事曆午夜背景工作異常終止。"])
        self.assertNotIn(secret, "\n".join(captured.output))

    async def test_midnight_guard_propagates_cancellation(self):
        self.manager._run_midnight_loop = AsyncMock(side_effect=asyncio.CancelledError())

        with self.assertRaises(asyncio.CancelledError):
            await self.manager._guard_midnight_loop()

    async def test_bind_persists_message_and_unbind_removes_it(self):
        guild = FakeGuild()
        channel = FakeTextChannel()
        guild.add_channel(channel)

        binding = await self.manager.bind(guild, channel, actor_id=123)
        self.assertEqual(binding.channel_id, channel.id)
        self.assertEqual(binding.message_id, 100)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(self.manager.get_binding(guild.id), binding)

        removed = await self.manager.unbind(guild, actor_id=123)
        self.assertTrue(removed)
        self.assertIsNone(self.manager.get_binding(guild.id))
        self.assertTrue(channel.messages[100].deleted)

    async def test_bind_does_not_require_read_message_history(self):
        guild = FakeGuild()
        channel = FakeTextChannel()
        channel.permissions.read_message_history = False
        guild.add_channel(channel)

        binding = await self.manager.bind(guild, channel, actor_id=123)

        self.assertEqual(binding.channel_id, channel.id)
        self.assertEqual(len(channel.sent), 1)

    async def test_create_event_only_mutates_discord_and_leaves_board_refresh_to_gateway(self):
        guild = FakeGuild()
        channel = FakeTextChannel()
        guild.add_channel(channel)
        self.manager._commit_bindings({1: CalendarBinding(1, channel.id, 100)})
        actor = SimpleNamespace(
            id=123,
            guild_permissions=SimpleNamespace(administrator=False, manage_events=True),
        )
        event_input = build_calendar_event_input(
            name="團練",
            start="2026-08-25 20:00",
            duration_minutes=120,
            location="Discord",
            now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
        )
        # create_event revalidates against real current time; keep this test independent of wall clock.
        event_input = event_input.__class__(
            event_input.name,
            datetime(2099, 8, 25, 20, 0, tzinfo=CALENDAR_TZ),
            datetime(2099, 8, 25, 22, 0, tzinfo=CALENDAR_TZ),
            event_input.location,
            event_input.description,
        )
        event = await self.manager.create_event(
            guild,
            CalendarDraft("create", event_input),
            actor,
        )
        self.assertEqual(event.id, 900)
        self.assertEqual(len(guild.created), 1)
        self.assertEqual(guild.created[0]["entity_type"], discord.EntityType.external)
        self.assertEqual(guild.created[0]["location"], "Discord")
        self.assertEqual(guild.fetch_events_calls, 0)
        self.assertEqual(guild.fetch_event_calls, 0)

    def test_ai_create_draft_is_only_a_draft(self):
        self.manager._bindings[1] = CalendarBinding(1, 10, 20)
        scope = CalendarScope(
            guild_id=1,
            user_id=123,
            can_manage_events=True,
            now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
        )
        draft = self.manager.build_create_draft(
            scope,
            {
                "name": "團練",
                "start": "2026-08-25 20:00",
                "duration_minutes": 120,
                "location": "Discord",
            },
        )
        self.assertEqual(draft.action, "create")
        self.assertIsNone(draft.event_id)
        self.assertEqual(draft.to_ai_payload()["duration_minutes"], 120)

    async def test_rebind_replaces_old_board_and_invalidates_old_message(self):
        guild = FakeGuild()
        first = FakeTextChannel(10)
        second = FakeTextChannel(11)
        guild.add_channel(first)
        guild.add_channel(second)

        old_binding = await self.manager.bind(guild, first, actor_id=1)
        new_binding = await self.manager.bind(guild, second, actor_id=1)

        self.assertTrue(first.messages[old_binding.message_id].deleted)
        self.assertEqual(self.manager.get_binding(guild.id), new_binding)
        old_interaction = SimpleNamespace(
            guild_id=guild.id,
            channel_id=first.id,
            message=first.messages[old_binding.message_id],
        )
        self.assertFalse(self.manager.board_interaction_is_current(old_interaction))

    async def test_refresh_recreates_missing_board_and_updates_binding(self):
        guild = FakeGuild()
        channel = FakeTextChannel(10)
        guild.add_channel(channel)
        self.manager._commit_bindings({1: CalendarBinding(1, channel.id, 999)})

        refreshed = await self.manager.refresh_guild(guild)

        self.assertTrue(refreshed)
        replacement = self.manager.get_binding(guild.id)
        self.assertEqual(replacement.message_id, 100)
        self.assertEqual(len(channel.sent), 1)
        self.assertEqual(channel.fetch_message_calls, 0)
        self.assertEqual(guild.fetch_events_calls, 0)

    def test_channel_and_guild_cleanup_remove_binding(self):
        self.manager._commit_bindings({1: CalendarBinding(1, 10, 20)})
        self.manager.handle_channel_delete(1, 10)
        self.assertIsNone(self.manager.get_binding(1))

        self.manager._commit_bindings({1: CalendarBinding(1, 11, 21)})
        self.manager.delete_guild(1)
        self.assertIsNone(self.manager.get_binding(1))

    def test_edit_picker_caps_select_at_25_and_has_second_page(self):
        events = [make_event(event_id=index) for index in range(1, 27)]
        first_page = CalendarEditPickerView(self.manager, 1, 1, events)
        first_select = next(
            child for child in first_page.children if isinstance(child, discord.ui.Select)
        )
        self.assertEqual(len(first_select.options), 25)
        self.assertTrue(any(getattr(child, "label", None) == "下一頁" for child in first_page.children))

        second_page = CalendarEditPickerView(self.manager, 1, 1, events, page=1)
        second_select = next(
            child for child in second_page.children if isinstance(child, discord.ui.Select)
        )
        self.assertEqual(len(second_select.options), 1)

    async def test_edit_event_uses_gateway_cache_and_leaves_board_refresh_to_gateway(self):
        guild = FakeGuild()
        channel = FakeTextChannel(10)
        guild.add_channel(channel)
        self.manager._commit_bindings({1: CalendarBinding(1, channel.id, 100)})
        current = make_event(
            event_id=5,
            start=datetime(2099, 8, 25, 12, 0, tzinfo=timezone.utc),
            end=datetime(2099, 8, 25, 14, 0, tzinfo=timezone.utc),
        )
        updated = make_event(
            event_id=5,
            name="新團練",
            start=datetime(2099, 8, 25, 13, 0, tzinfo=timezone.utc),
            end=datetime(2099, 8, 25, 15, 0, tzinfo=timezone.utc),
        )
        current.edit = AsyncMock(return_value=updated)
        guild.event_by_id[5] = current
        actor = SimpleNamespace(
            id=123,
            guild_permissions=SimpleNamespace(administrator=False, manage_events=True),
        )
        draft_input = build_calendar_event_input(
            name="新團練",
            start="2099-08-25 21:00",
            duration_minutes=120,
            location="Discord",
            now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
        )

        result = await self.manager.edit_event(
            guild,
            CalendarDraft("edit", draft_input, event_id=5),
            actor,
        )

        self.assertIs(result, updated)
        current.edit.assert_awaited_once()
        self.assertEqual(guild.fetch_event_calls, 0)
        self.assertEqual(guild.fetch_events_calls, 0)

    async def test_confirmation_rechecks_permission_before_mutation(self):
        self.manager._bindings[1] = CalendarBinding(1, 10, 20)
        event_input = build_calendar_event_input(
            name="團練",
            start="2099-08-25 20:00",
            duration_minutes=60,
            location="Discord",
            now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
        )
        draft = CalendarDraft("create", event_input)
        self.manager.create_event = AsyncMock()
        view = self.manager.confirmation_view(draft, user_id=7, guild_id=1)
        confirm_button = next(
            child for child in view.children if getattr(child, "label", None) == "確認"
        )
        interaction = SimpleNamespace(
            guild=FakeGuild(),
            guild_id=1,
            user=SimpleNamespace(
                id=7,
                guild_permissions=SimpleNamespace(administrator=False, manage_events=False),
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            message=None,
        )

        await confirm_button.callback(interaction)

        self.manager.create_event.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()

    async def test_concurrent_confirm_calls_create_event_once(self):
        self.manager._bindings[1] = CalendarBinding(1, 10, 20)
        draft = CalendarDraft(
            "create",
            build_calendar_event_input(
                name="團練",
                start="2099-08-25 20:00",
                duration_minutes=60,
                location="Discord",
            ),
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def create_event(*_args):
            started.set()
            await release.wait()
            return make_event(event_id=900)

        self.manager.create_event = AsyncMock(side_effect=create_event)
        view = self.manager.confirmation_view(draft, user_id=7, guild_id=1)
        button = next(child for child in view.children if child.label == "確認")

        def interaction():
            return SimpleNamespace(
                guild=FakeGuild(),
                guild_id=1,
                user=SimpleNamespace(
                    id=7,
                    guild_permissions=SimpleNamespace(administrator=False, manage_events=True),
                ),
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()),
                message=None,
            )

        first = interaction()
        second = interaction()
        first_task = asyncio.create_task(button.callback(first))
        await started.wait()
        await button.callback(second)
        release.set()
        await first_task

        self.manager.create_event.assert_awaited_once()
        self.assertIn("正在處理或已完成", second.followup.send.await_args.args[0])

    async def test_confirm_and_cancel_cannot_both_win(self):
        self.manager._bindings[1] = CalendarBinding(1, 10, 20)
        draft = CalendarDraft(
            "create",
            build_calendar_event_input(
                name="團練",
                start="2099-08-25 20:00",
                duration_minutes=60,
                location="Discord",
            ),
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def create_event(*_args):
            started.set()
            await release.wait()
            return make_event(event_id=900)

        self.manager.create_event = AsyncMock(side_effect=create_event)
        view = self.manager.confirmation_view(draft, user_id=7, guild_id=1)
        confirm = next(child for child in view.children if child.label == "確認")
        cancel = next(child for child in view.children if child.label == "取消")
        confirm_interaction = SimpleNamespace(
            guild=FakeGuild(), guild_id=1,
            user=SimpleNamespace(
                id=7,
                guild_permissions=SimpleNamespace(administrator=False, manage_events=True),
            ),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), message=None,
        )
        cancel_interaction = SimpleNamespace(
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock())
        )

        confirm_task = asyncio.create_task(confirm.callback(confirm_interaction))
        await started.wait()
        await cancel.callback(cancel_interaction)
        release.set()
        await confirm_task

        self.manager.create_event.assert_awaited_once()
        cancel_interaction.response.edit_message.assert_not_awaited()
        self.assertIn(
            "正在處理或已完成",
            cancel_interaction.response.send_message.await_args.args[0],
        )

    async def test_completed_confirmation_modal_cannot_mutate(self):
        draft = CalendarDraft(
            "create",
            build_calendar_event_input(
                name="團練",
                start="2099-08-25 20:00",
                duration_minutes=60,
                location="Discord",
            ),
        )
        self.manager.create_event = AsyncMock()
        view = self.manager.confirmation_view(draft, user_id=7, guild_id=1)
        self.assertTrue(await view.begin_action())
        await view.complete_action()
        modal = CalendarDraftCreateModal(
            self.manager,
            draft,
            source_message=None,
            confirmation_view=view,
        )
        interaction = SimpleNamespace(
            guild=FakeGuild(),
            user=SimpleNamespace(id=7),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await modal.on_submit(interaction)

        self.manager.create_event.assert_not_awaited()
        self.assertIn("正在處理或已完成", interaction.followup.send.await_args.args[0])

    async def test_confirmation_calendar_user_error_can_retry_successfully(self):
        self.manager._bindings[1] = CalendarBinding(1, 10, 20)
        draft = CalendarDraft(
            "create",
            build_calendar_event_input(
                name="團練",
                start="2099-08-25 20:00",
                duration_minutes=60,
                location="Discord",
            ),
        )
        self.manager.create_event = AsyncMock(
            side_effect=[CalendarUserError("暫時失敗"), make_event(event_id=900)]
        )
        view = self.manager.confirmation_view(draft, user_id=7, guild_id=1)
        button = next(child for child in view.children if child.label == "確認")

        def interaction():
            return SimpleNamespace(
                guild=FakeGuild(), guild_id=1,
                user=SimpleNamespace(
                    id=7,
                    guild_permissions=SimpleNamespace(administrator=False, manage_events=True),
                ),
                response=SimpleNamespace(defer=AsyncMock()),
                followup=SimpleNamespace(send=AsyncMock()), message=None,
            )

        await button.callback(interaction())
        retry = interaction()
        await button.callback(retry)

        self.assertEqual(self.manager.create_event.await_count, 2)
        self.assertIn("已建立活動", retry.followup.send.await_args.args[0])

    async def test_board_edit_uses_cache_and_responds_without_thinking(self):
        self.manager._bindings[1] = CalendarBinding(1, 10, 20)
        guild = FakeGuild()
        guild.events = [make_event(event_id=5)]
        interaction = SimpleNamespace(
            guild=guild,
            guild_id=1,
            channel_id=10,
            message=SimpleNamespace(id=20),
            user=SimpleNamespace(
                id=7,
                guild_permissions=SimpleNamespace(administrator=False, manage_events=True),
            ),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
                is_done=lambda: False,
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await self.manager.handle_board_action(interaction, "edit")

        interaction.response.defer.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()
        self.assertEqual(guild.fetch_events_calls, 0)

    async def test_board_browse_uses_cache_and_responds_without_thinking(self):
        self.manager._bindings[1] = CalendarBinding(1, 10, 20)
        guild = FakeGuild()
        guild.events = [make_event(event_id=5)]
        interaction = SimpleNamespace(
            guild=guild,
            guild_id=1,
            channel_id=10,
            message=SimpleNamespace(id=20),
            user=SimpleNamespace(id=7),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
                is_done=lambda: False,
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await self.manager.handle_board_action(interaction, "browse")

        interaction.response.defer.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()
        self.assertEqual(guild.fetch_events_calls, 0)

    async def test_board_refresh_edits_same_message_from_cache_without_thinking(self):
        self.manager._bindings[1] = CalendarBinding(1, 10, 20)
        guild = FakeGuild()
        guild.events = [make_event(event_id=5)]
        interaction = SimpleNamespace(
            guild=guild,
            guild_id=1,
            channel_id=10,
            message=SimpleNamespace(id=20),
            user=SimpleNamespace(id=7),
            response=SimpleNamespace(
                edit_message=AsyncMock(),
                defer=AsyncMock(),
                is_done=lambda: False,
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await self.manager.handle_board_action(interaction, "refresh")

        interaction.response.defer.assert_not_awaited()
        interaction.response.edit_message.assert_awaited_once()
        interaction.followup.send.assert_not_awaited()
        self.assertEqual(guild.fetch_events_calls, 0)

    async def test_admin_panel_channel_select_binds_with_silent_defer(self):
        guild = FakeGuild()
        channel = FakeTextChannel(10)
        guild.add_channel(channel)
        view = self.manager.admin_view(user_id=7, guild_id=guild.id)
        channel_select = next(
            item for item in view.children if isinstance(item, discord.ui.ChannelSelect)
        )
        channel_select._values = [SimpleNamespace(resolve=lambda: channel)]
        interaction = SimpleNamespace(
            guild=guild,
            guild_id=guild.id,
            user=SimpleNamespace(id=7),
            permissions=SimpleNamespace(administrator=True),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await channel_select.callback(interaction)

        interaction.response.defer.assert_awaited_once_with()
        interaction.edit_original_response.assert_awaited_once()
        binding = self.manager.get_binding(guild.id)
        self.assertIsNotNone(binding)
        self.assertEqual(binding.channel_id, channel.id)
        self.assertEqual(len(channel.sent), 1)
        self.assertIn("已將行事曆看板綁定", interaction.edit_original_response.await_args.kwargs["content"])
        buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
        self.assertTrue(all(not item.disabled for item in buttons))

    async def test_admin_panel_refresh_and_unbind_are_buttons_with_silent_defer(self):
        guild = FakeGuild()
        channel = FakeTextChannel(10)
        guild.add_channel(channel)
        await self.manager.bind(guild, channel, actor_id=7)
        view = self.manager.admin_view(user_id=7, guild_id=guild.id)

        refresh_button = next(item for item in view.children if getattr(item, "label", None) == "重新整理看板")
        refresh_interaction = SimpleNamespace(
            guild=guild,
            guild_id=guild.id,
            user=SimpleNamespace(id=7),
            permissions=SimpleNamespace(administrator=True),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        await refresh_button.callback(refresh_interaction)

        refresh_interaction.response.defer.assert_awaited_once_with()
        refresh_interaction.edit_original_response.assert_awaited_once()
        self.assertIn("已重新整理", refresh_interaction.edit_original_response.await_args.kwargs["content"])

        unbind_button = next(item for item in view.children if getattr(item, "label", None) == "解除綁定")
        unbind_interaction = SimpleNamespace(
            guild=guild,
            guild_id=guild.id,
            user=SimpleNamespace(id=7),
            permissions=SimpleNamespace(administrator=True),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        await unbind_button.callback(unbind_interaction)

        unbind_interaction.response.defer.assert_awaited_once_with()
        unbind_interaction.edit_original_response.assert_awaited_once()
        self.assertIsNone(self.manager.get_binding(guild.id))
        self.assertIn("已解除", unbind_interaction.edit_original_response.await_args.kwargs["content"])
        buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
        self.assertTrue(all(item.disabled for item in buttons))

    async def test_admin_panel_rejects_other_user_or_revoked_admin(self):
        view = self.manager.admin_view(user_id=7, guild_id=1)
        for user_id, administrator in ((8, True), (7, False)):
            interaction = SimpleNamespace(
                guild_id=1,
                user=SimpleNamespace(id=user_id),
                permissions=SimpleNamespace(administrator=administrator),
                response=SimpleNamespace(send_message=AsyncMock()),
            )
            self.assertFalse(await view.interaction_check(interaction))
            interaction.response.send_message.assert_awaited_once()


class CalendarAgentToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.manager = CalendarManager(Path(self.temp_dir.name) / "calendar_board.json")
        self.manager._bindings[1] = CalendarBinding(1, 10, 20)
        self.notifier = SimpleNamespace(fetch_current_offers=AsyncMock(return_value=None))
        self.ai_client = SimpleNamespace()
        self.tools = AgentTools(
            self.notifier,
            self.ai_client,
            calendar=self.manager,
            search_provider="search",
            image_search_provider="images",
            fetch_provider="fetch",
        )
        self.context = ToolContext("Guild", "general", "text")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_calendar_schemas_are_least_privilege(self):
        readonly = CalendarScope(
            guild_id=1,
            user_id=2,
            can_manage_events=False,
            now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
        )
        writable = CalendarScope(
            guild_id=1,
            user_id=3,
            can_manage_events=True,
            now=readonly.now,
        )
        readonly_names = {
            item["function"]["name"] for item in self.tools.schemas_for(readonly)
        }
        writable_names = {
            item["function"]["name"] for item in self.tools.schemas_for(writable)
        }
        self.assertIn("calendar_get_events", readonly_names)
        self.assertNotIn("calendar_propose_create", readonly_names)
        self.assertNotIn("calendar_propose_edit", readonly_names)
        self.assertIn("calendar_propose_create", writable_names)
        self.assertIn("calendar_propose_edit", writable_names)

    async def test_calendar_get_events_returns_request_refs_without_discord_ids(self):
        scope = CalendarScope(
            guild_id=1,
            user_id=3,
            can_manage_events=True,
            now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
        )
        event = make_event(event_id=987654321)
        self.manager.get_events_for_ai = AsyncMock(return_value=[event])
        research = ResearchContext()
        raw = await self.tools.execute(
            "calendar_get_events",
            "{}",
            self.context,
            research,
            None,
            scope,
        )
        payload = json.loads(raw)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["events"][0]["event_ref"], 1)
        self.assertNotIn("987654321", raw)
        self.assertEqual(research.calendar_event_refs, {1: 987654321})

    async def test_calendar_propose_create_only_sets_pending_draft(self):
        scope = CalendarScope(
            guild_id=1,
            user_id=3,
            can_manage_events=True,
            now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
        )
        research = ResearchContext()
        raw = await self.tools.execute(
            "calendar_propose_create",
            json.dumps(
                {
                    "name": "團練",
                    "start": "2026-08-25 20:00",
                    "duration_minutes": 120,
                    "location": "Discord",
                }
            ),
            self.context,
            research,
            None,
            scope,
        )
        payload = json.loads(raw)
        self.assertTrue(payload["requires_confirmation"])
        self.assertIsNotNone(research.calendar_draft)
        self.assertEqual(research.calendar_draft.action, "create")

    async def test_calendar_propose_edit_requires_current_request_ref(self):
        scope = CalendarScope(
            guild_id=1,
            user_id=3,
            can_manage_events=True,
            now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
        )
        research = ResearchContext()
        raw = await self.tools.execute(
            "calendar_propose_edit",
            '{"event_ref":1,"start":"2026-08-25 21:00"}',
            self.context,
            research,
            None,
            scope,
        )
        self.assertEqual(
            json.loads(raw),
            {"ok": False, "error": "event_ref_not_allowed"},
        )

    async def test_calendar_propose_edit_uses_current_request_ref_without_leaking_id(self):
        scope = CalendarScope(
            guild_id=1,
            user_id=3,
            can_manage_events=True,
            now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
        )
        event = make_event(
            event_id=777888999,
            start=datetime(2099, 8, 25, 12, 0, tzinfo=timezone.utc),
            end=datetime(2099, 8, 25, 14, 0, tzinfo=timezone.utc),
        )
        guild = FakeGuild()
        guild.events = [event]
        guild.event_by_id[event.id] = event
        self.manager._client = SimpleNamespace(get_guild=lambda _guild_id: guild)
        research = ResearchContext()

        await self.tools.execute(
            "calendar_get_events",
            "{}",
            self.context,
            research,
            None,
            scope,
        )
        raw = await self.tools.execute(
            "calendar_propose_edit",
            '{"event_ref":1,"start":"2099-08-25 21:00"}',
            self.context,
            research,
            None,
            scope,
        )

        payload = json.loads(raw)
        self.assertTrue(payload["requires_confirmation"])
        self.assertNotIn("777888999", raw)
        self.assertEqual(research.calendar_draft.action, "edit")
        self.assertEqual(research.calendar_draft.event_id, 777888999)
        self.assertEqual(guild.fetch_events_calls, 0)
        self.assertEqual(guild.fetch_event_calls, 0)


class CalendarChatIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_chat_adds_calendar_time_context_and_returns_pending_draft(self):
        with TemporaryDirectory() as temp_dir:
            manager = CalendarManager(Path(temp_dir) / "calendar_board.json")
            manager._bindings[1] = CalendarBinding(1, 10, 20)
            notifier = SimpleNamespace(fetch_current_offers=AsyncMock(return_value=None))
            tools = AgentTools(
                notifier,
                SimpleNamespace(),
                calendar=manager,
                search_provider="search",
                image_search_provider="images",
                fetch_provider="fetch",
            )
            first = AIResponse(
                None,
                (
                    AIToolCall(
                        "call-1",
                        "calendar_propose_create",
                        json.dumps(
                            {
                                "name": "團練",
                                "start": "2026-08-25 20:00",
                                "duration_minutes": 120,
                                "location": "Discord",
                            }
                        ),
                    ),
                ),
            )
            second = AIResponse("我已整理成待確認活動，請按確認後才會建立。", ())
            ai_client = SimpleNamespace(
                start=AsyncMock(),
                close=AsyncMock(),
                chat=AsyncMock(side_effect=[first, second]),
            )
            chat = ChatManager(ai_client, tools)
            chat.record_user_message(99, "User", "明天晚上八點新增團練兩小時")
            scope = CalendarScope(
                guild_id=1,
                user_id=3,
                can_manage_events=True,
                now=datetime(2026, 8, 24, 18, 35, tzinfo=CALENDAR_TZ),
            )
            reply = await chat.generate_reply(
                99,
                ToolContext("Guild", "general", "text"),
                calendar_scope=scope,
            )

            self.assertIsNotNone(reply.calendar_draft)
            self.assertEqual(reply.calendar_draft.action, "create")
            first_call = ai_client.chat.await_args_list[0]
            messages = first_call.args[0]
            self.assertEqual(messages[1]["role"], "system")
            self.assertIn("2026-08-24 18:35 UTC+8", messages[1]["content"])
            tool_names = {
                item["function"]["name"] for item in first_call.kwargs["tools"]
            }
            self.assertIn("calendar_propose_create", tool_names)
            self.assertIn("calendar_propose_edit", tool_names)


class CalendarBotIntegrationTest(unittest.IsolatedAsyncioTestCase):
    def test_calendar_is_single_admin_command_and_scheduled_event_intent_is_registered(self):
        with TemporaryDirectory() as temp_dir:
            manager = CalendarManager(Path(temp_dir) / "calendar_board.json")
            bot = HoroBot(
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(),
                calendar=manager,
                ai_client=SimpleNamespace(),
            )

            command = bot.tree.get_command("行事曆")
            self.assertIsNotNone(command)
            self.assertEqual(command.description, "開啟行事曆管理")
            self.assertTrue(command.guild_only)
            self.assertIsNotNone(command.default_permissions)
            self.assertTrue(command.default_permissions.administrator)
            self.assertFalse(hasattr(command, "commands"))
            self.assertTrue(bot.intents.guild_scheduled_events)

    async def test_calendar_command_opens_ephemeral_admin_panel(self):
        with TemporaryDirectory() as temp_dir:
            manager = CalendarManager(Path(temp_dir) / "calendar_board.json")
            bot = HoroBot(
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(),
                calendar=manager,
                ai_client=SimpleNamespace(),
            )
            interaction = SimpleNamespace(
                guild=SimpleNamespace(id=1),
                permissions=SimpleNamespace(administrator=True),
                user=SimpleNamespace(id=7),
                response=SimpleNamespace(send_message=AsyncMock()),
            )

            command = bot.tree.get_command("行事曆")
            await command.callback(interaction)

            interaction.response.send_message.assert_awaited_once()
            args = interaction.response.send_message.await_args.args
            kwargs = interaction.response.send_message.await_args.kwargs
            self.assertIn("目前尚未綁定", args[0])
            self.assertTrue(kwargs["ephemeral"])
            self.assertIsInstance(kwargs["view"], CalendarAdminView)
            channel_select = next(
                item for item in kwargs["view"].children if isinstance(item, discord.ui.ChannelSelect)
            )
            self.assertEqual(channel_select.channel_types, [discord.ChannelType.text])

    async def test_scheduled_event_gateway_refreshes_current_binding(self):
        guild = SimpleNamespace(id=1)
        calendar = SimpleNamespace(
            has_binding=lambda guild_id: guild_id == 1,
            refresh_guild=AsyncMock(return_value=True),
        )
        bot = SimpleNamespace(
            calendar=calendar,
            get_guild=lambda guild_id: guild if guild_id == 1 else None,
        )

        await HoroBot.on_scheduled_event_create(bot, SimpleNamespace(guild_id=1))

        calendar.refresh_guild.assert_awaited_once_with(guild)

    async def test_ai_calendar_reply_attaches_confirmation_without_mutation(self):
        with TemporaryDirectory() as temp_dir:
            manager = CalendarManager(Path(temp_dir) / "calendar_board.json")
            manager.create_event = AsyncMock()
            event_input = build_calendar_event_input(
                name="團練",
                start="2099-08-25 20:00",
                duration_minutes=60,
                location="Discord",
                now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
            )
            recorded = []
            chat = SimpleNamespace(
                record_assistant_message=lambda channel_id, content: recorded.append(
                    (channel_id, content)
                )
            )
            message = SimpleNamespace(
                guild=SimpleNamespace(id=1),
                author=SimpleNamespace(id=7),
                channel=SimpleNamespace(id=55, send=AsyncMock()),
                reply=AsyncMock(),
            )
            bot = SimpleNamespace(chat=chat, calendar=manager)

            await HoroBot._send_ai_answer(
                bot,
                message,
                ChatReply(
                    "請確認以下活動。",
                    calendar_draft=CalendarDraft("create", event_input),
                ),
            )

            message.reply.assert_awaited_once()
            sent_view = message.reply.await_args.kwargs["view"]
            self.assertEqual(sent_view.timeout, 10 * 60)
            self.assertEqual(len(sent_view.children), 3)
            manager.create_event.assert_not_awaited()
            self.assertEqual(len(recorded), 1)

    async def test_ai_confirmation_ui_failure_falls_back_without_mutation(self):
        with TemporaryDirectory() as temp_dir:
            manager = CalendarManager(Path(temp_dir) / "calendar_board.json")
            manager.create_event = AsyncMock()
            event_input = build_calendar_event_input(
                name="團練",
                start="2099-08-25 20:00",
                duration_minutes=60,
                location="Discord",
                now=datetime(2026, 8, 24, 18, 0, tzinfo=CALENDAR_TZ),
            )
            replies = []

            async def reply(content=None, **kwargs):
                replies.append((content, kwargs))
                if "view" in kwargs:
                    raise discord.HTTPException(
                        SimpleNamespace(status=500, reason="test"),
                        "confirmation failed",
                    )

            recorded = []
            message = SimpleNamespace(
                guild=SimpleNamespace(id=1),
                author=SimpleNamespace(id=7),
                channel=SimpleNamespace(id=55, send=AsyncMock()),
                reply=reply,
            )
            bot = SimpleNamespace(
                calendar=manager,
                chat=SimpleNamespace(
                    record_assistant_message=lambda channel_id, content: recorded.append(
                        (channel_id, content)
                    )
                ),
            )

            await HoroBot._send_ai_answer(
                bot,
                message,
                ChatReply(
                    "請確認以下活動。",
                    calendar_draft=CalendarDraft("create", event_input),
                ),
            )

            self.assertEqual(len(replies), 2)
            self.assertIn("view", replies[0][1])
            self.assertNotIn("view", replies[1][1])
            self.assertIn("確認按鈕無法顯示", replies[1][0])
            self.assertIn("本次草稿未執行", replies[1][0])
            manager.create_event.assert_not_awaited()
            self.assertEqual(len(recorded), 1)


if __name__ == "__main__":
    unittest.main()
