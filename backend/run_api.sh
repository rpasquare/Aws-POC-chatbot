#!/usr/bin/env bash
export AWS_PROFILE=deal-workspace-poc
export DATABASE_URL=postgresql://deal:deal@localhost:5432/deal_workspace
export AWS_REGION=us-east-1
export S3_BUCKET=poc-document-757046862280
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
