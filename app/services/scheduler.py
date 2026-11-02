import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import ScraperHealth, SearchJob, SeenListing, utc_now
from app.scrapers import close_all_scrapers, get_all_scrapers, get_scraper
from app.services.currency import currency_service
from app.services.matcher import AlertType, ScrapedListing, listing_matcher
from app.services.notifier import (
    DigestItem,
    JobDigest,
    notification_dispatcher,
)

logger = logging.getLogger(__name__)
settings = get_settings()

job_settings_lock = asyncio.Lock()

MAX_BACKOFF_SECONDS = 86400  # Cap exponential backoff at 24 hours
PRUNE_INTERVAL_SECONDS = 86400  # Run seen_listings cleanup once every 24 hours


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensures SQLite-hydrated datetimes carry UTC timezone metadata before comparison."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class SchedulerService:
    """
    Single-threaded sequential async job queue with randomized jitter, exponential
    backoff per scraper adapter, daily ČNB/ECB FX refresh, and 45-day database pruning.
    Never executes concurrent searches against marketplaces.
    """

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._manual_queue: asyncio.Queue[int] = asyncio.Queue()
        self._queued_manual_job_ids: set[int] = set()
        self._current_job_id: Optional[int] = None
        self._last_prune_at: Optional[datetime] = None

    @property
    def current_job_id(self) -> Optional[int]:
        return self._current_job_id

    def is_job_queued_or_running(self, job_id: int) -> bool:
        return self._current_job_id == job_id or job_id in self._queued_manual_job_ids

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return

        self._stop_event.clear()
        await self._initialize_state()
        self._task = asyncio.create_task(self._worker_loop(), name="relicta-scheduler")
        logger.info("Sequential SchedulerService started.")

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=15.0)
            except asyncio.TimeoutError:
                logger.warning("Scheduler task did not exit within 15s; cancelling.")
                self._task.cancel()
            except asyncio.CancelledError:
                pass
            self._task = None

        await close_all_scrapers()
        logger.info("Sequential SchedulerService stopped cleanly.")

    def trigger_job_now(self, job_id: int) -> bool:
        """
        Enqueues a job for immediate sequential execution by the background worker.
        Returns True if newly queued, False if already queued or running.
        """
        if self.is_job_queued_or_running(job_id):
            return False
        self._queued_manual_job_ids.add(job_id)
        self._manual_queue.put_nowait(job_id)
        self._wake_event.set()
        return True

    def notify_schedule_changed(self) -> None:
        """Wakes the scheduler loop when a job is created, resumed, or edited."""
        self._wake_event.set()

    async def _initialize_state(self) -> None:
        """
        Seeds ScraperHealth rows for all registered adapters and loads initial FX rates.
        """
        async with AsyncSessionLocal() as session:
            for platform_name in get_all_scrapers():
                health = await session.get(ScraperHealth, platform_name)
                if health is None:
                    session.add(
                        ScraperHealth(
                            platform=platform_name,
                            consecutive_failures=0,
                        )
                    )
            await session.commit()

            await currency_service.load_from_db(session)
            if currency_service.needs_refresh():
                await currency_service.refresh_rates(session)

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._run_maintenance_if_due()

                # 1. Prioritize manual "Run Now" triggers from the Web GUI
                if not self._manual_queue.empty():
                    manual_job_id = await self._manual_queue.get()
                    self._queued_manual_job_ids.discard(manual_job_id)
                    await self._run_single_job_by_id(manual_job_id, is_manual=True)
                    await asyncio.sleep(random.uniform(1.5, 3.5))
                    continue

                # 2. Check next scheduled due SearchJob
                due_job_id = await self._pop_next_due_job_id()
                if due_job_id is not None:
                    await self._run_single_job_by_id(due_job_id, is_manual=False)
                    # Stagger gap between sequential jobs
                    inter_job_sleep = random.uniform(2.0, 5.0)
                    await asyncio.sleep(inter_job_sleep)
                    continue

                # 3. Wait for next tick or manual wake signal
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(),
                        timeout=settings.SCHEDULER_TICK_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Unhandled error in scheduler worker loop: %s", exc)
                await asyncio.sleep(5.0)

    async def _run_maintenance_if_due(self) -> None:
        now = utc_now()

        if currency_service.needs_refresh():
            async with AsyncSessionLocal() as session:
                await currency_service.refresh_rates(session)

        if (
            self._last_prune_at is None
            or (now - self._last_prune_at).total_seconds() >= PRUNE_INTERVAL_SECONDS
        ):
            async with AsyncSessionLocal() as session:
                pruned = await self.prune_old_seen_listings(session)
                self._last_prune_at = now
                if pruned > 0:
                    logger.info(
                        "Pruned %d stale seen_listings older than %d days.",
                        pruned,
                        settings.SEEN_LISTINGS_RETENTION_DAYS,
                    )

    async def prune_old_seen_listings(self, session: AsyncSession) -> int:
        cutoff = utc_now() - timedelta(days=settings.SEEN_LISTINGS_RETENTION_DAYS)
        stmt = delete(SeenListing).where(SeenListing.last_seen_at < cutoff)
        result = await session.execute(stmt)
        await session.commit()
        return int(result.rowcount or 0)

    async def _pop_next_due_job_id(self) -> Optional[int]:
        now = utc_now()
        async with AsyncSessionLocal() as session:
            stmt = (
                select(SearchJob.id)
                .where(
                    SearchJob.is_active.is_(True),
                    (SearchJob.next_run_at.is_(None)) | (SearchJob.next_run_at <= now),
                )
                .order_by(SearchJob.next_run_at.asc().nullsfirst(), SearchJob.id.asc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def _run_single_job_by_id(
        self, job_id: int, *, is_manual: bool = False
    ) -> None:
        self._current_job_id = job_id
        try:
            async with job_settings_lock:
                async with AsyncSessionLocal() as session:
                    stmt = (
                        select(SearchJob)
                        .options(selectinload(SearchJob.user))
                        .where(SearchJob.id == job_id)
                    )
                    result = await session.execute(stmt)
                    job = result.scalar_one_or_none()
                    if job is None:
                        return
                    if not job.is_active and not is_manual:
                        return

                    await self._execute_job(session, job, is_manual=is_manual)
        finally:
            self._current_job_id = None

    async def _execute_job(
        self,
        session: AsyncSession,
        job: SearchJob,
        *,
        is_manual: bool = False,
    ) -> None:
        user = job.user
        base_currency = (user.base_currency if user else settings.DEFAULT_BASE_CURRENCY) or "CZK"
        platforms = [p.strip().lower() for p in (job.platforms or []) if p and p.strip()]

        logger.info(
            "Executing job #%d '%s' (user=%s, platforms=%s, manual=%s)",
            job.id,
            job.name,
            user.username if user else "unknown",
            platforms,
            is_manual,
        )

        digest = JobDigest(
            job_id=job.id,
            job_name=job.name,
            base_currency=base_currency,
        )

        for idx, platform_name in enumerate(platforms):
            if self._stop_event.is_set():
                break

            scraper = get_scraper(platform_name)
            if platform_name.startswith("custom:"):
                from app.models import CustomSource
                from app.scrapers.custom import CustomScraper
                source = await session.get(CustomSource, int(platform_name.split(":")[1]))
                scraper = CustomScraper(source) if source else None
            if scraper is None:
                logger.warning("Unknown scraper platform '%s' on job #%d", platform_name, job.id)
                continue

            health = await self._get_or_create_health(session, platform_name)
            now = utc_now()
            backoff_until = ensure_utc(health.backoff_until)

            if backoff_until is not None and backoff_until > now and not is_manual:
                logger.warning(
                    "Skipping platform '%s' for job #%d (in backoff until %s)",
                    platform_name,
                    job.id,
                    backoff_until.isoformat(),
                )
                continue

            if idx > 0:
                await scraper.sleep_jitter()

            try:
                listings = await scraper.search(
                    job=job,
                    session=session,
                    base_currency=base_currency,
                )
                await self._record_scraper_success(session, health)
            except Exception as exc:
                logger.warning(
                    "Scraper '%s' failed during job #%d ('%s'): %s",
                    platform_name,
                    job.id,
                    job.name,
                    exc,
                )
                await self._record_scraper_failure(session, health, str(exc))
                continue

            await self._process_platform_listings(
                session=session,
                job=job,
                base_currency=base_currency,
                listings=listings,
                digest=digest,
            )

        # Schedule next run with randomized jitter (±settings.SCHEDULER_JITTER_SECONDS)
        now = utc_now()
        jitter = random.randint(
            -settings.SCHEDULER_JITTER_SECONDS,
            settings.SCHEDULER_JITTER_SECONDS,
        )
        interval = max(60, int(job.poll_interval_seconds) + jitter)
        job.last_run_at = now
        job.next_run_at = now + timedelta(seconds=interval)

        # Flush SeenListing rows before sending external notifications
        await session.flush()

        # Dispatch batch digest if any deals/drops were queued
        if digest.has_items and user is not None:
            delivered = await notification_dispatcher.dispatch_job_digest(user, digest)
            if delivered:
                await self._mark_digest_items_notified(session, job, digest)

        await session.commit()

    async def _process_platform_listings(
        self,
        session: AsyncSession,
        job: SearchJob,
        base_currency: str,
        listings: list[ScrapedListing],
        digest: JobDigest,
    ) -> None:
        if not listings:
            return

        ext_ids = [item.external_id for item in listings]
        platform = listings[0].platform

        stmt = select(SeenListing).where(
            SeenListing.job_id == job.id,
            SeenListing.platform == platform,
            SeenListing.external_id.in_(ext_ids),
        )
        result = await session.execute(stmt)
        existing_map: dict[str, SeenListing] = {
            row.external_id: row for row in result.scalars().all()
        }

        now = utc_now()

        for item in listings:
            existing_seen = existing_map.get(item.external_id)
            match_res = listing_matcher.evaluate_full(
                listing=item,
                job=job,
                base_currency=base_currency,
                existing_seen=existing_seen,
            )

            # Persist/update SeenListing even if rejected in Stage 2 whenever we have
            # a fetched full_description or matched listing, so we never re-fetch detail HTML.
            should_persist = (
                existing_seen is not None
                or match_res.matched
                or item.full_description is not None
            )
            if not should_persist:
                continue

            if existing_seen is None:
                seen_row = SeenListing(
                    job_id=job.id,
                    platform=item.platform,
                    external_id=item.external_id,
                    title=item.title[:512],
                    url=item.url[:1024],
                    image_url=item.image_url[:1024] if item.image_url else None,
                    raw_price=item.raw_price,
                    raw_currency=item.raw_currency,
                    total_price_base=match_res.total_price_base,
                    is_unpriced=match_res.is_unpriced,
                    description_cached=item.full_description,
                    location=item.location[:255] if item.location else None,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_notified_price=None,
                    notified=False,
                )
                session.add(seen_row)
                await session.flush()
                existing_map[item.external_id] = seen_row
            else:
                seen_row = existing_seen
                seen_row.title = item.title[:512]
                seen_row.url = item.url[:1024]
                if item.image_url:
                    seen_row.image_url = item.image_url[:1024]
                seen_row.raw_price = item.raw_price
                seen_row.raw_currency = item.raw_currency
                seen_row.total_price_base = match_res.total_price_base
                seen_row.is_unpriced = match_res.is_unpriced
                if item.full_description and not seen_row.description_cached:
                    seen_row.description_cached = item.full_description
                if item.location:
                    seen_row.location = item.location[:255]
                seen_row.last_seen_at = now

            if not match_res.matched or match_res.alert_type == AlertType.NONE:
                continue

            digest_item = DigestItem(
                seen_listing_id=seen_row.id,
                listing=item,
                match=match_res,
            )

            if match_res.alert_type == AlertType.PRICE_DROP:
                digest.price_drops.append(digest_item)
            elif match_res.alert_type == AlertType.UNPRICED_DEAL:
                digest.unpriced_deals.append(digest_item)
            elif match_res.alert_type == AlertType.NEW_DEAL:
                digest.priced_new_deals.append(digest_item)

    async def _mark_digest_items_notified(
        self,
        session: AsyncSession,
        job: SearchJob,
        digest: JobDigest,
    ) -> None:
        all_items = digest.all_items()
        if not all_items:
            return

        seen_ids = [d.seen_listing_id for d in all_items]
        stmt = select(SeenListing).where(SeenListing.id.in_(seen_ids))
        result = await session.execute(stmt)
        rows_by_id = {row.id: row for row in result.scalars().all()}

        new_matches_added = 0
        for d_item in all_items:
            row = rows_by_id.get(d_item.seen_listing_id)
            if row is None:
                continue
            if not row.notified:
                new_matches_added += 1
            row.notified = True
            if d_item.match.total_price_base is not None:
                row.last_notified_price = d_item.match.total_price_base

        job.total_matches_count = (job.total_matches_count or 0) + new_matches_added

    async def _get_or_create_health(
        self, session: AsyncSession, platform: str
    ) -> ScraperHealth:
        health = await session.get(ScraperHealth, platform)
        if health is None:
            health = ScraperHealth(platform=platform, consecutive_failures=0)
            session.add(health)
            await session.flush()
        return health

    async def _record_scraper_success(
        self, session: AsyncSession, health: ScraperHealth
    ) -> None:
        health.consecutive_failures = 0
        health.last_success_at = utc_now()
        health.last_error_message = None
        health.backoff_until = None
        await session.flush()

    async def _record_scraper_failure(
        self,
        session: AsyncSession,
        health: ScraperHealth,
        error_msg: str,
    ) -> None:
        health.consecutive_failures = (health.consecutive_failures or 0) + 1
        health.last_error_message = error_msg[:1000]

        threshold = settings.MAX_CONSECUTIVE_FAILURES
        if health.consecutive_failures >= threshold:
            exponent = min(health.consecutive_failures - threshold + 1, 10)
            backoff_seconds = min((2**exponent) * 60, MAX_BACKOFF_SECONDS)
            backoff_until = utc_now() + timedelta(seconds=backoff_seconds)
            health.backoff_until = backoff_until
            await session.flush()

            # Send a one-time alert to admins the moment the threshold is crossed
            if health.consecutive_failures == threshold:
                await notification_dispatcher.notify_admins_scraper_health(
                    session=session,
                    platform=health.platform,
                    consecutive_failures=health.consecutive_failures,
                    error_message=error_msg,
                    backoff_until=backoff_until,
                )
        else:
            await session.flush()


scheduler_service = SchedulerService()