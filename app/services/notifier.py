import html
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from typing import Optional

import aiosmtplib
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User
from app.services.matcher import AlertType, FinalMatchResult, ScrapedListing
from app.services.ntfy_client import post_notification

logger = logging.getLogger(__name__)
settings = get_settings()

TELEGRAM_MAX_CHUNK_LENGTH = 3800
NTFY_MAX_BODY_BYTES = 3800


@dataclass(slots=True)
class DigestItem:
    seen_listing_id: int
    listing: ScrapedListing
    match: FinalMatchResult


@dataclass(slots=True)
class JobDigest:
    job_id: int
    job_name: str
    base_currency: str
    priced_new_deals: list[DigestItem] = field(default_factory=list)
    price_drops: list[DigestItem] = field(default_factory=list)
    unpriced_deals: list[DigestItem] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return (
            len(self.priced_new_deals)
            + len(self.price_drops)
            + len(self.unpriced_deals)
        )

    @property
    def has_items(self) -> bool:
        return self.total_count > 0

    def all_items(self) -> list[DigestItem]:
        return [*self.priced_new_deals, *self.price_drops, *self.unpriced_deals]


def format_currency(amount: Optional[float], currency: str) -> str:
    if amount is None:
        return "Dohodou"
    if currency.upper() == "CZK":
        return f"{int(round(amount)):,} CZK".replace(",", " ")
    return f"{amount:,.2f} {currency.upper()}".replace(",", " ")


def build_price_breakdown(item: DigestItem) -> str:
    """
    Produces a human-readable total price breakdown including shipping and
    original foreign currency note when converted.
    """
    listing = item.listing
    match = item.match
    base_curr = match.base_currency.upper()

    if match.is_unpriced or match.total_price_base is None:
        return "[UNPRICED / DOHODOU]"

    total_str = format_currency(match.total_price_base, base_curr)
    ship_str = format_currency(match.shipping_price_base, base_curr)

    details: list[str] = []
    if match.shipping_price_base > 0:
        details.append(f"incl. shipping {ship_str}")
    else:
        details.append("free shipping")

    if (
        listing.raw_price is not None
        and listing.raw_currency.upper() != base_curr
    ):
        orig_str = format_currency(listing.raw_price, listing.raw_currency)
        if listing.shipping_price is not None and listing.shipping_price > 0:
            ship_orig_curr = listing.shipping_currency or listing.raw_currency
            orig_ship_str = format_currency(listing.shipping_price, ship_orig_curr)
            details.append(f"orig. {orig_str} + {orig_ship_str}")
        else:
            details.append(f"orig. {orig_str}")

    return f"{total_str} ({', '.join(details)})"


class BaseNotifier(ABC):
    channel_name: str = "base"

    @abstractmethod
    def is_configured(self, user: User) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def send_digest(self, user: User, digest: JobDigest) -> tuple[bool, str]:
        raise NotImplementedError

    @abstractmethod
    async def send_system_alert(
        self, user: User, title: str, message: str
    ) -> tuple[bool, str]:
        raise NotImplementedError

    async def send_test(self, user: User) -> tuple[bool, str]:
        sample_listing = ScrapedListing(
            platform="bazos",
            external_id="test-999999",
            title="Test Listing - ThinkPad T14 Gen 3 (16GB RAM, 512GB NVMe)",
            url="https://www.bazos.cz",
            raw_price=8500.0,
            raw_currency="CZK",
            is_unpriced=False,
            snippet="Perfektní stav, záruka, osobní předání Praha nebo Zásilkovna.",
            location="Praha 12 (143 00)",
        )
        sample_match = FinalMatchResult(
            matched=True,
            alert_type=AlertType.NEW_DEAL,
            is_unpriced=False,
            total_price_base=8585.0,
            item_price_base=8500.0,
            shipping_price_base=85.0,
            base_currency=user.base_currency or "CZK",
        )
        sample_digest = JobDigest(
            job_id=0,
            job_name="Test Diagnostic Job",
            base_currency=user.base_currency or "CZK",
            priced_new_deals=[
                DigestItem(
                    seen_listing_id=0,
                    listing=sample_listing,
                    match=sample_match,
                )
            ],
        )
        return await self.send_digest(user, sample_digest)


class TelegramNotifier(BaseNotifier):
    """
    Dispatches HTML-formatted batch digests via the Telegram Bot HTTP API.
    Automatically splits oversized digests into multi-message chunks under 3800 chars.
    """

    channel_name = "telegram"

    def _resolve_credentials(self, user: User) -> tuple[str, str]:
        token = (
            (user.telegram_bot_token or "").strip()
            or settings.TELEGRAM_DEFAULT_BOT_TOKEN.strip()
        )
        chat_id = (user.telegram_chat_id or "").strip()
        return token, chat_id

    def is_configured(self, user: User) -> bool:
        token, chat_id = self._resolve_credentials(user)
        return bool(token and chat_id)

    async def send_digest(self, user: User, digest: JobDigest) -> tuple[bool, str]:
        token, chat_id = self._resolve_credentials(user)
        if not token or not chat_id:
            return False, "Telegram Bot Token or Chat ID is not configured."

        header = (
            f"🔔 <b>{html.escape(settings.APP_NAME)}: {html.escape(digest.job_name)}</b>\n"
            f"Found <b>{digest.total_count}</b> matching listing(s)\n"
        )

        blocks: list[str] = []

        if digest.price_drops:
            blocks.append("\n📉 <b>PRICE DROPS</b>")
            for item in digest.price_drops:
                blocks.append(self._format_item_html(item))

        if digest.priced_new_deals:
            blocks.append("\n🆕 <b>NEW VERIFIED DEALS</b>")
            for item in digest.priced_new_deals:
                blocks.append(self._format_item_html(item))

        if digest.unpriced_deals:
            blocks.append("\n🏷️ <b>[UNPRICED / DOHODOU]</b>")
            for item in digest.unpriced_deals:
                blocks.append(self._format_item_html(item))

        chunks = self._chunk_blocks(header, blocks, TELEGRAM_MAX_CHUNK_LENGTH)
        return await self._post_chunks(token, chat_id, chunks)

    async def send_system_alert(
        self, user: User, title: str, message: str
    ) -> tuple[bool, str]:
        token, chat_id = self._resolve_credentials(user)
        if not token or not chat_id:
            return False, "Telegram credentials missing."

        payload_text = (
            f"⚠️ <b>{html.escape(title)}</b>\n\n"
            f"{html.escape(message)}"
        )
        return await self._post_chunks(token, chat_id, [payload_text])

    @staticmethod
    def _format_item_html(item: DigestItem) -> str:
        listing = item.listing
        match = item.match
        platform_badge = f"[{listing.platform.upper()}]"
        safe_title = html.escape(listing.title)
        safe_url = html.escape(listing.url, quote=True)
        price_line = html.escape(build_price_breakdown(item))

        lines: list[str] = [
            f"• <b>{platform_badge}</b> <a href=\"{safe_url}\">{safe_title}</a>"
        ]

        if match.alert_type == AlertType.PRICE_DROP and match.price_drop is not None:
            pd = match.price_drop
            old_str = format_currency(pd.old_price_base, pd.base_currency)
            new_str = format_currency(pd.new_price_base, pd.base_currency)
            lines.append(
                f"  📉 <b>{html.escape(old_str)} → {html.escape(new_str)} (-{pd.drop_percent:.1f}%)</b>"
            )
            lines.append(f"  💰 {price_line}")
        elif match.is_unpriced:
            lines.append("  🏷️ <b>[UNPRICED / DOHODOU]</b>")
        else:
            lines.append(f"  💰 <b>{price_line}</b>")

        meta_parts: list[str] = []
        if listing.location:
            meta_parts.append(f"📍 {html.escape(listing.location)}")
        if listing.condition:
            meta_parts.append(f"📦 {html.escape(listing.condition)}")
        if listing.seller_feedback_score is not None:
            meta_parts.append(f"⭐ {listing.seller_feedback_score:.1f}%")
        if meta_parts:
            lines.append(f"  {' | '.join(meta_parts)}")

        return "\n".join(lines)

    @staticmethod
    def _chunk_blocks(header: str, blocks: list[str], max_len: int) -> list[str]:
        chunks: list[str] = []
        current = header
        for block in blocks:
            candidate = f"{current}\n{block}"
            if len(candidate) > max_len and current != header:
                chunks.append(current)
                current = f"{header}(continued)\n{block}"
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks

    async def _post_chunks(
        self, token: str, chat_id: str, chunks: list[str]
    ) -> tuple[bool, str]:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        async with httpx.AsyncClient(timeout=15.0) as client:
            for chunk in chunks:
                try:
                    resp = await client.post(
                        url,
                        json={
                            "chat_id": chat_id,
                            "text": chunk,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                    )
                    if resp.status_code >= 400:
                        logger.error(
                            "Telegram API error HTTP %d: %s",
                            resp.status_code,
                            resp.text,
                        )
                        return False, f"Telegram HTTP {resp.status_code}: {resp.text[:200]}"
                except httpx.RequestError as exc:
                    logger.error("Telegram network error: %s", exc)
                    return False, f"Network error talking to Telegram: {exc}"

        return True, f"Sent {len(chunks)} Telegram message(s) successfully."


class NtfyNotifier(BaseNotifier):
    """
    Dispatches Markdown-formatted batch digests to a public or self-hosted ntfy server.
    """

    channel_name = "ntfy"

    def _resolve_target(self, user: User) -> tuple[str, str, Optional[str]]:
        base_url = (
            (user.ntfy_url or "").strip()
            or settings.NTFY_DEFAULT_SERVER_URL.strip()
        ).rstrip("/")
        topic = (user.ntfy_topic or "").strip().lstrip("/")
        token = (user.ntfy_token or "").strip() or None
        return base_url, topic, token

    def is_configured(self, user: User) -> bool:
        base_url, topic, _ = self._resolve_target(user)
        return bool(base_url and topic)

    async def send_digest(self, user: User, digest: JobDigest) -> tuple[bool, str]:
        base_url, topic, token = self._resolve_target(user)
        if not base_url or not topic:
            return False, "ntfy server URL or topic is not configured."

        title = f"{settings.APP_NAME}: {digest.job_name} ({digest.total_count})"
        priority = "high" if digest.price_drops else "default"
        tags = "chart_with_downwards_trend,shopping" if digest.price_drops else "shopping,bell"

        lines: list[str] = []
        if digest.price_drops:
            lines.append("### Price Drops")
            for item in digest.price_drops:
                lines.append(self._format_item_md(item))

        if digest.priced_new_deals:
            lines.append("### New Verified Deals")
            for item in digest.priced_new_deals:
                lines.append(self._format_item_md(item))

        if digest.unpriced_deals:
            lines.append("### [UNPRICED / DOHODOU]")
            for item in digest.unpriced_deals:
                lines.append(self._format_item_md(item))

        body = "\n\n".join(lines)
        if len(body.encode("utf-8")) > NTFY_MAX_BODY_BYTES:
            body = body[: NTFY_MAX_BODY_BYTES - 60] + "\n\n...(truncated, check dashboard)"

        headers: dict[str, str] = {
            "Title": title.encode("ascii", errors="ignore").decode("ascii") or settings.APP_NAME,
            "Priority": priority,
            "Tags": tags,
            "Markdown": "yes",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        return await post_notification(base_url, topic, body, headers)

    async def send_system_alert(
        self, user: User, title: str, message: str
    ) -> tuple[bool, str]:
        base_url, topic, token = self._resolve_target(user)
        if not base_url or not topic:
            return False, "ntfy credentials missing."

        headers: dict[str, str] = {
            "Title": title.encode("ascii", errors="ignore").decode("ascii") or "Health Alert",
            "Priority": "high",
            "Tags": "warning,rotating_light",
            "Markdown": "yes",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        return await post_notification(base_url, topic, message, headers)

    @staticmethod
    def _format_item_md(item: DigestItem) -> str:
        listing = item.listing
        match = item.match
        badge = f"**[{listing.platform.upper()}]**"
        price_str = build_price_breakdown(item)

        if match.alert_type == AlertType.PRICE_DROP and match.price_drop is not None:
            pd = match.price_drop
            old_s = format_currency(pd.old_price_base, pd.base_currency)
            new_s = format_currency(pd.new_price_base, pd.base_currency)
            price_part = f"**{old_s} -> {new_s} (-{pd.drop_percent:.1f}%)** | {price_str}"
        else:
            price_part = f"**{price_str}**"

        loc_part = f" ({listing.location})" if listing.location else ""
        return f"- {badge} [{listing.title}]({listing.url}) — {price_part}{loc_part}"


class EmailNotifier(BaseNotifier):
    """
    Sends multipart (plaintext + responsive HTML) batch digest emails via aiosmtplib.
    """

    channel_name = "email"

    def is_configured(self, user: User) -> bool:
        return bool(
            settings.SMTP_HOST.strip()
            and user.email_address
            and user.email_address.strip()
        )

    async def send_digest(self, user: User, digest: JobDigest) -> tuple[bool, str]:
        if not self.is_configured(user):
            return False, "SMTP server or user email address is not configured."

        recipient = (user.email_address or "").strip()
        subject = f"[{settings.APP_NAME}] {digest.job_name}: {digest.total_count} deal(s) found"

        plain_body = self._build_Plaintext(digest)
        html_body = self._build_html(digest)
        return await self._send_email(recipient, subject, plain_body, html_body)

    async def send_system_alert(
        self, user: User, title: str, message: str
    ) -> tuple[bool, str]:
        if not self.is_configured(user):
            return False, "SMTP or user email not configured."

        recipient = (user.email_address or "").strip()
        subject = f"[{settings.APP_NAME} WARNING] {title}"
        html_body = (
            f"<html><body style='font-family:sans-serif;color:#1f2937;'>"
            f"<h2 style='color:#dc2626;'>{html.escape(title)}</h2>"
            f"<p style='white-space:pre-line;'>{html.escape(message)}</p>"
            f"</body></html>"
        )
        return await self._send_email(recipient, subject, message, html_body)

    async def _send_email(
        self,
        recipient: str,
        subject: str,
        plain_body: str,
        html_body: str,
    ) -> tuple[bool, str]:
        msg = EmailMessage()
        msg["From"] = settings.SMTP_FROM_EMAIL
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(plain_body)
        msg.add_alternative(html_body, subtype="html")

        use_implicit_tls = settings.SMTP_PORT == 465
        use_starttls = settings.SMTP_USE_TLS and not use_implicit_tls

        try:
            await aiosmtplib.send(
                msg,
                hostname=settings.SMTP_HOST,
                port=settings.SMTP_PORT,
                username=settings.SMTP_USERNAME or None,
                password=settings.SMTP_PASSWORD or None,
                use_tls=use_implicit_tls,
                start_tls=use_starttls,
                timeout=20.0,
            )
            return True, f"Sent email digest to {recipient}."
        except Exception as exc:
            logger.error("SMTP send failure to %s: %s", recipient, exc)
            return False, f"SMTP error: {exc}"

    @staticmethod
    def _build_Plaintext(digest: JobDigest) -> str:
        lines: list[str] = [
            f"{settings.APP_NAME} Deal Digest — {digest.job_name}",
            f"Total matches: {digest.total_count}",
            "=" * 50,
        ]

        sections = [
            ("PRICE DROPS", digest.price_drops),
            ("NEW VERIFIED DEALS", digest.priced_new_deals),
            ("[UNPRICED / DOHODOU]", digest.unpriced_deals),
        ]

        for heading, items in sections:
            if not items:
                continue
            lines.append(f"\n--- {heading} ---")
            for item in items:
                listing = item.listing
                price_str = build_price_breakdown(item)
                if (
                    item.match.alert_type == AlertType.PRICE_DROP
                    and item.match.price_drop is not None
                ):
                    pd = item.match.price_drop
                    old_s = format_currency(pd.old_price_base, pd.base_currency)
                    new_s = format_currency(pd.new_price_base, pd.base_currency)
                    price_str = f"DROP {old_s} -> {new_s} (-{pd.drop_percent:.1f}%) | {price_str}"
                loc = f" | Location: {listing.location}" if listing.location else ""
                lines.append(f"* [{listing.platform.upper()}] {listing.title}")
                lines.append(f"  Price: {price_str}{loc}")
                lines.append(f"  Link:  {listing.url}\n")

        return "\n".join(lines)

    @staticmethod
    def _build_html(digest: JobDigest) -> str:
        sections_html: list[str] = []

        section_defs = [
            ("📉 Price Drops", "#fef2f2", "#dc2626", digest.price_drops),
            ("🆕 New Verified Deals", "#eff6ff", "#2563eb", digest.priced_new_deals),
            ("🏷️ [UNPRICED / DOHODOU]", "#f9fafb", "#4b5563", digest.unpriced_deals),
        ]

        for title, bg_color, accent_color, items in section_defs:
            if not items:
                continue
            rows_html: list[str] = []
            for item in items:
                listing = item.listing
                match = item.match
                price_str = html.escape(build_price_breakdown(item))
                drop_badge = ""
                if match.alert_type == AlertType.PRICE_DROP and match.price_drop is not None:
                    pd = match.price_drop
                    old_s = html.escape(format_currency(pd.old_price_base, pd.base_currency))
                    new_s = html.escape(format_currency(pd.new_price_base, pd.base_currency))
                    drop_badge = (
                        f"<div style='color:#dc2626;font-weight:bold;font-size:13px;'>"
                        f"Price Drop: {old_s} &rarr; {new_s} (-{pd.drop_percent:.1f}%)"
                        f"</div>"
                    )

                loc_html = (
                    f"<span style='color:#6b7280;font-size:12px;'>📍 {html.escape(listing.location)}</span>"
                    if listing.location
                    else ""
                )
                rows_html.append(
                    f"<div style='padding:12px;border-bottom:1px solid #e5e7eb;background:#ffffff;'>"
                    f"<div style='font-size:14px;font-weight:600;'>"
                    f"<span style='color:{accent_color};'>[{html.escape(listing.platform.upper())}]</span> "
                    f"<a href='{html.escape(listing.url, quote=True)}' style='color:#111827;text-decoration:none;'>"
                    f"{html.escape(listing.title)}</a></div>"
                    f"{drop_badge}"
                    f"<div style='margin-top:4px;font-size:13px;color:#1f2937;'>"
                    f"<strong>{price_str}</strong> {loc_html}</div>"
                    f"</div>"
                )

            sections_html.append(
                f"<div style='margin-top:16px;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;'>"
                f"<div style='background:{bg_color};padding:10px 12px;font-weight:bold;color:{accent_color};'>"
                f"{title} ({len(items)})</div>"
                f"{''.join(rows_html)}"
                f"</div>"
            )

        return (
            f"<html><body style='font-family:system-ui,-apple-system,sans-serif;background:#f3f4f6;padding:20px;'>"
            f"<div style='max-width:640px;margin:0 auto;background:#ffffff;padding:20px;border-radius:10px;'>"
            f"<h2 style='margin:0 0 8px 0;color:#111827;'>{html.escape(settings.APP_NAME)}: {html.escape(digest.job_name)}</h2>"
            f"<p style='margin:0;color:#6b7280;font-size:14px;'>Found {digest.total_count} matching listing(s) in this scan cycle.</p>"
            f"{''.join(sections_html)}"
            f"</div></body></html>"
        )


class NotificationDispatcher:
    """
    Coordinates multi-channel batch digest delivery per user and dispatches
    one-time Scraper Health Warnings to system administrators.
    """

    def __init__(self) -> None:
        self.telegram = TelegramNotifier()
        self.ntfy = NtfyNotifier()
        self.email = EmailNotifier()
        self._channels: dict[str, BaseNotifier] = {
            self.telegram.channel_name: self.telegram,
            self.ntfy.channel_name: self.ntfy,
            self.email.channel_name: self.email,
        }

    async def dispatch_job_digest(self, user: User, digest: JobDigest) -> bool:
        """
        Sends `digest` to all channels enabled by `user`.
        Returns True if at least one enabled channel succeeded, or if the user
        has no notification channels enabled (so listings are still marked seen in the UI).
        """
        if not digest.has_items:
            return True

        active_notifiers: list[BaseNotifier] = []
        if user.notify_telegram and self.telegram.is_configured(user):
            active_notifiers.append(self.telegram)
        if user.notify_ntfy and self.ntfy.is_configured(user):
            active_notifiers.append(self.ntfy)
        if user.notify_email and self.email.is_configured(user):
            active_notifiers.append(self.email)

        if not active_notifiers:
            logger.debug(
                "User '%s' has no active/configured notification channels; marking digest seen for dashboard.",
                user.username,
            )
            return True

        any_succeeded = False
        for notifier in active_notifiers:
            ok, msg = await notifier.send_digest(user, digest)
            if ok:
                any_succeeded = True
                logger.info(
                    "Dispatched %s digest for job '%s' to user '%s' (%d items)",
                    notifier.channel_name,
                    digest.job_name,
                    user.username,
                    digest.total_count,
                )
            else:
                logger.warning(
                    "Failed to dispatch %s digest for job '%s' to user '%s': %s",
                    notifier.channel_name,
                    digest.job_name,
                    user.username,
                    msg,
                )

        return any_succeeded

    async def send_test_channel(
        self, user: User, channel: str
    ) -> tuple[bool, str]:
        notifier = self._channels.get(channel.strip().lower())
        if notifier is None:
            return False, f"Unknown notification channel '{channel}'."
        if not notifier.is_configured(user):
            return (
                False,
                f"Channel '{channel}' is missing required credentials. Save your settings first.",
            )
        return await notifier.send_test(user)

    async def notify_admins_scraper_health(
        self,
        session: AsyncSession,
        platform: str,
        consecutive_failures: int,
        error_message: str,
        backoff_until: datetime,
    ) -> None:
        """
        Sends a one-time Scraper Health Warning to all admin users when an adapter
        crosses `settings.MAX_CONSECUTIVE_FAILURES`.
        """
        stmt = select(User).where(User.is_admin.is_(True))
        result = await session.execute(stmt)
        admins = result.scalars().all()
        if not admins:
            return

        title = f"Scraper Health Warning: {platform.upper()}"
        body = (
            f"Marketplace adapter '{platform.upper()}' has failed {consecutive_failures} "
            f"consecutive times and entered exponential backoff.\n\n"
            f"Last Error: {error_message}\n"
            f"Paused Until (UTC): {backoff_until.strftime('%Y-%m-%d %H:%M:%S UTC')}\n\n"
            f"Please inspect container logs or verify whether {platform} changed its DOM/API."
        )

        for admin in admins:
            for enabled, notifier in (
                (admin.notify_telegram, self.telegram),
                (admin.notify_ntfy, self.ntfy),
                (admin.notify_email, self.email),
            ):
                if enabled and notifier.is_configured(admin):
                    ok, info = await notifier.send_system_alert(admin, title, body)
                    if not ok:
                        logger.warning(
                            "Failed sending health alert via %s to admin '%s': %s",
                            notifier.channel_name,
                            admin.username,
                            info,
                        )


notification_dispatcher = NotificationDispatcher()
