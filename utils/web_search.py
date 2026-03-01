"""
Multi-provider web search utility for Starzai.

Provider fallback chain (tries in order, no API key needed for top 2):
  1. DuckDuckGo  — primary, via duckduckgo-search (text, news, images, videos)
  2. Google News RSS — free fallback for news, ultra-fresh, via feedparser
  3. GNews.io API — optional, 100 req/day free (set GNEWS_API_KEY)
  4. CurrentsAPI  — optional, 600 req/day free (set CURRENTS_API_KEY)

Each provider implements `BaseSearchProvider` so adding more is trivial.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import aiohttp

from config.constants import (
    WEB_SEARCH_CACHE_TTL,
    WEB_SEARCH_MAX_RESULTS,
    WEB_SEARCH_MAX_SNIPPET_CHARS,
    WEB_SEARCH_NEWS_MAX_RESULTS,
    WEB_SEARCH_TIMEOUT,
)

logger = logging.getLogger(__name__)


# ── Data Models ──────────────────────────────────────────────────

@dataclass
class SearchResult:
    """A single web/news search result."""
    title: str
    url: str
    snippet: str
    source: str = ""
    image_url: str = ""
    published_at: str = ""


@dataclass
class MediaResult:
    """An image or video search result."""
    title: str
    url: str           # page URL
    media_url: str     # direct image/video URL
    thumbnail_url: str = ""
    source: str = ""
    media_type: str = "image"   # "image" or "video"
    duration: str = ""          # video duration if applicable


@dataclass
class SearchResponse:
    """Container for search results with metadata."""
    query: str
    results: List[SearchResult] = field(default_factory=list)
    images: List[MediaResult] = field(default_factory=list)
    videos: List[MediaResult] = field(default_factory=list)
    search_type: str = "web"     # "web" or "news"
    provider: str = "unknown"
    cached: bool = False
    error: Optional[str] = None

    @property
    def has_results(self) -> bool:
        return len(self.results) > 0

    @property
    def best_image(self) -> Optional[str]:
        """Return the best image URL from results or images list."""
        # First check if any text result has an image
        for r in self.results:
            if r.image_url:
                return r.image_url
        # Then check dedicated images
        if self.images:
            return self.images[0].media_url or self.images[0].thumbnail_url
        return None

    @property
    def best_video(self) -> Optional[MediaResult]:
        """Return the best video result."""
        return self.videos[0] if self.videos else None


# ── Base Provider ────────────────────────────────────────────────

class BaseSearchProvider(ABC):
    """Abstract base class for all search providers."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    async def is_available(self) -> bool: ...

    async def search_web(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return []

    async def search_news(self, query: str, max_results: int = 8) -> List[SearchResult]:
        return []

    async def search_images(self, query: str, max_results: int = 3) -> List[MediaResult]:
        return []

    async def search_videos(self, query: str, max_results: int = 3) -> List[MediaResult]:
        return []


# ── Provider 1: DuckDuckGo ───────────────────────────────────────

class DuckDuckGoProvider(BaseSearchProvider):
    """DuckDuckGo via duckduckgo-search library. Free, no key."""

    @property
    def name(self) -> str:
        return "duckduckgo"

    async def is_available(self) -> bool:
        return True

    async def search_web(self, query: str, max_results: int = 5) -> List[SearchResult]:
        from duckduckgo_search import AsyncDDGS
        async with AsyncDDGS() as ddgs:
            raw = await ddgs.atext(query, max_results=max_results)
        results = []
        for r in raw or []:
            results.append(SearchResult(
                title=r.get("title", "Untitled"),
                url=r.get("href", ""),
                snippet=r.get("body", "")[:WEB_SEARCH_MAX_SNIPPET_CHARS],
                source=r.get("source", ""),
            ))
        return results

    async def search_news(self, query: str, max_results: int = 8) -> List[SearchResult]:
        from duckduckgo_search import AsyncDDGS
        async with AsyncDDGS() as ddgs:
            raw = await ddgs.anews(query, max_results=max_results)
        results = []
        for r in raw or []:
            results.append(SearchResult(
                title=r.get("title", "Untitled"),
                url=r.get("url", ""),
                snippet=r.get("body", "")[:WEB_SEARCH_MAX_SNIPPET_CHARS],
                source=r.get("source", ""),
                image_url=r.get("image", ""),
                published_at=r.get("date", ""),
            ))
        return results

    async def search_images(self, query: str, max_results: int = 3) -> List[MediaResult]:
        from duckduckgo_search import AsyncDDGS
        async with AsyncDDGS() as ddgs:
            raw = await ddgs.aimages(query, max_results=max_results)
        results = []
        for r in raw or []:
            results.append(MediaResult(
                title=r.get("title", ""),
                url=r.get("url", ""),
                media_url=r.get("image", ""),
                thumbnail_url=r.get("thumbnail", ""),
                source=r.get("source", ""),
                media_type="image",
            ))
        return results

    async def search_videos(self, query: str, max_results: int = 3) -> List[MediaResult]:
        from duckduckgo_search import AsyncDDGS
        async with AsyncDDGS() as ddgs:
            raw = await ddgs.avideos(query, max_results=max_results)
        results = []
        for r in raw or []:
            # Extract thumbnail from nested images dict
            images = r.get("images", {})
            thumb = ""
            if isinstance(images, dict):
                thumb = images.get("large", "") or images.get("medium", "") or images.get("small", "")
            results.append(MediaResult(
                title=r.get("title", ""),
                url=r.get("content", ""),    # video URL (e.g. YouTube)
                media_url=r.get("content", ""),
                thumbnail_url=thumb,
                source=r.get("publisher", ""),
                media_type="video",
                duration=r.get("duration", ""),
            ))
        return results


# ── Provider 2: Google News RSS ──────────────────────────────────

class GoogleNewsRSSProvider(BaseSearchProvider):
    """Google News via RSS feed. Completely free, no API key, ultra-fresh."""

    @property
    def name(self) -> str:
        return "google_news_rss"

    async def is_available(self) -> bool:
        return True

    async def search_news(self, query: str, max_results: int = 8) -> List[SearchResult]:
        import feedparser
        url = f"https://news.google.com/rss/search?q={query}&hl=en&gl=US&ceid=US:en"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return []
                    xml_data = await resp.text()

            feed = await asyncio.get_event_loop().run_in_executor(
                None, feedparser.parse, xml_data,
            )
            results = []
            for entry in (feed.entries or [])[:max_results]:
                snippet = entry.get("summary", entry.get("description", ""))
                # Strip HTML tags from snippet
                import re
                snippet = re.sub(r"<[^>]+>", "", snippet)[:WEB_SEARCH_MAX_SNIPPET_CHARS]
                pub = entry.get("published", "")
                source_name = entry.get("source", {})
                if isinstance(source_name, dict):
                    source_name = source_name.get("title", "")
                elif hasattr(source_name, "title"):
                    source_name = source_name.title
                else:
                    source_name = str(source_name) if source_name else ""
                results.append(SearchResult(
                    title=entry.get("title", "Untitled"),
                    url=entry.get("link", ""),
                    snippet=snippet,
                    source=source_name,
                    published_at=pub,
                ))
            return results
        except Exception as exc:
            logger.warning("Google News RSS failed: %s", exc)
            return []

    async def search_web(self, query: str, max_results: int = 5) -> List[SearchResult]:
        # Google News RSS only does news
        return await self.search_news(query, max_results)


# ── Provider 3: GNews.io API (optional) ─────────────────────────

class GNewsProvider(BaseSearchProvider):
    """GNews.io — 100 requests/day free. Set GNEWS_API_KEY in .env."""

    def __init__(self) -> None:
        self._api_key = os.getenv("GNEWS_API_KEY", "")

    @property
    def name(self) -> str:
        return "gnews"

    async def is_available(self) -> bool:
        return bool(self._api_key)

    async def search_news(self, query: str, max_results: int = 8) -> List[SearchResult]:
        if not self._api_key:
            return []
        url = "https://gnews.io/api/v4/search"
        params = {
            "q": query,
            "lang": "en",
            "max": min(max_results, 10),
            "apikey": self._api_key,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning("GNews API returned %d", resp.status)
                        return []
                    data = await resp.json()
            results = []
            for article in data.get("articles", []):
                results.append(SearchResult(
                    title=article.get("title", "Untitled"),
                    url=article.get("url", ""),
                    snippet=article.get("description", "")[:WEB_SEARCH_MAX_SNIPPET_CHARS],
                    source=article.get("source", {}).get("name", ""),
                    image_url=article.get("image", ""),
                    published_at=article.get("publishedAt", ""),
                ))
            return results
        except Exception as exc:
            logger.warning("GNews API failed: %s", exc)
            return []

    async def search_web(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return await self.search_news(query, max_results)


# ── Provider 4: Currents API (optional) ─────────────────────────

class CurrentsAPIProvider(BaseSearchProvider):
    """Currents API — 600 requests/day free. Set CURRENTS_API_KEY in .env."""

    def __init__(self) -> None:
        self._api_key = os.getenv("CURRENTS_API_KEY", "")

    @property
    def name(self) -> str:
        return "currents"

    async def is_available(self) -> bool:
        return bool(self._api_key)

    async def search_news(self, query: str, max_results: int = 8) -> List[SearchResult]:
        if not self._api_key:
            return []
        url = "https://api.currentsapi.services/v1/search"
        params = {
            "keywords": query,
            "language": "en",
            "apiKey": self._api_key,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning("Currents API returned %d", resp.status)
                        return []
                    data = await resp.json()
            results = []
            for article in (data.get("news", []))[:max_results]:
                results.append(SearchResult(
                    title=article.get("title", "Untitled"),
                    url=article.get("url", ""),
                    snippet=article.get("description", "")[:WEB_SEARCH_MAX_SNIPPET_CHARS],
                    source=article.get("author", ""),
                    image_url=article.get("image", ""),
                    published_at=article.get("published", ""),
                ))
            return results
        except Exception as exc:
            logger.warning("Currents API failed: %s", exc)
            return []

    async def search_web(self, query: str, max_results: int = 5) -> List[SearchResult]:
        return await self.search_news(query, max_results)


# ══════════════════════════════════════════════════════════════════
#  WebSearcher — Multi-provider orchestrator with cache
# ══════════════════════════════════════════════════════════════════

class WebSearcher:
    """
    Async web search orchestrator with multi-provider fallback and caching.

    Tries providers in order. If the first fails or returns no results,
    falls back to the next available provider.
    """

    def __init__(self) -> None:
        # Provider chain — order matters (tried top to bottom)
        self._providers: List[BaseSearchProvider] = [
            DuckDuckGoProvider(),
            GoogleNewsRSSProvider(),
            GNewsProvider(),
            CurrentsAPIProvider(),
        ]
        # Simple in-memory cache: {cache_key: (timestamp, SearchResponse)}
        self._cache: Dict[str, Tuple[float, SearchResponse]] = {}

    # ── Cache ─────────────────────────────────────────────────────

    def _cache_key(self, query: str, search_type: str) -> str:
        return f"{search_type}:{query.lower().strip()}"

    def _get_cached(self, key: str) -> Optional[SearchResponse]:
        if key in self._cache:
            ts, resp = self._cache[key]
            if time.time() - ts < WEB_SEARCH_CACHE_TTL:
                resp.cached = True
                return resp
            del self._cache[key]
        return None

    def _set_cached(self, key: str, resp: SearchResponse) -> None:
        # Evict stale entries if cache too large
        if len(self._cache) > 300:
            now = time.time()
            expired = [k for k, (ts, _) in self._cache.items() if now - ts > WEB_SEARCH_CACHE_TTL]
            for k in expired:
                del self._cache[k]
        self._cache[key] = (time.time(), resp)

    # ── Public Search Methods ─────────────────────────────────────

    async def search(
        self,
        query: str,
        max_results: int = WEB_SEARCH_MAX_RESULTS,
    ) -> SearchResponse:
        """General web search with provider fallback."""
        cache_key = self._cache_key(query, "web")
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await asyncio.wait_for(
                self._search_with_fallback(query, max_results, search_type="web"),
                timeout=WEB_SEARCH_TIMEOUT,
            )
            self._set_cached(cache_key, response)
            return response
        except asyncio.TimeoutError:
            logger.warning("Web search timed out for: %s", query)
            return SearchResponse(query=query, error="Search timed out — please try again.")
        except Exception as exc:
            logger.error("Web search error for '%s': %s", query, exc, exc_info=True)
            return SearchResponse(query=query, error=f"Search failed: {exc}")

    async def search_news(
        self,
        query: str,
        max_results: int = WEB_SEARCH_NEWS_MAX_RESULTS,
    ) -> SearchResponse:
        """News search with provider fallback — optimized for freshness."""
        cache_key = self._cache_key(query, "news")
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        try:
            response = await asyncio.wait_for(
                self._search_with_fallback(query, max_results, search_type="news"),
                timeout=WEB_SEARCH_TIMEOUT,
            )
            self._set_cached(cache_key, response)
            return response
        except asyncio.TimeoutError:
            logger.warning("News search timed out for: %s", query)
            return SearchResponse(query=query, error="News search timed out — please try again.")
        except Exception as exc:
            logger.error("News search error for '%s': %s", query, exc, exc_info=True)
            return SearchResponse(query=query, error=f"News search failed: {exc}")

    async def search_media(
        self,
        query: str,
        max_images: int = 3,
        max_videos: int = 2,
    ) -> Tuple[List[MediaResult], List[MediaResult]]:
        """Fetch images and videos for a query. Uses DuckDuckGo."""
        images: List[MediaResult] = []
        videos: List[MediaResult] = []
        ddg = self._providers[0]  # DuckDuckGo is always first

        try:
            img_task = ddg.search_images(query, max_images)
            vid_task = ddg.search_videos(query, max_videos)
            images, videos = await asyncio.wait_for(
                asyncio.gather(img_task, vid_task, return_exceptions=True),
                timeout=WEB_SEARCH_TIMEOUT,
            )
            # Handle exceptions from gather
            if isinstance(images, BaseException):
                logger.warning("Image search failed: %s", images)
                images = []
            if isinstance(videos, BaseException):
                logger.warning("Video search failed: %s", videos)
                videos = []
        except asyncio.TimeoutError:
            logger.warning("Media search timed out for: %s", query)
        except Exception as exc:
            logger.warning("Media search error for '%s': %s", query, exc)

        return images, videos

    # ── Fallback Chain ────────────────────────────────────────────

    async def _search_with_fallback(
        self,
        query: str,
        max_results: int,
        search_type: str,
    ) -> SearchResponse:
        """Try each provider in order until one returns results."""
        last_error = ""

        for provider in self._providers:
            if not await provider.is_available():
                continue

            try:
                if search_type == "news":
                    results = await provider.search_news(query, max_results)
                else:
                    results = await provider.search_web(query, max_results)

                if results:
                    logger.debug(
                        "Search '%s' (%s) succeeded via %s (%d results)",
                        query, search_type, provider.name, len(results),
                    )

                    # Concurrently fetch media for richer results
                    response = SearchResponse(
                        query=query,
                        results=results,
                        search_type=search_type,
                        provider=provider.name,
                    )
                    return response

                logger.debug(
                    "Provider %s returned no results for '%s', trying next...",
                    provider.name, query,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Provider %s failed for '%s': %s — trying next...",
                    provider.name, query, exc,
                )
                continue

        # All providers exhausted
        return SearchResponse(
            query=query,
            search_type=search_type,
            error=f"All search providers failed. Last error: {last_error}" if last_error else None,
        )

    # ── Formatting Helpers ────────────────────────────────────────

    @staticmethod
    def format_results_for_llm(response: SearchResponse) -> str:
        """Format search results as context for the LLM prompt."""
        if not response.has_results:
            return f"[No search results found for: {response.query}]"

        lines = [
            f'[Web Search Results for "{response.query}" '
            f"— {response.search_type.upper()} via {response.provider}]",
            "",
        ]
        for i, r in enumerate(response.results, 1):
            source_tag = f" ({r.source})" if r.source else ""
            time_tag = f" [{r.published_at}]" if r.published_at else ""
            lines.append(f"{i}. **{r.title}**{source_tag}{time_tag}")
            lines.append(f"   URL: {r.url}")
            lines.append(f"   {r.snippet}")
            lines.append("")
        lines.append("[End of search results — synthesize a response using these sources]")
        return "\n".join(lines)

    @staticmethod
    def format_sources_for_embed(response: SearchResponse, max_sources: int = 5) -> str:
        """Format source links for display in a Discord embed."""
        if not response.has_results:
            return ""
        lines = []
        for i, r in enumerate(response.results[:max_sources], 1):
            title = r.title[:60] + "…" if len(r.title) > 60 else r.title
            source_tag = f" • {r.source}" if r.source else ""
            lines.append(f"{i}. [{title}]({r.url}){source_tag}")
        return "\n".join(lines)

    @staticmethod
    def format_video_for_embed(video: MediaResult) -> str:
        """Format a single video result for an embed field."""
        dur = f" ({video.duration})" if video.duration else ""
        src = f" • {video.source}" if video.source else ""
        return f"🎬 [{video.title[:60]}]({video.url}){dur}{src}"

