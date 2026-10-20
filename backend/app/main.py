"""Web entry point.

Handles sessions, permissions, upload authorisation, search, answer
generation and citation lookup. Never opens a PDF and never calls OCR.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from botocore.exceptions import ClientError
from fastapi import Cookie, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import db, s3
from app.answer_model import StreamingAnswerError, stream_answer
from app.answering import REFUSAL_TEXT, record_refusal_message, should_refuse
from app.auth import InvalidCredentialsError, TooManyAttemptsError, sign_in, sign_out
from app.authz import NotAMemberError, require_role, role_on_project
from app.citation_resolution import MessageNotFoundError as CitationMessageNotFoundError
from app.citation_resolution import resolve_citation
from app.citations import record_citations
from app.config import get_settings
from app.conversations import (
    Conversation,
    ConversationNotFoundError,
    MessageNotFoundError,
    create_conversation,
    get_message_model_log,
    list_conversations,
    list_messages,
)
from app.csrf import CSRFMiddleware, generate_csrf_token, set_csrf_cookie
from app.invite_acceptance import InviteNotValidError, PasswordRequiredError, accept_invite
from app.invites import InviteRejectedError, cancel_invite, list_pending_invites, send_invite
from app.prompting import assemble_prompt, substitute_originals
from app.retrieval import retrieve
from app.session_auth import SESSION_COOKIE_NAME, ResolvedCallerId
from app.uploads import (
    UploadRejectedError,
    authorise_upload,
    complete_upload,
    get_document_status,
    list_documents,
)

log = logging.getLogger("app.api")
NOT_FOUND = "not found"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()  # fails fast when configuration is incomplete
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.open_pool()
    log.info("api started region=%s bucket=%s", settings.aws_region, settings.s3_bucket)
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(title="Deal Workspace", version="0.1.0", lifespan=lifespan)

# Requirement 15.6: double-submit CSRF check for state-changing requests
# that rely on the session cookie. Added before CORS so CORS ends up
# outermost (Starlette runs middleware in reverse of add order) and its
# headers are still applied to the CSRF middleware's own 403 responses.
app.add_middleware(CSRFMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5174"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    """Readiness: dependencies this process cannot serve without."""
    database = db.healthy()
    status = "ok" if database else "degraded"
    return JSONResponse(
        status_code=200 if database else 503,
        content={"status": status, "database": database},
    )


class UploadRequest(BaseModel):
    filename: str
    content_type: str
    byte_size: int


class UploadResponse(BaseModel):
    document_id: str
    upload_url: str


@app.post("/api/projects/{project_id}/documents")
def create_upload(
    project_id: str,
    body: UploadRequest,
    caller_id: str = ResolvedCallerId,
) -> UploadResponse:
    """Authorise an upload slot for one document.

    Requirement 7.1/7.2/7.3/7.4 (project-invites spec): the caller is
    resolved via a real session when one is present, so an invited editor
    or viewer is genuinely denied here rather than silently acting as the
    `X-User-Id` placeholder's admin identity. Absent a session, behaviour
    is unchanged (`X-User-Id` still stands in for a resolved session, per
    the original Sprint 4 note). Every other check here — role, content
    type, size — is real either way.
    """
    with db.connection() as conn:
        try:
            slot = authorise_upload(
                conn,
                user_id=caller_id,
                project_id=project_id,
                filename=body.filename,
                content_type=body.content_type,
                byte_size=body.byte_size,
            )
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
        except UploadRejectedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return UploadResponse(document_id=slot.document_id, upload_url=slot.upload_url)


class CompletionResponse(BaseModel):
    document_id: str
    duplicate: bool


@app.post("/api/documents/{document_id}/complete")
def complete_document_upload(document_id: str) -> CompletionResponse:
    """Finalise an upload: hash, dedupe, and atomically enqueue parsing.

    Requirements 5.6, 5.8.
    """
    with db.connection() as conn:
        try:
            result = complete_upload(conn, document_id=document_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except UploadRejectedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return CompletionResponse(document_id=result.document_id, duplicate=result.duplicate)


class DocumentSummary(BaseModel):
    id: str
    filename: str
    status: str
    page_count: int | None
    failure_reason: str | None
    created_at: str
    ready_at: str | None

    @classmethod
    def from_row(cls, row: dict) -> "DocumentSummary":
        return cls(
            id=str(row["id"]),
            filename=row["filename"],
            status=row["status"],
            page_count=row["page_count"],
            failure_reason=row["failure_reason"],
            created_at=row["created_at"].isoformat(),
            ready_at=row["ready_at"].isoformat() if row["ready_at"] else None,
        )


@app.get("/api/projects/{project_id}/documents")
def get_project_documents(
    project_id: str,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> list[DocumentSummary]:
    """List documents visible to the caller's project. Requirement 5.7."""
    with db.connection() as conn:
        try:
            require_role(conn, x_user_id, project_id, "admin", "editor", "viewer")
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
        rows = list_documents(conn, project_id=project_id)
    return [DocumentSummary.from_row(row) for row in rows]


@app.get("/api/documents/{document_id}")
def get_document(document_id: str) -> DocumentSummary:
    """Status, page count and failure reason for polling. Requirement 6.2."""
    with db.connection() as conn:
        row = get_document_status(conn, document_id=document_id)
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return DocumentSummary.from_row(row)


class InviteRequest(BaseModel):
    email: str
    role: str


class InviteResponse(BaseModel):
    invite_id: str


@app.post("/api/projects/{project_id}/invites")
def create_invite(
    project_id: str,
    body: InviteRequest,
    caller_id: str = ResolvedCallerId,
) -> InviteResponse:
    """Send a project invite. Admin-only. Requirements 1.1, 1.2, 1.4, 1.5
    (project-invites spec). The email itself is sent by the caller of
    `send_invite` separately (task 5.1 wires this once app.email exists);
    this handler only creates the invite record and returns its id.

    Requirement 7.1: resolved via a real session when one is present, so
    the invite's `invited_by` reflects whoever actually sent it rather
    than always the `X-User-Id` placeholder's account.
    """
    with db.connection() as conn:
        try:
            slot = send_invite(
                conn,
                user_id=caller_id,
                project_id=project_id,
                email=body.email,
                role=body.role,
            )
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
        except InviteRejectedError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return InviteResponse(invite_id=slot.invite_id)


class PendingInvite(BaseModel):
    id: str
    email: str
    role: str
    invited_by_email: str
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> "PendingInvite":
        return cls(
            id=str(row["id"]),
            email=row["email"],
            role=row["role"],
            invited_by_email=row["invited_by_email"],
            created_at=row["created_at"].isoformat(),
        )


@app.get("/api/projects/{project_id}/invites")
def get_pending_invites(
    project_id: str,
    caller_id: str = ResolvedCallerId,
) -> list[PendingInvite]:
    """List pending invites for a project. Admin-only. Requirement 4.2.

    Requirement 7.1: resolved via a real session when one is present.
    """
    with db.connection() as conn:
        try:
            rows = list_pending_invites(conn, user_id=caller_id, project_id=project_id)
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    return [PendingInvite.from_row(row) for row in rows]


@app.post("/api/projects/{project_id}/invites/{invite_id}/cancel")
def cancel_project_invite(
    project_id: str,
    invite_id: str,
    caller_id: str = ResolvedCallerId,
) -> dict[str, str]:
    """Cancel a pending invite. Admin-only. Requirement 4.3.

    Requirement 7.1: resolved via a real session when one is present.
    """
    with db.connection() as conn:
        try:
            cancel_invite(conn, user_id=caller_id, project_id=project_id, invite_id=invite_id)
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    return {"status": "cancelled"}


class AcceptInviteRequest(BaseModel):
    token: str
    password: str | None = None


class AcceptInviteResponse(BaseModel):
    project_id: str
    first_time: bool


@app.post("/api/invites/accept")
def accept_project_invite(body: AcceptInviteRequest, response: Response) -> AcceptInviteResponse:
    """Accept an invite by token. No prior authentication required — the
    token itself is the credential. Requirements 2.1-2.4, 3.1, 3.3, 4.4.
    """
    with db.connection() as conn:
        try:
            result = accept_invite(conn, raw_token=body.token, password=body.password)
        except InviteNotValidError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        except PasswordRequiredError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=result.session_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_expiry_seconds,
    )
    set_csrf_cookie(response, generate_csrf_token())
    return AcceptInviteResponse(project_id=result.project_id, first_time=result.first_time)


class SignInRequest(BaseModel):
    email: str
    password: str


class SignInResponse(BaseModel):
    user_id: str


@app.post("/api/auth/session")
def sign_in_endpoint(body: SignInRequest, response: Response) -> SignInResponse:
    """Sign in with email and password. Requirements 1.1, 1.2, 1.3, 1.6.

    Requirement 1.3/15.5: unknown email and wrong password are
    indistinguishable to the caller -- same status, same message, and
    `sign_in` takes comparable time either way.
    Requirement 1.4/15.4: an email with too many recent failed attempts
    is rejected with 429 instead, independent of whether that email has
    an account at all.
    """
    with db.connection() as conn:
        try:
            result = sign_in(conn, email=body.email, password=body.password)
        except TooManyAttemptsError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except InvalidCredentialsError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    settings = get_settings()
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=result.session_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        max_age=settings.session_expiry_seconds,
    )
    set_csrf_cookie(response, generate_csrf_token())
    return SignInResponse(user_id=result.user_id)


@app.delete("/api/auth/session")
def sign_out_endpoint(
    response: Response,
    session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> dict[str, str]:
    """Sign out: delete the session row server-side, then clear the
    cookie. Requirement 1.5 -- a signed-out session cannot be replayed by
    re-sending the old cookie value, because the row backing it is gone,
    not just hidden from the browser.
    """
    if session_token is not None:
        with db.connection() as conn:
            sign_out(conn, raw_token=session_token)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    response.delete_cookie(key=get_settings().csrf_cookie_name)
    return {"status": "signed_out"}


class CallerRole(BaseModel):
    role: str | None
    email: str | None


@app.get("/api/projects/{project_id}/me")
def get_my_role(project_id: str, caller_id: str = ResolvedCallerId) -> CallerRole:
    """The resolved caller's role on a project, and the caller's own email,
    for the UI header/role chip. Requirement 7.5 (project-invites spec).
    Resolved the same way as the upload endpoint: a real session takes
    precedence over the `X-User-Id` placeholder when present, so the
    email shown is whoever actually signed in, not the hardcoded
    placeholder identity.
    """
    with db.connection() as conn:
        role = role_on_project(conn, caller_id, project_id)
        user = conn.execute("select email from users where id = %s", (caller_id,)).fetchone()
    return CallerRole(role=role, email=user["email"] if user else None)


@app.get("/api/documents/{document_id}/extracted-text")
def get_extracted_text(document_id: str) -> dict:
    """Return the parsed blocks for display. Demo/debug endpoint: shows what
    Docling (or Textract, for scanned pages) actually extracted.
    """
    with db.connection() as conn:
        row = conn.execute(
            "select project_id, status from documents where id = %s", (document_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    if row["status"] in ("awaiting_upload", "pending_parse", "parsing"):
        raise HTTPException(status_code=409, detail="parsing not complete yet")

    key = s3.parsed_key(str(row["project_id"]), document_id)
    try:
        payload = s3.get_bytes(key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("NoSuchKey", "404"):
            # The object genuinely isn't there -- a real, distinct 404.
            raise HTTPException(status_code=404, detail="parsed output not found") from exc
        # Any other S3 error (expired/invalid credentials, access denied,
        # wrong bucket, etc.) is not "not found" and should not look like
        # it -- a 503 makes clear this is a server-side problem to fix,
        # not evidence the document is missing.
        log.exception("S3 error reading parsed output key=%s", key)
        raise HTTPException(status_code=503, detail="could not read parsed output; try again shortly") from exc
    return json.loads(payload)


class ConversationResponse(BaseModel):
    id: str
    project_id: str
    title: str
    created_by: str
    created_at: str
    last_active: str

    @classmethod
    def from_conversation(cls, conversation: Conversation) -> "ConversationResponse":
        return cls(
            id=conversation.id,
            project_id=conversation.project_id,
            title=conversation.title,
            created_by=conversation.created_by,
            created_at=conversation.created_at.isoformat(),
            last_active=conversation.last_active.isoformat(),
        )


class CreateConversationRequest(BaseModel):
    title: str | None = None


@app.post("/api/projects/{project_id}/conversations")
def create_project_conversation(
    project_id: str,
    body: CreateConversationRequest,
    caller_id: str = ResolvedCallerId,
) -> ConversationResponse:
    """Start a new conversation. Admin or editor only; a viewer is a
    member but lacks this specific capability, so denial is 403, not 404
    (design doc error table: "Viewer attempts to start a conversation" ->
    403). A caller who holds no role at all on the project gets 404
    instead, per Requirement 2.2. Requirements 9.1, 9.2.
    """
    with db.connection() as conn:
        try:
            conversation = create_conversation(
                conn, user_id=caller_id, project_id=project_id, title=body.title
            )
        except NotAMemberError as exc:
            role = role_on_project(conn, caller_id, project_id)
            if role is None:
                raise HTTPException(status_code=404, detail=NOT_FOUND) from exc
            raise HTTPException(status_code=403, detail="viewers cannot start a conversation") from exc
    return ConversationResponse.from_conversation(conversation)


@app.get("/api/projects/{project_id}/conversations")
def get_project_conversations(
    project_id: str,
    caller_id: str = ResolvedCallerId,
) -> list[ConversationResponse]:
    """List a project's conversations, most recently active first. Any
    member (admin, editor or viewer) may read. Requirements 9.3, 9.4.
    """
    with db.connection() as conn:
        try:
            conversations = list_conversations(conn, user_id=caller_id, project_id=project_id)
        except NotAMemberError:
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    return [ConversationResponse.from_conversation(c) for c in conversations]


class CitationResponse(BaseModel):
    marker: int
    chunk_id: str | None
    document_id: str | None
    available: bool
    filename: str | None
    page_from: int | None


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    text: str
    author_id: str | None
    grounded: bool
    created_at: str
    citations: list[CitationResponse]
    has_model_log: bool


def _message_response(message) -> MessageResponse:
    return MessageResponse(
        id=message.id,
        session_id=message.session_id,
        role=message.role,
        text=message.text,
        author_id=message.author_id,
        grounded=message.grounded,
        created_at=message.created_at.isoformat(),
        citations=[CitationResponse(**c) for c in message.citations],
        has_model_log=message.has_model_log,
    )


@app.get("/api/conversations/{conversation_id}/messages")
def get_conversation_messages(
    conversation_id: str,
    caller_id: str = ResolvedCallerId,
) -> list[MessageResponse]:
    """List a conversation's messages in order, with citations attached.
    Any member of the conversation's project may read. Requirement 9.5.
    """
    with db.connection() as conn:
        try:
            messages = list_messages(conn, user_id=caller_id, conversation_id=conversation_id)
        except (ConversationNotFoundError, NotAMemberError):
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    return [_message_response(m) for m in messages]


class ModelLogResponse(BaseModel):
    message_id: str
    request_text: str
    response_text: str


@app.get("/api/messages/{message_id}/model-log")
def get_model_log(
    message_id: str,
    caller_id: str = ResolvedCallerId,
) -> ModelLogResponse:
    """Return the exact masked prompt sent to, and raw response received
    from, the answer model for one grounded message (Requirement 16.18).

    Any member (admin, editor or viewer) of the message's project may
    read -- same gating as reading the conversation itself. 404 for a
    non-member, a non-existent message, or a message with no log row
    (refusals never have one, Requirement 16.20).
    """
    with db.connection() as conn:
        try:
            log = get_message_model_log(conn, user_id=caller_id, message_id=message_id)
        except (MessageNotFoundError, NotAMemberError):
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    return ModelLogResponse(message_id=log.message_id, request_text=log.request_text, response_text=log.response_text)


class RegionPercentResponse(BaseModel):
    x0_pct: float
    y0_pct: float
    x1_pct: float
    y1_pct: float


class CitationResolutionResponse(BaseModel):
    available: bool
    document_id: str | None = None
    filename: str | None = None
    page_no: int | None = None
    page_width: float | None = None
    page_height: float | None = None
    image_url: str | None = None
    regions: list[RegionPercentResponse] = []


@app.get("/api/messages/{message_id}/citations/{marker}/resolve")
def get_citation_resolution(
    message_id: str,
    marker: int,
    caller_id: str = ResolvedCallerId,
) -> CitationResolutionResponse:
    """Resolve one citation marker to its page image and highlight regions,
    for the page viewer opened from a citation chip (Requirements 8.4,
    8.5, 8.6, 8.9, 12.2).

    Any member (admin, editor or viewer) of the message's project may
    open a cited page image (Requirement 11.1, resolved). 404 for a
    non-member or a non-existent message. A citation whose chunk or
    document was purged resolves with `available=False` rather than an
    error (Requirement 8.9) -- not a 404, since the citation and the
    message both still exist.
    """
    with db.connection() as conn:
        try:
            resolved = resolve_citation(conn, user_id=caller_id, message_id=message_id, marker=marker)
        except (CitationMessageNotFoundError, NotAMemberError):
            raise HTTPException(status_code=404, detail=NOT_FOUND) from None
    return CitationResolutionResponse(
        available=resolved.available,
        document_id=resolved.document_id,
        filename=resolved.filename,
        page_no=resolved.page_no,
        page_width=resolved.page_width,
        page_height=resolved.page_height,
        image_url=resolved.image_url,
        regions=[
            RegionPercentResponse(x0_pct=r.x0_pct, y0_pct=r.y0_pct, x1_pct=r.x1_pct, y1_pct=r.y1_pct)
            for r in resolved.regions
        ],
    )


class AskQuestionRequest(BaseModel):
    question: str


def _require_project_id_for_conversation(conn, conversation_id: str) -> str:
    row = conn.execute(
        "select project_id from chat_sessions where id = %s", (conversation_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    return str(row["project_id"])


@app.post("/api/conversations/{conversation_id}/messages")
def ask_question(
    conversation_id: str,
    body: AskQuestionRequest,
    caller_id: str = ResolvedCallerId,
) -> Response:
    """Ask a question in a conversation. Admin or editor only, same
    gating as starting a conversation (Requirement 9.2's capability
    matrix applies to asking questions too).

    Records the question, retrieves candidate passages, and either
    returns the fixed refusal response (Requirement 7.3/7.4, not
    streamed, since there is nothing progressive about a fixed string)
    or streams the grounded answer as it is generated (Requirement 7.5),
    recording the assistant message and its citations once the stream
    completes.
    """
    with db.connection() as conn:
        project_id = _require_project_id_for_conversation(conn, conversation_id)
        try:
            require_role(conn, caller_id, project_id, "admin", "editor")
        except NotAMemberError as exc:
            role = role_on_project(conn, caller_id, project_id)
            if role is None:
                raise HTTPException(status_code=404, detail=NOT_FOUND) from exc
            raise HTTPException(status_code=403, detail="viewers cannot ask questions") from exc

        conn.execute(
            "insert into chat_messages (session_id, role, text, author_id, grounded) "
            "values (%s, 'user', %s, %s, true)",
            (conversation_id, body.question, caller_id),
        )
        conn.execute(
            "update chat_sessions set last_active = now() where id = %s",
            (conversation_id,),
        )

        chunks = retrieve(conn, project_id=project_id, query_text=body.question)

        if should_refuse(chunks):
            message_id = record_refusal_message(conn, session_id=conversation_id)
            return JSONResponse(
                content={
                    "message_id": message_id,
                    "role": "assistant",
                    "text": REFUSAL_TEXT,
                    "grounded": False,
                    "citations": [],
                }
            )

        assembly = assemble_prompt(chunks, body.question)

    # Streaming happens outside the request's connection/transaction: the
    # Bedrock call can take several seconds, and holding a pooled Postgres
    # connection open for the duration of a slow external call would starve
    # the pool. A fresh connection is opened after the stream completes to
    # record the assistant message and its citations.
    def collect_and_record(fragments: Iterator[str]) -> Iterator[str]:
        full_text_parts: list[str] = []
        for fragment in fragments:
            full_text_parts.append(fragment)
            yield fragment

        # `raw_response_text` is exactly what Claude returned, still
        # carrying masked placeholders -- this, not the substituted
        # version, is what Requirement 16.16/16.21 requires the audit log
        # to persist. Citation marker extraction (below) also runs on
        # this raw text; substitution only ever replaces placeholder
        # tokens, never `[n]` markers, so the ordering is not load-bearing
        # for citations, but doing substitution last keeps the raw log
        # unambiguous.
        raw_response_text = "".join(full_text_parts)
        substituted_text = substitute_originals(raw_response_text, assembly.placeholder_mapping)

        with db.connection() as write_conn:
            row = write_conn.execute(
                "insert into chat_messages (session_id, role, text, author_id, grounded) "
                "values (%s, 'assistant', %s, null, true) "
                "returning id",
                (conversation_id, substituted_text),
            ).fetchone()
            message_id = str(row["id"])
            record_citations(
                write_conn,
                message_id=message_id,
                answer_text=raw_response_text,
                slot_map=assembly.slot_map,
                chunks=chunks,
            )
            write_conn.execute(
                "insert into message_model_logs (message_id, request_text, response_text) "
                "values (%s, %s, %s)",
                (message_id, assembly.prompt, raw_response_text),
            )

    # `stream_answer` is a generator: nothing runs until the first
    # `next()`. Pulling the first fragment here, before constructing the
    # `StreamingResponse`, means a `StreamingAnswerError` raised before
    # any text was produced still surfaces as a clean 503 (Requirement
    # 7's error table) rather than a broken 200 stream. A failure after
    # the first fragment has already been sent cannot change the status
    # code any more -- that is an inherent limit of HTTP streaming, not
    # something this handler can work around -- so it is left to
    # propagate and end the response early.
    fragments = stream_answer(assembly.prompt)
    try:
        first_fragment = next(fragments)
    except StopIteration:
        first_fragment = None
    except StreamingAnswerError as exc:
        raise HTTPException(
            status_code=503, detail="the answer model is temporarily unavailable; try again shortly"
        ) from exc

    def _prepend(first: str, rest: Iterator[str]) -> Iterator[str]:
        yield first
        yield from rest

    def generate() -> Iterator[str]:
        if first_fragment is not None:
            yield from collect_and_record(_prepend(first_fragment, fragments))
        else:
            yield from collect_and_record(fragments)

    return StreamingResponse(generate(), media_type="text/plain")
