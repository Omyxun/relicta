from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings, reload_settings, update_env_file
from app.database import get_db
from app.main import templates
from app.models import User
from app.routers.jobs import render_toast_html
from app.scrapers import get_scraper
from app.services.currency import currency_service
from app.services.notifier import notification_dispatcher

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    base_curr = current_user.base_currency or "CZK"
    settings = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "current_user": current_user,
            "supported_currencies": settings.SUPPORTED_CURRENCIES,
            "fx_rates": currency_service.get_all_rates(),
            "fx_rates_to_base": currency_service.get_rates_to_base(base_curr),
            "default_ntfy_url": settings.NTFY_DEFAULT_SERVER_URL,
            "has_shared_tg_token": bool(settings.TELEGRAM_DEFAULT_BOT_TOKEN.strip()),
            "has_smtp_configured": bool(settings.SMTP_HOST.strip()),
            # eBay credentials from .env
            "ebay_client_id": settings.EBAY_CLIENT_ID,
            "ebay_client_secret": settings.EBAY_CLIENT_SECRET,
            "ebay_marketplace_id": settings.EBAY_MARKETPLACE_ID,
            "ebay_ship_to_country": settings.EBAY_DEFAULT_SHIP_TO_COUNTRY,
            "has_ebay_credentials": bool(settings.EBAY_CLIENT_ID.strip() and settings.EBAY_CLIENT_SECRET.strip()),
        },
    )


@router.post("", response_class=HTMLResponse)
async def update_settings(
    request: Request,
    base_currency: str = Form("CZK"),
    # eBay API settings saved to .env
    ebay_client_id: str = Form(""),
    ebay_client_secret: str = Form(""),
    ebay_marketplace_id: str = Form("EBAY_DE"),
    ebay_ship_to_country: str = Form("CZ"),
    # User notification settings
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    notify_telegram: Optional[str] = Form(default=None),
    ntfy_url: str = Form(""),
    ntfy_topic: str = Form(""),
    ntfy_token: str = Form(""),
    notify_ntfy: Optional[str] = Form(default=None),
    email_address: str = Form(""),
    notify_email: Optional[str] = Form(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    settings = get_settings()
    norm_curr = base_currency.strip().upper()
    if norm_curr not in settings.SUPPORTED_CURRENCIES:
        norm_curr = settings.DEFAULT_BASE_CURRENCY

    current_user.base_currency = norm_curr
    current_user.telegram_bot_token = telegram_bot_token.strip() or None
    current_user.telegram_chat_id = telegram_chat_id.strip() or None
    current_user.notify_telegram = bool(notify_telegram)

    current_user.ntfy_url = ntfy_url.strip() or None
    current_user.ntfy_topic = ntfy_topic.strip() or None
    current_user.ntfy_token = ntfy_token.strip() or None
    current_user.notify_ntfy = bool(notify_ntfy)

    current_user.email_address = email_address.strip() or None
    current_user.notify_email = bool(notify_email)

    await db.commit()
    await db.refresh(current_user)

    # Persist eBay API settings directly to .env
    clean_id = ebay_client_id.strip()
    clean_secret = ebay_client_secret.strip()
    clean_mkt = ebay_marketplace_id.strip() or "EBAY_DE"
    clean_country = ebay_ship_to_country.strip().upper() or "CZ"

    env_updates = {
        "EBAY_CLIENT_ID": clean_id,
        "EBAY_CLIENT_SECRET": clean_secret,
        "EBAY_MARKETPLACE_ID": clean_mkt,
        "EBAY_DEFAULT_SHIP_TO_COUNTRY": clean_country,
    }
    update_env_file(env_updates)
    reload_settings()

    # Reset in-memory token on scraper instance
    ebay_scraper = get_scraper("ebay")
    if ebay_scraper and hasattr(ebay_scraper, "reset_token"):
        ebay_scraper.reset_token()

    toast = render_toast_html(
        request=request,
        message="Settings and eBay credentials saved to .env.",
        level="success",
        oob=False,
    )
    return HTMLResponse(content=toast)


@router.post("/test/ebay", response_class=HTMLResponse)
async def test_ebay_credentials_endpoint(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    form = await request.form()
    settings = get_settings()
    client_id = str(form.get("ebay_client_id", "")).strip() or settings.EBAY_CLIENT_ID.strip()
    client_secret = str(form.get("ebay_client_secret", "")).strip() or settings.EBAY_CLIENT_SECRET.strip()

    if not client_id or not client_secret:
        toast = render_toast_html(
            request=request,
            message="[EBAY] Provide both Client ID and Client Secret to test authentication.",
            level="error",
            oob=False,
        )
        return HTMLResponse(content=toast)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    form_data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                settings.EBAY_OAUTH_URL,
                headers=headers,
                data=form_data,
                auth=(client_id, client_secret),
            )
            if resp.status_code == 200:
                data = resp.json()
                expires_in = data.get("expires_in", 7200)
                msg = f"[EBAY] Authentication successful! Valid OAuth2 token acquired ({expires_in}s)."
                lvl = "success"
            else:
                try:
                    payload = resp.json()
                    err = payload.get("error_description") or payload.get("error") or resp.text
                except Exception:
                    err = resp.text
                msg = f"[EBAY] eBay rejected credentials (HTTP {resp.status_code}): {err}"
                lvl = "error"
    except Exception as exc:
        msg = f"[EBAY] Network error communicating with eBay OAuth2: {exc}"
        lvl = "error"

    toast = render_toast_html(request=request, message=msg, level=lvl, oob=False)
    return HTMLResponse(content=toast)


@router.post("/test/{channel}", response_class=HTMLResponse)
async def test_notification_channel(
    channel: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    if channel.lower() == "ebay":
        return await test_ebay_credentials_endpoint(request, current_user)

    ok, detail = await notification_dispatcher.send_test_channel(current_user, channel)
    toast = render_toast_html(
        request=request,
        message=f"[{channel.upper()}] {detail}",
        level="success" if ok else "error",
        oob=False,
    )
    return HTMLResponse(content=toast)
