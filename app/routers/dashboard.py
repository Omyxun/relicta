from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import get_current_user
from app.database import get_db
from app.main import templates
from app.models import ScraperHealth, SearchJob, SeenListing, User
from app.scrapers import get_all_scrapers
from app.services.currency import currency_service
from app.services.scheduler import ensure_utc, scheduler_service

router = APIRouter(tags=["dashboard"])


@router.get("/", response_class=HTMLResponse)
async def dashboard_view(
    request: Request,
    platform: Optional[str] = Query(default=None),
    job_id: Optional[str] = Query(default=None),
    pricing: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)

    # Safely parse job_id when HTML <select> submits ?job_id= (empty string)
    parsed_job_id: Optional[int] = None
    if job_id and job_id.strip().isdigit():
        parsed_job_id = int(job_id.strip())

    # 1. User Jobs & Active Count
    jobs_stmt = (
        select(SearchJob)
        .where(SearchJob.user_id == current_user.id)
        .order_by(SearchJob.name.asc())
    )
    jobs_res = await db.execute(jobs_stmt)
    user_jobs = list(jobs_res.scalars().all())
    active_jobs_count = sum(1 for j in user_jobs if j.is_active)

    # 2. Next scheduled scan across user's active jobs
    next_run_at: Optional[datetime] = None
    for j in user_jobs:
        if not j.is_active:
            continue
        j_next = ensure_utc(j.next_run_at)
        if j_next is not None and (next_run_at is None or j_next < next_run_at):
            next_run_at = j_next

    next_scan_seconds: Optional[int] = None
    if next_run_at is not None:
        next_scan_seconds = max(0, int((next_run_at - now).total_seconds()))

    # 3. Matches found in last 24h for this user
    matches_24h_stmt = (
        select(func.count(SeenListing.id))
        .join(SearchJob, SeenListing.job_id == SearchJob.id)
        .where(
            SearchJob.user_id == current_user.id,
            SeenListing.notified.is_(True),
            SeenListing.first_seen_at >= since_24h,
        )
    )
    matches_24h_res = await db.execute(matches_24h_stmt)
    matches_24h_count = int(matches_24h_res.scalar_one() or 0)

    # 4. Total matches all-time for this user
    total_matches_count = sum(j.total_matches_count or 0 for j in user_jobs)

    # 5. Scraper Health statuses
    health_stmt = select(ScraperHealth)
    health_res = await db.execute(health_stmt)
    health_by_platform = {h.platform: h for h in health_res.scalars().all()}

    scraper_statuses: list[dict[str, object]] = []
    for platform_key in get_all_scrapers():
        h = health_by_platform.get(platform_key)
        backoff_dt = ensure_utc(h.backoff_until) if h else None
        in_backoff = backoff_dt is not None and backoff_dt > now
        failures = h.consecutive_failures if h else 0

        if in_backoff:
            state = "backoff"
        elif failures > 0:
            state = "degraded"
        else:
            state = "healthy"

        scraper_statuses.append(
            {
                "platform": platform_key,
                "state": state,
                "consecutive_failures": failures,
                "last_success_at": h.last_success_at if h else None,
                "last_error_message": h.last_error_message if h else None,
                "backoff_until": backoff_dt,
            }
        )

    # 6. Recent Matches Feed (filtered by optional query parameters)
    feed_stmt = (
        select(SeenListing)
        .options(selectinload(SeenListing.job))
        .join(SearchJob, SeenListing.job_id == SearchJob.id)
        .where(
            SearchJob.user_id == current_user.id,
            SeenListing.notified.is_(True),
        )
    )

    if platform and platform.strip():
        feed_stmt = feed_stmt.where(SeenListing.platform == platform.strip().lower())
    if parsed_job_id is not None:
        feed_stmt = feed_stmt.where(SeenListing.job_id == parsed_job_id)
    if pricing == "priced":
        feed_stmt = feed_stmt.where(SeenListing.is_unpriced.is_(False))
    elif pricing == "unpriced":
        feed_stmt = feed_stmt.where(SeenListing.is_unpriced.is_(True))

    feed_stmt = feed_stmt.order_by(SeenListing.last_seen_at.desc()).limit(60)
    feed_res = await db.execute(feed_stmt)
    recent_matches = list(feed_res.scalars().all())

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "current_user": current_user,
            "user_jobs": user_jobs,
            "active_jobs_count": active_jobs_count,
            "total_jobs_count": len(user_jobs),
            "matches_24h_count": matches_24h_count,
            "total_matches_count": total_matches_count,
            "next_run_at": next_run_at,
            "next_scan_seconds": next_scan_seconds,
            "currently_running_job_id": scheduler_service.current_job_id,
            "scraper_statuses": scraper_statuses,
            "recent_matches": recent_matches,
            "selected_platform": platform or "",
            "selected_job_id": parsed_job_id,
            "selected_pricing": pricing or "",
            "fx_rates": currency_service.get_all_rates(),
        },
    )