# Sprint 1 demo

Shows: upload a PDF, watch it get parsed, see the extracted text.

## Prerequisites (one-time, already done)

- Docker running, `docker compose up -d db` (Postgres is already up)
- Migrations applied: `.venv/bin/python -m app.migrate up`
- AWS SSO login active: `aws sso login --profile deal-workspace-poc`

## Run the demo

```bash
export AWS_PROFILE=deal-workspace-poc
export DATABASE_URL=postgresql://deal:deal@localhost:5432/deal_workspace
export AWS_REGION=us-east-1
export S3_BUCKET=poc-document-757046862280

.venv/bin/python scripts/demo_upload_pipeline.py /path/to/any.pdf
```

Use any real PDF you have lying around, or generate the sample one used for
testing:

```bash
.venv/bin/python -c "
from tests.fixtures.pdfs import heading_and_paragraph_pdf
open('/tmp/demo_sample.pdf', 'wb').write(heading_and_paragraph_pdf())
"
.venv/bin/python scripts/demo_upload_pipeline.py /tmp/demo_sample.pdf
```

## What it shows, step by step

1. Uploads the file through the real authorisation + S3 + completion flow
2. Claims and runs the real parse job (Docling extraction, OCR routing,
   page image rendering)
3. Prints the document's final status and page count
4. Prints the extracted text blocks with headings and bounding boxes

## Cleaning up after a demo

Each run creates one document row and a few S3 objects. To remove them:

```bash
export AWS_PROFILE=deal-workspace-poc
DOC_ID=<paste the document_id printed by the script>

docker exec aws_poc-db-1 psql -U deal -d deal_workspace \
  -c "delete from documents where id = '$DOC_ID';"

aws s3 rm "s3://poc-document-757046862280/projects/00000000-0000-0000-0000-0000000000a1/documents/$DOC_ID/" --recursive
```
