from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
from urllib.parse import urlsplit, urlunsplit

import aiohttp


class AIClientError(RuntimeError):
    """Safe, user-facing-neutral error for 9Router failures."""


@dataclass(frozen=True, slots=True)
class AIToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class AIResponse:
    content: str | None
    tool_calls: tuple[AIToolCall, ...]


@dataclass(frozen=True, slots=True)
class AIRuntimeStatus:
    model_name: str | None
    effort: str | None
    router_available: bool
    router_version: str | None


class AIClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 90.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        parsed_base_url = urlsplit(self.base_url)
        if (
            parsed_base_url.scheme != "http"
            or not parsed_base_url.hostname
            or parsed_base_url.username is not None
            or parsed_base_url.password is not None
            or parsed_base_url.path != "/v1"
            or parsed_base_url.query
            or parsed_base_url.fragment
        ):
            raise ValueError("base_url must be an HTTP URL ending in /v1")
        try:
            parsed_base_url.port
        except ValueError as exc:
            raise ValueError("base_url must be an HTTP URL ending in /v1") from exc
        self.router_root_url = urlunsplit(
            (parsed_base_url.scheme, parsed_base_url.netloc, "", "", "")
        )
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def get_runtime_status(self) -> AIRuntimeStatus:
        if self._session is None or self._session.closed:
            await self.start()

        assert self._session is not None
        timeout = aiohttp.ClientTimeout(total=1.0)

        async def get_json(
            path: str,
            *,
            authenticated: bool = False,
            params: dict[str, str] | None = None,
        ) -> dict[str, object] | None:
            headers = {"Accept": "application/json"}
            if authenticated:
                headers["Authorization"] = f"Bearer {self.api_key}"
            try:
                async with self._session.get(
                    f"{self.router_root_url}{path}",
                    headers=headers,
                    params=params,
                    timeout=timeout,
                ) as response:
                    if response.status != 200:
                        return None
                    data = await response.json()
            except (aiohttp.ClientError, TimeoutError, ValueError):
                return None
            return data if isinstance(data, dict) else None

        models, model_info, health, version = await asyncio.gather(
            get_json("/v1/models", authenticated=True),
            get_json(
                "/v1/models/info",
                authenticated=True,
                params={"id": self.model},
            ),
            get_json("/api/health"),
            get_json("/api/version"),
        )

        model_name = None
        effort = None
        if model_info is not None:
            resolved = model_info.get("resolved")
            if isinstance(resolved, dict):
                raw_name = resolved.get("name")
                raw_effort = resolved.get("effort")
            else:
                raw_name = model_info.get("name")
                raw_effort = model_info.get("effort")
            if isinstance(raw_name, str) and raw_name.strip():
                model_name = raw_name.strip()
            if isinstance(raw_effort, str) and raw_effort.strip():
                effort = raw_effort.strip()

        if model_name is None and models is not None:
            rows = models.get("data")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict) or row.get("id") != self.model:
                        continue
                    raw_name = row.get("name")
                    model_name = (
                        raw_name.strip()
                        if isinstance(raw_name, str) and raw_name.strip()
                        else self.model
                    )
                    break

        router_available = health is not None and health.get("ok") is True
        router_version = None
        if version is not None:
            raw_version = version.get("currentVersion")
            if isinstance(raw_version, str) and raw_version.strip():
                router_version = raw_version.strip()

        return AIRuntimeStatus(
            model_name=model_name,
            effort=effort,
            router_available=router_available,
            router_version=router_version,
        )

    async def chat(
        self,
        messages: list[dict[str, object]],
        tools: list[dict[str, object]] | None = None,
    ) -> AIResponse:
        if self._session is None or self._session.closed:
            await self.start()

        assert self._session is not None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if tools is not None:
            payload["tools"] = tools

        try:
            async with self._session.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if response.status != 200:
                    raise AIClientError(
                        f"9Router request failed with HTTP {response.status}"
                    )
                data = await response.json()
        except AIClientError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise AIClientError("9Router request failed") from exc

        try:
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError
            content = message["content"]
            raw_tool_calls = message.get("tool_calls", [])
        except (KeyError, IndexError, TypeError) as exc:
            raise AIClientError("9Router returned an invalid response") from exc

        if content is not None and not isinstance(content, str):
            raise AIClientError("9Router returned an invalid response")

        if not isinstance(raw_tool_calls, list):
            raise AIClientError("9Router returned an invalid response")

        tool_calls = []
        for raw_tool_call in raw_tool_calls:
            if not isinstance(raw_tool_call, dict):
                raise AIClientError("9Router returned an invalid response")

            tool_call_id = raw_tool_call.get("id")
            tool_call_type = raw_tool_call.get("type")
            function = raw_tool_call.get("function")
            if (
                not isinstance(tool_call_id, str)
                or not tool_call_id.strip()
                or ("type" in raw_tool_call and tool_call_type != "function")
                or not isinstance(function, dict)
            ):
                raise AIClientError("9Router returned an invalid response")

            name = function.get("name")
            arguments = function.get("arguments")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(arguments, str)
            ):
                raise AIClientError("9Router returned an invalid response")

            tool_calls.append(
                AIToolCall(id=tool_call_id, name=name, arguments=arguments)
            )

        stripped_content = content.strip() if content is not None else None
        if not stripped_content and not tool_calls:
            raise AIClientError("9Router returned an empty response")
        return AIResponse(
            content=stripped_content or None,
            tool_calls=tuple(tool_calls),
        )

    async def embed(
        self,
        inputs: list[str],
        *,
        model: str,
        dimensions: int,
    ) -> list[tuple[float, ...]]:
        if not inputs or any(not isinstance(value, str) or not value for value in inputs):
            raise ValueError("inputs must contain non-empty strings")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if type(dimensions) is not int or dimensions <= 0:
            raise ValueError("dimensions must be a positive integer")
        if self._session is None or self._session.closed:
            await self.start()

        assert self._session is not None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model.strip(),
            "input": inputs,
            "dimensions": dimensions,
        }
        try:
            async with self._session.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json=payload,
            ) as response:
                if response.status != 200:
                    raise AIClientError(
                        f"9Router request failed with HTTP {response.status}"
                    )
                data = await response.json()
        except AIClientError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise AIClientError("9Router request failed") from exc

        if not isinstance(data, dict):
            raise AIClientError("9Router returned an invalid response")
        raw_rows = data.get("data")
        if not isinstance(raw_rows, list) or len(raw_rows) != len(inputs):
            raise AIClientError("9Router returned an invalid response")

        ordered: list[tuple[float, ...] | None] = [None] * len(inputs)
        for row in raw_rows:
            if not isinstance(row, dict):
                raise AIClientError("9Router returned an invalid response")
            index = row.get("index")
            vector = row.get("embedding")
            if (
                type(index) is not int
                or not 0 <= index < len(inputs)
                or ordered[index] is not None
                or not isinstance(vector, list)
                or len(vector) != dimensions
            ):
                raise AIClientError("9Router returned an invalid response")

            values: list[float] = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise AIClientError("9Router returned an invalid response")
                number = float(value)
                if not math.isfinite(number):
                    raise AIClientError("9Router returned an invalid response")
                values.append(number)
            ordered[index] = tuple(values)

        if any(vector is None for vector in ordered):
            raise AIClientError("9Router returned an invalid response")
        return [vector for vector in ordered if vector is not None]

    async def web_search(
        self,
        query: str,
        *,
        provider: str,
        search_type: str = "web",
        include_images: bool = False,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "provider": provider,
            "query": query,
            "max_results": 5,
            "search_type": search_type,
        }
        if include_images:
            payload["content_options"] = {"images": True}
        data = await self._web_request(
            "/v1/search",
            payload,
        )
        results = data.get("results")
        if not isinstance(results, list) or any(
            not isinstance(result, dict) for result in results
        ):
            raise AIClientError("9Router returned an invalid response")
        return data

    async def web_fetch(
        self,
        url: str,
        *,
        provider: str,
        content_format: str = "markdown",
        max_characters: int = 15000,
    ) -> dict[str, object]:
        if content_format not in {"markdown", "html"}:
            raise ValueError("content_format must be markdown or html")
        if type(max_characters) is not int or max_characters <= 0:
            raise ValueError("max_characters must be a positive integer")
        data = await self._web_request(
            "/v1/web/fetch",
            {
                "provider": provider,
                "url": url,
                "format": content_format,
                "max_characters": max_characters,
            },
        )
        content = data.get("content")
        if not isinstance(content, dict) or not isinstance(content.get("text"), str):
            raise AIClientError("9Router returned an invalid response")
        return data

    async def _web_request(
        self,
        path: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        if self._session is None or self._session.closed:
            await self.start()

        assert self._session is not None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with self._session.post(
                f"{self.router_root_url}{path}",
                headers=headers,
                json=payload,
            ) as response:
                if response.status != 200:
                    raise AIClientError(
                        f"9Router request failed with HTTP {response.status}"
                    )
                data = await response.json()
        except AIClientError:
            raise
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise AIClientError("9Router request failed") from exc

        if not isinstance(data, dict):
            raise AIClientError("9Router returned an invalid response")
        return data
