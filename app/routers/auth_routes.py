from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    clear_session_cookie,
    get_optional_user,
     get_user_count,
    hash_password,
    is_registration_open,
    password_needs_rehash,
    set_session_cookie,
    verify_password,
)
from app.config import get_settings
from app.database import get_db
from app.main import templates
from app.models import User

router = APIRouter(tags=["auth"])
settings = get_settings()


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
) -> Response:
    if current_user is not None:
        return RedirectResponse(url="/", status_code=303)

    user_count = await get_user_count(db)
    if user_count == 0:
        return RedirectResponse(url="/register", status_code=303)

    reg_open = await is_registration_open(db)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": None,
            "registration_open": reg_open,
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    clean_username = username.strip()
    stmt = select(User).where(User.username == clean_username)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    reg_open = await is_registration_open(db)
    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Invalid username or password.",
                "registration_open": reg_open,
            },
            status_code=401,
        )

    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        await db.commit()

    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, user, request=request)
    return response


@router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_user),
) -> Response:
    if current_user is not None:
        return RedirectResponse(url="/", status_code=303)

    reg_open = await is_registration_open(db)
    user_count = await get_user_count(db)
    if not reg_open:
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={
            "error": None,
            "is_first_user": user_count == 0,
            "supported_currencies": settings.SUPPORTED_CURRENCIES,
            "default_currency": settings.DEFAULT_BASE_CURRENCY,
        },
    )


@router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    base_currency: str = Form("CZK"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    reg_open = await is_registration_open(db)
    user_count = await get_user_count(db)
    if not reg_open:
        return RedirectResponse(url="/login", status_code=303)

    clean_username = username.strip()
    error: Optional[str] = None

    if len(clean_username) < 3 or len(clean_username) > 64:
        error = "Username must be between 3 and 64 characters."
    elif len(password) < 8:
        error = "Password must be at least 8 characters long."
    elif password != password_confirm:
        error = "Passwords do not match."
    else:
        existing_stmt = select(User).where(User.username == clean_username)
        existing_res = await db.execute(existing_stmt)
        if existing_res.scalar_one_or_none() is not None:
            error = "Username is already taken."

    if error:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context={
                "error": error,
                "is_first_user": user_count == 0,
                "supported_currencies": settings.SUPPORTED_CURRENCIES,
                "default_currency": base_currency,
            },
            status_code=400,
        )

    normalized_curr = base_currency.strip().upper()
    if normalized_curr not in settings.SUPPORTED_CURRENCIES:
        normalized_curr = settings.DEFAULT_BASE_CURRENCY

    new_user = User(
        username=clean_username,
        password_hash=hash_password(password),
        is_admin=(user_count == 0),
        base_currency=normalized_curr,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    response = RedirectResponse(url="/", status_code=303)
    set_session_cookie(response, new_user, request=request)
    return response


@router.post("/logout")
@router.get("/logout")
async def logout() -> Response:
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response