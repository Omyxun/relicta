import asyncio
import logging
import re
import time
from typing import Any, Optional

import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import SearchJob
from app.scrapers.base import (
    BaseScraper,
    ScraperError,
    ScraperParseError,
)
from app.services.matcher import ScrapedListing

logger = logging.getLogger(__name__)
settings = get_settings()

EBAY_OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
EBAY_WEB_SEARCH_URL = "https://www.ebay.de/sch/i.html"
EBAY_ITEM_ID_RE = re.compile(r"/itm/(?:[^/]+/)?(\d+)")
EBAY_PRICE_NUM_RE = re.compile(r"(\d[\d\.,]*)")


class EbayScraper(BaseScraper):
    """
    Hybrid eBay adapter:
    - Uses the official eBay Browse API (`buy/browse/v1/item_summary/search`) when
      EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are configured in .env.
    - Automatically falls back to direct HTML scraping on ebay.de (Newly Listed,
      Buy It Now, shipping to CZ) when API credentials are not configured.
    """

    platform_name = "ebay"

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        super().__init__(http_client=http_client)
        self._token_lock = asyncio.Lock()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def has_api_credentials(self) -> bool:
        return bool(settings.EBAY_CLIENT_ID.strip() and settings.EBAY_CLIENT_SECRET.strip())

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

            if self.has_api_credentials:
                items = await self._search_single_query_api(query_str)
            else:
                items = await self._search_single_query_html(query_str)

            for item in items:
                if item.external_id not in deduped_by_id:
                    deduped_by_id[item.external_id] = item

        return list(deduped_by_id.values())

    # -------------------------------------------------------------------------
    # Keyless Web Scraping Fallback (ebay.de)
    # -------------------------------------------------------------------------

    async def _search_single_query_html(self, query_str: str) -> list[ScrapedListing]:
        params: dict[str, str | int | float] = {
            "_nkw": query_str,
            "_sop": "10",        # Newly listed
            "LH_BIN": "1",       # Buy It Now (Fixed price)
            "_fcid": "56",       # Ship to Czech Republic (country code 56 on eBay)
            "_ipg": "60",
        }
        headers = self.build_browser_headers(
            referer="https://www.ebay.de/",
            extra_headers={"Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        response = await self.request("GET", EBAY_WEB_SEARCH_URL, params=params, headers=headers)
        return self._parse_web_html(response.text)

    def _parse_web_html(self, html: str) -> list[ScrapedListing]:
        if not html or len(html) < 500:
            raise ScraperParseError(self.platform_name, "Empty HTML response from eBay web search")

        tree = LexborHTMLParser(html)
        cards = tree.css("li.s-item, div.s-item__wrapper")
        if not cards:
            return []

        listings: list[ScrapedListing] = []
        for card in cards:
            parsed = self._parse_web_card(card)
            if parsed is not None:
                listings.append(parsed)
        return listings

    def _parse_web_card(self, card: LexborNode) -> Optional[ScrapedListing]:
        link_node = card.css_first("a.s-item__link")
        if link_node is None:
            return None

        href = (link_node.attributes.get("href") or "").strip()
        id_match = EBAY_ITEM_ID_RE.search(href)
        if not id_match:
            return None
        external_id = id_match.group(1)

        # Skip eBay's hidden dummy header item ("Shop on eBay" / id 123456)
        if external_id == "123456":
            return None

        title_node = card.css_first("div.s-item__title span[role='heading']") or card.css_first("div.s-item__title")
        if title_node is None:
            return None
        title = title_node.text(strip=True)
        for prefix in ("Neues Angebot", "New Listing"):
            if title.startswith(prefix):
                title = title[len(prefix):].strip()
        if not title or title.lower() == "shop on ebay":
            return None

        clean_url = f"https://www.ebay.de/itm/{external_id}"

        price_node = card.css_first("span.s-item__price")
        price_text = price_node.text(strip=True) if price_node else ""
        # Ignore price range listings (e.g., "EUR 10,00 bis EUR 45,00")
        if " bis " in price_text.lower() or " to " in price_text.lower():
            return None
        raw_price, raw_currency = self._parse_ebay_money(price_text)
        if raw_price is None or raw_price <= 0:
            return None

        ship_node = (
            card.css_first("span.s-item__shipping")
            or card.css_first("span.s-item__logisticsCost")
        )
        ship_text = ship_node.text(strip=True) if ship_node else ""
        shipping_price = 0.0
        shipping_currency = raw_currency
        if ship_text:
            low_ship = ship_text.lower()
            if "kostenlos" in low_ship or "free" in low_ship or "gratis" in low_ship:
                shipping_price = 0.0
            else:
                parsed_ship, parsed_ship_curr = self._parse_ebay_money(ship_text)
                if parsed_ship is not None:
                    shipping_price = parsed_ship
                    shipping_currency = parsed_ship_curr

        img_node = card.css_first("div.s-item__image-wrapper img") or card.css_first("img")
        image_url: Optional[str] = None
        if img_node is not None:
            image_url = (
                img_node.attributes.get("src")
                or img_node.attributes.get("data-src")
                or ""
            ).strip() or None

        cond_node = card.css_first("span.SECONDARY_INFO")
        condition = cond_node.text(strip=True) if cond_node else None

        loc_node = card.css_first("span.s-item__location") or card.css_first("span.s-item__itemLocation")
        location = loc_node.text(strip=True) if loc_node else "eBay EU"

        return ScrapedListing(
            platform=self.platform_name,
            external_id=external_id,
            title=title,
            url=clean_url,
            raw_price=raw_price,
            raw_currency=raw_currency,
            shipping_price=shipping_price,
            shipping_currency=shipping_currency,
            is_unpriced=False,
            snippet=condition or "",
            full_description=f"{title}\n{condition or ''}",
            image_url=image_url,
            location=location,
            condition=condition,
        )

    @staticmethod
    def _parse_ebay_money(text: str) -> tuple[Optional[float], str]:
        if not text:
            return None, "EUR"

        upper = text.upper()
        currency = "EUR"
        if "USD" in upper or "$" in upper:
            currency = "USD"
        elif "GBP" in upper or "£" in upper:
            currency = "GBP"
        elif "PLN" in upper:
            currency = "PLN"
        elif "CZK" in upper or "KČ" in upper:
            currency = "CZK"

        match = EBAY_PRICE_NUM_RE.search(text)
        if not match:
            return None, currency

        num_str = match.group(1)
        # Convert German/European decimal format (e.g., "1.250,50" -> "1250.50")
        if "," in num_str and "." in num_str:
            if num_str.rfind(",") > num_str.rfind("."):
                num_str = num_str.replace(".", "").replace(",", ".")
            else:
                num_str = num_str.replace(",", "")
        elif "," in num_str:
            num_str = num_str.replace(",", ".")

        try:
            return float(num_str), currency
        except ValueError:
            return None, currency

    # -------------------------------------------------------------------------
    # Official eBay Browse API (used when EBAY_CLIENT_ID & SECRET are set)
    # -------------------------------------------------------------------------

    async def _get_valid_access_token(self) -> str:
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
            return self._access_token

    async def _search_single_query_api(self, query_str: str) -> list[ScrapedListing]:
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

        payload: dict[str, Any] = response.json()
        summaries = payload.get("itemSummaries")
        if not summaries or not isinstance(summaries, list):
            return []

        listings: list[ScrapedListing] = []
        for raw_summary in summaries:
            if isinstance(raw_summary, dict):
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

        condition = str(item.get("condition") or "").strip() or None
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
            snippet=condition or "",
            full_description=condition or "",
            image_url=image_url,
            condition=condition,
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
            if str(opt.get("shippingCostType") or "").upper() == "FREE":
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