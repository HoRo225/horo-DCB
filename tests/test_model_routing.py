import asyncio
from types import SimpleNamespace

from src.ai.runtime import CAPACITY_FALLBACK_MODEL, PRIMARY_MODEL, CodexService


class Store:
    available = True

    def get(self, key):
        return None

    def set(self, key, thread_id):
        pass


class Thread:
    id = "test"

    async def turn(self, inputs):
        return SimpleNamespace(run=self.run)

    async def run(self):
        return SimpleNamespace(final_response="OK", items=())


class Codex:
    def __init__(self, fail_primary):
        self.fail_primary = fail_primary
        self.models = []

    async def thread_start(self, **options):
        self.models.append(options["model"])
        if self.fail_primary and len(self.models) == 1:
            raise RuntimeError("server overloaded")
        return Thread()


async def check():
    for fail_primary, expected in (
        (False, [PRIMARY_MODEL]),
        (True, [PRIMARY_MODEL, CAPACITY_FALLBACK_MODEL]),
    ):
        codex = Codex(fail_primary)
        reply = await CodexService(codex, Store()).chat(
            "guild:1:channel:1:user:1", "ping", (),
        )
        assert codex.models == expected
        assert reply.text == "OK"


if __name__ == "__main__":
    asyncio.run(check())
