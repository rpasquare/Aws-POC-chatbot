FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Dependency layer first so application edits do not invalidate it.
COPY pyproject.toml ./
RUN pip install --no-cache-dir "fastapi==0.115.6" "uvicorn[standard]==0.34.0" \
    "pydantic==2.10.4" "pydantic-settings==2.7.0" \
    "psycopg[binary,pool]==3.2.3" "boto3==1.35.87"

COPY app ./app
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 app && chown -R app:app /srv
USER app

EXPOSE 8000

# Overridden by the worker service.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
