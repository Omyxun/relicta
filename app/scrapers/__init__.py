from typing import Optional

from app.scrapers.base import (
    BaseScraper,
    ScraperConfigError,
    ScraperError,
    ScraperParseError,
    ScraperRateLimitError,
    ScraperUpstreamError,
)
from app.scrapers.bazos import BazosScraper
from app.scrapers.ebay import EbayScraper
from app.scrapers.sbazar import SbazarScraper

_SCRAPER_REGISTRY: dict[str, BaseScraper] = {
    BazosScraper.platform_name: BazosScraper(),
    SbazarScraper.platform_name: SbazarScraper(),
    EbayScraper.platform_name: EbayScraper(),
}


def register_scraper(scraper: BaseScraper) -> None:
    """
    Allows drop-in registration of v2 scraper adapters (e.g., Vinted via curl_cffi)
    without modifying core scheduler or matcher logic.
    """
    _SCRAPER_REGISTRY[scraper.platform_name.lower()] = scraper


def get_scraper(platform: str) -> Optional[BaseScraper]:
    return _SCRAPER_REGISTRY.get(platform.strip().lower())


def get_all_scrapers() -> dict[str, BaseScraper]:
    return dict(_SCRAPER_REGISTRY)


async def close_all_scrapers() -> None:
    for scraper in _SCRAPER_REGISTRY.values():
        await scraper.close()


__all__ = [
    "BaseScraper",
    "BazosScraper",
    "EbayScraper",
    "SbazarScraper",
    "ScraperConfigError",
    "ScraperError",
    "ScraperParseError",
    "ScraperRateLimitError",
    "ScraperUpstreamError",
    "close_all_scrapers",
    "get_all_scrapers",
    "get_scraper",
    "register_scraper",
]