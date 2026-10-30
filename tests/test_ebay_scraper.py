import unittest
from unittest.mock import AsyncMock, patch

from app.models import SearchJob
from app.scrapers import get_all_scrapers, get_scraper
from app.scrapers.ebay import EbayScraper
from app.services.matcher import ScrapedListing


class TestEbayScraper(unittest.TestCase):
    def setUp(self):
        self.scraper = EbayScraper()

    def test_scraper_registration(self):
        scraper = get_scraper("ebay")
        self.assertIsNotNone(scraper)
        self.assertIsInstance(scraper, EbayScraper)
        self.assertIn("ebay", get_all_scrapers())

    def test_price_parsing_eur(self):
        price, currency = self.scraper._parse_ebay_money("EUR 39,00")
        self.assertEqual(price, 39.0)
        self.assertEqual(currency, "EUR")

        price2, currency2 = self.scraper._parse_ebay_money("1.250,50 EUR")
        self.assertEqual(price2, 1250.50)
        self.assertEqual(currency2, "EUR")

    def test_price_parsing_usd(self):
        price, currency = self.scraper._parse_ebay_money("$45.50")
        self.assertEqual(price, 45.50)
        self.assertEqual(currency, "USD")

    def test_price_parsing_gbp(self):
        price, currency = self.scraper._parse_ebay_money("£12.99")
        self.assertEqual(price, 12.99)
        self.assertEqual(currency, "GBP")

    def test_price_parsing_pln(self):
        price, currency = self.scraper._parse_ebay_money("1 488,04 zł")
        self.assertEqual(price, 1488.04)
        self.assertEqual(currency, "PLN")

    def test_api_summary_parsing(self):
        mock_item = {
            "itemId": "v1|123456789012|0",
            "title": "Lenovo ThinkPad T14 Gen 2",
            "itemWebUrl": "https://www.ebay.de/itm/123456789012",
            "price": {"value": "450.00", "currency": "EUR"},
            "shippingOptions": [
                {
                    "shippingCostType": "FIXED",
                    "shippingCost": {"value": "15.00", "currency": "EUR"},
                }
            ],
            "condition": "Used",
            "seller": {"feedbackPercentage": "99.4"},
            "itemLocation": {"country": "DE", "postalCode": "10115"},
        }

        listing = self.scraper._parse_item_summary(mock_item)
        self.assertIsNotNone(listing)
        self.assertEqual(listing.external_id, "v1|123456789012|0")
        self.assertEqual(listing.title, "Lenovo ThinkPad T14 Gen 2")
        self.assertEqual(listing.raw_price, 450.0)
        self.assertEqual(listing.raw_currency, "EUR")
        self.assertEqual(listing.shipping_price, 15.0)
        self.assertEqual(listing.shipping_currency, "EUR")
        self.assertEqual(listing.condition, "Used")
        self.assertEqual(listing.seller_feedback_score, 99.4)
        self.assertEqual(listing.location, "DE 10115")

    def test_parse_web_card_modern_layout(self):
        html = """
        <li class="s-card" data-listingid="123456789">
            <div class="su-card-container">
                <a class="s-card__link" href="https://www.ebay.de/itm/123456789?hash=abc">
                    <span role="heading">Neues AngebotLenovo ThinkPad T14sWird in neuem Fenster oder Tab geöffnet</span>
                </a>
                <span class="s-card__price">EUR 350,00</span>
                <span class="s-card__shipping">Kostenloser Versand</span>
                <span class="s-card__subtitle">Gebraucht</span>
            </div>
        </li>
        """
        from selectolax.lexbor import LexborHTMLParser
        tree = LexborHTMLParser(html)
        card = tree.css_first(".s-card")
        listing = self.scraper._parse_web_card(card)
        self.assertIsNotNone(listing)
        self.assertEqual(listing.external_id, "123456789")
        self.assertEqual(listing.title, "Lenovo ThinkPad T14s")
        self.assertEqual(listing.raw_price, 350.0)
        self.assertEqual(listing.raw_currency, "EUR")
        self.assertEqual(listing.shipping_price, 0.0)
        self.assertEqual(listing.condition, "Gebraucht")

    def test_skip_dummy_card(self):
        html = """
        <li class="s-card" data-listingid="123456">
            <a class="s-card__link" href="https://www.ebay.com/itm/123456">
                <span role="heading">Shop on eBay</span>
            </a>
            <span class="s-card__price">$20.00</span>
        </li>
        """
        from selectolax.lexbor import LexborHTMLParser
        tree = LexborHTMLParser(html)
        card = tree.css_first(".s-card")
        listing = self.scraper._parse_web_card(card)
        self.assertIsNone(listing)


if __name__ == "__main__":
    unittest.main()
