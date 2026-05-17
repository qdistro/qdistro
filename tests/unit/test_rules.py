"""Tests for qdistro_admin_rules.RulesEngine + Rule.

Covers YAML parsing, validation (bad schemas don't crash the broker),
matching, precedence (first-match), and rule listing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from qdistro_admin_rules import Rule, RulesEngine


# --- Rule.matches ---------------------------------------------------------

class TestRuleMatches:
    def test_all_selectors_match(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 uid=2000, action="a", exe="/p")
        assert r.matches(uid=2000, action="a", exe="/p")

    def test_wildcard_uid_matches_any(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 action="a")
        assert r.matches(uid=2000, action="a", exe="/x")
        assert r.matches(uid=3000, action="a", exe="/y")

    def test_mismatched_uid_rejects(self):
        r = Rule(name="x", decision="allow", source_path="/p", uid=2000)
        assert not r.matches(uid=2001, action="a", exe="/p")

    def test_empty_rule_is_wildcard_all(self):
        r = Rule(name="x", decision="deny", source_path="/p")
        assert r.matches(uid=0, action="x", exe="/anything")

    # qdwin §6.10 / qdwin_shell_v1@v13 — secctx selectors.

    def test_app_id_match(self):
        r = Rule(name="x", decision="deny", source_path="/p",
                 app_id="qdistro.tier3.user1")
        assert r.matches(uid=0, action="a", exe="/x",
                         app_id="qdistro.tier3.user1")
        assert not r.matches(uid=0, action="a", exe="/x",
                             app_id="qdistro.tier3.user2")

    def test_app_id_rule_rejects_when_caller_has_none(self):
        # Selector presence implies "must equal" — unsandboxed callers
        # (app_id="") don't match a rule that names a non-empty app_id.
        r = Rule(name="x", decision="deny", source_path="/p",
                 app_id="qdistro.tier3.user1")
        assert not r.matches(uid=0, action="a", exe="/x")
        assert not r.matches(uid=0, action="a", exe="/x", app_id="")

    def test_app_id_empty_string_selector_matches_only_unsandboxed(self):
        # `app_id: ""` is rare but explicit: only callers without a
        # secctx tag match. Useful for "deny anything *not* sandboxed".
        r = Rule(name="x", decision="deny", source_path="/p", app_id="")
        assert r.matches(uid=0, action="a", exe="/x")
        assert not r.matches(uid=0, action="a", exe="/x",
                             app_id="qdistro.tier3.user1")

    def test_sandbox_engine_match(self):
        r = Rule(name="x", decision="deny", source_path="/p",
                 sandbox_engine="qdistro.tier3")
        assert r.matches(uid=0, action="a", exe="/x",
                         sandbox_engine="qdistro.tier3")
        assert not r.matches(uid=0, action="a", exe="/x",
                             sandbox_engine="qdistro.tier2")

    def test_combined_action_and_app_id(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 action="qdistro.clipboard.transfer:user1:admin",
                 app_id="qdistro.tier3.user1")
        assert r.matches(uid=1000,
                         action="qdistro.clipboard.transfer:user1:admin",
                         exe="/usr/bin/qdshell",
                         app_id="qdistro.tier3.user1")
        # action match + app_id mismatch → no.
        assert not r.matches(
            uid=1000,
            action="qdistro.clipboard.transfer:user1:admin",
            exe="/usr/bin/qdshell",
            app_id="qdistro.tier3.attacker")
        # app_id match + action mismatch → no.
        assert not r.matches(
            uid=1000,
            action="qdistro.clipboard.transfer:user2:admin",
            exe="/usr/bin/qdshell",
            app_id="qdistro.tier3.user1")

    def test_legacy_rule_without_secctx_selectors_matches_secctx_caller(self):
        # Backwards compat: a pre-v13 rule (no app_id selector) keeps
        # matching even when the caller now propagates app_id.
        r = Rule(name="x", decision="allow", source_path="/p",
                 action="a")
        assert r.matches(uid=0, action="a", exe="/x")
        assert r.matches(uid=0, action="a", exe="/x",
                         app_id="qdistro.tier3.user1",
                         sandbox_engine="qdistro.tier3")

    # qdwin_shell_v1@v15 — mime_type selector (receive-only).

    def test_mime_type_match(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 mime_type="text/plain")
        assert r.matches(uid=0, action="a", exe="/x",
                         mime_type="text/plain")
        assert not r.matches(uid=0, action="a", exe="/x",
                             mime_type="image/png")

    def test_mime_type_rule_rejects_when_caller_has_none(self):
        # Selector presence implies "must equal" — a non-receive
        # caller (transfer, handoff) carries empty mime and must not
        # match a rule that names a non-empty mime_type.
        r = Rule(name="x", decision="allow", source_path="/p",
                 mime_type="text/plain")
        assert not r.matches(uid=0, action="a", exe="/x")
        assert not r.matches(uid=0, action="a", exe="/x", mime_type="")

    def test_mime_type_with_action_combines(self):
        r = Rule(name="x", decision="deny", source_path="/p",
                 action="qdistro.clipboard.receive:user1:admin",
                 mime_type="image/png")
        assert r.matches(uid=0,
                         action="qdistro.clipboard.receive:user1:admin",
                         exe="/x",
                         mime_type="image/png")
        # Same action, different mime → no match.
        assert not r.matches(
            uid=0,
            action="qdistro.clipboard.receive:user1:admin",
            exe="/x",
            mime_type="text/plain")

    def test_legacy_rule_matches_receive_caller_carrying_mime(self):
        # Backwards compat: a pre-v15 rule that doesn't name mime_type
        # still matches a v15 receive call (which now propagates mime).
        r = Rule(name="x", decision="allow", source_path="/p",
                 action="qdistro.clipboard.receive:user1:admin")
        assert r.matches(
            uid=0,
            action="qdistro.clipboard.receive:user1:admin",
            exe="/x",
            mime_type="text/plain")

    # task(052) — mime_type fnmatch glob (auto-detect on `*`).

    def test_mime_type_glob_text_star(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 mime_type="text/*")
        assert r.matches(uid=0, action="a", exe="/x",
                         mime_type="text/plain")
        assert r.matches(uid=0, action="a", exe="/x",
                         mime_type="text/uri-list")
        assert r.matches(uid=0, action="a", exe="/x",
                         mime_type="text/html")
        # Glob doesn't bleed into image/*.
        assert not r.matches(uid=0, action="a", exe="/x",
                             mime_type="image/png")

    def test_mime_type_glob_image_star(self):
        r = Rule(name="x", decision="deny", source_path="/p",
                 mime_type="image/*")
        assert r.matches(uid=0, action="a", exe="/x",
                         mime_type="image/png")
        assert r.matches(uid=0, action="a", exe="/x",
                         mime_type="image/jpeg")
        assert not r.matches(uid=0, action="a", exe="/x",
                             mime_type="text/plain")

    def test_mime_type_glob_subtype_suffix(self):
        # Globs aren't just prefix — fnmatchcase supports trailing `*`
        # too. */json matches any subtype ending in "json".
        r = Rule(name="x", decision="allow", source_path="/p",
                 mime_type="*/json")
        assert r.matches(uid=0, action="a", exe="/x",
                         mime_type="application/json")
        # Note: fnmatch's `*` matches `/` too, so this also matches
        # a `text/foo/json`-style synthetic. That's fine — IANA mimes
        # only have one `/`, so the practical effect is "subtype is
        # exactly json across all top-level types".
        assert not r.matches(uid=0, action="a", exe="/x",
                             mime_type="application/xml")

    def test_mime_type_exact_path_unchanged_by_glob_logic(self):
        # No `*` in the rule value → exact-eq path stays intact.
        # Pinning this so a future refactor doesn't accidentally
        # turn `text/plain` into a literal-glob match for itself
        # only (which is fine but slow).
        r = Rule(name="x", decision="allow", source_path="/p",
                 mime_type="text/plain")
        assert r.matches(uid=0, action="a", exe="/x",
                         mime_type="text/plain")
        assert not r.matches(uid=0, action="a", exe="/x",
                             mime_type="text/plainfoo")

    def test_mime_type_glob_does_not_match_empty(self):
        # A glob-typed rule still requires the caller to pass a
        # non-empty mime — same selector-presence-implies-must-match
        # rule as the exact path.
        r = Rule(name="x", decision="allow", source_path="/p",
                 mime_type="text/*")
        assert not r.matches(uid=0, action="a", exe="/x")
        assert not r.matches(uid=0, action="a", exe="/x",
                             mime_type="")

    # task(057) — fnmatch globs auto-applied to all string selectors
    # (action, exe, app_id, sandbox_engine), same auto-detect rule.

    def test_action_glob_prefix(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 action="qdistro.clipboard.*")
        assert r.matches(uid=0, action="qdistro.clipboard.transfer:user1:admin",
                         exe="/x")
        assert r.matches(uid=0, action="qdistro.clipboard.receive:user1:admin",
                         exe="/x")
        assert not r.matches(uid=0, action="qdistro.handoff:user1:admin",
                             exe="/x")

    def test_action_glob_silo_pair(self):
        r = Rule(name="x", decision="deny", source_path="/p",
                 action="qdistro.clipboard.*:user1:*")
        assert r.matches(uid=0,
                         action="qdistro.clipboard.transfer:user1:admin",
                         exe="/x")
        assert r.matches(uid=0,
                         action="qdistro.clipboard.receive:user1:user2",
                         exe="/x")
        assert not r.matches(uid=0,
                             action="qdistro.clipboard.transfer:user2:admin",
                             exe="/x")

    def test_action_exact_unchanged(self):
        # No '*' → exact-eq, identical to pre-task-057 behaviour.
        r = Rule(name="x", decision="allow", source_path="/p",
                 action="qdistro.clipboard.transfer:user1:admin")
        assert r.matches(uid=0,
                         action="qdistro.clipboard.transfer:user1:admin",
                         exe="/x")
        assert not r.matches(uid=0,
                             action="qdistro.clipboard.transfer:user2:admin",
                             exe="/x")

    def test_exe_glob_python_versions(self):
        # `/usr/bin/python3*` matches python3, python3.13, python3-foo.
        r = Rule(name="x", decision="allow", source_path="/p",
                 exe="/usr/bin/python3*")
        assert r.matches(uid=0, action="a", exe="/usr/bin/python3")
        assert r.matches(uid=0, action="a", exe="/usr/bin/python3.13")
        assert r.matches(uid=0, action="a", exe="/usr/bin/python3-foo")
        assert not r.matches(uid=0, action="a", exe="/usr/bin/python")

    def test_exe_glob_directory_wildcard(self):
        # `/opt/*/bin/foo` matches /opt/anywhere/bin/foo.
        r = Rule(name="x", decision="deny", source_path="/p",
                 exe="/opt/*/bin/foo")
        assert r.matches(uid=0, action="a", exe="/opt/myapp/bin/foo")
        assert r.matches(uid=0, action="a", exe="/opt/v2/bin/foo")
        assert not r.matches(uid=0, action="a", exe="/opt/myapp/lib/foo")

    def test_app_id_glob_reverse_dns(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 app_id="org.example.*")
        assert r.matches(uid=0, action="a", exe="/x",
                         app_id="org.example.viewer")
        assert r.matches(uid=0, action="a", exe="/x",
                         app_id="org.example.editor.toolkit")
        assert not r.matches(uid=0, action="a", exe="/x",
                             app_id="com.example.viewer")

    def test_app_id_glob_with_dot_does_not_match_empty(self):
        # A meaningful glob like `org.example.*` requires the caller
        # to carry a value with that prefix; the empty default that
        # unsandboxed callers send never matches. Mirrors the
        # `mime_type=text/*` behaviour. (A bare `*` glob WOULD match
        # empty because fnmatch's `*` accepts the empty string —
        # admins authoring that mean "any value, including empty",
        # which is a deliberate semantic.)
        r = Rule(name="x", decision="allow", source_path="/p",
                 app_id="org.example.*")
        assert not r.matches(uid=0, action="a", exe="/x")
        assert not r.matches(uid=0, action="a", exe="/x", app_id="")
        assert r.matches(uid=0, action="a", exe="/x",
                         app_id="org.example.app")

    def test_sandbox_engine_glob(self):
        r = Rule(name="x", decision="deny", source_path="/p",
                 sandbox_engine="podman*")
        assert r.matches(uid=0, action="a", exe="/x",
                         sandbox_engine="podman")
        assert r.matches(uid=0, action="a", exe="/x",
                         sandbox_engine="podman-rootless")
        assert not r.matches(uid=0, action="a", exe="/x",
                             sandbox_engine="waypipe")


# --- YAML loading ---------------------------------------------------------

class TestLoad:
    def test_missing_directory_is_silent(self, tmp_path):
        eng = RulesEngine(str(tmp_path / "no-such-dir"))
        assert eng.rules() == []
        assert eng.load_errors() == []

    def test_empty_directory_loads_zero_rules(self, tmp_path):
        eng = RulesEngine(str(tmp_path))
        assert eng.rules() == []

    def test_single_file_single_rule(self, tmp_path):
        (tmp_path / "a.yaml").write_text("""
- name: dev allow
  decision: allow
  match:
    uid: 2000
    action: test.action
  scope: 1h
""")
        eng = RulesEngine(str(tmp_path))
        rules = eng.rules()
        assert len(rules) == 1
        assert rules[0].decision == "allow"
        assert rules[0].uid == 2000
        assert rules[0].action == "test.action"
        assert rules[0].scope == "1h"
        assert rules[0].source_path.endswith("a.yaml")

    def test_precedence_is_sorted_filename_then_list_order(self, tmp_path):
        """Files load alphabetically; within a file, list order."""
        (tmp_path / "20-deny.yaml").write_text("""
- name: deny-b
  decision: deny
  match: {action: b}
""")
        (tmp_path / "10-allow.yaml").write_text("""
- name: allow-a
  decision: allow
  match: {action: a}
- name: allow-b
  decision: allow
  match: {action: b}
""")
        eng = RulesEngine(str(tmp_path))
        names = [r.name for r in eng.rules()]
        assert names == ["allow-a", "allow-b", "deny-b"]
        # allow-b beats deny-b because 10-allow.yaml sorts before 20-deny.yaml.
        m = eng.match(uid=0, action="b", exe="")
        assert m is not None and m.decision == "allow"

    def test_match_finds_first(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- {name: specific, decision: deny, match: {uid: 2000, action: x}}
- {name: broad,    decision: allow, match: {action: x}}
""")
        eng = RulesEngine(str(tmp_path))
        m = eng.match(uid=2000, action="x", exe="/p")
        assert m.name == "specific"
        m2 = eng.match(uid=3000, action="x", exe="/p")
        assert m2.name == "broad"

    def test_no_match_returns_none(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- {name: only, decision: allow, match: {action: x}}
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.match(uid=0, action="y", exe="") is None

    def test_yml_extension_also_loaded(self, tmp_path):
        (tmp_path / "r.yml").write_text("""
- {name: yml, decision: allow, match: {action: z}}
""")
        eng = RulesEngine(str(tmp_path))
        assert len(eng.rules()) == 1

    def test_non_yaml_files_ignored(self, tmp_path):
        (tmp_path / "r.txt").write_text("""
- {name: x, decision: allow}
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.rules() == []

    # secctx selectors round-trip through YAML.

    def test_yaml_app_id_selector_loads(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: tier3-user1-clip
  decision: allow
  match:
    action: qdistro.clipboard.transfer:user1:admin
    app_id: qdistro.tier3.user1
  rationale: dev silo can paste into admin terminal
""")
        eng = RulesEngine(str(tmp_path))
        rules = eng.rules()
        assert len(rules) == 1
        assert rules[0].app_id == "qdistro.tier3.user1"
        assert rules[0].sandbox_engine is None

    def test_yaml_sandbox_engine_selector_loads(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: block-tier2-out
  decision: deny
  match:
    sandbox_engine: qdistro.tier2
""")
        eng = RulesEngine(str(tmp_path))
        rules = eng.rules()
        assert len(rules) == 1
        assert rules[0].sandbox_engine == "qdistro.tier2"

    def test_yaml_match_finds_app_id(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: tier3-user1-out
  decision: allow
  match: {action: qdistro.handoff.activate:user1:admin, app_id: qdistro.tier3.user1}
""")
        eng = RulesEngine(str(tmp_path))
        m = eng.match(uid=1000,
                      action="qdistro.handoff.activate:user1:admin",
                      exe="/usr/bin/qdshell",
                      app_id="qdistro.tier3.user1")
        assert m is not None and m.decision == "allow"
        # Different app_id → no match (default-deny path applies).
        assert eng.match(uid=1000,
                        action="qdistro.handoff.activate:user1:admin",
                        exe="/usr/bin/qdshell",
                        app_id="qdistro.tier3.user2") is None

    def test_yaml_app_id_only_rule_does_not_match_unsandboxed(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: tier3-out
  decision: allow
  match: {app_id: qdistro.tier3.user1}
""")
        eng = RulesEngine(str(tmp_path))
        # Caller has no app_id (legacy non-secctx call).
        assert eng.match(uid=1000, action="a", exe="/x") is None
        # Caller has the matching app_id.
        m = eng.match(uid=1000, action="a", exe="/x",
                      app_id="qdistro.tier3.user1")
        assert m is not None and m.decision == "allow"

    # qdwin_shell_v1@v15 — mime_type selector round-trips.

    def test_yaml_mime_type_selector_loads(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: receive-text-only
  decision: allow
  match:
    action: qdistro.clipboard.receive:user1:admin
    mime_type: text/plain
  rationale: dev silo can paste plain text into admin terminal
""")
        eng = RulesEngine(str(tmp_path))
        rules = eng.rules()
        assert len(rules) == 1
        assert rules[0].mime_type == "text/plain"
        assert rules[0].app_id is None

    def test_yaml_per_mime_pair_authoring(self, tmp_path):
        # Canonical admin authoring use case: same silo pair, allow
        # text/plain and deny image/png. First-match precedence applies.
        (tmp_path / "r.yaml").write_text("""
- name: text-allow
  decision: allow
  match:
    action: qdistro.clipboard.receive:user1:admin
    mime_type: text/plain
- name: image-deny
  decision: deny
  match:
    action: qdistro.clipboard.receive:user1:admin
    mime_type: image/png
""")
        eng = RulesEngine(str(tmp_path))
        m_text = eng.match(uid=1000,
                           action="qdistro.clipboard.receive:user1:admin",
                           exe="/x", mime_type="text/plain")
        assert m_text is not None and m_text.decision == "allow"
        m_img = eng.match(uid=1000,
                          action="qdistro.clipboard.receive:user1:admin",
                          exe="/x", mime_type="image/png")
        assert m_img is not None and m_img.decision == "deny"
        # Mime not named by any rule → falls through to default (None).
        m_other = eng.match(uid=1000,
                            action="qdistro.clipboard.receive:user1:admin",
                            exe="/x", mime_type="text/uri-list")
        assert m_other is None

    def test_yaml_mime_type_rule_does_not_match_transfer(self, tmp_path):
        # A receive-authored mime_type rule must not bleed into
        # CheckClipboardTransfer (which carries no single mime).
        (tmp_path / "r.yaml").write_text("""
- name: receive-only
  decision: allow
  match:
    mime_type: text/plain
""")
        eng = RulesEngine(str(tmp_path))
        # Transfer-style call: mime_type omitted (default "").
        assert eng.match(uid=1000,
                        action="qdistro.clipboard.transfer:user1:admin",
                        exe="/x") is None

    def test_yaml_mime_type_glob_loads(self, tmp_path):
        # task(052) — fnmatch glob round-trips through YAML.
        (tmp_path / "r.yaml").write_text("""
- name: text-allow-all
  decision: allow
  match:
    action: qdistro.clipboard.receive:user1:admin
    mime_type: text/*
- name: image-deny-all
  decision: deny
  match:
    action: qdistro.clipboard.receive:user1:admin
    mime_type: image/*
""")
        eng = RulesEngine(str(tmp_path))
        # Two narrow rules + glob means "text allowed, image denied,
        # everything else falls through to default (no rule)."
        assert eng.match(uid=1000,
                        action="qdistro.clipboard.receive:user1:admin",
                        exe="/x", mime_type="text/plain").decision == "allow"
        assert eng.match(uid=1000,
                        action="qdistro.clipboard.receive:user1:admin",
                        exe="/x", mime_type="text/html").decision == "allow"
        assert eng.match(uid=1000,
                        action="qdistro.clipboard.receive:user1:admin",
                        exe="/x", mime_type="image/png").decision == "deny"
        assert eng.match(uid=1000,
                        action="qdistro.clipboard.receive:user1:admin",
                        exe="/x", mime_type="application/pdf") is None


# --- Validation / error handling -----------------------------------------

class TestValidation:
    def _load(self, tmp_path, body: str) -> RulesEngine:
        (tmp_path / "r.yaml").write_text(body)
        return RulesEngine(str(tmp_path))

    def test_missing_decision_is_error_not_crash(self, tmp_path):
        eng = self._load(tmp_path, "- {name: x, match: {action: a}}\n")
        assert eng.rules() == []
        assert any("decision" in e for e in eng.load_errors())

    def test_invalid_decision_is_error(self, tmp_path):
        eng = self._load(tmp_path, "- {name: x, decision: maybe}\n")
        assert eng.rules() == []
        assert any("decision" in e for e in eng.load_errors())

    def test_invalid_scope_is_error(self, tmp_path):
        eng = self._load(tmp_path, "- {name: x, decision: allow, scope: 5min}\n")
        assert eng.rules() == []
        assert any("scope" in e for e in eng.load_errors())

    def test_unknown_top_level_key_is_error(self, tmp_path):
        eng = self._load(tmp_path, "- {name: x, decision: allow, color: blue}\n")
        assert eng.rules() == []
        assert any("color" in e for e in eng.load_errors())

    def test_unknown_match_key_is_error(self, tmp_path):
        eng = self._load(tmp_path, "- {name: x, decision: allow, match: {pid: 42}}\n")
        assert eng.rules() == []
        assert any("pid" in e for e in eng.load_errors())

    def test_mime_type_must_be_string(self, tmp_path):
        eng = self._load(tmp_path,
            "- {name: x, decision: allow, match: {mime_type: 42}}\n")
        assert eng.rules() == []
        assert any("mime_type" in e for e in eng.load_errors())

    def test_bad_types_are_errors(self, tmp_path):
        eng = self._load(tmp_path, "- {name: x, decision: allow, match: {uid: '2000'}}\n")
        assert eng.rules() == []

    def test_top_level_must_be_list(self, tmp_path):
        eng = self._load(tmp_path, "name: not-a-list\n")
        assert eng.rules() == []
        assert any("list" in e for e in eng.load_errors())

    def test_malformed_yaml_doesnt_kill_sibling_files(self, tmp_path):
        (tmp_path / "broken.yaml").write_text(": : :\n")
        (tmp_path / "good.yaml").write_text(
            "- {name: keep, decision: allow, match: {action: a}}\n")
        eng = RulesEngine(str(tmp_path))
        names = [r.name for r in eng.rules()]
        assert names == ["keep"]
        assert any("broken.yaml" in e for e in eng.load_errors())

    def test_reload_replaces_not_appends(self, tmp_path):
        f = tmp_path / "r.yaml"
        f.write_text("- {name: v1, decision: allow, match: {action: a}}\n")
        eng = RulesEngine(str(tmp_path))
        assert [r.name for r in eng.rules()] == ["v1"]
        f.write_text("- {name: v2, decision: deny, match: {action: a}}\n")
        eng.reload()
        assert [r.name for r in eng.rules()] == ["v2"]


# --- argv match-kinds (qsu / spec/21) ------------------------------------

class TestArgvMatchKinds:
    """task(061) — argv_exact / argv_basename / argv_prefix selectors."""

    # In-memory Rule construction (no YAML).

    def test_argv_exact_match(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 argv_exact=("/usr/bin/apt-get", "update"))
        assert r.matches(uid=0, action="a", exe="/x",
                         argv=["/usr/bin/apt-get", "update"])
        assert not r.matches(uid=0, action="a", exe="/x",
                             argv=["/usr/bin/apt-get", "upgrade"])
        # Length mismatch never matches.
        assert not r.matches(uid=0, action="a", exe="/x",
                             argv=["/usr/bin/apt-get", "update", "-y"])
        # Missing argv on caller → no match.
        assert not r.matches(uid=0, action="a", exe="/x")

    def test_argv_basename_eq(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 argv_basename="python3")
        assert r.matches(uid=0, action="a", exe="/x",
                         argv=["/usr/bin/python3", "script.py"])
        assert r.matches(uid=0, action="a", exe="/x",
                         argv=["python3", "script.py"])
        assert not r.matches(uid=0, action="a", exe="/x",
                             argv=["/usr/bin/python", "script.py"])

    def test_argv_basename_glob(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 argv_basename="python3*")
        assert r.matches(uid=0, action="a", exe="/x",
                         argv=["/usr/bin/python3"])
        assert r.matches(uid=0, action="a", exe="/x",
                         argv=["/usr/bin/python3.13"])
        assert not r.matches(uid=0, action="a", exe="/x",
                             argv=["/usr/bin/python2.7"])

    def test_argv_prefix_match(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 argv_prefix=("/usr/bin/systemctl", "restart"))
        assert r.matches(uid=0, action="a", exe="/x",
                         argv=["/usr/bin/systemctl", "restart", "nginx"])
        assert r.matches(uid=0, action="a", exe="/x",
                         argv=["/usr/bin/systemctl", "restart", "nginx", "--now"])
        # Just the prefix exactly is fine — len(argv) == len(prefix).
        assert r.matches(uid=0, action="a", exe="/x",
                         argv=["/usr/bin/systemctl", "restart"])
        # Mismatch in any prefix element → no.
        assert not r.matches(uid=0, action="a", exe="/x",
                             argv=["/usr/bin/systemctl", "stop", "nginx"])
        # Argv shorter than prefix → no.
        assert not r.matches(uid=0, action="a", exe="/x",
                             argv=["/usr/bin/systemctl"])

    def test_argv_selector_implies_argv_must_be_present(self):
        # Same selector-presence semantics as app_id: a clipboard
        # CheckClipboardTransfer call (no argv in details) must not
        # accidentally match a qsu-authored argv rule.
        r = Rule(name="x", decision="allow", source_path="/p",
                 argv_basename="python3")
        assert not r.matches(uid=0, action="a", exe="/x")
        assert not r.matches(uid=0, action="a", exe="/x", argv=None)
        assert not r.matches(uid=0, action="a", exe="/x", argv=[])

    def test_argv_combined_with_action_and_uid(self):
        r = Rule(name="x", decision="allow", source_path="/p",
                 uid=1001, action="qsu.exec:root",
                 argv_prefix=("/usr/bin/systemctl",))
        assert r.matches(uid=1001, action="qsu.exec:root", exe="/qsu",
                         argv=["/usr/bin/systemctl", "restart", "nginx"])
        # uid mismatch → no.
        assert not r.matches(uid=1002, action="qsu.exec:root", exe="/qsu",
                             argv=["/usr/bin/systemctl", "restart", "nginx"])
        # action mismatch → no.
        assert not r.matches(uid=1001, action="qsu.exec:user", exe="/qsu",
                             argv=["/usr/bin/systemctl", "restart", "nginx"])

    # YAML load round-trip.

    def test_yaml_argv_exact_loads(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: dev-apt-update
  decision: allow
  match:
    uid: 1001
    action: qsu.exec:root
    argv_exact:
      - /usr/bin/apt-get
      - update
  scope: forever_exe
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.load_errors() == []
        rules = eng.rules()
        assert len(rules) == 1
        assert rules[0].argv_exact == ("/usr/bin/apt-get", "update")
        assert rules[0].argv_basename is None
        assert rules[0].argv_prefix is None

    def test_yaml_argv_basename_loads(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: any-python3-as-root
  decision: allow
  match:
    uid: 1000
    action: qsu.exec:root
    argv_basename: python3
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.load_errors() == []
        assert eng.rules()[0].argv_basename == "python3"

    def test_yaml_argv_prefix_loads(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: admin-systemctl-restart
  decision: allow
  match:
    uid: 1000
    action: qsu.exec:root
    argv_prefix:
      - /usr/bin/systemctl
      - restart
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.load_errors() == []
        assert eng.rules()[0].argv_prefix == ("/usr/bin/systemctl", "restart")

    def test_yaml_argv_match_via_engine(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: dev-apt-update
  decision: allow
  match:
    uid: 1001
    action: qsu.exec:root
    argv_exact: [/usr/bin/apt-get, update]
- name: admin-systemctl-restart-anything
  decision: allow
  match:
    uid: 1000
    action: qsu.exec:root
    argv_prefix: [/usr/bin/systemctl, restart]
""")
        eng = RulesEngine(str(tmp_path))
        m = eng.match(uid=1001, action="qsu.exec:root", exe="/q",
                      argv=["/usr/bin/apt-get", "update"])
        assert m is not None and m.name == "dev-apt-update"
        m2 = eng.match(uid=1000, action="qsu.exec:root", exe="/q",
                       argv=["/usr/bin/systemctl", "restart", "nginx"])
        assert m2 is not None and m2.name == "admin-systemctl-restart-anything"
        # No argv on the request → argv-selector rules don't match.
        assert eng.match(uid=1001, action="qsu.exec:root", exe="/q") is None

    # Validation.

    def test_yaml_argv_exact_must_be_list_of_strings(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: bad
  decision: allow
  match:
    argv_exact: [/usr/bin/x, 42]
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.rules() == []
        assert any("argv_exact" in e for e in eng.load_errors())

    def test_yaml_argv_exact_must_be_non_empty(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: bad
  decision: allow
  match:
    argv_exact: []
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.rules() == []
        assert any("argv_exact" in e for e in eng.load_errors())

    def test_yaml_argv_basename_with_slash_rejected(self, tmp_path):
        # Authoring trap — argv_basename is matched against
        # basename(argv[0]); a path-shaped value never matches.
        (tmp_path / "r.yaml").write_text("""
- name: bad
  decision: allow
  match:
    argv_basename: /usr/bin/python3
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.rules() == []
        assert any("argv_basename" in e for e in eng.load_errors())

    def test_yaml_multiple_argv_kinds_rejected(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: bad
  decision: allow
  match:
    argv_basename: python3
    argv_prefix: [/usr/bin/python3]
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.rules() == []
        assert any("argv" in e for e in eng.load_errors())

    def test_yaml_argv_prefix_must_be_list(self, tmp_path):
        (tmp_path / "r.yaml").write_text("""
- name: bad
  decision: allow
  match:
    argv_prefix: /usr/bin/systemctl
""")
        eng = RulesEngine(str(tmp_path))
        assert eng.rules() == []
        assert any("argv_prefix" in e for e in eng.load_errors())
