"""Send notifications to public sites or operator-approved private ntfy servers."""
import ipaddress
from urllib.parse import quote, urlsplit

import aiohttp

from app.config import get_settings
from app.security import origin
from app.services.site_renderer import PublicResolver


def target(server: str, topic: str) -> tuple[str, bool]:
    server = server.strip().rstrip("/")
    parts = urlsplit(server)
    server_origin = origin(server)
    if parts.query or parts.fragment or not topic:
        raise ValueError("Use an ntfy server URL without a query or fragment and a nonempty topic.")
    settings = get_settings()
    approved = {origin(value) for value in (
        settings.NTFY_DEFAULT_SERVER_URL, *settings.NTFY_ALLOWED_PRIVATE_SERVERS
    ) if value.strip()}
    allow_private = server_origin in approved
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass  # Hostnames are checked at connection time by PublicResolver.
    else:
        if not allow_private and not address.is_global:
            raise ValueError("Ask the operator to approve this private ntfy server in .env.")
    return f"{server}/{quote(topic, safe='')}", allow_private


async def post_notification(server: str, topic: str, body: str, headers: dict[str, str]) -> tuple[bool, str]:
    try:
        endpoint, allow_private = target(server, topic)
        # The guarded resolver pins the validated public address to the connection.
        connector = aiohttp.TCPConnector(resolver=None if allow_private else PublicResolver())
        async with aiohttp.ClientSession(connector=connector, trust_env=False,
                timeout=aiohttp.ClientTimeout(total=15)) as client:
            async with client.post(endpoint, data=body.encode("utf-8"), headers=headers,
                    allow_redirects=False) as response:
                if not 200 <= response.status < 300:
                    detail = (await response.content.read(200)).decode("utf-8", errors="replace")
                    return False, f"ntfy HTTP {response.status}: {detail}"
        return True, "Sent ntfy notification successfully."
    except (ValueError, aiohttp.ClientError, TimeoutError) as exc:
        return False, f"Could not send ntfy notification: {exc}"
