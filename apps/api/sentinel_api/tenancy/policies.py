"""Declarative authorization policy.

Authorization lives in one table, not scattered through endpoint handlers. Two
reasons, both practical:

1. A reviewer can read the whole permission model in one screen. Scattered
   ``if role == ...`` checks cannot be audited.
2. ``tests/test_authz_matrix.py`` enumerates every route on the app and fails
   if any route is missing from this table. "We forgot the authz check on the
   new endpoint" stops being a matter of luck.

Cross-tenant access is not expressed here at all — it is handled by returning
404 from the object lookup, backed by row-level security. This table answers
only "may this role perform this action within its own tenant".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    ORG_ADMIN = "org_admin"
    INSTRUCTOR = "instructor"
    REVIEWER = "reviewer"
    CANDIDATE = "candidate"


ALL_ROLES = frozenset(Role)
STAFF = frozenset({Role.ORG_ADMIN, Role.INSTRUCTOR, Role.REVIEWER})
#: Roles that may author content. Reviewers are deliberately excluded: a
#: reviewer decides whether a session was legitimate, and giving the adjudicator
#: the ability to edit the exam they are adjudicating collapses that separation.
AUTHORS = frozenset({Role.ORG_ADMIN, Role.INSTRUCTOR})


@dataclass(frozen=True)
class Rule:
    """One endpoint's authorization requirement."""

    roles: frozenset[Role]
    #: True when the endpoint requires no authenticated session at all.
    public: bool = False
    #: True when the endpoint may be called with a valid session that has not
    #: yet selected an organization (login → switch-org, /auth/me).
    org_optional: bool = False
    #: Notes for the reader, and for the report the matrix test prints.
    note: str = ""


PUBLIC = Rule(roles=frozenset(), public=True)

#: (method, path template) -> Rule
POLICY: dict[tuple[str, str], Rule] = {
    # -- infrastructure ---------------------------------------------------
    ("GET", "/health"): PUBLIC,
    ("GET", "/ready"): PUBLIC,
    ("GET", "/api/v1/public-keys"): Rule(
        roles=frozenset(),
        public=True,
        note="Deliberately unauthenticated. Verification that requires Sentinel's "
        "permission is not verification.",
    ),
    # -- auth --------------------------------------------------------------
    ("POST", "/api/v1/auth/login"): PUBLIC,
    ("POST", "/api/v1/auth/mfa/verify"): Rule(
        roles=frozenset(), public=True, note="Consumes a short-lived mfa_challenge token."
    ),
    ("POST", "/api/v1/auth/mfa/enrol"): Rule(
        roles=frozenset(),
        public=True,
        note="Authenticates itself: accepts an access token OR the short-lived "
        "mfa_enrol token login hands back when a role requires MFA and the account "
        "has none. Without the second case the required roles are unreachable.",
    ),
    ("POST", "/api/v1/auth/mfa/enrol/confirm"): Rule(
        roles=frozenset(), public=True, note="Same token handling as /mfa/enrol."
    ),
    ("POST", "/api/v1/auth/refresh"): PUBLIC,
    ("POST", "/api/v1/auth/logout"): PUBLIC,
    ("POST", "/api/v1/auth/switch-org"): Rule(
        roles=ALL_ROLES, org_optional=True, note="Binds a session to one tenant."
    ),
    ("GET", "/api/v1/auth/me"): Rule(roles=ALL_ROLES, org_optional=True),
    # -- organization ------------------------------------------------------
    ("GET", "/api/v1/org"): Rule(roles=ALL_ROLES),
    ("GET", "/api/v1/org/members"): Rule(
        roles=frozenset({Role.ORG_ADMIN, Role.INSTRUCTOR}),
        note="Candidates must not enumerate their cohort.",
    ),
    ("GET", "/api/v1/org/activity"): Rule(roles=frozenset({Role.ORG_ADMIN})),
    # -- candidate ---------------------------------------------------------
    ("GET", "/api/v1/me/exams"): Rule(
        roles=frozenset({Role.CANDIDATE}),
        note="Returns only assignments belonging to the calling candidate.",
    ),
    # -- candidate delivery ------------------------------------------------
    # Every one of these is additionally scoped to the caller's own session in
    # the handler, and to their own rows by the RLS policies migration 0002
    # adds. The role check here is the outermost of three.
    ("POST", "/api/v1/sessions"): Rule(roles=frozenset({Role.CANDIDATE})),
    ("GET", "/api/v1/sessions/{session_id}"): Rule(roles=frozenset({Role.CANDIDATE})),
    ("GET", "/api/v1/assignments/{assignment_id}/session"): Rule(roles=frozenset({Role.CANDIDATE})),
    ("PUT", "/api/v1/sessions/{session_id}/answers/{paper_item_id}"): Rule(
        roles=frozenset({Role.CANDIDATE})
    ),
    ("POST", "/api/v1/sessions/{session_id}/submit"): Rule(roles=frozenset({Role.CANDIDATE})),
    ("GET", "/api/v1/sessions/{session_id}/result"): Rule(roles=frozenset({Role.CANDIDATE})),
    # -- code judge --------------------------------------------------------
    # The API only ever enqueues. Execution happens in the judge worker, which
    # is a separate process with no HTTP surface — an endpoint that could start
    # a container would make an API compromise into root on the judge host.
    ("POST", "/api/v1/sessions/{session_id}/items/{paper_item_id}/run"): Rule(
        roles=frozenset({Role.CANDIDATE})
    ),
    ("GET", "/api/v1/sessions/{session_id}/items/{paper_item_id}/runs"): Rule(
        roles=frozenset({Role.CANDIDATE})
    ),
    ("GET", "/api/v1/sessions/{session_id}/runs/{run_id}"): Rule(roles=frozenset({Role.CANDIDATE})),
    # -- staff -------------------------------------------------------------
    ("GET", "/api/v1/exams"): Rule(roles=AUTHORS | {Role.REVIEWER}),
    ("POST", "/api/v1/exams"): Rule(roles=AUTHORS),
    ("POST", "/api/v1/exams/{exam_id}/versions"): Rule(roles=AUTHORS),
    ("GET", "/api/v1/exams/{exam_id}/versions"): Rule(roles=AUTHORS | {Role.REVIEWER}),
    ("POST", "/api/v1/exam-versions/{version_id}/publish"): Rule(
        roles=AUTHORS, note="Freezes the structure a candidate's paper is generated from."
    ),
    ("POST", "/api/v1/exam-versions/{version_id}/assignments"): Rule(roles=AUTHORS),
    # -- question bank -----------------------------------------------------
    ("POST", "/api/v1/banks"): Rule(roles=AUTHORS),
    ("GET", "/api/v1/banks"): Rule(roles=AUTHORS),
    ("POST", "/api/v1/banks/{bank_id}/questions"): Rule(roles=AUTHORS),
    ("GET", "/api/v1/banks/{bank_id}/questions"): Rule(
        roles=AUTHORS,
        note="Returns answer keys. Reviewers are excluded: adjudicating a session "
        "does not require the key, and the key is the most valuable thing to leak.",
    ),
    ("POST", "/api/v1/questions/import"): Rule(roles=AUTHORS),
    ("GET", "/api/v1/banks/{bank_id}/export"): Rule(
        roles=frozenset({Role.ORG_ADMIN}),
        note="A whole bank with answer keys in one response. Narrowest sensible role.",
    ),
}


class PolicyError(LookupError):
    pass


def rule_for(method: str, path: str) -> Rule:
    try:
        return POLICY[(method.upper(), path)]
    except KeyError as exc:
        raise PolicyError(
            f"No authorization rule for {method.upper()} {path}. "
            "Every route must be declared in POLICY — see tenancy/policies.py."
        ) from exc


def permits(rule: Rule, roles: frozenset[str]) -> bool:
    if rule.public:
        return True
    return bool(roles & {str(r) for r in rule.roles})
