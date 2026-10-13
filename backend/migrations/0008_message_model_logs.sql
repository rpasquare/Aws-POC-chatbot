-- Per-message audit log of exactly what was sent to, and received from,
-- the answer model (Requirement 16.16-16.21). One row per grounded
-- assistant message; refusal messages never get a row here, since the
-- answer model was never called for them (Requirement 16.20).
--
-- request_text/response_text are deliberately the masked forms that
-- actually crossed the boundary to/from the model -- never substituted
-- with real PII values (Requirement 16.21).

create table message_model_logs (
  message_id    uuid primary key references chat_messages(id) on delete cascade,
  request_text  text not null,
  response_text text not null,
  created_at    timestamptz not null default now()
);
