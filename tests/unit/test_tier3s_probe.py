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


def run(root, user=None, pin=None, path_prepend=None, extra_env=None, timeout=None):
    env = dict(os.environ, QDISTRO_PROBE_ROOT=str(root))
    if pin is not None:
        env["QDISTRO_PROBE_PIN"] = str(pin)
    if path_prepend is not None:
        env["PATH"] = f"{path_prepend}:{env['PATH']}"
    env.update(extra_env or {})
    args = ["bash", str(SCRIPT), "--user", user or ME]
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout)


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
    one-entry archive. The import itself creates no temporary directory, and
    nss_q's per-attempt scratch file falls back to /tmp or /dev/shm when
    TMPDIR is not writable."""
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
# C2 model A: per-uid runsc roots exist only for qt3s-* podman callers (the
# spawn creates them at launch); probing a non-caller checks only the base.

def qt3s_bin(tmp_path):
    """PATH fakes resolving `qt3s-probe` to the caller's own uid, so the
    probe's qt3s-* per-uid checks run without a real account or NSS writes."""
    uid = os.getuid()
    b = tmp_path / "qt3sbin"
    b.mkdir(exist_ok=True)
    (b / "id").write_text(
        "#!/bin/sh\nfor a; do last=\"$a\"; done\n"
        'case "${last:-}" in -*|"") exec /usr/bin/id "$@" ;; esac\n'
        'case " $* " in *" -u "*) echo %d; exit 0 ;; esac\n'
        "echo 'uid=%d(qt3s-probe) gid=100(qt3s-probe)'\n" % (uid, uid))
    (b / "getent").write_text(
        '#!/bin/sh\ncase "$1 $2" in\n'
        "  'passwd qt3s-probe') echo 'qt3s-probe:x:%d:100::/home/qt3s-probe:/bin/sh' ;;\n"
        "esac\nexit 0\n" % uid)
    for f in ("id", "getent"):
        (b / f).chmod(0o755)
    return b


def qt3s_probe(inst, tmp_path):
    return run(inst.root, user="qt3s-probe", pin=inst.pin,
               path_prepend=f"{qt3s_bin(tmp_path)}:{inst.bin}")


def test_state_root_passes_for_a_provisioned_qt3s_caller(tmp_path):
    inst = Install(tmp_path)
    r = qt3s_probe(inst, tmp_path)
    assert f"PASS state_root: {inst.state_root} (uid {os.getuid()} 0700" in r.stdout, r.stdout


def test_state_root_is_base_only_for_a_non_caller(tmp_path):
    """admin (and any non-qt3s user) never invokes runsc: a missing per-uid
    dir is not a prerequisite — only the root-owned base is."""
    inst = Install(tmp_path)
    inst.state_root.rmdir()
    r = run(inst.root, pin=inst.pin, path_prepend=inst.bin)
    assert f"PASS state_root: {inst.state_root.parent} (per-uid dir is a qt3s-* caller prerequisite" \
        in r.stdout, r.stdout


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
    r = qt3s_probe(inst, tmp_path)
    assert r.returncode == 1, r.stdout
    assert "FAIL state_root:" in r.stdout
    if damage == "base-mode":
        assert "systemd-tmpfiles --create qdistro-tier3s.conf" in r.stdout
    else:
        assert "spawn-tier3s.sh creates it" in r.stdout


def test_a_stalled_nss_answer_is_not_a_lookup(tmp_path):
    """sol r5 P3-4: as_user's `timeout 5 getent … | cut` dropped timeout's
    status — a provider that prints a complete passwd line and then wedges
    is killed at the bound, and what it printed is not a result: runuser
    must never run."""
    inst = Install(tmp_path)
    other = next(p for p in pwd.getpwall() if p.pw_name != ME)
    b = tmp_path / "nssbin"; b.mkdir()
    marker = tmp_path / "runuser-ran"
    (b / "getent").write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{other.pw_name}:x:{other.pw_uid}:{other.pw_gid}"
        f"::{other.pw_dir or '/nonexistent'}:{other.pw_shell or '/sbin/nologin'}'\nsleep 60\n")
    (b / "runuser").write_text(f"#!/bin/sh\necho runuser >> '{marker}'\nexit 0\n")
    for f in ("getent", "runuser"):
        (b / f).chmod(0o755)
    r = run(inst.root, user=other.pw_name, pin=inst.pin, path_prepend=f"{b}:{inst.bin}")
    assert "FAIL nss:" in r.stdout, r.stdout
    # the verdict names the lookup and its status: a kill at the bound
    # (timeout's 124) is told apart from a missing entry (getent's 2)
    assert "(getent passwd rc=124; " in r.stdout, r.stdout
    assert not marker.exists(), "a killed NSS lookup still reached runuser"


def test_a_wedged_id_lookup_for_the_foreign_user_is_bounded(tmp_path):
    """fable A r3 P3-2, applied to id/id -u: EVERY foreign-user NSS lookup in
    the probe is bounded — a wedged provider hangs the spawn's probe (and the
    launch unit's start) otherwise. The fake id wedges only on a name
    argument, so self-lookups (id -u / id -un) still work."""
    inst = Install(tmp_path)
    other = next(p for p in pwd.getpwall() if p.pw_name != ME)
    b = tmp_path / "idbin"; b.mkdir()
    (b / "id").write_text(
        "#!/bin/sh\nfor a; do last=\"$a\"; done\n"
        "case \"${last:-}\" in -*|\"\") exec /usr/bin/id \"$@\" ;; esac\n"
        "sleep 600\n")
    (b / "id").chmod(0o755)
    t0 = time.time()
    r = run(inst.root, user=other.pw_name, pin=inst.pin,
            path_prepend=f"{b}:{inst.bin}", timeout=60)
    assert time.time() - t0 < 45, "a wedged NSS lookup hung the probe"
    assert "FAIL" in r.stdout, r.stdout


def test_a_uid_printed_before_a_stall_is_not_a_lookup(tmp_path):
    """sol B-i r1 P1-3: `timeout 5 id -u <user>` is a valid result only at
    rc 0 with exactly one numeric uid line. A lookup that PRINTS a complete
    uid line and then wedges is killed at the bound (rc 124) — what it
    printed is not a result. Both id -u sites (the state-root uid and
    as_user's) status-gate, or the probe adopts a uid a dead lookup typed.
    The fake prints the user's REAL uid, so only the timeout status — not
    the output shape — can tell the lookup failed."""
    inst = Install(tmp_path)
    other = next(p for p in pwd.getpwall() if p.pw_name != ME)
    b = tmp_path / "idbin"; b.mkdir()
    (b / "id").write_text(
        "#!/bin/sh\nfor a; do last=\"$a\"; done\n"
        # self-lookups (id -u / id -un / bare id) still work
        "case \"${last:-}\" in -*|\"\") exec /usr/bin/id \"$@\" ;; esac\n"
        # `id -u <name>`: print the real uid, THEN wedge; `id <name>` answers
        "case \" $* \" in *\" -u \"*) echo %d; sleep 600 ;;"
        " *) exec /usr/bin/id \"$@\" ;; esac\n" % other.pw_uid)
    (b / "id").chmod(0o755)
    t0 = time.time()
    r = run(inst.root, user=other.pw_name, pin=inst.pin,
            path_prepend=f"{b}:{inst.bin}", timeout=60)
    assert time.time() - t0 < 45, "a stalled uid lookup hung the probe"
    # the state-root site names the failed lookup, not a missing directory
    assert "the uid lookup for" in r.stdout, r.stdout
    # and as_user's site never adopts the printed prefix either
    assert "FAIL nss:" in r.stdout, r.stdout
    assert "(id -u rc=124; " in r.stdout, r.stdout


# --- NSS retry semantics (nss_q) --------------------------------------------
# A lookup killed at the bound (rc 124) is retried — on a saturated VM disk a
# cold page-in of the lookup's binary/modules/passwd file plausibly outlasted
# one 5 s bound (full-20261006T175536Z-3524705) — while ONE waiting budget is
# shared across every nss_q call site in a probe (sol r152 P2). A missing
# entry is final at once; a wedged provider is paid for once per probe; and a
# TERM-resistant or uninterruptible child cannot stretch the budget past the
# SIGKILL escalation / outer cap (sol r152 P1).

def counting_getent(b, other, stall_first, rc_after=0):
    """getent that wedges on its first `stall_first` passwd calls for
    `other`, then answers with rc_after (0: the real entry). Each call is
    counted in b/getent.calls."""
    calls = b / "getent.calls"
    line = (f"{other.pw_name}:x:{other.pw_uid}:{other.pw_gid}"
            f"::{other.pw_dir or '/nonexistent'}:{other.pw_shell or '/sbin/nologin'}")
    (b / "getent").write_text(
        "#!/bin/sh\n"
        f"echo x >> '{calls}'\n"
        f"n=$(wc -l < '{calls}')\n"
        f"if [ \"$n\" -le {stall_first} ]; then sleep 60; fi\n"
        + (f"printf '%s\\n' '{line}'\n" if rc_after == 0 else "")
        + f"exit {rc_after}\n")
    (b / "getent").chmod(0o755)
    return calls


def test_a_lookup_killed_once_at_the_bound_is_retried(tmp_path):
    inst = Install(tmp_path)
    other = next(p for p in pwd.getpwall() if p.pw_name != ME)
    b = tmp_path / "nssbin"; b.mkdir()
    calls = counting_getent(b, other, stall_first=1)
    r = run(inst.root, user=other.pw_name, pin=inst.pin,
            path_prepend=f"{b}:{inst.bin}", timeout=60)
    assert "FAIL nss:" not in r.stdout, r.stdout
    assert len(calls.read_text().splitlines()) == 2, "one kill, one retry"


def test_a_missing_entry_is_final_without_retry(tmp_path):
    inst = Install(tmp_path)
    other = next(p for p in pwd.getpwall() if p.pw_name != ME)
    b = tmp_path / "nssbin"; b.mkdir()
    calls = counting_getent(b, other, stall_first=0, rc_after=2)
    t0 = time.time()
    r = run(inst.root, user=other.pw_name, pin=inst.pin,
            path_prepend=f"{b}:{inst.bin}", timeout=60)
    assert time.time() - t0 < 30, "a missing entry was waited on"
    assert "FAIL nss:" in r.stdout and "(getent passwd rc=2; 1 tries; " in r.stdout, r.stdout
    assert len(calls.read_text().splitlines()) == 1, "a missing entry was retried"


def test_a_wedged_provider_costs_one_retry_budget_per_probe(tmp_path):
    """Every attempt wedges: the lookup fails once the shared budget
    (NSS_TRIES x (NSS_BOUND + NSS_KILL_GRACE) = 21 s of NSS wait) is spent —
    three full 5 s bounds plus whatever shortened bound the remainder buys —
    and every later NSS lookup in the same probe fails at once instead of
    paying the budget again (the spawn waits on the probe under its token
    lock)."""
    inst = Install(tmp_path)
    other = next(p for p in pwd.getpwall() if p.pw_name != ME)
    b = tmp_path / "idbin"; b.mkdir()
    calls = b / "id.calls"
    (b / "id").write_text(
        "#!/bin/sh\nfor a; do last=\"$a\"; done\n"
        "case \"${last:-}\" in -*|\"\") exec /usr/bin/id \"$@\" ;; esac\n"
        f"echo x >> '{calls}'\nsleep 600\n")
    (b / "id").chmod(0o755)
    t0 = time.time()
    r = run(inst.root, user=other.pw_name, pin=inst.pin,
            path_prepend=f"{b}:{inst.bin}", timeout=60)
    took = time.time() - t0
    n = len(calls.read_text().splitlines())
    assert 3 <= n <= 6, calls.read_text()
    assert took < 30, f"a wedged provider was waited on per lookup site ({took:.0f} s)"
    assert "FAIL user: the NSS lookup for" in r.stdout and "timed out" in r.stdout, r.stdout
    assert "shared NSS wait budget" in r.stdout, r.stdout
    assert "FAIL nss:" in r.stdout and "(id -u rc=124; 0 tries; " in r.stdout, r.stdout


def test_a_term_resistant_lookup_is_killed_at_the_escalation(tmp_path):
    """sol r152 P1: `timeout <bound>` alone sends TERM and then waits on the
    child forever — a lookup that ignores TERM would hang the probe and
    never reach a retry or the wedge. The attempt now runs under
    `timeout -k` (SIGKILL after the grace) inside an outer cap, so a
    TERM-resistant child costs bound+grace per attempt and even an
    uninterruptible child cannot stretch the shared budget."""
    inst = Install(tmp_path)
    other = next(p for p in pwd.getpwall() if p.pw_name != ME)
    b = tmp_path / "idbin"; b.mkdir()
    calls = b / "id.calls"
    (b / "id").write_text(
        "#!/bin/sh\nfor a; do last=\"$a\"; done\n"
        "case \"${last:-}\" in -*|\"\") exec /usr/bin/id \"$@\" ;; esac\n"
        f"echo x >> '{calls}'\n"
        "trap '' TERM\nsleep 600\n")
    (b / "id").chmod(0o755)
    t0 = time.time()
    r = run(inst.root, user=other.pw_name, pin=inst.pin,
            path_prepend=f"{b}:{inst.bin}", timeout=90)
    took = time.time() - t0
    assert took < 40, f"a TERM-resistant NSS lookup hung the probe ({took:.0f} s)"
    n = len(calls.read_text().splitlines())
    assert 2 <= n <= 4, calls.read_text()
    assert "FAIL user: the NSS lookup for" in r.stdout and "timed out" in r.stdout, r.stdout


def test_the_nss_budget_is_shared_across_lookup_sites(tmp_path):
    """sol r152 P2: the retry budget is ONE per-probe wait allowance, not a
    fresh NSS_TRIES x NSS_BOUND at every nss_q site. Earlier lookups that
    recover after stalls still charge their wait to the budget, so a
    provider that wedges later cannot be waited on for the full budget
    again."""
    inst = Install(tmp_path)
    other = next(p for p in pwd.getpwall() if p.pw_name != ME)
    b = tmp_path / "nssbin"; b.mkdir()
    idc = b / "id.calls"
    (b / "id").write_text(
        "#!/bin/sh\nfor a; do last=\"$a\"; done\n"
        "case \"${last:-}\" in -*|\"\") exec /usr/bin/id \"$@\" ;; esac\n"
        f"echo x >> '{idc}'\n"
        f"n=$(wc -l < '{idc}')\n"
        "if [ \"$n\" -le 2 ]; then sleep 60; fi\n"
        "exec /usr/bin/id \"$@\"\n")
    # getent wedges on every call: by the time the nss section asks for it,
    # the earlier id stalls have already eaten ~10 s of the 21 s budget, so
    # it gets the remainder only — never another 3 x 5 s.
    calls = counting_getent(b, other, stall_first=99)
    (b / "id").chmod(0o755)
    t0 = time.time()
    r = run(inst.root, user=other.pw_name, pin=inst.pin,
            path_prepend=f"{b}:{inst.bin}", timeout=90)
    took = time.time() - t0
    n = len(calls.read_text().splitlines())
    assert 1 <= n <= 2, calls.read_text()
    assert took < 40, f"NSS lookups were paid a fresh budget per site ({took:.0f} s)"
    assert "FAIL nss:" in r.stdout and "(getent passwd rc=124; " in r.stdout, r.stdout
