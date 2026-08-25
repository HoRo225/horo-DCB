from __future__ import annotations

from dataclasses import dataclass, field
import html
from html.parser import HTMLParser
import ipaddress
import json
import logging
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

from src.calendar_events import CALENDAR_TZ, CalendarDraft, CalendarManager, CalendarScope, CalendarUserError
from src.steam_free_games import SteamFreeGamesNotifier, SteamOffer

if TYPE_CHECKING:
    from src.ai_client import AIClient
    from src.semantic_memory import MemoryScope, SemanticMemory


@dataclass(frozen=True, slots=True)
class ToolContext:
    guild_name: str | None
    channel_name: str
    channel_type: str


@dataclass(slots=True)
class ResearchSource:
    source_id: int
    title: str
    url: str
    snippet: str
    published_at: str | None


@dataclass(frozen=True, slots=True)
class ResearchImage:
    url: str
    description: str


@dataclass(slots=True)
class ResearchContext:
    sources: list[ResearchSource] = field(default_factory=list)
    allowed_fetch_urls: set[str] = field(default_factory=set)
    reply_images: list[ResearchImage] = field(default_factory=list)
    image_search_requested: bool = False
    search_calls: int = 0
    fetch_calls: int = 0
    memory_search_calls: int = 0
    calendar_event_refs: dict[int, int] = field(default_factory=dict)
    calendar_draft: CalendarDraft | None = None


_EMPTY_PARAMETERS = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_TOOL_NAMES = {
    "get_current_channel_info",
    "get_steam_free_games",
    "web_search",
    "web_fetch",
    "search_channel_memory",
    "calendar_get_events",
    "calendar_propose_create",
    "calendar_propose_edit",
}
_MAX_WEB_SEARCH_CALLS = 2
_MAX_WEB_FETCH_CALLS = 2
_MAX_MEMORY_SEARCH_CALLS = 2
_MAX_FETCH_CHARACTERS = 15000
_MAX_IMAGE_FETCH_CHARACTERS = 50000
_MAX_IMAGE_FALLBACK_FETCHES = 1
_IMAGE_META_KEYS = frozenset(
    {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}
)
_IMAGE_JSON_KEYS = frozenset({"image", "thumbnailurl", "contenturl"})
_LOW_QUALITY_IMAGE_ALT_TERMS = (
    "logo",
    "icon",
    "avatar",
    "profile picture",
    "highlight story",
    "placeholder",
    "sprite",
)
_MARKDOWN_IMAGE_PATTERN = re.compile(
    r"!\[([^\]]*)\]\((https?://[^\s<>\)]+)\)",
    re.IGNORECASE,
)
_SOCIAL_PROFILE_HOSTS = frozenset(
    {
        "instagram.com",
        "facebook.com",
        "threads.net",
        "x.com",
        "twitter.com",
        "tiktok.com",
        "youtube.com",
    }
)
_IMAGE_PATH_PATTERN = re.compile(r"\.(?:jpe?g|png|webp)$", re.IGNORECASE)


def _json_image_urls(value: object, *, image_context: bool = False) -> list[str]:
    if isinstance(value, str):
        return [value] if image_context else []
    if isinstance(value, list):
        urls: list[str] = []
        for item in value:
            urls.extend(_json_image_urls(item, image_context=image_context))
        return urls
    if not isinstance(value, dict):
        return []

    urls: list[str] = []
    if image_context:
        direct_url = value.get("url")
        if isinstance(direct_url, str):
            urls.append(direct_url)
    for key, item in value.items():
        normalized_key = str(key).lower()
        if normalized_key in _IMAGE_JSON_KEYS:
            urls.extend(_json_image_urls(item, image_context=True))
        elif isinstance(item, (dict, list)):
            urls.extend(_json_image_urls(item))
    return urls


class _PageImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.preferred_urls: list[str] = []
        self.other_urls: list[str] = []
        self.descriptions: dict[str, str] = {}
        self._json_ld_parts: list[str] | None = None

    @staticmethod
    def _attribute_map(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {
            key.lower(): value
            for key, value in attrs
            if isinstance(key, str) and isinstance(value, str)
        }

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = self._attribute_map(attrs)
        normalized_tag = tag.lower()
        if normalized_tag == "meta":
            key = (attributes.get("property") or attributes.get("name") or "").lower()
            content = attributes.get("content")
            if key in _IMAGE_META_KEYS and content:
                self.preferred_urls.append(content)
            return
        if normalized_tag == "link":
            rel = set(attributes.get("rel", "").lower().split())
            href = attributes.get("href")
            if "image_src" in rel and href:
                self.preferred_urls.append(href)
            return
        if normalized_tag == "script":
            if attributes.get("type", "").lower() == "application/ld+json":
                self._json_ld_parts = []
            return
        if normalized_tag not in {"img", "source"}:
            return

        description = attributes.get("alt", "")
        for key in ("src", "data-src", "data-lazy-src"):
            value = attributes.get(key)
            if value:
                self.other_urls.append(value)
                if description:
                    self.descriptions[value] = description
        for item in attributes.get("srcset", "").split(","):
            candidate = item.strip().split(" ", 1)[0]
            if candidate:
                self.other_urls.append(candidate)
                if description:
                    self.descriptions[candidate] = description

    def handle_data(self, data: str) -> None:
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "script" or self._json_ld_parts is None:
            return
        raw = "".join(self._json_ld_parts).strip()
        self._json_ld_parts = None
        if not raw:
            return
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return
        self.preferred_urls.extend(_json_image_urls(payload))


class AgentTools:
    def __init__(
        self,
        steam_notifier: SteamFreeGamesNotifier,
        ai_client: AIClient,
        semantic_memory: SemanticMemory | None = None,
        calendar: CalendarManager | None = None,
        *,
        search_provider: str,
        image_search_provider: str | None = None,
        fetch_provider: str,
    ) -> None:
        self._steam_notifier = steam_notifier
        self._ai_client = ai_client
        self._semantic_memory = semantic_memory
        self._calendar = calendar
        self._search_provider = search_provider
        self._image_search_provider = image_search_provider or search_provider
        self._fetch_provider = fetch_provider

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return self.schemas_for(None)

    def schemas_for(self, calendar_scope: CalendarScope | None) -> list[dict[str, Any]]:
        schemas = [
            {
                "type": "function",
                "function": {
                    "name": "get_current_channel_info",
                    "description": "Get the current Discord channel context.",
                    "parameters": {**_EMPTY_PARAMETERS, "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_steam_free_games",
                    "description": "Get current qualifying Steam free-game offers.",
                    "parameters": {**_EMPTY_PARAMETERS, "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "Search the web for current information.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 300,
                            },
                            "search_type": {
                                "type": "string",
                                "enum": ["web", "news"],
                            },
                            "include_images": {"type": "boolean"},
                        },
                        "required": ["query", "search_type"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_fetch",
                    "description": "Fetch an allowed page returned by web search.",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                },
            },
        ]
        if self._semantic_memory is not None:
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": "search_channel_memory",
                        "description": "Search long-term semantic memory for the current Discord channel or thread.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 300,
                                }
                            },
                            "required": ["query"],
                            "additionalProperties": False,
                        },
                    },
                }
            )
        if (
            self._calendar is not None
            and calendar_scope is not None
            and self._calendar.has_binding(calendar_scope.guild_id)
        ):
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": "calendar_get_events",
                        "description": "Get upcoming Discord calendar events for this server. Use this before identifying an event to edit.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 100,
                                }
                            },
                            "additionalProperties": False,
                        },
                    },
                }
            )
            if calendar_scope.can_manage_events:
                schemas.extend(
                    [
                        {
                            "type": "function",
                            "function": {
                                "name": "calendar_propose_create",
                                "description": "Prepare a calendar event draft for human confirmation. This never creates the Discord event by itself.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "minLength": 1, "maxLength": 100},
                                        "start": {"type": "string", "minLength": 16, "maxLength": 16},
                                        "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
                                        "location": {"type": "string", "minLength": 1, "maxLength": 100},
                                        "description": {"type": "string", "maxLength": 1000},
                                    },
                                    "required": ["name", "start", "duration_minutes", "location"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        {
                            "type": "function",
                            "function": {
                                "name": "calendar_propose_edit",
                                "description": "Prepare edits to an event_ref returned by calendar_get_events for human confirmation. This never edits Discord by itself.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "event_ref": {"type": "integer", "minimum": 1, "maximum": 25},
                                        "name": {"type": "string", "minLength": 1, "maxLength": 100},
                                        "start": {"type": "string", "minLength": 16, "maxLength": 16},
                                        "duration_minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
                                        "location": {"type": "string", "minLength": 1, "maxLength": 100},
                                        "description": {"type": "string", "maxLength": 1000},
                                    },
                                    "required": ["event_ref"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                    ]
                )
        return schemas

    @staticmethod
    def _json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _serialize_offer(offer: SteamOffer) -> dict[str, Any]:
        return {
            "name": offer.name,
            "old_price": offer.old_price,
            "description": offer.description[:300],
            "developers": list(offer.developers[:5]),
            "store_url": offer.store_url,
        }

    @staticmethod
    def _bounded_text(value: object, limit: int) -> str:
        return value[:limit] if isinstance(value, str) else ""

    @staticmethod
    def _is_safe_web_url(url: object) -> bool:
        if (
            not isinstance(url, str)
            or not url
            or len(url) > 2048
            or url != url.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
        ):
            return False

        try:
            parsed = urlsplit(url)
            parsed.port
        except ValueError:
            return False

        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False

        hostname = parsed.hostname.rstrip(".").lower()
        if (
            not hostname
            or hostname in {"localhost", "local", "internal"}
            or hostname.endswith((".localhost", ".local", ".internal"))
        ):
            return False

        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            # ponytail: DNS and redirects remain the fetch provider's responsibility.
            return ":" not in hostname
        return address.is_global

    @staticmethod
    def _is_social_profile_url(url: object) -> bool:
        if not isinstance(url, str):
            return False
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False

        hostname = (parsed.hostname or "").rstrip(".").lower()
        for prefix in ("www.", "m."):
            if hostname.startswith(prefix):
                hostname = hostname[len(prefix) :]
                break
        if hostname not in _SOCIAL_PROFILE_HOSTS:
            return False

        segments = [segment.lower() for segment in parsed.path.split("/") if segment]
        if hostname in {
            "instagram.com",
            "threads.net",
            "x.com",
            "twitter.com",
            "tiktok.com",
        }:
            return len(segments) <= 1
        if hostname == "youtube.com":
            return not segments or segments[0] not in {"watch", "shorts", "live"}
        return not any(
            segment in {
                "posts",
                "photos",
                "photo.php",
                "videos",
                "video",
                "reel",
                "reels",
                "watch",
            }
            for segment in segments
        )

    @staticmethod
    def _clean_image_description(value: object) -> str:
        if not isinstance(value, str):
            return ""
        description = " ".join(html.unescape(value).split())[:200]
        lowered = description.casefold()
        if not description or any(
            term in lowered for term in _LOW_QUALITY_IMAGE_ALT_TERMS
        ):
            return ""
        return description

    @classmethod
    def _normalize_image_url(
        cls,
        candidate: object,
        *,
        base_url: str | None = None,
    ) -> str | None:
        if not isinstance(candidate, str):
            return None
        image_url = html.unescape(candidate).strip().strip("\"'")
        image_url = image_url.replace("\\u0026", "&").replace("\\/", "/")
        image_url = image_url.rstrip("\\),.;]}")
        if base_url:
            image_url = urljoin(base_url, image_url)
        try:
            path = urlsplit(image_url).path
        except ValueError:
            return None
        if (
            not _IMAGE_PATH_PATTERN.search(path)
            or not cls._is_safe_web_url(image_url)
        ):
            return None
        return image_url

    @classmethod
    def _extract_safe_image_candidates(
        cls,
        value: object,
        *,
        base_url: str | None = None,
    ) -> list[tuple[str, str]]:
        if not isinstance(value, str) or not value:
            return []

        normalized = html.unescape(value)
        normalized = normalized.replace("\\\\u0026", "&")
        normalized = normalized.replace("\\u0026", "&")
        normalized = normalized.replace("\\/", "/")

        parser = _PageImageParser()
        try:
            parser.feed(normalized)
            parser.close()
        except (TypeError, ValueError):
            parser = _PageImageParser()

        rejected_urls: set[str] = set()
        markdown_candidates: list[tuple[str, str]] = []
        for match in _MARKDOWN_IMAGE_PATTERN.finditer(normalized):
            image_url = cls._normalize_image_url(match.group(2), base_url=base_url)
            if image_url is None:
                continue
            description = cls._clean_image_description(match.group(1))
            if not description:
                rejected_urls.add(image_url)
                continue
            markdown_candidates.append((image_url, description))

        tag_candidates: list[tuple[str, str]] = []
        for candidate in parser.other_urls:
            image_url = cls._normalize_image_url(candidate, base_url=base_url)
            if image_url is None:
                continue
            raw_description = parser.descriptions.get(candidate, "")
            description = cls._clean_image_description(raw_description)
            if raw_description and not description:
                rejected_urls.add(image_url)
                continue
            tag_candidates.append((image_url, description))

        candidates: list[tuple[object, str]] = [
            *((candidate, "") for candidate in parser.preferred_urls),
            *markdown_candidates,
            *tag_candidates,
        ]

        images: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        for candidate, description in candidates:
            image_url = cls._normalize_image_url(candidate, base_url=base_url)
            if (
                image_url is None
                or image_url in rejected_urls
                or image_url in seen_urls
            ):
                continue
            seen_urls.add(image_url)
            images.append((image_url, description))
            if len(images) >= 4:
                break
        return images

    @classmethod
    def _extract_safe_image_urls(
        cls,
        value: object,
        *,
        base_url: str | None = None,
    ) -> list[str]:
        return [
            image_url
            for image_url, _ in cls._extract_safe_image_candidates(
                value,
                base_url=base_url,
            )
        ]

    async def _collect_fallback_images(
        self,
        sources: list[ResearchSource],
        research_context: ResearchContext,
    ) -> None:
        fallback_fetches = 0
        for source in sources:
            if self._is_social_profile_url(source.url):
                continue
            if (
                len(research_context.reply_images) >= 4
                or research_context.fetch_calls >= _MAX_WEB_FETCH_CALLS
                or fallback_fetches >= _MAX_IMAGE_FALLBACK_FETCHES
            ):
                break
            fallback_fetches += 1
            research_context.fetch_calls += 1
            try:
                response = await self._ai_client.web_fetch(
                    source.url,
                    provider=self._fetch_provider,
                    content_format="html",
                    max_characters=_MAX_IMAGE_FETCH_CHARACTERS,
                )
                content_data = response.get("content")
                if not isinstance(content_data, dict):
                    raise TypeError
                page_text = content_data.get("text")
                if not isinstance(page_text, str):
                    raise TypeError
            except Exception:
                logging.info("Image fallback page fetch failed.")
                continue

            for image_url, candidate_description in self._extract_safe_image_candidates(
                page_text,
                base_url=source.url,
            ):
                if any(
                    existing.url == image_url
                    for existing in research_context.reply_images
                ):
                    continue
                description = candidate_description or source.title or source.url[:200]
                research_context.reply_images.append(
                    ResearchImage(image_url, description)
                )
                if len(research_context.reply_images) >= 4:
                    break

    @staticmethod
    def _serialize_source(source: ResearchSource) -> dict[str, Any]:
        return {
            "source_id": source.source_id,
            "title": source.title,
            "url": source.url,
            "snippet": source.snippet,
            "published_at": source.published_at,
        }

    async def _execute_web_search(
        self,
        arguments: dict[str, Any],
        research_context: ResearchContext,
    ) -> str:
        if not {"query", "search_type"} <= set(arguments) or not set(arguments) <= {
            "query",
            "search_type",
            "include_images",
        }:
            return self._json({"ok": False, "error": "invalid_arguments"})

        query = arguments.get("query")
        search_type = arguments.get("search_type")
        include_images = arguments.get("include_images", False)
        if (
            not isinstance(query, str)
            or not 1 <= len(query.strip()) <= 300
            or not isinstance(search_type, str)
            or search_type not in {"web", "news"}
            or type(include_images) is not bool
        ):
            return self._json({"ok": False, "error": "invalid_arguments"})
        query = query.strip()
        if include_images:
            research_context.image_search_requested = True

        if research_context.search_calls >= _MAX_WEB_SEARCH_CALLS:
            return self._json({"ok": False, "error": "search_limit_reached"})
        research_context.search_calls += 1

        selected_provider = (
            self._image_search_provider if include_images else self._search_provider
        )
        providers = [selected_provider]
        if include_images and selected_provider != self._search_provider:
            providers.append(self._search_provider)

        response: dict[str, object] | None = None
        raw_results: list[object] | None = None
        for index, provider in enumerate(providers):
            try:
                candidate = await self._ai_client.web_search(
                    query,
                    provider=provider,
                    search_type=search_type,
                    include_images=include_images,
                )
                candidate_results = candidate.get("results")
                if not isinstance(candidate_results, list):
                    raise TypeError
            except Exception:
                if index + 1 < len(providers):
                    logging.info(
                        "Image search provider unavailable; retrying general search provider."
                    )
                    continue
                logging.error("Web search tool request failed.")
                return self._json({"ok": False, "error": "web_search_unavailable"})
            response = candidate
            raw_results = candidate_results
            break

        if response is None or raw_results is None:
            return self._json({"ok": False, "error": "web_search_unavailable"})

        new_sources = []
        new_source_objects: list[ResearchSource] = []
        for result in raw_results[:5]:
            if not isinstance(result, dict):
                continue
            url = result.get("url")
            if not self._is_safe_web_url(url):
                continue

            published_at = result.get("published_at")
            source = ResearchSource(
                source_id=len(research_context.sources) + 1,
                title=self._bounded_text(result.get("title"), 200),
                url=url,
                snippet=self._bounded_text(result.get("snippet"), 1000),
                published_at=(
                    self._bounded_text(published_at, 100)
                    if isinstance(published_at, str)
                    else None
                ),
            )
            research_context.sources.append(source)
            research_context.allowed_fetch_urls.add(url)
            new_source_objects.append(source)
            new_sources.append(self._serialize_source(source))

            if include_images and len(research_context.reply_images) < 4:
                image_url = result.get("metadata", {}).get("image_url") if isinstance(result.get("metadata"), dict) else None
                if self._is_safe_web_url(image_url) and all(
                    image.url != image_url for image in research_context.reply_images
                ):
                    research_context.reply_images.append(
                        ResearchImage(image_url, source.title)
                    )

        if include_images and isinstance(response.get("images"), list):
            for image in response["images"]:
                if len(research_context.reply_images) >= 4:
                    break
                image_url = image if isinstance(image, str) else (
                    image.get("url") if isinstance(image, dict) else None
                )
                if not self._is_safe_web_url(image_url) or any(
                    existing.url == image_url
                    for existing in research_context.reply_images
                ):
                    continue
                provider_description = (
                    image.get("description") if isinstance(image, dict) else None
                )
                description = self._bounded_text(provider_description, 200) or query[:200]
                research_context.reply_images.append(
                    ResearchImage(image_url, description)
                )

        if include_images and not research_context.reply_images:
            await self._collect_fallback_images(
                new_source_objects,
                research_context,
            )

        payload = {"ok": True, "total": len(new_sources), "sources": new_sources}
        if include_images:
            payload["image_count"] = len(research_context.reply_images)
        return self._json(payload)

    async def _execute_web_fetch(
        self,
        arguments: dict[str, Any],
        research_context: ResearchContext,
    ) -> str:
        if set(arguments) != {"url"} or not isinstance(arguments.get("url"), str):
            return self._json({"ok": False, "error": "invalid_arguments"})

        url = arguments["url"]
        source = next(
            (source for source in research_context.sources if source.url == url),
            None,
        )
        if (
            url not in research_context.allowed_fetch_urls
            or source is None
            or not self._is_safe_web_url(url)
        ):
            return self._json({"ok": False, "error": "url_not_allowed"})

        if research_context.fetch_calls >= _MAX_WEB_FETCH_CALLS:
            return self._json({"ok": False, "error": "fetch_limit_reached"})
        research_context.fetch_calls += 1

        try:
            response = await self._ai_client.web_fetch(
                url,
                provider=self._fetch_provider,
            )
            content_data = response.get("content")
            if not isinstance(content_data, dict):
                raise TypeError
            content = content_data.get("text")
            if not isinstance(content, str):
                raise TypeError
        except Exception:
            logging.error("Web fetch tool request failed.")
            return self._json({"ok": False, "error": "web_fetch_unavailable"})

        response_title = response.get("title")
        title = (
            self._bounded_text(response_title, 200)
            if isinstance(response_title, str)
            else source.title
        )
        truncated = len(content) > _MAX_FETCH_CHARACTERS
        return self._json(
            {
                "ok": True,
                "source_id": source.source_id,
                "title": title,
                "url": source.url,
                "content": content[:_MAX_FETCH_CHARACTERS],
                "truncated": truncated,
            }
        )

    async def _execute_memory_search(
        self,
        arguments: dict[str, Any],
        research_context: ResearchContext,
        memory_scope: MemoryScope | None,
    ) -> str:
        if set(arguments) != {"query"}:
            return self._json({"ok": False, "error": "invalid_arguments"})
        query = arguments.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
            return self._json({"ok": False, "error": "invalid_arguments"})
        if (
            memory_scope is None
            or self._semantic_memory is None
            or not self._semantic_memory.available
        ):
            return self._json({"ok": False, "error": "memory_unavailable"})
        if research_context.memory_search_calls >= _MAX_MEMORY_SEARCH_CALLS:
            return self._json({"ok": False, "error": "memory_search_limit_reached"})
        research_context.memory_search_calls += 1
        try:
            memories = await self._semantic_memory.search(query.strip(), memory_scope)
        except Exception:
            logging.error("Semantic memory search failed.")
            return self._json({"ok": False, "error": "memory_unavailable"})
        return self._json({"ok": True, "total": len(memories), "memories": memories})

    async def _execute_calendar_get_events(
        self,
        arguments: dict[str, Any],
        research_context: ResearchContext,
        calendar_scope: CalendarScope | None,
    ) -> str:
        if self._calendar is None or calendar_scope is None:
            return self._json({"ok": False, "error": "calendar_unavailable"})
        if set(arguments) - {"query"}:
            return self._json({"ok": False, "error": "invalid_arguments"})
        query = arguments.get("query")
        if query is not None and (
            not isinstance(query, str) or not 1 <= len(query.strip()) <= 100
        ):
            return self._json({"ok": False, "error": "invalid_arguments"})
        try:
            events = await self._calendar.get_events_for_ai(
                calendar_scope,
                query.strip() if isinstance(query, str) else None,
            )
        except CalendarUserError:
            return self._json({"ok": False, "error": "calendar_unavailable"})
        research_context.calendar_event_refs.clear()
        serialized = []
        for event_ref, event in enumerate(events, start=1):
            research_context.calendar_event_refs[event_ref] = event.id
            start_time = event.start_time.astimezone(CALENDAR_TZ)
            end_time = event.end_time.astimezone(CALENDAR_TZ) if event.end_time else None
            serialized.append(
                {
                    "event_ref": event_ref,
                    "name": self._bounded_text(event.name, 100),
                    "start": start_time.strftime("%Y-%m-%d %H:%M"),
                    "end": end_time.strftime("%Y-%m-%d %H:%M") if end_time else None,
                    "location": self._bounded_text(event.location, 100),
                    "entity_type": event.entity_type.name,
                    "status": event.status.name,
                }
            )
        return self._json({"ok": True, "total": len(serialized), "events": serialized})

    def _execute_calendar_propose_create(
        self,
        arguments: dict[str, Any],
        research_context: ResearchContext,
        calendar_scope: CalendarScope | None,
    ) -> str:
        if self._calendar is None or calendar_scope is None:
            return self._json({"ok": False, "error": "calendar_unavailable"})
        try:
            draft = self._calendar.build_create_draft(calendar_scope, arguments)
        except CalendarUserError:
            return self._json({"ok": False, "error": "invalid_arguments"})
        research_context.calendar_draft = draft
        return self._json(
            {
                "ok": True,
                "requires_confirmation": True,
                "draft": draft.to_ai_payload(),
            }
        )

    async def _execute_calendar_propose_edit(
        self,
        arguments: dict[str, Any],
        research_context: ResearchContext,
        calendar_scope: CalendarScope | None,
    ) -> str:
        if self._calendar is None or calendar_scope is None:
            return self._json({"ok": False, "error": "calendar_unavailable"})
        event_ref = arguments.get("event_ref")
        if type(event_ref) is not int or event_ref <= 0:
            return self._json({"ok": False, "error": "invalid_arguments"})
        event_id = research_context.calendar_event_refs.get(event_ref)
        if event_id is None:
            return self._json({"ok": False, "error": "event_ref_not_allowed"})
        changes = {key: value for key, value in arguments.items() if key != "event_ref"}
        if not changes:
            return self._json({"ok": False, "error": "invalid_arguments"})
        try:
            draft = await self._calendar.build_edit_draft(
                calendar_scope,
                event_id,
                changes,
            )
        except CalendarUserError:
            return self._json({"ok": False, "error": "calendar_event_unavailable"})
        research_context.calendar_draft = draft
        return self._json(
            {
                "ok": True,
                "requires_confirmation": True,
                "draft": draft.to_ai_payload(),
            }
        )

    async def execute(
        self,
        name: str,
        arguments: str,
        context: ToolContext,
        research_context: ResearchContext,
        memory_scope: MemoryScope | None = None,
        calendar_scope: CalendarScope | None = None,
    ) -> str:
        if name not in _TOOL_NAMES:
            return self._json({"ok": False, "error": "tool_not_available"})

        try:
            parsed_arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return self._json({"ok": False, "error": "invalid_arguments"})

        if not isinstance(parsed_arguments, dict):
            return self._json({"ok": False, "error": "invalid_arguments"})

        if name == "web_search":
            return await self._execute_web_search(parsed_arguments, research_context)
        if name == "web_fetch":
            return await self._execute_web_fetch(parsed_arguments, research_context)
        if name == "search_channel_memory":
            return await self._execute_memory_search(
                parsed_arguments,
                research_context,
                memory_scope,
            )
        if name == "calendar_get_events":
            return await self._execute_calendar_get_events(
                parsed_arguments,
                research_context,
                calendar_scope,
            )
        if name == "calendar_propose_create":
            return self._execute_calendar_propose_create(
                parsed_arguments,
                research_context,
                calendar_scope,
            )
        if name == "calendar_propose_edit":
            return await self._execute_calendar_propose_edit(
                parsed_arguments,
                research_context,
                calendar_scope,
            )
        if parsed_arguments:
            return self._json({"ok": False, "error": "invalid_arguments"})

        try:
            if name == "get_current_channel_info":
                return self._json(
                    {
                        "ok": True,
                        "guild_name": context.guild_name,
                        "channel_name": context.channel_name,
                        "channel_type": context.channel_type,
                    }
                )

            result = await self._steam_notifier.fetch_current_offers()
            if result is None:
                return self._json({"ok": False, "error": "steam_unavailable"})

            return self._json(
                {
                    "ok": True,
                    "total": len(result.offers),
                    "offers": [
                        self._serialize_offer(offer) for offer in result.offers[:20]
                    ],
                }
            )
        except Exception:
            logging.error("Agent tool execution failed.")
            return self._json({"ok": False, "error": "tool_failed"})
