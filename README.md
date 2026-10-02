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

This starts a `pgvector`-enabled Postgres on `localhost:5433`.
Port 5432 is left free because a dev machine may already be running the
Pdf-To-Knowledge database there.

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

The Vite dev server proxies `/api`, `/health`, and `/ready` to port 8000,
so the page at `http://localhost:5174` does not call that port directly.

## Docker Compose on one machine

From `backend/`:

```bash
docker compose up -d --build
docker compose exec api python -m app.migrate seed
```

| Container | Software | Host port |
|---|---|---|
| `db` | PostgreSQL 17 with pgvector | `5433` |
| `api` | Python 3.12, FastAPI 0.115.6, Uvicorn 0.34.0, Pydantic 2.10.4, psycopg 3.2.3, boto3 1.35.87, httpx 0.28.1, pypdfium2 5.13.0, Pillow 11.1.0 | `8000` |
| `worker` | The same image as `api` | none |
| `frontend` | Built with Node 22, served by nginx 1.27 | `8080` |

`backend/.env` must set `AWS_REGION`, `S3_BUCKET`, and
`EXTRACTION_SERVICE_URL`. On the shared dev EC2 instance that URL is
`http://host.docker.internal:8001`, which is the Pdf-To-Knowledge API
already published on the same machine. Compose overrides `DATABASE_URL`
so the API uses the `db` container.

## Shared dev EC2

This app runs on the same instance as Pdf-To-Knowledge. It does not replace
that service. Pdf-To-Knowledge keeps port `8001`. This app uses `8000` for
the API and `8080` for the site. Its database stays inside Docker and is
published on `5433`, so it does not take port `5432`.

The first copy is manual. Later deploys are **Actions → CI/CD → Run
workflow**. That workflow runs only when you start it.

1. Session Manager into the instance.
2. Clone to `/opt/aws-poc-chatbot` with the same style of read-only deploy
   key used for Pdf-To-Knowledge. Add a second deploy key on this
   repository, or reuse a machine user that can read both private repos.
3. Create `backend/.env`:

   ```bash
   AWS_REGION=us-east-1
   S3_BUCKET=your-bucket-name
   EXTRACTION_SERVICE_URL=http://host.docker.internal:8001
   ```

4. `docker compose -f backend/docker-compose.yml up -d --build` from
   `/opt/aws-poc-chatbot`, then
   `docker compose -f backend/docker-compose.yml exec api python -m app.migrate seed`.
5. Open security-group inbound **TCP 8080** from Anywhere-IPv4. The site
   and the API are both served there. Port `8000` can stay closed.
6. On the instance role `pdf-to-knowledge-ec2`, allow
   `s3:GetObject`, `s3:PutObject`, and `s3:HeadObject` on the bucket, plus
   `bedrock:InvokeModel` in `us-east-1`. Create the S3 bucket in
   `us-east-1` and enable the Titan embed model and Claude 3.5 Sonnet in
   Amazon Bedrock. Uploads and answers need those. `/health` does not.
7. In this GitHub repository, add the same three Actions variables used by
   Pdf-To-Knowledge: `AWS_REGION`, `AWS_ROLE_ARN`, and `EC2_INSTANCE_ID`.
   The existing GitHub role can send the deploy command to this instance.

`http://PUBLIC_IP:8080/health` should return `{"status":"ok"}`.

## Running tests

```bash
cd backend
pytest
```

Needs the same Postgres from step 1. Pdf-To-Knowledge does not need to be
running for the backend's own test suite — the extraction client is tested
against a stubbed HTTP transport, not a live service.
