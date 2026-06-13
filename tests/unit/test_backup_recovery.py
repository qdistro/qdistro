"""Unit coverage for the vault-recovery COLLECTOR (06-backup-dr §3.4 / §2c).

The security property under test: EXACTLY the intended recovery material is
collected, and NO secret can leak in. validate_recovery_input is the fail-closed
gate; materialise_recovery folds the (validated) inputs + generated docs into the
collector's recovery/ subdir. The daily service never touches the recovery
passphrase or the live vault master key — it only copies an ALREADY-EXPORTED
encrypted bundle + PUBLIC files and writes static docs.
"""
from __future__ import annotations

import importlib.util
import json
import os
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


_load("qdistro_backup_manifest", SNAP_DIR / "qdistro_backup_manifest.py")
_load("qdistro_backup_cli", SNAP_DIR / "qdistro_backup_cli.py")
rec = _load("qdistro_backup_recovery", SNAP_DIR / "qdistro_backup_recovery.py")
svc = _load("qdistro_backup_service", SNAP_DIR / "qdistro_backup_service.py")


# A real-shaped (structurally valid) recovery bundle — no real crypto needed
# for the structural gate.
GOOD_BUNDLE = {
    "version": 1, "label": "qdistro-vault-recovery", "created": 1700000000,
    "kdf": {"alg": "scrypt", "n": 1 << 17, "r": 8, "p": 1, "salt": "AAAA"},
    "aead": {"alg": "AES-256-GCM", "nonce": "BBBB", "ciphertext": "CCCC"},
}
GOOD_ALLOWED = "owner@example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAtest comment\n"
GOOD_RECIPIENTS = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqtest\n"


def _w(tmp_path, name, body, mode=0o600):
    p = tmp_path / name
    if isinstance(body, (dict, list)):
        body = json.dumps(body)
    p.write_text(body)
    os.chmod(p, mode)
    return str(p)


class TestValidateAccepts:
    # The test files are owned by the running uid (not root), so the accept
    # cases pass service_owner_uid=os.getuid(); the wrong-owner refusal is its
    # own test below.
    UID = os.getuid()

    def test_good_recovery_bundle(self, tmp_path):
        p = _w(tmp_path, "b.json", GOOD_BUNDLE)
        data = rec.validate_recovery_input(p, "recovery_bundle",
                                           service_owner_uid=self.UID)
        assert json.loads(data)["version"] == 1

    def test_good_allowed_signers(self, tmp_path):
        p = _w(tmp_path, "as", GOOD_ALLOWED)
        assert rec.validate_recovery_input(
            p, "allowed_signers", service_owner_uid=self.UID) == \
            GOOD_ALLOWED.encode()

    def test_good_recipients_age_and_ssh(self, tmp_path):
        body = GOOD_RECIPIENTS + "ssh-ed25519 AAAAC3pub host\n# a comment\n"
        p = _w(tmp_path, "rc", body)
        assert rec.validate_recovery_input(
            p, "recipients", service_owner_uid=self.UID) == body.encode()

    def test_returns_exact_bytes_for_copy(self, tmp_path):
        """The bytes returned == the file bytes (so validate and copy can't
        diverge — the no-TOCTOU property)."""
        p = _w(tmp_path, "b.json", GOOD_BUNDLE)
        with open(p, "rb") as f:
            assert rec.validate_recovery_input(
                p, "recovery_bundle", service_owner_uid=self.UID) == f.read()


class TestValidateRefusesSecrets:
    @pytest.mark.parametrize("marker", [
        "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n",
        "-----BEGIN PRIVATE KEY-----\nx\n",
        "-----BEGIN RSA PRIVATE KEY-----\nx\n",
        "AGE-SECRET-KEY-1QQQQQQQQQ\n",
    ])
    def test_private_key_marker_refused_for_every_kind(self, tmp_path, marker):
        # Any kind: a file that *looks like a private key* must be refused.
        for kind in ("recovery_bundle", "allowed_signers", "recipients"):
            p = _w(tmp_path, f"secret-{kind}", marker)
            with pytest.raises(rec.RecoveryCollectError):
                rec.validate_recovery_input(p, kind)

    def test_age_secret_key_smuggled_in_recipients_refused(self, tmp_path):
        body = GOOD_RECIPIENTS + "AGE-SECRET-KEY-1LEAKED\n"
        p = _w(tmp_path, "rc", body)
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(p, "recipients")

    def test_symlink_refused(self, tmp_path):
        real = _w(tmp_path, "real.json", GOOD_BUNDLE)
        link = tmp_path / "link.json"
        link.symlink_to(real)
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(str(link), "recovery_bundle")

    def test_group_or_world_writable_refused(self, tmp_path):
        p = _w(tmp_path, "b.json", GOOD_BUNDLE, mode=0o666)
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(p, "recovery_bundle")

    def test_wrong_owner_refused(self, tmp_path, monkeypatch):
        # The current test uid is neither 0 nor a configured owner -> refuse.
        p = _w(tmp_path, "b.json", GOOD_BUNDLE)
        real_uid = os.stat(p).st_uid
        # default: only root(0) accepted -> a non-root-owned file is refused
        if real_uid != 0:
            with pytest.raises(rec.RecoveryCollectError):
                rec.validate_recovery_input(p, "recovery_bundle")
        # but accepted when the configured service_owner_uid matches
        data = rec.validate_recovery_input(
            p, "recovery_bundle", service_owner_uid=real_uid)
        assert json.loads(data)["version"] == 1

    def test_non_regular_file_refused(self, tmp_path):
        d = tmp_path / "adir"
        d.mkdir()
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(str(d), "recovery_bundle",
                                        service_owner_uid=os.getuid())


class TestValidateRejectsWrongFormat:
    def test_bundle_must_be_qdistro_recovery_shape(self, tmp_path):
        # a different JSON file mis-pointed at the bundle slot is refused
        p = _w(tmp_path, "b.json", {"hello": "world"})
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(p, "recovery_bundle",
                                        service_owner_uid=os.getuid())

    def test_bundle_wrong_version_refused(self, tmp_path):
        bad = dict(GOOD_BUNDLE, version=99)
        p = _w(tmp_path, "b.json", bad)
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(p, "recovery_bundle",
                                        service_owner_uid=os.getuid())

    def test_bundle_not_json_refused(self, tmp_path):
        p = _w(tmp_path, "b.json", "not json at all {[")
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(p, "recovery_bundle",
                                        service_owner_uid=os.getuid())

    @pytest.mark.parametrize("section,field", [
        ("kdf", "salt"), ("aead", "nonce"), ("aead", "ciphertext")])
    def test_bundle_empty_crypto_field_refused(self, tmp_path, section, field):
        # a hollowed-out bundle (empty salt/nonce/ciphertext) is useless
        # recovery material and must be refused, not shipped as the escape hatch
        import copy
        bad = copy.deepcopy(GOOD_BUNDLE)
        bad[section][field] = ""
        p = _w(tmp_path, "b.json", bad)
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(p, "recovery_bundle",
                                        service_owner_uid=os.getuid())

    def test_allowed_signers_garbage_line_refused(self, tmp_path):
        p = _w(tmp_path, "as", "this is not a signer line\n")
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(p, "allowed_signers",
                                        service_owner_uid=os.getuid())

    def test_recipients_garbage_line_refused(self, tmp_path):
        p = _w(tmp_path, "rc", "neither-age-nor-ssh\n")
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(p, "recipients",
                                        service_owner_uid=os.getuid())

    def test_empty_public_file_refused(self, tmp_path):
        for kind, name in (("allowed_signers", "as"), ("recipients", "rc")):
            p = _w(tmp_path, name, "# only a comment\n")
            with pytest.raises(rec.RecoveryCollectError):
                rec.validate_recovery_input(p, kind,
                                            service_owner_uid=os.getuid())

    def test_unknown_kind_refused(self, tmp_path):
        p = _w(tmp_path, "x", "whatever")
        with pytest.raises(rec.RecoveryCollectError):
            rec.validate_recovery_input(p, "bogus_kind",
                                        service_owner_uid=os.getuid())


class TestRedactedConfig:
    def test_omits_sign_key_entirely(self):
        cfg = {"host_id": "h1", "remote": "u@nas:/b",
               "recipients": "/etc/qdistro/recips.txt",
               "sign_key": "/etc/qdistro/SECRET-sign-ed25519",
               "sign_identity": "owner@x",
               "allowed_signers": "/etc/qdistro/as",
               "subvols": [{"name": "data", "collector": False,
                            "source": "/home/silos/alice"},
                           {"name": "meta", "collector": True,
                            "paths": ["/etc/qdistro"]}]}
        text = rec.redacted_config_text(cfg)
        # the secret signing key PATH/VALUE must NOT appear anywhere (the doc's
        # own "sign_key ... OMITTED" note legitimately names the field).
        assert "SECRET-sign-ed25519" not in text
        assert cfg["sign_key"] not in text
        # but the non-secret layout IS echoed
        assert "h1" in text and "u@nas:/b" in text
        assert "owner@x" in text and "/etc/qdistro/as" in text
        assert "data" in text and "meta" in text


class TestMaterialise:
    """The end-to-end fold: exactly the intended files + docs land, the secrets
    do not, and a mispointed secret aborts the whole thing."""

    def _cfg(self):
        return {"host_id": "h1", "remote": "/srv/b", "recipients": "/r",
                "sign_key": "/SECRET", "sign_identity": "owner@x",
                "allowed_signers": "/as",
                "subvols": [{"name": "metadata", "collector": True,
                             "paths": ["/etc/qdistro"]}]}

    def test_materialise_collects_exactly_the_intended_set(self, tmp_path):
        bundle = _w(tmp_path, "vault-recovery.json", GOOD_BUNDLE)
        allowed = _w(tmp_path, "as", GOOD_ALLOWED)
        recips = _w(tmp_path, "rc", GOOD_RECIPIENTS)
        dest = tmp_path / "collect"
        dest.mkdir()
        rec_cfg = {"collector": "metadata", "bundle": bundle,
                   "allowed_signers": allowed, "recipients": recips,
                   "service_owner_uid": os.getuid()}
        svc.materialise_recovery(rec_cfg, self._cfg(), str(dest))
        rdir = dest / "recovery"
        present = sorted(os.listdir(rdir))
        assert present == sorted([
            "RESTORE-RUNBOOK.txt", "allowed_signers", "config-redacted.txt",
            "manifest-schema.txt", "recipients", "vault-recovery.json"])
        # the copied bundle is byte-identical to the validated source
        assert (rdir / "vault-recovery.json").read_text() == \
            json.dumps(GOOD_BUNDLE)
        # NO secret leaked: the sign_key path is absent from every doc
        for f in present:
            assert "/SECRET" not in (rdir / f).read_text()
        # runbook states the allowed_signers copy is documentation-only
        runbook = (rdir / "RESTORE-RUNBOOK.txt").read_text()
        assert "DOCUMENTATION ONLY" in runbook

    def test_materialise_aborts_if_an_input_is_a_secret(self, tmp_path):
        # operator mis-points bundle at the PRIVATE signing key
        leak = _w(tmp_path, "sign", "-----BEGIN OPENSSH PRIVATE KEY-----\nx\n")
        allowed = _w(tmp_path, "as", GOOD_ALLOWED)
        dest = tmp_path / "collect"
        dest.mkdir()
        rec_cfg = {"collector": "metadata", "bundle": leak,
                   "allowed_signers": allowed, "recipients": None,
                   "service_owner_uid": os.getuid()}
        with pytest.raises(rec.RecoveryCollectError):
            svc.materialise_recovery(rec_cfg, self._cfg(), str(dest))

    def test_materialise_optional_inputs_skipped_when_absent(self, tmp_path):
        # only the bundle configured; docs still generated, no allowed/recips
        bundle = _w(tmp_path, "vault-recovery.json", GOOD_BUNDLE)
        dest = tmp_path / "collect"
        dest.mkdir()
        rec_cfg = {"collector": "metadata", "bundle": bundle,
                   "allowed_signers": None, "recipients": None,
                   "service_owner_uid": os.getuid()}
        svc.materialise_recovery(rec_cfg, self._cfg(), str(dest))
        rdir = dest / "recovery"
        assert (rdir / "vault-recovery.json").exists()
        assert not (rdir / "allowed_signers").exists()
        assert (rdir / "manifest-schema.txt").exists()

    def test_materialise_rebuilds_fresh_each_run(self, tmp_path):
        bundle = _w(tmp_path, "vault-recovery.json", GOOD_BUNDLE)
        dest = tmp_path / "collect"
        dest.mkdir()
        rec_cfg = {"collector": "metadata", "bundle": bundle,
                   "allowed_signers": None, "recipients": None,
                   "service_owner_uid": os.getuid()}
        svc.materialise_recovery(rec_cfg, self._cfg(), str(dest))
        # plant a stale file in recovery/, then re-run -> it must disappear
        (dest / "recovery" / "stale").write_text("old")
        svc.materialise_recovery(rec_cfg, self._cfg(), str(dest))
        assert not (dest / "recovery" / "stale").exists()


class TestConfigRecoveryTable:
    BASE = """
host_id = "h1"
recipients = "/r"
remote = "/srv/b"
sign_key = "/k"
[[subvol]]
name = "metadata"
collector = true
paths = ["/etc/qdistro"]
"""

    def _w(self, tmp_path, body):
        p = tmp_path / "backup.conf"
        p.write_text(body)
        return str(p)

    def test_recovery_table_parsed(self, tmp_path):
        body = self.BASE + ('\n[recovery]\ncollector = "metadata"\n'
                            'bundle = "/etc/qdistro/recovery/b.json"\n'
                            'allowed_signers = "/as"\nrecipients = "/rc"\n'
                            'service_owner_uid = 0\n')
        cfg = svc.load_config(self._w(tmp_path, body))
        assert cfg["recovery"]["collector"] == "metadata"
        assert cfg["recovery"]["bundle"].endswith("b.json")
        assert cfg["recovery"]["service_owner_uid"] == 0

    def test_recovery_collector_must_name_a_collector(self, tmp_path):
        body = self.BASE + '\n[recovery]\ncollector = "nope"\n'
        with pytest.raises(svc.BackupServiceError):
            svc.load_config(self._w(tmp_path, body))

    def test_recovery_collector_cannot_name_a_plain_subvol(self, tmp_path):
        body = (self.BASE + '[[subvol]]\nname = "data"\nsource = "/d"\n'
                '\n[recovery]\ncollector = "data"\n')
        with pytest.raises(svc.BackupServiceError):
            svc.load_config(self._w(tmp_path, body))

    def test_no_recovery_table_is_none(self, tmp_path):
        cfg = svc.load_config(self._w(tmp_path, self.BASE))
        assert cfg["recovery"] is None
