"""Migration runner.

Plain SQL files applied in filename order, each in its own transaction, with
the applied set recorded in `schema_migrations`. A session advisory lock means
two processes starting at once cannot apply the same file twice.

    python -m app.migrate up      apply every pending migration
    python -m app.migrate status  show applied and pending
    python -m app.migrate seed    load development fixtures
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import psycopg

from app.config import get_settings

log = logging.getLogger("app.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SEED_FILE = MIGRATIONS_DIR / "dev_seed.sql"
LOCK_KEY = 8_241_773_115_002_001

_BOOTSTRAP = """
create table if not exists schema_migrations (
  version     text primary key,
  checksum    text not null,
  applied_at  timestamptz not null default now()
)
"""


def _files() -> list[Path]:
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.name != SEED_FILE.name)


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _connect() -> psycopg.Connection:
    return psycopg.connect(get_settings().database_url, autocommit=True)


def _applied(conn: psycopg.Connection) -> dict[str, str]:
    conn.execute(_BOOTSTRAP)
    rows = conn.execute("select version, checksum from schema_migrations").fetchall()
    return {version: checksum for version, checksum in rows}


def up() -> int:
    applied_count = 0
    with _connect() as conn:
        conn.execute("select pg_advisory_lock(%s)", (LOCK_KEY,))
        try:
            applied = _applied(conn)
            for path in _files():
                version = path.stem
                checksum = _checksum(path)
                if version in applied:
                    if applied[version] != checksum:
                        raise RuntimeError(
                            f"migration {version} changed after being applied "
                            f"({applied[version]} -> {checksum}); add a new migration instead"
                        )
                    continue
                log.info("applying %s", version)
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute(
                        "insert into schema_migrations (version, checksum) values (%s, %s)",
                        (version, checksum),
                    )
                applied_count += 1
        finally:
            conn.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
    log.info("migrations applied=%d", applied_count)
    return applied_count


def status() -> None:
    with _connect() as conn:
        applied = _applied(conn)
    for path in _files():
        state = "applied" if path.stem in applied else "pending"
        print(f"{state:>7}  {path.stem}")


def seed() -> None:
    """Development fixtures. Idempotent, and never run against production."""
    if not SEED_FILE.exists():
        raise FileNotFoundError(SEED_FILE)
    with _connect() as conn, conn.transaction():
        conn.execute(SEED_FILE.read_text())
    log.info("development seed loaded")


def main(argv: list[str]) -> int:
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(name)s %(message)s")
    command = argv[1] if len(argv) > 1 else "up"
    if command == "up":
        up()
    elif command == "status":
        status()
    elif command == "seed":
        up()
        seed()
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
