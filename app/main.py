import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.auth import AuthenticationRequired
from app.config import get_settings
from app.database import init_db
from app.services.notifier import format_currency
from app.services.scheduler import ensure_utc, scheduler_service
from app.instance_lock import InstanceLock

settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def time_ago_filter(dt: Optional[datetime]) -> str:
    utc_dt = ensure_utc(dt)
    if utc_dt is None:
        return "Never"
    now = datetime.now(timezone.utc)
    diff = (now - utc_dt).total_seconds()
    if diff < -5:
        future_sec = int(abs(diff))
        if future_sec < 60:
            return f"in {future_sec}s"
        if future_sec < 3600:
            return f"in {future_sec // 60}m {future_sec % 60}s"
        return f"in {future_sec // 3600}h {(future_sec % 3600) // 60}m"
    if diff < 10:
        return "Just now"
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


def datetime_utc_filter(dt: Optional[datetime]) -> str:
    utc_dt = ensure_utc(dt)
    if utc_dt is None:
        return "—"
    return utc_dt.strftime("%Y-%m-%d %H:%M:%S UTC")


templates.env.filters["time_ago"] = time_ago_filter
templates.env.filters["datetime_utc"] = datetime_utc_filter
templates.env.filters["format_currency"] = format_currency
templates.env.globals["app_name"] = settings.APP_NAME


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Starting %s...", settings.APP_NAME)
    lock = InstanceLock(settings.DATABASE_URL)
    lock.acquire()
    try:
        await init_db()
        await scheduler_service.start()
        try:
            yield
        finally:
            logger.info("Shutting down %s...", settings.APP_NAME)
            await scheduler_service.stop()
    finally:
        lock.release()


app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.exception_handler(AuthenticationRequired)
async def auth_exception_handler(
    request: Request, _exc: AuthenticationRequired
) -> Response:
    if request.headers.get("HX-Request") == "true":
        return Response(
            status_code=200,
            headers={"HX-Redirect": "/login"},
        )
    return RedirectResponse(url="/login", status_code=303)


@app.get("/healthz", response_class=JSONResponse, include_in_schema=False)
async def healthcheck() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "app": settings.APP_NAME,
            "active_job_id": scheduler_service.current_job_id,
        }
    )


# Register modular routers after `templates` initialization to avoid circular imports
from app.routers import auth_routes, dashboard, jobs, settings as settings_router  # noqa: E402

app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(jobs.router)
app.include_router(settings_router.router)
from app.routers import sources
app.include_router(sources.router)
