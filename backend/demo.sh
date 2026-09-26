#!/usr/bin/env bash
# One-command sprint 1 demo: upload a PDF and watch it get parsed.
#
# Usage: ./demo.sh path/to/file.pdf
set -euo pipefail

export AWS_PROFILE=deal-workspace-poc
export DATABASE_URL=postgresql://deal:deal@localhost:5432/deal_workspace
export AWS_REGION=us-east-1
export S3_BUCKET=poc-document-757046862280

PDF_PATH="${1:-}"
if [ -z "$PDF_PATH" ]; then
  STAMP="$(date +%s)"
  PDF_PATH="/tmp/demo_sample_${STAMP}.pdf"
  echo "No PDF given, generating a fresh sample at $PDF_PATH"
  .venv/bin/python -c "
from tests.fixtures.pdfs import build_pdf
content = (
    b'BT /F1 24 Tf 72 700 Td (Net Asset Value) Tj ET\n'
    b'BT /F1 12 Tf 72 650 Td (Reported NAV as at run ${STAMP} is \$42.6m.) Tj ET'
)
open('$PDF_PATH', 'wb').write(build_pdf(content))
"
fi

.venv/bin/python scripts/demo_upload_pipeline.py "$PDF_PATH"


