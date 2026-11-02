"""Parsing primitives for user-configured marketplace sources.

Browser access is mediated by the public-page renderer; extraction is shared
between setup previews and scheduled scans.
"""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from app.services.matcher import ScrapedListing
from app.scrapers.base import BaseScraper


class CustomScraper(BaseScraper):
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.platform_name = f"custom:{source.id}"

    async def search(self, job, session, base_currency="CZK"):
        from app.services.site_renderer import render_site
        results = {}
        for query in job.search_queries:
            page = await render_site(search_url(self.source.search_url, self.source.search_parameter, query))
            for listing in extract_listings(page["html"], page["url"], self.platform_name, self.source.currency, self.source.selectors):
                results[listing.external_id] = listing
        return list(results.values())


def search_url(template: str, parameter: str, query: str) -> str:
    parts = urlsplit(template)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Enter a public HTTP or HTTPS search URL without credentials.")
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if sum(key == parameter for key, _ in pairs) != 1:
        raise ValueError("The search parameter must appear exactly once in the URL.")
    pairs = [(key, query if key == parameter else value) for key, value in pairs]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))


def _absolute_url(base: str, value: str) -> str | None:
    url = urljoin(base, value.strip())
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _price(text: str) -> float | None:
    match = re.search(r"\d[\d\s\u00a0.,]*", text)
    if not match:
        return None
    value = re.sub(r"\s", "", match.group())
    if "." in value and "," in value:
        decimal = "." if value.rfind(".") > value.rfind(",") else ","
        value = value.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif "," in value or "." in value:
        separator = "," if "," in value else "."
        chunks = value.split(separator)
        value = "".join(chunks) if len(chunks[-1]) == 3 else "".join(chunks[:-1]) + "." + chunks[-1]
    try:
        amount = float(value)
        return amount if amount > 0 else None
    except ValueError:
        return None


def extract_listings(html: str, page_url: str, platform: str, currency: str,
                     selectors: dict[str, str]) -> list[ScrapedListing]:
    if not all(selectors.get(field) for field in ("card", "title", "link")):
        raise ValueError("Select a listing card, title, and link.")
    try:
        cards = LexborHTMLParser(html).css(selectors["card"])
        listings = []
        seen = set()
        for card in cards:
            def node(field: str):
                selector = selectors.get(field)
                return card.css_first(selector) if selector else None

            title_node, link_node = node("title"), node("link")
            title = title_node.text(separator=" ", strip=True) if title_node else ""
            href = link_node.attributes.get("href", "") if link_node else ""
            url = _absolute_url(page_url, href) if href else None
            if not title or not url or url in seen:
                continue
            seen.add(url)
            price_node, image_node = node("price"), node("image")
            location_node, description_node = node("location"), node("description")
            price = _price(price_node.text(separator=" ", strip=True)) if price_node else None
            image = image_node.attributes.get("src", "") if image_node else ""
            listings.append(ScrapedListing(
                platform=platform, external_id=hashlib.sha256(url.encode()).hexdigest(),
                title=title[:512], url=url[:1024], raw_price=price, raw_currency=currency,
                is_unpriced=price is None, image_url=_absolute_url(page_url, image) if image else None,
                location=location_node.text(separator=" ", strip=True)[:255] if location_node else None,
                snippet=description_node.text(separator=" ", strip=True) if description_node else "",
            ))
    except (ValueError, TypeError) as exc:
        raise ValueError("A selected field no longer matches the page; retest the source.") from exc
    if not listings:
        raise ValueError("No usable listings found. Check the selected fields or whether the site blocks access.")
    return listings
