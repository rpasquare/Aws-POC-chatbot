"""Tests for the model audit log and the substitution it is paired with.

These exercise the exact write pattern `app/main.py`'s message-streaming
handler performs (insert chat_messages with the substituted text, insert
message_model_logs with the raw masked prompt/response), directly against
a real Postgres, rather than through the full HTTP/streaming stack --
mirroring the project's existing `test_index.py`/`test_retrieval.py`
pattern for handler-level logic that needs a real database.

Validates: Requirements 16.16, 16.17, 16.20, 16.21
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings as hyp_settings
from hypothesis import strategies as st

from app import db
from app.config import get_settings
from app.prompting import PlaceholderMapping, substitute_originals

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
    reason="requires a real Postgres (DATABASE_URL); not reachable in this environment",
)


@pytest.fixture(scope="module", autouse=True)
def _migrated_schema():
    from app import migrate

    migrate.up()


@pytest.fixture
def conn():
    db.open_pool()
    with db.connection() as c:
        yield c
    db.close_pool()


def _make_conversation(conn) -> str:
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
    conversation_id = str(uuid.uuid4())
    conn.execute(
        "insert into chat_sessions (id, project_id, title, created_by) "
        "values (%s, %s, 'Test conversation', %s)",
        (conversation_id, project_id, user_id),
    )
    return conversation_id


def _write_grounded_message(conn, *, conversation_id: str, request_text: str, raw_response_text: str, mapping: PlaceholderMapping) -> str:
    """Mirrors exactly what `app/main.py`'s `collect_and_record` does."""
    substituted_text = substitute_originals(raw_response_text, mapping)
    row = conn.execute(
        "insert into chat_messages (session_id, role, text, author_id, grounded) "
        "values (%s, 'assistant', %s, null, true) "
        "returning id",
        (conversation_id, substituted_text),
    ).fetchone()
    message_id = str(row["id"])
    conn.execute(
        "insert into message_model_logs (message_id, request_text, response_text) "
        "values (%s, %s, %s)",
        (message_id, request_text, raw_response_text),
    )
    return message_id


def _write_refusal_message(conn, *, conversation_id: str, text: str) -> str:
    """Mirrors `app.answering.record_refusal_message` -- no model_logs row."""
    row = conn.execute(
        "insert into chat_messages (session_id, role, text, author_id, grounded) "
        "values (%s, 'assistant', %s, null, false) "
        "returning id",
        (conversation_id, text),
    ).fetchone()
    return str(row["id"])


# --- unit tests --------------------------------------------------------------


def test_model_log_stores_exact_masked_request_and_response(conn):
    conversation_id = _make_conversation(conn)
    mapping = PlaceholderMapping(values_by_placeholder={"{NAME}": ["John Smith"]})

    message_id = _write_grounded_message(
        conn,
        conversation_id=conversation_id,
        request_text="[1] report.pdf, page 1\n    Contact {NAME}.\n\nQuestion: who is the contact?",
        raw_response_text="The contact is {NAME} [1].",
        mapping=mapping,
    )

    log_row = conn.execute(
        "select request_text, response_text from message_model_logs where message_id = %s",
        (message_id,),
    ).fetchone()
    assert log_row["request_text"] == "[1] report.pdf, page 1\n    Contact {NAME}.\n\nQuestion: who is the contact?"
    assert log_row["response_text"] == "The contact is {NAME} [1]."

    # The displayed message text is the substituted version, distinct
    # from the raw log.
    message_row = conn.execute("select text from chat_messages where id = %s", (message_id,)).fetchone()
    assert message_row["text"] == "The contact is John Smith [1]."
    assert message_row["text"] != log_row["response_text"]


def test_refusal_message_has_no_model_log_row(conn):
    conversation_id = _make_conversation(conn)

    message_id = _write_refusal_message(conn, conversation_id=conversation_id, text="documents do not cover this")

    log_row = conn.execute(
        "select 1 from message_model_logs where message_id = %s", (message_id,)
    ).fetchone()
    assert log_row is None


def test_model_log_response_text_never_equals_substituted_text_when_substitution_changed_something(conn):
    conversation_id = _make_conversation(conn)
    mapping = PlaceholderMapping(values_by_placeholder={"{EMAIL}": ["jane@example.com"]})

    message_id = _write_grounded_message(
        conn,
        conversation_id=conversation_id,
        request_text="prompt text",
        raw_response_text="Email is {EMAIL}.",
        mapping=mapping,
    )

    log_row = conn.execute(
        "select response_text from message_model_logs where message_id = %s", (message_id,)
    ).fetchone()
    message_row = conn.execute("select text from chat_messages where id = %s", (message_id,)).fetchone()

    assert log_row["response_text"] == "Email is {EMAIL}."
    assert message_row["text"] == "Email is jane@example.com."
    assert log_row["response_text"] != message_row["text"]


def test_message_id_foreign_key_cascades_on_message_delete(conn):
    conversation_id = _make_conversation(conn)
    mapping = PlaceholderMapping(values_by_placeholder={})

    message_id = _write_grounded_message(
        conn, conversation_id=conversation_id, request_text="p", raw_response_text="r", mapping=mapping
    )
    conn.execute("delete from chat_messages where id = %s", (message_id,))

    log_row = conn.execute(
        "select 1 from message_model_logs where message_id = %s", (message_id,)
    ).fetchone()
    assert log_row is None


# --- property-based test: P48 (task 8d.7) ----------------------------------

_pii_samples = ["jane.doe@example.com", "555-123-4567"]


@given(
    pii_sample=st.sampled_from(_pii_samples),
    prefix=st.text(alphabet=st.characters(whitelist_categories=["L", "N"]), min_size=0, max_size=15),
    suffix=st.text(alphabet=st.characters(whitelist_categories=["L", "N"]), min_size=0, max_size=15),
)
@hyp_settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_model_log_persistence_correctness(conn, pii_sample, prefix, suffix):
    """P48: the persisted request_text/response_text are byte-identical
    to what was sent/received, response_text is never equal to the
    substituted answer text when substitution changed something, and
    neither field contains any raw synthetic PII value (the mapping only
    ever maps a placeholder, never leaks a real value into the masked
    text itself).

    **Validates: Requirements 16.16, 16.17, 16.21**
    """
    conversation_id = _make_conversation(conn)
    placeholder = "{EMAIL}" if "@" in pii_sample else "{PHONE}"
    mapping = PlaceholderMapping(values_by_placeholder={placeholder: [pii_sample]})

    request_text = f"[1] doc.pdf, page 1\n    {prefix} {placeholder} {suffix}\n\nQuestion: q?"
    raw_response_text = f"The value is {placeholder}."

    message_id = _write_grounded_message(
        conn,
        conversation_id=conversation_id,
        request_text=request_text,
        raw_response_text=raw_response_text,
        mapping=mapping,
    )

    log_row = conn.execute(
        "select request_text, response_text from message_model_logs where message_id = %s",
        (message_id,),
    ).fetchone()
    message_row = conn.execute("select text from chat_messages where id = %s", (message_id,)).fetchone()

    # Byte-identical to what was sent/received.
    assert log_row["request_text"] == request_text
    assert log_row["response_text"] == raw_response_text

    # Neither logged field contains the raw PII value -- it was never in
    # the masked text to begin with.
    assert pii_sample not in log_row["request_text"]
    assert pii_sample not in log_row["response_text"]

    # response_text differs from the substituted message text, since
    # substitution did change something here.
    assert log_row["response_text"] != message_row["text"]
    assert pii_sample in message_row["text"]


# --- property-based test: P49 (task 8d.8) ----------------------------------


@given(
    refusal_text=st.text(
        alphabet=st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00"),
        min_size=1,
        max_size=50,
    )
)
@hyp_settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_refusal_messages_never_have_model_log(conn, refusal_text):
    """P49: for any question taking the refusal path, no
    message_model_logs row exists for the resulting message's id.

    **Validates: Requirements 16.20**
    """
    conversation_id = _make_conversation(conn)

    message_id = _write_refusal_message(conn, conversation_id=conversation_id, text=refusal_text)

    log_row = conn.execute(
        "select 1 from message_model_logs where message_id = %s", (message_id,)
    ).fetchone()
    assert log_row is None
