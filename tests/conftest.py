import pytest

from horo_dcb import codex as codex_module
from horo_dcb.codex import CodexClient
from tests.support.app_server import FakeAppServer


@pytest.fixture
async def app_server():
    server = FakeAppServer()
    server.url = await server.start()
    yield server
    await server.close()


@pytest.fixture
async def codex(app_server, monkeypatch):
    monkeypatch.setattr(codex_module, "RECONNECT_FIRST", 0.01)
    monkeypatch.setattr(codex_module, "RECONNECT_MAX", 0.01)
    monkeypatch.setattr(codex_module, "REQUEST_TIMEOUT", 2)
    monkeypatch.setattr(codex_module, "TERMINAL_POLL", 0.05)
    client = CodexClient(socket_path=None, url=app_server.url)
    await client.start()
    yield client
    await client.close()
