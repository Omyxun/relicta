"""Render public pages with all page HTTP requests mediated by a guarded client."""
import asyncio
import base64
import ipaddress
import socket
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

_slots = asyncio.Semaphore(2)


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not records or any(not ipaddress.ip_address(record[4][0]).is_global for record in records):
            raise ValueError("Only public website addresses are allowed.")
        return [dict(hostname=host, host=r[4][0], port=port, family=r[0], proto=0, flags=0) for r in records]

    async def close(self):
        pass


def check_url(url):
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
        raise ValueError("Use a public HTTP/HTTPS URL on a standard port without credentials.")
    try:
        address = ipaddress.ip_address(p.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("Private and local addresses are not allowed.")


async def render_site(url, preview=False):
    check_url(url)
    from playwright.async_api import async_playwright
    async with _slots, asyncio.timeout(45):
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver()),
                timeout=aiohttp.ClientTimeout(total=15), trust_env=False) as client:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(args=["--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"])
                try:
                    context = await browser.new_context(viewport={"width": 1200, "height": 900}, service_workers="block", accept_downloads=False)
                    count = 0
                    total = 0
                    async def intercept(route):
                        nonlocal count, total
                        count += 1
                        try:
                            check_url(route.request.url)
                            if count > 150 or total > 20_000_000 or route.request.method != "GET":
                                return await route.abort()
                            async with client.get(route.request.url, allow_redirects=False) as response:
                                body = await response.content.read(2_000_001)
                                if len(body) > 2_000_000:
                                    return await route.abort()
                                total += len(body)
                                headers = {k: v for k, v in response.headers.items() if k.lower() in ("content-type", "location")}
                                await route.fulfill(status=response.status, headers=headers, body=body)
                        except Exception:
                            await route.abort()
                    await context.route("**/*", intercept)
                    await context.route_web_socket("**/*", lambda ws: ws.close())
                    page = await context.new_page()
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=25000)
                    if response is None or response.status >= 400:
                        raise ValueError("The site blocked access or could not be loaded.")
                    await page.wait_for_timeout(1500)
                    html = await page.content()
                    result = {"html": html, "url": page.url}
                    if preview:
                        # Only pixels and inert element metadata reach the authenticated UI.
                        result["image"] = base64.b64encode(await page.screenshot()).decode()
                        result["elements"] = await page.evaluate('''() => {
                          const all = Array.from(document.querySelectorAll('body *')).slice(0, 10000);
                          function selector(e) {
                            const cls = Array.from(e.classList).filter(c => /^[a-zA-Z_-][\\w-]*$/.test(c)).slice(0,3);
                            return e.tagName.toLowerCase() + cls.map(c => '.' + CSS.escape(c)).join('');
                          }
                          return all.map(e => {const r=e.getBoundingClientRect(); return {
                            x:r.x,y:r.y,w:r.width,h:r.height, selector:selector(e),
                            path:Array.from((function*(n){while(n && n!==document.body){yield n;n=n.parentElement}})(e)).reverse().map(selector).join(' > ')
                          }}).filter(e=>e.w>0 && e.h>0 && e.y<900 && e.y+e.h>0);
                        }''')
                    return result
                finally:
                    await browser.close()
