#!/usr/bin/env bash
# Runs on the shared dev EC2 instance after the workflow checks out the commit.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f backend/.env ]]; then
  echo "Missing /opt/aws-poc-chatbot/backend/.env" >&2
  exit 1
fi

for key in AWS_REGION S3_BUCKET EXTRACTION_SERVICE_URL; do
  if ! grep -q "^${key}=" backend/.env; then
    echo "Missing ${key} in backend/.env" >&2
    exit 1
  fi
done

# Session Manager keeps only 24 KB of output. A full Docker build log is
# larger than that and the command is then marked TimedOut.
compose_log="$(mktemp)"
if ! docker compose -f backend/docker-compose.yml up -d --build >"$compose_log" 2>&1; then
  echo "docker compose up failed" >&2
  tail -n 80 "$compose_log" >&2
  exit 1
fi
tail -n 30 "$compose_log"
docker compose -f backend/docker-compose.yml exec -T api python -m app.migrate seed
docker compose -f backend/docker-compose.yml ps
