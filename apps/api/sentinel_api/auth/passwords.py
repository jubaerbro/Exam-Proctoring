"""Password hashing and policy.

Argon2id with OWASP-current parameters. No composition rules ("must contain a
symbol") — they measurably reduce entropy by pushing people toward `Passw0rd!`.
Length plus a breach check is the current NIST/NCSC position.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

# m=64MiB, t=3, p=4 — OWASP's recommended Argon2id configuration.
_hasher = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID
)

# A small built-in list. Production loads a real breach corpus (e.g. the
# Pwned Passwords k-anonymity API or a local bloom filter); the interface below
# is what that swaps into.
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "123456789",
        "qwertyuiop",
        "letmein12345",
        "administrator",
        "welcome12345",
        "iloveyou1234",
        "sentinel1234",
        "changeme1234",
        "111111111111",
        "qwerty123456",
    }
)


class PasswordPolicyError(ValueError):
    pass


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password.

    Returns False rather than raising for a missing hash — but still performs a
    dummy verification so that "user has no password" and "wrong password" take
    the same time. Otherwise login timing enumerates accounts.
    """
    if not password_hash:
        _dummy_verify()
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


_DUMMY_HASH = _hasher.hash("sentinel-timing-equalizer")


def _dummy_verify() -> None:
    try:
        _hasher.verify(_DUMMY_HASH, "not-the-password")
    except VerifyMismatchError:
        pass


def validate_password(password: str, *, min_length: int = 12) -> None:
    """Raise PasswordPolicyError if the password is unacceptable."""
    if len(password) < min_length:
        raise PasswordPolicyError(f"Password must be at least {min_length} characters.")
    if len(password) > 1024:
        # Argon2 is memory-hard; unbounded input is a cheap DoS.
        raise PasswordPolicyError("Password must be at most 1024 characters.")
    if password.lower() in _COMMON_PASSWORDS:
        raise PasswordPolicyError("This password appears in known breach corpora.")


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_breach_list(path: Path) -> None:  # pragma: no cover - production hook
    """STUB. Production replaces the in-memory set with a real corpus."""
    raise NotImplementedError(
        "Breach-corpus loading is not implemented. The built-in list is a placeholder "
        "and must not be mistaken for a real breached-password check."
    )
