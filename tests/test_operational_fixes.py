import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

from starlette.requests import Request

from app.instance_lock import InstanceLock
from app.models import User, SearchJob, SeenListing
from app.main import app
from app.routers.settings import update_settings
from app.services.currency import currency_service


class InstanceLockTests(unittest.TestCase):
    def test_second_instance_is_rejected_and_release_allows_restart(self):
        with tempfile.TemporaryDirectory(dir=".") as directory:
            url = "sqlite+aiosqlite:///" + str(Path(directory) / "test.db")
            first, second = InstanceLock(url), InstanceLock(url)
            first.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()


class SettingsTests(unittest.TestCase):
    def test_currency_conversion_and_non_admin_cannot_change_shared_credentials(self):
        user = User(id=1, username="user", password_hash="hash", is_admin=False, base_currency="CZK")
        job = SearchJob(user_id=1, name="job", min_price=1000, max_price=2500, shipping_buffer=100)
        listing = SeenListing(job_id=1, platform="ebay", external_id="1", title="item", url="https://example.com", total_price_base=1250, last_notified_price=1500)
        jobs_result, listings_result = MagicMock(), MagicMock()
        jobs_result.scalars.return_value.all.return_value = [job]
        listings_result.scalars.return_value.all.return_value = [listing]
        session = MagicMock()
        session.execute = AsyncMock(side_effect=[jobs_result, listings_result])
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        request = Request({"type": "http", "method": "POST", "path": "/settings", "headers": [], "query_string": b"", "server": ("test", 80), "scheme": "http"})
        with patch.object(currency_service, "_rates_to_czk", {"CZK": 1.0, "EUR": 25.0}), patch("app.routers.settings.save_ebay_settings") as save, patch("app.routers.settings.render_toast_html", return_value="ok"):
            operation = update_settings(request, base_currency="EUR", ebay_client_id="attack", ebay_client_secret="secret", ebay_marketplace_id="EBAY_DE", ebay_ship_to_country="CZ", telegram_bot_token="", telegram_chat_id="", notify_telegram=None, ntfy_url="", ntfy_topic="", ntfy_token="", email_address="", notify_email=None, _lock=None, db=session, current_user=user)
            with self.assertRaises(StopIteration):
                operation.send(None)
            save.assert_not_called()
        self.assertEqual((job.min_price, job.max_price, job.shipping_buffer), (40, 100, 4))
        self.assertEqual((listing.total_price_base, listing.last_notified_price), (50, 60))
        self.assertEqual(user.base_currency, "EUR")


if __name__ == "__main__":
    unittest.main()



