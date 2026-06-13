"""Unit tests for disposable export-back (07-disposables-plan P2 / D7 copy-exception):
the defensive promoter (qdistro_disposable_export) and the session-manager store
method import_from_disposable. Pure host lane — no podman, no D-Bus, no real
/var/lib. The real-podman + real-broker half lives in the VM probe."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

SM_DIR = Path(__file__).resolve().parents[2] / "session_manager"
if str(SM_DIR) not in sys.path:
    sys.path.insert(0, str(SM_DIR))

import qdistro_disposable_export as ex  # noqa: E402


# ---------------------------------------------------------------------------
# Pure name hygiene
# ---------------------------------------------------------------------------

def test_sanitize_class_leaf_encodes_slash():
    assert ex.sanitize_class_leaf("text/plain") == "text%2Fplain"
    assert ex.sanitize_class_leaf("agent-scratch") == "agent-scratch"
    # '%' is encoded first so the mapping is unambiguous/reversible.
    assert ex.sanitize_class_leaf("a%b/c") == "a%25b%2Fc"


@pytest.mark.parametrize("name,ok", [
    ("out.txt", True),
    ("a b.txt", True),
    (".hidden", True),
    ("", False),
    (".", False),
    ("..", False),
    ("a/b", False),
    ("a\x00b", False),
    ("a\nb", False),
    ("a\tb", False),
    ("_receipt.json", False),   # reserved importer-owned name
])
def test_sanitize_filename(name, ok):
    got = ex.sanitize_filename(name)
    assert (got == name) if ok else (got is None)


# ---------------------------------------------------------------------------
# Promoter — happy paths
# ---------------------------------------------------------------------------

def _meta(token="abcdef0123456789abcdef0123456789", oc="agent-scratch",
          silo="work", inp=None):
    return {"version": 1, "launch_token": token, "request_silo": silo,
            "open_class": oc, "container": "disp-x", "created": 1,
            "input_basename": inp}


def test_promote_happy_two_files(tmp_path):
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "result.txt").write_text("hello world")
    (payload / "data.bin").write_bytes(b"\x00\x01\x02\x03")
    state = tmp_path / "state"
    state.mkdir()

    receipt = ex.promote_export(str(payload), str(state),
                                meta=_meta(inp="src.txt"), now_epoch=1_700_000_000)

    dest = Path(receipt["dest"])
    assert dest.is_dir()
    # Incoming/<class-leaf>/<token8>-<ts>/
    assert dest.parent.name == "agent-scratch"
    assert dest.parent.parent.name == ex.INCOMING_DIRNAME
    assert dest.name.startswith("abcdef01-")
    assert (dest / "result.txt").read_text() == "hello world"
    assert (dest / "data.bin").read_bytes() == b"\x00\x01\x02\x03"

    names = {f["name"]: f for f in receipt["files"]}
    assert set(names) == {"result.txt", "data.bin"}
    assert names["result.txt"]["size"] == 11
    import hashlib
    assert names["result.txt"]["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert receipt["request_silo"] == "work"
    assert receipt["source_input"] == "src.txt"
    assert receipt["open_class"] == "agent-scratch"
    # The receipt file is on disk and matches.
    on_disk = json.loads((dest / ex.RECEIPT_NAME).read_text())
    assert on_disk["files"] == receipt["files"]


def test_promote_empty_payload_is_clean_zero_file(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    state = tmp_path / "state"; state.mkdir()
    receipt = ex.promote_export(str(payload), str(state), meta=_meta(),
                                now_epoch=1_700_000_000)
    assert receipt["files"] == []
    assert Path(receipt["dest"]).is_dir()
    assert (Path(receipt["dest"]) / ex.RECEIPT_NAME).is_file()


def test_promote_text_plain_class_leaf(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    (payload / "a.txt").write_text("x")
    state = tmp_path / "state"; state.mkdir()
    receipt = ex.promote_export(str(payload), str(state),
                                meta=_meta(oc="text/plain"),
                                now_epoch=1_700_000_000)
    assert Path(receipt["dest"]).parent.name == "text%2Fplain"


def test_promote_collision_suffixes(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    for _ in range(2):
        payload = tmp_path / "payload"
        if payload.exists():
            import shutil
            shutil.rmtree(payload)
        payload.mkdir()
        (payload / "a.txt").write_text("x")
        ex.promote_export(str(payload), str(state), meta=_meta(),
                          now_epoch=1_700_000_000)  # same token8 + ts both times
    cls_dir = state / ex.INCOMING_DIRNAME / "agent-scratch"
    landed = sorted(p.name for p in cls_dir.iterdir())
    assert len(landed) == 2
    assert any(n.endswith("-1") for n in landed)  # second got a suffix


# ---------------------------------------------------------------------------
# Promoter — all-or-nothing refusals (nothing promoted)
# ---------------------------------------------------------------------------

def _assert_nothing_landed(state):
    """After a refusal there must be NO completed import directory (a leftover
    temp dir would start with '.import-' and is cleaned in promote()'s finally)."""
    inc = Path(state) / ex.INCOMING_DIRNAME
    completed = []
    if inc.exists():
        for cls in inc.iterdir():
            for d in cls.iterdir():
                if not d.name.startswith(".import-"):
                    completed.append(d)
    assert not completed, f"a payload was promoted despite a refusal: {completed}"


def test_promote_symlink_refused(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    (payload / "ok.txt").write_text("x")
    os.symlink("/etc/passwd", payload / "evil")
    state = tmp_path / "state"; state.mkdir()
    with pytest.raises(ex.ExportPolicyError):
        ex.promote_export(str(payload), str(state), meta=_meta(),
                          now_epoch=1)
    _assert_nothing_landed(state)


def test_promote_fifo_refused(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    os.mkfifo(payload / "pipe")
    state = tmp_path / "state"; state.mkdir()
    with pytest.raises(ex.ExportPolicyError):
        ex.promote_export(str(payload), str(state), meta=_meta(), now_epoch=1)
    _assert_nothing_landed(state)


def test_promote_subdir_refused(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    (payload / "sub").mkdir()
    state = tmp_path / "state"; state.mkdir()
    with pytest.raises(ex.ExportPolicyError):
        ex.promote_export(str(payload), str(state), meta=_meta(), now_epoch=1)
    _assert_nothing_landed(state)


def test_promote_reserved_receipt_name_refused(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    (payload / ex.RECEIPT_NAME).write_text("forged")
    state = tmp_path / "state"; state.mkdir()
    with pytest.raises(ex.ExportPolicyError):
        ex.promote_export(str(payload), str(state), meta=_meta(), now_epoch=1)
    _assert_nothing_landed(state)


def test_promote_max_files_cap(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    for i in range(5):
        (payload / f"f{i}").write_text("x")
    state = tmp_path / "state"; state.mkdir()
    with pytest.raises(ex.ExportPolicyError):
        ex.promote_export(str(payload), str(state), meta=_meta(), now_epoch=1,
                          caps=ex.ExportCaps(max_files=3))
    _assert_nothing_landed(state)


def test_promote_per_file_size_cap(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    (payload / "big").write_bytes(b"x" * 100)
    state = tmp_path / "state"; state.mkdir()
    with pytest.raises(ex.ExportPolicyError):
        ex.promote_export(str(payload), str(state), meta=_meta(), now_epoch=1,
                          caps=ex.ExportCaps(max_file_bytes=10))
    _assert_nothing_landed(state)


def test_promote_total_size_cap(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    (payload / "a").write_bytes(b"x" * 60)
    (payload / "b").write_bytes(b"y" * 60)
    state = tmp_path / "state"; state.mkdir()
    with pytest.raises(ex.ExportPolicyError):
        ex.promote_export(str(payload), str(state), meta=_meta(), now_epoch=1,
                          caps=ex.ExportCaps(max_file_bytes=1000,
                                             max_total_bytes=100))
    _assert_nothing_landed(state)


def test_promote_refuses_symlinked_incoming(tmp_path):
    """A silo-planted symlink at <state>/Incoming must NOT redirect the root
    importer outside state_path (codex SHIP blocker). The no-follow chain refuses
    it and nothing lands at the symlink target."""
    state = tmp_path / "state"; state.mkdir()
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir()
    os.symlink(elsewhere, state / ex.INCOMING_DIRNAME)  # attacker redirect
    payload = tmp_path / "payload"; payload.mkdir()
    (payload / "a.txt").write_text("x")
    with pytest.raises(ex.ExportError):
        ex.promote_export(str(payload), str(state), meta=_meta(),
                          now_epoch=1_700_000_000)
    assert not any(elsewhere.iterdir()), "import landed at the symlink target!"


def test_promote_refuses_symlinked_class_dir(tmp_path):
    """Same defence one level deeper: Incoming/<class-leaf> planted as a symlink."""
    state = tmp_path / "state"; state.mkdir()
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir()
    inc = state / ex.INCOMING_DIRNAME; inc.mkdir()
    os.symlink(elsewhere, inc / "agent-scratch")
    payload = tmp_path / "payload"; payload.mkdir()
    (payload / "a.txt").write_text("x")
    with pytest.raises(ex.ExportError):
        ex.promote_export(str(payload), str(state), meta=_meta(),
                          now_epoch=1_700_000_000)
    assert not any(elsewhere.iterdir()), "import landed at the symlink target!"


def test_promote_all_or_nothing_one_bad_entry(tmp_path):
    payload = tmp_path / "payload"; payload.mkdir()
    (payload / "good.txt").write_text("fine")
    os.symlink("/etc/hostname", payload / "bad")
    state = tmp_path / "state"; state.mkdir()
    with pytest.raises(ex.ExportPolicyError):
        ex.promote_export(str(payload), str(state), meta=_meta(), now_epoch=1)
    # good.txt must NOT have landed.
    _assert_nothing_landed(state)


# ---------------------------------------------------------------------------
# Store.import_from_disposable — fail-closed paths with a minimal fake ops
# ---------------------------------------------------------------------------

import qdistro_session_manager as sm  # noqa: E402
from qdistro_session_manager import BadArgument, BadState  # noqa: E402

TOKEN = "abcdef0123456789abcdef0123456789"


class _ExportFakeOps:
    """Minimal _SystemOps stand-in for the export-back store tests — only the
    methods import_from_disposable / dispose_by_token / reap_export_staging touch."""
    def __init__(self):
        self.disp_containers: list[str] = []
        self.disp_token_map: dict[str, list[str]] = {}
        self.disp_removed: list[str] = []
        self.remove_leaves_live = False  # simulate a rm that doesn't free the token
        self.broker_verdict = "allow"
        self.state_path: str | None = None
        self.state_resolve_raises: BaseException | None = None
        self.live_tokens: set[str] | None = set()

    # dispose path
    def disp_containers_by_token(self, token):
        return [n for n in self.disp_token_map.get(token, [])
                if n in self.disp_containers]

    def disp_container_remove(self, name):
        self.disp_removed.append(name)
        if not self.remove_leaves_live and name in self.disp_containers:
            self.disp_containers.remove(name)
        return True

    # export path
    def broker_check_permission(self, action):
        return self.broker_verdict

    def export_resolve_state_path(self, silo):
        if self.state_resolve_raises is not None:
            raise self.state_resolve_raises
        return self.state_path

    def disp_live_tokens(self):
        return None if self.live_tokens is None else set(self.live_tokens)


def _store(tmp_path, ops):
    return sm._SiloStore(ops, config_path=tmp_path / "silos.yaml",
                         export_staging_base=tmp_path / "staging")


def _stage(base: Path, token=TOKEN, *, meta=True, meta_text=None,
           payload_files=None):
    d = base / token
    (d / "payload").mkdir(parents=True)
    if payload_files:
        for name, content in payload_files.items():
            (d / "payload" / name).write_text(content)
    if meta:
        text = meta_text if meta_text is not None else json.dumps({
            "version": 1, "launch_token": token, "request_silo": "work",
            "open_class": "agent-scratch", "container": "disp-x",
            "created": 1, "input_basename": None})
        (d / "meta.json").write_text(text)
    return d


class _Cls:
    def __init__(self, export=True, edit=False):
        self.export = export
        self.edit = edit
        self.name = "agent-scratch"


@pytest.fixture
def export_class(monkeypatch):
    cls = _Cls(export=True)
    monkeypatch.setattr(sm._dispclasses, "resolve_from_registry",
                        lambda name, **kw: cls)
    return cls


@pytest.fixture
def edit_class(monkeypatch):
    cls = _Cls(export=True, edit=True)
    monkeypatch.setattr(sm._dispclasses, "resolve_from_registry",
                        lambda name, **kw: cls)
    return cls


def test_import_malformed_token_badargument(tmp_path):
    store = _store(tmp_path, _ExportFakeOps())
    with pytest.raises(BadArgument):
        store.import_from_disposable("not a token!")


def test_import_absent_staging_is_clean_zero_file(tmp_path):
    store = _store(tmp_path, _ExportFakeOps())
    receipt = store.import_from_disposable(TOKEN)
    assert receipt["files"] == []
    assert receipt["dest"] is None


def test_import_present_staging_missing_meta_badstate(tmp_path):
    ops = _ExportFakeOps()
    base = tmp_path / "staging"
    _stage(base, meta=False)
    with pytest.raises(BadState):
        _store(tmp_path, ops).import_from_disposable(TOKEN)


def test_import_corrupt_meta_badstate(tmp_path):
    ops = _ExportFakeOps()
    base = tmp_path / "staging"
    _stage(base, meta_text="{not json")
    with pytest.raises(BadState):
        _store(tmp_path, ops).import_from_disposable(TOKEN)


def test_import_meta_token_mismatch_badstate(tmp_path):
    ops = _ExportFakeOps()
    base = tmp_path / "staging"
    _stage(base, meta_text=json.dumps({
        "version": 1, "launch_token": "0" * 32, "request_silo": "work",
        "open_class": "agent-scratch"}))
    with pytest.raises(BadState):
        _store(tmp_path, ops).import_from_disposable(TOKEN)


def test_import_happy_path_promotes_and_removes_staging(tmp_path, export_class):
    ops = _ExportFakeOps()
    ops.state_path = str(tmp_path / "silostate")
    (tmp_path / "silostate").mkdir()
    base = tmp_path / "staging"
    _stage(base, payload_files={"out.txt": "result"})

    receipt = _store(tmp_path, ops).import_from_disposable(TOKEN)

    assert [f["name"] for f in receipt["files"]] == ["out.txt"]
    dest = Path(receipt["dest"])
    assert (dest / "out.txt").read_text() == "result"
    # One-shot: staging removed after a durable import.
    assert not (base / TOKEN).exists()


def test_import_broker_deny_refuses_and_keeps_staging(tmp_path, export_class):
    ops = _ExportFakeOps()
    ops.broker_verdict = "unknown"
    ops.state_path = str(tmp_path / "silostate"); (tmp_path / "silostate").mkdir()
    base = tmp_path / "staging"
    _stage(base, payload_files={"out.txt": "x"})
    with pytest.raises(BadState):
        _store(tmp_path, ops).import_from_disposable(TOKEN)
    assert (base / TOKEN).exists()  # not destroyed on a policy denial


def test_import_class_not_export_capable_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sm._dispclasses, "resolve_from_registry",
                        lambda name, **kw: _Cls(export=False))
    ops = _ExportFakeOps()
    ops.state_path = str(tmp_path / "silostate"); (tmp_path / "silostate").mkdir()
    base = tmp_path / "staging"
    _stage(base, payload_files={"out.txt": "x"})
    with pytest.raises(BadState):
        _store(tmp_path, ops).import_from_disposable(TOKEN)


def test_import_untemplated_silo_refused(tmp_path, export_class):
    ops = _ExportFakeOps()
    ops.state_path = None  # untemplated — no binding
    base = tmp_path / "staging"
    _stage(base, payload_files={"out.txt": "x"})
    with pytest.raises(BadState):
        _store(tmp_path, ops).import_from_disposable(TOKEN)


def test_import_disposable_still_live_after_dispose_refused(tmp_path, export_class):
    ops = _ExportFakeOps()
    ops.disp_containers = ["disp-weston-terminal-20260613-120000"]
    ops.disp_token_map = {TOKEN: ["disp-weston-terminal-20260613-120000"]}
    ops.remove_leaves_live = True  # rm "succeeds" but the token still resolves
    ops.state_path = str(tmp_path / "silostate"); (tmp_path / "silostate").mkdir()
    base = tmp_path / "staging"
    _stage(base, payload_files={"out.txt": "x"})
    with pytest.raises(BadState):
        _store(tmp_path, ops).import_from_disposable(TOKEN)


def test_reap_export_staging_orphans_only(tmp_path):
    ops = _ExportFakeOps()
    base = tmp_path / "staging"
    live = "f" * 32
    orphan = "e" * 32
    _stage(base, token=live)
    _stage(base, token=orphan)
    (base / "not-a-token").mkdir()  # must never be touched
    ops.live_tokens = {live}
    reaped = _store(tmp_path, ops).reap_export_staging()
    assert reaped == [orphan]
    assert (base / live).exists()
    assert not (base / orphan).exists()
    assert (base / "not-a-token").exists()


def test_reap_export_staging_fail_closed_on_live_query_failure(tmp_path):
    """If the live-token query fails (None), the sweep must SKIP entirely and
    keep ALL staging — never read 'query failed' as 'none live' and delete a
    user's not-yet-imported artifacts (adversarial review M1)."""
    ops = _ExportFakeOps()
    base = tmp_path / "staging"
    orphan = "e" * 32
    _stage(base, token=orphan)
    ops.live_tokens = None  # podman/runuser query failed
    reaped = _store(tmp_path, ops).reap_export_staging()
    assert reaped == []
    assert (base / orphan).exists()  # kept — not destroyed on a failed query


# ---------------------------------------------------------------------------
# Edit-round-trip lander (promote_edit) — pure, real-fs
# ---------------------------------------------------------------------------

_EDIT_META = {"launch_token": "a" * 32, "open_class": "agent-scratch",
              "request_silo": "work", "container": "disp-x"}


def _payload(tmp_path, files):
    p = tmp_path / "payload"
    p.mkdir()
    for n, c in files.items():
        (p / n).write_text(c)
    return str(p)


def test_promote_edit_lands_beside_source(tmp_path):
    state = tmp_path / "state"
    (state / "docs").mkdir(parents=True)
    src = state / "docs" / "report.txt"
    src.write_text("ORIGINAL")
    payload = _payload(tmp_path, {"whatever.txt": "EDITED"})
    r = ex.promote_edit(payload, str(state), source_rel="docs/report.txt",
                        meta=_EDIT_META, now_epoch=1700000000)
    assert r["mode"] == "edit"
    assert r["source"] == "docs/report.txt"
    dest = Path(r["dest"])
    assert dest == state / "docs" / "report.txt.disp-edited"
    assert dest.read_text() == "EDITED"
    assert src.read_text() == "ORIGINAL"  # source NEVER overwritten
    assert r["files"][0]["name"] == "report.txt.disp-edited"
    # No temp litter left behind in the source dir.
    assert sorted(os.listdir(state / "docs")) == [
        "report.txt", "report.txt.disp-edited"]


def test_promote_edit_never_overwrites_collision_suffix(tmp_path):
    state = tmp_path / "state"
    (state / "d").mkdir(parents=True)
    (state / "d" / "f.txt").write_text("ORIG")
    payload = _payload(tmp_path, {"x": "E1"})
    r1 = ex.promote_edit(payload, str(state), source_rel="d/f.txt",
                         meta=_EDIT_META, now_epoch=1)
    r2 = ex.promote_edit(payload, str(state), source_rel="d/f.txt",
                         meta=_EDIT_META, now_epoch=2)
    assert Path(r1["dest"]).name == "f.txt.disp-edited"
    assert Path(r2["dest"]).name == "f.txt.disp-edited-1"
    assert (state / "d" / "f.txt").read_text() == "ORIG"


def test_promote_edit_zero_file_is_clean_noop(tmp_path):
    state = tmp_path / "state"
    (state / "d").mkdir(parents=True)
    (state / "d" / "f.txt").write_text("ORIG")
    payload = _payload(tmp_path, {})
    r = ex.promote_edit(payload, str(state), source_rel="d/f.txt",
                        meta=_EDIT_META, now_epoch=1)
    assert r["mode"] == "edit" and r["files"] == [] and r["dest"] is None
    # Nothing landed beside the source.
    assert os.listdir(state / "d") == ["f.txt"]


def test_promote_edit_multi_file_refused(tmp_path):
    state = tmp_path / "state"
    (state / "d").mkdir(parents=True)
    (state / "d" / "f.txt").write_text("ORIG")
    payload = _payload(tmp_path, {"a": "1", "b": "2"})
    with pytest.raises(ex.ExportPolicyError, match="single edited file"):
        ex.promote_edit(payload, str(state), source_rel="d/f.txt",
                        meta=_EDIT_META, now_epoch=1)
    assert os.listdir(state / "d") == ["f.txt"]


def test_promote_edit_symlink_dir_component_refused(tmp_path):
    """A silo-owner-planted symlink in the source's parent chain must NOT redirect
    the root lander outside the silo (O_NOFOLLOW walk)."""
    state = tmp_path / "state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text("o")
    os.symlink(str(outside), str(state / "evil"))
    payload = _payload(tmp_path, {"x": "E"})
    with pytest.raises(ex.ExportPolicyError, match="symlink"):
        ex.promote_edit(payload, str(state), source_rel="evil/s.txt",
                        meta=_EDIT_META, now_epoch=1)
    # The outside dir was never written into.
    assert os.listdir(outside) == ["s.txt"]


def test_promote_edit_symlinked_source_refused(tmp_path):
    state = tmp_path / "state"
    (state / "d").mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("T")
    os.symlink(str(target), str(state / "d" / "f.txt"))
    payload = _payload(tmp_path, {"x": "E"})
    with pytest.raises(ex.ExportPolicyError, match="source.*symlink"):
        ex.promote_edit(payload, str(state), source_rel="d/f.txt",
                        meta=_EDIT_META, now_epoch=1)


def test_promote_edit_missing_source_refused(tmp_path):
    state = tmp_path / "state"
    (state / "d").mkdir(parents=True)
    payload = _payload(tmp_path, {"x": "E"})
    with pytest.raises(ex.ExportStateError, match="no longer exists"):
        ex.promote_edit(payload, str(state), source_rel="d/gone.txt",
                        meta=_EDIT_META, now_epoch=1)


@pytest.mark.parametrize("bad", [
    "/etc/passwd", "../../etc/passwd", "a//b", "a/../b", "", ".", "..",
    "a/_receipt.json",
])
def test_split_source_rel_rejects_escapes(bad):
    with pytest.raises(ex.ExportPolicyError):
        ex.split_source_rel(bad)


def test_split_source_rel_ok():
    assert ex.split_source_rel("docs/report.txt") == (["docs"], "report.txt")
    assert ex.split_source_rel("f.txt") == ([], "f.txt")


# ---------------------------------------------------------------------------
# Store import_from_disposable — the edit-round-trip branch
# ---------------------------------------------------------------------------

def _stage_edit(base, token=TOKEN, *, silo="work", source_realpath,
                payload_files=None):
    d = base / token
    (d / "payload").mkdir(parents=True)
    for name, content in (payload_files or {}).items():
        (d / "payload" / name).write_text(content)
    (d / "meta.json").write_text(json.dumps({
        "version": 1, "launch_token": token, "request_silo": silo,
        "open_class": "agent-scratch", "container": "disp-x", "created": 1,
        "input_basename": os.path.basename(source_realpath),
        "edit_mode": True, "input_realpath": source_realpath}))
    return d


def test_import_edit_lands_beside_source(tmp_path, edit_class):
    """End-to-end on a real fs: an edit_mode import promotes the single edited
    file beside the source under the resolved silo state, never overwriting it."""
    ops = _ExportFakeOps()
    state = tmp_path / "silostate"
    (state / "docs").mkdir(parents=True)
    src = state / "docs" / "report.txt"
    src.write_text("ORIGINAL")
    ops.state_path = str(state)
    base = tmp_path / "staging"
    _stage_edit(base, source_realpath=str(src),
                payload_files={"out.txt": "EDITED BYTES"})

    receipt = _store(tmp_path, ops).import_from_disposable(TOKEN)

    assert receipt["mode"] == "edit"
    dest = Path(receipt["dest"])
    assert dest == state / "docs" / "report.txt.disp-edited"
    assert dest.read_text() == "EDITED BYTES"
    assert src.read_text() == "ORIGINAL"
    assert not (base / TOKEN).exists()  # one-shot staging removal


def test_import_edit_class_not_edit_capable_refused(tmp_path, export_class):
    """edit_mode staging but the class is export-capable yet NOT edit-capable
    (registry dropped edit) — refused fail-closed, staging kept."""
    ops = _ExportFakeOps()
    state = tmp_path / "silostate"
    (state / "d").mkdir(parents=True)
    src = state / "d" / "f.txt"
    src.write_text("O")
    ops.state_path = str(state)
    base = tmp_path / "staging"
    _stage_edit(base, source_realpath=str(src), payload_files={"o": "E"})
    with pytest.raises(BadState, match="not edit-capable"):
        _store(tmp_path, ops).import_from_disposable(TOKEN)
    assert (base / TOKEN).exists()


def test_import_edit_source_outside_state_refused(tmp_path, edit_class):
    """input_realpath that is NOT strictly under the resolved silo state is
    refused (no cross-silo edit write), staging kept."""
    ops = _ExportFakeOps()
    state = tmp_path / "silostate"
    state.mkdir()
    ops.state_path = str(state)
    outside = tmp_path / "elsewhere" / "f.txt"
    outside.parent.mkdir()
    outside.write_text("O")
    base = tmp_path / "staging"
    _stage_edit(base, source_realpath=str(outside), payload_files={"o": "E"})
    with pytest.raises(BadState, match="not under the request silo"):
        _store(tmp_path, ops).import_from_disposable(TOKEN)
    assert (base / TOKEN).exists()
    assert outside.read_text() == "O"  # untouched


def test_import_edit_missing_input_realpath_refused(tmp_path, edit_class):
    """edit_mode meta with no input_realpath is corrupt -> BadState, staging kept."""
    ops = _ExportFakeOps()
    state = tmp_path / "silostate"
    state.mkdir()
    ops.state_path = str(state)
    base = tmp_path / "staging"
    d = base / TOKEN
    (d / "payload").mkdir(parents=True)
    (d / "payload" / "o").write_text("E")
    (d / "meta.json").write_text(json.dumps({
        "version": 1, "launch_token": TOKEN, "request_silo": "work",
        "open_class": "agent-scratch", "container": "disp-x", "created": 1,
        "input_basename": "f.txt", "edit_mode": True}))  # no input_realpath
    with pytest.raises(BadState, match="input_realpath|source path"):
        _store(tmp_path, ops).import_from_disposable(TOKEN)
    assert (base / TOKEN).exists()


def test_import_edit_sibling_silo_prefix_not_confused(tmp_path, edit_class):
    """A source under a SIBLING silo dir sharing a name prefix (…/work2 vs …/work)
    must not pass the 'strictly under state' check."""
    ops = _ExportFakeOps()
    state = tmp_path / "work"
    state.mkdir()
    sibling = tmp_path / "work2"
    sibling.mkdir()
    src = sibling / "f.txt"
    src.write_text("O")
    ops.state_path = str(state)
    base = tmp_path / "staging"
    _stage_edit(base, source_realpath=str(src), payload_files={"o": "E"})
    with pytest.raises(BadState, match="not under the request silo"):
        _store(tmp_path, ops).import_from_disposable(TOKEN)
