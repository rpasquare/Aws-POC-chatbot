#!/usr/bin/env bash
# Starts the worker. Reads DATABASE_URL/AWS_REGION/S3_BUCKET from a .env
# file in this directory (see README.md).
#
# AWS_PROFILE (for SSO credentials) must be set in your shell before
# running this script -- see run_api.sh for why this can't live in .env.
if [ -z "${AWS_PROFILE:-}" ]; then
  echo "warning: AWS_PROFILE is not set in this shell." >&2
  echo "  S3 calls will use boto3's default credential resolution, which" >&2
  echo "  may be wrong or missing. Run: export AWS_PROFILE=your-profile-name" >&2
fi
exec .venv/bin/python -m app.worker
