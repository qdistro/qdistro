#!/usr/bin/python3
"""Small process-lifetime oracle for remote nested supervisor tests."""
from __future__ import annotations

import os
import signal
import sys
import time


def option(name: str) -> int | None:
    try:
        return int(sys.argv[sys.argv.index(name) + 1])
    except ValueError:
        return None


if "--helper-fd" in sys.argv:
    role = "controller"
    inherited = [option("--endpoint-fd"), option("--helper-fd")]
elif "--grant-fd" in sys.argv:
    role = "launcher"
    inherited = [
        option("--grant-fd"), option("--secret-fd"),
        option("--tls-cert-fd"), option("--tls-key-fd"),
        option("--peer-cert-fd"), option("--local-fd"),
    ]
else:
    role = "helper"
    inherited = [option("--controller-fd")]
    for name in ("--config-fd", "--close-fd"):
        value = option(name)
        if value is not None:
            inherited.append(value)

for fd in inherited:
    if fd is None:
        raise SystemExit(90)
    os.fstat(fd)

log_path = os.environ.get("MM_SUPERVISOR_TEST_LOG")
if log_path:
    with open(log_path, "a", encoding="ascii") as log:
        log.write(f"{role} {os.getpid()} {len(inherited)}\n")

if role == "helper":
    time.sleep(0.1)
    raise SystemExit(int(os.environ.get("MM_SUPERVISOR_HELPER_EXIT", "0")))

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(10)
