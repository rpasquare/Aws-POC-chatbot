#!/usr/bin/env bash
# Starts the worker. Reads DATABASE_URL/AWS_REGION/S3_BUCKET from a .env
# file in this directory (see README.md), or from your shell environment
# if already set (e.g. AWS_PROFILE for SSO credentials).
exec .venv/bin/python -m app.worker
