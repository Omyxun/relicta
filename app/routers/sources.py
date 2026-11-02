import asyncio
from dataclasses import asdict
from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.main import templates
from app.models import CustomSource, SearchJob, User, ScraperHealth
from app.scrapers.custom import extract_listings, search_url
from app.services.site_renderer import render_site, check_url

router = APIRouter(prefix="/sources")


class Configuration(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    search_url: str = Field(max_length=2048)
    search_parameter: str = Field(min_length=1, max_length=256)
    currency: str
    selectors: dict[str, str]


def editable(source, user):
    if source is None:
        raise HTTPException(404, "Source not found.")
    if source.creator_id != user.id and not user.is_admin:
        raise HTTPException(403, "Only the creator or an administrator can change this source.")


async def tested(config):
    from app.config import get_settings
    if config.currency not in get_settings().SUPPORTED_CURRENCIES:
        raise HTTPException(422, "Choose a supported currency.")
    if any(len(v) > 1000 for v in config.selectors.values()):
        raise HTTPException(422, "Selected field is too complex. Try a simpler element.")
    try:
        query = dict(parse_qsl(urlsplit(config.search_url).query))[config.search_parameter]
        url = search_url(config.search_url, config.search_parameter, query)
        page = await render_site(url)
        return extract_listings(page["html"], page["url"], "preview", config.currency, config.selectors)
    except Exception as exc:
        raise HTTPException(422, str(exc) or "The site took too long to load.") from exc


@router.get("")
async def index(request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    sources = (await db.execute(select(CustomSource))).scalars().all()
    health = (await db.execute(select(ScraperHealth))).scalars().all()
    return templates.TemplateResponse(request=request, name="sources.html", context={
        "current_user": user, "sources": sources, "health": {h.platform: h for h in health}})


@router.post("/preview")
async def preview(payload: dict, user: User = Depends(get_current_user)):
    try:
        url = str(payload.get("url", ""))
        check_url(url)
        result = await render_site(url, preview=True)
        result.pop("html")
        result["parameters"] = list(dict(parse_qsl(urlsplit(url).query)).items())
        return result
    except Exception as exc:
        raise HTTPException(422, "Could not preview this public page. Check the URL, browser installation, or site blocking.") from exc


@router.post("/test")
async def test(config: Configuration, user: User = Depends(get_current_user)):
    return [asdict(item) for item in await tested(config)]


@router.post("")
async def create(config: Configuration, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    await tested(config)
    source = CustomSource(creator_id=user.id, **config.model_dump())
    db.add(source)
    await db.commit()
    return {"id": source.id}


@router.put("/{source_id}")
async def update(source_id: int, config: Configuration, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    source = await db.get(CustomSource, source_id)
    editable(source, user)
    await tested(config)
    for key, value in config.model_dump().items():
        setattr(source, key, value)
    health = await db.get(ScraperHealth, f"custom:{source_id}")
    if health:
        health.consecutive_failures = 0
        health.backoff_until = None
        health.last_error_message = None
    await db.commit()
    return {"id": source.id}


@router.delete("/{source_id}")
async def delete(source_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    source = await db.get(CustomSource, source_id)
    editable(source, user)
    jobs = (await db.execute(select(SearchJob))).scalars().all()
    used = [f"#{j.id} {j.name}" for j in jobs if f"custom:{source_id}" in j.platforms]
    if used:
        raise HTTPException(409, "Remove this source from these jobs first: " + ", ".join(used))
    await db.delete(source)
    await db.commit()
    return {"deleted": True}
