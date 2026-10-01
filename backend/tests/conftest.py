"""Shared test fixtures.

Sets the required configuration environment variables before any test
imports `app.config`, so tests don't depend on a `.env` file existing.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql://deal:deal@localhost:5432/deal_workspace_test")
os.environ.setdefault("AWS_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "deal-workspace-dev")
os.environ.setdefault("EXTRACTION_SERVICE_URL", "http://localhost:8001")
