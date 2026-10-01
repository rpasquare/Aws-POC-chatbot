"""Delete a document and all its derived data, so it can be re-uploaded and
re-tested from scratch.

Deletes the `documents` row (cascading to `document_pages` and
`ingestion_jobs` via foreign keys) and removes every S3 object under that
document's prefix.

Usage:
    .venv/bin/python scripts/reset_document.py <document_id>
    .venv/bin/python scripts/reset_document.py --filename AAPL_test_report.pdf
    .venv/bin/python scripts/reset_document.py --all   # wipe every document
"""

from __future__ import annotations

import sys

from app import db, s3


def _delete_one(conn, document_id: str, project_id: str) -> None:
    prefix = s3.document_prefix(project_id, document_id)
    removed = s3.delete_prefix(prefix)
    conn.execute("delete from documents where id = %s", (document_id,))
    print(f"deleted document {document_id} (removed {removed} S3 objects under {prefix})")


def main(argv: list[str]) -> int:
    db.open_pool()
    try:
        with db.connection() as conn:
            if len(argv) == 2 and argv[1] == "--all":
                rows = conn.execute("select id, project_id from documents").fetchall()
                for row in rows:
                    _delete_one(conn, str(row["id"]), str(row["project_id"]))
                print(f"total deleted: {len(rows)}")
                return 0

            if len(argv) == 3 and argv[1] == "--filename":
                rows = conn.execute(
                    "select id, project_id from documents where filename = %s", (argv[2],)
                ).fetchall()
                if not rows:
                    print(f"no document found with filename {argv[2]!r}")
                    return 1
                for row in rows:
                    _delete_one(conn, str(row["id"]), str(row["project_id"]))
                return 0

            if len(argv) == 2:
                document_id = argv[1]
                row = conn.execute(
                    "select id, project_id from documents where id = %s", (document_id,)
                ).fetchone()
                if row is None:
                    print(f"no document found with id {document_id!r}")
                    return 1
                _delete_one(conn, str(row["id"]), str(row["project_id"]))
                return 0

            print(__doc__)
            return 2
    finally:
        db.close_pool()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
