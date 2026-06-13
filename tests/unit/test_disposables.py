"""Tests for the pure disposable-silo helpers + the session-manager reaper
sweep (07-disposables-plan P1)."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SM_DIR = REPO_ROOT / "session_manager"
sys.path.insert(0, str(SM_DIR))


def _load(label: str, path: Path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


disp = _load("qdistro_disposables", SM_DIR / "qdistro_disposables.py")


# ---- naming ----------------------------------------------------------

def test_disposable_name_shape():
    n = disp.disposable_name("pdf", "20260612-151828")
    assert n == "disp-pdf-20260612-151828"
    assert disp.is_disposable_container(n)
    assert disp.parse_disposable_name(n) == ("pdf", "20260612-151828")


def test_disposable_name_collision_suffix():
    n = disp.disposable_name("pdf", "20260612-151828", suffix="ab12")
    assert n == "disp-pdf-20260612-151828-ab12"
    assert disp.is_disposable_container(n)


@pytest.mark.parametrize("bad", ["Bad", "has_underscore", "white space",
                                 "../x", "", "a/b", "x" * 64])
def test_validate_workload_rejects(bad):
    with pytest.raises(disp.DisposableError):
        disp.validate_workload(bad)


def test_disposable_name_rejects_bad_timestamp():
    with pytest.raises(disp.DisposableError):
        disp.disposable_name("pdf", "not-a-ts")


# ---- secctx app_id ---------------------------------------------------

def test_secctx_appid():
    token = "f8e14f7cb8d479f9f1f2de4fd5c98f2a"
    assert disp.disposable_secctx_appid(token) == f"qdistro.disp.{token}"
    assert disp.is_disposable_appid(f"qdistro.disp.{token}")
    assert not disp.is_disposable_appid("qdistro.tier2")
    assert not disp.is_disposable_appid("qdistro.disp.NOTHEX")


def test_secctx_appid_rejects_bad_token():
    with pytest.raises(disp.DisposableError):
        disp.disposable_secctx_appid("xyz")  # not hex / too short


def test_is_disposable_token():
    # Accepts a well-formed per-spawn launch token (8..64 lowercase hex).
    assert disp.is_disposable_token("0123456789abcdef0123456789abcdef")
    assert disp.is_disposable_token("deadbeef")
    # Rejects: uppercase, too short, non-hex, oversized, injection-ish, non-str.
    assert not disp.is_disposable_token("DEADBEEF")
    assert not disp.is_disposable_token("short")
    assert not disp.is_disposable_token("g0123456789abcdef")
    assert not disp.is_disposable_token("a" * 65)
    assert not disp.is_disposable_token("0123; rm -rf /")
    assert not disp.is_disposable_token("")
    assert not disp.is_disposable_token(None)  # type: ignore[arg-type]


def test_dispose_action():
    assert disp.dispose_action("pdf") == "qdistro.dispose.spawn:pdf"
    with pytest.raises(disp.DisposableError):
        disp.dispose_action("BAD")


# ---- sweep targets ---------------------------------------------------

def test_sweep_targets_only_disposables():
    names = [
        "disp-pdf-20260612-151828",         # disposable
        "disp-office-20260612-151900-ab",   # disposable w/ suffix
        "qdistro-silo-mybrowser",           # persistent tier-2: NOT touched
        "disp-",                            # malformed: NOT a disposable
        "dispatcher",                       # not a disposable (no shape)
        "disposable-thing",                 # not disp- prefix shape
    ]
    targets = disp.disp_sweep_targets(names)
    assert targets == [
        "disp-pdf-20260612-151828",
        "disp-office-20260612-151900-ab",
    ]


def test_is_disposable_container_strict():
    assert disp.is_disposable_container("disp-pdf-20260612-151828")
    assert not disp.is_disposable_container("disp-pdf")           # no ts
    assert not disp.is_disposable_container("disp-pdf-20260612")  # short ts
    assert not disp.is_disposable_container("qdistro-silo-pdf")
    assert not disp.is_disposable_container("")
    # A trailing newline must NOT slip past (fullmatch, not $-anchored match).
    assert not disp.is_disposable_container("disp-pdf-20260612-151828\n")
    assert not disp.is_disposable_container("disp-pdf-20260612-151828\nevil")


def test_validate_workload_rejects_trailing_newline():
    with pytest.raises(disp.DisposableError):
        disp.validate_workload("pdf\n")
    with pytest.raises(disp.DisposableError):
        disp.validate_workload("pdf\nrm")


# ---- session-manager reaper (with a fake ops) ------------------------

class FakeOps:
    def __init__(self, containers):
        self._containers = list(containers)
        self.removed: list[str] = []

    def disp_container_list(self):
        return list(self._containers)

    def disp_container_remove(self, name):
        if not disp.is_disposable_container(name):
            raise ValueError(name)
        self.removed.append(name)
        self._containers.remove(name)
        return True


class _Reaper:
    """Minimal stand-in exercising the real reaper logic shape: list ->
    disp_sweep_targets -> remove. Mirrors
    SiloManager.reap_disposable_containers without importing the 2k-line
    daemon."""
    def __init__(self, ops):
        self._ops = ops

    def reap(self):
        reaped = []
        for name in disp.disp_sweep_targets(self._ops.disp_container_list()):
            if self._ops.disp_container_remove(name):
                reaped.append(name)
        return reaped


def test_reaper_removes_only_disposables():
    ops = FakeOps([
        "disp-pdf-20260612-151828",
        "qdistro-silo-browser",       # persistent — must survive
        "disp-office-20260612-152000",
    ])
    reaped = _Reaper(ops).reap()
    assert set(reaped) == {
        "disp-pdf-20260612-151828", "disp-office-20260612-152000"}
    assert ops.removed == reaped
    assert "qdistro-silo-browser" in ops._containers  # untouched


# ---- lease (TTL max-lifetime) pure helpers ---------------------------

@pytest.mark.parametrize("raw,want", [
    ("0", 0),
    ("1", 1),
    ("300", 300),
    ("  300  ", 300),                 # surrounding whitespace tolerated
    ("99999999999999999999", 99999999999999999999),  # python int, no overflow
    (300, 300),                       # already an int
    (0, 0),
    # Fail-closed -> None (candidate is skipped, never reaped on a guess):
    (None, None),
    ("", None),
    ("<no value>", None),             # podman's absent-label sentinel
    ("-5", None),
    (-5, None),
    ("5.0", None),
    ("5 6", None),
    ("0x10", None),
    ("inf", None),
    ("nan", None),
    ("5m", None),
    ("  ", None),
    ("\t300\n", 300),                 # str.strip() handles tabs/newlines
    (True, None),                     # bool is an int subclass — rejected
    (False, None),
    (3.5, None),                      # raw float object rejected
    (["300"], None),                  # wrong type
])
def test_parse_lease_seconds(raw, want):
    assert disp.parse_lease_seconds(raw) == want


@pytest.mark.parametrize("now,created,ttl,want", [
    # No lease / opt-out: ttl None or <= 0 -> never expired.
    (1000.0, 0, None, False),
    (1000.0, 0, 0, False),
    (10_000.0, 0, -1, False),
    # created unknown -> fail-safe, never expired.
    (10_000.0, None, 300, False),
    # Within the lease window -> not expired (age == ttl is NOT expired).
    (1300.0, 1000, 300, False),
    (1299.0, 1000, 300, False),
    # Past the lease -> expired.
    (1301.0, 1000, 300, True),
    (10_000.0, 1000, 300, True),
    # Clock jumped backwards (negative age) -> clamp to not-expired.
    (500.0, 1000, 300, False),
])
def test_lease_expired(now, created, ttl, want):
    assert disp.lease_expired(now, created, ttl) is want


_TOK = "0123456789abcdef0123456789abcdef"


def _cand(name, token=_TOK, ttl="300", created="1000"):
    return {"name": name, "token": token, "ttl": ttl, "created": created}


def test_lease_sweep_targets_reaps_only_expired_well_formed():
    now = 2000.0  # created=1000 + ttl=300 -> expired by now
    cands = [
        _cand("disp-pdf-20260612-151828"),                       # expired -> reap
        _cand("disp-office-20260612-152000", ttl="5000"),        # under ttl -> keep
        _cand("disp-agent-20260612-152100", ttl="0"),            # no lease -> keep
        _cand("disp-agent-20260612-152200", ttl="<no value>"),   # no ttl label -> keep
        _cand("disp-agent-20260612-152300", created="<no value>"),  # no created -> keep
        _cand("disp-agent-20260612-152400", ttl="5m"),           # malformed ttl -> keep
        _cand("qdistro-silo-browser"),                           # not disp-shaped -> keep
        _cand("disp-evil-20260612-152500", token="NOTHEX"),      # bad token label -> keep
        _cand("disp-evil-20260612-152600", token="<no value>"),  # missing token -> keep
        _cand("disp-pdf-20260612-151828\nevil"),                 # newline name -> keep
    ]
    assert disp.lease_sweep_targets(cands, now) == ["disp-pdf-20260612-151828"]


def test_lease_sweep_targets_empty():
    assert disp.lease_sweep_targets([], 5000.0) == []


def test_lease_sweep_targets_missing_keys_are_skipped():
    # A candidate dict missing fields (a malformed podman row) is skipped, not
    # crashed on.
    assert disp.lease_sweep_targets([{"name": "disp-x-20260612-151828"}],
                                    9_000_000_000.0) == []


# ---- process-tree-empty lease pure helpers ---------------------------

@pytest.mark.parametrize("raw,want", [
    ("1", True),
    (" 1 ", True),
    ("\t1\n", True),
    (1, True),
    (True, True),
    # Fail-closed -> not opted in:
    ("0", False),
    (0, False),
    (False, False),
    ("", False),
    ("<no value>", False),
    (None, False),
    ("yes", False),
    ("true", False),
    ("11", False),
    (2, False),
    (["1"], False),
])
def test_lease_opt_in(raw, want):
    assert disp.lease_opt_in(raw) is want


@pytest.mark.parametrize("out,want", [
    # Header + a single PID1 weston row -> [(1,"weston")]
    ("PID   COMMAND\n1     weston\n", [(1, "weston")]),
    # Path-form command preserved verbatim (basename match happens in
    # proctree_empty, not here).
    ("PID COMMAND\n1 /usr/bin/weston\n", [(1, "/usr/bin/weston")]),
    # Multiple rows.
    ("PID COMMAND\n1 weston\n42 weston-terminal\n",
     [(1, "weston"), (42, "weston-terminal")]),
    # A command descriptor itself may be a single token even if it had args;
    # split(None,1) keeps the remainder as one comm field.
    ("PID COMMAND\n1 weston --foo bar\n", [(1, "weston --foo bar")]),
    # Fail-closed -> None (caller SKIPs):
    ("", None),                         # empty
    ("   \n  \n", None),                # blank
    ("PID COMMAND\n", None),            # header only, no process row
    ("PID COMMAND\nweston\n", None),    # row missing the PID field
    ("PID COMMAND\nxx weston\n", None), # non-integer PID
    ("PID COMMAND\n1\n", None),         # PID with no comm
    ("PID COMMAND\n-1 weston\n", None), # negative (isdigit rejects '-')
    (None, None),                       # wrong type
    (123, None),
])
def test_parse_podman_top_pids(out, want):
    assert disp.parse_podman_top_pids(out) == want


@pytest.mark.parametrize("out,want", [
    # Only PID1 weston remains -> empty.
    ("PID COMMAND\n1 weston\n", True),
    ("PID COMMAND\n1 /usr/bin/weston\n", True),  # basename match
    # Not empty: an inner client still running.
    ("PID COMMAND\n1 weston\n42 weston-terminal\n", False),
    # Not empty: PID1 is not weston (should-never-happen, fail-closed).
    ("PID COMMAND\n1 bash\n", False),
    # The sole row is not PID1 (podman oddity) -> fail-closed.
    ("PID COMMAND\n7 weston\n", False),
    # Unparseable -> fail-closed False.
    ("", False),
    ("PID COMMAND\n", False),
    (None, False),
])
def test_proctree_empty(out, want):
    assert disp.proctree_empty(out) is want


def test_proctree_empty_custom_pid1_comm():
    assert disp.proctree_empty("PID COMMAND\n1 sway\n", pid1_comm="sway") is True
    assert disp.proctree_empty("PID COMMAND\n1 weston\n", pid1_comm="sway") is False


@pytest.mark.parametrize("now,created,grace,want", [
    # created unknown -> fail-safe never elapsed.
    (10_000.0, None, 30, False),
    # within grace -> not elapsed.
    (1020.0, 1000, 30, False),
    (1029.0, 1000, 30, False),
    # at/past grace -> elapsed.
    (1030.0, 1000, 30, True),
    (5000.0, 1000, 30, True),
    # grace None -> default (30).
    (1029.0, 1000, None, False),
    (1030.0, 1000, None, True),
    # negative grace -> treated as the default, not "no grace".
    (1010.0, 1000, -5, False),
    (1030.0, 1000, -5, True),
    # clock jumped backwards (negative age) -> clamp to not elapsed.
    (500.0, 1000, 30, False),
])
def test_proctree_grace_elapsed(now, created, grace, want):
    assert disp.proctree_grace_elapsed(now, created, grace) is want


_PTOK = "0123456789abcdef0123456789abcdef"


def _pcand(name, token=_PTOK, proctree="1", created="1000", grace="30"):
    return {"name": name, "token": token, "proctree": proctree,
            "created": created, "grace": grace}


def test_proctree_candidate_eligible_happy_path():
    # disp-* name, valid token, proctree opt-in, created old enough past grace.
    cand = _pcand("disp-agent-20260612-151828")
    assert disp.proctree_candidate_eligible(cand, now_epoch=2000.0) is True


@pytest.mark.parametrize("cand,now", [
    # not opted in -> ineligible
    (_pcand("disp-agent-20260612-151828", proctree="0"), 2000.0),
    (_pcand("disp-agent-20260612-151828", proctree="<no value>"), 2000.0),
    # within grace -> ineligible (mid-startup protection)
    (_pcand("disp-agent-20260612-151828"), 1010.0),
    # created absent/malformed -> ineligible (cannot judge age)
    (_pcand("disp-agent-20260612-151828", created="<no value>"), 9e9),
    (_pcand("disp-agent-20260612-151828", created="x"), 9e9),
    # bad token -> ineligible
    (_pcand("disp-agent-20260612-151828", token="NOTHEX"), 9e9),
    (_pcand("disp-agent-20260612-151828", token="<no value>"), 9e9),
    # non-disp name -> ineligible (never collateral)
    (_pcand("qdistro-silo-browser"), 9e9),
    (_pcand("disp-x-20260612-151828\nevil"), 9e9),
])
def test_proctree_candidate_ineligible(cand, now):
    assert disp.proctree_candidate_eligible(cand, now_epoch=now) is False


def test_proctree_candidate_missing_keys_skipped():
    assert disp.proctree_candidate_eligible({"name": "disp-x-20260612-151828"},
                                            now_epoch=9e9) is False


@pytest.mark.parametrize("wid,want", [
    ("step-1", True),
    ("a", True),
    ("wf-2026-06-13-abc", True),
    ("0build", True),
    ("x" * 128, True),
    # Fail-closed:
    ("", False),
    ("-leading", False),
    ("UPPER", False),
    ("has space", False),
    ("semi;colon", False),
    ("x" * 129, False),       # too long
    ("wf$", False),
    (None, False),
    (123, False),
    (["step-1"], False),
])
def test_is_workflow_id(wid, want):
    assert disp.is_workflow_id(wid) is want


# ---------------------------------------------------------------------------
# parse_podman_ps_json — robust JSON candidate parsing + hostile-byte fuzzing
# ---------------------------------------------------------------------------

_LK = ["qdistro_tier2_token", "qdistro_lease_ttl"]
_GOOD_NAME = "disp-pdf-20260612-151828"
_TOK = "0123456789abcdef0123456789abcdef"


def test_ps_json_parses_modern_shape():
    raw = json.dumps([
        {"Names": [_GOOD_NAME],
         "Labels": {"qdistro_tier2_token": _TOK, "qdistro_lease_ttl": "300"}},
    ])
    rows = disp.parse_podman_ps_json(raw, _LK)
    assert rows == [{"name": _GOOD_NAME, "qdistro_tier2_token": _TOK,
                     "qdistro_lease_ttl": "300"}]


def test_ps_json_absent_label_is_none():
    raw = json.dumps([{"Names": [_GOOD_NAME], "Labels": {}}])
    rows = disp.parse_podman_ps_json(raw, _LK)
    assert rows[0]["qdistro_tier2_token"] is None
    assert rows[0]["qdistro_lease_ttl"] is None


def test_ps_json_null_labels_object_is_empty():
    # podman may render no-labels as Labels: null.
    raw = json.dumps([{"Names": [_GOOD_NAME], "Labels": None}])
    rows = disp.parse_podman_ps_json(raw, _LK)
    assert rows == [{"name": _GOOD_NAME, "qdistro_tier2_token": None,
                     "qdistro_lease_ttl": None}]


def test_ps_json_names_as_plain_string_fallback():
    raw = json.dumps([{"Names": _GOOD_NAME, "Labels": {}}])
    assert disp.parse_podman_ps_json(raw, _LK)[0]["name"] == _GOOD_NAME


def test_ps_json_singular_name_fallback():
    raw = json.dumps([{"Name": _GOOD_NAME, "Labels": {}}])
    assert disp.parse_podman_ps_json(raw, _LK)[0]["name"] == _GOOD_NAME


@pytest.mark.parametrize("raw", [
    "",                        # blank
    "   ",                     # whitespace
    "not json",                # garbage
    '[{"Names": ["disp-',      # truncated stream
    "{}",                      # top-level object, not a list
    '"a string"',              # top-level scalar
    "123",                     # top-level int
    "null",                    # top-level null
])
def test_ps_json_malformed_input_fails_closed(raw):
    # A garbled/truncated stream -> [] for the WHOLE pass (never best-effort
    # partial rows). The caller treats this like a podman failure.
    assert disp.parse_podman_ps_json(raw, _LK) == []


def test_ps_json_non_str_input_fails_closed():
    assert disp.parse_podman_ps_json(None, _LK) == []
    assert disp.parse_podman_ps_json(123, _LK) == []
    assert disp.parse_podman_ps_json(["already", "parsed"], _LK) == []


def test_ps_json_bytes_input_decoded():
    raw = json.dumps([{"Names": [_GOOD_NAME], "Labels": {}}]).encode()
    assert disp.parse_podman_ps_json(raw, _LK)[0]["name"] == _GOOD_NAME


def test_ps_json_invalid_utf8_bytes_fail_closed():
    assert disp.parse_podman_ps_json(b"\xff\xfe not utf8", _LK) == []


def test_ps_json_one_malformed_record_does_not_poison_array():
    raw = json.dumps([
        "a bare string, not a dict",            # skipped
        {"Labels": {}},                          # no Names -> skipped
        {"Names": [], "Labels": {}},             # empty Names -> skipped
        {"Names": [123, _GOOD_NAME], "Labels": {}},  # first non-str skipped
        {"Names": [_GOOD_NAME], "Labels": {}},   # GOOD
    ])
    rows = disp.parse_podman_ps_json(raw, _LK)
    # The two records that DO yield a name survive (the [123,name] one picks the
    # str entry); the dict/None/empty ones are skipped without poisoning.
    names = [r["name"] for r in rows]
    assert names == [_GOOD_NAME, _GOOD_NAME]


@pytest.mark.parametrize("hostile", [
    "x\ny",                    # embedded newline
    "x\x00y",                  # NUL
    "x\x1fy",                  # the OLD US separator byte
    "x\ty",                    # tab
    'x"y\'z',                  # quotes
    "x\r\ny",                  # CRLF
    "‮​",            # unicode bidi / zero-width
    "x" * 100000,              # very long
    "disp-evil-20200101-000000",  # a forged disp name AS a label value
])
def test_ps_json_hostile_label_bytes_confined_to_value(hostile):
    # An adversarial label VALUE (any of these bytes) is JSON-escaped, so it
    # round-trips as the exact value and can NEVER (a) crash the parse, (b) forge
    # a second record, or (c) change the genuine row's name. The value flows on
    # to the existing int/token gates which reject it -> the candidate is skipped
    # downstream, never reaped on these bytes.
    raw = json.dumps([
        {"Names": [_GOOD_NAME],
         "Labels": {"qdistro_tier2_token": hostile, "qdistro_lease_ttl": "300"}},
    ])
    rows = disp.parse_podman_ps_json(raw, _LK)
    assert len(rows) == 1                       # NO forged second record
    assert rows[0]["name"] == _GOOD_NAME        # name intact
    assert rows[0]["qdistro_tier2_token"] == hostile  # confined to the value
    # And the existing token gate rejects the hostile value (skip downstream).
    assert disp.is_disposable_token(hostile) is False


def test_ps_json_hostile_bytes_do_not_select_a_well_formed_expired():
    # A garbled label on ONE disposable must not stop a SECOND, genuinely-expired
    # well-formed disposable from being selected by lease_sweep_targets.
    raw = json.dumps([
        {"Names": ["disp-bad-20200101-000000"],
         "Labels": {"qdistro_tier2_token": "x\ny\x1f", "qdistro_lease_ttl": "5"}},
        {"Names": [_GOOD_NAME],
         "Labels": {"qdistro_tier2_token": _TOK, "qdistro_lease_ttl": "5"}},
    ])
    rows = disp.parse_podman_ps_json(
        raw, ["qdistro_tier2_token", "qdistro_lease_ttl"])
    cands = [{"name": r["name"], "token": r["qdistro_tier2_token"],
              "ttl": r["qdistro_lease_ttl"], "created": "1000"} for r in rows]
    # now=2000, created=1000, ttl=5 -> age 1000 > 5 -> expired. The bad one is
    # skipped (garbled token), the good one IS selected.
    targets = disp.lease_sweep_targets(cands, now_epoch=2000.0)
    assert targets == [_GOOD_NAME]


# ---------------------------------------------------------------------------
# Stuck-descendant kill-candidate decision — pure, fail-safe, fuzzed
# ---------------------------------------------------------------------------

_CID = "a" * 64
_OTHER = "b" * 64


def _cgroup_in(cid):
    return ("0::/user.slice/user-1006.slice/user@1006.service/user.slice/"
            f"libpod-{cid}.scope\n")


@pytest.mark.parametrize("cid,want", [
    ("a" * 64, True),
    ("0123456789abcdef" * 4, True),
    ("a" * 63, False),         # too short
    ("a" * 65, False),         # too long
    ("A" * 64, False),         # uppercase not allowed
    ("g" * 64, False),         # non-hex
    ("", False),
    (None, False),
    (123, False),
])
def test_is_full_container_id(cid, want):
    assert disp.is_full_container_id(cid) is want


def test_top_hpids_parses_host_pids():
    out = "HPID COMMAND\n2001 sleep\n2002 conmon\n"
    assert disp.parse_podman_top_hpids(out) == [2001, 2002]


def test_top_hpids_header_only_is_empty_list():
    # Header but no process rows -> [] (nothing to clean up, not an error).
    assert disp.parse_podman_top_hpids("HPID COMMAND\n") == []


@pytest.mark.parametrize("out", [
    "",                        # empty -> unusable
    "   ",                     # blank
    "HPID COMMAND\nnotpid x\n",   # non-integer pid
    "HPID COMMAND\n-5 x\n",       # negative
    "HPID COMMAND\n0 kernel\n",   # pid 0 (every-process target) refused
    "HPID COMMAND\n12.5 x\n",     # float
    123,                          # not a str
    None,
])
def test_top_hpids_malformed_fails_closed(out):
    assert disp.parse_podman_top_hpids(out) is None


def test_cgroup_membership_exact_component_match():
    assert disp.cgroup_belongs_to_container(_cgroup_in(_CID), _CID) is True


def test_cgroup_membership_other_container_is_false():
    assert disp.cgroup_belongs_to_container(_cgroup_in(_OTHER), _CID) is False


@pytest.mark.parametrize("cg", [
    None,
    123,
    "",
    "0::/user.slice/no-libpod-scope-here\n",
    # the id embedded as a SUBSTRING of a longer component, NOT a whole
    # libpod-<id>.scope component -> must NOT authorize (loose-substring guard).
    f"0::/user.slice/evil-libpod-{_CID}.scope-suffix\n",
    f"0::/user.slice/prefix-libpod-{_CID}.scope\n",
    # right scope text but for a DIFFERENT, malformed id length
    f"0::/libpod-{'a' * 63}.scope\n",
])
def test_cgroup_membership_fails_closed(cg):
    assert disp.cgroup_belongs_to_container(cg, _CID) is False


def test_cgroup_membership_bad_full_id_is_false():
    # Even a perfectly-matching scope text does not authorize if the id we were
    # asked to match is not a well-formed full id.
    assert disp.cgroup_belongs_to_container(_cgroup_in("short"), "short") is False


def test_kill_set_selects_only_verified_pids():
    hpids = [2001, 2002, 2003, 2004]
    cgroups = {
        2001: _cgroup_in(_CID),       # ours
        2002: _cgroup_in(_CID),       # ours
        2003: _cgroup_in(_OTHER),     # another container -> NOT killed
        2004: None,                   # unreadable -> NOT killed
    }
    assert disp.stuck_descendant_kill_set(hpids, cgroups, _CID) == [2001, 2002]


def test_kill_set_none_hpids_is_empty():
    assert disp.stuck_descendant_kill_set(None, {}, _CID) == []


def test_kill_set_bad_full_id_is_empty():
    # No authorization anchor -> kill NOTHING (fail-safe), even with good cgroups.
    assert disp.stuck_descendant_kill_set([2001], {2001: _cgroup_in("x")},
                                          "not-a-full-id") == []


def test_kill_set_skips_nonpositive_and_bool_pids():
    hpids = [0, -1, True, 2001]
    cgroups = {0: _cgroup_in(_CID), -1: _cgroup_in(_CID),
               True: _cgroup_in(_CID), 2001: _cgroup_in(_CID)}
    # Only the genuine positive int pid survives; 0/-1/bool are excluded.
    assert disp.stuck_descendant_kill_set(hpids, cgroups, _CID) == [2001]


def test_kill_set_missing_cgroup_entry_is_skipped():
    # A pid with no recorded cgroup text (dict miss) is unverifiable -> skipped.
    assert disp.stuck_descendant_kill_set([2001, 2002], {2001: _cgroup_in(_CID)},
                                          _CID) == [2001]
