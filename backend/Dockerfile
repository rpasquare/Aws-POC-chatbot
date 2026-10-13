FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependency layer first so application edits do not invalidate it.
# Install whatever pyproject.toml declares, including cryptography, which
# the API imports at startup even when PII masking is left off.
COPY pyproject.toml ./
RUN python - <<'PY'
import subprocess
import tomllib

with open("pyproject.toml", "rb") as project_file:
    dependencies = tomllib.load(project_file)["project"]["dependencies"]
subprocess.check_call(["pip", "install", "--no-cache-dir", *dependencies])
PY

COPY app ./app
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 app && chown -R app:app /srv
USER app

EXPOSE 8000

# Overridden by the worker service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
