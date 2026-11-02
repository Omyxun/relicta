import asyncio
import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.config import Settings
from app.security import SameOriginMiddleware
from app.services.ntfy_client import post_notification, target


class SecretTests(unittest.TestCase):
    def test_missing_and_documented_placeholder_secrets_are_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValidationError):
                Settings(_env_file=None)
        for value in ["insecure-dev-secret-key-replace-immediately-in-env",
                      "replace-with-a-unique-random-secret",
                      "change-this-to-a-64-char-random-hex-string-in-production"]:
            with self.assertRaises(ValidationError):
                Settings(_env_file=None, SECRET_KEY=value)
        Settings(_env_file=None, SECRET_KEY="a-unique-test-secret-for-validation")


class BrowserTests(unittest.TestCase):
    def setUp(self):
        async def endpoint(request):
            return PlainTextResponse("ok")
        app = Starlette(routes=[Route("/settings", endpoint, methods=["POST", "PUT", "DELETE"])])
        app.add_middleware(SameOriginMiddleware)
        self.client = TestClient(app, base_url="http://192.168.1.10:8000")

    def test_lan_browser_and_programmatic_requests_work(self):
        for headers in [{}, {"Origin": "http://192.168.1.10:8000"},
                        {"Referer": "http://192.168.1.10:8000/settings"}]:
            self.assertEqual(self.client.post("/settings", headers=headers).status_code, 200)

    def test_cross_origin_and_null_origin_requests_are_rejected(self):
        for headers in [{"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Origin": "http://192.168.1.10:9000"}, {"Sec-Fetch-Site": "cross-site"}]:
            for method in ["POST", "PUT", "DELETE"]:
                self.assertEqual(self.client.request(method, "/settings", headers=headers).status_code, 403)


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_server_requires_operator_approval_and_redirects_are_blocked(self):
        requests = []

        async def handler(reader, writer):
            data = await reader.readuntil(b"\r\n\r\n")
            requests.append(data)
            if data.startswith(b"POST /success "):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            else:
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        config = Settings(_env_file=None, SECRET_KEY="a-unique-test-secret-for-validation")
        try:
            with patch("app.services.ntfy_client.get_settings", return_value=config):
                ok, _ = await post_notification(url, "topic", "hello", {})
                self.assertFalse(ok)
                self.assertEqual(requests, [])
                config.NTFY_ALLOWED_PRIVATE_SERVERS = (url,)
                ok, detail = await post_notification(url, "topic", "hello", {})
                self.assertFalse(ok)
                self.assertIn("302", detail)
                self.assertEqual(len(requests), 1)
                self.assertTrue(requests[0].startswith(b"POST /topic HTTP/1.1"))
                ok, _ = await post_notification(url, "success", "hello", {"Authorization": "Bearer test"})
                self.assertTrue(ok)
                self.assertEqual(len(requests), 2)
                self.assertIn(b"Authorization: Bearer test", requests[1])
        finally:
            server.close()
            await server.wait_closed()

    async def test_private_default_is_allowed_and_topics_cannot_change_destination(self):
        config = Settings(_env_file=None, SECRET_KEY="a-unique-test-secret-for-validation",
                          NTFY_DEFAULT_SERVER_URL="http://192.168.1.20:8080")
        with patch("app.services.ntfy_client.get_settings", return_value=config):
            self.assertEqual(target(config.NTFY_DEFAULT_SERVER_URL, "topic?x=1#fragment"),
                             ("http://192.168.1.20:8080/topic%3Fx%3D1%23fragment", True))
            for url in ["http://127.0.0.1", "http://[::1]", "file:///etc/passwd", "http://user:pass@example.com"]:
                with self.assertRaises(ValueError):
                    target(url, "topic")
