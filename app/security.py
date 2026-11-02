"""Browser request protections compatible with HTTP on a trusted LAN."""
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Use an HTTP/HTTPS URL without credentials.")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class SameOriginMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            from starlette.requests import Request
            request = Request(scope)
            supplied = request.headers.get("origin") or request.headers.get("referer")
            try:
                allowed = request.headers.get("sec-fetch-site") != "cross-site"
                if supplied:
                    allowed = allowed and origin(supplied) == origin(str(request.url))
            except ValueError:
                allowed = False
            if not allowed:
                response = JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
