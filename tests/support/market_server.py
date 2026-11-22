import json
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "stocks"
MISSING = {"tv": "-", "s": "-", "c": "", "z": "-"}
FILES = {
    ("TaiwanStockInfo", None): "info.json",
    ("TaiwanStockPrice", "2330"): "price_2330.json",
    ("TaiwanStockPrice", "2888"): "price_2888.json",
    ("TaiwanStockDividendResult", "2330"): "div_2330.json",
    ("TaiwanStockDividendResult", "00878"): "div_00878.json",
    ("TaiwanStockSplitPrice", "0050"): "split_0050.json",
}


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def success(rows: list) -> dict:
    return {"msg": "success", "status": 200, "data": rows}


class FakeMarket:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {f"{row['ex']}_{row['c']}": row for row in fixture("mis.json")["msgArray"] if row.get("c")}
        self.bodies: dict[tuple[str, str | None], object] = {key: fixture(name) for key, name in FILES.items()}
        self.boards: dict[str, object] = {"twse": fixture("twse_day_all.json"), "tpex": fixture("tpex_quotes.json")}
        self.statuses: dict[str, int] = {}
        self.board_requests: list[str] = []
        self.quote_requests: list[list[str]] = []
        self.finmind_requests: list[dict[str, str]] = []
        self.server: TestServer | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/quote", self.quote)
        app.router.add_get("/finmind", self.finmind)
        app.router.add_get("/twse", self.board)
        app.router.add_get("/tpex", self.board)
        self.server = TestServer(app)
        await self.server.start_server()

    async def close(self) -> None:
        if self.server is not None:
            await self.server.close()

    def url(self, path: str) -> str:
        assert self.server is not None
        return str(self.server.make_url(path))

    def finmind_count(self, dataset: str) -> int:
        return sum(request["dataset"] == dataset for request in self.finmind_requests)

    async def quote(self, request: web.Request) -> web.Response:
        channels = request.query["ex_ch"].split("|")
        self.quote_requests.append(channels)
        if "quote" in self.statuses:
            return web.Response(status=self.statuses["quote"])
        return web.json_response({"msgArray": [self.rows.get(channel.removesuffix(".tw"), MISSING) for channel in channels], "rtcode": "0000", "rtmessage": "OK"})

    async def finmind(self, request: web.Request) -> web.Response:
        query = dict(request.query)
        self.finmind_requests.append(query)
        dataset = query["dataset"]
        if dataset in self.statuses:
            return web.Response(status=self.statuses[dataset])
        return web.json_response(self.bodies.get((dataset, query.get("data_id")), success([])))

    async def board(self, request: web.Request) -> web.Response:
        name = request.path.strip("/")
        self.board_requests.append(name)
        if name in self.statuses:
            return web.Response(status=self.statuses[name])
        return web.json_response(self.boards[name])
