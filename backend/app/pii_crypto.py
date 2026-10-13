"""Envelope encryption for `chunks.original_text_enc` (Requirement 16.12).

Why envelope encryption rather than a single fixed application key: KMS's
`encrypt`/`decrypt` operations are sized for small payloads (a handful of
KB), and calling KMS over the network for every chunk's full text would be
slow at any real volume. The standard pattern instead asks KMS for a fresh,
random *data key* per encryption, uses that data key locally (AES-256-GCM)
to encrypt the actual text, then discards the plaintext data key
immediately. Only the data key's *KMS-encrypted* form is kept, bundled
alongside the ciphertext. Decryption reverses this: ask KMS to decrypt the
small wrapped data key (one network call), then decrypt the text locally
with the result.

KMS itself never sees chunk text, and the one thing it persists forever is
the account's master key (`pii_kms_key_id` in `app/config.py`) -- never a
data key, never ciphertext. Compromising the database alone is not enough
to read `original_text_enc`; the attacker would also need live KMS access
under this application's IAM identity.

Stored blob format (what `encrypt_original_text` returns and
`decrypt_original_text` consumes), a single `bytes` value:

    [4 bytes: length of wrapped data key, big-endian]
    [wrapped data key -- KMS's CiphertextBlob]
    [12 bytes: AES-GCM nonce]
    [remaining bytes: AES-GCM ciphertext, tag appended (cryptography's
     AESGCM.encrypt convention)]

This is one `bytea` value with everything needed to decrypt it later,
rather than three separate columns, since nothing else ever needs to query
on the wrapped key or nonce independently.
"""

from __future__ import annotations

import struct
from functools import lru_cache

import boto3
from botocore.client import BaseClient
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import get_settings

_NONCE_LENGTH_BYTES = 12
_WRAPPED_KEY_LENGTH_HEADER = struct.Struct(">I")  # 4-byte big-endian length


@lru_cache(maxsize=1)
def client() -> BaseClient:
    settings = get_settings()
    return boto3.client("kms", region_name=settings.aws_region)


def encrypt_original_text(plaintext: str) -> bytes:
    """Encrypt `plaintext` (a chunk's pre-mask original text) for storage
    in `chunks.original_text_enc`.

    Generates a fresh AES-256 data key via KMS `generate_data_key`, uses it
    locally for AES-256-GCM encryption, and returns one `bytes` blob
    bundling the KMS-wrapped data key, the nonce, and the ciphertext (see
    module docstring for the exact layout). The plaintext data key is held
    only for the duration of this call.
    """
    settings = get_settings()
    response = client().generate_data_key(KeyId=settings.pii_kms_key_id, KeySpec="AES_256")
    plaintext_key = response["Plaintext"]
    wrapped_key = response["CiphertextBlob"]

    nonce = _random_nonce()
    ciphertext = AESGCM(plaintext_key).encrypt(nonce, plaintext.encode("utf-8"), None)

    return _WRAPPED_KEY_LENGTH_HEADER.pack(len(wrapped_key)) + wrapped_key + nonce + ciphertext


def decrypt_original_text(blob: bytes) -> str:
    """Reverse of `encrypt_original_text`: unwrap the data key via KMS
    `decrypt`, then decrypt the ciphertext locally, returning the original
    plaintext.
    """
    (wrapped_key_length,) = _WRAPPED_KEY_LENGTH_HEADER.unpack_from(blob, 0)
    offset = _WRAPPED_KEY_LENGTH_HEADER.size
    wrapped_key = blob[offset : offset + wrapped_key_length]
    offset += wrapped_key_length
    nonce = blob[offset : offset + _NONCE_LENGTH_BYTES]
    offset += _NONCE_LENGTH_BYTES
    ciphertext = blob[offset:]

    response = client().decrypt(CiphertextBlob=wrapped_key)
    plaintext_key = response["Plaintext"]

    plaintext_bytes = AESGCM(plaintext_key).decrypt(nonce, ciphertext, None)
    return plaintext_bytes.decode("utf-8")


def _random_nonce() -> bytes:
    import os

    return os.urandom(_NONCE_LENGTH_BYTES)


__all__ = ["client", "decrypt_original_text", "encrypt_original_text"]
