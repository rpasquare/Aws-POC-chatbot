#!/usr/bin/env bash
# Delete a document and its derived data so it can be re-uploaded fresh.
#
# Usage:
#   ./reset_document.sh <document_id>
#   ./reset_document.sh --filename AAPL_test_report.pdf
#   ./reset_document.sh --all
set -euo pipefail

export AWS_PROFILE=deal-workspace-poc
export DATABASE_URL=postgresql://deal:deal@localhost:5432/deal_workspace
export AWS_REGION=us-east-1
export S3_BUCKET=poc-document-757046862280

.venv/bin/python scripts/reset_document.py "$@"
