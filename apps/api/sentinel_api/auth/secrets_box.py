"""Symmetric encryption for secrets stored at rest.

Currently used only for TOTP secrets. A TOTP secret in plaintext in the database
is a second factor that a database read defeats, which makes it not a second
factor.

STATUS: PARTIALLY IMPLEMENTED. This is AES-256-GCM with a locally held key.
Real envelope encryption — a data key wrapped by a KMS master key, so that the
database administrator cannot decrypt — is Phase 11 work and shares the fate of
KmsKeyProvider in ``keys.py``.
"""

from __future__ import annotations

import os
import secrets as _secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
_VERSION = b"\x01"


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise ValueError(f"Key must be {KEY_BYTES} bytes, got {len(key)}.")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, *, aad: bytes = b"sentinel.mfa") -> bytes:
        nonce = _secrets.token_bytes(NONCE_BYTES)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return _VERSION + nonce + ct

    def decrypt(self, blob: bytes, *, aad: bytes = b"sentinel.mfa") -> str:
        if not blob or blob[:1] != _VERSION:
            raise ValueError("Unrecognised ciphertext version.")
        nonce = blob[1 : 1 + NONCE_BYTES]
        ct = blob[1 + NONCE_BYTES :]
        return self._aead.decrypt(nonce, ct, aad).decode("utf-8")

    @classmethod
    def from_file(cls, path: str | Path) -> SecretBox:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(
                f"MFA encryption key not found at {p}. Run scripts/keygen.py first."
            )
        return cls(p.read_bytes())

    @staticmethod
    def generate_key_file(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(_secrets.token_bytes(KEY_BYTES))
        return path
