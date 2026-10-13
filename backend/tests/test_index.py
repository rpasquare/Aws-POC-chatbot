"""Tests for the index stage job handler.

Needs a real Postgres with pgvector (the `chunks` table has a `vector(512)`
column and a generated `tsvector` column that cannot be exercised against a
mock). No real-DB fixture exists yet elsewhere in this test suite, so this
module creates its own minimal one: skip the whole module if
`DATABASE_URL` is not reachable, otherwise apply migrations once and wrap
each test in a transaction-per-test via `app.db`.

S3 and `app.embeddings.embed_texts` are mocked — no real Bedrock or S3
calls. `app.jobs.Job` is constructed directly rather than claimed from the
queue, since only the handler itself is under test here.

Validates: Requirements 6.3, 6.12, 11.1, 11.3
"""

from __future__ import annotations

import json
import uuid

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

from app import db
from app.config import get_settings
from app.embeddings import EmbeddingResult
from app.guardrails import GuardrailResult
from app.index import run_index_job
from app.jobs import Job

settings = get_settings()


def _db_reachable() -> bool:
    try:
        conn = psycopg.connect(settings.database_url, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _db_reachable(),
    reason="requires a real Postgres with pgvector (DATABASE_URL); not reachable in this environment",
)


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema():
    """Apply migrations once per test run against the configured database."""
    from app import migrate

    migrate.up()


@pytest.fixture
def conn():
    db.open_pool()
    with db.connection() as c:
        yield c
        # Tests commit via the handler's own connection usage; clean up
        # explicitly rather than relying on rollback since `db.connection()`
        # commits on clean exit.
    db.close_pool()


def _make_project_and_document(conn) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    conn.execute(
        "insert into users (id, email, external_id) values (%s, %s, %s)",
        (user_id, f"{user_id}@example.com", user_id),
    )
    project_id = str(uuid.uuid4())
    conn.execute(
        "insert into projects (id, name, created_by) values (%s, 'Test Project', %s)",
        (project_id, user_id),
    )
    document_id = str(uuid.uuid4())
    conn.execute(
        "insert into documents (id, project_id, filename, status, s3_key_original, uploaded_by) "
        "values (%s, %s, 'test.pdf', 'indexing', 'projects/x/documents/x/original.pdf', %s)",
        (document_id, project_id, user_id),
    )
    return project_id, document_id


def _parsed_blocks() -> list[dict]:
    return [
        {
            "page_no": 1,
            "text": "Reported NAV for the interest as at 30 September 2025 is $42.6m.",
            "bbox": [72.0, 340.5, 468.0, 366.2],
            "kind": "paragraph",
            "heading_path": "Capital Account > Net Asset Value",
        },
        {
            "page_no": 1,
            "text": "Called capital stands at $10.0m.",
            "bbox": [72.0, 370.0, 468.0, 390.0],
            "kind": "paragraph",
            "heading_path": "Capital Account > Net Asset Value",
        },
    ]


def _fake_embed_texts(monkeypatch, dimensions: int = 512, embed_model: str = "fake-model-v1"):
    def _fake(texts: list[str]) -> list[EmbeddingResult]:
        return [EmbeddingResult(vector=tuple([0.01 * (i + 1)] * dimensions), embed_model=embed_model) for i in range(len(texts))]

    monkeypatch.setattr("app.index.embed_texts", _fake)
    return embed_model


def _fake_get_bytes(monkeypatch, blocks: list[dict]):
    payload = json.dumps({"pages": [{"page_no": 1, "width": 612.0, "height": 792.0}], "blocks": blocks}).encode("utf-8")
    monkeypatch.setattr("app.index.s3.get_bytes", lambda key: payload)


def _fake_check_guardrail(monkeypatch, *, mask_text: str | None = None):
    """No-op guardrail by default (returns text unchanged, pii_detected
    False). If `mask_text` is given, any chunk text containing it is
    replaced with a `[REDACTED]` marker and reported as masked, so tests
    can exercise Requirement 16.2/16.7 without a real Bedrock call.

    Also fakes `encrypt_original_text` with a reversible, deterministic
    stand-in (prefixing a marker) so tests never call real KMS, and so a
    test can assert on exactly what was "encrypted" without needing a
    real decrypt round trip.
    """
    from app.guardrails import GuardrailResult

    def _fake(text: str) -> GuardrailResult:
        if mask_text is not None and mask_text in text:
            return GuardrailResult(filtered_text=text.replace(mask_text, "[REDACTED]"), pii_detected=True)
        return GuardrailResult(filtered_text=text, pii_detected=False)

    def _fake_encrypt(plaintext: str) -> bytes:
        return b"FAKE_ENC:" + plaintext.encode("utf-8")

    monkeypatch.setattr("app.index.check_guardrail", _fake)
    monkeypatch.setattr("app.index.encrypt_original_text", _fake_encrypt)


# --- unit tests --------------------------------------------------------------


def test_run_index_job_inserts_chunks_regions_and_advances_status(conn, monkeypatch):
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    _fake_check_guardrail(monkeypatch)
    embed_model = _fake_embed_texts(monkeypatch)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    chunks = conn.execute(
        "select id, ordinal, heading_path, embed_model, page_from, page_to "
        "from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()
    assert len(chunks) == 1  # both blocks share a heading path, under target tokens
    assert chunks[0]["embed_model"] == embed_model
    assert chunks[0]["page_from"] == 1
    assert chunks[0]["page_to"] == 1

    regions = conn.execute(
        "select cr.page_no, cr.x0, cr.y0, cr.x1, cr.y1 "
        "from chunk_regions cr join chunks c on c.id = cr.chunk_id "
        "where c.document_id = %s",
        (document_id,),
    ).fetchall()
    assert len(regions) == 2  # one region per contributing block

    status = conn.execute("select status from documents where id = %s", (document_id,)).fetchone()
    assert status["status"] == "ready"


def test_run_index_job_twice_is_idempotent(conn, monkeypatch):
    """P15: running index twice yields the identical chunk set, not doubled."""
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    _fake_check_guardrail(monkeypatch)
    _fake_embed_texts(monkeypatch)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    first_chunks = conn.execute(
        "select ordinal, text, heading_path, page_from, page_to, token_count "
        "from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()
    first_regions_count = conn.execute(
        "select count(*) as n from chunk_regions cr join chunks c on c.id = cr.chunk_id "
        "where c.document_id = %s",
        (document_id,),
    ).fetchone()["n"]

    # Document must be back in 'indexing' for a second index run to be legal
    # per the state machine (ready -> indexing is a declared transition).
    from app.documents import apply_transition

    apply_transition(conn, document_id, "indexing")
    run_index_job(conn, job)

    second_chunks = conn.execute(
        "select ordinal, text, heading_path, page_from, page_to, token_count "
        "from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()
    second_regions_count = conn.execute(
        "select count(*) as n from chunk_regions cr join chunks c on c.id = cr.chunk_id "
        "where c.document_id = %s",
        (document_id,),
    ).fetchone()["n"]

    assert len(second_chunks) == len(first_chunks)
    assert [dict(c) for c in second_chunks] == [dict(c) for c in first_chunks]
    assert second_regions_count == first_regions_count

    status = conn.execute("select status from documents where id = %s", (document_id,)).fetchone()
    assert status["status"] == "ready"


def test_embed_model_recorded_matches_embeddings_output(conn, monkeypatch):
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    _fake_check_guardrail(monkeypatch)
    embed_model = _fake_embed_texts(monkeypatch, embed_model="amazon.titan-embed-text-v2:0")

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    rows = conn.execute("select embed_model from chunks where document_id = %s", (document_id,)).fetchall()
    assert len(rows) >= 1
    assert all(row["embed_model"] == embed_model for row in rows)


# --- PII guardrail integration (Requirement 16.2, 16.7; task 8.8) ----------


def test_guardrail_runs_before_embedding_and_masked_text_is_stored(conn, monkeypatch):
    """The chunk text passed to embed_texts, and the text stored in
    `chunks.text`, both equal check_guardrail's filtered output -- never
    the original -- when the guardrail masks something."""
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    _fake_check_guardrail(monkeypatch, mask_text="$42.6m")

    embedded_texts: list[str] = []

    def _capturing_embed_texts(texts):
        embedded_texts.extend(texts)
        return [EmbeddingResult(vector=tuple([0.01] * 512), embed_model="fake-model-v1") for _ in texts]

    monkeypatch.setattr("app.index.embed_texts", _capturing_embed_texts)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    rows = conn.execute(
        "select text, pii_masked from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()
    assert len(rows) == 1
    assert "$42.6m" not in rows[0]["text"]
    assert "[REDACTED]" in rows[0]["text"]
    assert rows[0]["pii_masked"] is True

    # The exact same filtered text is what reached embed_texts.
    assert embedded_texts == [rows[0]["text"]]


def test_pii_masked_false_when_nothing_detected(conn, monkeypatch):
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    _fake_check_guardrail(monkeypatch)  # no mask_text -> never intervenes
    _fake_embed_texts(monkeypatch)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    rows = conn.execute("select pii_masked from chunks where document_id = %s", (document_id,)).fetchall()
    assert len(rows) >= 1
    assert all(row["pii_masked"] is False for row in rows)


# --- property-based test: P42 (task 8.9) -----------------------------------

_pii_samples = ["555-123-4567", "jane.doe@example.com", "123-45-6789"]

_body_text = st.text(
    alphabet=st.characters(whitelist_categories=["L", "N"], max_codepoint=0x2764),
    min_size=1,
    max_size=30,
).filter(lambda s: s.strip() and not any(sample in s for sample in _pii_samples))


@given(prefix=_body_text, pii_sample=st.sampled_from(_pii_samples), suffix=_body_text)
@hyp_settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_index_time_pii_containment(conn, monkeypatch, prefix, pii_sample, suffix):
    """P42: for any chunk text including synthetic PII, both the text
    passed to embed_texts and the text stored in chunks.text equal
    check_guardrail's filtered output, never the original, when they
    differ.

    **Validates: Requirements 16.2, 16.7**
    """
    project_id, document_id = _make_project_and_document(conn)
    block_text = f"{prefix} {pii_sample} {suffix}"
    _fake_get_bytes(
        monkeypatch,
        [
            {
                "page_no": 1,
                "text": block_text,
                "bbox": [72.0, 340.5, 468.0, 366.2],
                "kind": "paragraph",
                "heading_path": "Section",
            }
        ],
    )

    def _fake_guardrail(text: str) -> GuardrailResult:
        if pii_sample in text:
            return GuardrailResult(filtered_text=text.replace(pii_sample, "[REDACTED]"), pii_detected=True)
        return GuardrailResult(filtered_text=text, pii_detected=False)

    monkeypatch.setattr("app.index.check_guardrail", _fake_guardrail)
    monkeypatch.setattr("app.index.encrypt_original_text", lambda plaintext: b"FAKE_ENC:" + plaintext.encode("utf-8"))

    embedded_texts: list[str] = []

    def _capturing_embed_texts(texts):
        embedded_texts.extend(texts)
        return [EmbeddingResult(vector=tuple([0.01] * 512), embed_model="fake-model-v1") for _ in texts]

    monkeypatch.setattr("app.index.embed_texts", _capturing_embed_texts)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    stored = conn.execute(
        "select text from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()

    assert len(stored) == len(embedded_texts) == 1
    assert pii_sample not in stored[0]["text"]
    assert pii_sample not in embedded_texts[0]
    assert stored[0]["text"] == embedded_texts[0]


# --- property-based test: P44 (task 8.12) ----------------------------------


@given(block_text=_body_text)
@hyp_settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_index_fails_closed_when_guardrail_unreachable(conn, monkeypatch, block_text):
    """P44: if check_guardrail exhausts retries and raises, embed_texts is
    never invoked with that text, and the index job ends in a failure
    state (the exception propagates rather than being swallowed).

    **Validates: Requirements 16.11**
    """
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(
        monkeypatch,
        [
            {
                "page_no": 1,
                "text": block_text or "placeholder text",
                "bbox": [72.0, 340.5, 468.0, 366.2],
                "kind": "paragraph",
                "heading_path": "Section",
            }
        ],
    )

    class _GuardrailUnreachable(RuntimeError):
        pass

    def _always_raise(text: str):
        raise _GuardrailUnreachable("guardrail unreachable after retries")

    monkeypatch.setattr("app.index.check_guardrail", _always_raise)

    embed_texts_called = {"value": False}

    def _embed_texts_should_not_run(texts):
        embed_texts_called["value"] = True
        return []

    monkeypatch.setattr("app.index.embed_texts", _embed_texts_should_not_run)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    with pytest.raises(_GuardrailUnreachable):
        run_index_job(conn, job)

    assert embed_texts_called["value"] is False

    # No chunks were inserted -- the job ended in failure, not a partial
    # or silently-succeeded state.
    rows = conn.execute("select count(*) as n from chunks where document_id = %s", (document_id,)).fetchone()
    assert rows["n"] == 0


# --- original_text_enc wiring (Requirement 16.12; task 8c.5) --------------


def test_original_text_enc_stored_when_pii_masked(conn, monkeypatch):
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    _fake_check_guardrail(monkeypatch, mask_text="$42.6m")
    _fake_embed_texts(monkeypatch)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    rows = conn.execute(
        "select pii_masked, original_text_enc from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["pii_masked"] is True
    assert rows[0]["original_text_enc"] is not None
    # The fake encrypt_original_text prefixes a marker and keeps the
    # plaintext legible for this assertion -- real KMS/AES is exercised
    # separately in test_pii_crypto.py.
    assert bytes(rows[0]["original_text_enc"]).startswith(b"FAKE_ENC:")
    assert b"$42.6m" in bytes(rows[0]["original_text_enc"])


def test_original_text_enc_null_when_no_pii(conn, monkeypatch):
    project_id, document_id = _make_project_and_document(conn)
    _fake_get_bytes(monkeypatch, _parsed_blocks())
    _fake_check_guardrail(monkeypatch)  # no mask_text -> never intervenes
    _fake_embed_texts(monkeypatch)

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    rows = conn.execute(
        "select original_text_enc from chunks where document_id = %s", (document_id,)
    ).fetchall()
    assert len(rows) >= 1
    assert all(row["original_text_enc"] is None for row in rows)


# --- property-based test: index-time original-text recoverability (task 8c.6) ---


@given(prefix=_body_text, pii_sample=st.sampled_from(_pii_samples), suffix=_body_text)
@hyp_settings(
    max_examples=4,
    deadline=None,  # real KMS network calls per example; latency varies, not a regression to catch
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_index_time_original_text_recoverable(conn, monkeypatch, prefix, pii_sample, suffix):
    """For any chunk text including synthetic PII, decrypting the stored
    original_text_enc after an index run returns exactly the pre-mask
    text that was passed to check_guardrail; for chunk text with no PII,
    original_text_enc is null.

    Uses the real pii_crypto encrypt/decrypt (against the real KMS key),
    not a fake, since this specifically proves the stored blob is
    genuinely recoverable end-to-end -- only check_guardrail and
    embed_texts are faked here.

    **Validates: Requirements 16.12**
    """
    from app.guardrails import GuardrailResult
    from app.pii_crypto import decrypt_original_text

    project_id, document_id = _make_project_and_document(conn)
    block_text = f"{prefix} {pii_sample} {suffix}"
    _fake_get_bytes(
        monkeypatch,
        [
            {
                "page_no": 1,
                "text": block_text,
                "bbox": [72.0, 340.5, 468.0, 366.2],
                "kind": "paragraph",
                "heading_path": "Section",
            }
        ],
    )

    def _fake_guardrail(text: str) -> GuardrailResult:
        if pii_sample in text:
            return GuardrailResult(filtered_text=text.replace(pii_sample, "[REDACTED]"), pii_detected=True)
        return GuardrailResult(filtered_text=text, pii_detected=False)

    monkeypatch.setattr("app.index.check_guardrail", _fake_guardrail)
    monkeypatch.setattr(
        "app.index.embed_texts",
        lambda texts: [EmbeddingResult(vector=tuple([0.01] * 512), embed_model="fake-model-v1") for _ in texts],
    )

    job = Job(id=1, document_id=document_id, stage="index", attempts=0, max_attempts=3)
    run_index_job(conn, job)

    rows = conn.execute(
        "select original_text_enc from chunks where document_id = %s order by ordinal",
        (document_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["original_text_enc"] is not None

    # build_chunks prepends the heading path ("Section") to the chunk's
    # assembled text before check_guardrail ever sees it -- the recovered
    # original must match that assembled text, not the raw block text.
    recovered = decrypt_original_text(bytes(rows[0]["original_text_enc"]))
    assert recovered == f"Section\n\n{block_text}"
