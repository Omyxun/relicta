import re
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.main import templates
from app.models import SearchJob, User, utc_now
from app.scrapers import get_all_scrapers
from app.services.scheduler import scheduler_service

async def available_sources(db):
    from app.models import CustomSource
    sources = (await db.execute(select(CustomSource))).scalars().all()
    return {**get_all_scrapers(), **{f"custom:{s.id}": s.name for s in sources}}


router = APIRouter(prefix="/jobs", tags=["jobs"])

DEFAULT_EXCLUDED_KEYWORDS = (
    "koupím, sháním, poptávám, vyměním, na díly, nefunkční, prasklý, pouze krabice"
)


def parse_search_queries_input(raw_input: str) -> list[str]:
    """Splits multi-line or comma-separated search queries into a deduplicated list."""
    if not raw_input:
        return []
    parts = re.split(r"[\r\n,]+", raw_input)
    seen: set[str] = set()
    cleaned: list[str] = []
    for p in parts:
        item = p.strip()
        if item and item.lower() not in seen:
            seen.add(item.lower())
            cleaned.append(item)
    return cleaned


def render_toast_html(
    request: Request,
    message: str,
    level: str = "success",
    oob: bool = True,
) -> str:
    template = templates.get_template("partials/toast.html")
    return template.render(
        {
            "request": request,
            "message": message,
            "level": level,
            "oob": oob,
        }
    )


def render_job_row_with_toast(
    request: Request,
    job: SearchJob,
    current_user: User,
    toast_message: str,
    toast_level: str = "success",
) -> HTMLResponse:
    row_tpl = templates.get_template("partials/job_row.html")
    row_html = row_tpl.render(
        {
            "request": request,
            "job": job,
            "current_user": current_user,
            "is_running": scheduler_service.current_job_id == job.id,
            "is_queued": scheduler_service.is_job_queued_or_running(job.id)
            and scheduler_service.current_job_id != job.id,
        }
    )
    toast_html = render_toast_html(
        request, message=toast_message, level=toast_level, oob=True
    )
    return HTMLResponse(content=f"{row_html}\n{toast_html}")


async def _get_owned_job(
    job_id: int, current_user: User, db: AsyncSession
) -> SearchJob:
    stmt = select(SearchJob).where(
        SearchJob.id == job_id,
        SearchJob.user_id == current_user.id,
    )
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Search job not found.")
    return job


@router.get("", response_class=HTMLResponse)
async def list_jobs(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    stmt = (
        select(SearchJob)
        .where(SearchJob.user_id == current_user.id)
        .order_by(SearchJob.id.desc())
    )
    result = await db.execute(stmt)
    jobs = list(result.scalars().all())
    running_ids = {
        j.id for j in jobs if scheduler_service.is_job_queued_or_running(j.id)
    }

    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={
            "current_user": current_user,
            "jobs": jobs,
            "running_ids": running_ids,
            "current_job_id": scheduler_service.current_job_id,
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def new_job_modal(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="partials/job_modal.html",
        context={
            "current_user": current_user,
            "job": None,
            "available_platforms": list((await available_sources(db)).keys()),
            "default_excluded": DEFAULT_EXCLUDED_KEYWORDS,
            "error": None,
        },
    )


@router.get("/{job_id}/edit", response_class=HTMLResponse)
async def edit_job_modal(
    job_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    job = await _get_owned_job(job_id, current_user, db)
    return templates.TemplateResponse(
        request=request,
        name="partials/job_modal.html",
        context={
            "current_user": current_user,
            "job": job,
            "available_platforms": list((await available_sources(db)).keys()),
            "default_excluded": DEFAULT_EXCLUDED_KEYWORDS,
            "error": None,
        },
    )


@router.post("", response_class=HTMLResponse)
async def create_job(
    request: Request,
    name: str = Form(...),
    platforms: list[str] = Form(default=[]),
    search_queries_raw: str = Form(...),
    min_price: float = Form(100.0),
    max_price: float = Form(10000.0),
    shipping_buffer: float = Form(85.0),
    allow_unpriced: Optional[str] = Form(default=None),
    require_shipping: Optional[str] = Form(default=None),
    required_keywords: str = Form(default=""),
    excluded_keywords: str = Form(default=DEFAULT_EXCLUDED_KEYWORDS),
    poll_interval_seconds: int = Form(300),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    valid_platforms = [
        p.strip().lower()
        for p in platforms
        if p.strip().lower() in await available_sources(db)
    ]
    queries = parse_search_queries_input(search_queries_raw)

    error: Optional[str] = None
    if not name.strip():
        error = "Job name is required."
    elif not valid_platforms:
        error = "Select at least one marketplace platform."
    elif not queries:
        error = "Provide at least one search query."
    elif min_price < 0 or max_price <= 0 or min_price > max_price:
        error = "Minimum price must be >= 0 and less than or equal to maximum price."

    if error:
        return templates.TemplateResponse(
            request=request,
            name="partials/job_modal.html",
            context={
                "current_user": current_user,
                "job": None,
                "available_platforms": list((await available_sources(db)).keys()),
                "default_excluded": excluded_keywords,
                "error": error,
            },
        )

    job = SearchJob(
        user_id=current_user.id,
        name=name.strip()[:128],
        platforms=valid_platforms,
        search_queries=queries,
        min_price=round(min_price, 2),
        max_price=round(max_price, 2),
        shipping_buffer=round(max(0.0, shipping_buffer), 2),
        allow_unpriced=bool(allow_unpriced),
        require_shipping=bool(require_shipping),
        required_keywords=required_keywords.strip(),
        excluded_keywords=excluded_keywords.strip(),
        poll_interval_seconds=max(60, int(poll_interval_seconds)),
        is_active=True,
        next_run_at=utc_now(),
    )
    db.add(job)
    await db.commit()
    scheduler_service.trigger_job_now(job.id, is_manual=False)
    await db.refresh(job)

    return Response(status_code=200, headers={"HX-Redirect": "/jobs"})


@router.post("/{job_id}", response_class=HTMLResponse)
async def update_job(
    job_id: int,
    request: Request,
    name: str = Form(...),
    platforms: list[str] = Form(default=[]),
    search_queries_raw: str = Form(...),
    min_price: float = Form(100.0),
    max_price: float = Form(10000.0),
    shipping_buffer: float = Form(85.0),
    allow_unpriced: Optional[str] = Form(default=None),
    require_shipping: Optional[str] = Form(default=None),
    required_keywords: str = Form(default=""),
    excluded_keywords: str = Form(default=""),
    poll_interval_seconds: int = Form(300),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    job = await _get_owned_job(job_id, current_user, db)
    valid_platforms = [
        p.strip().lower()
        for p in platforms
        if p.strip().lower() in await available_sources(db)
    ]
    queries = parse_search_queries_input(search_queries_raw)

    error: Optional[str] = None
    if not name.strip():
        error = "Job name is required."
    elif not valid_platforms:
        error = "Select at least one marketplace platform."
    elif not queries:
        error = "Provide at least one search query."
    elif min_price < 0 or max_price <= 0 or min_price > max_price:
        error = "Minimum price must be >= 0 and less than or equal to maximum price."

    if error:
        return templates.TemplateResponse(
            request=request,
            name="partials/job_modal.html",
            context={
                "current_user": current_user,
                "job": job,
                "available_platforms": list((await available_sources(db)).keys()),
                "default_excluded": DEFAULT_EXCLUDED_KEYWORDS,
                "error": error,
            },
        )

    job.name = name.strip()[:128]
    job.platforms = valid_platforms
    job.search_queries = queries
    job.min_price = round(min_price, 2)
    job.max_price = round(max_price, 2)
    job.shipping_buffer = round(max(0.0, shipping_buffer), 2)
    job.allow_unpriced = bool(allow_unpriced)
    job.require_shipping = bool(require_shipping)
    job.required_keywords = required_keywords.strip()
    job.excluded_keywords = excluded_keywords.strip()
    job.poll_interval_seconds = max(60, int(poll_interval_seconds))

    await db.commit()
    await db.refresh(job)

    scheduler_service.notify_schedule_changed()
    return Response(status_code=200, headers={"HX-Redirect": "/jobs"})


@router.post("/{job_id}/toggle", response_class=HTMLResponse)
async def toggle_job(
    job_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    job = await _get_owned_job(job_id, current_user, db)
    job.is_active = not job.is_active
    if job.is_active:
        job.next_run_at = utc_now()
    await db.commit()
    if job.is_active:
        scheduler_service.trigger_job_now(job.id, is_manual=False)
    else:
        scheduler_service.notify_schedule_changed()
    await db.refresh(job)

    status_str = "resumed" if job.is_active else "paused"
    return render_job_row_with_toast(
        request=request,
        job=job,
        current_user=current_user,
        toast_message=f"Job '{job.name}' {status_str}.",
        toast_level="info" if not job.is_active else "success",
    )


@router.post("/{job_id}/clone", response_class=HTMLResponse)
async def clone_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    source = await _get_owned_job(job_id, current_user, db)
    cloned = SearchJob(
        user_id=current_user.id,
        name=f"{source.name[:115]} (Copy)",
        platforms=list(source.platforms or []),
        search_queries=list(source.search_queries or []),
        min_price=source.min_price,
        max_price=source.max_price,
        shipping_buffer=source.shipping_buffer,
        allow_unpriced=source.allow_unpriced,
        require_shipping=source.require_shipping,
        required_keywords=source.required_keywords,
        excluded_keywords=source.excluded_keywords,
        poll_interval_seconds=source.poll_interval_seconds,
        is_active=False,  # Start cloned jobs paused so user can tweak before first scan
        next_run_at=None,
    )
    db.add(cloned)
    await db.commit()
    return Response(status_code=200, headers={"HX-Redirect": "/jobs"})


@router.post("/{job_id}/run", response_class=HTMLResponse)
async def run_job_now(
    job_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    job = await _get_owned_job(job_id, current_user, db)
    newly_queued = scheduler_service.trigger_job_now(job.id)
    if newly_queued:
        msg = f"Queued '{job.name}' for immediate sequential execution."
        lvl = "success"
    else:
        msg = f"'{job.name}' is already queued or currently scanning."
        lvl = "info"

    return render_job_row_with_toast(
        request=request,
        job=job,
        current_user=current_user,
        toast_message=msg,
        toast_level=lvl,
    )


@router.delete("/{job_id}", response_class=HTMLResponse)
async def delete_job(
    job_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> HTMLResponse:
    job = await _get_owned_job(job_id, current_user, db)
    job_name = job.name
    await db.delete(job)
    await db.commit()

    toast_html = render_toast_html(
        request=request,
        message=f"Deleted job '{job_name}'.",
        level="info",
        oob=True,
    )
    # Returning empty main content removes the table row via hx-swap="outerHTML",
    # while the OOB toast fragment appends to #toast-container.
    return HTMLResponse(content=toast_html)
