"""Configuration, loaded from the environment and validated once at startup.

Every value the application needs is declared here. Values without a default
are required: if one is missing the process fails on startup rather than
failing later inside a request or a job handler.
"""

from __future__ import annotations

import re
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REGION_PATTERN = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- required ---------------------------------------------------------
    database_url: str
    """Postgres DSN, e.g. postgresql://user:pass@db:5432/deal_workspace"""

    aws_region: str
    """Region every resource must live in. Requirement 12.4."""

    s3_bucket: str
    """Bucket holding originals, parse output and page images."""

    extraction_service_url: str
    """Base URL of the standalone PDF extraction service (Pdf-To-Knowledge).
    The parse stage submits jobs here instead of extracting in-process."""

    # --- storage ----------------------------------------------------------
    s3_endpoint_url: str | None = None
    """Set only for local object-storage emulation."""

    # --- upload limits ----------------------------------------------------
    max_upload_bytes: int = Field(default=100 * 1024 * 1024, gt=0)
    upload_url_expiry_seconds: int = Field(default=900, gt=0)
    page_image_url_expiry_seconds: int = Field(default=300, gt=0)
    allowed_content_types: tuple[str, ...] = ("application/pdf",)

    # --- pipeline ---------------------------------------------------------
    extraction_poll_seconds: float = Field(default=2.0, gt=0)
    """Delay between polls of the extraction service's job status."""

    extraction_poll_timeout_seconds: float = Field(default=600.0, gt=0)
    """How long the parse handler waits for the extraction service before
    giving up and letting the job fail/retry."""

    page_image_dpi: int = Field(default=110, gt=0)
    chunk_target_tokens: int = Field(default=350, gt=0)
    chunk_overlap_tokens: int = Field(default=60, ge=0)
    job_max_attempts: int = Field(default=3, gt=0)
    job_stale_after_seconds: int = Field(default=900, gt=0)
    job_poll_seconds: float = Field(default=2.0, gt=0)
    retention_days: int = Field(default=30, gt=0)

    # --- retrieval and models --------------------------------------------
    embed_model: str = "amazon.titan-embed-text-v2:0"
    embed_dimensions: int = Field(default=512, gt=0)
    answer_model: str = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    retrieval_top_k: int = Field(default=8, gt=0)
    rrf_k: int = Field(default=60, gt=0)
    relevance_threshold: float = 0.02

    # --- auth (project-invites feature) -----------------------------------
    session_expiry_seconds: int = Field(default=60 * 60 * 24 * 30, gt=0)
    """How long a session issued at invite acceptance remains valid. Default
    30 days; this is a real credential, separate from the X-User-Id
    placeholder the rest of the application still uses."""

    session_cookie_secure: bool = True
    """Whether the session cookie requires HTTPS. Set to false only for
    local development over plain http://localhost; must be true wherever
    the app is reachable over the network."""

    csrf_cookie_name: str = "csrf_token"
    """Name of the double-submit CSRF cookie set alongside the session
    cookie (Requirement 15.6). Unlike the session cookie this one is not
    HttpOnly, since the frontend must read it to echo it back as a
    header; its security comes from same-origin policy, not secrecy."""

    sign_in_rate_limit_max_attempts: int = Field(default=5, gt=0)
    """Failed sign-in attempts allowed per email before Requirement 1.4/15.4's
    rate limit rejects further attempts with 429. Five is generous enough
    for a real user who mistypes a password a couple of times, while still
    bounding an offline-style guessing campaign against one address."""

    sign_in_rate_limit_window_seconds: int = Field(default=15 * 60, gt=0)
    """Window the attempt count above is measured over. Default 15 minutes:
    long enough that the limit meaningfully slows guessing, short enough
    that a genuine user who got locked out is not stuck for long."""

    email_backend: str = "smtp"
    """'ses' for production, 'smtp' (pointed at Mailpit) for local
    development. Switching environments is this one value, not a code
    change."""

    email_from_address: str = "invites@deal-workspace.local"
    smtp_host: str = "localhost"
    smtp_port: int = Field(default=1025, gt=0)
    """Mailpit's default SMTP port locally."""

    frontend_base_url: str = "http://localhost:5174"
    """Used to build the accept-invite link included in the email."""

    @field_validator("email_backend")
    @classmethod
    def _email_backend_is_known(cls, value: str) -> str:
        if value not in ("ses", "smtp"):
            raise ValueError(f"email_backend must be 'ses' or 'smtp', got {value!r}")
        return value

    # --- runtime ----------------------------------------------------------
    db_pool_min_size: int = Field(default=1, ge=1)
    db_pool_max_size: int = Field(default=10, ge=1)
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _dsn_looks_like_postgres(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgres://")):
            raise ValueError("database_url must be a postgresql:// DSN")
        return value

    @field_validator("aws_region")
    @classmethod
    def _region_is_well_formed(cls, value: str) -> str:
        if not _REGION_PATTERN.match(value):
            raise ValueError(f"aws_region {value!r} is not a valid AWS region name")
        return value

    @field_validator("chunk_overlap_tokens")
    @classmethod
    def _overlap_below_target(cls, value: int, info) -> int:
        target = info.data.get("chunk_target_tokens")
        if target is not None and value >= target:
            raise ValueError("chunk_overlap_tokens must be below chunk_target_tokens")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache settings. Raises pydantic.ValidationError when invalid."""
    return Settings()
