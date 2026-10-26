import logging
import re
from typing import Optional
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser, LexborNode
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SearchJob
from app.scrapers.base import BaseScraper, ScraperParseError
from app.services.currency import currency_service
from app.services.matcher import ScrapedListing, is_listing_unpriced, listing_matcher

logger = logging.getLogger(__name__)

BAZOS_SEARCH_URL = "https://www.bazos.cz/search.php"
BAZOS_BASE_URL = "https://www.bazos.cz"
BAZOS_ID_RE = re.compile(r"/inzerat/(\d+)/")
PRICE_DIGITS_RE = re.compile(r"[^\d]")


class BazosScraper(BaseScraper):
    """
    Scrapes Page 1 of Bazoš.cz search results via Lexbor HTML parser and executes
    Two-Stage Lazy Description Fetching only when a listing passes Stage 1 checks.
    """

    platform_name = "bazos"

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

            page_listings = await self._fetch_search_page(
                query_str=query_str,
                job=job,
                base_currency=base_currency,
            )

            for item in page_listings:
                if item.external_id in deduped_by_id:
                    continue

                # Check if we already cached the full description in memory or SQLite
                cached_desc = await self.get_cached_description(item.external_id, session=session)
                if cached_desc is not None:
                    item.full_description = cached_desc

                # Run Stage 1 filter before deciding whether to fetch the detail page
                stage1 = listing_matcher.evaluate_stage1(item, job, base_currency=base_currency)
                if stage1.passed_stage1 and stage1.needs_detail_fetch and item.full_description is None:
                    await self.sleep_jitter()
                    full_desc = await self._fetch_detail_description(item.url)
                    if full_desc is not None:
                        item.full_description = full_desc
                        self.remember_description_in_memory(
                            self.platform_name, item.external_id, full_desc
                        )

                deduped_by_id[item.external_id] = item

        return list(deduped_by_id.values())

    async def _fetch_search_page(
        self,
        query_str: str,
        job: SearchJob,
        base_currency: str,
    ) -> list[ScrapedListing]:
        params: dict[str, str | int | float] = {
            "hledat": query_str,
            "rubriky": "www",
            "hlokalita": "",
            "humkreis": "25",
            "cenaod": "",
            "cenado": "",
            "Submit": "Hledat",
            "kitx": "ano",
        }

        # Only apply server-side price bounds if unpriced ('Dohodou') ads are disabled.
        # Bazoš hides 'Dohodou' items whenever cenaod/cenado are populated.
        if not job.allow_unpriced:
            min_czk = int(max(0.0, currency_service.convert(job.min_price, base_currency, "CZK") - job.shipping_buffer))
            max_czk = int(max(1.0, currency_service.convert(job.max_price, base_currency, "CZK")))
            if min_czk > 0:
                params["cenaod"] = str(min_czk)
            if max_czk > 0:
                params["cenado"] = str(max_czk)

        headers = self.build_browser_headers(referer=BAZOS_BASE_URL)
        response = await self.request("GET", BAZOS_SEARCH_URL, params=params, headers=headers)
        return self._parse_search_html(response.text)

    def _parse_search_html(self, html: str) -> list[ScrapedListing]:
        if not html or len(html) < 200:
            raise ScraperParseError(self.platform_name, "Empty or truncated HTML from Bazoš search")

        tree = LexborHTMLParser(html)
        containers = tree.css("div.inzeraty.inzeratyflex")
        if not containers:
            # Fallback for category/table layout
            containers = tree.css("table.inzeraty")

        # Verify page is a valid Bazoš document even if 0 results matched
        if not containers:
            has_bazos_layout = (
                tree.css_first("div.sirka") is not None
                or tree.css_first("form#formhledat") is not None
                or "Nebyl nalezen žádný inzerát" in html
                or "Žádný inzerát nebyl nalezen" in html
            )
            if not has_bazos_layout:
                raise ScraperParseError(
                    self.platform_name,
                    "Bazoš DOM layout signature missing; possible captcha or structural change",
                )
            return []

        results: list[ScrapedListing] = []
        for node in containers:
            parsed = self._parse_listing_node(node)
            if parsed is not None:
                results.append(parsed)
        return results

    def _parse_listing_node(self, node: LexborNode) -> Optional[ScrapedListing]:
        title_link = (
            node.css_first("h2.nadpis a")
            or node.css_first("span.nadpis a")
            or node.css_first("div.inzeratynadpis a")
        )
        if title_link is None:
            return None

        href = (title_link.attributes.get("href") or "").strip()
        if not href:
            return None

        full_url = urljoin(BAZOS_BASE_URL, href)
        id_match = BAZOS_ID_RE.search(full_url)
        if not id_match:
            return None
        external_id = id_match.group(1)

        title = title_link.text(strip=True)
        if not title:
            return None

        snippet_node = node.css_first("div.popis")
        snippet = snippet_node.text(strip=True) if snippet_node else ""

        img_node = node.css_first("img.obrazek")
        image_url: Optional[str] = None
        if img_node is not None:
            raw_src = (img_node.attributes.get("src") or "").strip()
            if raw_src and "empty.gif" not in raw_src:
                image_url = urljoin(full_url, raw_src)

        price_node = node.css_first("div.inzeratycena") or node.css_first("span.cena")
        price_text = price_node.text(strip=True) if price_node else ""
        raw_price, unpriced = self._parse_bazos_price(price_text)

        loc_node = node.css_first("div.inzeratylok")
        location: Optional[str] = None
        if loc_node is not None:
            loc_lines = [
                part.strip()
                for part in loc_node.text(separator=" ", strip=True).split()
                if part.strip()
            ]
            if loc_lines:
                location = " ".join(loc_lines)

        return ScrapedListing(
            platform=self.platform_name,
            external_id=external_id,
            title=title,
            url=full_url,
            raw_price=raw_price,
            raw_currency="CZK",
            shipping_price=None,
            shipping_currency=None,
            is_unpriced=unpriced,
            snippet=snippet,
            full_description=None,
            image_url=image_url,
            location=location,
        )

    @staticmethod
    def _parse_bazos_price(price_text: str) -> tuple[Optional[float], bool]:
        if not price_text:
            return None, True

        if is_listing_unpriced(raw_price=1.0, price_text=price_text):
            return None, True

        digits_only = PRICE_DIGITS_RE.sub("", price_text)
        if not digits_only:
            return None, True

        try:
            value = float(digits_only)
        except ValueError:
            return None, True

        if value <= 0:
            return None, True
        return value, False

    async def _fetch_detail_description(self, detail_url: str) -> Optional[str]:
        """
        Secondary lazy HTTP GET to a Bazoš ad detail page to retrieve the full description.
        """
        try:
            headers = self.build_browser_headers(referer=BAZOS_SEARCH_URL)
            response = await self.request("GET", detail_url, headers=headers)
            tree = LexborHTMLParser(response.text)
            desc_node = tree.css_first("div.popisdetail") or tree.css_first("td.popis")
            if desc_node is None:
                logger.debug("[bazos] Detail description node missing on %s", detail_url)
                return None
            return desc_node.text(separator="\n", strip=True)
        except Exception as exc:
            logger.warning("[bazos] Failed to fetch detail page %s: %s", detail_url, exc)
            return None