"""Tests for qdistro_backup_manifest — signed, hash-chained backup
manifests (06-backup-dr §3.1-3.2). Uses real ssh-keygen -Y signing over a
throwaway ed25519 key."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SNAP_DIR = REPO_ROOT / "snapshots"


def _load(label: str, path: Path):
    spec = importlib.util.spec_from_file_location(label, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[label] = mod
    spec.loader.exec_module(mod)
    return mod


mf = _load("qdistro_backup_manifest", SNAP_DIR / "qdistro_backup_manifest.py")


@pytest.fixture
def signing_key(tmp_path):
    """A throwaway ed25519 keypair + an allowed_signers file pinning it to
    a fixed identity."""
    key = tmp_path / "backup-sign"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "backup@qdistro",
         "-f", str(key)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pub = (tmp_path / "backup-sign.pub").read_text().strip()
    identity = "owner@qdistro"
    allowed = tmp_path / "allowed_signers"
    # allowed_signers line: <identity> <keytype> <key>
    allowed.write_text(f"{identity} {pub}\n")
    return {"key": str(key), "allowed": str(allowed), "identity": identity}


def _entry(subvol="home", blob="home-1.btrfs.age", data=b"x", parent=None):
    return mf.build_entry(subvol, blob, mf.sha256_hex(data), len(data), parent)


# ---- build / canonicalise --------------------------------------------

def test_canonical_bytes_order_independent():
    a = mf.build_manifest(0, "host", 100, [_entry()], None)
    b = {  # same content, different key insertion order
        "entries": a["entries"], "host_id": "host", "prev_manifest_sha256": None,
        "created_at": 100, "seq": 0, "version": mf.MANIFEST_VERSION,
    }
    assert mf.manifest_canonical_bytes(a) == mf.manifest_canonical_bytes(b)


def test_build_entry_rejects_bad_sha():
    with pytest.raises(mf.ManifestError):
        mf.build_entry("home", "b", "nothex", 1, None)


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ".", ".hidden",
                                 "/abs", "back\\slash"])
def test_build_entry_rejects_path_traversal_blob(bad):
    good = mf.sha256_hex(b"x")
    with pytest.raises(mf.ManifestError):
        mf.build_entry("home", bad, good, 1, None)
    with pytest.raises(mf.ManifestError):
        mf.build_entry(bad, "blob", good, 1, None)  # subvol too


def test_parse_manifest_type_checks_raise_manifest_error():
    import json as _json
    # entries with a missing field -> ManifestError, not KeyError
    bad = _json.dumps({"version": 1, "seq": 0, "created_at": 1,
                       "host_id": "h", "entries": [{}],
                       "prev_manifest_sha256": None})
    with pytest.raises(mf.ManifestError):
        mf.parse_manifest(bad)
    # seq as a string -> ManifestError, not a later TypeError
    bad2 = _json.dumps({"version": 1, "seq": "0", "created_at": 1,
                        "host_id": "h", "entries": [], "prev_manifest_sha256": None})
    with pytest.raises(mf.ManifestError):
        mf.parse_manifest(bad2)
    # a path-traversal blob smuggled into a parsed manifest is rejected
    bad3 = _json.dumps({"version": 1, "seq": 0, "created_at": 1, "host_id": "h",
                        "entries": [{"subvol": "home", "blob": "../etc/x",
                                     "sha256": mf.sha256_hex(b"x"), "size": 1,
                                     "parent_blob": None}],
                        "prev_manifest_sha256": None})
    with pytest.raises(mf.ManifestError):
        mf.parse_manifest(bad3)


# ---- signing ---------------------------------------------------------

def test_sign_then_verify_roundtrip(signing_key):
    m = mf.build_manifest(0, "host", 100, [_entry()], None)
    canon = mf.manifest_canonical_bytes(m)
    sig = mf.sign_manifest(canon, signing_key["key"])
    assert "BEGIN SSH SIGNATURE" in sig
    assert mf.verify_signature(canon, sig, signing_key["allowed"],
                               signing_key["identity"]) is True


def test_verify_rejects_tampered_payload(signing_key):
    m = mf.build_manifest(0, "host", 100, [_entry()], None)
    sig = mf.sign_manifest(mf.manifest_canonical_bytes(m), signing_key["key"])
    # Flip a byte in the signed-over content (e.g. a blob substitution).
    m["entries"][0]["blob"] = "evil.btrfs.age"
    tampered = mf.manifest_canonical_bytes(m)
    assert mf.verify_signature(tampered, sig, signing_key["allowed"],
                               signing_key["identity"]) is False


def test_verify_rejects_wrong_identity(signing_key):
    m = mf.build_manifest(0, "host", 100, [_entry()], None)
    canon = mf.manifest_canonical_bytes(m)
    sig = mf.sign_manifest(canon, signing_key["key"])
    assert mf.verify_signature(canon, sig, signing_key["allowed"],
                               "attacker@elsewhere") is False


def test_verify_bad_signature_string_is_false_not_raise(signing_key):
    m = mf.build_manifest(0, "host", 100, [_entry()], None)
    canon = mf.manifest_canonical_bytes(m)
    assert mf.verify_signature(canon, "not a signature", signing_key["allowed"],
                               signing_key["identity"]) is False


# ---- chain -----------------------------------------------------------

def _chain():
    m0 = mf.build_manifest(
        0, "host", 100,
        [_entry("home", "home-0.age", b"base", None)], None)
    m1 = mf.build_manifest(
        1, "host", 200,
        [_entry("home", "home-1.age", b"inc1", "home-0.age")],
        mf.manifest_sha256(m0))
    m2 = mf.build_manifest(
        2, "host", 300,
        [_entry("home", "home-2.age", b"inc2", "home-1.age")],
        mf.manifest_sha256(m1))
    return [m0, m1, m2]


def test_valid_chain_passes():
    mf.verify_chain(_chain())  # no raise


def test_chain_detects_dropped_run_hash_break():
    c = _chain()
    del c[1]  # drop the middle run; m2.prev points at m1 which is gone
    with pytest.raises(mf.ManifestError, match="hash chain"):
        mf.verify_chain(c)


def test_chain_detects_seq_rollback():
    c = _chain()
    c[2]["seq"] = 1  # not strictly increasing vs c[1]
    with pytest.raises(mf.ManifestError, match="strictly increasing"):
        mf.verify_chain(c)


def test_chain_detects_dropped_incremental():
    # m1's incremental parent (home-0) never appeared: start the chain at an
    # incremental with no preceding full send.
    m0 = mf.build_manifest(
        0, "host", 100,
        [_entry("home", "home-1.age", b"inc", "home-0.age")], None)
    with pytest.raises(mf.ManifestError, match="not present"):
        mf.verify_chain([m0])


def test_first_manifest_must_have_null_prev():
    m0 = mf.build_manifest(0, "host", 100, [_entry()], "deadbeef")
    with pytest.raises(mf.ManifestError, match="must have prev"):
        mf.verify_chain([m0])


# ---- freshness -------------------------------------------------------

def test_freshness_blocks_rollback():
    newest = mf.build_manifest(3, "host", 100, [_entry()], None)
    mf.check_freshness(newest, 3)  # equal is OK
    with pytest.raises(mf.ManifestError, match="rollback"):
        mf.check_freshness(newest, 5)  # owner expected >= 5


# ---- blob verification ----------------------------------------------

def test_verify_blobs_detects_corruption(tmp_path):
    blob = tmp_path / "home-0.age"
    blob.write_bytes(b"original ciphertext")
    m = mf.build_manifest(
        0, "host", 100,
        [mf.build_entry("home", "home-0.age", mf.sha256_hex(b"original ciphertext"),
                        len(b"original ciphertext"), None)],
        None)
    assert mf.verify_blobs(m, str(tmp_path)) == []
    blob.write_bytes(b"corrupted!!!!!!!!!!")  # same length, different content
    problems = mf.verify_blobs(m, str(tmp_path))
    assert any("sha256 mismatch" in p for p in problems)


def test_verify_blobs_detects_missing(tmp_path):
    m = mf.build_manifest(
        0, "host", 100,
        [mf.build_entry("home", "gone.age", mf.sha256_hex(b"x"), 1, None)], None)
    assert any("missing" in p for p in mf.verify_blobs(m, str(tmp_path)))


# ---- restore order ---------------------------------------------------

def test_restore_order_full_then_incrementals():
    order = mf.restore_order(_chain(), "home")
    assert [e["blob"] for e in order] == ["home-0.age", "home-1.age", "home-2.age"]
    assert order[0]["parent_blob"] is None


def test_restore_order_rejects_no_full_send():
    m0 = mf.build_manifest(
        0, "host", 100,
        [_entry("home", "home-1.age", b"inc", "home-0.age")], None)
    with pytest.raises(mf.ManifestError, match="full send"):
        mf.restore_order([m0], "home")
