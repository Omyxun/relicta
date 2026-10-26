from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
     JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    base_currency: Mapped[str] = mapped_column(String(8), default="CZK", nullable=False)

    # Notification credentials & toggles
    telegram_bot_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    telegram_chat_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    ntfy_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    ntfy_topic: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    ntfy_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email_address: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notify_ntfy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    search_jobs: Mapped[list["SearchJob"]] = relationship(
        "SearchJob",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SearchJob(Base):
    __tablename__ = "search_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    platforms: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    search_queries: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    min_price: Mapped[float] = mapped_column(Float, default=100.0, nullable=False)
    max_price: Mapped[float] = mapped_column(Float, default=10000.0, nullable=False)
    shipping_buffer: Mapped[float] = mapped_column(Float, default=85.0, nullable=False)

    allow_unpriced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    require_shipping: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    required_keywords: Mapped[str] = mapped_column(Text, default="", nullable=False)
    excluded_keywords: Mapped[str] = mapped_column(
        Text,
        default="koupím, sháním, poptávám, vyměním, na díly, nefunkční, prasklý, pouze krabice",
        nullable=False,
    )

    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=300, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=True, index=True
    )
    total_matches_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="search_jobs")
    seen_listings: Mapped[list["SeenListing"]] = relationship(
        "SeenListing",
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SeenListing(Base):
    __tablename__ = "seen_listings"
    __table_args__ = (
        UniqueConstraint("job_id", "platform", "external_id", name="uq_job_platform_extid"),
        Index("ix_seen_platform_extid", "platform", "external_id"),
        Index("ix_seen_last_seen_at", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("search_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    image_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)

    raw_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    raw_currency: Mapped[str] = mapped_column(String(8), default="CZK", nullable=False)
    total_price_base: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_unpriced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    description_cached: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    last_notified_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    job: Mapped["SearchJob"] = relationship("SearchJob", back_populates="seen_listings")


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"

    currency_code: Mapped[str] = mapped_column(String(8), primary_key=True)
    rate_to_czk: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class ScraperHealth(Base):
    __tablename__ = "scraper_health"

    platform: Mapped[str] = mapped_column(String(32), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    backoff_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )