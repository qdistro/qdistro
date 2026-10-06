"""Regression for the false green during the entrypoint startup window."""
from pathlib import Path
import os
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = ROOT / "tests/integration/vm/probes/presentation-live.sh"


@pytest.mark.parametrize(
    "marker,running,app,expected",
    [(False, True, True, 1), (True, False, True, 1),
     (True, True, False, 1), (True, True, True, 0)],
)
def test_readiness_requires_handoff_and_live_app(marker, running, app, expected):
    source = PROBE.read_text()
    functions = source[source.index("inner_running() {"):source.index("shared_labels() {")]
    harness = r"""
as_admin() {
    case "$1 $2" in
        'podman logs')
            [ "$MARKER" = 1 ] && echo "inner weston up; exec'ing app: qfileman"
            return 0 ;;
        'podman inspect') echo "$RUNNING" ;;
        'podman exec') [ "$APP" = 1 ] ;;
        *) return 2 ;;
    esac
}
sleep() { :; }
# Keep failure diagnostics inside the harness; no live VM/log dependency.
cat() { :; }
wait_inner test-container
"""
    result = subprocess.run(
        ["bash", "-c", functions + harness], capture_output=True, text=True,
        env={**os.environ, "MARKER": str(int(marker)),
             "RUNNING": str(running).lower(), "APP": str(int(app))}, timeout=5,
    )
    assert result.returncode == expected, result.stderr


@pytest.mark.parametrize("pid1,ready", [
    (b"/bin/bash\0/usr/local/bin/qdistro-tier2-entrypoint\0qfileman\0", False),
    (b"/usr/bin/python3\0/usr/bin/qfileman\0", True),
])
def test_readiness_rejects_entrypoint_argv(monkeypatch, pid1, ready):
    import builtins
    import io
    import socket

    source = PROBE.read_text()
    code = source.split('as_admin podman exec "$container" python3 -c \'', 1)[1].split("\n' >/dev/null", 1)[0]
    class Listener:
        def settimeout(self, timeout):
            assert timeout == 2

        def connect(self, path):
            assert path == "/run/user/1000/wayland-tier2"

    monkeypatch.setattr(builtins, "open", lambda *args: io.BytesIO(pid1))
    monkeypatch.setattr(os, "readlink", lambda path: "/usr/bin/python3.13")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setattr(socket, "socket", lambda *args: Listener())
    if ready:
        exec(code, {})
    else:
        with pytest.raises(AssertionError):
            exec(code, {})


@pytest.mark.parametrize("target", ["rootfs", "presentation"])
@pytest.mark.parametrize("created,write_rc", [(False, 1), (True, 0), (True, 1)])
def test_s40_write_denials_check_creation(tmp_path, target, created, write_rc):
    source = (ROOT / "tests/integration/vm/s40-tier2-hardening.sh").read_text()
    if target == "rootfs":
        block = source[source.index('# Redirect tests creation itself;'):source.index('# --- 5+6.')]
        expected = "write / blocked by read-only"
    else:
        block = source[source.index('PRES_PROBE='):source.index('\nSIBLINGS=')]
        block = block.replace('/var/lib/qdistro/presentation', str(tmp_path))
        expected = "container write into presentation directory denied"
    # Run the real assertion with a fake exec; a false denial (create then
    # return nonzero) must fail for both the rootfs and host-visible bind.
    harness = '''
CONTAINER=fake
PASSCOUNT=0; FAILCOUNT=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
runuser() {
    case "$*" in
        *touch*) echo 'obsolete touch probe' >&2; exit 99 ;;
        *': > '*)
            if [ "$CREATED" = 1 ] && [ -n "${PRES_PROBE:-}" ]; then : > "$PRES_PROBE"; fi
            return "$WRITE_RC" ;;
        *'[ ! -e /no-such-write ]'*) [ "$CREATED" = 0 ] ;;
        *'rm -f'*) return 0 ;;
        *) echo "unexpected fake exec: $*" >&2; exit 98 ;;
    esac
}
'''
    proc = subprocess.run(["bash", "-c", harness + block + '\nexit "$FAILCOUNT"'],
                          env={**os.environ, "CREATED": str(int(created)), "WRITE_RC": str(write_rc)},
                          capture_output=True, text=True, timeout=5)
    assert proc.returncode == int(created), proc.stdout + proc.stderr
    assert (f"PASS: {expected}" in proc.stdout) == (not created), proc.stdout
