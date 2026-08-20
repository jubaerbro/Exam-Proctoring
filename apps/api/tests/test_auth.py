"""Authentication and session-security tests."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from sentinel_api.auth.passwords import hash_password, validate_password, verify_password
from sentinel_api.auth.tokens import hash_refresh_token

# ------------------------------------------------------------------ passwords


def test_password_round_trip() -> None:
    h = hash_password("correct-horse-battery")
    assert verify_password("correct-horse-battery", h)
    assert not verify_password("wrong-horse-battery", h)


def test_password_hash_is_argon2id() -> None:
    assert hash_password("correct-horse-battery").startswith("$argon2id$")


def test_verify_against_missing_hash_is_false_not_error() -> None:
    """An OIDC-only account has no password hash. Verification must fail
    closed rather than raise, and must still burn time so login timing does not
    distinguish 'no password set' from 'wrong password'."""
    started = time.perf_counter()
    assert verify_password("anything", None) is False
    assert time.perf_counter() - started > 0.005, "Missing-hash path returned suspiciously fast."


@pytest.mark.parametrize("bad", ["short", "password123", "a" * 2000])
def test_password_policy_rejects(bad: str) -> None:
    with pytest.raises(ValueError):
        validate_password(bad)


def test_password_policy_accepts_a_long_passphrase() -> None:
    validate_password("three horses walked into a barn")


# ---------------------------------------------------------------------- login


def test_login_succeeds_and_binds_single_org(candidate_login: dict) -> None:
    assert candidate_login["org_id"], "A user in exactly one org should be bound to it."
    assert candidate_login["roles"] == ["candidate"]
    assert len(candidate_login["organizations"]) == 1


def test_login_sets_httponly_cookies(
    client: TestClient, seed_domain: str, seed_password: str
) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": f"daniel.okafor@{seed_domain}", "password": seed_password},
    )
    assert resp.status_code == 200
    raw = resp.headers.get_list("set-cookie")
    access = next(c for c in raw if c.startswith("sentinel_access="))
    refresh = next(c for c in raw if c.startswith("sentinel_refresh="))
    csrf = next(c for c in raw if c.startswith("sentinel_csrf="))

    assert "HttpOnly" in access, "Access cookie must be httpOnly (XSS token theft)."
    assert "HttpOnly" in refresh
    assert "HttpOnly" not in csrf, "CSRF cookie must be readable by the SPA."
    assert "Path=/api/v1/auth" in refresh, "Refresh cookie must be scoped to the auth endpoints."


def test_wrong_password_is_rejected(client: TestClient, seed_domain: str) -> None:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": f"mei.tanaka@{seed_domain}", "password": "not-the-password-at-all"},
    )
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_unknown_and_known_accounts_give_identical_errors(
    client: TestClient, seed_domain: str
) -> None:
    """Login must not enumerate accounts.

    A different message, status, or shape for 'no such user' hands an attacker
    a list of valid addresses before a credential-stuffing run.
    """
    unknown = client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@nowhere.example.org", "password": "some-password-here"},
    )
    known = client.post(
        "/api/v1/auth/login",
        json={"email": f"mei.tanaka@{seed_domain}", "password": "some-password-here"},
    )
    assert unknown.status_code == known.status_code == 401
    assert unknown.json()["detail"] == known.json()["detail"]
    assert unknown.json()["type"] == known.json()["type"]


def test_me_requires_authentication(app) -> None:
    fresh = TestClient(app)
    assert fresh.get("/api/v1/auth/me").status_code == 401


def test_me_returns_identity(client: TestClient, candidate_login: dict) -> None:
    resp = client.get(
        "/api/v1/auth/me",
        headers={"authorization": f"Bearer {candidate_login['access_token']}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["roles"] == ["candidate"]
    assert body["mfa_enabled"] is False


# -------------------------------------------------------------------- tokens


def test_access_token_does_not_carry_permissions(candidate_login: dict) -> None:
    """Permissions are resolved per request, so a revoked role takes effect
    within one access-token lifetime and needs no blacklist."""
    import jwt

    payload = jwt.decode(candidate_login["access_token"], options={"verify_signature": False})
    assert "roles" in payload
    for forbidden in ("permissions", "scopes", "can", "abilities"):
        assert forbidden not in payload


def test_tampered_access_token_is_rejected(client: TestClient, candidate_login: dict) -> None:
    token = candidate_login["access_token"]
    head, payload, sig = token.split(".")
    forged = f"{head}.{payload}.{'A' * len(sig)}"
    resp = client.get("/api/v1/auth/me", headers={"authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_alg_none_token_is_rejected(client: TestClient, candidate_login: dict) -> None:
    """Classic JWT confusion attack. The decoder pins algorithms=[EdDSA]."""
    import base64
    import json

    import jwt

    payload = jwt.decode(candidate_login["access_token"], options={"verify_signature": False})

    def b64(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(payload)}."
    resp = client.get("/api/v1/auth/me", headers={"authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_refresh_tokens_are_stored_hashed(app_engine: Engine, candidate_login: dict) -> None:
    """A database read must not yield usable credentials."""
    with app_engine.connect() as conn:
        rows = conn.execute(text("SELECT token_hash FROM refresh_token LIMIT 5")).all()
    assert rows, "Expected refresh tokens from the login fixture."
    for (digest,) in rows:
        assert isinstance(digest, (bytes, memoryview))
        assert len(bytes(digest)) == 32, "Stored value should be a SHA-256 digest."


def test_refresh_rotates_and_reuse_revokes_the_family(
    app, seed_domain: str, seed_password: str
) -> None:
    """Rotation plus reuse detection.

    Presenting an already-rotated refresh token means it leaked. We cannot tell
    the attacker from the victim, so the whole family is revoked — both lose
    the session, which is the correct trade.
    """
    # `with` runs the app lifespan, which builds the key providers and codecs.
    with TestClient(app) as session:
        pass
    session = TestClient(app)
    login = session.post(
        "/api/v1/auth/login",
        json={"email": f"luis.ferreira@{seed_domain}", "password": seed_password},
    )
    assert login.status_code == 200
    stolen = session.cookies.get("sentinel_refresh")
    assert stolen

    first = session.post("/api/v1/auth/refresh")
    assert first.status_code == 200
    rotated = session.cookies.get("sentinel_refresh")
    assert rotated != stolen, "Refresh token was not rotated."

    # The attacker replays the old token on a separate client.
    attacker = TestClient(app)
    attacker.cookies.set("sentinel_refresh", stolen)
    replay = attacker.post("/api/v1/auth/refresh")
    assert replay.status_code == 401
    assert "reuse" in replay.json()["detail"].lower()

    # ...and the legitimate holder is now locked out too. Intended.
    after = session.post("/api/v1/auth/refresh")
    assert after.status_code == 401


def test_logout_revokes_the_refresh_token(
    app, app_engine: Engine, seed_domain: str, seed_password: str
) -> None:
    session = TestClient(app)
    session.post(
        "/api/v1/auth/login",
        json={"email": f"nadia.hassan@{seed_domain}", "password": seed_password},
    )
    raw = session.cookies.get("sentinel_refresh")
    assert raw, "Refresh cookie was not stored by the client."
    assert session.post("/api/v1/auth/logout").status_code == 204

    with app_engine.connect() as conn:
        revoked = conn.execute(
            text("SELECT revoked_at FROM refresh_token WHERE token_hash = :h"),
            {"h": hash_refresh_token(raw)},
        ).scalar()
    assert revoked is not None


# ----------------------------------------------------------------------- mfa


def test_reviewer_cannot_sign_in_without_mfa_enrolment(
    client: TestClient, admin_engine, seed_domain: str, seed_password: str
) -> None:
    """Fail closed.

    The reviewer role can read webcam imagery of candidates in their homes. If
    MFA is required for the role and the account has not enrolled, refusing the
    session is the only safe answer — signing them in "just this once" would
    make the requirement advisory.

    The enrolment state is reset first. Phase 2 added a working enrolment
    endpoint, so whether this account has a factor now depends on which other
    tests have run — and a test whose result depends on test ordering is not
    evidence of anything.
    """
    from sqlalchemy import text as _text

    with admin_engine.begin() as conn:
        conn.execute(
            _text(
                "UPDATE app_user SET mfa_secret_enc = NULL, mfa_enabled_at = NULL WHERE email = :e"
            ),
            {"e": f"reviewer@{seed_domain}"},
        )

    resp = client.post(
        "/api/v1/auth/login",
        json={"email": f"reviewer@{seed_domain}", "password": seed_password},
    )
    assert resp.status_code == 403
    assert "multi-factor" in resp.json()["detail"].lower()
    assert "access_token" not in resp.json()


def test_mfa_challenge_token_is_not_an_access_token(client: TestClient) -> None:
    """A challenge token must not open a session on its own."""
    from sentinel_api.core.runtime import get_runtime

    rt = get_runtime()
    import uuid as _uuid

    token, _ = rt.access_codec.issue(
        user_id=_uuid.uuid4(), org_id=None, roles=("candidate",), typ="mfa_challenge"
    )
    resp = client.get("/api/v1/auth/me", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 401
