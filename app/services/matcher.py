import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional

from app.config import get_settings
from app.models import SearchJob, SeenListing
from app.services.currency import CurrencyService, currency_service

settings = get_settings()

UNPRICED_TEXT_MARKERS: tuple[str, ...] = (
    "dohodou",
    "v textu",
    "nabídněte",
    "nabidnete",
    "zdarma",
    "info v textu",
)

PICKUP_ONLY_PATTERNS: tuple[str, ...] = (
    "pouze osobní",
    "pouze osobni",
    "osobní předání",
    "osobni predani",
    "osobní odběr",
    "osobni odber",
    "neposílám",
    "neposilam",
    "poštou ne",
    "postou ne",
    "jen osobně",
    "jen osobne",
    "pouze osobně",
    "pouze osobne",
)


class AlertType(StrEnum):
    NONE = "none"
    NEW_DEAL = "new_deal"
    UNPRICED_DEAL = "unpriced_deal"
    PRICE_DROP = "price_drop"


@dataclass(slots=True)
class ScrapedListing:
    platform: str
    external_id: str
    title: str
    url: str
    raw_price: Optional[float] = None
    raw_currency: str = "CZK"
    shipping_price: Optional[float] = None
    shipping_currency: Optional[str] = None
    is_unpriced: bool = False
    snippet: str = ""
    full_description: Optional[str] = None
    image_url: Optional[str] = None
    location: Optional[str] = None
    condition: Optional[str] = None
    seller_feedback_score: Optional[float] = None


@dataclass(slots=True)
class PriceDropInfo:
    old_price_base: float
    new_price_base: float
    drop_amount_base: float
    drop_percent: float
    base_currency: str


@dataclass(slots=True)
class Stage1Result:
    passed_stage1: bool
    needs_detail_fetch: bool
    is_unpriced: bool
    total_price_base: Optional[float]
    rejection_reason: Optional[str] = None


@dataclass(slots=True)
class FinalMatchResult:
    matched: bool
    alert_type: AlertType
    is_unpriced: bool
    total_price_base: Optional[float]
    item_price_base: Optional[float]
    shipping_price_base: float
    base_currency: str
    tag_label: Optional[str] = None
    price_drop: Optional[PriceDropInfo] = None
    rejection_reason: Optional[str] = None


def strip_diacritics(text: str) -> str:
    """Normalizes Czech/European characters to ASCII lowercase for resilient keyword matching."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c)).lower()


def is_listing_unpriced(raw_price: Optional[float], price_text: Optional[str] = None) -> bool:
    if raw_price is None or raw_price <= 0:
        return True
    if price_text:
        lowered = price_text.strip().lower()
        if any(marker in lowered for marker in UNPRICED_TEXT_MARKERS):
            return True
    return False


class KeywordEvaluator:
    """
    Evaluates comma-separated AND groups with pipe-separated (`|`) OR alternatives.
    Supports both literal substring/pattern matching (diacritic-insensitive) and explicit
    regex patterns prefixed with `re:`.
    Example required_keywords: "16gb|32gb, oled|ips, ssd"
    """

    @staticmethod
    def parse_and_or_groups(raw_keywords: str) -> list[list[str]]:
        if not raw_keywords or not raw_keywords.strip():
            return []
        and_groups: list[list[str]] = []
        for group_str in raw_keywords.split(","):
            group_str = group_str.strip()
            if not group_str:
                continue
            or_terms = [term.strip() for term in group_str.split("|") if term.strip()]
            if or_terms:
                and_groups.append(or_terms)
        return and_groups

    @staticmethod
    def parse_flat_terms(raw_keywords: str) -> list[str]:
        if not raw_keywords or not raw_keywords.strip():
            return []
        terms: list[str] = []
        for chunk in raw_keywords.split(","):
            for sub in chunk.split("|"):
                cleaned = sub.strip()
                if cleaned:
                    terms.append(cleaned)
        return terms

    @classmethod
    def term_matches(cls, term: str, raw_text_lower: str, normalized_text: str) -> bool:
        if term.startswith("re:"):
            pattern = term[3:].strip()
            try:
                return bool(
                    re.search(pattern, raw_text_lower, flags=re.IGNORECASE)
                    or re.search(pattern, normalized_text, flags=re.IGNORECASE)
                )
            except re.error:
                return False

        # Support regex wildcards or boundary expressions if user wrote regex syntax
        has_regex_meta = any(ch in term for ch in (r"\b", ".*", "^", "$", "[", "]", "(", ")"))
        if has_regex_meta:
            try:
                return bool(
                    re.search(term, raw_text_lower, flags=re.IGNORECASE)
                    or re.search(
                        strip_diacritics(term), normalized_text, flags=re.IGNORECASE
                    )
                )
            except re.error:
                pass

        norm_term = strip_diacritics(term)
        return (term.lower() in raw_text_lower) or (norm_term in normalized_text)

    @classmethod
    def matches_all_required(cls, required_keywords: str, text: str) -> bool:
        groups = cls.parse_and_or_groups(required_keywords)
        if not groups:
            return True
        raw_lower = (text or "").lower()
        norm_text = strip_diacritics(text or "")
        for or_group in groups:
            if not any(cls.term_matches(term, raw_lower, norm_text) for term in or_group):
                return False
        return True

    @classmethod
    def matches_any_excluded(cls, excluded_keywords: str, text: str) -> Optional[str]:
        terms = cls.parse_flat_terms(excluded_keywords)
        if not terms:
            return None
        raw_lower = (text or "").lower()
        norm_text = strip_diacritics(text or "")
        for term in terms:
            if cls.term_matches(term, raw_lower, norm_text):
                return term
        return None

    @classmethod
    def matches_pickup_only(cls, text: str) -> Optional[str]:
        raw_lower = (text or "").lower()
        norm_text = strip_diacritics(text or "")
        for phrase in PICKUP_ONLY_PATTERNS:
            if phrase in raw_lower or strip_diacritics(phrase) in norm_text:
                return phrase
        return None


class ListingMatcher:
    """
    Executes two-stage filtering, currency normalization, shipping calculation,
    unpriced ('Dohodou') validation, and price drop computation.
    """

    def __init__(self, fx_service: CurrencyService = currency_service) -> None:
        self.fx = fx_service

    def calculate_pricing(
        self,
        listing: ScrapedListing,
        job: SearchJob,
        base_currency: str = "CZK",
    ) -> tuple[bool, Optional[float], Optional[float], float]:
        """
        Returns (is_unpriced, total_price_base, item_price_base, shipping_price_base).
        - Structured shipping (e.g., eBay): (item_price + api_shipping) converted to base_currency.
        - Unstructured shipping (Bazoš, Sbazar): item_price converted to base_currency + job.shipping_buffer.
        """
        unpriced = listing.is_unpriced or is_listing_unpriced(listing.raw_price)
        if unpriced or listing.raw_price is None:
            return True, None, None, 0.0

        item_base = self.fx.convert(listing.raw_price, listing.raw_currency, base_currency)

        if listing.shipping_price is not None:
            ship_curr = listing.shipping_currency or listing.raw_currency
            shipping_base = self.fx.convert(listing.shipping_price, ship_curr, base_currency)
        else:
            # Unstructured shipping: job.shipping_buffer is already denominated in user's base_currency
            shipping_base = round(max(0.0, float(job.shipping_buffer)), 2)

        total_base = round(item_base + shipping_base, 2)
        return False, total_base, item_base, shipping_base

    def evaluate_stage1(
        self,
        listing: ScrapedListing,
        job: SearchJob,
        base_currency: str = "CZK",
    ) -> Stage1Result:
        """
        Stage 1: Evaluates price boundaries and snippet text BEFORE making an expensive
        detail-page HTTP GET request (used primarily by BazosScraper).
        """
        unpriced, total_base, _, _ = self.calculate_pricing(listing, job, base_currency)

        # 1. Price / Unpriced check
        if unpriced:
            if not job.allow_unpriced:
                return Stage1Result(
                    passed_stage1=False,
                    needs_detail_fetch=False,
                    is_unpriced=True,
                    total_price_base=None,
                    rejection_reason="Unpriced listing ('Dohodou') disallowed by job configuration",
                )
        else:
            assert total_base is not None
            if total_base < job.min_price:
                return Stage1Result(
                    passed_stage1=False,
                    needs_detail_fetch=False,
                    is_unpriced=False,
                    total_price_base=total_base,
                    rejection_reason=f"Total price {total_base} {base_currency} below min_price {job.min_price}",
                )
            if total_base > job.max_price:
                return Stage1Result(
                    passed_stage1=False,
                    needs_detail_fetch=False,
                    is_unpriced=False,
                    total_price_base=total_base,
                    rejection_reason=f"Total price {total_base} {base_currency} above max_price {job.max_price}",
                )

        # 2. Immediate negative checks on Title + Snippet
        initial_text = f"{listing.title}\n{listing.snippet or ''}"
        if listing.full_description:
            initial_text = f"{listing.title}\n{listing.full_description}"

        matched_excluded = KeywordEvaluator.matches_any_excluded(
            job.excluded_keywords, initial_text
        )
        if matched_excluded:
            return Stage1Result(
                passed_stage1=False,
                needs_detail_fetch=False,
                is_unpriced=unpriced,
                total_price_base=total_base,
                rejection_reason=f"Excluded keyword '{matched_excluded}' found in snippet/title",
            )

        if job.require_shipping:
            pickup_phrase = KeywordEvaluator.matches_pickup_only(initial_text)
            if pickup_phrase:
                return Stage1Result(
                    passed_stage1=False,
                    needs_detail_fetch=False,
                    is_unpriced=unpriced,
                    total_price_base=total_base,
                    rejection_reason=f"Pickup-only phrase '{pickup_phrase}' found in snippet/title",
                )

        # 3. Determine whether a lazy secondary detail fetch is required
        if listing.full_description is not None:
            return Stage1Result(
                passed_stage1=True,
                needs_detail_fetch=False,
                is_unpriced=unpriced,
                total_price_base=total_base,
            )

        has_required_rules = bool(job.required_keywords and job.required_keywords.strip())
        has_excluded_rules = bool(job.excluded_keywords and job.excluded_keywords.strip())
        snippet_satisfies_required = KeywordEvaluator.matches_all_required(
            job.required_keywords, initial_text
        )

        needs_detail = (
            unpriced
            or (has_required_rules and not snippet_satisfies_required)
            or has_excluded_rules
            or job.require_shipping
        )

        return Stage1Result(
            passed_stage1=True,
            needs_detail_fetch=needs_detail,
            is_unpriced=unpriced,
            total_price_base=total_base,
        )

    def evaluate_full(
        self,
        listing: ScrapedListing,
        job: SearchJob,
        base_currency: str = "CZK",
        existing_seen: Optional[SeenListing] = None,
    ) -> FinalMatchResult:
        """
        Stage 2: Evaluates full text (title + full_description/snippet), enforces strict
        'Dohodou' rules, and checks state deduplication or price drops against `existing_seen`.
        """
        unpriced, total_base, item_base, shipping_base = self.calculate_pricing(
            listing, job, base_currency
        )

        if unpriced:
            if not job.allow_unpriced:
                return FinalMatchResult(
                    matched=False,
                    alert_type=AlertType.NONE,
                    is_unpriced=True,
                    total_price_base=None,
                    item_price_base=None,
                    shipping_price_base=0.0,
                    base_currency=base_currency,
                    rejection_reason="Unpriced listing disallowed",
                )
        else:
            assert total_base is not None
            if not (job.min_price <= total_base <= job.max_price):
                return FinalMatchResult(
                    matched=False,
                    alert_type=AlertType.NONE,
                    is_unpriced=False,
                    total_price_base=total_base,
                    item_price_base=item_base,
                    shipping_price_base=shipping_base,
                    base_currency=base_currency,
                    rejection_reason="Total price out of job budget bounds",
                )

        body_text = listing.full_description if listing.full_description is not None else listing.snippet
        full_text = f"{listing.title}\n{body_text or ''}"

        # 1. Excluded keywords check on full text
        matched_excluded = KeywordEvaluator.matches_any_excluded(job.excluded_keywords, full_text)
        if matched_excluded:
            return FinalMatchResult(
                matched=False,
                alert_type=AlertType.NONE,
                is_unpriced=unpriced,
                total_price_base=total_base,
                item_price_base=item_base,
                shipping_price_base=shipping_base,
                base_currency=base_currency,
                rejection_reason=f"Excluded keyword '{matched_excluded}' matched in full text",
            )

        # 2. Require shipping check on full text
        if job.require_shipping:
            pickup_phrase = KeywordEvaluator.matches_pickup_only(full_text)
            if pickup_phrase:
                return FinalMatchResult(
                    matched=False,
                    alert_type=AlertType.NONE,
                    is_unpriced=unpriced,
                    total_price_base=total_base,
                    item_price_base=item_base,
                    shipping_price_base=shipping_base,
                    base_currency=base_currency,
                    rejection_reason=f"Pickup-only phrase '{pickup_phrase}' matched in full text",
                )

        # 3. Required keywords check on full text
        if not KeywordEvaluator.matches_all_required(job.required_keywords, full_text):
            return FinalMatchResult(
                matched=False,
                alert_type=AlertType.NONE,
                is_unpriced=unpriced,
                total_price_base=total_base,
                item_price_base=item_base,
                shipping_price_base=shipping_base,
                base_currency=base_currency,
                rejection_reason="Missing one or more required keywords",
            )

        # 4. Strict verification for Unpriced / Dohodou items
        if unpriced:
            # If user left `required_keywords` blank, still verify that at least one search query
            # token appears in the title/description so irrelevant unpriced ads never leak through.
            if not (job.required_keywords and job.required_keywords.strip()):
                query_groups = " | ".join(q.strip() for q in job.search_queries if q.strip())
                if query_groups and not KeywordEvaluator.matches_all_required(query_groups, full_text):
                    return FinalMatchResult(
                        matched=False,
                        alert_type=AlertType.NONE,
                        is_unpriced=True,
                        total_price_base=None,
                        item_price_base=None,
                        shipping_price_base=0.0,
                        base_currency=base_currency,
                        rejection_reason="Unpriced listing failed strict positive query match",
                    )

            if existing_seen is not None and existing_seen.notified:
                return FinalMatchResult(
                    matched=True,
                    alert_type=AlertType.NONE,
                    is_unpriced=True,
                    total_price_base=None,
                    item_price_base=None,
                    shipping_price_base=0.0,
                    base_currency=base_currency,
                    tag_label="[UNPRICED / DOHODOU]",
                    rejection_reason="Already notified unpriced listing",
                )

            return FinalMatchResult(
                matched=True,
                alert_type=AlertType.UNPRICED_DEAL,
                is_unpriced=True,
                total_price_base=None,
                item_price_base=None,
                shipping_price_base=0.0,
                base_currency=base_currency,
                tag_label="[UNPRICED / DOHODOU]",
            )

        # 5. Priced Listing: Deduplication & Price Drop evaluation
        assert total_base is not None
        if existing_seen is None or not existing_seen.notified:
            return FinalMatchResult(
                matched=True,
                alert_type=AlertType.NEW_DEAL,
                is_unpriced=False,
                total_price_base=total_base,
                item_price_base=item_base,
                shipping_price_base=shipping_base,
                base_currency=base_currency,
            )

        # Evaluate Price Drop on previously notified listing
        last_notified = existing_seen.last_notified_price
        if last_notified is not None and total_base < last_notified:
            # Guard against false price drops caused purely by daily FX rate fluctuations
            raw_dropped = (
                existing_seen.raw_price is None
                or listing.raw_price is None
                or listing.raw_price < existing_seen.raw_price
            )
            if raw_dropped:
                drop_amount = round(last_notified - total_base, 2)
                drop_pct = round((drop_amount / last_notified) * 100.0, 1) if last_notified > 0 else 0.0
                min_abs_threshold = self.fx.convert(
                    settings.PRICE_DROP_MIN_ABSOLUTE_CZK, "CZK", base_currency
                )

                if drop_pct >= settings.PRICE_DROP_MIN_PERCENT or drop_amount >= min_abs_threshold:
                    return FinalMatchResult(
                        matched=True,
                        alert_type=AlertType.PRICE_DROP,
                        is_unpriced=False,
                        total_price_base=total_base,
                        item_price_base=item_base,
                        shipping_price_base=shipping_base,
                        base_currency=base_currency,
                        price_drop=PriceDropInfo(
                            old_price_base=round(last_notified, 2),
                            new_price_base=total_base,
                            drop_amount_base=drop_amount,
                            drop_percent=drop_pct,
                            base_currency=base_currency,
                        ),
                    )

        return FinalMatchResult(
            matched=True,
            alert_type=AlertType.NONE,
            is_unpriced=False,
            total_price_base=total_base,
            item_price_base=item_base,
            shipping_price_base=shipping_base,
            base_currency=base_currency,
            rejection_reason="Already notified and no qualifying price drop",
        )


listing_matcher = ListingMatcher()