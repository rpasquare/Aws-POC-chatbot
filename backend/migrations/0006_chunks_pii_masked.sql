-- Tracks whether check_guardrail actually redacted a chunk's text at index
-- time. Lets an operator find how much of a project's content contains PII
-- without re-scanning every chunk, and is what the index-stage correctness
-- property (P42) checks against.
-- Requirements: 16.2, 16.7

alter table chunks add column pii_masked boolean not null default false;
