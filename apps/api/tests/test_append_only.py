"""Append-only enforcement.

Sentinel's central claim is that its record cannot be quietly rewritten. These
tests assert that at the database level, where it survives application bugs,
support scripts, and a careless migration.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.conftest import admin_tx, demo_org_id

pytestmark = pytest.mark.security

APPEND_ONLY_TABLES = [
    "audit_event",
    "answer_revision",
    "evidence_access_log",
    "activity_log",
    "review_decision",
    "purge_record",
]


def test_append_only_tables_have_a_deny_trigger(admin_engine: Engine) -> None:
    with admin_engine.connect() as conn:
        protected = {
            r[0]
            for r in conn.execute(
                text(
                    """
                    SELECT DISTINCT c.relname
                    FROM pg_trigger t
                    JOIN pg_class c ON c.oid = t.tgrelid
                    JOIN pg_proc p ON p.oid = t.tgfoid
                    WHERE NOT t.tgisinternal AND p.proname = 'deny_mutation'
                    """
                )
            )
        }
    missing = set(APPEND_ONLY_TABLES) - protected
    assert missing == set(), f"Append-only tables without a deny trigger: {sorted(missing)}"


@pytest.fixture
def chain_fixture(admin_engine: Engine):
    """A session with one signed genesis audit event, torn down afterwards.

    Teardown has to disable the trigger to delete the rows, which is itself the
    point: removing an audit event requires deliberately switching off the
    protection, as the owner, outside the application.
    """
    ids = {"key_id": uuid.uuid4()}
    org_id = demo_org_id(admin_engine)

    # signing_key is not tenant-owned; audit_event and exam_session are.
    with admin_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO signing_key (id, public_key, key_fingerprint, private_key_ref) "
                "VALUES (:i, '\\x00'::bytea, :f, 'test')"
            ),
            {"i": ids["key_id"], "f": f"test-{ids['key_id'].hex[:12]}"},
        )

    with admin_tx(admin_engine, org_id=org_id) as conn:
        assignment = conn.execute(
            text("SELECT id, exam_version_id, candidate_user_id FROM exam_assignment LIMIT 1")
        ).one()
        # exam_session is UNIQUE on (assignment_id, attempt_no). Claiming the
        # next free attempt number keeps the fixture independent of whatever
        # earlier runs left behind, without needing a disposable database.
        attempt = conn.execute(
            text(
                "SELECT coalesce(max(attempt_no), 0) + 1 FROM exam_session WHERE assignment_id = :a"
            ),
            {"a": assignment.id},
        ).scalar_one()
        session_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO exam_session (id, org_id, assignment_id, exam_version_id, "
                "candidate_user_id, attempt_no, paper_seed) "
                "VALUES (:i, :o, :a, :v, :c, :n, '\\x00'::bytea)"
            ),
            {
                "i": session_id,
                "o": org_id,
                "a": assignment.id,
                "v": assignment.exam_version_id,
                "c": assignment.candidate_user_id,
                "n": attempt,
            },
        )
        conn.execute(
            text(
                "INSERT INTO audit_event (org_id, session_id, server_seq, event_type, "
                "canonical_sha256, prev_hash, signature, signing_key_id) VALUES "
                "(:o, :s, 1, 'SESSION_STARTED', decode(repeat('61',32),'hex'), NULL, "
                "decode(repeat('62',64),'hex'), :k)"
            ),
            {"o": org_id, "s": session_id, "k": ids["key_id"]},
        )
        ids["org_id"] = org_id
        ids["session_id"] = session_id

    yield ids

    with admin_tx(admin_engine, org_id=ids["org_id"]) as conn:
        # Removing an audit event needs BOTH protections switched off: the
        # append-only trigger, and row-level security (audit_event has no
        # DELETE policy at all, so RLS silently matches zero rows). Doing this
        # deliberately, as the owner, outside the application, is the only way
        # — which is the property the product depends on.
        conn.execute(text("ALTER TABLE audit_event DISABLE TRIGGER audit_event_immutable"))
        conn.execute(text("ALTER TABLE audit_event DISABLE ROW LEVEL SECURITY"))
        conn.execute(
            text("DELETE FROM audit_event WHERE session_id = :s"), {"s": ids["session_id"]}
        )
        conn.execute(text("ALTER TABLE audit_event ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE audit_event FORCE ROW LEVEL SECURITY"))
        conn.execute(text("ALTER TABLE audit_event ENABLE TRIGGER audit_event_immutable"))
        conn.execute(text("DELETE FROM exam_session WHERE id = :s"), {"s": ids["session_id"]})
    with admin_engine.begin() as conn:
        conn.execute(text("DELETE FROM signing_key WHERE id = :k"), {"k": ids["key_id"]})


def test_rls_provides_no_delete_or_update_path_for_audit_events(
    app_engine: Engine, chain_fixture: dict
) -> None:
    """A third, independent protection on the chain.

    audit_event has a SELECT policy and an INSERT policy and nothing else. With
    RLS enabled, an UPDATE or DELETE therefore matches zero rows regardless of
    table privileges — so even if the append-only trigger were dropped by a
    careless migration, the application role still could not rewrite history.
    """
    from sqlalchemy import text as _text

    with app_engine.begin() as conn:
        conn.execute(
            _text("SELECT set_config('sentinel.org_id', :o, true)"),
            {"o": str(chain_fixture["org_id"])},
        )
        conn.execute(_text("SELECT set_config('sentinel.role', 'org_admin', true)"))
        result = conn.execute(
            _text("DELETE FROM audit_event WHERE session_id = :s"),
            {"s": chain_fixture["session_id"]},
        )
        assert result.rowcount == 0, "RLS should expose no DELETE path for audit_event."

    with app_engine.connect() as conn:
        conn.execute(
            _text("SELECT set_config('sentinel.org_id', :o, true)"),
            {"o": str(chain_fixture["org_id"])},
        )
        conn.execute(_text("SELECT set_config('sentinel.role', 'org_admin', true)"))
        still_there = conn.execute(
            _text("SELECT count(*) FROM audit_event WHERE session_id = :s"),
            {"s": chain_fixture["session_id"]},
        ).scalar()
    assert still_there == 1


@contextmanager
def _rls_disabled(engine: Engine, table: str):
    """Temporarily stand RLS down on a table, as the owner.

    Needed because the two protections stack: with RLS on, an UPDATE or DELETE
    on audit_event matches zero rows and the trigger never fires. To test the
    trigger specifically, the outer layer has to be moved out of the way.
    """
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))


def test_audit_event_cannot_be_updated(admin_engine: Engine, chain_fixture: dict) -> None:
    with _rls_disabled(admin_engine, "audit_event"):
        with admin_engine.begin() as conn, pytest.raises(DBAPIError) as exc:
            conn.execute(
                text("UPDATE audit_event SET severity = 4 WHERE session_id = :s"),
                {"s": chain_fixture["session_id"]},
            )
    assert "append-only" in str(exc.value)


def test_audit_event_cannot_be_deleted(admin_engine: Engine, chain_fixture: dict) -> None:
    with _rls_disabled(admin_engine, "audit_event"):
        with admin_engine.begin() as conn, pytest.raises(DBAPIError) as exc:
            conn.execute(
                text("DELETE FROM audit_event WHERE session_id = :s"),
                {"s": chain_fixture["session_id"]},
            )
    assert "append-only" in str(exc.value)


def test_genesis_constraint_rejects_orphan_prev_hash(
    admin_engine: Engine, chain_fixture: dict
) -> None:
    """seq > 1 must carry a prev_hash. A chain cannot begin in the middle."""
    with (
        admin_tx(admin_engine, org_id=chain_fixture["org_id"]) as conn,
        pytest.raises(IntegrityError) as exc,
    ):
        conn.execute(
            text(
                "INSERT INTO audit_event (org_id, session_id, server_seq, event_type, "
                "canonical_sha256, prev_hash, signature, signing_key_id) VALUES "
                "(:o, :s, 2, 'TAB_HIDDEN', decode(repeat('61',32),'hex'), NULL, "
                "decode(repeat('62',64),'hex'), "
                "(SELECT id FROM signing_key ORDER BY created_at DESC LIMIT 1))"
            ),
            {"o": chain_fixture["org_id"], "s": chain_fixture["session_id"]},
        )
    assert "audit_genesis_prev" in str(exc.value)


def test_duplicate_client_seq_is_rejected(admin_engine: Engine, chain_fixture: dict) -> None:
    """Replay protection at the storage layer."""
    key_id = chain_fixture["key_id"]
    params = {"o": chain_fixture["org_id"], "s": chain_fixture["session_id"], "k": key_id}
    insert = text(
        "INSERT INTO audit_event (org_id, session_id, server_seq, client_seq, event_type, "
        "canonical_sha256, prev_hash, signature, signing_key_id) VALUES "
        "(:o, :s, :seq, 99, 'TAB_HIDDEN', decode(repeat('61',32),'hex'), "
        "decode(repeat('61',32),'hex'), decode(repeat('62',64),'hex'), :k)"
    )
    with admin_tx(admin_engine, org_id=chain_fixture["org_id"]) as conn:
        conn.execute(insert, {**params, "seq": 2})

    with (
        admin_tx(admin_engine, org_id=chain_fixture["org_id"]) as conn,
        pytest.raises(IntegrityError) as exc,
    ):
        conn.execute(insert, {**params, "seq": 3})
    assert "audit_client_seq_uq" in str(exc.value)


def test_signature_length_is_enforced(admin_engine: Engine, chain_fixture: dict) -> None:
    with (
        admin_tx(admin_engine, org_id=chain_fixture["org_id"]) as conn,
        pytest.raises(IntegrityError) as exc,
    ):
        conn.execute(
            text(
                "INSERT INTO audit_event (org_id, session_id, server_seq, event_type, "
                "canonical_sha256, prev_hash, signature, signing_key_id) VALUES "
                "(:o, :s, 50, 'TAB_HIDDEN', decode(repeat('61',32),'hex'), "
                "decode(repeat('61',32),'hex'), '\\x00'::bytea, :k)"
            ),
            {
                "o": chain_fixture["org_id"],
                "s": chain_fixture["session_id"],
                "k": chain_fixture["key_id"],
            },
        )
    assert "audit_sig_len" in str(exc.value)


def test_review_decision_requires_real_justification(
    admin_engine: Engine, chain_fixture: dict
) -> None:
    """A mandatory-justification field satisfiable with "." is theatre."""
    org_id = chain_fixture["org_id"]
    session_id = chain_fixture["session_id"]
    with admin_tx(admin_engine, org_id=org_id) as conn:
        reviewer = conn.execute(
            text("SELECT user_id FROM membership WHERE role = 'reviewer' LIMIT 1")
        ).scalar_one()

    with admin_tx(admin_engine, org_id=org_id) as conn:
        review_id = conn.execute(
            text(
                "INSERT INTO review (org_id, session_id, queued_reason) "
                "VALUES (:o, :s, 'manual') RETURNING id"
            ),
            {"o": org_id, "s": session_id},
        ).scalar_one()

    try:
        with admin_tx(admin_engine, org_id=org_id) as conn, pytest.raises(IntegrityError) as exc:
            conn.execute(
                text(
                    "INSERT INTO review_decision (org_id, review_id, session_id, reviewer_id, "
                    "outcome, justification, audit_event_seq) VALUES "
                    "(:o, :rv, :s, :r, 'dismiss', 'ok', 1)"
                ),
                {"o": org_id, "rv": review_id, "s": session_id, "r": reviewer},
            )
        assert "decision_justification_len" in str(exc.value)

        # ...and a real justification is accepted.
        with admin_tx(admin_engine, org_id=org_id) as conn:
            conn.execute(
                text(
                    "INSERT INTO review_decision (org_id, review_id, session_id, reviewer_id, "
                    "outcome, justification, audit_event_seq) VALUES "
                    "(:o, :rv, :s, :r, 'dismiss', :j, 1)"
                ),
                {
                    "o": org_id,
                    "rv": review_id,
                    "s": session_id,
                    "r": reviewer,
                    "j": "Fullscreen exit coincided with a reported browser crash; "
                    "candidate statement is consistent with the network log.",
                },
            )
    finally:
        with admin_tx(admin_engine, org_id=org_id) as conn:
            conn.execute(
                text("ALTER TABLE review_decision DISABLE TRIGGER review_decision_immutable")
            )
            conn.execute(text("DELETE FROM review_decision WHERE review_id = :r"), {"r": review_id})
            conn.execute(
                text("ALTER TABLE review_decision ENABLE TRIGGER review_decision_immutable")
            )
            conn.execute(text("DELETE FROM review WHERE id = :r"), {"r": review_id})
