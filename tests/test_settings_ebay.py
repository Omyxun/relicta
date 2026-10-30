import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from starlette.testclient import TestClient

from app.main import app
from app.auth import get_current_user
from app.models import User
from app.config import get_settings, reload_settings, update_env_file


class TestSettingsEbayEnv(unittest.TestCase):
    def setUp(self):
        self.test_env = Path(".env.test_tmp")
        self.test_env.write_text(
            'APP_NAME="Relicta"\nEBAY_CLIENT_ID=""\nEBAY_CLIENT_SECRET=""\n',
            encoding="utf-8"
        )
        self.mock_user = User(
            id=1,
            username="testadmin",
            password_hash="fake",
            is_admin=True,
            base_currency="CZK",
        )
        app.dependency_overrides[get_current_user] = lambda: self.mock_user
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        if self.test_env.exists():
            self.test_env.unlink()

    def test_update_env_file_ebay_credentials(self):
        update_env_file({
            "EBAY_CLIENT_ID": "test-app-id-123",
            "EBAY_CLIENT_SECRET": "test-cert-id-456",
            "EBAY_MARKETPLACE_ID": "EBAY_DE",
            "EBAY_DEFAULT_SHIP_TO_COUNTRY": "CZ",
        }, env_path=str(self.test_env))

        content = self.test_env.read_text(encoding="utf-8")
        self.assertIn('EBAY_CLIENT_ID="test-app-id-123"', content)
        self.assertIn('EBAY_CLIENT_SECRET="test-cert-id-456"', content)
        self.assertIn('EBAY_MARKETPLACE_ID="EBAY_DE"', content)
        self.assertIn('EBAY_DEFAULT_SHIP_TO_COUNTRY="CZ"', content)
        self.assertIn('APP_NAME="Relicta"', content)

    def test_settings_get_renders_ebay_gui(self):
        resp = self.client.get("/settings")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("eBay API Integration", resp.text)
        self.assertIn("ebay_client_id", resp.text)
        self.assertIn("ebay_client_secret", resp.text)
        self.assertIn("ebay_marketplace_id", resp.text)
        self.assertIn("ebay_ship_to_country", resp.text)
        self.assertIn("Test Credentials", resp.text)

    def test_test_ebay_endpoint_requires_credentials(self):
        resp = self.client.post("/settings/test/ebay", data={"ebay_client_id": "", "ebay_client_secret": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Provide both Client ID and Client Secret", resp.text)


if __name__ == "__main__":
    unittest.main()
