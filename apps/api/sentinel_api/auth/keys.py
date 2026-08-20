"""Signing key custody.

Two distinct keys, deliberately not shared:

* the **audit key** signs the per-session hash chain
* the **JWT key** signs access tokens

Sharing one key would mean that anything able to mint a session token could also
forge an audit event. Those are very different blast radii.

The ``KeyProvider`` interface is the seam ADR-0003 promised: ``FileKeyProvider``
is for local development only, and ``config.py`` refuses to start the process if
it is selected in production. ``KmsKeyProvider`` is a STUB — Phase 11 work.
"""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from abc import ABC, abstractmethod
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def key_fingerprint(public_key: bytes) -> str:
    """base64url(SHA-256(raw public key)), unpadded."""
    digest = hashlib.sha256(public_key).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


class KeyProvider(ABC):
    @abstractmethod
    def sign(self, message: bytes) -> bytes: ...

    @abstractmethod
    def public_key_bytes(self) -> bytes: ...

    def fingerprint(self) -> str:
        return key_fingerprint(self.public_key_bytes())

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(self.public_key_bytes()).verify(signature, message)
            return True
        except Exception:
            return False


class FileKeyProvider(KeyProvider):
    """DEVELOPMENT ONLY.

    Anyone with host access can read this key and forge a chain that verifies,
    which voids every tamper-evidence claim Sentinel makes. Refused in
    production by Settings validation.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(
                f"Signing key not found at {self._path}. Run scripts/keygen.py first."
            )
        self._check_permissions()
        self._private = self._load()

    def _check_permissions(self) -> None:
        mode = self._path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError(
                f"{self._path} is group/world accessible ({stat.filemode(mode)}). "
                "A signing key readable by other users is not a signing key."
            )

    def _load(self) -> Ed25519PrivateKey:
        data = self._path.read_bytes()
        key = serialization.load_pem_private_key(data, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("Signing key must be Ed25519.")
        return key

    def sign(self, message: bytes) -> bytes:
        return self._private.sign(message)

    def public_key_bytes(self) -> bytes:
        return self._private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )

    def private_key_ref(self) -> str:
        return f"file://{self._path}"


class KmsKeyProvider(KeyProvider):  # pragma: no cover - not implemented
    """STUB — NOT IMPLEMENTED.

    Production key custody (T4.3, T6.4) requires that the API service account can
    sign but no human can export, and that the database administrator cannot
    administer the key. That is the whole basis of the tamper-evidence claim and
    it is not built yet.
    """

    def __init__(self, key_ref: str) -> None:
        raise NotImplementedError(
            "KmsKeyProvider is not implemented. Sentinel cannot run in production "
            "until key custody is real. See SECURITY.md §3 and DEPLOYMENT.md §6."
        )

    def sign(self, message: bytes) -> bytes:
        raise NotImplementedError

    def public_key_bytes(self) -> bytes:
        raise NotImplementedError


def generate_keypair(path: Path) -> tuple[Path, Path]:
    """Write a new Ed25519 keypair. Private key is written 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()

    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    # Create with restrictive permissions from the outset rather than
    # chmod-ing after write, which leaves a window where the key is readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)

    pub_path = path.with_suffix(".pub")
    pub_path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return path, pub_path


def build_provider(provider: str, *, path: str, key_ref: str) -> KeyProvider:
    if provider == "file":
        return FileKeyProvider(path)
    if provider == "kms":
        return KmsKeyProvider(key_ref)
    raise ValueError(f"Unknown signing key provider: {provider!r}")
