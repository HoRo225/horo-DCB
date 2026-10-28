from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import aiohttp

from src.codex.model_settings import DEFAULT_MODEL_SETTINGS, parse_model_list, resolve_model_settings
from src.codex.rpc import AppServerClient
from src.config import CODEX_TOKEN_FILE, CODEX_URL, codex_token


async def rejected_status(url: str) -> int | None:
    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(url, headers={"Authorization": "Bearer wrong-token"}):
                return None
        except aiohttp.WSServerHandshakeError as exc:
            return exc.status


async def smoke(url: str, token: str) -> list[str]:
    client = AppServerClient(url, token, client_version="smoke")
    client.start()
    try:
        if not await client.wait_ready(30):
            return ["initialize：30 秒內未完成握手"]
        print(f"initialize：{client.user_agent}")
        account = (await client.request("account/read", {})).get("account")
        if isinstance(account, dict):
            print(f"account/read：已登入（{account.get('type')}，{account.get('planType')}）")
        else:
            print("account/read：未登入")
        response = await client.request("model/list", {"includeHidden": False})
        catalog = parse_model_list(response.get("data"))
        default = next((item["model"] for item in response["data"] if item.get("isDefault")), None)
        print(f"model/list：{len(catalog)} 個模型，上游預設 {default}")
        for info in catalog:
            print(f"  {info.model}：預設 {info.default_effort}，可選 {'、'.join(info.supported_efforts)}")
        try:
            resolved = resolve_model_settings(DEFAULT_MODEL_SETTINGS, catalog)
            print(f"bot 預設模型可用：{resolved.primary.model}（{resolved.primary.effort}）"
                  f"，備援 {resolved.fallback.model if resolved.fallback else '無'}")
        except ValueError as exc:
            print(f"警告：bot 預設模型無法使用（{exc}）")
    finally:
        await client.close()
    status = await rejected_status(url)
    print(f"錯誤 token：HTTP {status}")
    return [] if status == 401 else [f"錯誤 token 應回 HTTP 401，實際為 {status}"]


def main() -> None:
    parser = argparse.ArgumentParser(description="對 codex app-server 做 initialize、account/read、model/list 冒煙測試。")
    parser.add_argument("--url", default=CODEX_URL)
    parser.add_argument("--token-file", type=Path, default=CODEX_TOKEN_FILE)
    args = parser.parse_args()
    failures = asyncio.run(smoke(args.url, codex_token(args.token_file)))
    for failure in failures:
        print(f"失敗：{failure}", file=sys.stderr)
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
