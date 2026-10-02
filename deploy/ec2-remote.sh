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

docker compose -f backend/docker-compose.yml up -d --build
docker compose -f backend/docker-compose.yml exec -T api python -m app.migrate seed
docker compose -f backend/docker-compose.yml ps
