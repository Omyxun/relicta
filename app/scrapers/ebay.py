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
    ScraperRateLimitError,
)
from app.services.matcher import ScrapedListing

try:
    from curl_cffi.requests import AsyncSession as CffiAsyncSession
    HAS_CURL_CFFI = True
except ImportError:
    CffiAsyncSession = None
    HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)
settings = get_settings()

EBAY_OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"
EBAY_ITEM_ID_RE = re.compile(r"/itm/(?:[^/]+/)?(\d+)")
EBAY_PRICE_NUM_RE = re.compile(r"(\d[\d\s.,]*)")

# Fallback domains for European web scraping (all support shipping filter to CZ)
EBAY_WEB_DOMAINS: tuple[tuple[str, str], ...] = (
    ("https://www.ebay.de/sch/i.html", "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"),
    ("https://www.ebay.fr/sch/i.html", "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7"),
    ("https://www.ebay.pl/sch/i.html", "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7"),
)

TITLE_PREFIXES: tuple[str, ...] = (
    "Neues Angebot",
    "New Listing",
    "Nowa oferta sprzedaży",
    "Nouvelle annonce",
)

TITLE_SUFFIXES: tuple[str, ...] = (
    "Wird in neuem Fenster oder Tab geöffnet",
    "Otwiera się w nowym oknie lub nowej karcie",
    "S'ouvre dans une nouvelle fenêtre ou un nouvel onglet",
    "Opens in a new window or tab",
    "egieznA",
    "Anzeige",
)

FREE_SHIPPING_KEYWORDS: tuple[str, ...] = (
    "kostenlos",
    "free",
    "gratis",
    "bezpłatna",
    "bezplatna",
    "gratuit",
)


class EbayScraper(BaseScraper):
    """
    Hybrid eBay adapter:
    - Uses the official eBay Browse API (`buy/browse/v1/item_summary/search`) when
      EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are configured in .env.
    - Automatically falls back to multi-domain EU web scraping (Newly Listed,
      Buy It Now, shipping to CZ) using curl_cffi / httpx when API credentials
      are not configured.
    """

    platform_name = "ebay"

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None) -> None:
        super().__init__(http_client=http_client)
        self._token_lock = asyncio.Lock()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @property
    def has_api_credentials(self) -> bool:
        cur_settings = get_settings()
        return bool(cur_settings.EBAY_CLIENT_ID.strip() and cur_settings.EBAY_CLIENT_SECRET.strip())

    def reset_token(self) -> None:
        self._access_token = None
        self._token_expires_at = 0.0

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
    # Keyless Web Scraping Fallback (Multi-domain EU with anti-bot resilience)
    # -------------------------------------------------------------------------

    async def _search_single_query_html(self, query_str: str) -> list[ScrapedListing]:
        params: dict[str, str | int | float] = {
            "_nkw": query_str,
            "_sop": "10",        # Newly listed
            "LH_BIN": "1",       # Buy It Now (Fixed price)
            "_fcid": "56",       # Ship to Czech Republic (eBay country code 56)
            "_ipg": "60",
        }

        last_error: Optional[Exception] = None

        for search_url, accept_lang in EBAY_WEB_DOMAINS:
            try:
                html = await self._fetch_domain_html(search_url, params, accept_lang)
                if html:
                    listings = self._parse_web_html(html)
                    if listings:
                        logger.debug(
                            "[%s] Successfully retrieved %d listings from %s for query '%s'",
                            self.platform_name,
                            len(listings),
                            search_url,
                            query_str,
                        )
                        return listings
            except ScraperRateLimitError as exc:
                logger.info(
                    "[%s] Domain %s rate-limited/blocked (%s); attempting next fallback domain.",
                    self.platform_name,
                    search_url,
                    exc,
                )
                last_error = exc
            except Exception as exc:
                logger.warning(
                    "[%s] Domain %s failed: %s; attempting next fallback domain.",
                    self.platform_name,
                    search_url,
                    exc,
                )
                last_error = exc

        if last_error:
            if isinstance(last_error, ScraperRateLimitError):
                raise last_error
            raise ScraperError(
                self.platform_name,
                f"All eBay web search fallback domains failed for '{query_str}': {last_error}",
            ) from last_error

        return []

    async def _fetch_domain_html(
        self, url: str, params: dict[str, str | int | float], accept_lang: str
    ) -> Optional[str]:
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": accept_lang,
            "Referer": url.rsplit("/", 1)[0] + "/",
            "DNT": "1",
            "Sec-GPC": "1",
        }

        # Strategy 1: Use curl_cffi with Firefox browser impersonation to bypass Akamai bot detection
        if HAS_CURL_CFFI:
            try:
                async with CffiAsyncSession(impersonate="firefox") as session:
                    resp = await session.get(url, params=params, headers=headers, timeout=12.0)
                    if resp.status_code in (403, 429):
                        raise ScraperRateLimitError(
                            self.platform_name,
                            f"HTTP {resp.status_code} anti-bot challenge on {url}",
                            status_code=resp.status_code,
                        )
                    if resp.status_code == 200:
                        text = resp.text
                        # Check for splashui / captcha interstitial page
                        if len(text) < 50000 and any(
                            pat in text for pat in ("splashui", "Bitte entschuldigen", "Pardon the interruption")
                        ):
                            raise ScraperRateLimitError(
                                self.platform_name,
                                f"Anti-bot CAPTCHA challenge detected on {url}",
                                status_code=403,
                            )
                        return text
            except ScraperRateLimitError:
                raise
            except Exception as exc:
                logger.debug("[%s] curl_cffi request failed on %s: %s", self.platform_name, url, exc)

        # Strategy 2: Fall back to BaseScraper httpx request
        browser_headers = self.build_browser_headers(
            referer=url.rsplit("/", 1)[0] + "/",
            extra_headers={"Accept-Language": accept_lang},
        )
        response = await self.request("GET", url, params=params, headers=browser_headers)
        if response.status_code == 200:
            text = response.text
            if len(text) < 50000 and "splashui" in text:
                raise ScraperRateLimitError(
                    self.platform_name,
                    f"Anti-bot CAPTCHA challenge on {url}",
                    status_code=403,
                )
            return text

        return None

    def _parse_web_html(self, html: str) -> list[ScrapedListing]:
        if not html or len(html) < 500:
            raise ScraperParseError(self.platform_name, "Empty HTML response from eBay web search")

        tree = LexborHTMLParser(html)
        # Supports both modern (.s-card) and legacy (li.s-item, div.s-item__wrapper) search DOM layouts
        cards = tree.css(".s-card, li.s-item, div.s-item__wrapper")
        if not cards:
            return []

        listings: list[ScrapedListing] = []
        for card in cards:
            parsed = self._parse_web_card(card)
            if parsed is not None:
                listings.append(parsed)
        return listings

    def _parse_web_card(self, card: LexborNode) -> Optional[ScrapedListing]:
        link_node = (
            card.css_first("a[href*='/itm/']")
            or card.css_first("a.s-card__link")
            or card.css_first("a.s-item__link")
        )
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

        heading_node = (
            card.css_first("[role='heading']")
            or card.css_first(".s-card__title")
            or card.css_first("div.s-item__title")
        )
        if heading_node is None:
            return None

        title = heading_node.text(strip=True)
        for prefix in TITLE_PREFIXES:
            if title.startswith(prefix):
                title = title[len(prefix):].strip()

        for suffix in TITLE_SUFFIXES:
            if title.endswith(suffix):
                title = title[:-len(suffix)].strip()

        if not title or title.lower() == "shop on ebay":
            return None

        clean_url = f"https://www.ebay.com/itm/{external_id}"

        price_node = (
            card.css_first(".s-card__price")
            or card.css_first("span.s-item__price")
            or card.css_first("[class*='price']")
        )
        price_text = price_node.text(strip=True) if price_node else ""
        if " bis " in price_text.lower() or " to " in price_text.lower():
            return None

        raw_price, raw_currency = self._parse_ebay_money(price_text)
        if raw_price is None or raw_price <= 0:
            return None

        ship_node = (
            card.css_first("[class*='shipping']")
            or card.css_first("[class*='logistics']")
            or card.css_first("[class*='delivery']")
        )
        ship_text = ship_node.text(strip=True) if ship_node else ""
        shipping_price: Optional[float] = 0.0
        shipping_currency: Optional[str] = raw_currency

        if ship_text:
            low_ship = ship_text.lower()
            if any(k in low_ship for k in FREE_SHIPPING_KEYWORDS):
                shipping_price = 0.0
            else:
                parsed_ship, parsed_ship_curr = self._parse_ebay_money(ship_text)
                if parsed_ship is not None:
                    shipping_price = parsed_ship
                    shipping_currency = parsed_ship_curr

        img_node = (
            card.css_first(".s-card__media-wrapper img")
            or card.css_first("div.s-item__image-wrapper img")
            or card.css_first("img")
        )
        image_url: Optional[str] = None
        if img_node is not None:
            image_url = (
                img_node.attributes.get("src")
                or img_node.attributes.get("data-src")
                or ""
            ).strip() or None

        cond_node = (
            card.css_first("span.SECONDARY_INFO")
            or card.css_first(".s-card__subtitle")
            or card.css_first(".s-item__subtitle")
        )
        condition = cond_node.text(strip=True) if cond_node else None

        loc_node = (
            card.css_first("span.s-item__location")
            or card.css_first(".s-card__location")
            or card.css_first("span.s-item__itemLocation")
        )
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

        # Normalize non-breaking spaces
        norm_text = text.replace("\xa0", " ")
        upper = norm_text.upper()
        currency = "EUR"
        if "USD" in upper or "$" in upper:
            currency = "USD"
        elif "GBP" in upper or "£" in upper:
            currency = "GBP"
        elif "PLN" in upper or "ZŁ" in upper:
            currency = "PLN"
        elif "CZK" in upper or "KČ" in upper:
            currency = "CZK"

        match = EBAY_PRICE_NUM_RE.search(norm_text)
        if not match:
            return None, currency

        num_str = match.group(1).replace(" ", "")
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
            cur_settings = get_settings()
            response = await self.request(
                "POST",
                cur_settings.EBAY_OAUTH_URL,
                headers=headers,
                data=form_data,
                auth=(cur_settings.EBAY_CLIENT_ID.strip(), cur_settings.EBAY_CLIENT_SECRET.strip()),
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

    async def _search_single_query_api(self, query_str: str) -> list[ScrapedListing]:
        token = await self._get_valid_access_token()
        cur_settings = get_settings()
        ship_to_country = cur_settings.EBAY_DEFAULT_SHIP_TO_COUNTRY.strip().upper() or "CZ"

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Accept-Language": "cs-CZ,en-US;q=0.9",
            "X-EBAY-C-MARKETPLACE-ID": cur_settings.EBAY_MARKETPLACE_ID,
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
            cur_settings.EBAY_BROWSE_SEARCH_URL,
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
