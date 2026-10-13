"""Password hashing.

Argon2id, OWASP's current recommendation for new systems: memory-hard
against GPU/ASIC brute-forcing in a way bcrypt is not. Used only by the
project-invites feature's acceptance flow; the rest of the application
still identifies callers via the `X-User-Id` placeholder.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hash a password for storage. Never store the return value anywhere
    but a password column; never log it.
    """
    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """True if `plain` is the password that produced `hashed`."""
    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False


__all__ = ["hash_password", "verify_password"]
