"""Tests for envelope encryption of chunk original text.

Validates: Requirements 16.12
"""

from __future__ import annotations

import os

from app import pii_crypto
from app.config import get_settings

settings = get_settings()


class _FakeKmsClient:
    """Stands in for the boto3 kms client.

    Mimics real KMS semantics closely enough for the encryption module to
    be tested without AWS: `generate_data_key` returns a random AES-256
    key plus a "wrapped" form that is just that same key XORed with a
    fixed per-client secret (so it is not literally plaintext on the
    wire), and `decrypt` reverses the XOR to recover the original key.
    """

    def __init__(self):
        self.generate_calls: list[dict] = []
        self.decrypt_calls: list[dict] = []
        self._xor_secret = os.urandom(32)

    def _wrap(self, plaintext_key: bytes) -> bytes:
        return bytes(b ^ s for b, s in zip(plaintext_key, self._xor_secret, strict=True))

    def generate_data_key(self, *, KeyId, KeySpec):
        self.generate_calls.append({"KeyId": KeyId, "KeySpec": KeySpec})
        plaintext_key = os.urandom(32)
        return {"Plaintext": plaintext_key, "CiphertextBlob": self._wrap(plaintext_key)}

    def decrypt(self, *, CiphertextBlob):
        self.decrypt_calls.append({"CiphertextBlob": CiphertextBlob})
        return {"Plaintext": self._wrap(CiphertextBlob)}  # XOR is its own inverse


def test_encrypt_then_decrypt_round_trips(monkeypatch):
    fake = _FakeKmsClient()
    monkeypatch.setattr(pii_crypto, "client", lambda: fake)

    original = "John Smith's email is john.smith@example.com."
    blob = pii_crypto.encrypt_original_text(original)

    assert blob != original.encode("utf-8")  # genuinely scrambled, not a no-op

    recovered = pii_crypto.decrypt_original_text(blob)

    assert recovered == original


def test_encrypt_uses_configured_key_id(monkeypatch):
    fake = _FakeKmsClient()
    monkeypatch.setattr(pii_crypto, "client", lambda: fake)

    pii_crypto.encrypt_original_text("some text")

    assert fake.generate_calls[0]["KeyId"] == settings.pii_kms_key_id
    assert fake.generate_calls[0]["KeySpec"] == "AES_256"


def test_different_plaintexts_produce_different_ciphertexts(monkeypatch):
    fake = _FakeKmsClient()
    monkeypatch.setattr(pii_crypto, "client", lambda: fake)

    blob_a = pii_crypto.encrypt_original_text("Alice's number is 555-0001")
    blob_b = pii_crypto.encrypt_original_text("Bob's number is 555-0002")

    assert blob_a != blob_b


def test_same_plaintext_encrypted_twice_produces_different_blobs(monkeypatch):
    """Fresh data key + fresh nonce each call -- no two encryptions of the
    same text should look identical, which would otherwise leak that two
    chunks share a value just from ciphertext comparison."""
    fake = _FakeKmsClient()
    monkeypatch.setattr(pii_crypto, "client", lambda: fake)

    text = "repeated real value"
    blob_1 = pii_crypto.encrypt_original_text(text)
    blob_2 = pii_crypto.encrypt_original_text(text)

    assert blob_1 != blob_2
    assert pii_crypto.decrypt_original_text(blob_1) == text
    assert pii_crypto.decrypt_original_text(blob_2) == text


def test_round_trip_preserves_unicode():
    fake = _FakeKmsClient()

    import app.pii_crypto as crypto_module

    original_client = crypto_module.client
    crypto_module.client = lambda: fake
    try:
        original = "Café résumé — José's naïve façade: 日本語"
        blob = crypto_module.encrypt_original_text(original)
        recovered = crypto_module.decrypt_original_text(blob)
    finally:
        crypto_module.client = original_client

    assert recovered == original
