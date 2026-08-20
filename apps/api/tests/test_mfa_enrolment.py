"""TOTP enrolment.

Phase 1 shipped MFA verification with no way to enrol, which made `reviewer` and
`org_admin` permanently unusable — the check was correct and the feature was
unreachable. These tests cover the way out, and the ways it must not open.
"""

from __future__ import annotations

import pyotp
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.security


def _clear_mfa(engine: Engine, email: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE app_user SET mfa_secret_enc = NULL, mfa_enabled_at = NULL WHERE email = :e"
            ),
            {"e": email.lower()},
        )


def test_login_still_fails_closed_for_an_unenrolled_required_role(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> None:
    """The Phase 1 guarantee is unchanged: no session without a second factor."""
    _clear_mfa(admin_engine, f"reviewer@{seed_domain}")
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": f"reviewer@{seed_domain}", "password": seed_password},
    )
    assert resp.status_code == 403
    assert "access_token" not in resp.json()
    assert "multi-factor" in resp.json()["detail"].lower()


def test_the_refusal_carries_an_enrolment_token(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> None:
    _clear_mfa(admin_engine, f"reviewer@{seed_domain}")
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": f"reviewer@{seed_domain}", "password": seed_password},
    )
    assert resp.json()["enrolment_token"]
    assert resp.json()["enrolment_expires_in"] == 900


def test_an_enrolment_token_opens_nothing_else(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> None:
    """The narrowest possible credential. It enrols; it does not sign you in."""
    _clear_mfa(admin_engine, f"reviewer@{seed_domain}")
    token = client.post(
        "/api/v1/auth/login",
        json={"email": f"reviewer@{seed_domain}", "password": seed_password},
    ).json()["enrolment_token"]
    headers = {"Authorization": f"Bearer {token}"}

    for path in ("/api/v1/auth/me", "/api/v1/org", "/api/v1/exams", "/api/v1/banks"):
        resp = client.get(path, headers=headers)
        assert resp.status_code == 401, f"{path} accepted an enrolment token"


def test_a_wrong_code_does_not_enrol(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> None:
    """Storing the secret before the code is proven would enable an account
    whose owner never successfully scanned the QR code."""
    _clear_mfa(admin_engine, f"reviewer@{seed_domain}")
    token = client.post(
        "/api/v1/auth/login",
        json={"email": f"reviewer@{seed_domain}", "password": seed_password},
    ).json()["enrolment_token"]
    headers = {"Authorization": f"Bearer {token}"}

    secret = client.post("/api/v1/auth/mfa/enrol", headers=headers).json()["secret"]
    bad = client.post(
        "/api/v1/auth/mfa/enrol/confirm",
        json={"secret": secret, "code": "000000"},
        headers=headers,
    )
    assert bad.status_code == 401

    with admin_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT mfa_secret_enc FROM app_user WHERE email = :e"),
            {"e": f"reviewer@{seed_domain}"},
        ).scalar()
    assert stored is None


def test_beginning_enrolment_stores_nothing(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> None:
    _clear_mfa(admin_engine, f"reviewer@{seed_domain}")
    token = client.post(
        "/api/v1/auth/login",
        json={"email": f"reviewer@{seed_domain}", "password": seed_password},
    ).json()["enrolment_token"]
    begin = client.post("/api/v1/auth/mfa/enrol", headers={"Authorization": f"Bearer {token}"})
    assert begin.status_code == 200
    assert begin.json()["already_enrolled"] is False
    assert "otpauth://" in begin.json()["provisioning_uri"]

    with admin_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT mfa_secret_enc FROM app_user WHERE email = :e"),
                {"e": f"reviewer@{seed_domain}"},
            ).scalar()
            is None
        )


def test_the_secret_is_stored_encrypted_not_in_the_clear(
    client: TestClient, admin_engine: Engine, seed_domain: str, seed_password: str
) -> None:
    """A database read must not yield a working second factor."""
    _clear_mfa(admin_engine, f"reviewer@{seed_domain}")
    token = client.post(
        "/api/v1/auth/login",
        json={"email": f"reviewer@{seed_domain}", "password": seed_password},
    ).json()["enrolment_token"]
    headers = {"Authorization": f"Bearer {token}"}
    secret = client.post("/api/v1/auth/mfa/enrol", headers=headers).json()["secret"]
    client.post(
        "/api/v1/auth/mfa/enrol/confirm",
        json={"secret": secret, "code": pyotp.TOTP(secret).now()},
        headers=headers,
    )

    with admin_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT mfa_secret_enc FROM app_user WHERE email = :e"),
            {"e": f"reviewer@{seed_domain}"},
        ).scalar()
    assert stored is not None
    assert secret.encode() not in bytes(stored)


def test_the_reviewer_role_is_usable_after_enrolment(
    client: TestClient, reviewer_login: dict
) -> None:
    """The end of the Phase 1 defect: a reviewer can now hold a session."""
    assert reviewer_login["access_token"]
    resp = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {reviewer_login['access_token']}"},
    )
    assert resp.status_code == 200
    assert resp.json()["mfa_enabled"] is True
    assert "reviewer" in resp.json()["roles"]


def test_an_enrolled_org_admin_passes_the_mfa_gate(
    client: TestClient, org_admin: dict[str, str]
) -> None:
    """`authorize` refuses a required role whose token lacks `otp` in `amr`.

    Reaching an org_admin-only endpoint proves the whole chain: enrol, sign in,
    challenge, verify, and a token that satisfies the gate.
    """
    resp = client.get("/api/v1/org/activity", headers=org_admin)
    assert resp.status_code == 200


def test_an_unenrolled_candidate_is_unaffected(client: TestClient, candidate_login: dict) -> None:
    """MFA is required for roles that can read webcam imagery. Candidates
    cannot, and must not be locked out of their own exam by a control aimed at
    staff."""
    assert candidate_login["access_token"]
