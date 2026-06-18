"""Unit tests for cli/qdistro_security_cli — the user-visible conflict /
declassification CLI surface (doc/guards.md vocabulary).

Covers: show snapshot, verdict rendering (allow vs deny with the cross-silo /
declassify call-to-action and a non-zero exit on deny), impact (guarded
descendants), and that request-declassify only advances STATE (never narrows
guards, never bypasses authority).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "broker"))
sys.path.insert(0, str(_ROOT / "cli"))

import qdistro_security_cli as cli  # noqa: E402
from qdistro_lineage_store import Entity, LineageStore  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "lineage.sqlite")
    s = LineageStore(path)
    s.record_entity(Entity(
        eid="file:secret", kind="file", guards=frozenset({"local-only"}),
        compartments=frozenset({"work"}), conflict_classes=frozenset({"hw"}),
        facets={"sensitivity": "confidential", "declassification": "none"},
    ))
    s.close()
    return path


def _run(db, *argv):
    return cli.main(["--db", db, *argv])


# --------------------------------------------------------------------------
# show
# --------------------------------------------------------------------------

def test_show_renders_snapshot_json(db, capsys):
    rc = _run(db, "show", "file:secret", "--json")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["snapshot"]["guards"] == ["local-only"]
    assert out["snapshot"]["sensitivity"] == "confidential"
    assert out["snapshot"]["declassification"] == "none"


def test_show_unknown_entity_returns_2(db, capsys):
    assert _run(db, "show", "file:nope") == 2


# --------------------------------------------------------------------------
# verdict
# --------------------------------------------------------------------------

def test_verdict_local_only_remote_upload_denies_nonzero_exit(db, capsys):
    rc = _run(db, "verdict", "file:secret", "--chokepoint", "browser-upload", "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "deny"
    assert out["allowed"] is False
    assert rc == 1  # non-zero so VM/scripts can assert fail-closed


def test_verdict_local_clipboard_allows(db, capsys):
    rc = _run(db, "verdict", "file:secret", "--chokepoint", "clipboard", "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "allow"
    assert rc == 0


def test_verdict_deny_prints_declassify_call_to_action(db, capsys):
    _run(db, "verdict", "file:secret", "--chokepoint", "browser-upload")
    text = capsys.readouterr().out
    assert "declassify" in text and "workflow" in text


# --------------------------------------------------------------------------
# impact
# --------------------------------------------------------------------------

def test_impact_lists_guarded_descendants(db, capsys):
    s = LineageStore(db)
    s.record_entity(Entity(eid="file:deriv", kind="artifact",
                           guards=frozenset({"local-only"})))
    s.record_edge("wasDerivedFrom", "file:deriv", "file:secret")
    s.close()
    rc = _run(db, "impact", "file:secret", "--json")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["guarded_descendants"]["file:deriv"] == ["local-only"]


# --------------------------------------------------------------------------
# request-declassify — STATE only, never narrows / never bypasses authority
# These run as the admin uid (default authority); the non-admin-uid denial is
# covered by test_cli_request_declassify_non_admin_uid_denied below.
# --------------------------------------------------------------------------

@pytest.fixture
def admin_uid(monkeypatch):
    monkeypatch.setattr(cli.os, "getuid", lambda: 1000, raising=False)


def test_request_declassify_advances_state_only(db, admin_uid, capsys):
    rc = _run(db, "request-declassify", "file:secret", "--json")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["allowed"] is True and out["change_class"] == "state"
    # guards are untouched by a state request
    s = LineageStore(db)
    try:
        ent = s.get_entity("file:secret")
        assert ent.guards == frozenset({"local-only"})
        assert ent.facets["declassification"] == "requested"
    finally:
        s.close()


def test_request_approved_without_authority_denied(db, admin_uid, capsys):
    # admin uid passes the delegation gate, but 'approved' still needs --authority
    _run(db, "request-declassify", "file:secret")
    capsys.readouterr()
    rc = _run(db, "request-declassify", "file:secret", "--state", "approved", "--json")
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["allowed"] is False


def test_request_approved_with_authority_allowed(db, admin_uid, capsys):
    _run(db, "request-declassify", "file:secret")
    capsys.readouterr()
    rc = _run(db, "request-declassify", "file:secret", "--state", "approved",
              "--authority", "agent:admin", "--json")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["allowed"] is True


# --------------------------------------------------------------------------
# grant / grants — delegation surface
#
# The granter is authenticated from the REAL OS uid; there is no --granted-by
# flag. To exercise the admin path we monkeypatch _authenticated_granter to the
# admin principal (= running as uid 1000). The default test uid is NOT admin, so
# unpatched grant calls take the unprivileged path — which is the escalation
# guard we want to assert.
# --------------------------------------------------------------------------

@pytest.fixture
def as_admin(monkeypatch):
    """Run the CLI as if invoked by the admin uid (granter == admin principal)."""
    import qdistro_security_grants as g
    monkeypatch.setattr(cli, "_authenticated_granter",
                        lambda gs: g.ADMIN_APP_PRINCIPAL)


def test_grant_then_grants_shows_scope(db, as_admin, capsys):
    rc = _run(db, "grant", "wf:export", "sensitivity", "--json")
    assert rc == 0
    capsys.readouterr()
    rc = _run(db, "grants", "wf:export", "--json")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["effective_scopes"] == ["security.sensitivity"]


def test_grant_revoke_nets_to_empty(db, as_admin, capsys):
    _run(db, "grant", "wf:export", "guards")
    _run(db, "grant", "wf:export", "guards", "--revoke")
    capsys.readouterr()
    rc = _run(db, "grants", "wf:export", "--json")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["effective_scopes"] == []


def test_grant_unknown_scope_refused(db, as_admin, capsys):
    rc = _run(db, "grant", "wf:export", "everything")
    assert rc == 2


def test_grants_admin_app_shows_default_all(db, capsys):
    rc = _run(db, "grants", "agent:admin", "--json")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["is_admin_app"] is True


def test_cli_non_admin_uid_cannot_self_assert_admin(db, monkeypatch, capsys):
    """The real fix for the escalation: a non-admin OS uid holds nothing and there
    is NO flag to claim the admin identity, so it cannot mint any grant."""
    # Force a non-admin uid; _authenticated_granter -> uid:<n> who holds nothing.
    monkeypatch.setattr(cli.os, "getuid", lambda: 4242, raising=False)
    rc = _run(db, "grant", "wf:victim", "*")
    assert rc == 2
    capsys.readouterr()
    rc = _run(db, "grants", "wf:victim", "--json")
    out = json.loads(capsys.readouterr().out)
    assert out["effective_scopes"] == []


def test_cli_holder_can_delegate_what_it_holds(db, monkeypatch, capsys):
    import qdistro_security_grants as g
    # admin seeds wf:lead with guards
    monkeypatch.setattr(cli, "_authenticated_granter", lambda gs: g.ADMIN_APP_PRINCIPAL)
    _run(db, "grant", "wf:lead", "guards")
    capsys.readouterr()
    # now run as wf:lead (a non-admin holder of guards): may re-delegate guards
    monkeypatch.setattr(cli, "_authenticated_granter", lambda gs: "wf:lead")
    rc = _run(db, "grant", "wf:helper", "guards", "--json")
    assert rc == 0
    capsys.readouterr()
    # but not sensitivity it doesn't hold
    rc = _run(db, "grant", "wf:helper", "sensitivity")
    assert rc == 2


def test_cli_request_declassify_non_admin_uid_denied(db, monkeypatch, capsys):
    # a non-admin OS uid with no grant cannot even advance declassification STATE
    monkeypatch.setattr(cli.os, "getuid", lambda: 4242, raising=False)
    rc = _run(db, "request-declassify", "file:secret", "--json")
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["allowed"] is False and "delegation denied" in out["reason"]


def test_cli_request_declassify_as_admin_uid_allowed(db, monkeypatch, capsys):
    # admin uid -> admin principal -> default authority advances STATE
    monkeypatch.setattr(cli.os, "getuid", lambda: 1000, raising=False)
    rc = _run(db, "request-declassify", "file:secret", "--json")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["allowed"] is True and out["change_class"] == "state"
