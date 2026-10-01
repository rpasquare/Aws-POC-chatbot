# AWS POC Workspace

Upload a PDF, ask questions about it, get answers with citations back to
the exact page and passage.

This is split across two repositories, both of which need to be running
for the full app to work:

- **This repo** — the Deal Workspace API, worker, and frontend
- **[Pdf-To-Knowledge](https://github.com/rpasquare/Pdf-To-Knowledge)** —
  a separate service that does the actual PDF text extraction (Docling +
  Textract OCR). Deal Workspace calls out to it instead of extracting
  PDFs itself.

## What needs to be running

Five things, in this order:

1. **Postgres** (with the `pgvector` extension)
2. **Pdf-To-Knowledge's API + worker** — see that repo's own README
3. **This repo's API** (`backend/`, port 8000)
4. **This repo's worker** (`backend/`, separate process from the API)
5. **Frontend** (`frontend/`, port 5174)

If step 2 isn't running, document uploads will get stuck at the parsing
stage — the worker will fail trying to reach the extraction service.

## 1. Start Postgres

```bash
cd backend
docker compose up -d db
```

This starts a `pgvector`-enabled Postgres on `localhost:5432`.

## 2. Start Pdf-To-Knowledge

Clone it separately, and follow its own README to get its API (port 8001)
and worker running. Confirm it's up before continuing:

```bash
curl http://localhost:8001/health
# {"status":"ok"}
```

## 3. Configure and run this repo's backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Copy `.env.example` to `.env` and fill in real values — at minimum
`DATABASE_URL`, `AWS_REGION`, `S3_BUCKET`, and `EXTRACTION_SERVICE_URL`
(defaults to `http://localhost:8001`, matching step 2 above).

Run migrations:
```bash
python -m app.migrate up
```

Start the API (one terminal):
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Start the worker (a separate terminal):
```bash
python -m app.worker
```

## 4. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Opens on `http://localhost:5174`, already configured to talk to the
backend on port 8000.

## Verifying everything is wired up correctly

```bash
curl http://localhost:8000/health   # this repo's API
curl http://localhost:8001/health   # Pdf-To-Knowledge's API
```

Both should return `{"status":"ok"}`. If either worker isn't running,
uploads will sit in a `pending_parse`/`parsing` state indefinitely instead
of reaching `ready`.

## Running tests

```bash
cd backend
pytest
```

Needs the same Postgres from step 1. Pdf-To-Knowledge does not need to be
running for the backend's own test suite — the extraction client is tested
against a stubbed HTTP transport, not a live service.
