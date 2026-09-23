"""Tests for the optional content-signing verify primitive (F1/F9).

Uses a THROWAWAY ephemeral GPG key generated inside a temp GNUPGHOME per session
and torn down automatically. No real release private key is ever generated or
handled — that is a human-custody operation, and the v1 key does not exist yet.

Coverage (per the design GO/NO-GO):
  - default unsigned (no signing.json) -> no-op, no regression (F1 + F9)
  - enforced + good content/manifest/sig/correct key -> accepted
  - enforced + tampered file (digest mismatch) -> refused
  - enforced + corrupt/bad sig -> refused
  - enforced + missing manifest entry / extra file on disk -> refused
  - enforced + wrong key (sig by a different key than trusted fpr) -> refused
  - enforced + gpgv missing -> refused (not silently skipped)
  - enforced + malformed manifest line -> refused (no parse-fail-open)
  - malformed signing.json / bad version / short fpr / relative+missing keyring
    -> fail closed (TrustConfigError), never silent unsigned fallback
  - symlink / non-regular file under content dir -> refused
  - F9 install-plugin: enforced accepts good, refuses tampered, leaves no dest
  - F1 verify-scheme: unsigned no-op exit 0
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parents[1]
SV_PATH = HERE / "Scripts" / "python" / "src" / "plugins" / "signing_verify.py"
HELPER_PATH = HERE / "Scripts" / "python" / "src" / "plugins" / "plugin-helper.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sv = _load("signing_verify", SV_PATH)
helper = _load("plugin_helper", HELPER_PATH)


pytestmark = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgv") is None,
    reason="gpg/gpgv required for signing tests",
)


# --------------------------------------------------------------------------- #
# Ephemeral throwaway keys
# --------------------------------------------------------------------------- #
class _Key:
    def __init__(self, gnupghome: Path, fpr: str, keyring: Path):
        self.gnupghome = gnupghome
        self.fpr = fpr
        self.keyring = keyring


def _gpg(gnupghome: Path, *args: str, input_bytes: bytes | None = None):
    env = os.environ.copy()
    env["GNUPGHOME"] = str(gnupghome)
    return subprocess.run(
        ["gpg", "--batch", "--no-tty", "--yes", *args],
        env=env, input=input_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    )


def _make_key(tmp_path: Path, name: str) -> _Key:
    gnupghome = tmp_path / ("gnupg-" + name)
    gnupghome.mkdir(mode=0o700)
    params = (
        "%no-protection\n"
        "Key-Type: eddsa\nKey-Curve: ed25519\n"
        "Key-Usage: sign\n"
        f"Name-Real: qdshell test {name}\n"
        f"Name-Email: {name}@test.invalid\n"
        "Expire-Date: 0\n%commit\n"
    ).encode()
    _gpg(gnupghome, "--gen-key", input_bytes=params)
    out = _gpg(gnupghome, "--list-keys", "--with-colons").stdout.decode()
    fpr = None
    for line in out.splitlines():
        if line.startswith("fpr:"):
            fpr = line.split(":")[9]
            break
    assert fpr and len(fpr) == 40
    keyring = gnupghome / "pub.gpg"
    keyring.write_bytes(_gpg(gnupghome, "--export", fpr).stdout)
    return _Key(gnupghome, fpr, keyring)


@pytest.fixture(scope="module")
def key(tmp_path_factory) -> _Key:
    return _make_key(tmp_path_factory.mktemp("k1"), "k1")


@pytest.fixture(scope="module")
def other_key(tmp_path_factory) -> _Key:
    return _make_key(tmp_path_factory.mktemp("k2"), "k2")


# --------------------------------------------------------------------------- #
# Helpers to build a signed content dir
# --------------------------------------------------------------------------- #
def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _build_content(d: Path, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _write_manifest(d: Path, files: dict[str, bytes]) -> Path:
    lines = "".join(f"{_sha256(data)}  {rel}\n" for rel, data in sorted(files.items()))
    m = d / sv.MANIFEST_NAME
    m.write_text(lines, encoding="utf-8")
    return m


def _sign(key: _Key, manifest: Path) -> Path:
    sig = manifest.parent / sv.SIGNATURE_NAME
    _gpg(key.gnupghome, "--detach-sign", "--local-user", key.fpr,
         "-o", str(sig), str(manifest))
    return sig


def _signed_dir(tmp: Path, key: _Key, files: dict[str, bytes]) -> Path:
    d = tmp
    d.mkdir(parents=True, exist_ok=True)
    _build_content(d, files)
    m = _write_manifest(d, files)
    _sign(key, m)
    return d


def _trust(key: _Key) -> dict:
    return {"fingerprint": key.fpr.lower(), "keyringPath": str(key.keyring)}


def _write_config(path: Path, key: _Key | None, version=1, bad_keyring=None):
    if key is None:
        doc = {"version": version, "trustedKey": None}
    else:
        kr = bad_keyring if bad_keyring is not None else str(key.keyring)
        doc = {"version": version, "trustedKey": {"fingerprint": key.fpr, "keyringPath": kr}}
    path.write_text(json.dumps(doc), encoding="utf-8")


GOOD = {"colors.json": b'{"bg":"#000"}', "sub/extra.txt": b"hello"}


# --------------------------------------------------------------------------- #
# verify_dir — happy + negative
# --------------------------------------------------------------------------- #
def test_good_content_accepted(tmp_path, key):
    d = _signed_dir(tmp_path / "c", key, GOOD)
    sv.verify_dir(d, _trust(key))  # no raise


def test_tampered_file_refused(tmp_path, key):
    d = _signed_dir(tmp_path / "c", key, GOOD)
    (d / "colors.json").write_bytes(b'{"bg":"#fff"}')  # change after signing
    with pytest.raises(sv.VerifyError, match="digest mismatch"):
        sv.verify_dir(d, _trust(key))


def test_corrupt_sig_refused(tmp_path, key):
    d = _signed_dir(tmp_path / "c", key, GOOD)
    sig = d / sv.SIGNATURE_NAME
    sig.write_bytes(sig.read_bytes()[:-4] + b"\x00\x00\x00\x00")
    with pytest.raises(sv.VerifyError):
        sv.verify_dir(d, _trust(key))


def test_missing_sig_refused(tmp_path, key):
    d = _signed_dir(tmp_path / "c", key, GOOD)
    (d / sv.SIGNATURE_NAME).unlink()
    with pytest.raises(sv.VerifyError, match="missing detached signature"):
        sv.verify_dir(d, _trust(key))


def test_missing_manifest_entry_refused(tmp_path, key):
    """File on disk not covered by the manifest -> extra file -> refuse."""
    d = _signed_dir(tmp_path / "c", key, GOOD)
    (d / "rogue.txt").write_bytes(b"injected")  # add after signing
    with pytest.raises(sv.VerifyError, match="not in manifest"):
        sv.verify_dir(d, _trust(key))


def test_manifest_entry_missing_on_disk_refused(tmp_path, key):
    """Manifest declares a file that does not exist on disk -> refuse."""
    files = dict(GOOD)
    d = tmp_path / "c"
    d.mkdir(parents=True)
    _build_content(d, files)
    files_with_ghost = dict(files)
    files_with_ghost["ghost.txt"] = b"never written"
    m = _write_manifest(d, files_with_ghost)
    _sign(key, m)
    with pytest.raises(sv.VerifyError, match="missing on disk"):
        sv.verify_dir(d, _trust(key))


def test_wrong_key_refused_unknown_key(tmp_path, key, other_key):
    """Manifest signed by an UNKNOWN key (not in the trusted keyring) -> gpgv
    rejects outright. Fail closed."""
    d = tmp_path / "c"
    d.mkdir(parents=True)
    _build_content(d, GOOD)
    m = _write_manifest(d, GOOD)
    _sign(other_key, m)  # signed by a key absent from key.keyring
    with pytest.raises(sv.VerifyError):
        sv.verify_dir(d, _trust(key))


def test_wrong_key_refused_fingerprint_binding(tmp_path, key, other_key):
    """Multi-key keyring: gpgv ACCEPTS other_key's sig, but the configured trust
    pins `key`'s fingerprint -> our VALIDSIG binding rejects it. This proves the
    fingerprint bind is not merely deferring to the keyring's accept/reject."""
    d = tmp_path / "c"
    d.mkdir(parents=True)
    _build_content(d, GOOD)
    m = _write_manifest(d, GOOD)
    _sign(other_key, m)
    # Build a keyring that contains BOTH keys so gpgv accepts the sig.
    both = tmp_path / "both.gpg"
    both.write_bytes(key.keyring.read_bytes() + other_key.keyring.read_bytes())
    trust = {"fingerprint": key.fpr.lower(), "keyringPath": str(both)}
    with pytest.raises(sv.VerifyError, match="not the configured trusted key"):
        sv.verify_dir(d, trust)


def test_gpgv_missing_refused(tmp_path, key, monkeypatch):
    # _gpgv_verify does `import shutil; shutil.which("gpgv")`; patch the global module.
    d = _signed_dir(tmp_path / "c", key, GOOD)
    monkeypatch.setattr(shutil, "which", lambda _x: None)
    with pytest.raises(sv.VerifyError, match="gpgv not found"):
        sv.verify_dir(d, _trust(key))


def test_malformed_manifest_line_refused(tmp_path, key):
    d = tmp_path / "c"
    d.mkdir(parents=True)
    _build_content(d, GOOD)
    (d / sv.MANIFEST_NAME).write_text("not a valid manifest line\n", encoding="utf-8")
    _sign(key, d / sv.MANIFEST_NAME)
    with pytest.raises(sv.VerifyError, match="malformed manifest line"):
        sv.verify_dir(d, _trust(key))


def test_manifest_with_traversal_path_refused(tmp_path, key):
    d = tmp_path / "c"
    d.mkdir(parents=True)
    _build_content(d, {"ok.txt": b"x"})
    line = f"{_sha256(b'x')}  ../escape.txt\n"
    (d / sv.MANIFEST_NAME).write_text(line, encoding="utf-8")
    _sign(key, d / sv.MANIFEST_NAME)
    with pytest.raises(sv.VerifyError, match="unsafe segment"):
        sv.verify_dir(d, _trust(key))


def test_symlink_in_content_refused(tmp_path, key):
    d = _signed_dir(tmp_path / "c", key, GOOD)
    # Add a symlink AND its manifest entry so set-equality alone wouldn't catch it.
    target = d / "colors.json"
    link = d / "link.json"
    link.symlink_to(target)
    files = dict(GOOD)
    files["link.json"] = b'{"bg":"#000"}'
    m = _write_manifest(d, files)
    _sign(key, m)
    with pytest.raises(sv.VerifyError, match="non-regular file"):
        sv.verify_dir(d, _trust(key))


# --------------------------------------------------------------------------- #
# Trust config — fail closed
# --------------------------------------------------------------------------- #
def test_absent_config_is_unsigned(tmp_path):
    assert sv.load_trust_config(tmp_path / "nope.json") is None
    assert sv.is_enforced(tmp_path / "nope.json") is False


def test_explicit_null_trustedkey_is_unsigned(tmp_path, key):
    cfg = tmp_path / "signing.json"
    _write_config(cfg, None)
    assert sv.load_trust_config(cfg) is None


def test_bad_json_fails_closed(tmp_path):
    cfg = tmp_path / "signing.json"
    cfg.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(sv.TrustConfigError, match="valid JSON"):
        sv.load_trust_config(cfg)


def test_unsupported_version_fails_closed(tmp_path, key):
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key, version=99)
    with pytest.raises(sv.TrustConfigError, match="unsupported version"):
        sv.load_trust_config(cfg)


def test_short_fingerprint_fails_closed(tmp_path, key):
    cfg = tmp_path / "signing.json"
    cfg.write_text(json.dumps({
        "version": 1,
        "trustedKey": {"fingerprint": key.fpr[:16], "keyringPath": str(key.keyring)},
    }), encoding="utf-8")
    with pytest.raises(sv.TrustConfigError, match="40-hex"):
        sv.load_trust_config(cfg)


def test_relative_keyring_fails_closed(tmp_path, key):
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key, bad_keyring="relative/keyring.gpg")
    with pytest.raises(sv.TrustConfigError, match="absolute path"):
        sv.load_trust_config(cfg)


def test_missing_keyring_fails_closed(tmp_path, key):
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key, bad_keyring="/nonexistent/keyring.gpg")
    with pytest.raises(sv.TrustConfigError, match="not found"):
        sv.load_trust_config(cfg)


def test_well_formed_config_enforced(tmp_path, key):
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    trust = sv.load_trust_config(cfg)
    assert trust is not None
    assert trust["fingerprint"] == key.fpr.lower()
    assert sv.is_enforced(cfg) is True


# --------------------------------------------------------------------------- #
# maybe_verify_dir — the mode gate used by the helper
# --------------------------------------------------------------------------- #
def test_maybe_verify_unsigned_is_noop(tmp_path, key):
    # Even if a dir would fail verification, unsigned mode never verifies.
    d = _signed_dir(tmp_path / "c", key, GOOD)
    (d / "colors.json").write_bytes(b"tampered")
    assert sv.maybe_verify_dir(d, tmp_path / "absent.json") is False


def test_maybe_verify_enforced_good(tmp_path, key):
    d = _signed_dir(tmp_path / "c", key, GOOD)
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    assert sv.maybe_verify_dir(d, cfg) is True


def test_maybe_verify_enforced_tampered_raises(tmp_path, key):
    d = _signed_dir(tmp_path / "c", key, GOOD)
    (d / "colors.json").write_bytes(b"tampered")
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    with pytest.raises(sv.VerifyError):
        sv.maybe_verify_dir(d, cfg)


# --------------------------------------------------------------------------- #
# F1 verify-scheme CLI
# --------------------------------------------------------------------------- #
def test_verify_scheme_unsigned_noop(tmp_path, key, monkeypatch):
    d = _signed_dir(tmp_path / "scheme", key, GOOD)
    (d / "colors.json").write_bytes(b"tampered")  # would fail IF enforced
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(tmp_path / "absent.json"))
    assert helper.verify_scheme(str(d)) == 0  # no-op, current behavior


def test_verify_scheme_enforced_good(tmp_path, key, monkeypatch):
    d = _signed_dir(tmp_path / "scheme", key, GOOD)
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    assert helper.main(["verify-scheme", str(d)]) == 0


def test_verify_scheme_enforced_tampered_exit_nonzero(tmp_path, key, monkeypatch):
    d = _signed_dir(tmp_path / "scheme", key, GOOD)
    (d / "colors.json").write_bytes(b"tampered")
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    assert helper.main(["verify-scheme", str(d)]) == 1  # fail closed via CLI


def test_verify_scheme_malformed_config_exit_nonzero(tmp_path, monkeypatch):
    d = tmp_path / "scheme"
    d.mkdir()
    cfg = tmp_path / "signing.json"
    cfg.write_text("{bad", encoding="utf-8")
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    assert helper.main(["verify-scheme", str(d)]) == 1  # never silent unsigned


# --------------------------------------------------------------------------- #
# F9 install-plugin — verify the FINAL destination
# --------------------------------------------------------------------------- #
def _fake_clone(plugin_id: str, key: _Key, files: dict[str, bytes], sign=True,
                sign_key: _Key | None = None):
    """Return a fake _clone_sparse that materializes a signed plugin tree."""
    def _clone_sparse(repo_url, checkout, temp_dir):
        src = Path(temp_dir) / plugin_id
        src.mkdir(parents=True)
        _build_content(src, files)
        if sign:
            m = _write_manifest(src, files)
            _sign(sign_key or key, m)
    return _clone_sparse


PLUGIN_FILES = {"manifest.json": b'{"id":"safe"}', "main.qml": b"import QtQuick\n"}


def test_install_plugin_unsigned_default(tmp_path, key, monkeypatch):
    dest = tmp_path / "plugins" / "safe"
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(helper, "_clone_sparse",
                        _fake_clone("safe", key, PLUGIN_FILES, sign=False))
    assert helper.install_plugin("https://example.test/r.git", "safe", str(dest)) == 0
    assert (dest / "main.qml").is_file()


def test_install_plugin_enforced_good(tmp_path, key, monkeypatch):
    dest = tmp_path / "plugins" / "safe"
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    monkeypatch.setattr(helper, "_clone_sparse",
                        _fake_clone("safe", key, PLUGIN_FILES))
    assert helper.install_plugin("https://example.test/r.git", "safe", str(dest)) == 0
    assert (dest / "main.qml").is_file()


def test_install_plugin_enforced_tampered_destination_leaves_nothing(tmp_path, key, monkeypatch):
    """A clone signed by an untrusted key must be refused and the dest removed
    (fail closed, no unverified bytes left where they would load as live QML)."""
    dest = tmp_path / "plugins" / "safe"
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    evil = _make_key(tmp_path, "evil")
    monkeypatch.setattr(helper, "_clone_sparse",
                        _fake_clone("safe", key, PLUGIN_FILES, sign_key=evil))
    # The helper loads its own signing_verify instance, so catch THAT class.
    with pytest.raises(helper.signing_verify.VerifyError):
        helper.install_plugin("https://example.test/r.git", "safe", str(dest))
    assert not dest.exists()  # destination cleaned up


def test_install_plugin_enforced_preserves_user_settings(tmp_path, key, monkeypatch):
    """User-local settings.json is preserved AND excluded from the signed set."""
    dest = tmp_path / "plugins" / "safe"
    dest.mkdir(parents=True)
    (dest / "settings.json").write_text('{"keep":true}', encoding="utf-8")
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    monkeypatch.setattr(helper, "_clone_sparse",
                        _fake_clone("safe", key, PLUGIN_FILES))
    assert helper.install_plugin("https://example.test/r.git", "safe", str(dest)) == 0
    assert (dest / "settings.json").read_text(encoding="utf-8") == '{"keep":true}'
    assert (dest / "main.qml").is_file()


def test_install_plugin_enforced_upgrade_clears_stale_files(tmp_path, key, monkeypatch):
    """Enforced upgrade over a dest with a STALE file from a prior version must
    succeed: the stale file is cleared (preserving settings.json) so set-equality
    against the new signed manifest passes, instead of bricking + wiping the dir."""
    dest = tmp_path / "plugins" / "safe"
    dest.mkdir(parents=True)
    (dest / "settings.json").write_text('{"keep":true}', encoding="utf-8")
    (dest / "stale_old_version.qml").write_text("import QtQuick\n", encoding="utf-8")
    (dest / "sub").mkdir()
    (dest / "sub" / "gone.txt").write_text("removed in new version", encoding="utf-8")
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    monkeypatch.setattr(helper, "_clone_sparse",
                        _fake_clone("safe", key, PLUGIN_FILES))
    assert helper.install_plugin("https://example.test/r.git", "safe", str(dest)) == 0
    assert (dest / "settings.json").read_text(encoding="utf-8") == '{"keep":true}'
    assert (dest / "main.qml").is_file()
    assert not (dest / "stale_old_version.qml").exists()
    assert not (dest / "sub").exists()


def test_install_plugin_symlinked_dest_refused_target_untouched(tmp_path, key, monkeypatch):
    """If dest is a symlink to another dir, install must refuse BEFORE any cleanup
    so it can never delete/overwrite files outside the intended plugin dir."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("do not touch", encoding="utf-8")
    dest = tmp_path / "plugins" / "safe"
    dest.parent.mkdir(parents=True)
    dest.symlink_to(victim, target_is_directory=True)
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    monkeypatch.setattr(helper, "_clone_sparse",
                        _fake_clone("safe", key, PLUGIN_FILES))
    with pytest.raises(helper.signing_verify.VerifyError, match="not a real directory"):
        helper.install_plugin("https://example.test/r.git", "safe", str(dest))
    # The symlink target's contents are completely untouched.
    assert (victim / "important.txt").read_text(encoding="utf-8") == "do not touch"
    assert not (victim / "main.qml").exists()


def test_install_plugin_enforced_bad_upgrade_keeps_old_and_no_staging(tmp_path, key, monkeypatch):
    """A failed (untrusted-key) upgrade over an existing plugin must NOT wipe the
    existing dir, and must leave no .staging-* leftovers (atomic-swap fail-closed)."""
    dest = tmp_path / "plugins" / "safe"
    dest.mkdir(parents=True)
    (dest / "main.qml").write_text("OLD GOOD\n", encoding="utf-8")
    (dest / "settings.json").write_text('{"keep":true}', encoding="utf-8")
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    evil = _make_key(tmp_path, "evil3")
    monkeypatch.setattr(helper, "_clone_sparse",
                        _fake_clone("safe", key, PLUGIN_FILES, sign_key=evil))
    with pytest.raises(helper.signing_verify.VerifyError):
        helper.install_plugin("https://example.test/r.git", "safe", str(dest))
    # Old plugin untouched; no staging leftovers next to it.
    assert (dest / "main.qml").read_text(encoding="utf-8") == "OLD GOOD\n"
    leftovers = [p.name for p in (tmp_path / "plugins").iterdir() if p.name.startswith(".staging-")]
    assert leftovers == []


def test_install_plugin_main_cli_tampered_returns_1(tmp_path, key, monkeypatch):
    dest = tmp_path / "plugins" / "safe"
    cfg = tmp_path / "signing.json"
    _write_config(cfg, key)
    monkeypatch.setenv("QDSHELL_SIGNING_FILE", str(cfg))
    other = _make_key(tmp_path, "evil2")
    monkeypatch.setattr(helper, "_clone_sparse",
                        _fake_clone("safe", key, PLUGIN_FILES, sign_key=other))
    assert helper.main(["install-plugin", "https://example.test/r.git", "safe", str(dest)]) == 1
