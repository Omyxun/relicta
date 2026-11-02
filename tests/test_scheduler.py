import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from selectolax.lexbor import LexborHTMLParser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth import get_current_user
from app.database import Base, get_db
from app.main import app
from app.models import ScraperHealth, SearchJob, User, utc_now
from app.services.scheduler import SchedulerService, ensure_utc, settings


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    async def run_worker_once(self, scheduler, due_lookup):
        async def execute(*args, **kwargs):
            scheduler._stop_event.set()

        with (
            patch.object(scheduler, "_run_maintenance_if_due", new_callable=AsyncMock),
            patch.object(scheduler, "_pop_next_due_job_id", side_effect=due_lookup) as lookup,
            patch.object(scheduler, "_run_single_job_by_id", side_effect=execute) as run,
            patch("app.services.scheduler.random.uniform", return_value=0),
            patch.object(settings, "SCHEDULER_TICK_SECONDS", 300),
        ):
            await asyncio.wait_for(scheduler._worker_loop(), timeout=10)
        return lookup, run

    async def test_new_job_has_priority_over_regular_due_jobs_and_is_deduplicated(self):
        scheduler = SchedulerService()
        self.assertTrue(scheduler.trigger_job_now(42, is_manual=False))
        self.assertFalse(scheduler.trigger_job_now(42, is_manual=False))
        lookup, run = await self.run_worker_once(scheduler, lambda: 1)
        lookup.assert_not_awaited()
        run.assert_awaited_once_with(42, is_manual=False)
        self.assertFalse(scheduler.is_job_queued_or_running(42))

    async def test_new_job_arriving_during_due_lookup_keeps_priority(self):
        scheduler = SchedulerService()

        async def lookup():
            scheduler.trigger_job_now(42, is_manual=False)
            return 1

        _, run = await self.run_worker_once(scheduler, lookup)
        run.assert_awaited_once_with(42, is_manual=False)

    async def test_schedule_change_during_due_lookup_does_not_lose_wake_signal(self):
        scheduler = SchedulerService()
        calls = 0

        async def lookup():
            nonlocal calls
            calls += 1
            if calls == 1:
                scheduler.notify_schedule_changed()
                return None
            return 42

        _, run = await self.run_worker_once(scheduler, lookup)
        self.assertEqual(calls, 2)
        run.assert_awaited_once_with(42, is_manual=False)

    async def test_manual_run_keeps_manual_behavior(self):
        scheduler = SchedulerService()
        scheduler.trigger_job_now(42)
        _, run = await self.run_worker_once(scheduler, lambda: None)
        run.assert_awaited_once_with(42, is_manual=True)


class FirstScanTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=".")
        database_path = Path(self.directory.name) / "test.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            self.user = User(username="test", password_hash="hash", base_currency="CZK")
            session.add(self.user)
            await session.commit()

        self.scheduler = SchedulerService()

        async def db_override():
            async with self.sessions() as session:
                yield session

        app.dependency_overrides[get_db] = db_override
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.router_patch = patch("app.routers.jobs.scheduler_service", self.scheduler)
        self.session_patch = patch("app.services.scheduler.AsyncSessionLocal", self.sessions)
        self.router_patch.start()
        self.session_patch.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        app.dependency_overrides.clear()
        self.session_patch.stop()
        self.router_patch.stop()
        await self.engine.dispose()
        self.directory.cleanup()

    async def create_job(self):
        response = await self.client.post("/jobs", data={
            "name": "New search", "platforms": "bazos",
            "search_queries_raw": "thinkpad", "poll_interval_seconds": "300",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["HX-Redirect"], "/jobs")
        async with self.sessions() as session:
            return (await session.execute(select(SearchJob))).scalar_one()

    async def test_create_queues_first_scan_then_schedules_repeat_after_completion(self):
        job = await self.create_job()
        self.assertTrue(self.scheduler.is_job_queued_or_running(job.id))
        self.assertIsNone(job.last_run_at)
        self.assertLessEqual(ensure_utc(job.next_run_at), utc_now())

        queued_page = await self.client.get("/jobs")
        self.assertIn("Queued to scan", queued_page.text)
        tree = LexborHTMLParser(queued_page.text)
        self.assertEqual(tree.css_first("#job-rows").attributes["hx-trigger"], "every 5s")
        self.assertEqual(tree.css_first("tr[id]").attributes["hx-select"], "unset")

        scraper = AsyncMock()

        async def search(**kwargs):
            page = await self.client.get("/jobs")
            self.assertIn("Scanning now", page.text)
            self.scheduler._stop_event.set()
            return []

        scraper.search.side_effect = search
        with (
            patch.object(self.scheduler, "_run_maintenance_if_due", new_callable=AsyncMock),
            patch("app.services.scheduler.get_scraper", return_value=scraper),
            patch("app.services.scheduler.random.uniform", return_value=0),
            patch("app.services.scheduler.random.randint", return_value=0),
        ):
            await asyncio.wait_for(self.scheduler._worker_loop(), timeout=10)
        scraper.search.assert_awaited_once()
        async with self.sessions() as session:
            completed = await session.get(SearchJob, job.id)
            self.assertIsNotNone(completed.last_run_at)
            self.assertEqual(completed.next_run_at - completed.last_run_at, timedelta(seconds=300))
        page = await self.client.get("/jobs")
        self.assertNotIn("Queued to scan", page.text)
        self.assertNotIn("Scanning now", page.text)
        self.assertIn("Next: in 4m", page.text)

    async def test_resuming_queues_a_scan_but_pausing_before_execution_prevents_it(self):
        job = await self.create_job()
        # Pause before the automatically queued scan reaches the worker.
        response = await self.client.post(f"/jobs/{job.id}/toggle")
        self.assertEqual(response.status_code, 200)
        with patch.object(self.scheduler, "_execute_job", new_callable=AsyncMock) as execute:
            await self.scheduler._run_single_job_by_id(job.id, is_manual=False)
        execute.assert_not_awaited()

        # Consume the skipped entry, then resume the paused job.
        self.scheduler._immediate_queue.get_nowait()
        self.scheduler._queued_job_ids.clear()
        response = await self.client.post(f"/jobs/{job.id}/toggle")
        self.assertIn("Queued to scan", response.text)
        self.assertEqual(self.scheduler._immediate_queue.get_nowait(), (job.id, False))

    async def test_automatic_first_scan_respects_marketplace_backoff(self):
        job = await self.create_job()
        async with self.sessions() as session:
            session.add(ScraperHealth(platform="bazos", consecutive_failures=5,
                                      backoff_until=utc_now() + timedelta(minutes=10)))
            await session.commit()
        scraper = AsyncMock()
        with patch("app.services.scheduler.get_scraper", return_value=scraper):
            await self.scheduler._run_single_job_by_id(job.id, is_manual=False)
        scraper.search.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
