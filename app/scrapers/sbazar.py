import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import SearchJob
from app.scrapers.base import BaseScraper, ScraperParseError
from app.services.currency import currency_service
from app.services.matcher import ScrapedListing, listing_matcher

logger = logging.getLogger(__name__)

SBAZAR_SEARCH_API_URL = "https://www.sbazar.cz/api/v1/items/search"
SBAZAR_ITEM_API_URL = "https://www.sbazar.cz/api/v1/items/{item_id}"
SBAZAR_BASE_URL = "https://www.sbazar.cz"


class SbazarScraper(BaseScraper):
    """
    Queries Sbazar.cz's internal REST JSON API directly.
    Performs lazy detail lookup against /api/v1/items/{id} if the search payload
    only contains a truncated description and Stage 1 requires full-text verification.
    """

    platform_name = "sbazar"

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

            items = await self._fetch_query_items(
                query_str=query_str,
                job=job,
                base_currency=base_currency,
            )

            for item in items:
                if item.external_id in deduped_by_id:
                    continue

                if item.full_description is None:
                    cached_desc = await self.get_cached_description(
                        item.external_id, session=session
                    )
                    if cached_desc is not None:
                        item.full_description = cached_desc

                stage1 = listing_matcher.evaluate_stage1(item, job, base_currency=base_currency)
                if (
                    stage1.passed_stage1
                    and stage1.needs_detail_fetch
                    and item.full_description is None
                ):
                    await self.sleep_jitter()
                    full_desc = await self._fetch_item_detail(item.external_id)
                    if full_desc is not None:
                        item.full_description = full_desc
                        self.remember_description_in_memory(
                            self.platform_name, item.external_id, full_desc
                        )

                deduped_by_id[item.external_id] = item

        return list(deduped_by_id.values())

    async def _fetch_query_items(
        self,
        query_str: str,
        job: SearchJob,
        base_currency: str,
    ) -> list[ScrapedListing]:
        params: dict[str, str | int | float] = {
            "phrase": query_str,
            "limit": 40,
            "offset": 0,
            "sort": "-create_date",
        }

        if not job.allow_unpriced:
            min_czk = int(
                max(
                    0.0,
                    currency_service.convert(job.min_price, base_currency, "CZK")
                    - job.shipping_buffer,
                )
            )
            max_czk = int(max(1.0, currency_service.convert(job.max_price, base_currency, "CZK")))
            if min_czk > 0:
                params["price_from"] = min_czk
            if max_czk > 0:
                params["price_to"] = max_czk

        headers = self.build_browser_headers(
            referer=f"{SBAZAR_BASE_URL}/hledej/{query_str}",
            accept="application/json, text/plain, */*",
        )
        response = await self.request("GET", SBAZAR_SEARCH_API_URL, params=params, headers=headers)

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ScraperParseError(
                self.platform_name, f"Invalid JSON from Sbazar search API: {exc}"
            ) from exc

        if "results" not in payload or not isinstance(payload["results"], list):
            raise ScraperParseError(
                self.platform_name,
                "Sbazar API payload missing 'results' array",
            )

        listings: list[ScrapedListing] = []
        for raw_item in payload["results"]:
            if not isinstance(raw_item, dict):
                continue
            parsed = self._parse_api_item(raw_item)
            if parsed is not None:
                listings.append(parsed)

        return listings

    def _parse_api_item(self, data: dict[str, Any]) -> Optional[ScrapedListing]:
        raw_id = data.get("id")
        if raw_id is None:
            return None
        external_id = str(raw_id).strip()

        title = str(data.get("name") or "").strip()
        if not title:
            return None

        price_by_agreement = bool(data.get("price_by_agreement", False))
        raw_price_val = data.get("price")
        raw_price: Optional[float] = None
        is_unpriced = price_by_agreement

        if not price_by_agreement and raw_price_val is not None:
            try:
                parsed_price = float(raw_price_val)
                if parsed_price > 0:
                    raw_price = parsed_price
                else:
                    is_unpriced = True
            except (TypeError, ValueError):
                is_unpriced = True
        else:
            is_unpriced = True

        url = self._build_listing_url(data, external_id)
        image_url = self._extract_image_url(data)
        location = self._extract_location(data)

        description_field = str(data.get("description") or data.get("perex") or "").strip()
        # Sbazar search endpoint often includes `description` or `perex`; if `description` is
        # explicitly present in the item object, store it on `full_description` as well.
        full_desc: Optional[str] = (
            str(data["description"]).strip()
            if isinstance(data.get("description"), str) and data.get("description")
            else None
        )

        return ScrapedListing(
            platform=self.platform_name,
            external_id=external_id,
            title=title,
            url=url,
            raw_price=raw_price,
            raw_currency="CZK",
            shipping_price=None,
            shipping_currency=None,
            is_unpriced=is_unpriced,
            snippet=description_field,
            full_description=full_desc,
            image_url=image_url,
            location=location,
        )

    @staticmethod
    def _build_listing_url(data: dict[str, Any], external_id: str) -> str:
        seo_name = str(data.get("seo_name") or "inzerat").strip()
        user_data = data.get("user")
        shop_slug = ""
        if isinstance(user_data, dict):
            user_service = user_data.get("user_service")
            if isinstance(user_service, dict):
                shop_slug = str(user_service.get("shop_url") or "").strip()

        if not shop_slug:
            category = data.get("category")
            if isinstance(category, dict):
                shop_slug = str(category.get("seo_name") or "inzerat").strip()

        slug_prefix = shop_slug if shop_slug else "inzerat"
        return f"{SBAZAR_BASE_URL}/{slug_prefix}/detail/{external_id}-{seo_name}"

    @staticmethod
    def _extract_image_url(data: dict[str, Any]) -> Optional[str]:
        images = data.get("images")
        if not isinstance(images, list) or not images:
            return None

        first_img = images[0]
        if not isinstance(first_img, dict):
            return None

        raw_url = str(first_img.get("url") or "").strip()
        if not raw_url:
            return None

        if raw_url.startswith("//"):
            raw_url = f"https:{raw_url}"
        elif raw_url.startswith("/"):
            raw_url = f"{SBAZAR_BASE_URL}{raw_url}"

        # Append standard Sbazar CDN resize suffix if URL ends with a bare path
        if "sdn.cz" in raw_url and not raw_url.endswith((".jpg", ".jpeg", ".png", ".webp")):
            raw_url = f"{raw_url}?fl=exf|res,400,400,1|jpg,80,,1"
        return raw_url

    @staticmethod
    def _extract_location(data: dict[str, Any]) -> Optional[str]:
        locality = data.get("locality")
        if isinstance(locality, dict):
            municipality = str(locality.get("municipality") or "").strip()
            district = str(locality.get("district") or "").strip()
            if municipality and district and municipality != district:
                return f"{municipality} ({district})"
            if municipality:
                return municipality
            if district:
                return district
        return None

    async def _fetch_item_detail(self, external_id: str) -> Optional[str]:
        url = SBAZAR_ITEM_API_URL.format(item_id=external_id)
        headers = self.build_browser_headers(
            referer=SBAZAR_BASE_URL,
            accept="application/json, text/plain, */*",
        )
        try:
            response = await self.request("GET", url, headers=headers)
            payload: dict[str, Any] = response.json()
            result_obj = payload.get("result")
            if isinstance(result_obj, dict):
                desc = str(result_obj.get("description") or "").strip()
                if desc:
                    return desc
            return None
        except Exception as exc:
            logger.warning("[sbazar] Failed to fetch item detail %s: %s", external_id, exc)
            return None