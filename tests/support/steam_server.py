import json

from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.support.steam_items import missing

LOGO = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{app}/0000/capsule_sm_120.jpg?t=1"


class FakeSteam:
    def __init__(self) -> None:
        self.items: dict[int, dict] = {}
        self.listed: list[int] = []
        self.statuses: dict[str, int] = {}
        self.bodies: dict[str, object] = {}
        self.searches: list[dict] = []
        self.lookups: list[dict] = []
        self.server: TestServer | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/search", self.search)
        app.router.add_get("/items", self.get_items)
        self.server = TestServer(app)
        await self.server.start_server()

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()

    def url(self, path: str) -> str:
        assert self.server is not None
        return str(self.server.make_url(path))

    def offer(self, item: dict, *, listed: bool = True) -> None:
        self.items[item["appid"]] = item
        if listed and item["appid"] not in self.listed:
            self.listed.append(item["appid"])

    def withdraw(self, app: int) -> None:
        self.items.pop(app, None)
        self.listed = [value for value in self.listed if value != app]

    def looked_up(self) -> list[list[int]]:
        return [[entry["appid"] for entry in query["ids"]] for query in self.lookups]

    def reply(self, name: str, body: object) -> web.Response:
        if name in self.statuses:
            return web.Response(status=self.statuses[name])
        return web.json_response(self.bodies.get(name, body))

    async def search(self, request: web.Request) -> web.Response:
        self.searches.append(dict(request.query))
        return self.reply("search", {"desc": "", "items": [{"name": self.items[app]["name"] if app in self.items else "", "logo": LOGO.format(app=app)} for app in self.listed]})

    async def get_items(self, request: web.Request) -> web.Response:
        query = json.loads(request.query["input_json"])
        self.lookups.append(query)
        return self.reply("items", {"response": {"store_items": [self.items.get(entry["appid"]) or missing(entry["appid"]) for entry in query["ids"]]}})
