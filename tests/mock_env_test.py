import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

class SubscriptableMock:
    def __class_getitem__(cls, item):
        return cls

# Create module mocks
for mod_name in [
    "httpx",
    "sqlalchemy",
    "sqlalchemy.engine",
    "sqlalchemy.orm",
    "sqlalchemy.ext",
    "sqlalchemy.ext.asyncio",
    "selectolax",
    "selectolax.lexbor",
    "pydantic",
    "pydantic_settings",
]:
    m = types.ModuleType(mod_name)
    m.__dict__["AsyncEngine"] = MagicMock
    m.__dict__["AsyncSession"] = MagicMock
    m.__dict__["AsyncClient"] = MagicMock
    m.__dict__["Limits"] = MagicMock
    m.__dict__["Timeout"] = MagicMock
    m.__dict__["Response"] = MagicMock
    m.__dict__["RequestError"] = Exception
    m.__dict__["TimeoutException"] = Exception
    m.__dict__["select"] = MagicMock
    m.__dict__["Engine"] = MagicMock
    m.__dict__["JSON"] = lambda *a, **kw: None
    m.__dict__["Boolean"] = lambda *a, **kw: None
    m.__dict__["DateTime"] = lambda *a, **kw: None
    m.__dict__["Float"] = lambda *a, **kw: None
    m.__dict__["ForeignKey"] = lambda *a, **kw: None
    m.__dict__["Index"] = lambda *a, **kw: None
    m.__dict__["Integer"] = lambda *a, **kw: None
    m.__dict__["String"] = lambda *a, **kw: None
    m.__dict__["Text"] = lambda *a, **kw: None
    m.__dict__["UniqueConstraint"] = lambda *a, **kw: None
    m.__dict__["Mapped"] = SubscriptableMock
    m.__dict__["mapped_column"] = lambda *a, **kw: None
    m.__dict__["relationship"] = lambda *a, **kw: None
    event_mock = MagicMock()
    event_mock.listens_for = lambda *a, **kw: (lambda f: f)
    m.__dict__["event"] = event_mock
    m.__dict__["async_sessionmaker"] = MagicMock
    engine_mock = MagicMock()
    engine_mock.sync_engine = MagicMock()
    m.__dict__["create_async_engine"] = lambda *a, **kw: engine_mock
    m.__dict__["DeclarativeBase"] = type("DeclarativeBase", (), {})
    m.__dict__["BaseSettings"] = type("BaseSettings", (), {})
    m.__dict__["SettingsConfigDict"] = MagicMock
    m.__dict__["Field"] = MagicMock
    m.__dict__["field_validator"] = lambda *a, **kw: (lambda f: f)
    m.__dict__["LexborHTMLParser"] = MagicMock
    m.__dict__["LexborNode"] = MagicMock
    sys.modules[mod_name] = m

class DummySettings:
    DATABASE_URL = "sqlite+aiosqlite:///./data/relicta.db"
    SCRAPER_REQUEST_TIMEOUT_SECONDS = 20.0
    SCRAPER_MIN_DELAY_SECONDS = 3.0
    SCRAPER_MAX_DELAY_SECONDS = 7.0
    PRICE_DROP_MIN_PERCENT = 1.0
    PRICE_DROP_MIN_ABSOLUTE_CZK = 50.0
    DEFAULT_BASE_CURRENCY = "CZK"
    SUPPORTED_CURRENCIES = ("CZK", "EUR", "USD", "GBP", "PLN")
    CNB_RATES_URL = "http://example.com"
    ECB_FALLBACK_URL = "http://example.com"
    FX_REFRESH_INTERVAL_SECONDS = 86400
    FB_COOKIE_C_USER = "100012345"
    FB_COOKIE_XS = "abc%3Axyz"
    FB_COOKIE_DATR = "datr_tok"
    FB_MARKETPLACE_LOCATION = "prague"

sys.modules["app.config"] = types.ModuleType("app.config")
sys.modules["app.config"].get_settings = lambda: DummySettings()

from app.scrapers import get_all_scrapers, get_scraper
from app.scrapers.facebook import (
    FacebookMarketplaceScraper,
    FB_ITEM_ID_RE,
    FB_PRICE_AMOUNT_RE,
)

class TestFacebookScraperIsolated(unittest.TestCase):
    def setUp(self):
        self.scraper = FacebookMarketplaceScraper()

    def test_scraper_registered(self):
        scraper = get_scraper("facebook")
        self.assertIsNotNone(scraper)
        self.assertEqual(scraper.platform_name, "facebook")
        all_scrapers = get_all_scrapers()
        self.assertIn("facebook", all_scrapers)

    def test_price_parsing_czk(self):
        price, currency = self.scraper._parse_formatted_price("1 500 Kč")
        self.assertEqual(price, 1500.0)
        self.assertEqual(currency, "CZK")

    def test_price_parsing_czk_no_space(self):
        price, currency = self.scraper._parse_formatted_price("25000Kč")
        self.assertEqual(price, 25000.0)
        self.assertEqual(currency, "CZK")

    def test_price_parsing_eur_comma(self):
        price, currency = self.scraper._parse_formatted_price("€120,50")
        self.assertEqual(price, 120.50)
        self.assertEqual(currency, "EUR")

    def test_price_parsing_usd(self):
        price, currency = self.scraper._parse_formatted_price("$250")
        self.assertEqual(price, 250.0)
        self.assertEqual(currency, "USD")

    def test_price_parsing_gbp(self):
        price, currency = self.scraper._parse_formatted_price("£99.99")
        self.assertEqual(price, 99.99)
        self.assertEqual(currency, "GBP")

    def test_price_parsing_pln(self):
        price, currency = self.scraper._parse_formatted_price("350 zł")
        self.assertEqual(price, 350.0)
        self.assertEqual(currency, "PLN")

    def test_price_parsing_free(self):
        price, currency = self.scraper._parse_formatted_price("Zdarma")
        self.assertEqual(price, 0.0)

    def test_is_login_page_detection(self):
        login_html = "<html><head><title>Log in to Facebook</title></head><body><form class='login_form'></form></body></html>"
        self.assertTrue(self.scraper._is_login_page(login_html))

        marketplace_html = "<html><head><title>Marketplace</title></head><body><div>marketplace content</div></body></html>"
        self.assertFalse(self.scraper._is_login_page(marketplace_html))

    def test_cookie_header_assembly(self):
        cookie_header = self.scraper._build_cookie_header()
        self.assertIn("c_user=100012345", cookie_header)
        self.assertIn("xs=abc%3Axyz", cookie_header)
        self.assertIn("datr=datr_tok", cookie_header)

    def test_has_credentials(self):
        self.assertTrue(self.scraper.has_credentials)

    def test_recursive_find_listings(self):
        data = {
            "viewer": {
                "marketplace_search": {
                    "feed_units": {
                        "edges": [
                            {
                                "node": {
                                    "__typename": "MarketplaceListing",
                                    "id": "1122334455",
                                    "marketplace_listing_title": "Lenovo ThinkPad T14",
                                    "listing_price": {
                                        "amount": "14000",
                                        "currency": "CZK",
                                    },
                                    "primary_listing_photo": {
                                        "image": {
                                            "uri": "https://fb.com/pic.jpg"
                                        }
                                    },
                                    "location": {
                                        "name": "Prague"
                                    },
                                    "redacted_description": {
                                        "text": "Core i7, 32GB RAM"
                                    }
                                }
                            }
                        ]
                    }
                }
            }
        }
        listings = self.scraper._recursive_find_listings(data)
        self.assertEqual(len(listings), 1)
        item = listings[0]
        self.assertEqual(item.external_id, "1122334455")
        self.assertEqual(item.title, "Lenovo ThinkPad T14")
        self.assertEqual(item.raw_price, 14000.0)
        self.assertEqual(item.raw_currency, "CZK")
        self.assertEqual(item.platform, "facebook")
        self.assertEqual(item.url, "https://www.facebook.com/marketplace/item/1122334455")
        self.assertEqual(item.full_description, "Core i7, 32GB RAM")
        self.assertFalse(item.is_unpriced)

    def test_unpriced_listing_node(self):
        node = {
            "__typename": "MarketplaceListing",
            "id": "99887766",
            "title": "Stůl za odvoz",
            "formatted_price": "Zdarma",
            "location_text": "Brno",
        }
        listing = self.scraper._try_parse_listing_node(node)
        self.assertIsNotNone(listing)
        self.assertEqual(listing.external_id, "99887766")
        self.assertEqual(listing.title, "Stůl za odvoz")
        self.assertTrue(listing.is_unpriced)

    def test_card_text_parsing(self):
        text = "1 200 Kč\nDell Monitor 24\"\nPraha 4"
        title, raw_price, raw_currency = self.scraper._parse_card_text(text)
        self.assertEqual(title, "Dell Monitor 24\"")
        self.assertEqual(raw_price, 1200.0)
        self.assertEqual(raw_currency, "CZK")


class TestFacebookAsync(unittest.IsolatedAsyncioTestCase):
    async def test_search_empty_queries_returns_empty(self):
        scraper = FacebookMarketplaceScraper()
        job = MagicMock()
        job.search_queries = []
        results = await scraper.search(job)
        self.assertEqual(results, [])

    async def test_search_executes_queries_without_credentials(self):
        scraper = FacebookMarketplaceScraper()
        with patch.object(FacebookMarketplaceScraper, "has_credentials", False):
            job = MagicMock()
            job.search_queries = ["iphone"]
            with patch.object(scraper, "_search_single_query", new=AsyncMock(return_value=[])):
                results = await scraper.search(job)
                self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
