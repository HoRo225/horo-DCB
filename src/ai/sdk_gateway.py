from __future__ import annotations

from typing import Any

from pydantic import RootModel


async def read_rate_limits_payload(codex: Any) -> dict[str, Any]:
    response = await codex._client.request(
        "account/rateLimits/read",
        None,
        response_model=RootModel[dict[str, Any]],
    )
    return response.root
