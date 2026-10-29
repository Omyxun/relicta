import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser
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

# Facebook Marketplace URL patterns
FB_MARKETPLACE_SEARCH_URL = "https://www.facebook.com/marketplace/{location}/search/"
FB_MARKETPLACE_ITEM_URL = "https://www.facebook.com/marketplace/item/{item_id}"
FB_GRAPHQL_URL = "https://www.facebook.com/api/graphql/"

# Regex to extract listing IDs from URLs or JSON payloads
FB_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")
FB_PRICE_AMOUNT_RE = re.compile(r"(\d[\d\s.,]*)")


class FacebookMarketplaceScraper(BaseScraper):
    """
    Facebook Marketplace scraper using two strategies:

    1. **Primary — Embedded JSON hydration extraction**: Fetches the Marketplace
       search page HTML and parses the server-side-rendered JSON payloads that
       Facebook embeds in ``<script>`` tags for React hydration. This avoids
       reliance on obfuscated CSS class names that change on every FB deployment.

    2. **Fallback — DOM link extraction**: If the hydration JSON yields no
       results, falls back to extracting ``/marketplace/item/`` anchor links
       and surrounding text from the rendered HTML.

    **Authentication**: Facebook Marketplace search works publicly without
    authentication. Optional session cookies (``FB_COOKIE_C_USER`` and
    ``FB_COOKIE_XS``) can be configured in ``.env`` to access authenticated
    marketplace feeds or bypass regional interstitials.
    """

    platform_name = "facebook"

    @property
    def has_credentials(self) -> bool:
        return bool(
            settings.FB_COOKIE_C_USER.strip() and settings.FB_COOKIE_XS.strip()
        )

    def _build_cookie_header(self) -> str:
        """Assembles the Cookie header from configured Facebook session tokens."""
        parts: list[str] = []
        if settings.FB_COOKIE_C_USER.strip():
            parts.append(f"c_user={settings.FB_COOKIE_C_USER.strip()}")
        if settings.FB_COOKIE_XS.strip():
            parts.append(f"xs={settings.FB_COOKIE_XS.strip()}")
        if settings.FB_COOKIE_DATR.strip():
            parts.append(f"datr={settings.FB_COOKIE_DATR.strip()}")
        return "; ".join(parts)

    def _build_fb_headers(self, referer: Optional[str] = None) -> dict[str, str]:
        """Browser-like headers with optional Facebook session cookies injected."""
        extra_headers: dict[str, str] = {
            "Accept-Language": "cs-CZ,cs;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Fetch-User": "?1",
        }
        cookie_header = self._build_cookie_header()
        if cookie_header:
            extra_headers["Cookie"] = cookie_header

        headers = self.build_browser_headers(
            referer=referer or "https://www.facebook.com/marketplace/",
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            extra_headers=extra_headers,
        )
        return headers

    async def search(
        self,
        job: SearchJob,
        session: Optional[AsyncSession] = None,
        base_currency: str = "CZK",
    ) -> list[ScrapedListing]:
        queries = [q.strip() for q in (job.search_queries or []) if q and q.strip()]
        if not queries:
            return []

        if not self.has_credentials:
            logger.debug(
                "[facebook] FB_COOKIE_C_USER / FB_COOKIE_XS not configured. "
                "Executing public unauthenticated Marketplace search."
            )

        deduped_by_id: dict[str, ScrapedListing] = {}

        for idx, query_str in enumerate(queries):
            if idx > 0:
                await self.sleep_jitter()

            items = await self._search_single_query(query_str, job)

            for item in items:
                if item.external_id not in deduped_by_id:
                    deduped_by_id[item.external_id] = item

        return list(deduped_by_id.values())

    async def _search_single_query(
        self, query_str: str, job: SearchJob
    ) -> list[ScrapedListing]:
        """
        Fetches a Facebook Marketplace search page and extracts listings from
        the embedded JSON hydration data or, failing that, from DOM links.
        """
        location = settings.FB_MARKETPLACE_LOCATION.strip() or "prague"
        search_url = FB_MARKETPLACE_SEARCH_URL.format(location=quote_plus(location))

        params: dict[str, str | int | float] = {
            "query": query_str,
            "sortBy": "creation_time_descend",
        }
        # Only set minPrice parameter when unpriced ads are disallowed;
        # otherwise upstream Facebook search hides 0/unpriced listings
        if not getattr(job, "allow_unpriced", False):
            min_p = getattr(job, "min_price", None)
            if isinstance(min_p, (int, float)) and min_p > 0:
                params["minPrice"] = int(min_p)
        max_p = getattr(job, "max_price", None)
        if isinstance(max_p, (int, float)) and max_p > 0:
            params["maxPrice"] = int(max_p)

        headers = self._build_fb_headers(
            referer=f"https://www.facebook.com/marketplace/{quote_plus(location)}/"
        )

        response = await self.request("GET", search_url, params=params, headers=headers)
        html = response.text

        if not html or len(html) < 500:
            raise ScraperParseError(
                self.platform_name,
                "Empty or truncated HTML response from Facebook Marketplace",
            )

        # Check for login redirect / challenge page
        if self._is_login_page(html):
            if self.has_credentials:
                msg = (
                    "Facebook returned a login page — session cookies (FB_COOKIE_C_USER / "
                    "FB_COOKIE_XS) are expired or invalid. Please update them in .env."
                )
            else:
                msg = (
                    "Facebook required authentication for this search request. "
                    "Configure FB_COOKIE_C_USER and FB_COOKIE_XS in .env to authenticate."
                )
            raise ScraperConfigError(self.platform_name, msg)

        # Strategy 1: Extract from embedded JSON hydration payloads
        listings = self._extract_from_hydration_json(html)

        # Strategy 2: Fallback to DOM link parsing
        if not listings:
            listings = self._extract_from_dom(html)

        return listings

    @staticmethod
    def _is_login_page(html: str) -> bool:
        """Detects whether Facebook redirected to a login/checkpoint page."""
        login_signals = (
            "/login/",
            "login.php",
            "login_form",
            "checkpoint",
            '"require_login"',
            "log into facebook",
            "log in to facebook",
            "přihlaste se k facebooku",
            "přihlaste se",
            "LWI",  # Facebook's lightweight login interstitial
        )
        lower = html[:10000].lower()
        has_marketplace = (
            "marketplace" in lower
            or "marketplace_search" in lower
            or "groupcommerceproductitem" in lower
        )
        has_login = any(sig.lower() in lower for sig in login_signals)
        return has_login and not has_marketplace

    # -------------------------------------------------------------------------
    # Strategy 1: Embedded JSON / React Hydration Payload Extraction
    # -------------------------------------------------------------------------

    def _extract_from_hydration_json(self, html: str) -> list[ScrapedListing]:
        """
        Facebook SSR pages embed large JSON blobs inside <script> tags for
        React hydration. We parse script tags looking for marketplace listing
        payloads and extract structured listing data from them.
        """
        listings: list[ScrapedListing] = []
        seen_ids: set[str] = set()

        markers = (
            "marketplace_search",
            "MarketplaceListing",
            "GroupCommerceProductItem",
            "listing_price",
            "marketplace_listing_title",
        )

        # Strategy 1A: Parse via Lexbor parser (fast & robust against tag variations)
        tree = LexborHTMLParser(html)
        candidates: list[str] = []
        for s in tree.css("script"):
            content = s.text()
            if content and any(m in content for m in markers):
                candidates.append(content)

        # Strategy 1B: Fallback regex scan for <script> blocks if Lexbor found none
        if not candidates:
            script_re = re.compile(
                r"<script[^>]*>(.*?)</script>",
                re.DOTALL | re.IGNORECASE,
            )
            for match in script_re.finditer(html):
                content = match.group(1)
                if any(m in content for m in markers):
                    candidates.append(content)

        for blob_str in candidates:
            # Try to find parseable JSON substrings within the blob
            json_objects = self._extract_json_objects(blob_str)
            for obj in json_objects:
                found = self._recursive_find_listings(obj)
                for listing in found:
                    if listing.external_id not in seen_ids:
                        seen_ids.add(listing.external_id)
                        listings.append(listing)

        if listings:
            logger.debug(
                "[facebook] Extracted %d listings from hydration JSON", len(listings)
            )

        return listings

    @staticmethod
    def _extract_json_objects(text: str) -> list[dict[str, Any]]:
        """
        Attempts to parse JSON objects from a script tag's text content.
        Handles cases where the JSON is wrapped in JS function calls like
        ``require(...)`` or ``__d("...", ..., {...})``.
        """
        results: list[dict[str, Any]] = []

        # Direct JSON parse attempt
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                results.append(parsed)
                return results
            elif isinstance(parsed, list):
                results.extend([x for x in parsed if isinstance(x, dict)])
                return results
        except (json.JSONDecodeError, ValueError):
            pass

        # Find all top-level JSON object substrings via brace matching
        start_positions = [i for i, ch in enumerate(text) if ch == "{"]
        for start in start_positions[:100]:
            depth = 0
            end = start
            for j in range(start, min(start + 500_000, len(text))):
                if text[j] == "{":
                    depth += 1
                elif text[j] == "}":
                    depth -= 1
                    if depth == 0:
                        end = j + 1
                        break
            if end > start and depth == 0:
                candidate = text[start:end]
                if len(candidate) > 50:  # Skip trivially small objects
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            results.append(parsed)
                        elif isinstance(parsed, list):
                            results.extend([x for x in parsed if isinstance(x, dict)])
                    except (json.JSONDecodeError, ValueError):
                        continue

        return results

    def _recursive_find_listings(
        self, data: Any, depth: int = 0
    ) -> list[ScrapedListing]:
        """
        Recursively walks a nested JSON structure looking for nodes that
        represent Facebook Marketplace listing objects.
        """
        if depth > 25:
            return []

        listings: list[ScrapedListing] = []

        if isinstance(data, dict):
            listing = self._try_parse_listing_node(data)
            if listing is not None:
                listings.append(listing)
            else:
                for value in data.values():
                    listings.extend(self._recursive_find_listings(value, depth + 1))

        elif isinstance(data, list):
            for item in data:
                listings.extend(self._recursive_find_listings(item, depth + 1))

        return listings

    def _try_parse_listing_node(self, node: dict[str, Any]) -> Optional[ScrapedListing]:
        """
        Checks whether a dict node represents a Marketplace listing and
        converts it to a ``ScrapedListing`` if so.

        Facebook's internal GraphQL schema uses several shapes:
        - ``__typename: "GroupCommerceProductItem"`` (common modern SSR shape)
        - ``__typename: "MarketplaceListing"`` with ``id``, ``marketplace_listing_title``,
          ``listing_price: {amount, currency}``
        - Flattened shapes with ``listing_id``, ``title``, ``formatted_price``
        """
        # Skip sold, hidden, or non-live listings
        if node.get("is_sold") is True or node.get("is_hidden") is True or node.get("is_live") is False:
            return None

        typename = node.get("__typename", "")
        has_listing_price = "listing_price" in node
        has_formatted_price = "formatted_price" in node

        recognized_types = (
            "MarketplaceListing",
            "MarketplaceListingWithContext",
            "Listing",
            "GroupCommerceProductItem",
        )

        # Identify nodes that look like listings
        if typename not in recognized_types and not (has_listing_price or has_formatted_price):
            return None

        # Extract ID
        raw_id = (
            node.get("id")
            or node.get("listing_id")
            or node.get("story_id")
        )
        if raw_id is None:
            # Try to extract from a nested target node
            target = node.get("target") or node.get("node") or {}
            if isinstance(target, dict):
                raw_id = target.get("id")
        if not raw_id:
            return None
        external_id = str(raw_id).strip()

        # Extract title
        title = (
            node.get("marketplace_listing_title")
            or node.get("title")
            or node.get("name")
        )
        if isinstance(title, dict):
            title = title.get("text") or title.get("value") or ""
        if not title or not isinstance(title, str):
            return None
        title = title.strip()
        if not title:
            return None

        # Extract price and currency
        raw_price: Optional[float] = None
        raw_currency = "CZK"
        is_unpriced = False

        price_obj = node.get("listing_price")
        if isinstance(price_obj, dict):
            if price_obj.get("currency"):
                raw_currency = str(price_obj["currency"]).strip().upper()
            elif price_obj.get("formatted_amount"):
                _, parsed_curr = self._parse_formatted_price(str(price_obj["formatted_amount"]))
                if parsed_curr:
                    raw_currency = parsed_curr

            try:
                raw_price = float(price_obj.get("amount", 0))
            except (TypeError, ValueError):
                raw_price = None
        elif has_formatted_price:
            formatted = str(node.get("formatted_price") or "")
            raw_price, raw_currency = self._parse_formatted_price(formatted)
        else:
            # Try alternate price keys
            for key in ("price", "current_price"):
                p = node.get(key)
                if isinstance(p, dict):
                    if p.get("currency"):
                        raw_currency = str(p["currency"]).strip().upper()
                    elif p.get("formatted_amount"):
                        _, parsed_curr = self._parse_formatted_price(str(p["formatted_amount"]))
                        if parsed_curr:
                            raw_currency = parsed_curr
                    try:
                        raw_price = float(p.get("amount", 0))
                    except (TypeError, ValueError):
                        continue
                    break

        if raw_price is None or raw_price <= 0:
            is_unpriced = True

        # Build URL
        url = FB_MARKETPLACE_ITEM_URL.format(item_id=external_id)

        # Extract image
        image_url: Optional[str] = None
        primary_photo = node.get("primary_listing_photo") or node.get("image")
        if isinstance(primary_photo, dict):
            if isinstance(primary_photo.get("image"), dict):
                image_url = primary_photo["image"].get("uri") or primary_photo["image"].get("url")
            if not image_url:
                image_url = (
                    primary_photo.get("uri")
                    or primary_photo.get("url")
                    or primary_photo.get("imageUrl")
                )
        elif isinstance(node.get("primaryListingPhoto"), dict):
            plp = node["primaryListingPhoto"]
            if isinstance(plp.get("image"), dict):
                image_url = plp["image"].get("uri") or plp["image"].get("url")
            if not image_url:
                image_url = plp.get("uri") or plp.get("url")

        # Extract location safely without ternary operator precedence bugs
        location: Optional[str] = None
        loc_obj = node.get("location")
        if not isinstance(loc_obj, dict):
            seller_obj = node.get("marketplace_listing_seller")
            if isinstance(seller_obj, dict):
                loc_obj = seller_obj.get("location")

        if isinstance(loc_obj, dict):
            rev_geo = loc_obj.get("reverse_geocode")
            if isinstance(rev_geo, dict):
                city_page = rev_geo.get("city_page")
                if isinstance(city_page, dict):
                    location = city_page.get("display_name")
                if not location:
                    location = rev_geo.get("city")
            if not location:
                location = loc_obj.get("name") or loc_obj.get("city")

        # Try top-level location_text
        if not location:
            location_text = node.get("location_text") or node.get("marketplace_listing_location")
            if isinstance(location_text, dict):
                location = location_text.get("text")
            elif isinstance(location_text, str):
                location = location_text.strip()

        # Extract description / snippet
        description = None
        for desc_key in ("redacted_description", "description"):
            desc_val = node.get(desc_key)
            if isinstance(desc_val, dict):
                description = desc_val.get("text") or desc_val.get("value")
                break
            elif isinstance(desc_val, str) and desc_val.strip():
                description = desc_val.strip()
                break

        snippet = (description or title)[:300] if description else title

        # Extract condition
        condition: Optional[str] = None
        cond_val = node.get("condition_text") or node.get("custom_sub_titles_with_rendering_flags")
        if isinstance(cond_val, dict):
            condition = cond_val.get("text")
        elif isinstance(cond_val, str):
            condition = cond_val.strip()
        elif isinstance(cond_val, list) and cond_val:
            first = cond_val[0]
            if isinstance(first, dict):
                condition = first.get("text")
            elif isinstance(first, str):
                condition = first

        return ScrapedListing(
            platform=self.platform_name,
            external_id=external_id,
            title=title,
            url=url,
            raw_price=raw_price,
            raw_currency=raw_currency,
            shipping_price=None,
            shipping_currency=None,
            is_unpriced=is_unpriced,
            snippet=snippet,
            full_description=description,
            image_url=image_url,
            location=location or "Facebook Marketplace",
            condition=condition,
        )

    @staticmethod
    def _parse_formatted_price(text: str) -> tuple[Optional[float], str]:
        """Parses Facebook's human-readable price string (e.g., 'Kč 1 500', '€120')."""
        if not text:
            return None, "CZK"

        upper = text.upper().replace("\xa0", " ")

        # Detect currency
        currency = "CZK"
        if "€" in upper or "EUR" in upper:
            currency = "EUR"
        elif "$" in upper or "USD" in upper:
            currency = "USD"
        elif "£" in upper or "GBP" in upper:
            currency = "GBP"
        elif "PLN" in upper or "ZŁ" in upper or "ZL" in upper:
            currency = "PLN"
        elif "KČ" in upper or "CZK" in upper:
            currency = "CZK"

        # Free / Zadarmo / Zdarma
        lowered = text.strip().lower()
        if any(w in lowered for w in ("free", "zadarmo", "zdarma", "gratis")):
            return 0.0, currency

        match = FB_PRICE_AMOUNT_RE.search(text)
        if not match:
            return None, currency

        num_str = match.group(1).strip()
        # Remove whitespace used as thousands separator (common in CZ locale: "1 500")
        num_str = re.sub(r"\s+", "", num_str)
        # Handle European decimal format
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
    # Strategy 2: DOM-Based Fallback (anchor link extraction)
    # -------------------------------------------------------------------------

    def _extract_from_dom(self, html: str) -> list[ScrapedListing]:
        """
        Fallback: scans the rendered DOM for ``/marketplace/item/`` links and
        extracts whatever surrounding text is available. This is less reliable
        than JSON extraction because Facebook obfuscates CSS class names, but
        it provides basic coverage when hydration data is stripped.
        """
        tree = LexborHTMLParser(html)
        item_links = tree.css('a[href*="/marketplace/item/"]')
        if not item_links:
            logger.debug("[facebook] No /marketplace/item/ links found in DOM")
            return []

        listings: list[ScrapedListing] = []
        seen_ids: set[str] = set()

        for link_node in item_links:
            href = (link_node.attributes.get("href") or "").strip()
            id_match = FB_ITEM_ID_RE.search(href)
            if not id_match:
                continue

            external_id = id_match.group(1)
            if external_id in seen_ids:
                continue
            seen_ids.add(external_id)

            # Walk up to the card container and collect visible text
            card_text = self._collect_card_text(link_node)
            if not card_text:
                continue

            # Attempt to separate title and price from the card text
            title, raw_price, raw_currency = self._parse_card_text(card_text)
            if not title:
                continue

            is_unpriced = raw_price is None or raw_price <= 0
            url = FB_MARKETPLACE_ITEM_URL.format(item_id=external_id)

            # Try to find a nearby image
            image_url = self._find_nearby_image(link_node)

            listings.append(
                ScrapedListing(
                    platform=self.platform_name,
                    external_id=external_id,
                    title=title,
                    url=url,
                    raw_price=raw_price,
                    raw_currency=raw_currency,
                    shipping_price=None,
                    shipping_currency=None,
                    is_unpriced=is_unpriced,
                    snippet=title,
                    full_description=None,
                    image_url=image_url,
                    location="Facebook Marketplace",
                )
            )

        if listings:
            logger.debug(
                "[facebook] Extracted %d listings from DOM fallback", len(listings)
            )

        return listings

    @staticmethod
    def _collect_card_text(link_node: Any) -> str:
        """
        Walks up from the link node to collect text from the enclosing card
        container. Facebook wraps each listing card in 2–4 levels of divs.
        """
        node = link_node
        for _ in range(5):
            parent = node.parent
            if parent is None:
                break
            node = parent

        text = node.text(separator="\n", strip=True) if node else ""
        return text

    def _parse_card_text(
        self, text: str
    ) -> tuple[Optional[str], Optional[float], str]:
        """
        Heuristically splits a Facebook listing card's concatenated text into
        title and price components. Facebook cards typically render as:
          Price
          Title
          Location
        """
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not lines:
            return None, None, "CZK"

        # Try to identify which line is the price
        price_line_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if any(
                sym in line for sym in ("Kč", "€", "$", "£", "CZK", "EUR", "USD", "GBP")
            ) or re.match(r"^\d[\d\s.,]*$", line.strip()):
                price_line_idx = i
                break

        if price_line_idx is not None:
            raw_price, raw_currency = self._parse_formatted_price(lines[price_line_idx])
            # Title is typically the line after the price
            remaining = [l for j, l in enumerate(lines) if j != price_line_idx]
            title = remaining[0] if remaining else None
        else:
            raw_price = None
            raw_currency = "CZK"
            title = lines[0]

        return title, raw_price, raw_currency

    @staticmethod
    def _find_nearby_image(link_node: Any) -> Optional[str]:
        """Searches sibling/parent nodes for an ``<img>`` tag with a src attribute."""
        node = link_node
        for _ in range(5):
            parent = node.parent
            if parent is None:
                break
            img = parent.css_first("img[src]")
            if img is not None:
                src = (img.attributes.get("src") or "").strip()
                if src and not src.startswith("data:"):
                    return src
            node = parent
        return None
