"""Generate local development keys.

DEVELOPMENT ONLY. These are files on disk. Anyone with host access can read the
audit key and forge a chain that verifies, which voids every tamper-evidence
claim Sentinel makes. ``Settings`` refuses to start the process with
``SIGNING_KEY_PROVIDER=file`` when ``SENTINEL_ENV=production``.

Three keys, deliberately separate:

  audit_ed25519  signs the per-session hash chain
  jwt_ed25519    signs access tokens
  mfa_aes        encrypts TOTP secrets at rest

Sharing one key across these would mean anything able to mint a session token
could also forge an audit event, and a database read would defeat the second
factor. Different blast radii deserve different keys.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "api"))

from sentinel_api.auth.keys import generate_keypair, key_fingerprint  # noqa: E402
from sentinel_api.auth.secrets_box import SecretBox  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Sentinel development keys.")
    parser.add_argument(
        "--out",
        default=None,
        help="Directory to write keys into. Defaults to /run/keys inside the "
        "container image, and to <repo>/.keys everywhere else.",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing keys.")
    args = parser.parse_args()

    # `/run/keys` is the volume the compose stack mounts into the API. It does
    # not exist on a developer machine, and failing there with a permission
    # error is a poor first experience — so outside the container the default is
    # the same `<repo>/.keys` that `.env.local.example` points at.
    if args.out is not None:
        out = Path(args.out)
    elif Path("/run/keys").is_dir():
        out = Path("/run/keys")
    else:
        from sentinel_api.core.config import repo_root

        out = repo_root() / ".keys"

    out.mkdir(parents=True, exist_ok=True)
    print(f"Writing development keys to {out}")

    made: list[str] = []

    for name in ("audit_ed25519", "jwt_ed25519"):
        path = out / f"{name}.key"
        if path.exists() and not args.force:
            print(f"  exists  {path}")
            continue
        generate_keypair(path)
        made.append(name)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        assert isinstance(key, Ed25519PrivateKey)
        raw_pub = key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
        )
        print(f"  created {path}  fingerprint={key_fingerprint(raw_pub)}")

    mfa_path = out / "mfa_aes.key"
    if mfa_path.exists() and not args.force:
        print(f"  exists  {mfa_path}")
    else:
        SecretBox.generate_key_file(mfa_path)
        made.append("mfa_aes")
        print(f"  created {mfa_path}")

    if made:
        print(
            "\nDEVELOPMENT KEYS. Do not deploy these. Production requires a KMS/HSM "
            "the database administrator cannot administer — see SECURITY.md §3."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
