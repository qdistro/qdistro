"""tier3s/provision-runsc.sh against a fake pinned bundle (QDISTRO_RUNSC_PREFIX hook).

Runs the real script; never touches /usr. Requires bash, tar with zstd.
"""
import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tier3s" / "provision-runsc.sh"
WRAPPER = REPO / "tier3s" / "tier3s-runsc"
VERSION = "runsc version release-29990101.0"

pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd missing")


def sha(p):
    return hashlib.sha512(Path(p).read_bytes()).hexdigest()


def make_bundle(tmp, version=VERSION, extra=None, rc=0):
    b = tmp / "bundle"
    (b / "gvisor-bin").mkdir(parents=True)
    (b / "runsc").write_text(f"#!/bin/sh\necho '{version}'\nexit {rc}\n")
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


def args_env(tmp, pin, offline=True, **extra_env):
    if os.geteuid() == 0:
        pytest.skip("QDISTRO_RUNSC_PREFIX is refused for root by design; run as a normal user")
    root = tmp / "root"
    env = dict(os.environ, QDISTRO_RUNSC_PREFIX=str(root), TMPDIR=str(tmp), **extra_env)
    args = ["bash", str(SCRIPT), "--pin", str(pin), "--cache-dir", str(tmp / "cache")]
    if offline:
        args.append("--offline")
    return args, env, root


def run(tmp, pin, offline=True, **extra_env):
    args, env, root = args_env(tmp, pin, offline, **extra_env)
    return subprocess.run(args, env=env, capture_output=True, text=True), root


def leftovers(root):
    d = root / "usr/libexec/qdistro"
    out = [p.name for p in d.iterdir() if ".new." in p.name or ".old." in p.name]
    out += [p.name for p in (root / "etc/qdistro").iterdir() if ".old." in p.name or ".new." in p.name]
    return out


def wait_for(pred, proc, what, timeout=60):
    deadline = time.monotonic() + timeout
    while not pred():
        assert proc.poll() is None, f"process exited before {what}"
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def start(tmp, pin, name, **extra_env):
    """Popen the real script with stdout/stderr in files (pollable)."""
    args, env, root = args_env(tmp, pin, **extra_env)
    out = open(tmp / f"{name}.out", "w")
    err = open(tmp / f"{name}.err", "w")
    return subprocess.Popen(args, env=env, stdout=out, stderr=err), tmp / f"{name}.out", tmp / f"{name}.err"


def root_capable_cmd(cmd):
    """Run cmd with euid 0: directly when we are root (VM), else inside an
    unprivileged user namespace (uid 0 mapped to us, so nothing real is
    writable). Skips when neither is available."""
    if os.geteuid() == 0:
        return cmd
    if shutil.which("unshare") and subprocess.run(["unshare", "-r", "true"],
                                                  capture_output=True).returncode == 0:
        return ["unshare", "-r", "--"] + cmd
    pytest.skip("needs root or unprivileged user namespaces")


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


# --- root-only guards (astra full P3): reached with euid 0 ------------------

def bogus_arch_pin(tmp):
    """A READABLE alternate pin that would die later at the arch check, before
    touching anything, should the guard under test ever be removed."""
    b, tar = make_bundle(tmp)
    return write_pin(tmp, b, tar, arch="not-an-arch")


def test_pin_override_refused_for_root(tmp_path):
    pin = bogus_arch_pin(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QDISTRO_RUNSC_")}
    r = subprocess.run(root_capable_cmd(["bash", str(SCRIPT), "--pin", str(pin), "--offline"]),
                       env=env, capture_output=True, text=True)
    assert r.returncode == 1
    assert r.stderr.strip() == ("provision-runsc: FAIL: --pin is a unit-test option (needs "
                                f"QDISTRO_RUNSC_PREFIX); a real install uses {SCRIPT.parent}/RUNSC_RELEASE")


def test_prefix_hook_refused_for_root(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith("QDISTRO_RUNSC_")}
    env["QDISTRO_RUNSC_PREFIX"] = str(tmp_path / "root")
    r = subprocess.run(root_capable_cmd(["bash", str(SCRIPT), "--offline",
                                         "--cache-dir", str(tmp_path / "nocache")]),
                       env=env, capture_output=True, text=True)
    assert r.returncode == 1
    assert r.stderr.strip() == ("provision-runsc: FAIL: QDISTRO_RUNSC_PREFIX is a unit-test hook "
                                "and is refused for root")
    assert not (tmp_path / "root").exists()


@pytest.mark.parametrize("hook", ["QDISTRO_RUNSC_FAIL_AFTER_SWAP", "QDISTRO_RUNSC_PAUSE_AFTER_SWAP"])
def test_test_hooks_refused_for_root(tmp_path, hook):
    env = {k: v for k, v in os.environ.items() if not k.startswith("QDISTRO_RUNSC_")}
    env[hook] = str(tmp_path)
    r = subprocess.run(root_capable_cmd(["bash", str(SCRIPT), "--offline",
                                         "--cache-dir", str(tmp_path / "nocache")]),
                       env=env, capture_output=True, text=True)
    assert r.returncode == 1
    assert r.stderr.strip() == (f"provision-runsc: FAIL: {hook} is a unit-test hook "
                                "(needs QDISTRO_RUNSC_PREFIX)")


def test_must_run_as_root_without_prefix(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("caller is root")
    env = {k: v for k, v in os.environ.items() if not k.startswith("QDISTRO_RUNSC_")}
    r = subprocess.run(["bash", str(SCRIPT), "--offline"], env=env, capture_output=True, text=True)
    assert r.returncode == 1 and r.stderr.strip() == "provision-runsc: FAIL: must run as root"


# --- version text with a nonzero exit ---------------------------------------

def test_version_text_with_nonzero_exit_fails_closed(tmp_path):
    b, tar = make_bundle(tmp_path, rc=1)          # prints the pinned version, exits 1
    pin = write_pin(tmp_path, b, tar)
    r, root = run(tmp_path, pin)
    assert r.returncode == 1
    assert f"release skew: runsc --version says '{VERSION}'" in r.stderr
    assert not (root / "usr/libexec/qdistro/runsc").exists()
    assert not (root / "etc/qdistro/runsc-release").exists()


# --- wrapper environment scrubbing (real tier3s-runsc) ----------------------

def test_wrapper_scrubs_environment_and_fixes_flags(tmp_path):
    """Run the real wrapper in a private mount namespace whose /usr/libexec is a
    bind of a fake tree, so /usr/libexec/qdistro/runsc/runsc is a script that
    dumps the environ it was exec'd with and its argv."""
    if not shutil.which("unshare") or subprocess.run(
            ["unshare", "-rm", "true"], capture_output=True).returncode != 0:
        pytest.skip("needs user+mount namespaces")
    fake = tmp_path / "libexec"
    (fake / "qdistro/runsc").mkdir(parents=True)
    dump = fake / "qdistro/runsc/runsc"
    dump.write_text('#!/bin/sh\nprintf "ENV\\n"; tr "\\0" "\\n" < /proc/$$/environ\n'
                    'printf "ARGS\\n"; for a in "$@"; do printf "%s\\n" "$a"; done\n')
    dump.chmod(0o755)
    env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/1000", RUNSC_TEST_KNOB="1",
               GVISOR_X="y", PATH=f"/nonexistent/evil:{os.environ['PATH']}")
    r = subprocess.run(["unshare", "-rm", "--", "sh", "-c",
                        'mount --bind "$1" /usr/libexec && exec "$2" create --bundle "a b"',
                        "sh", str(fake), str(WRAPPER)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = r.stdout.split("\n")
    env_lines = out[out.index("ENV") + 1:out.index("ARGS")]
    args = out[out.index("ARGS") + 1:-1]
    assert [l for l in env_lines if l] == ["PATH=/usr/bin:/bin"]
    assert args == ["--ignore-cgroups", "--platform=systrap", "--oci-seccomp",
                    "create", "--bundle", "a b"]


# --- concurrency (astra full P2) --------------------------------------------

def test_concurrent_provisions_are_serialized(tmp_path):
    """A pauses right after its swap and then fails; B starts while A holds the
    transaction. B must wait for the lock, then see A's restored (damaged)
    state and repair it. Without the lock B takes the idempotent fast path on
    A's swapped-in tree and reports success for a state A then rolls back."""
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    r, root = run(tmp_path, pin)
    assert r.returncode == 0, r.stderr
    side = root / "usr/libexec/qdistro/runsc/gvisor-bin/gvisor_sentry"
    side.chmod(0o700)                     # damaged live install
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    a, a_out, a_err = start(tmp_path, pin, "A", QDISTRO_RUNSC_PAUSE_AFTER_SWAP=str(ctl),
                            QDISTRO_RUNSC_FAIL_AFTER_SWAP="1")
    try:
        wait_for(lambda: (ctl / "reached").exists(), a, "A to reach the post-swap point")
        bp, b_out, b_err = start(tmp_path, pin, "B")
        try:
            deadline = time.monotonic() + 60
            while "waiting for the provisioning lock" not in b_out.read_text():
                assert bp.poll() is None, ("B finished while A held the transaction:\n"
                                           + b_out.read_text() + b_err.read_text())
                assert time.monotonic() < deadline, "B never reported waiting for the lock"
                time.sleep(0.05)
            time.sleep(0.5)
            assert bp.poll() is None, "B did not stay blocked while A held the lock"
            (ctl / "release").touch()
            assert a.wait(timeout=60) == 1
            assert bp.wait(timeout=120) == 0, b_out.read_text() + b_err.read_text()
        finally:
            if bp.poll() is None:
                bp.kill()
    finally:
        (ctl / "release").touch()
        if a.poll() is None:
            a.kill()
    ao, ae, bo = a_out.read_text(), a_err.read_text(), b_out.read_text()
    assert "TEST: injected failure after swap" in ae and "rolling back" in ao
    assert "ROLLBACK INCOMPLETE" not in ae
    assert "already installed" not in bo, bo
    assert "PASS (TEST prefix" in bo
    # B acquired the lock only after A's transaction ended (A's rollback done)
    assert bo.index("waiting for the provisioning lock") < bo.index("holding the provisioning lock")
    assert side.stat().st_mode & 0o777 == 0o755          # B's repair is the final state
    assert (root / "etc/qdistro/runsc-release").read_bytes() == pin.read_bytes()
    assert (root / "usr/libexec/qdistro/tier3s-runsc").read_bytes() == WRAPPER.read_bytes()
    assert leftovers(root) == []
    r3, _ = run(tmp_path, pin)
    assert r3.returncode == 0 and "already installed" in r3.stdout


def test_failed_rollback_preserves_recovery_material(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    r, root = run(tmp_path, pin)
    assert r.returncode == 0, r.stderr
    side = root / "usr/libexec/qdistro/runsc/gvisor-bin/gvisor_sentry"
    side.chmod(0o700)
    wrapper = root / "usr/libexec/qdistro/tier3s-runsc"
    old_wrapper = wrapper.read_bytes()
    old_stamp = (root / "etc/qdistro/runsc-release").read_bytes()
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    a, a_out, a_err = start(tmp_path, pin, "A", QDISTRO_RUNSC_PAUSE_AFTER_SWAP=str(ctl),
                            QDISTRO_RUNSC_FAIL_AFTER_SWAP="1")
    try:
        wait_for(lambda: (ctl / "reached").exists(), a, "A to reach the post-swap point")
        # make the wrapper restore impossible: the live wrapper path becomes a
        # non-empty directory, which `mv -fT <file> <dir>` cannot replace
        wrapper.unlink()
        wrapper.mkdir()
        (wrapper / "blocker").write_text("x")
        (ctl / "release").touch()
        assert a.wait(timeout=60) == 1
    finally:
        (ctl / "release").touch()
        if a.poll() is None:
            a.kill()
    err = a_err.read_text()
    assert "ROLLBACK INCOMPLETE; kept for manual recovery:" in err, err
    kept = [Path(k) for k in err.split("kept for manual recovery:")[1].split()]
    assert all(k.exists() for k in kept), kept               # every named path is real
    by = {k.name.split(".")[0]: k for k in kept}
    assert set(by) == {"runsc", "tier3s-runsc"}, kept       # tree + wrapper material
    assert by["tier3s-runsc"].read_bytes() == old_wrapper    # the unrestorable old wrapper
    assert (by["runsc"] / "runsc").exists()                  # the staged tree, after the exchange back
    # the parts that could be restored were: old tree back live, old stamp back live
    assert side.stat().st_mode & 0o777 == 0o700
    assert (root / "etc/qdistro/runsc-release").read_bytes() == old_stamp
    assert wrapper.is_dir()                                  # untouched by the failed restore


# --- lock directory trust / download publication ----------------------------

def test_untrusted_lock_dir_is_refused(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    lockdir = tmp_path / "root/run/qdistro-runsc"
    lockdir.mkdir(parents=True)
    lockdir.chmod(0o777)
    r, root = run(tmp_path, pin)
    assert r.returncode == 1
    assert f"untrusted path: {lockdir} is group/other-writable (mode 777)" in r.stderr
    assert not (root / "usr/libexec/qdistro/runsc").exists()


def test_symlinked_lock_dir_is_refused(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    (tmp_path / "root/run").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "root/run/qdistro-runsc").symlink_to(tmp_path / "elsewhere")
    r, _ = run(tmp_path, pin)
    assert r.returncode == 1 and "is a symlink" in r.stderr
    assert list((tmp_path / "elsewhere").iterdir()) == []


def _fake_curl(tmp, src):
    d = tmp / "fakecurl"
    d.mkdir()
    c = d / "curl"
    c.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && { shift; out=$1; }; shift; done\n'
                 f'cp "{src}" "$out"\n')
    c.chmod(0o755)
    return d


def test_download_is_verified_before_it_is_published(tmp_path):
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    src = tmp_path / "served.tar.zstd"
    tar.rename(src)                                     # cache now empty
    bad = tmp_path / "bad.tar.zstd"
    bad.write_bytes(src.read_bytes() + b"x")
    fc = _fake_curl(tmp_path, bad)
    r, root = run(tmp_path, pin, offline=False, PATH=f"{fc}:{os.environ['PATH']}")
    assert r.returncode == 1 and "download not cached" in r.stderr
    cache = tmp_path / "cache/29990101.0"
    assert not (cache / "gvisor.tar.zstd").exists()
    assert not cache.exists() or list(cache.iterdir()) == []   # nothing published
    bad.write_bytes(src.read_bytes())                   # now serve the right bytes
    r, root = run(tmp_path, pin, offline=False, PATH=f"{fc}:{os.environ['PATH']}")
    assert r.returncode == 0, r.stderr
    assert "cached " in r.stdout
    assert [p.name for p in cache.iterdir()] == ["gvisor.tar.zstd"]   # no temporaries
    assert sha(cache / "gvisor.tar.zstd") == sha(src)


# --- cache publication only through a trusted chain (astra fix r1 P2) ------

def _logging_curl(tmp, src):
    """Fake curl that records each invocation, then serves src."""
    d = tmp / "logcurl"
    d.mkdir()
    log = tmp / "curl.calls"
    c = d / "curl"
    c.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n'
                 'while [ $# -gt 0 ]; do [ "$1" = -o ] && { shift; out=$1; }; shift; done\n'
                 f'cp "{src}" "$out"\n')
    c.chmod(0o755)
    return d, log


def _online_without_cached_tarball(tmp):
    b, tar = make_bundle(tmp)
    pin = write_pin(tmp, b, tar)
    src = tmp / "served.tar.zstd"
    tar.rename(src)
    fc, calls = _logging_curl(tmp, src)
    return pin, fc, calls


def test_download_refused_into_writable_cache_dir(tmp_path):
    pin, fc, calls = _online_without_cached_tarball(tmp_path)
    cache = tmp_path / "cache"
    cache.chmod(0o777)
    r, root = run(tmp_path, pin, offline=False, PATH=f"{fc}:{os.environ['PATH']}")
    assert r.returncode == 1
    assert f"untrusted path: {cache} is group/other-writable (mode 777)" in r.stderr
    assert not calls.exists(), "downloaded although the cache dir is untrusted"
    assert list((cache / "29990101.0").iterdir()) == []
    assert not (root / "usr/libexec/qdistro/runsc").exists()


def test_download_refused_through_symlinked_cache_dir(tmp_path):
    """cache/<release> is a symlink into a dir someone else controls, next to
    an unrelated sentinel file: nothing is downloaded or written there and the
    sentinel's content and mode are unchanged."""
    pin, fc, calls = _online_without_cached_tarball(tmp_path)
    rel = tmp_path / "cache/29990101.0"
    rel.rmdir()
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("precious\n")
    sentinel.chmod(0o600)
    (attacker / "gvisor.tar.zstd.lnk").symlink_to(sentinel)
    rel.symlink_to(attacker)
    r, root = run(tmp_path, pin, offline=False, PATH=f"{fc}:{os.environ['PATH']}")
    assert r.returncode == 1
    assert f"untrusted path: {rel} is a symlink" in r.stderr
    assert not calls.exists()
    assert sorted(p.name for p in attacker.iterdir()) == ["gvisor.tar.zstd.lnk"]
    assert sentinel.read_text() == "precious\n" and sentinel.stat().st_mode & 0o777 == 0o600
    assert not (root / "usr/libexec/qdistro/runsc").exists()


def test_existing_tarball_in_untrusted_cache_is_still_read_safely(tmp_path):
    """Reading stays supported: the tarball is copied privately and verified."""
    b, tar = make_bundle(tmp_path)
    pin = write_pin(tmp_path, b, tar)
    (tmp_path / "cache").chmod(0o777)
    r, root = run(tmp_path, pin)
    assert r.returncode == 0, r.stderr
    assert "verified private copy" in r.stdout
    assert sha(root / "usr/libexec/qdistro/runsc/runsc") == sha(b / "runsc")


# --- root runs only a root-controlled checkout ------------------------------

def untrusted_checkout(tmp, script, how):
    """Byte-identical copy of the real tier3s files in a checkout another user
    could modify (copied, because the property under test is WHERE the script
    lives; the copy is asserted identical to the real file)."""
    co = tmp / "co" / "tier3s"
    co.mkdir(parents=True)
    for f in ("provision-runsc.sh", "probe.sh", "RUNSC_RELEASE", "tier3s-runsc"):
        shutil.copy2(REPO / "tier3s" / f, co / f)
        assert (co / f).read_bytes() == (REPO / "tier3s" / f).read_bytes()
    if how == "dir":
        co.chmod(0o777)
        why = f"{co} is other-writable (mode 777)"
    else:
        (co / "RUNSC_RELEASE").chmod(0o646)
        why = f"{co}/RUNSC_RELEASE is other-writable (mode 646)"
    return co / script, why


@pytest.mark.parametrize("how", ["dir", "pin"])
def test_root_refuses_untrusted_checkout(tmp_path, how):
    script, why = untrusted_checkout(tmp_path, "provision-runsc.sh", how)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QDISTRO_RUNSC_")}
    r = subprocess.run(root_capable_cmd(["bash", str(script), "--offline",
                                         "--cache-dir", str(tmp_path / "nocache")]),
                       env=env, capture_output=True, text=True)
    assert r.returncode == 1
    assert r.stderr.strip() == ("provision-runsc: FAIL: refusing to run as root from a checkout another "
                                f"user could modify: {why} (use a root-owned copy)")


# --- the caller's PATH never supplies a tool (astra fix r3 P2) -------------

SHADOWED = ["dirname", "basename", "id", "stat", "sed", "grep", "find", "sha512sum", "cut",
            "sort", "comm", "tr", "head", "tail", "cat", "cmp", "readlink", "uname", "env",
            "mktemp", "install", "mv", "cp", "rm", "flock", "tar", "curl", "runuser",
            "podman", "getenforce", "seq", "sleep", "chmod", "chown", "mkdir", "ln", "ls",
            "wc", "zstd", "realpath"]


def shadow_path(tmp, names=SHADOWED):
    """A PATH dir whose tools append to a marker and then delegate to the real
    tool, so the script still behaves normally while any use is recorded."""
    d = tmp / "shadow"
    d.mkdir()
    marker = tmp / "SHADOW-RAN"
    for name in names:
        real = shutil.which(name)
        body = f'#!/bin/sh\necho "{name} $*" >> "{marker}"\n'
        body += f'exec "{real}" "$@"\n' if real else "exit 127\n"
        (d / name).write_text(body)
        (d / name).chmod(0o755)
    return d, marker


BASH = shutil.which("bash")


def test_provision_never_uses_caller_path_tools(tmp_path):
    shadow, marker = shadow_path(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QDISTRO_RUNSC_")}
    env["PATH"] = f"{shadow}:{os.environ['PATH']}"
    # non-root, no prefix: dies "must run as root" after computing HERE
    r = subprocess.run([BASH, str(SCRIPT), "--offline"], cwd=tmp_path, env=env,
                       capture_output=True, text=True)
    if os.geteuid() != 0:
        assert r.stderr.strip() == "provision-runsc: FAIL: must run as root"
    # euid 0: dies at the --pin refusal after computing HERE
    pin = bogus_arch_pin(tmp_path)
    cmd = root_capable_cmd([BASH, str(SCRIPT), "--pin", str(pin), "--offline"])
    if cmd[0] == "unshare":
        cmd[0] = shutil.which("unshare")
    r = subprocess.run(cmd, cwd=tmp_path, env=env, capture_output=True, text=True)
    assert "--pin is a unit-test option" in r.stderr, r.stderr
    # help
    subprocess.run([BASH, str(SCRIPT), "-h"], env=env, capture_output=True, text=True)
    assert not marker.exists(), "caller PATH tool ran: " + marker.read_text()


def test_wrapper_never_uses_caller_path_env(tmp_path):
    """Same mount-namespace fixture as the scrubbing test, with a shadow `env`
    first on PATH: the wrapper must use /usr/bin/env, not the caller's."""
    if not shutil.which("unshare") or subprocess.run(
            ["unshare", "-rm", "true"], capture_output=True).returncode != 0:
        pytest.skip("needs user+mount namespaces")
    shadow, marker = shadow_path(tmp_path, names=["env"])
    fake = tmp_path / "libexec"
    (fake / "qdistro/runsc").mkdir(parents=True)
    dump = fake / "qdistro/runsc/runsc"
    dump.write_text('#!/bin/sh\ntr "\\0" "\\n" < /proc/$$/environ\n')
    dump.chmod(0o755)
    env = dict(os.environ, PATH=f"{shadow}:{os.environ['PATH']}", EVIL="1")
    r = subprocess.run([shutil.which("unshare"), "-rm", "--", "/bin/sh", "-c",
                        'mount --bind "$1" /usr/libexec && exec "$2" x', "sh", str(fake), str(WRAPPER)],
                       env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert [l for l in r.stdout.split("\n") if l] == ["PATH=/usr/bin:/bin"]
    assert not marker.exists(), "the caller's env ran: " + marker.read_text()
