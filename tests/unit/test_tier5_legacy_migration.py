"""Upgrade from the root-run tier-5 lifecycle (sol t5-r1 P2).

scripts/install/lib/tier5-legacy.sh runs unmodified with its state paths
pointed at a scratch tree. Old wrappers, old guests and old lock holders are
real processes; the tests assert which files survive.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts/install/lib/tier5-legacy.sh"
INSTALLER = ROOT / "scripts/install/install-tier5-for-vm.sh"

pytestmark = pytest.mark.slow

# a uid no process has: the wrapper scan never matches a stray process
UNUSED_UID = 2**31 - 7


class Legacy:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.hold = tmp / "run/qdistro-tier5"
        self.boot = tmp / "run/qdistro-tier5-boot"
        self.owned = tmp / "var/lib/qdistro/tier5-owned"
        self.procs: list[subprocess.Popen] = []

    def populate(self) -> None:
        self.hold.mkdir(parents=True)
        for kind in ("hold", "create", "live"):
            (self.hold / f"{kind}-t5-a").write_text("")
        self.boot.mkdir(parents=True)
        (self.boot / "t5-a.qcow2").write_text("")
        self.owned.mkdir(parents=True)
        (self.owned / "t5-a").write_text("tier5/a 1 2\n")

    def state(self) -> list[bool]:
        return [self.hold.exists(), self.boot.exists(), self.owned.exists()]

    def retire(self, wrapper_uid: int = UNUSED_UID,
               pythonpath: str | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ,
                   T5_LEGACY_HOLD_DIR=str(self.hold),
                   T5_LEGACY_BOOT_DIR=str(self.boot),
                   T5_LEGACY_OWNED_DIR=str(self.owned),
                   T5_LEGACY_WRAPPER_UID=str(wrapper_uid))
        if pythonpath:
            env["PYTHONPATH"] = pythonpath
        return subprocess.run(
            ["bash", "-c", f'. "{LIB}"; t5_legacy_retire'],
            env=env, capture_output=True, text=True, timeout=60, check=False)

    def start(self, argv: list[str], ready: Path | None = None) -> None:
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.procs.append(p)
        if ready is not None:
            deadline = time.monotonic() + 10
            while not ready.exists():
                assert time.monotonic() < deadline, f"{argv} never ready"
                time.sleep(0.02)

    def stop(self) -> None:
        for p in self.procs:
            p.kill()
            p.wait()
        self.procs.clear()


@pytest.fixture
def legacy(tmp_path):
    lg = Legacy(tmp_path)
    yield lg
    lg.stop()


def test_nothing_to_migrate(legacy):
    r = legacy.retire()
    assert r.returncode == 0, r.stdout + r.stderr


def test_quiescent_old_lifecycle_is_migrated(legacy):
    # ensures: with no old wrapper, lock holder or guest, all old state —
    #          locks, boot mounts, the root registry — is retired
    legacy.populate()
    r = legacy.retire()
    assert r.returncode == 0, r.stdout + r.stderr
    assert legacy.state() == [False, False, False]


def test_held_old_lock_defers_migration(legacy):
    # ensures: an old wrapper's shared session hold (its whole session) is
    #          seen, and nothing is deleted under it — deleting the lock
    #          files would split the namespace from the new admin-run one
    legacy.populate()
    ready = legacy.tmp / "locked"
    # one process holds the lock (no forked child keeps it past stop())
    legacy.start([sys.executable, "-c",
                  "import fcntl, sys, time\n"
                  "f = open(sys.argv[1], 'a')\n"
                  "fcntl.flock(f, fcntl.LOCK_SH)\n"
                  "open(sys.argv[2], 'w')\n"
                  "time.sleep(60)\n",
                  str(legacy.hold / "hold-t5-a"), str(ready)], ready)
    r = legacy.retire()
    assert r.returncode == 1, r.stdout + r.stderr
    assert "held by a live wrapper" in r.stdout
    assert legacy.state() == [True, True, True]
    # the old session ends: the re-run migrates
    legacy.stop()
    r = legacy.retire()
    assert r.returncode == 0, r.stdout + r.stderr
    assert legacy.state() == [False, False, False]


def test_running_old_wrapper_defers_migration(legacy):
    # ensures: an old root-run wrapper that holds no lock yet (still in its
    #          preflight/reaper) also defers; new-lifecycle wrappers (another
    #          uid) never count
    legacy.populate()
    script = legacy.tmp / "src/tier5-vm/spawn-tier5.sh"
    script.parent.mkdir(parents=True)
    ready = legacy.tmp / "wrapper-up"
    script.write_text(f": >{ready}\nsleep 60\n")
    legacy.start(["bash", str(script), "--vm", "t5-a", "--", "foot"], ready)
    r = legacy.retire(wrapper_uid=UNUSED_UID)
    assert r.returncode == 0, r.stdout + r.stderr
    legacy.populate()
    r = legacy.retire(wrapper_uid=os.getuid())
    assert r.returncode == 1, r.stdout + r.stderr
    assert "old root-run tier-5 wrapper is running" in r.stdout
    assert legacy.state() == [True, True, True]


def test_old_guest_without_wrapper_defers_migration(legacy):
    # ensures: a guest still booted from the old bind-mount dir (its wrapper
    #          was killed) defers too — its disk must not be unmounted and
    #          its state not dropped while it runs
    legacy.populate()
    ready = legacy.tmp / "guest-up"
    legacy.start([sys.executable, "-c",
                  "import sys,time; open(sys.argv[2],'w'); time.sleep(60)",
                  f"file={legacy.boot}/t5-a.qcow2", str(ready)], ready)
    r = legacy.retire()
    assert r.returncode == 1, r.stdout + r.stderr
    assert "boots from" in r.stdout
    assert legacy.state() == [True, True, True]


def test_unverifiable_process_scan_defers_migration(legacy, tmp_path):
    # ensures: a process table the scan cannot read is never "quiescent"
    legacy.populate()
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import builtins, errno, os, re\n"
        "_o = builtins.open\n"
        "def _g(f, *a, **k):\n"
        "    if re.fullmatch(r'/proc/\\d+/cmdline', str(f)):\n"
        "        raise PermissionError(errno.EACCES, 'denied', str(f))\n"
        "    return _o(f, *a, **k)\n"
        "builtins.open = _g\n")
    r = legacy.retire(pythonpath=str(shim))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cannot verify" in r.stdout
    assert legacy.state() == [True, True, True]


def test_installer_retires_only_through_the_quiescence_check():
    # ensures: the installer no longer deletes the old state unconditionally
    #          and runs the check only after the wrappers and policy that
    #          stop new old-lifecycle launches are replaced
    text = INSTALLER.read_text()
    assert "rm -rf /var/lib/qdistro/tier5-owned" not in text
    assert "rm -rf /run/qdistro-tier5" not in text
    retire = text.index("t5_legacy_retire")
    assert text.index('cat > "$dst"') < retire
    assert text.index("org.qdistro.tier5.policy\"\n") < retire
