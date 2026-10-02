"""tier3s/probe.sh: profile gate, first-missing reporting, and integrity-before-exec.

Runs the REAL probe.sh against a synthetic installation under the
QDISTRO_PROBE_ROOT test hook (with QDISTRO_PROBE_PIN naming a test pin made
from that synthetic bundle). podman/newuidmap/newgidmap are PATH fakes; no
podman, runsc or sandbox ever runs. The fake runsc is a harmless shell script
that appends to a marker file and prints the pinned version string, so a test
can prove whether the probe executed it.
"""
import hashlib
import io
import os
import tarfile
import pwd
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tier3s" / "probe.sh"
WRAPPER = REPO / "tier3s" / "tier3s-runsc"
VERSION = "runsc version release-29990101.0"
ME = pwd.getpwuid(os.getuid()).pw_name


def sha(p):
    return hashlib.sha512(Path(p).read_bytes()).hexdigest()


def run(root, user=None, pin=None, path_prepend=None, extra_env=None):
    env = dict(os.environ, QDISTRO_PROBE_ROOT=str(root))
    if pin is not None:
        env["QDISTRO_PROBE_PIN"] = str(pin)
    if path_prepend is not None:
        env["PATH"] = f"{path_prepend}:{env['PATH']}"
    env.update(extra_env or {})
    args = ["bash", str(SCRIPT), "--user", user or ME]
    return subprocess.run(args, env=env, capture_output=True, text=True)


def fake_runsc(marker, version=VERSION, rc=0, salt="", text="executed"):
    return (f"#!/bin/sh\n# fake runsc {salt}\necho {text} >> '{marker}'\n"
            f"echo '{version}'\nexit {rc}\n")


class Install:
    """A complete synthetic dev-profile installation under tmp/root."""

    def __init__(self, tmp, version=VERSION, rc=0):
        self.tmp = tmp
        self.root = tmp / "root"
        self.marker = tmp / "MARKER"
        self.bin = tmp / "fakebin"
        self.runsc_dir = self.root / "usr/libexec/qdistro/runsc"
        self.wrapper = self.root / "usr/libexec/qdistro/tier3s-runsc"
        for d in (self.root / "etc/qdistro", self.runsc_dir / "gvisor-bin", self.bin):
            d.mkdir(parents=True, exist_ok=True)
        (self.root / "etc/qdistro/profile").write_text("QDISTRO_PROFILE=dev\n")
        for db in ("subuid", "subgid"):
            (self.root / "etc" / db).write_text(f"{ME}:100000:65536\n")
        self.state_root = self.root / "run/qdistro-tier3s-runsc" / str(os.getuid())
        self.state_root.mkdir(parents=True)
        (self.runsc_dir / "runsc").write_text(fake_runsc(self.marker, version, rc))
        (self.runsc_dir / "gvisor-bin/gvisor_sentry").write_text("#!/bin/sh\n# sentry\n")
        (self.runsc_dir / "gvisor-bin/runsc-fd-parking").write_text("#!/bin/sh\n# parking\n")
        shutil.copyfile(WRAPPER, self.wrapper)
        # explicit modes: the probe requires 0755 dirs/executables and refuses
        # group/other-writable ancestors whatever the caller's umask is
        for d in [self.root, *self.root.rglob("*")]:
            if d.is_dir():
                d.chmod(0o755)
        self.state_root.chmod(0o700)
        for f in (self.runsc_dir / "runsc", self.wrapper,
                  *(self.runsc_dir / "gvisor-bin").iterdir()):
            f.chmod(0o755)
        self.pin = tmp / "RUNSC_RELEASE"
        self.pin.write_text(
            "# test pin\nrelease=29990101.0\n"
            f"version_string={VERSION}\n"
            f"runsc_sha512={sha(self.runsc_dir / 'runsc')}\n"
            f"sidecar_gvisor_sentry_sha512={sha(self.runsc_dir / 'gvisor-bin/gvisor_sentry')}\n"
            f"sidecar_runsc-fd-parking_sha512={sha(self.runsc_dir / 'gvisor-bin/runsc-fd-parking')}\n")
        shutil.copyfile(self.pin, self.root / "etc/qdistro/runsc-release")
        (self.root / "etc/qdistro/runsc-release").chmod(0o644)
        fakes = {
            "podman": f"""#!/bin/sh
case "$*" in
  "version --format {{{{.Client.Version}}}}") echo 6.0.2 ;;
  "image exists "*) exit "${{T3S_FAKE_IMG_EXISTS_RC:-0}}" ;;
  "import -q - "*)
    ls -A "${{TMPDIR:-/tmp}}" > "$T3S_FAKE_DIR/tmpdir-at-import"
    cat > "$T3S_FAKE_DIR/imported.tar"
    [ "${{T3S_FAKE_IMPORT_RC:-0}}" = 0 ] || {{ echo "fake import failed: boom" >&2; exit "$T3S_FAKE_IMPORT_RC"; }} ;;
  "--runtime {self.wrapper} create "*) echo fakeid ;;
  "inspect --format {{{{.OCIRuntime}}}} "*) echo "{self.wrapper}" ;;
  "inspect --format {{{{.ProcessLabel}}}}|{{{{.HostConfig.SecurityOpt}}}} "*) echo "|[label=disable]" ;;
  "rm -f "*) ;;
  *) echo "fake podman: unexpected: $*" >&2; exit 99 ;;
esac
""",
            "newuidmap": "#!/bin/sh\nexit 0\n",
            "newgidmap": "#!/bin/sh\nexit 0\n",
        }
        for name, body in fakes.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def probe(self, **kw):
        return run(self.root, pin=self.pin, path_prepend=self.bin, **kw)

    def executed(self):
        return self.marker.exists()


def lines(r, kind):
    return [l for l in r.stdout.splitlines() if l.startswith(kind + " ")]


def assert_not_executed(r, inst):
    assert r.returncode == 1, r.stdout + r.stderr
    assert "FAIL runsc_version: not executed:" in r.stdout, r.stdout
    assert not inst.executed(), "probe executed an unverified runsc:\n" + r.stdout


# --- profile gate / hooks --------------------------------------------------

def test_refuses_without_profile(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 2 and "REFUSE profile" in r.stdout


def test_refuses_hardened_profile(tmp_path):
    (tmp_path / "etc/qdistro").mkdir(parents=True)
    (tmp_path / "etc/qdistro/profile").write_text("QDISTRO_PROFILE=release\n")
    r = run(tmp_path)
    assert r.returncode == 2 and "release" in r.stdout


def test_pin_hook_refused_without_test_root(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "QDISTRO_PROBE_ROOT"}
    env["QDISTRO_PROBE_PIN"] = str(tmp_path / "pin")
    r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    assert r.returncode == 2
    assert "QDISTRO_PROBE_PIN is a unit-test hook and needs QDISTRO_PROBE_ROOT" in r.stderr


def test_names_first_missing_when_runsc_absent(tmp_path):
    (tmp_path / "etc/qdistro").mkdir(parents=True)
    (tmp_path / "etc/qdistro/profile").write_text("QDISTRO_PROFILE=dev\n")
    r = run(tmp_path)
    assert r.returncode == 1
    last = r.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT FAIL: first missing prerequisite:")
    fails = lines(r, "FAIL")
    assert fails and fails[0].split()[1].rstrip(":") in last
    assert "FAIL runsc: not provisioned" in r.stdout


# --- the clean synthetic run (positive control) ----------------------------

def test_test_root_never_exits_zero(tmp_path):
    """An otherwise-clean probe under the test root is TEST-PASS / exit 3, never 0."""
    inst = Install(tmp_path)
    r = inst.probe()
    assert r.stdout.startswith("TEST MODE:")
    assert lines(r, "FAIL") == [], r.stdout
    assert r.stdout.strip().splitlines()[-1].startswith("RESULT TEST-PASS:"), r.stdout
    assert r.returncode == 3, r.stdout
    # the verified runsc was executed exactly once, through the verified fd
    assert inst.marker.read_text() == "executed\n"
    assert "PASS runsc_version: " + VERSION in r.stdout


def test_version_text_with_nonzero_exit_fails(tmp_path):
    inst = Install(tmp_path, rc=1)       # prints the pinned version, exits 1
    r = inst.probe()
    assert r.returncode == 1
    assert f"FAIL runsc_version: version '{VERSION}' rc=1" in r.stdout
    assert inst.executed()               # verified, so it ran; the rc decides


# --- integrity before execution (astra full P2) ----------------------------

def test_replaced_runsc_with_correct_stamp_is_never_executed(tmp_path):
    """The review's scenario: stamp intact, runsc replaced by a side-effecting
    script that prints the expected version."""
    inst = Install(tmp_path)
    rs = inst.runsc_dir / "runsc"
    rs.write_text(fake_runsc(inst.marker, salt="REPLACED BY ATTACKER"))
    r = inst.probe()
    assert "sha512:runsc" in r.stdout
    assert_not_executed(r, inst)


def test_byte_only_tamper_of_runsc_is_never_executed(tmp_path):
    inst = Install(tmp_path)
    rs = inst.runsc_dir / "runsc"
    data = bytearray(rs.read_bytes())
    i = data.index(b"fake runsc")
    data[i] = ord("F")                   # same length, same path/mode/owner
    rs.write_bytes(bytes(data))
    r = inst.probe()
    assert "file set differs" not in r.stdout
    assert "FAIL bundle: sha512:runsc" in r.stdout, r.stdout
    assert_not_executed(r, inst)


def test_byte_only_tamper_of_sidecar_hits_the_hash_loop(tmp_path):
    """Same paths/modes/owners, changed bytes: only the per-file hash loop sees it."""
    inst = Install(tmp_path)
    side = inst.runsc_dir / "gvisor-bin/gvisor_sentry"
    before = os.stat(side)
    data = bytearray(side.read_bytes())
    data[-2] = ord("X")
    side.write_bytes(bytes(data))
    after = os.stat(side)
    assert (before.st_size, before.st_mode, before.st_uid) == (after.st_size, after.st_mode, after.st_uid)
    r = inst.probe()
    assert "file set differs" not in r.stdout
    assert "FAIL bundle: sha512:gvisor-bin/gvisor_sentry" in r.stdout, r.stdout
    assert_not_executed(r, inst)


def test_symlinked_runsc_dir_is_never_executed(tmp_path):
    """A RUNSC_DIR symlink to an otherwise perfect bundle elsewhere."""
    inst = Install(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    inst.runsc_dir.rename(elsewhere)
    inst.runsc_dir.symlink_to(elsewhere)
    r = inst.probe()
    assert f"FAIL install_path: untrusted: {inst.runsc_dir} is a symlink" in r.stdout, r.stdout
    assert_not_executed(r, inst)


def test_writable_ancestor_is_never_executed(tmp_path):
    """Perfect bundle, but a parent directory others could rename things in."""
    inst = Install(tmp_path)
    parent = inst.root / "usr/libexec/qdistro"
    parent.chmod(0o775)
    r = inst.probe()
    assert f"FAIL install_path: untrusted: {parent} is group/other-writable (mode 775)" in r.stdout
    # the file set is listed (opens nothing) but no content is read under an
    # untrusted path, and the wrapper is not compared or handed to podman
    assert "FAIL bundle: sha512 not checked: install path untrusted" in r.stdout
    assert "FAIL wrapper: not checked: install path untrusted" in r.stdout
    assert_not_executed(r, inst)


def test_fifo_swapped_in_under_untrusted_path_does_not_hang(tmp_path):
    """A writable ancestor lets someone swap a sidecar for a FIFO after the
    listing; the probe must not read content under that path (it would block)."""
    inst = Install(tmp_path)
    (inst.root / "usr/libexec/qdistro").chmod(0o775)
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    env = dict(os.environ, QDISTRO_PROBE_ROOT=str(inst.root), QDISTRO_PROBE_PIN=str(inst.pin),
               QDISTRO_PROBE_PAUSE_AT="after-listing", QDISTRO_PROBE_PAUSE_DIR=str(ctl),
               PATH=f"{inst.bin}:{os.environ['PATH']}")
    p = subprocess.Popen(["timeout", "20", "bash", str(SCRIPT), "--user", ME], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not (ctl / "after-listing.reached").exists():
        assert p.poll() is None and time.monotonic() < deadline, "never reached after-listing"
        time.sleep(0.05)
    side = inst.runsc_dir / "gvisor-bin/gvisor_sentry"
    side.unlink()
    os.mkfifo(side, 0o755)
    (ctl / "after-listing.release").touch()
    out, err = p.communicate(timeout=60)
    assert p.returncode == 1, (p.returncode, out)          # 124 = hung on the FIFO
    assert "FAIL bundle: sha512 not checked: install path untrusted" in out
    assert_not_executed(subprocess.CompletedProcess([], p.returncode, out, err), inst)


def test_group_writable_runsc_is_never_executed(tmp_path):
    inst = Install(tmp_path)
    (inst.runsc_dir / "runsc").chmod(0o775)
    r = inst.probe()
    assert "file set differs" in r.stdout and "f 775" in r.stdout
    assert_not_executed(r, inst)


@pytest.mark.skipif(os.geteuid() != 0, reason="needs root to chown (VM run)")
def test_foreign_owned_runsc_is_never_executed(tmp_path):
    inst = Install(tmp_path)
    os.chown(inst.runsc_dir / "runsc", 65534, -1)       # nobody, bytes unchanged
    r = inst.probe()
    assert "file set differs" in r.stdout
    assert_not_executed(r, inst)


def test_unverified_wrapper_is_not_handed_to_podman(tmp_path):
    inst = Install(tmp_path)
    inst.wrapper.write_text("#!/bin/sh\nexec /bin/true\n")
    log = tmp_path / "podman.log"
    pod = inst.bin / "podman"
    pod.write_text(pod.read_text().replace("#!/bin/sh\n", f"#!/bin/sh\necho \"$*\" >> '{log}'\n", 1))
    r = inst.probe()
    assert r.returncode == 1
    assert "FAIL wrapper:" in r.stdout
    assert "FAIL podman_runtime: not checked: podman missing or wrapper not verified" in r.stdout
    assert "--runtime" not in log.read_text()


def test_test_hooks_refused_without_test_root(tmp_path):
    for hook in ("QDISTRO_PROBE_PAUSE_AT", "QDISTRO_PROBE_PAUSE_DIR"):
        env = {k: v for k, v in os.environ.items() if k != "QDISTRO_PROBE_ROOT"}
        env[hook] = str(tmp_path)
        r = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
        assert r.returncode == 2 and f"{hook} is a unit-test hook" in r.stderr


def _swap_during(inst, point, tmp_path, in_place=False):
    """Run the probe paused at <point>; while paused, replace runsc with a
    same-mode script that leaves a different marker: atomically by rename (new
    inode), or in_place (same inode, rewritten bytes)."""
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    env = dict(os.environ, QDISTRO_PROBE_ROOT=str(inst.root), QDISTRO_PROBE_PIN=str(inst.pin),
               QDISTRO_PROBE_PAUSE_AT=point, QDISTRO_PROBE_PAUSE_DIR=str(ctl),
               PATH=f"{inst.bin}:{os.environ['PATH']}")
    p = subprocess.Popen(["bash", str(SCRIPT), "--user", ME], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 60
    while not (ctl / f"{point}.reached").exists():
        assert p.poll() is None, "probe exited before reaching " + point + ":\n" + p.communicate()[0]
        assert time.monotonic() < deadline, "probe never reached " + point
        time.sleep(0.05)
    if in_place:
        rs = inst.runsc_dir / "runsc"
        ino = os.stat(rs).st_ino
        with open(rs, "r+") as f:
            f.write(fake_runsc(inst.marker, text="ATTACKER"))
            f.truncate()
        assert os.stat(rs).st_ino == ino
    else:
        evil = inst.runsc_dir / ".evil"
        evil.write_text(fake_runsc(inst.marker, text="ATTACKER"))
        evil.chmod(0o755)
        os.replace(evil, inst.runsc_dir / "runsc")
    (ctl / f"{point}.release").touch()
    out, err = p.communicate(timeout=120)
    return p.returncode, out, err


def test_swap_between_validation_and_open_is_never_executed(tmp_path):
    inst = Install(tmp_path)
    rc, out, err = _swap_during(inst, "before-open", tmp_path)
    assert rc == 1, out + err
    assert "FAIL runsc_version: not executed: opened inode" in out, out
    assert not inst.executed(), out


def test_in_place_rewrite_between_validation_and_open_is_never_executed(tmp_path):
    """Same inode (dev:ino unchanged), new bytes: only the sha512 read through
    the opened fd catches it."""
    inst = Install(tmp_path)
    rc, out, err = _swap_during(inst, "before-open", tmp_path, in_place=True)
    assert rc == 1, out + err
    assert "FAIL runsc_version: not executed: sha512 of the opened inode differs" in out, out
    assert not inst.executed(), out


def test_swap_between_verify_and_exec_runs_the_verified_inode(tmp_path):
    """After the fd is verified, replacing the path does not change what runs."""
    inst = Install(tmp_path)
    rc, out, err = _swap_during(inst, "before-exec", tmp_path)
    assert "PASS runsc_version: " + VERSION in out, out + err
    assert inst.marker.read_text() == "executed\n", "the swapped-in path was executed"
    # every check passed before the swap, so this point-in-time screen is a
    # TEST-PASS; what matters is WHICH inode ran
    assert rc == 3, out


# --- scratch-image import: no temporary path, no chmod (astra fix r2 P2) ---

def _import_env(inst, **extra):
    return dict(T3S_FAKE_IMG_EXISTS_RC="1", T3S_FAKE_DIR=str(inst.tmp), **extra)


def _imported_members(inst):
    t = tarfile.open(fileobj=io.BytesIO((inst.tmp / "imported.tar").read_bytes()))
    return [(m.name, m.isdir(), m.mode, m.uid, m.gid) for m in t.getmembers()]


def test_scratch_image_import_needs_no_temporary_path(tmp_path):
    """Absent scratch image, TMPDIR unusable: the probe still imports a valid
    one-entry archive, because it never creates a temporary directory."""
    inst = Install(tmp_path)
    ro = tmp_path / "ro-tmp"
    ro.mkdir()
    ro.chmod(0o555)
    r = inst.probe(extra_env=_import_env(inst, TMPDIR=str(ro)))
    assert r.returncode == 3, r.stdout + r.stderr
    assert _imported_members(inst) == [(".", True, 0o755, 0, 0)]


def test_scratch_image_import_leaves_hostile_tmpdir_and_sentinel_alone(tmp_path):
    inst = Install(tmp_path)
    hostile = tmp_path / "hostile-tmp"
    hostile.mkdir()
    hostile.chmod(0o777)
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("precious\n")
    sentinel.chmod(0o600)
    r = inst.probe(extra_env=_import_env(inst, TMPDIR=str(hostile)))
    assert r.returncode == 3, r.stdout + r.stderr
    assert (inst.tmp / "tmpdir-at-import").read_text() == ""   # nothing there while importing
    assert list(hostile.iterdir()) == []
    assert sentinel.read_text() == "precious\n" and sentinel.stat().st_mode & 0o777 == 0o600


def test_scratch_image_import_failure_is_reported(tmp_path):
    inst = Install(tmp_path)
    log = tmp_path / "podman.log"
    pod = inst.bin / "podman"
    pod.write_text(pod.read_text().replace("#!/bin/sh\n", f"#!/bin/sh\necho \"$*\" >> '{log}'\n", 1))
    r = inst.probe(extra_env=_import_env(inst, T3S_FAKE_IMPORT_RC="1"))
    assert r.returncode == 1
    assert ("FAIL podman_runtime: could not import the empty scratch image "
            "localhost/tier3s-probe:empty (rc=1: fake import failed: boom)") in r.stdout, r.stdout
    assert "FAIL label_disable: not checked: scratch image import failed" in r.stdout
    assert " create " not in log.read_text()


# --- root runs only a root-controlled checkout ------------------------------

@pytest.mark.parametrize("how", ["dir", "pin"])
def test_root_refuses_untrusted_checkout(tmp_path, how):
    from test_tier3s_provision import root_capable_cmd, untrusted_checkout
    script, why = untrusted_checkout(tmp_path, "probe.sh", how)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QDISTRO_PROBE_")}
    r = subprocess.run(root_capable_cmd(["bash", str(script)]), env=env, capture_output=True, text=True)
    assert r.returncode == 2, r.stdout + r.stderr
    assert r.stdout.strip() == ("REFUSE checkout: refusing to run as root from a checkout another user "
                                f"could modify: {why} (use a root-owned copy)")


# --- the caller's PATH never supplies a tool (astra fix r3 P2) -------------

def test_probe_never_uses_caller_path_tools(tmp_path):
    """Real (non-test) mode, stopped early on purpose (help; a refused test
    hook) so nothing beyond the bootstrap runs on the host."""
    from test_tier3s_provision import BASH, shadow_path
    shadow, marker = shadow_path(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("QDISTRO_PROBE_")}
    env["PATH"] = f"{shadow}:{os.environ['PATH']}"
    r = subprocess.run([BASH, str(SCRIPT), "-h"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "PREREQUISITE SCREEN" in r.stdout
    r = subprocess.run([BASH, str(SCRIPT)], cwd=tmp_path, env=dict(env, QDISTRO_PROBE_PIN="x"),
                       capture_output=True, text=True)
    assert r.returncode == 2 and "QDISTRO_PROBE_PIN is a unit-test hook" in r.stderr
    assert not marker.exists(), "caller PATH tool ran: " + marker.read_text()


# --- runsc state root (CONTRACT.md D-A1) ------------------------------------

def test_state_root_passes_when_provisioned(tmp_path):
    inst = Install(tmp_path)
    r = run(inst.root, pin=inst.pin, path_prepend=inst.bin)
    assert f"PASS state_root: {inst.state_root} (uid {os.getuid()} 0700" in r.stdout, r.stdout


@pytest.mark.parametrize("damage", ["missing", "mode", "symlink", "base-mode"])
def test_state_root_missing_or_loose_fails(tmp_path, damage):
    inst = Install(tmp_path)
    if damage == "missing":
        inst.state_root.rmdir()
    elif damage == "mode":
        inst.state_root.chmod(0o750)
    elif damage == "symlink":
        inst.state_root.rmdir()
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        inst.state_root.symlink_to(tmp_path / "elsewhere")
    else:
        inst.state_root.parent.chmod(0o775)
    r = run(inst.root, pin=inst.pin, path_prepend=inst.bin)
    assert r.returncode == 1, r.stdout
    assert "FAIL state_root:" in r.stdout
    assert "systemd-tmpfiles --create qdistro-tier3s.conf" in r.stdout
