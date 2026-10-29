import unittest
from unittest.mock import AsyncMock, patch

from app.scrapers import get_all_scrapers, get_scraper
from app.scrapers.facebook import FacebookMarketplaceScraper
from app.models import SearchJob
from app.services.matcher import ScrapedListing


class TestFacebookMarketplaceScraper(unittest.TestCase):
    def setUp(self):
        self.scraper = FacebookMarketplaceScraper()

    def test_scraper_registration(self):
        scraper = get_scraper("facebook")
        self.assertIsNotNone(scraper)
        self.assertIsInstance(scraper, FacebookMarketplaceScraper)
        self.assertIn("facebook", get_all_scrapers())

    def test_price_parsing_czk(self):
        price, currency = self.scraper._parse_formatted_price("1 500 Kč")
        self.assertEqual(price, 1500.0)
        self.assertEqual(currency, "CZK")

    def test_price_parsing_eur(self):
        price, currency = self.scraper._parse_formatted_price("€120,50")
        self.assertEqual(price, 120.50)
        self.assertEqual(currency, "EUR")

    def test_price_parsing_usd(self):
        price, currency = self.scraper._parse_formatted_price("$250")
        self.assertEqual(price, 250.0)
        self.assertEqual(currency, "USD")

    def test_price_parsing_free(self):
        price, currency = self.scraper._parse_formatted_price("Zdarma")
        self.assertEqual(price, 0.0)

    def test_is_login_page(self):
        login_html = "<html><head><title>Log in to Facebook</title></head><body><div class='login_form'>...</div></body></html>"
        self.assertTrue(self.scraper._is_login_page(login_html))

        marketplace_html = "<html><head><title>Marketplace</title></head><body><div id='marketplace'>Listings here</div></body></html>"
        self.assertFalse(self.scraper._is_login_page(marketplace_html))

    def test_hydration_json_extraction(self):
        sample_html = """
        <!DOCTYPE html>
        <html>
        <head><title>Marketplace Search</title></head>
        <body>
        <script type="application/json" data-sjs>
        {
            "require": [
                ["RelayPrefetchedStreamCache", "next", [], [
                    {
                        "data": {
                            "viewer": {
                                "marketplace_search": {
                                    "feed_units": {
                                        "edges": [
                                            {
                                                "node": {
                                                    "__typename": "MarketplaceListing",
                                                    "id": "1234567890",
                                                    "marketplace_listing_title": "ThinkPad T14 Gen 2",
                                                    "listing_price": {
                                                        "amount": "12500",
                                                        "currency": "CZK"
                                                    },
                                                    "primary_listing_photo": {
                                                        "image": {
                                                            "uri": "https://scontent.xx.fbcdn.net/v/t1/pic.jpg"
                                                        }
                                                    },
                                                    "location": {
                                                        "name": "Prague, Czech Republic"
                                                    },
                                                    "redacted_description": {
                                                        "text": "Great laptop with 16GB RAM and 512GB SSD."
                                                    }
                                                }
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    }
                ]]
            ]
        }
        </script>
        </body>
        </html>
        """
        listings = self.scraper._extract_from_hydration_json(sample_html)
        self.assertEqual(len(listings), 1)
        item = listings[0]
        self.assertEqual(item.external_id, "1234567890")
        self.assertEqual(item.title, "ThinkPad T14 Gen 2")
        self.assertEqual(item.raw_price, 12500.0)
        self.assertEqual(item.raw_currency, "CZK")
        self.assertFalse(item.is_unpriced)
        self.assertEqual(item.location, "Prague, Czech Republic")
        self.assertEqual(item.url, "https://www.facebook.com/marketplace/item/1234567890")
        self.assertEqual(item.image_url, "https://scontent.xx.fbcdn.net/v/t1/pic.jpg")
        self.assertEqual(item.full_description, "Great laptop with 16GB RAM and 512GB SSD.")

    def test_group_commerce_product_item_extraction(self):
        sample_html = """
        <html>
        <head><title>Marketplace</title></head>
        <body>
        <script type="application/json" data-sjs>
        {
            "require": [
                ["ScheduledServerJS", "handle", null, [{
                    "node": {
                        "__typename": "GroupCommerceProductItem",
                        "id": "2688509021552402",
                        "marketplace_listing_title": "Apple MacBook Air 13",
                        "listing_price": {
                            "formatted_amount": "1 999 Kč",
                            "amount": "1999.00"
                        },
                        "primary_listing_photo": {
                            "__typename": "CatalogMarketplaceEnhancementTransformedImage",
                            "image": {
                                "uri": "https://scontent.xx.fbcdn.net/pic.jpg"
                            }
                        },
                        "location": {
                            "reverse_geocode": {
                                "city_page": {
                                    "display_name": "Praha"
                                }
                            }
                        },
                        "is_sold": false,
                        "is_live": true,
                        "is_hidden": false
                    }
                },
                {
                    "node": {
                        "__typename": "GroupCommerceProductItem",
                        "id": "9999999999999999",
                        "marketplace_listing_title": "Sold MacBook Pro",
                        "listing_price": {
                            "formatted_amount": "5 000 Kč",
                            "amount": "5000.00"
                        },
                        "is_sold": true,
                        "is_live": true,
                        "is_hidden": false
                    }
                }]
            ]
        }
        </script>
        </body>
        </html>
        """
        listings = self.scraper._extract_from_hydration_json(sample_html)
        # Should extract the active listing and skip the sold one
        self.assertEqual(len(listings), 1)
        item = listings[0]
        self.assertEqual(item.external_id, "2688509021552402")
        self.assertEqual(item.title, "Apple MacBook Air 13")
        self.assertEqual(item.raw_price, 1999.0)
        self.assertEqual(item.raw_currency, "CZK")
        self.assertEqual(item.location, "Praha")
        self.assertEqual(item.image_url, "https://scontent.xx.fbcdn.net/pic.jpg")

    def test_dom_fallback_extraction(self):
        sample_html = """
        <html>
        <body>
            <div>
                <div>
                    <div>
                        <a href="/marketplace/item/9876543210/?ref=search">
                            <span>15 000 Kč</span>
                            <span>MacBook Air M1 2020</span>
                            <span>Brno</span>
                        </a>
                        <img src="https://scontent.xx.fbcdn.net/v/t2/mac.jpg" />
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        listings = self.scraper._extract_from_dom(sample_html)
        self.assertEqual(len(listings), 1)
        item = listings[0]
        self.assertEqual(item.external_id, "9876543210")
        self.assertEqual(item.raw_price, 15000.0)
        self.assertEqual(item.raw_currency, "CZK")
        self.assertIn("MacBook Air", item.title)
        self.assertEqual(item.url, "https://www.facebook.com/marketplace/item/9876543210")


class TestFacebookMarketplaceSearch(unittest.IsolatedAsyncioTestCase):
    async def test_search_without_credentials_executes_query(self):
        scraper = FacebookMarketplaceScraper()
        mock_listing = ScrapedListing(
            platform="facebook",
            external_id="111222333",
            title="ThinkPad T14",
            url="https://www.facebook.com/marketplace/item/111222333",
            raw_price=12000.0,
            raw_currency="CZK",
        )
        job = SearchJob(
            name="Test Job",
            platforms=["facebook"],
            search_queries=["thinkpad"],
            min_price=100.0,
            max_price=20000.0,
            shipping_buffer=85.0,
        )

        with patch.object(scraper, "_search_single_query", new=AsyncMock(return_value=[mock_listing])):
            results = await scraper.search(job)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].external_id, "111222333")

    async def test_search_empty_queries_returns_empty(self):
        scraper = FacebookMarketplaceScraper()
        job = SearchJob(
            name="Empty Job",
            platforms=["facebook"],
            search_queries=[],
        )
        results = await scraper.search(job)
        self.assertEqual(results, [])

    def test_cookie_header_empty_when_no_credentials(self):
        scraper = FacebookMarketplaceScraper()
        with patch("app.scrapers.facebook.settings.FB_COOKIE_C_USER", ""), \
             patch("app.scrapers.facebook.settings.FB_COOKIE_XS", ""), \
             patch("app.scrapers.facebook.settings.FB_COOKIE_DATR", ""):
            header = scraper._build_cookie_header()
            self.assertEqual(header, "")

    def test_cookie_header_built_when_credentials_present(self):
        scraper = FacebookMarketplaceScraper()
        with patch("app.scrapers.facebook.settings.FB_COOKIE_C_USER", "123456"), \
             patch("app.scrapers.facebook.settings.FB_COOKIE_XS", "abcdef"), \
             patch("app.scrapers.facebook.settings.FB_COOKIE_DATR", "datrtoken"):
            header = scraper._build_cookie_header()
            self.assertEqual(header, "c_user=123456; xs=abcdef; datr=datrtoken")


if __name__ == "__main__":
    unittest.main()
