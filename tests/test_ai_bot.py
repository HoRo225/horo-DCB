from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.admin.panel import AdminPanelView
from src.ai.access import CodexAccess
from src.ai.access_service import AiAccessService
from src.ai.client import CodexBridgeClient
from src.ai.protocol import CodexArchiveResult, CodexBridgeError, CodexChatReply, CodexRuntimeStatus
from src.ai import discord as ai_discord
from src.bot import HoroBot


class BotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_preserves_http_cleanup_window(self):
        client = CodexBridgeClient('http://unused', 'unused')
        client._request = AsyncMock(return_value={'reply': 'answer'})
        async with client.accepted_request('guild:1:channel:2:user:3', parent_channel_id=None) as job:
            now = asyncio.get_running_loop().time()
            job.work_deadline = now + 20
            job.http_deadline = now + 25
            await client.chat(job.key, 'question', (), job=job)
            args = client._request.call_args.kwargs
            self.assertGreater(args['timeout_seconds'], 24)
            self.assertLessEqual(args['timeout_seconds'], 25)
            self.assertGreater(args['payload']['budget_ms'], 19000)
            self.assertLessEqual(args['payload']['budget_ms'], 20000)
            self.assertIsNone(args['payload']['parent_channel_id'])

    async def test_expired_work_never_sends_chat(self):
        client = CodexBridgeClient('http://unused', 'unused')
        client._request = AsyncMock()
        async with client.accepted_request('guild:1:channel:2:user:3') as job:
            job.work_deadline = asyncio.get_running_loop().time() - 1
            with self.assertRaises(CodexBridgeError):
                await client.chat(job.key, 'question', (), job=job)
        client._request.assert_not_awaited()

    async def test_archive_counts_and_parent_scope(self):
        client = CodexBridgeClient('http://unused', 'unused')
        client._request = AsyncMock(return_value={
            'detached_count': 3, 'archived_count': 2, 'archive_unconfirmed_count': 1,
        })
        result = await client.archive_scope(1, 2, include_children=True)
        self.assertEqual(result, CodexArchiveResult(3, 2, 1))
        self.assertEqual(client._request.call_args.kwargs['payload'], {
            'guild_id': 1, 'channel_id': 2, 'include_children': True,
        })
        self.assertLessEqual(client._request.call_args.kwargs['timeout_seconds'], 12)

    async def test_archive_rejects_inconsistent_counts(self):
        client = CodexBridgeClient('http://unused', 'unused')
        client._request = AsyncMock(return_value={
            'detached_count': 1, 'archived_count': 2, 'archive_unconfirmed_count': 0,
        })
        with self.assertRaises(CodexBridgeError):
            await client.archive_scope(1)

    async def test_archive_accepts_alias_aggregate_counts(self):
        client = CodexBridgeClient('http://unused', 'unused')
        client._request = AsyncMock(return_value={
            'detached_count': 2, 'archived_count': 1, 'archive_unconfirmed_count': 0,
        })
        self.assertEqual(await client.archive_scope(1), CodexArchiveResult(2, 1, 0))

    def access(self, root):
        with patch('src.ai.access.DEFAULT_CODEX_ACCESS_STATE_PATH', Path(root) / 'access.json'):
            access = CodexAccess(True, 1)
        access.set_channels(1, frozenset({2, 4}))
        access.set_roles(1, frozenset({3}))
        return access

    async def test_cancelled_caller_does_not_cancel_owned_change(self):
        with tempfile.TemporaryDirectory() as root:
            access = self.access(root)
            entered, released = asyncio.Event(), asyncio.Event()
            async def archive(*args, **kwargs):
                entered.set()
                await released.wait()
                return CodexArchiveResult(1, 1, 0)
            client = SimpleNamespace(archive_scope=archive)
            service = AiAccessService(access, client)
            caller = asyncio.create_task(service.change_roles(1, frozenset({5})))
            await entered.wait()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertEqual(access.role_ids, frozenset({3}))
            released.set()
            await service.close(deadline=asyncio.get_running_loop().time() + 1)
            self.assertEqual(access.role_ids, frozenset({5}))
            self.assertFalse(access._suspended)

    async def test_retired_panel_does_not_discard_accepted_queued_change(self):
        with tempfile.TemporaryDirectory() as root:
            access = self.access(root)
            client = SimpleNamespace(archive_scope=AsyncMock(return_value=CodexArchiveResult(1, 1, 0)))
            service = AiAccessService(access, client)
            current = True
            async with access.mutation_lock:
                caller = asyncio.create_task(service.change_roles(
                    1, frozenset({5}), still_current=lambda: current,
                ))
                await asyncio.sleep(0)
                self.assertEqual(len(service._tasks), 1)
                client.archive_scope.assert_not_awaited()
                current = False
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
            await service.close(deadline=asyncio.get_running_loop().time() + 1)
            client.archive_scope.assert_awaited_once_with(1)
            self.assertEqual(access.role_ids, frozenset({5}))
            self.assertFalse(access._suspended)
            with patch('src.ai.access.DEFAULT_CODEX_ACCESS_STATE_PATH', Path(root) / 'access.json'):
                self.assertEqual(CodexAccess(True, 1).role_ids, frozenset({5}))

    async def test_retired_panel_before_acceptance_creates_no_owner(self):
        with tempfile.TemporaryDirectory() as root:
            access = self.access(root)
            before = (Path(root) / 'access.json').read_bytes()
            client = SimpleNamespace(archive_scope=AsyncMock())
            service = AiAccessService(access, client)
            with patch.object(service, '_change', new=AsyncMock()) as change:
                self.assertEqual(await service.change_roles(
                    1, frozenset({5}), still_current=lambda: False,
                ), 'unchanged')
                change.assert_not_called()
            self.assertFalse(service._tasks)
            client.archive_scope.assert_not_awaited()
            self.assertEqual(access.role_ids, frozenset({3}))
            self.assertEqual((Path(root) / 'access.json').read_bytes(), before)

    async def test_panel_unavailable_snapshot_preserves_specific_reason(self):
        with tempfile.TemporaryDirectory() as root:
            access = self.access(root)
            panel = SimpleNamespace(
                guild_id=1, codex_access=access,
                _codex_stale_allowlist_counts=lambda: (0, 0),
                _codex_channel_permission_detail=lambda: None,
                _codex_stale_detail=lambda counts: None,
                _ai_display=lambda: ('plan', '', ''),
                _header=lambda *args: [], _rate_visible=lambda: False,
                _detail=Mock(return_value=None), _actions=lambda **kwargs: None,
                _set_container=Mock(),
            )
            panel._ai_state = lambda: AdminPanelView._ai_state(panel)
            for reason, expected in (
                ('initializing', 'AI 服務初始化中'),
                ('state_unavailable', 'AI 對話狀態檔不可用'),
                ('draining', 'AI 服務正在收尾'),
                ('auth_required', 'Codex 尚未登入'),
                ('status_stale', 'AI 帳號狀態已過期，請稍後重新整理'),
                ('unavailable', 'Codex bridge 無法連線'),
            ):
                with self.subTest(reason=reason):
                    panel.codex_status = CodexRuntimeStatus(
                        available=False, authenticated=False, plan=None,
                        sdk_version=None, runtime_version=None, web_search=None,
                        thread_count=0, ready=False, reason=reason,
                    )
                    self.assertEqual(panel._ai_state().detail, expected)
                    panel._detail.reset_mock()
                    AdminPanelView._render_ai(panel)
                    next_step = ('請確認 AI 服務正在執行且連線設定正確，再重新整理。'
                                 if reason == 'unavailable' else expected)
                    panel._detail.assert_called_once_with('下一步', next_step)
            access.enabled = False
            self.assertEqual(panel._ai_state().detail, 'AI 對話目前依設定停用')

    async def test_failed_detach_does_not_reduce_channels_or_grant_roles(self):
        with tempfile.TemporaryDirectory() as root:
            access = self.access(root)
            client = SimpleNamespace(archive_scope=AsyncMock(side_effect=CodexBridgeError('unavailable')))
            service = AiAccessService(access, client)
            self.assertEqual(await service.change_channels(1, frozenset({2})), 'archive_failed')
            self.assertEqual(access.channel_ids, frozenset({2, 4}))
            self.assertEqual(await service.change_roles(1, frozenset({5})), 'archive_failed')
            self.assertEqual(access.role_ids, frozenset({3}))
            self.assertFalse(access._suspended)

    async def test_unconfirmed_sdk_archive_commits_with_warning(self):
        with tempfile.TemporaryDirectory() as root:
            access = self.access(root)
            client = SimpleNamespace(archive_scope=AsyncMock(return_value=CodexArchiveResult(1, 0, 1)))
            service = AiAccessService(access, client)
            self.assertEqual(await service.change_roles(1, frozenset({5})), 'updated_with_warning')
            self.assertEqual(access.role_ids, frozenset({5}))

    async def test_status_rejects_unknown_reason(self):
        client = CodexBridgeClient('http://unused', 'unused')
        client._request = AsyncMock(return_value={
            'available': True, 'authenticated': True, 'plan': None,
            'sdk_version': None, 'runtime_version': None, 'web_search': None,
            'thread_count': 0, 'active_requests': 0, 'queued_requests': 0,
            'protocol_version': 2, 'ready': True,
            'reason': 'private detail', 'status_fetched_at': None, 'status_stale': False,
        })
        with self.assertRaises(CodexBridgeError):
            await client.get_runtime_status()

    def message(self):
        author = SimpleNamespace(id=3, bot=False, roles=[SimpleNamespace(id=6)])
        guild = SimpleNamespace(id=1, get_member=lambda user: author)
        channel = SimpleNamespace(id=2, type="text", send=AsyncMock())
        message = SimpleNamespace(
            author=author, guild=guild, channel=channel, content="<@9> question",
            mentions=[SimpleNamespace(id=9)], attachments=[], embeds=[], stickers=[],
            webhook_id=None, reference=None, reply=AsyncMock(),
        )
        return message

    async def test_expired_admission_has_no_late_error_output(self):
        with tempfile.TemporaryDirectory() as root:
            access = self.access(root)
            access.set_roles(1, frozenset({6}))
            client = CodexBridgeClient('http://unused', 'unused')
            client.work_timeout_seconds = 0
            message = self.message()
            await ai_discord.handle_message(
                message, bot_user_id=9, codex=client, access=access,
                member_cache_enabled=True, text_display_enabled=False,
                media_executor=SimpleNamespace(),
            )
            message.reply.assert_not_awaited()

    async def test_each_output_chunk_rechecks_roles(self):
        message = self.message()
        can_send = AsyncMock(side_effect=[True, False])
        result = await ai_discord.send_ai_answer(
            message, "answer" * 500, text_display_enabled=False, can_send=can_send,
        )
        self.assertEqual(result, 'unauthorized')
        message.reply.assert_awaited_once()
        message.channel.send.assert_not_awaited()

    async def test_parent_deletion_includes_children_thread_deletion_does_not(self):
        codex = SimpleNamespace(archive_scope=AsyncMock(return_value=CodexArchiveResult(0, 0, 0)))
        bot = SimpleNamespace(
            calendar=SimpleNamespace(handle_channel_delete=AsyncMock()),
            temp_voice_enabled=False, codex=codex,
        )
        channel = SimpleNamespace(id=2, guild=SimpleNamespace(id=1))
        with patch('src.bot.is_text_channel', return_value=True):
            await HoroBot.on_guild_channel_delete(bot, channel)
        self.assertTrue(codex.archive_scope.call_args.kwargs['include_children'])
        await HoroBot.on_raw_thread_delete(bot, SimpleNamespace(guild_id=1, thread_id=8))
        self.assertFalse(codex.archive_scope.call_args.kwargs['include_children'])

    async def test_setup_hook_does_not_probe_bridge(self):
        codex = CodexBridgeClient('http://unreachable', 'unused')
        bot = SimpleNamespace(
            codex=codex,
            add_view=lambda view: None,
            calendar_controller=SimpleNamespace(persistent_board_view=lambda: None),
            calendar=SimpleNamespace(start=AsyncMock()),
            steam_free_games_enabled=True,
            steam_free_games=SimpleNamespace(start=lambda bot: None),
            tree=SimpleNamespace(sync=AsyncMock()),
        )
        with patch.object(codex, '_request', new=AsyncMock()) as request:
            try:
                await HoroBot.setup_hook(bot)
                request.assert_not_awaited()
                bot.calendar.start.assert_awaited_once_with(bot)
                bot.tree.sync.assert_awaited_once()
            finally:
                await codex.close()


if __name__ == '__main__':
    unittest.main()
