import asyncio
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import discord

from src.bot import HoroBot
from src.ai.discord import codex_error_text
from src.ai.client import CodexBridgeClient
from src.ai.protocol import CodexBridgeError
from tests.support.access import configured_access

from tests.support.ai import settle, stop_tasks, StatusService, Typing, make_admin, interaction, role


class BotAdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = CodexBridgeClient("http://codex:8765", "a" * 64, cooldown_seconds=0)
        self.access = configured_access(True, 10, channel_ids=(20,))
        self.access.set_roles(10, frozenset({70}))
        self.bot = HoroBot(
            self.client, self.access, SimpleNamespace(),
            SimpleNamespace(close=AsyncMock()), SimpleNamespace(close=AsyncMock()),
            ai_text_display_enabled=False,
        )
        self.bot._connection.user = SimpleNamespace(id=99)
        self.members = {}
        self.guild = SimpleNamespace(id=10, get_member=self.members.get)
        self.guild.fetch_member = AsyncMock(side_effect=lambda user_id: self.members.get(user_id))
        self.chat_release = asyncio.Event()
        self.chat_entered = asyncio.Event()
        self.chat_calls = []
        self.chat_finished = []
        self.tasks = []
        self.gates = [self.chat_release]
        self.image_reads = []
        self.reply_text = "answer"

        async def request(_method, path, *, payload=None, **_kwargs):
            if path == "/v1/chat":
                self.chat_calls.append(payload)
                self.chat_entered.set()
                try:
                    await self.chat_release.wait()
                    return {"reply": self.reply_text}
                finally:
                    self.chat_finished.append(payload["conversation_key"])
            if path == "/v1/status":
                return StatusService().status_data
            return {}
        self.client._request = request

    async def asyncTearDown(self):
        for gate in self.gates:
            gate.set()
        await stop_tasks(self.tasks)
        await asyncio.wait_for(self.client.close(), 1)
        await asyncio.wait_for(self.bot.close(), 1)

    def message(self, user_id=30, *, thread=False, image_gate=None):
        member = SimpleNamespace(
            id=user_id, bot=False, display_name=f"Member {user_id}", guild=self.guild,
            roles=[SimpleNamespace(id=70)],
        )
        self.members[user_id] = member
        deliveries = []
        output_entered = asyncio.Event()
        channel = SimpleNamespace(
            id=21 if thread else 20, parent_id=20 if thread else None,
            type=discord.ChannelType.public_thread if thread else discord.ChannelType.text,
            typing=Typing, sent=[],
        )
        message = SimpleNamespace(
            author=member, guild=self.guild, channel=channel, webhook_id=None,
            content="<@99> private prompt", attachments=[], reference=None,
            mentions=[SimpleNamespace(id=99)], deliveries=deliveries,
            output_entered=output_entered, output_gate=None, after_output=None,
        )

        async def deliver(content=None, **kwargs):
            output_entered.set()
            if message.output_gate is not None:
                await message.output_gate.wait()
            view = kwargs.get("view")
            text = content if content is not None else repr(view.to_components())
            deliveries.append(text)
            if message.after_output is not None:
                await message.after_output()

        async def send(content=None, **kwargs):
            channel.sent.append(content if content is not None else repr(kwargs["view"].to_components()))
            await deliver(content, **kwargs)

        message.reply = deliver
        channel.send = send
        if image_gate is not None:
            self.gates.append(image_gate)
            async def read():
                self.image_reads.append(user_id)
                await image_gate.wait()
                return b"\x89PNG\r\n\x1a\n"
            message.attachments = [SimpleNamespace(
                filename="one.png", content_type="image/png", size=8, read=read,
            )]
        return message

    def start(self, message):
        task = asyncio.create_task(self.bot.on_message(message))
        self.tasks.append(task)
        return task

    def revoke(self, user_id):
        self.members[user_id] = SimpleNamespace(
            id=user_id, bot=False, roles=[], guild=self.guild, display_name="Member",
        )

    async def test_global_capacity_rejects_seventh_job_before_attachment_download(self):
        image_gate = asyncio.Event()
        image_gate.set()
        messages = [self.message(user_id, image_gate=image_gate) for user_id in range(30, 37)]
        jobs = [self.start(message) for message in messages]
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        await settle()
        self.assertEqual(len(self.chat_calls), 2)
        self.assertEqual(self.image_reads, [30, 31])
        self.assertTrue(jobs[6].done(), "seventh accepted job must receive busy immediately")
        self.assertIn(codex_error_text("busy"), messages[6].deliveries)
        self.chat_release.set()
        await asyncio.wait_for(asyncio.gather(*jobs), 0.5)
        self.assertEqual([call["text"] for call in self.chat_calls], ["private prompt"] * 6)

    async def test_shared_thread_accepts_only_one_additional_waiting_job(self):
        messages = [self.message(user_id, thread=True) for user_id in (30, 31, 32)]
        first = self.start(messages[0])
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        second = self.start(messages[1])
        third = self.start(messages[2])
        await settle()
        self.assertTrue(third.done(), "third job for one conversation must be rejected")
        self.assertEqual(len(self.chat_calls), 1)
        self.chat_release.set()
        await asyncio.wait_for(asyncio.gather(first, second, third), 0.3)
        self.assertEqual(len(self.chat_calls), 2)

    async def test_same_key_stays_serialized_through_discord_output(self):
        self.chat_release.set()
        first_message = self.message(30, thread=True)
        first_message.output_gate = asyncio.Event()
        self.gates.append(first_message.output_gate)
        first = self.start(first_message)
        await asyncio.wait_for(first_message.output_entered.wait(), 0.3)
        second = self.start(self.message(31, thread=True))
        await settle()
        self.assertEqual(len(self.chat_calls), 1, "next SDK turn started while prior Discord output was blocked")
        first_message.output_gate.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(len(self.chat_calls), 2)

    async def test_queued_job_times_out_without_starting_sdk_or_replaying_prompt(self):
        self.client.queue_timeout_seconds = 0.02
        first = self.start(self.message(30, thread=True))
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        message = self.message(31, thread=True)
        second = self.start(message)
        done, _ = await asyncio.wait({second}, timeout=0.2)
        self.assertIn(second, done, "queued job exceeded its configured queue deadline")
        self.assertEqual(len(self.chat_calls), 1)
        self.assertIn(codex_error_text("timeout"), message.deliveries)
        self.chat_release.set()
        await asyncio.wait_for(first, 0.3)

    async def test_image_download_has_a_bound_before_sdk_start(self):
        self.client.image_timeout_seconds = 0.02
        image_gate = asyncio.Event()
        message = self.message(image_gate=image_gate)
        job = self.start(message)
        done, _ = await asyncio.wait({job}, timeout=0.2)
        self.assertIn(job, done, "attachment download exceeded its configured deadline")
        self.assertEqual(self.chat_calls, [])
        self.assertNotEqual(message.deliveries, [])

    async def test_overall_deadline_includes_queue_and_image_work(self):
        for stage in ("queue", "images"):
            with self.subTest(stage=stage):
                self.client.work_timeout_seconds = 0.03
                self.client.queue_timeout_seconds = 1
                self.client.image_timeout_seconds = 1
                if stage == "queue":
                    blocker = self.start(self.message(30, thread=True))
                    await asyncio.wait_for(self.chat_entered.wait(), 0.3)
                    job = self.start(self.message(31, thread=True))
                else:
                    job = self.start(self.message(32, image_gate=asyncio.Event()))
                done, _ = await asyncio.wait({job}, timeout=0.2)
                self.assertIn(job, done, f"accepted deadline excluded {stage}")
                await stop_tasks(self.tasks)
                self.chat_entered.clear()

    async def test_overall_deadline_also_bounds_discord_output(self):
        self.client.work_timeout_seconds = 0.02
        self.chat_release.set()
        message = self.message()
        message.output_gate = asyncio.Event()
        self.gates.append(message.output_gate)
        job = self.start(message)
        await asyncio.wait_for(message.output_entered.wait(), 0.3)
        done, _ = await asyncio.wait({job}, timeout=0.2)
        self.assertIn(job, done, "accepted deadline excluded Discord output")
        message.output_gate.set()
        followup = self.message(31)
        await asyncio.wait_for(self.bot.on_message(followup), 0.3)
        self.assertIn("answer", followup.deliveries)

    async def test_role_revoked_during_images_uses_current_member_not_message_snapshot(self):
        self.chat_release.set()
        image_gate = asyncio.Event()
        message = self.message(image_gate=image_gate)
        job = self.start(message)
        await settle()
        self.assertEqual(self.image_reads, [30])
        self.revoke(30)
        image_gate.set()
        await asyncio.wait_for(job, 0.3)
        self.assertEqual(self.chat_calls, [])
        self.assertNotIn("answer", message.deliveries)

    async def test_role_revoked_while_queued_prevents_sdk_start(self):
        first = self.start(self.message(30, thread=True))
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        queued_message = self.message(31, thread=True)
        second = self.start(queued_message)
        await settle()
        self.revoke(31)
        self.chat_release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(len(self.chat_calls), 1)
        self.assertNotIn("answer", queued_message.deliveries)

    async def test_configuration_generation_change_invalidates_already_accepted_jobs(self):
        first_message = self.message(30, thread=True)
        first = self.start(first_message)
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        second_message = self.message(31, thread=True)
        second = self.start(second_message)
        await settle()
        # Both users still have role 70; a new configuration must still invalidate their old work.
        self.access.set_channels(10, frozenset({20, 22}))
        self.chat_release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 0.3)
        self.assertEqual(len(self.chat_calls), 1)
        self.assertNotIn("answer", first_message.deliveries + second_message.deliveries)

    async def test_member_update_cancels_active_revoked_work(self):
        message = self.message()
        job = self.start(message)
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        before = message.author
        self.revoke(30)
        await asyncio.wait_for(self.bot.on_member_update(before, self.members[30]), 0.3)
        await settle()
        self.assertTrue(job.done(), "revoked member's running request was not cancelled")
        self.assertNotIn("answer", message.deliveries)

    async def test_fresh_role_check_stops_each_native_and_text_display_output_chunk(self):
        # Exercise the HTTP fallback explicitly even with member events enabled.
        self.guild.get_member = lambda _user_id: None
        self.chat_release.set()
        self.reply_text = "answer" * 1500
        for display in (False, True):
            for before_first in (False, True):
                with self.subTest(display=display, before_first=before_first):
                    self.bot.ai_text_display_enabled = display
                    message = self.message()
                    if before_first:
                        self.revoke(30)
                        self.guild.fetch_member = AsyncMock(side_effect=[
                            message.author, message.author, self.members[30],
                        ])
                    else:
                        self.guild.fetch_member = AsyncMock(side_effect=self.members.get)
                        async def revoke_after_first():
                            self.revoke(30)
                        message.after_output = revoke_after_first
                    with self.assertLogs(level=logging.INFO) as logs:
                        await asyncio.wait_for(self.bot.on_message(message), 0.3)
                    self.assertEqual(len(message.deliveries), 0 if before_first else 1)
                    self.assertEqual(self.guild.fetch_member.await_count, 3 if before_first else 4)
                    self.assertEqual(message.channel.sent, [])
                    rendered = "\n".join(logs.output)
                    self.assertIn("result=unauthorized", rendered)
                    self.assertNotIn("result=success", rendered)

    async def test_current_member_cache_avoids_http_role_fetches(self):
        self.chat_release.set()
        message = self.message()

        await asyncio.wait_for(self.bot.on_message(message), 0.3)

        self.assertEqual(message.deliveries, ["answer"])
        self.guild.fetch_member.assert_not_awaited()

    async def test_member_cache_miss_fetch_failures_never_use_message_snapshot(self):
        self.chat_release.set()
        self.guild.get_member = lambda _user_id: None
        forbidden = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "private detail",
        )
        missing = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "private detail",
        )
        for failure in (TimeoutError(), forbidden, missing, None):
            with self.subTest(failure=type(failure).__name__):
                message = self.message()
                self.guild.fetch_member = AsyncMock(return_value=None, side_effect=failure)

                await asyncio.wait_for(self.bot.on_message(message), 0.3)

                self.guild.fetch_member.assert_awaited_once_with(message.author.id)
                self.assertEqual(self.chat_calls, [])
                self.assertNotIn("answer", message.deliveries)
                self.assertNotIn("private detail", repr(message.deliveries))

    async def test_text_display_fallback_rechecks_revoked_roles(self):
        self.chat_release.set()
        self.bot.ai_text_display_enabled = True
        message = self.message()
        calls = []
        async def fail_then_revoke(content=None, **kwargs):
            calls.append((content, kwargs))
            if len(calls) == 1:
                self.revoke(30)
                raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "private transport data")
        message.reply = fail_then_revoke
        with self.assertLogs(level=logging.INFO) as logs:
            await asyncio.wait_for(self.bot.on_message(message), 0.3)
        self.assertEqual(len(calls), 1, "native fallback emitted AI output after roles were revoked")
        rendered = "\n".join(logs.output)
        self.assertIn("result=unauthorized", rendered)
        self.assertNotIn("result=success", rendered)
        self.assertNotIn("private transport data", rendered)

    async def test_unsent_and_partial_discord_failures_never_log_success(self):
        self.chat_release.set()
        self.reply_text = "private answer" * 600
        for display in (False, True):
            for fail_first in (False, True):
                with self.subTest(display=display, fail_first=fail_first):
                    self.bot.ai_text_display_enabled = display
                    message = self.message()
                    calls = []
                    reply = message.reply
                    async def fail(content=None, **kwargs):
                        calls.append((content, kwargs))
                        raise discord.HTTPException(
                            SimpleNamespace(status=400, reason="Bad Request"),
                            "private transport data",
                        )
                    message.channel.send = fail
                    if fail_first:
                        message.reply = fail
                    else:
                        message.reply = reply
                    with self.assertLogs(level=logging.INFO) as logs:
                        await asyncio.wait_for(self.bot.on_message(message), 0.3)
                    self.assertEqual(len(message.deliveries), 0 if fail_first else 1)
                    self.assertEqual(len(calls), 2 if display else 1)
                    rendered = "\n".join(logs.output)
                    self.assertIn("result=unavailable", rendered)
                    self.assertNotIn("result=success", rendered)
                    for private in ("private prompt", "private answer", "private transport data", "Member 30"):
                        self.assertNotIn(private, rendered)

    async def test_successful_text_display_fallback_logs_success_after_remaining_text(self):
        self.chat_release.set()
        self.bot.ai_text_display_enabled = True
        self.reply_text = "a" * 4000 + "b" * 3000
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first):
                message = self.message()
                delivered = []
                calls = []
                failed = False
                async def send(content=None, **kwargs):
                    nonlocal failed
                    view = kwargs.get("view")
                    calls.append("display" if view is not None else "native")
                    if view is not None and not failed and (fail_first or delivered):
                        failed = True
                        raise discord.HTTPException(
                            SimpleNamespace(status=400, reason="Bad Request"), "private transport data",
                        )
                    delivered.append(view.children[0].content if view is not None else content)
                message.reply = message.channel.send = send
                with self.assertLogs(level=logging.INFO) as logs:
                    await asyncio.wait_for(self.bot.on_message(message), 0.3)
                self.assertEqual("".join(delivered), "a" * 4000 + "b" * 3000)
                self.assertEqual(
                    calls,
                    ["display", "native", "native", "native", "native"]
                    if fail_first else ["display", "display", "native", "native"],
                )
                self.assertIn("result=success", "\n".join(logs.output))

    async def test_client_close_finishes_cancelled_jobs_before_session_close(self):
        first = self.start(self.message(30, thread=True))
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        second = self.start(self.message(31, thread=True))
        await settle()
        pending_at_close = []
        session = SimpleNamespace(closed=False)
        async def close_session():
            pending_at_close.append(sum(not task.done() for task in (first, second)))
            session.closed = True
        session.close = close_session
        self.client._session = session
        await asyncio.wait_for(self.client.close(), 0.3)
        self.assertEqual(pending_at_close, [0])
        self.assertTrue(first.done() and second.done())
        self.assertEqual(len(self.chat_calls), 1)

    async def test_success_logs_only_safe_numeric_lifecycle_timings(self):
        self.chat_release.set()
        message = self.message()
        with self.assertLogs(level=logging.INFO) as logs:
            await asyncio.wait_for(self.bot.on_message(message), 0.3)
        rendered = "\n".join(logs.output)
        self.assertNotIn("private prompt", rendered)
        self.assertNotIn("Member 30", rendered)
        for field in ("queue_ms", "images_ms", "sdk_ms", "discord_ms"):
            self.assertRegex(rendered, rf"\b{field}=[0-9]+(?:\.[0-9]+)?\b")
        self.assertIn("result=success", rendered)

    async def test_role_change_cancels_old_jobs_before_external_archive(self):
        message = self.message()
        job = self.start(message)
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        original_request = self.client._request
        active_at_archive = []
        async def request(method, path, **kwargs):
            if path == "/v1/archive":
                active_at_archive.append(not job.done())
                return {}
            return await original_request(method, path, **kwargs)
        self.client._request = request
        view = make_admin(self.access, self.client)
        await asyncio.wait_for(view.handle_codex_role_select(interaction(), (role(80),)), 0.3)
        self.assertEqual(active_at_archive, [False])
        self.assertTrue(job.done())
        self.assertEqual(self.access.role_ids, frozenset({80}))
        self.assertNotIn("answer", message.deliveries)

    async def test_close_and_archive_do_not_cancel_cleanup_twice(self):
        cleanup_entered = asyncio.Event()
        cleanup_release = asyncio.Event()
        self.gates.append(cleanup_release)
        repeated_cancellation = []
        async def request(_method, path, **_kwargs):
            if path != "/v1/chat":
                return {}
            self.chat_entered.set()
            try:
                await self.chat_release.wait()
            except asyncio.CancelledError:
                cleanup_entered.set()
                try:
                    await cleanup_release.wait()
                except asyncio.CancelledError:
                    repeated_cancellation.append(True)
                    raise
                raise
            return {"reply": "answer"}
        self.client._request = request
        job = self.start(self.message())
        await asyncio.wait_for(self.chat_entered.wait(), 0.3)
        closing = asyncio.create_task(self.client.close())
        self.tasks.append(closing)
        await settle()
        self.assertTrue(cleanup_entered.is_set(), "client close never cancelled its running request")
        archiving = asyncio.create_task(self.client.archive_scope(10))
        self.tasks.append(archiving)
        await settle()
        cleanup_release.set()
        await asyncio.wait_for(asyncio.gather(closing, archiving), 0.3)
        self.assertEqual(repeated_cancellation, [])
        self.assertTrue(job.done())


    async def test_accepted_error_output_keeps_thread_ownership_and_can_be_cancelled(self):
        for action in ("close", "archive"):
            with self.subTest(action=action):
                client = CodexBridgeClient("http://codex:8765", "a" * 64, cooldown_seconds=0)
                self.client = self.bot.codex = client
                requests = []
                async def request(_method, path, *, payload=None, **_kwargs):
                    if path == "/v1/chat":
                        requests.append(payload)
                        raise CodexBridgeError("unavailable")
                    return {}
                client._request = request
                first_message = self.message(30, thread=True)
                first_message.output_gate = asyncio.Event()
                self.gates.append(first_message.output_gate)
                first = self.start(first_message)
                second = None
                try:
                    await asyncio.wait_for(first_message.output_entered.wait(), 0.3)
                    second = self.start(self.message(31, thread=True))
                    await settle()
                    with self.subTest(check="same-thread ordering"):
                        self.assertEqual(len(requests), 1, "next turn began while accepted error output was still blocked")
                        self.assertFalse(second.done())
                    if action == "close":
                        await asyncio.wait_for(client.close(), 0.3)
                    else:
                        await asyncio.wait_for(client.archive_scope(10), 0.3)
                    await settle()
                    with self.subTest(check="registered error delivery"):
                        self.assertTrue(first.done(), f"{action} lost track of accepted error delivery")
                        self.assertTrue(second.done())
                finally:
                    first_message.output_gate.set()
                    await stop_tasks([task for task in (first, second) if task is not None])
                    await asyncio.wait_for(client.close(), 0.3)

    async def test_timeout_error_delivery_has_a_bounded_cleanup_budget(self):
        self.client.work_timeout_seconds = 0.02
        self.client.cleanup_timeout_seconds = 0.02
        message = self.message(thread=True)
        message.output_gate = asyncio.Event()
        self.gates.append(message.output_gate)
        job = self.start(message)
        await asyncio.wait_for(message.output_entered.wait(), 0.3)
        done, _ = await asyncio.wait({job}, timeout=0.2)
        self.assertIn(job, done, "timeout notification exceeded its remaining work and cleanup budget")
        self.assertEqual(message.deliveries, [])
        self.assertEqual(len(self.chat_calls), 1)


if __name__ == "__main__":
    unittest.main()
