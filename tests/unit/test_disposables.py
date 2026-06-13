"""Tests for the pure disposable-silo helpers + the session-manager reaper
sweep (07-disposables-plan P1)."""
from __future__ import annotations

import importlib.util
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
