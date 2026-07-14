#!/usr/bin/env python3
"""Test launcher for qdwin's broker-owned external RDP listener fd.

The real pairing service will own socket creation and mTLS relay admission.
This helper proves the privilege boundary: root creates a private AF_UNIX
listener, passes only its fd to an admin-owned qdwin, and never exposes a qdwin
TCP listener.  The VM gate's root socat is only an outer-relay surrogate.
"""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import signal
import socket
import subprocess
import time
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", required=True, type=Path)
    ap.add_argument("--xdg-runtime", required=True, type=Path)
    ap.add_argument("--config", required=True)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--rdp-module", required=True)
    ap.add_argument("--shell", required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--width", required=True, type=int)
    ap.add_argument("--height", required=True, type=int)
    args = ap.parse_args()

    listener_path = args.runtime / "rdp-listener.sock"
    listener_path.unlink(missing_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(listener_path))
    os.chmod(listener_path, 0o600)
    listener.listen(1)

    user = pwd.getpwnam("admin")
    groups = [entry.gr_gid for entry in grp.getgrall()
              if "admin" in entry.gr_mem]
    groups.append(user.pw_gid)

    def drop_privileges() -> None:
        os.setgroups(sorted(set(groups)))
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)

    module_dir = "/usr/lib64/libweston-14"
    module_map = ";".join((
        f"headless-backend.so={module_dir}/headless-backend.so",
        f"rdp-backend.so={args.rdp_module}",
    ))
    env = os.environ.copy()
    env.update({
        "HOME": user.pw_dir,
        "XDG_RUNTIME_DIR": str(args.xdg_runtime),
        "QDWIN_ALLOWED_UID": str(user.pw_uid),
        "QDWIN_ALLOWED_LOCKER_ANY": "1",
        "QDWIN_ENABLE_SCREENSHOOTER": "1",
        "QDWIN_TEST_PLACE_APPID": "qdwin-marker-client",
        "QDWIN_TEST_PLACE_X": str(args.width - 256),
        "QDWIN_TEST_PLACE_Y": "200",
        "WESTON_MODULE_MAP": module_map,
    })
    command = [
        "weston", "--backends=headless,rdp", "--renderer=pixman",
        f"--shell={args.shell}", f"--config={args.config}",
        f"--width={args.width}", f"--height={args.height}",
        f"--external-listener-fd={listener.fileno()}",
        f"--rdp-tls-cert={args.cert}", f"--rdp-tls-key={args.key}",
        f"--socket={args.socket}", f"--log={args.runtime / 'weston.log'}",
    ]
    process = subprocess.Popen(
        command, env=env, pass_fds=(listener.fileno(),),
        preexec_fn=drop_privileges)
    (args.runtime / "weston.pid").write_text(
        f"{process.pid}\n", encoding="ascii")
    os.chown(args.runtime / "weston.pid", user.pw_uid, user.pw_gid)

    stopping = False

    def stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while process.poll() is None and not stopping:
            time.sleep(0.1)
        if stopping and process.poll() is None:
            process.terminate()
        return process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        listener.close()
        listener_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
