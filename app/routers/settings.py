from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import get_settings
from app.database import get_db
from app.main import templates
from app.models import User
from app.routers.jobs import render_toast_html
from app.services.currency import currency_service
from app.services.notifier import notification_dispatcher

router = APIRouter(prefix="/settings", tags=["settings"])
settings = get_settings()


@router.get("", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "current_user": current_user,
            "supported_currencies": settings.SUPPORTED_CURRENCIES,
            "fx_rates": currency_service.get_all_rates(),
            "default_ntfy_url": settings.NTFY_DEFAULT_SERVER_URL,
            "has_shared_tg_token": bool(settings.TELEGRAM_DEFAULT_BOT_TOKEN.strip()),
            "has_smtp_configured": bool(settings.SMTP_HOST.strip()),
        },
    )


@router.post("", response_class=HTMLResponse)
async def update_settings(
    request: Request,
    base_currency: str = Form("CZK"),
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

    toast = render_toast_html(
        request=request,
        message="Settings and notification preferences saved.",
        level="success",
        oob=False,
    )
    return HTMLResponse(content=toast)


@router.post("/test/{channel}", response_class=HTMLResponse)
async def test_notification_channel(
    channel: str,
    request: Request,
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    ok, detail = await notification_dispatcher.send_test_channel(current_user, channel)
    toast = render_toast_html(
        request=request,
        message=f"[{channel.upper()}] {detail}",
        level="success" if ok else "error",
        oob=False,
    )
    return HTMLResponse(content=toast)