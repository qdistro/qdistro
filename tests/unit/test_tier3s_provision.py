"""tier3s/provision-runsc.sh against a fake pinned bundle (QDISTRO_RUNSC_PREFIX hook).

Runs the real script; never touches /usr. Requires bash, tar with zstd.
"""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tier3s" / "provision-runsc.sh"
WRAPPER = REPO / "tier3s" / "tier3s-runsc"
VERSION = "runsc version release-29990101.0"

pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd missing")


def sha(p):
    return hashlib.sha512(Path(p).read_bytes()).hexdigest()


def make_bundle(tmp, version=VERSION, extra=None):
    b = tmp / "bundle"
    (b / "gvisor-bin").mkdir(parents=True)
    (b / "runsc").write_text(f"#!/bin/sh\necho '{version}'\n")
    (b / "gvisor-bin" / "gvisor_sentry").write_text("#!/bin/sh\n")
    (b / "gvisor-bin" / "runsc-fd-parking").write_text("#!/bin/sh\n# parking\n")
    if extra:
        (b / "gvisor-bin" / extra).write_text("x")
    for f in b.rglob("*"):
        if f.is_file():
            f.chmod(0o755)
    cache = tmp / "cache" / "29990101.0"
    cache.mkdir(parents=True)
    tar = cache / "gvisor.tar.zstd"
    subprocess.run(["tar", "--zstd", "-cf", str(tar), "-C", str(b), "."], check=True)
    return b, tar


def write_pin(tmp, b, tar, **over):
    vals = {
        "release": "29990101.0",
        "arch": os.uname().machine,
        "base_url": "https://invalid.example/none",
        "version_string": VERSION,
        "tarball": "gvisor.tar.zstd",
        "tarball_sha512": sha(tar),
        "runsc_sha512": sha(b / "runsc"),
        "sidecar_gvisor_sentry_sha512": sha(b / "gvisor-bin" / "gvisor_sentry"),
        "sidecar_runsc-fd-parking_sha512": sha(b / "gvisor-bin" / "runsc-fd-parking"),
    }
    vals.update(over)
    pin = tmp / "RUNSC_RELEASE"
    pin.write_text("# test pin\n" + "".join(f"{k}={v}\n" for k, v in vals.items()))
    return pin


def run(tmp, pin, offline=True):
    root = tmp / "root"
    env = dict(os.environ, QDISTRO_RUNSC_PREFIX=str(root), TMPDIR=str(tmp))
    args = ["bash", str(SCRIPT), "--pin", str(pin), "--cache-dir", str(tmp / "cache")]
    if offline:
        args.append("--offline")
    return subprocess.run(args, env=env, capture_output=True, text=True), root


def test_installs_then_idempotent(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    r, root = run(tmp_path, pin)
    assert r.returncode == 0, r.stderr
    d = root / "usr/libexec/qdistro/runsc"
    assert sha(d / "runsc") == sha(b / "runsc")
    assert (d / "gvisor-bin" / "gvisor_sentry").exists()
    assert (root / "usr/libexec/qdistro/tier3s-runsc").read_bytes() == WRAPPER.read_bytes()
    assert (root / "etc/qdistro/runsc-release").read_bytes() == pin.read_bytes()
    r2, _ = run(tmp_path, pin)
    assert r2.returncode == 0 and "already installed" in r2.stdout


def test_tarball_hash_mismatch_fails_closed(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar, tarball_sha512="0" * 128)
    r, root = run(tmp_path, pin)
    assert r.returncode != 0 and "tarball sha512 mismatch" in r.stderr
    assert not (root / "usr/libexec/qdistro/runsc").exists()


def test_file_hash_mismatch_fails_closed(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar, sidecar_gvisor_sentry_sha512="a" * 128)
    r, root = run(tmp_path, pin)
    assert r.returncode != 0 and "gvisor-bin/gvisor_sentry" in r.stderr
    assert not (root / "usr/libexec/qdistro/runsc").exists()


def test_unpinned_sidecar_fails_closed(tmp_path):
    b, tar = make_bundle(tmp_path, extra="surprise")
    pin = write_pin(tmp_path, b, tar)
    r, _ = run(tmp_path, pin)
    assert r.returncode != 0 and "unpinned file" in r.stderr


def test_version_skew_fails_and_keeps_old_install(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    assert run(tmp_path, pin)[0].returncode == 0
    good = pin.read_bytes()
    pin2 = write_pin(tmp_path, b, tar, version_string="runsc version release-other")
    r, root = run(tmp_path, pin2)
    assert r.returncode != 0 and "release skew" in r.stderr
    assert (root / "usr/libexec/qdistro/runsc/runsc").exists()
    assert (root / "etc/qdistro/runsc-release").read_bytes() == good


def test_offline_without_cache_fails(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    tar.unlink()
    r, _ = run(tmp_path, pin)
    assert r.returncode != 0 and "offline" in r.stderr


def test_extra_symlink_breaks_idempotence_and_is_replaced(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    r, root = run(tmp_path, pin)
    assert r.returncode == 0, r.stderr
    d = root / "usr/libexec/qdistro/runsc"
    (d / "gvisor-bin" / "evil").symlink_to("/bin/sh")
    r2, _ = run(tmp_path, pin)
    assert r2.returncode == 0, r2.stderr
    assert "already installed" not in r2.stdout
    assert not (d / "gvisor-bin" / "evil").exists()
    assert not list(d.parent.glob("runsc.new.*"))


def test_pin_override_refused_without_prefix(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "QDISTRO_RUNSC_PREFIX"}
    r = subprocess.run(["bash", str(SCRIPT), "--pin", str(tmp_path / "x")],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0


def test_lost_exec_bit_is_not_idempotent_and_is_repaired(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    r, root = run(tmp_path, pin)
    assert r.returncode == 0, r.stderr
    side = root / "usr/libexec/qdistro/runsc/gvisor-bin/gvisor_sentry"
    side.chmod(0o644)
    r2, _ = run(tmp_path, pin)
    assert r2.returncode == 0 and "already installed" not in r2.stdout, r2.stdout + r2.stderr
    assert side.stat().st_mode & 0o777 == 0o755


def test_failure_after_swap_restores_previous_install(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    r, root = run(tmp_path, pin)
    assert r.returncode == 0, r.stderr
    d = root / "usr/libexec/qdistro/runsc"
    side = d / "gvisor-bin" / "gvisor_sentry"
    side.chmod(0o700)  # make the live install differ so provision really swaps
    env = dict(os.environ, QDISTRO_RUNSC_PREFIX=str(root), TMPDIR=str(tmp_path),
               QDISTRO_RUNSC_FAIL_AFTER_SWAP="1")
    r2 = subprocess.run(["bash", str(SCRIPT), "--pin", str(pin), "--cache-dir",
                         str(tmp_path / "cache"), "--offline"],
                        env=env, capture_output=True, text=True)
    assert r2.returncode != 0 and "injected failure" in r2.stderr
    assert "rolling back" in r2.stdout
    assert side.stat().st_mode & 0o777 == 0o700          # the OLD tree is back
    assert (root / "etc/qdistro/runsc-release").read_bytes() == pin.read_bytes()
    left = [p.name for p in d.parent.iterdir() if ".new." in p.name or ".old." in p.name]
    left += [p.name for p in (root / "etc/qdistro").iterdir() if ".old." in p.name or ".new." in p.name]
    assert left == []
