"""Database access. One connection pool per process, raw SQL throughout.

The pool is opened explicitly by whichever entry point owns the process, so
that a missing or unreachable database is discovered at startup.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

_pool: ConnectionPool | None = None


def open_pool() -> ConnectionPool:
    """Create the pool and wait until a connection can actually be made."""
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        _pool.open(wait=True, timeout=30)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool is not open; call open_pool() first")
    return _pool


@contextmanager
def connection() -> Iterator[Connection]:
    """A pooled connection wrapped in a transaction.

    Committed on clean exit, rolled back on exception.
    """
    with pool().connection() as conn:
        yield conn


def healthy() -> bool:
    """Cheap reachability probe used by readiness checks."""
    try:
        with connection() as conn:
            conn.execute("select 1")
        return True
    except Exception:
        return False
