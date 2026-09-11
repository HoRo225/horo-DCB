import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.admin.panel import AdminPanelView
from src.ai.protocol import CodexRuntimeStatus


async def settle():
    # Let runnable tasks reach their event gates; never wait for wall-clock IO.
    for _ in range(12):
        await asyncio.sleep(0)


async def stop_tasks(tasks):
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 1)


class ControlledCodex:
    metadata = SimpleNamespace(serverInfo=SimpleNamespace(version="0.147.0"))

    def __init__(self):
        self.gates = {}
        self.entered = {phase: asyncio.Event() for phase in (
            "account", "start", "resume", "turn", "run", "interrupt", "archive",
        )}
        self.started = []
        self.resumed = []
        self.runs = []
        self.interrupted = []
        self.archived = []
        self.run_error = None
        self.interrupt_error = None

    async def pause(self, phase):
        self.entered[phase].set()
        gate = self.gates.get(phase)
        if gate is not None:
            await gate.wait()

    async def account(self):
        await self.pause("account")
        return SimpleNamespace(account=SimpleNamespace(root=SimpleNamespace(
            plan_type=SimpleNamespace(value="free"),
        )))

    async def thread_start(self, **options):
        self.started.append(options)
        thread_id = f"thread-{len(self.started)}"
        await self.pause("start")
        return ControlledThread(self, thread_id)

    async def thread_resume(self, thread_id, **options):
        self.resumed.append((thread_id, options))
        await self.pause("resume")
        return ControlledThread(self, thread_id)

    async def thread_archive(self, thread_id):
        self.archived.append(thread_id)
        await self.pause("archive")


class ControlledThread:
    def __init__(self, codex, thread_id):
        self.codex = codex
        self.id = thread_id
        self.run_waiter = None
        self.interrupted = False

    async def turn(self, inputs):
        await self.codex.pause("turn")
        return self

    async def run(self):
        self.codex.runs.append(self.id)
        self.run_waiter = asyncio.create_task(self.codex.pause("run"))
        try:
            await self.run_waiter
        except asyncio.CancelledError:
            if not self.interrupted:
                raise
        if self.codex.run_error is not None:
            raise self.codex.run_error
        return SimpleNamespace(final_response="answer")

    async def interrupt(self):
        self.codex.interrupted.append(self.id)
        await self.codex.pause("interrupt")
        if self.codex.interrupt_error is not None:
            raise self.codex.interrupt_error
        # Successful synthetic interrupt terminates this turn only, like the SDK event.
        self.interrupted = True
        if self.run_waiter is not None:
            self.run_waiter.cancel()


class StatusService:
    def __init__(self):
        self.status_data = {
            "available": True, "authenticated": True, "plan": "free",
            "sdk_version": "0.147.0", "runtime_version": "0.147.0",
            "web_search": "live", "thread_count": 0,
        }
        self.error = None

    async def status(self):
        return self.status_data

    async def chat(self, *_args):
        if self.error is not None:
            raise self.error
        return "answer"


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


def make_admin(access, client):
    return AdminPanelView(
        user_id=30, guild_id=10, codex_client=client, codex_access=access,
        codex_status=CodexRuntimeStatus(True, True, "free", "0.147.0", "0.147.0", "live", 0),
        temp_voice=SimpleNamespace(get_guild_status=lambda _guild: SimpleNamespace(
            state_available=True, parent_channel_id=1, child_count=0,
        )),
        steam_free_games=SimpleNamespace(get_guild_status=lambda _guild: SimpleNamespace(
            state_available=True, channel_id=2, active_app_count=0,
            poll_interval_seconds=900, role_ids=(),
        )),
        temp_voice_enabled=False, steam_free_games_enabled=False,
    )


def interaction():
    return SimpleNamespace(
        response=SimpleNamespace(defer=AsyncMock()),
        guild=SimpleNamespace(id=10), user=SimpleNamespace(id=30, roles=[]),
        edit_original_response=AsyncMock(),
    )


def role(role_id):
    return SimpleNamespace(id=role_id, guild=SimpleNamespace(id=10), is_default=lambda: False)
