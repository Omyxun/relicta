import asyncio
import logging
import random
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import SearchJob, SeenListing
from app.services.matcher import ScrapedListing

logger = logging.getLogger(__name__)
settings = get_settings()

USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
)


class ScraperError(Exception):
    """Base exception for all scraper-related errors."""

    def __init__(self, platform: str, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.platform = platform
        self.message = message
        self.status_code = status_code


class ScraperRateLimitError(ScraperError):
    """Raised when a target marketplace responds with HTTP 429 or 403 anti-bot challenge."""


class ScraperUpstreamError(ScraperError):
    """Raised on HTTP 5xx, network timeouts, or DNS failures."""


class ScraperParseError(ScraperError):
    """Raised when the DOM or JSON payload structure deviates from expected schema."""


class ScraperConfigError(ScraperError):
    """Raised when required API credentials (e.g., eBay Client ID/Secret) are missing."""


class BaseScraper(ABC):
    """
    Abstract base class for all marketplace scrapers.
    Provides connection pooling, User-Agent rotation, randomized jitter delays,
    and cross-job SQLite/memory description caching.
    """

    platform_name: str = "base"
    _mem_desc_cache: OrderedDict[tuple[str, str], str] = OrderedDict()
    _mem_desc_cache_max_size: int = 4096

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        self._external_client = http_client is not None
        self._client: Optional[httpx.AsyncClient] = http_client

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
            timeout = httpx.Timeout(
                connect=10.0,
                read=settings.SCRAPER_REQUEST_TIMEOUT_SECONDS,
                write=10.0,
                pool=10.0,
            )
            self._client = httpx.AsyncClient(
                http2=True,
                limits=limits,
                timeout=timeout,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed and not self._external_client:
            await self._client.aclose()

    def build_browser_headers(
        self,
        referer: Optional[str] = None,
        accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        extra_headers: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": accept,
            "Accept-Language": "cs-CZ,cs;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin" if referer else "none",
        }
        if referer:
            headers["Referer"] = referer
        if extra_headers:
            headers.update(extra_headers)
        return headers

    async def sleep_jitter(
        self,
        min_seconds: Optional[float] = None,
        max_seconds: Optional[float] = None,
    ) -> None:
        low = min_seconds if min_seconds is not None else settings.SCRAPER_MIN_DELAY_SECONDS
        high = max_seconds if max_seconds is not None else settings.SCRAPER_MAX_DELAY_SECONDS
        delay = random.uniform(low, max(low, high))
        logger.debug("[%s] Stagger delay: sleeping %.2fs", self.platform_name, delay)
        await asyncio.sleep(delay)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, str | int | float]] = None,
        headers: Optional[dict[str, str]] = None,
        data: Optional[dict[str, str]] = None,
        json_body: Optional[dict[str, object]] = None,
        auth: Optional[ tuple[str, str] ] = None,
    ) -> httpx.Response:
        client = await self.get_client()
        merged_headers = headers or self.build_browser_headers()

        try:
            response = await client.request(
                method=method,
                url=url,
                params=params,
                headers=merged_headers,
                data=data,
                json=json_body,
                auth=auth,
            )
        except httpx.TimeoutException as exc:
            raise ScraperUpstreamError(
                self.platform_name,
                f"Request timed out for {url}: {exc}",
            ) from exc
        except httpx.RequestError as exc:
            raise ScraperUpstreamError(
                self.platform_name,
                f"Network error requesting {url}: {exc}",
            ) from exc

        if response.status_code in (403, 429):
            raise ScraperRateLimitError(
                self.platform_name,
                f"Anti-bot or rate limit HTTP {response.status_code} from {url}",
                status_code=response.status_code,
            )
        if response.status_code >= 500:
            raise ScraperUpstreamError(
                self.platform_name,
                f"Upstream server error HTTP {response.status_code} from {url}",
                status_code=response.status_code,
            )
        if response.status_code >= 400:
            raise ScraperError(
                self.platform_name,
                f"Unexpected client HTTP {response.status_code} from {url}",
                status_code=response.status_code,
            )

        return response

    @classmethod
    def remember_description_in_memory(
        cls, platform: str, external_id: str, description: str
    ) -> None:
        key = (platform, external_id)
        cls._mem_desc_cache[key] = description
        cls._mem_desc_cache.move_to_end(key)
        while len(cls._mem_desc_cache) > cls._mem_desc_cache_max_size:
            cls._mem_desc_cache.popitem(last=False)

    async def get_cached_description(
        self,
        external_id: str,
        session: Optional[AsyncSession] = None,
    ) -> Optional[str]:
        """
        Checks in-memory LRU cache first, then queries `seen_listings.description_cached`
        across all jobs in SQLite so an individual listing page is never fetched twice.
        """
        mem_key = (self.platform_name, external_id)
        if mem_key in self._mem_desc_cache:
            self._mem_desc_cache.move_to_end(mem_key)
            return self._mem_desc_cache[mem_key]

        stmt = (
            select(SeenListing.description_cached)
            .where(
                SeenListing.platform == self.platform_name,
                SeenListing.external_id == external_id,
                SeenListing.description_cached.is_not(None),
            )
            .limit(1)
        )

        if session is not None:
            result = await session.execute(stmt)
            cached = result.scalar_one_or_none()
        else:
            async with AsyncSessionLocal() as temp_session:
                result = await temp_session.execute(stmt)
                cached = result.scalar_one_or_none()

        if cached:
            self.remember_description_in_memory(self.platform_name, external_id, cached)
        return cached

    @abstractmethod
    async def search(
        self,
        job: SearchJob,
        session: Optional[AsyncSession] = None,
        base_currency: str = "CZK",
    ) -> list[ScrapedListing]:
        """
        Executes Page 1 search queries for `job` and returns deduplicated `ScrapedListing` items.
        """
        raise NotImplementedError