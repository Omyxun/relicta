import asyncio
import logging
import time
from typing import Any, Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import SearchJob
from app.scrapers.base import (
    BaseScraper,
    ScraperConfigError,
    ScraperError,
    ScraperParseError,
)
from app.services.matcher import ScrapedListing

logger = logging.getLogger(__name__)
settings = get_settings()

EBAY_OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"


class EbayScraper(BaseScraper):
    """
    Integrates with the official eBay Browse API (`buy/browse/v1/item_summary/search`).
    Automatically acquires, caches, and renews OAuth2 Client Credentials tokens
    and extracts explicit shipping costs to the user's configured country (default CZ).
    """

    platform_name = "ebay"

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        super().__init__(http_client=http_client)
        self._token_lock = asyncio.Lock()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    async def _get_valid_access_token(self) -> str:
        if not settings.EBAY_CLIENT_ID or not settings.EBAY_CLIENT_SECRET:
            raise ScraperConfigError(
                self.platform_name,
                "EBAY_CLIENT_ID and EBAY_CLIENT_SECRET must be configured in .env to use EbayScraper",
            )

        now = time.monotonic()
        if self._access_token and now < (self._token_expires_at - 120.0):
            return self._access_token

        async with self._token_lock:
            now = time.monotonic()
            if self._access_token and now < (self._token_expires_at - 120.0):
                return self._access_token

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            }
            form_data = {
                "grant_type": "client_credentials",
                "scope": EBAY_OAUTH_SCOPE,
            }
            response = await self.request(
                "POST",
                settings.EBAY_OAUTH_URL,
                headers=headers,
                data=form_data,
                auth=(settings.EBAY_CLIENT_ID, settings.EBAY_CLIENT_SECRET),
            )

            try:
                token_data: dict[str, Any] = response.json()
            except ValueError as exc:
                raise ScraperParseError(
                    self.platform_name, f"Invalid JSON from eBay OAuth2 endpoint: {exc}"
                ) from exc

            token = token_data.get("access_token")
            expires_in = int(token_data.get("expires_in", 7200))
            if not token or not isinstance(token, str):
                raise ScraperError(
                    self.platform_name,
                    f"eBay OAuth2 response missing access_token: {token_data}",
                )

            self._access_token = token
            self._token_expires_at = time.monotonic() + float(expires_in)
            logger.info("[ebay] Acquired new OAuth2 token (valid for %ds)", expires_in)
            return self._access_token

    async def search(
        self,
        job: SearchJob,
        session: Optional[AsyncSession] = None,
        base_currency: str = "CZK",
    ) -> list[ScrapedListing]:
        queries = [q.strip() for q in (job.search_queries or []) if q and q.strip()]
        if not queries:
            return []

        deduped_by_id: dict[str, ScrapedListing] = {}

        for idx, query_str in enumerate(queries):
            if idx > 0:
                await self.sleep_jitter()

            items = await self._search_single_query(query_str)
            for item in items:
                if item.external_id not in deduped_by_id:
                    deduped_by_id[item.external_id] = item

        return list(deduped_by_id.values())

    async def _search_single_query(self, query_str: str) -> list[ScrapedListing]:
        token = await self._get_valid_access_token()
        ship_to_country = settings.EBAY_DEFAULT_SHIP_TO_COUNTRY.strip().upper() or "CZ"

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Language": "cs-CZ,en-US;q=0.9",
            "X-EBAY-C-MARKETPLACE-ID": settings.EBAY_MARKETPLACE_ID,
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country={ship_to_country}",
        }

        filter_str = f"deliveryCountry:{ship_to_country},buyingOptions:{{FIXED_PRICE}}"
        params: dict[str, str | int | float] = {
            "q": query_str,
            "sort": "newlyListed",
            "limit": 50,
            "filter": filter_str,
        }

        response = await self.request(
            "GET",
            settings.EBAY_BROWSE_SEARCH_URL,
            params=params,
            headers=headers,
        )

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ScraperParseError(
                self.platform_name, f"Invalid JSON from eBay Browse API: {exc}"
            ) from exc

        summaries = payload.get("itemSummaries")
        if not summaries or not isinstance(summaries, list):
            return []

        listings: list[ScrapedListing] = []
        for raw_summary in summaries:
            if not isinstance(raw_summary, dict):
                continue
            parsed = self._parse_item_summary(raw_summary)
            if parsed is not None:
                listings.append(parsed)

        return listings

    def _parse_item_summary(self, item: dict[str, Any]) -> Optional[ScrapedListing]:
        item_id = str(item.get("itemId") or "").strip()
        title = str(item.get("title") or "").strip()
        web_url = str(item.get("itemWebUrl") or "").strip()
        if not item_id or not title or not web_url:
            return None

        price_obj = item.get("price")
        raw_price: Optional[float] = None
        raw_currency = "EUR"
        is_unpriced = False

        if isinstance(price_obj, dict):
            raw_currency = str(price_obj.get("currency") or "EUR").strip().upper()
            try:
                raw_price = float(price_obj.get("value", 0.0))
                if raw_price <= 0:
                    is_unpriced = True
            except (TypeError, ValueError):
                is_unpriced = True
        else:
            is_unpriced = True

        shipping_price, shipping_currency = self._extract_shipping(item, raw_currency)

        image_url: Optional[str] = None
        image_obj = item.get("image")
        if isinstance(image_obj, dict) and image_obj.get("imageUrl"):
            image_url = str(image_obj["imageUrl"]).strip()
        else:
            thumbs = item.get("thumbnailImages")
            if isinstance(thumbs, list) and thumbs and isinstance(thumbs[0], dict):
                image_url = str(thumbs[0].get("imageUrl") or "").strip() or None

        condition = str(item.get("condition") or "").strip() or None

        seller_feedback: Optional[float] = None
        seller_obj = item.get("seller")
        if isinstance(seller_obj, dict) and seller_obj.get("feedbackPercentage") is not None:
            try:
                seller_feedback = float(seller_obj["feedbackPercentage"])
            except (TypeError, ValueError):
                seller_feedback = None

        location: Optional[str] = None
        loc_obj = item.get("itemLocation")
        if isinstance(loc_obj, dict):
            country = str(loc_obj.get("country") or "").strip()
            postal = str(loc_obj.get("postalCode") or "").strip()
            location = f"{country} {postal}".strip() or None

        short_desc = str(item.get("shortDescription") or "").strip()
        snippet_parts = [p for p in (condition, short_desc) if p]
        snippet = " | ".join(snippet_parts)

        return ScrapedListing(
            platform=self.platform_name,
            external_id=item_id,
            title=title,
            url=web_url,
            raw_price=raw_price,
            raw_currency=raw_currency,
            shipping_price=shipping_price,
            shipping_currency=shipping_currency,
            is_unpriced=is_unpriced,
            snippet=snippet,
            full_description=snippet,
            image_url=image_url,
            location=location,
            condition=condition,
            seller_feedback_score=seller_feedback,
        )

    @staticmethod
    def _extract_shipping(
        item: dict[str, Any], default_currency: str
    ) -> tuple[Optional[float], Optional[str]]:
        shipping_options = item.get("shippingOptions")
        if not isinstance(shipping_options, list) or not shipping_options:
            return None, None

        lowest_cost: Optional[float] = None
        lowest_currency: Optional[str] = None

        for opt in shipping_options:
            if not isinstance(opt, dict):
                continue
            cost_type = str(opt.get("shippingCostType") or "").upper()
            if cost_type == "FREE":
                return 0.0, default_currency

            cost_obj = opt.get("shippingCost")
            if isinstance(cost_obj, dict) and cost_obj.get("value") is not None:
                try:
                    val = float(cost_obj["value"])
                    curr = str(cost_obj.get("currency") or default_currency).strip().upper()
                    if lowest_cost is None or val < lowest_cost:
                        lowest_cost = val
                        lowest_currency = curr
                except (TypeError, ValueError):
                    continue

        return lowest_cost, lowest_currency