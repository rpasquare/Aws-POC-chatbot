-- Recoverable, encrypted copy of a chunk's pre-mask original text.
-- Needed so an answer can reverse-substitute a masked placeholder back to
-- the real value the user asked about (Requirement 16.12-16.15), without
-- storing the real PII in plaintext anywhere.
--
-- Null for chunks with no detected PII (pii_masked = false) -- there is
-- nothing to recover, so nothing is encrypted or stored for them.
--
-- Encrypted at the application layer (envelope encryption via AWS KMS,
-- see app/pii_crypto.py) before this column is ever written -- Postgres
-- never sees plaintext PII through this column.

alter table chunks add column original_text_enc bytea;
