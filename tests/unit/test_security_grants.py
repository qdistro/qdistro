"""Unit tests for qdistro_security_grants — capability-based delegation of
security-field mutation authority (decisions/security-field-mutation-authority.md).

Covers, fail-closed: admin-app default-allow, exact per-field grant allow,
ungranted deny, wildcard (security.* and *) scope, exact-match rigidity (no
prefix/parent matching), grant of an unknown field rejected, revoke is
append-only / does not rewrite history, replay nets grants vs revokes, and the
grant log stays inside the sealed hash chain.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "broker"))

from qdistro_lineage_store import LineageStore  # noqa: E402
from qdistro_security_grants import (  # noqa: E402
    ADMIN_APP_PRINCIPAL,
    GRANT_FACT,
    REVOKE_FACT,
    GrantError,
    SecurityGrantStore,
    grant_subject,
    grants_permit_field,
    normalize_field,
    scope_matches_field,
)


@pytest.fixture
def store(tmp_path):
    s = LineageStore(str(tmp_path / "lineage.db"))
    yield s
    s.close()


@pytest.fixture
def grants(store):
    return SecurityGrantStore(store)


# --------------------------------------------------------------------------
# pure matcher
# --------------------------------------------------------------------------

def test_normalize_accepts_bare_and_qualified():
    assert normalize_field("guards") == "security.guards"
    assert normalize_field("security.guards") == "security.guards"


def test_normalize_rejects_unknown_field():
    with pytest.raises(GrantError):
        normalize_field("everything")
    with pytest.raises(GrantError):
        normalize_field("security.everything")


def test_exact_scope_matches_only_that_field():
    assert scope_matches_field("security.guards", "security.guards")
    assert not scope_matches_field("security.guards", "security.compartments")


def test_no_prefix_or_parent_matching():
    # 'security' is NOT a wildcard and must not match a child field.
    assert not scope_matches_field("security", "security.guards")
    # A near-miss field token must not match.
    assert not scope_matches_field("security.guard", "security.guards")


def test_wildcards_match_every_field():
    for f in ("security.guards", "security.compartments", "security.sensitivity",
              "security.conflict_classes", "security.declassification"):
        assert scope_matches_field("security.*", f)
        assert scope_matches_field("*", f)


def test_unknown_field_never_matched_even_by_wildcard():
    assert not scope_matches_field("*", "security.bogus")
    assert not grants_permit_field(frozenset({"*"}), "security.bogus")


# --------------------------------------------------------------------------
# admin-app default authority
# --------------------------------------------------------------------------

def test_admin_app_allowed_by_default_with_no_grant(grants):
    chk = grants.check(ADMIN_APP_PRINCIPAL, "security.guards")
    assert chk.allowed and chk.via == "admin-app-default"


def test_admin_app_cannot_be_locked_out_by_empty_log(grants):
    # No grants in the store at all; admin app still allowed every field.
    for f in ("guards", "compartments", "sensitivity", "declassification",
              "conflict_classes"):
        assert grants.check(ADMIN_APP_PRINCIPAL, f).allowed


def test_custom_admin_principal(store):
    g = SecurityGrantStore(store, admin_app="agent:custom-admin")
    assert g.check("agent:custom-admin", "security.guards").allowed
    # the default admin is now just an ordinary ungranted principal
    assert not g.check(ADMIN_APP_PRINCIPAL, "security.guards").allowed


# --------------------------------------------------------------------------
# delegated grant: allow / deny / scope
# --------------------------------------------------------------------------

def test_ungranted_principal_denied(grants):
    chk = grants.check("wf:export", "security.guards")
    assert not chk.allowed and "no mutation grant" in chk.reason


def test_exact_field_grant_allows_only_that_field(grants):
    grants.grant("wf:export", "security.sensitivity", granter=ADMIN_APP_PRINCIPAL)
    assert grants.check("wf:export", "security.sensitivity").allowed
    # a different field is still denied
    assert not grants.check("wf:export", "security.guards").allowed


def test_grant_reports_authorising_scope(grants):
    grants.grant("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    chk = grants.check("wf:export", "security.guards")
    assert chk.allowed and chk.via == "security.guards"


def test_security_wildcard_grants_all_security_fields(grants):
    grants.grant("wf:promote", "security.*", granter=ADMIN_APP_PRINCIPAL)
    for f in ("guards", "compartments", "sensitivity", "conflict_classes",
              "declassification"):
        chk = grants.check("wf:promote", f)
        assert chk.allowed and chk.via == "security.*"


def test_full_wildcard_grants_all(grants):
    grants.grant("wf:promote", "*", granter=ADMIN_APP_PRINCIPAL)
    assert grants.check("wf:promote", "security.guards").via == "*"


def test_grant_unknown_scope_rejected(grants):
    with pytest.raises(GrantError):
        grants.grant("wf:x", "security.everything", granter=ADMIN_APP_PRINCIPAL)
    with pytest.raises(GrantError):
        grants.grant("wf:x", "filesystem", granter=ADMIN_APP_PRINCIPAL)


def test_grant_requires_non_empty_granter(grants):
    with pytest.raises(GrantError):
        grants.grant("wf:x", "guards", granter="")


# --------------------------------------------------------------------------
# revoke: append-only, replay nets grant vs revoke
# --------------------------------------------------------------------------

def test_revoke_removes_capability(grants):
    grants.grant("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    assert grants.check("wf:export", "guards").allowed
    grants.revoke("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    assert not grants.check("wf:export", "guards").allowed


def test_revoke_is_scope_specific(grants):
    grants.grant("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    grants.grant("wf:export", "sensitivity", granter=ADMIN_APP_PRINCIPAL)
    grants.revoke("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    assert not grants.check("wf:export", "guards").allowed
    # the other grant is untouched
    assert grants.check("wf:export", "sensitivity").allowed


def test_re_grant_after_revoke_restores(grants):
    grants.grant("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    grants.revoke("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    grants.grant("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    assert grants.check("wf:export", "guards").allowed


def test_revoke_does_not_rewrite_grant_history(store, grants):
    """A revoke is a NEW append-only event; the original grant row stays in the
    log and the sealed chain still verifies (audit-immutable)."""
    grants.grant("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    grants.revoke("wf:export", "guards", granter=ADMIN_APP_PRINCIPAL)
    log = store.assertions_for(grant_subject("wf:export"), authority="broker-derived")
    facts = [e["fact"] for e in log]
    # Both events present, in order, original grant NOT deleted/edited.
    assert facts == [GRANT_FACT, REVOKE_FACT]
    assert store.verify_chain() is True


def test_revoke_full_wildcard_does_not_cancel_narrower(grants):
    grants.grant("wf:x", "security.*", granter=ADMIN_APP_PRINCIPAL)
    grants.grant("wf:x", "guards", granter=ADMIN_APP_PRINCIPAL)
    grants.revoke("wf:x", "security.*", granter=ADMIN_APP_PRINCIPAL)
    # security.* gone, but the narrower exact guards grant survives.
    assert grants.check("wf:x", "guards").allowed
    assert not grants.check("wf:x", "compartments").allowed


# --------------------------------------------------------------------------
# fail-closed edge cases
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# can_delegate — who may hand out a capability (no self-service escalation)
# --------------------------------------------------------------------------

def test_admin_may_delegate_anything(grants):
    assert grants.can_delegate(ADMIN_APP_PRINCIPAL, "guards").allowed
    assert grants.can_delegate(ADMIN_APP_PRINCIPAL, "*").allowed


def test_ungranted_principal_cannot_delegate(grants):
    assert not grants.can_delegate("wf:rogue", "guards").allowed
    assert not grants.can_delegate("wf:rogue", "*").allowed


def test_holder_can_delegate_only_what_it_holds(grants):
    grants.grant("wf:lead", "guards", granter=ADMIN_APP_PRINCIPAL)
    # holds guards -> may delegate guards
    assert grants.can_delegate("wf:lead", "guards").allowed
    # does not hold sensitivity -> may not delegate it
    assert not grants.can_delegate("wf:lead", "sensitivity").allowed
    # cannot mint a wildcard from a single-field grant
    assert not grants.can_delegate("wf:lead", "security.*").allowed
    assert not grants.can_delegate("wf:lead", "*").allowed


def test_wildcard_holder_can_delegate_within_scope(grants):
    grants.grant("wf:lead", "security.*", granter=ADMIN_APP_PRINCIPAL)
    # security.* holder may delegate any security field and security.*
    assert grants.can_delegate("wf:lead", "guards").allowed
    assert grants.can_delegate("wf:lead", "security.*").allowed
    # but not the full '*' (broader than it holds)
    assert not grants.can_delegate("wf:lead", "*").allowed


def test_empty_granter_cannot_delegate(grants):
    assert not grants.can_delegate("", "guards").allowed


def test_empty_principal_denied(grants):
    assert not grants.check("", "security.guards").allowed


def test_check_unknown_field_denied(grants):
    grants.grant("wf:x", "*", granter=ADMIN_APP_PRINCIPAL)
    # even with a full wildcard, a bogus field cannot be matched
    assert not grants.check("wf:x", "bogus").allowed
