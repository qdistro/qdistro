"""tier3s/probe.sh profile gate and first-missing reporting (QDISTRO_PROBE_ROOT hook)."""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "tier3s" / "probe.sh"


def run(root, user=None):
    env = dict(os.environ, QDISTRO_PROBE_ROOT=str(root))
    args = ["bash", str(SCRIPT), "--user", user or os.environ.get("USER", "root")]
    return subprocess.run(args, env=env, capture_output=True, text=True)


def test_refuses_without_profile(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 2 and "REFUSE profile" in r.stdout


def test_refuses_hardened_profile(tmp_path):
    (tmp_path / "etc/qdistro").mkdir(parents=True)
    (tmp_path / "etc/qdistro/profile").write_text("QDISTRO_PROFILE=release\n")
    r = run(tmp_path)
    assert r.returncode == 2 and "release" in r.stdout


def test_names_first_missing_when_runsc_absent(tmp_path):
    (tmp_path / "etc/qdistro").mkdir(parents=True)
    (tmp_path / "etc/qdistro/profile").write_text("QDISTRO_PROFILE=dev\n")
    r = run(tmp_path)
    assert r.returncode == 1
    last = r.stdout.strip().splitlines()[-1]
    assert last.startswith("RESULT FAIL: first missing prerequisite:")
    fails = [l for l in r.stdout.splitlines() if l.startswith("FAIL ")]
    assert fails and fails[0].split()[1].rstrip(":") in last
    assert "FAIL runsc: not provisioned" in r.stdout


def test_test_root_never_exits_zero(tmp_path):
    r = run(tmp_path)
    assert r.returncode != 0
    assert r.stdout.startswith("TEST MODE:")


def test_clean_test_root_exits_3_not_0(tmp_path):
    """Probe's TEST mode must never report a host PASS. Full PASS needs podman,
    so this only checks the exit contract when not all checks pass either way."""
    r = run(tmp_path)
    assert r.returncode in (1, 2, 3) and r.returncode != 0
