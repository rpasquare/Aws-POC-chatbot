#!/usr/bin/env bash
# Starts the API. Reads DATABASE_URL/AWS_REGION/S3_BUCKET from a .env file
# in this directory (see README.md).
#
# AWS_PROFILE (for SSO credentials) must be set in your shell before
# running this script -- it cannot be read from .env, since boto3 reads it
# directly from the environment. Warn rather than silently falling back to
# whatever default credentials boto3 finds, which can be a different,
# wrong identity with no S3 access -- a failure mode that looks like a
# generic "not found" error rather than a credentials problem.
if [ -z "${AWS_PROFILE:-}" ]; then
  echo "warning: AWS_PROFILE is not set in this shell." >&2
  echo "  S3 calls (uploads, page images, parsed output) will use boto3's" >&2
  echo "  default credential resolution, which may be wrong or missing." >&2
  echo "  Run: export AWS_PROFILE=your-profile-name" >&2
fi
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
