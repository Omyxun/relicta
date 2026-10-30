import logging
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ExchangeRate

logger = logging.getLogger(__name__)
settings = get_settings()

# Realistic fallback rates to CZK in case the initial boot has no internet access
DEFAULT_FALLBACK_RATES_TO_CZK: dict[str, float] = {
    "CZK": 1.0,
    "EUR": 25.30,
    "USD": 23.40,
    "GBP": 30.10,
    "PLN": 5.85,
}


class CurrencyService:
    """
    Manages FX rates anchored to CZK using the Czech National Bank (ČNB) daily feed
    with automatic fallback to the ECB/Frankfurter JSON API.
    Caches rates in memory and persists them in SQLite.
    """

    def __init__(self) -> None:
        self._rates_to_czk: dict[str, float] = dict(DEFAULT_FALLBACK_RATES_TO_CZK)
        self._last_loaded_at: Optional[datetime] = None

    async def load_from_db(self, session: AsyncSession) -> dict[str, float]:
        stmt = select(ExchangeRate)
        result = await session.execute(stmt)
        rows = result.scalars().all()

        if not rows:
            await self._seed_defaults(session)
            return dict(self._rates_to_czk)

        latest_update: Optional[datetime] = None
        for row in rows:
            code = row.currency_code.upper()
            self._rates_to_czk[code] = float(row.rate_to_czk)
            row_updated = row.updated_at
            if row_updated.tzinfo is None:
                row_updated = row_updated.replace(tzinfo=timezone.utc)
            if latest_update is None or row_updated > latest_update:
                latest_update = row_updated

        self._rates_to_czk["CZK"] = 1.0
        self._last_loaded_at = latest_update
        return dict(self._rates_to_czk)

    async def _seed_defaults(self, session: AsyncSession) -> None:
        now = datetime.now(timezone.utc)
        for code, rate in DEFAULT_FALLBACK_RATES_TO_CZK.items():
            existing = await session.get(ExchangeRate, code)
            if not existing:
                session.add(ExchangeRate(currency_code=code, rate_to_czk=rate, updated_at=now))
        await session.commit()

    def needs_refresh(self) -> bool:
        if self._last_loaded_at is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self._last_loaded_at).total_seconds()
        return elapsed >= settings.FX_REFRESH_INTERVAL_SECONDS

    async def refresh_rates(
        self,
        session: AsyncSession,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> dict[str, float]:
        """
        Fetches latest FX rates from ČNB daily text feed, falling back to ECB JSON feed.
        Persists updated rates to SQLite and updates the in-memory cache.
        """
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient(timeout=15.0, follow_redirects=True)

        try:
            parsed_rates = await self._fetch_cnb_rates(client)
            if not parsed_rates:
                logger.warning("ČNB FX fetch returned no rates; attempting ECB fallback.")
                parsed_rates = await self._fetch_ecb_fallback_rates(client)

            if parsed_rates:
                parsed_rates["CZK"] = 1.0
                now = datetime.now(timezone.utc)
                for code, rate_to_czk in parsed_rates.items():
                    existing = await session.get(ExchangeRate, code)
                    if existing:
                        existing.rate_to_czk = rate_to_czk
                        existing.updated_at = now
                    else:
                        session.add(
                            ExchangeRate(
                                currency_code=code,
                                rate_to_czk=rate_to_czk,
                                updated_at=now,
                            )
                        )
                    self._rates_to_czk[code] = rate_to_czk

                await session.commit()
                self._last_loaded_at = now
                logger.info("Updated FX rates (to CZK): %s", self._rates_to_czk)
            else:
                logger.error("Both ČNB and ECB FX feeds failed; retaining cached/fallback rates.")
                await self.load_from_db(session)
        finally:
            if owns_client:
                await client.aclose()

        return dict(self._rates_to_czk)

    async def _fetch_cnb_rates(self, client: httpx.AsyncClient) -> dict[str, float]:
        """
        Parses ČNB denni_kurz.txt format:
        Line 1: 26.10.2026 #207
        Line 2: země|měna|množství|kód|kurz
        Line 3+: EMU|euro|1|EUR|25,345
        """
        try:
            resp = await client.get(settings.CNB_RATES_URL)
            resp.raise_for_status()
            lines = [line.strip() for line in resp.text.splitlines() if line.strip()]
            if len(lines) < 3:
                return {}

            rates: dict[str, float] = {}
            for line in lines[2:]:
                parts = line.split("|")
                if len(parts) != 5:
                    continue
                _, _, amount_str, code, rate_str = parts
                code = code.strip().upper()
                if code not in settings.SUPPORTED_CURRENCIES:
                    continue
                amount = float(amount_str.replace(",", "."))
                rate = float(rate_str.replace(",", "."))
                if amount > 0:
                    rates[code] = round(rate / amount, 6)
            return rates
        except Exception as exc:
            logger.warning("Failed to fetch or parse ČNB FX rates: %s", exc)
            return {}

    async def _fetch_ecb_fallback_rates(self, client: httpx.AsyncClient) -> dict[str, float]:
        """
        Parses Frankfurter (ECB) JSON response anchored to EUR:
        {"amount":1.0,"base":"EUR","date":"...","rates":{"CZK":25.3,"GBP":0.84,"PLN":4.32,"USD":1.08}}
        """
        try:
            resp = await client.get(settings.ECB_FALLBACK_URL)
            resp.raise_for_status()
            data = resp.json()
            eur_rates: dict[str, float] = data.get("rates", {})
            eur_to_czk = float(eur_rates.get("CZK", 0.0))
            if eur_to_czk <= 0:
                return {}

            rates: dict[str, float] = {"EUR": round(eur_to_czk, 6), "CZK": 1.0}
            for code in ("USD", "GBP", "PLN"):
                eur_to_target = float(eur_rates.get(code, 0.0))
                if eur_to_target > 0:
                    rates[code] = round(eur_to_czk / eur_to_target, 6)
            return rates
        except Exception as exc:
            logger.warning("Failed to fetch or parse ECB fallback rates: %s", exc)
            return {}

    def get_rate_to_czk(self, currency_code: str) -> float:
        code = (currency_code or "CZK").strip().upper()
        return self._rates_to_czk.get(code, DEFAULT_FALLBACK_RATES_TO_CZK.get(code, 1.0))

    def convert(
        self,
        amount: float,
        from_currency: str,
        to_currency: str = "CZK",
    ) -> float:
        """
        Converts an amount from `from_currency` into `to_currency` using CZK as the pivot.
        """
        src = (from_currency or "CZK").strip().upper()
        dst = (to_currency or "CZK").strip().upper()
        if src == dst:
            return round(amount, 2)

        amount_in_czk = amount * self.get_rate_to_czk(src)
        if dst == "CZK":
            return round(amount_in_czk, 2)

        dst_rate_to_czk = self.get_rate_to_czk(dst)
        if dst_rate_to_czk <= 0:
            return round(amount_in_czk, 2)
        return round(amount_in_czk / dst_rate_to_czk, 2)

    def get_all_rates(self) -> dict[str, float]:
        return dict(self._rates_to_czk)

    def get_rates_to_base(self, base_currency: str) -> dict[str, str]:
        base = (base_currency or 'CZK').strip().upper()
        rates: dict[str, str] = {}
        for code in settings.SUPPORTED_CURRENCIES:
            if code == base:
                continue
            val = self.convert(1.0, from_currency=code, to_currency=base)
            if val >= 100:
                formatted = f'{val:.1f}'
            elif val >= 1:
                formatted = f'{val:.3f}'
            else:
                formatted = f'{val:.4f}'
            rates[code] = formatted
        return rates



currency_service = CurrencyService()