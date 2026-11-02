import unittest

from app.scrapers.custom import extract_listings, search_url


class CustomSourceParsingTests(unittest.TestCase):
    def test_search_parameter_is_encoded_and_other_parameters_are_preserved(self):
        self.assertEqual(
            search_url("https://example.com/search?q=old&sort=new", "q", "červený laptop"),
            "https://example.com/search?q=%C4%8Derven%C3%BD+laptop&sort=new",
        )
        with self.assertRaises(ValueError):
            search_url("https://example.com/?q=one&q=two", "q", "new")

    def test_extracts_deduplicated_listings_and_unpriced_items(self):
        html = '''<article class="item"><a class="link" href="/item/1#x">
          <span class="title">Laptop</span></a><span class="price">1 250,50 Kč</span></article>
          <article class="item"><a class="link" href="/item/1"><span class="title">Duplicate</span></a></article>
          <article class="item"><a class="link" href="/item/2"><span class="title">Other</span></a></article>'''
        selectors = {"card": ".item", "title": ".title", "link": ".link", "price": ".price"}
        listings = extract_listings(html, "https://example.com/search", "custom:1", "CZK", selectors)
        self.assertEqual(len(listings), 2)
        self.assertEqual(listings[0].url, "https://example.com/item/1")
        self.assertEqual(listings[0].raw_price, 1250.5)
        self.assertTrue(listings[1].is_unpriced)
        self.assertEqual(len(listings[0].external_id), 64)

    def test_rejects_unsafe_links_and_missing_matches(self):
        selectors = {"card": ".item", "title": ".title", "link": ".link"}
        html = '<div class="item"><a class="link" href="javascript:alert(1)"><span class="title">X</span></a></div>'
        with self.assertRaises(ValueError):
            extract_listings(html, "https://example.com", "custom:1", "EUR", selectors)


if __name__ == "__main__":
    unittest.main()
