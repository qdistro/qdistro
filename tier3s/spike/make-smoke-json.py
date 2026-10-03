#!/usr/bin/env python3
"""Regenerate tier3s/spike/smoke.json = tier-2 weston-terminal.json + syslog.

Phase S (03, step 1): the smoke profile is the tier-2 weston-terminal
profile plus exactly one ALLOW entry for syslog(2). Run from the repo root;
--check exits 1 if the checked-in smoke.json differs from a fresh render.
"""
import hashlib, json, sys

SRC = "tier2/seccomp/weston-terminal.json"
DST = "tier3s/spike/smoke.json"


def render():
    raw = open(SRC, "rb").read()
    d = json.loads(raw)
    d["comment"] = (
        "tier3s Phase S smoke profile = tier2/seccomp/weston-terminal.json (sha256 "
        + hashlib.sha256(raw).hexdigest()
        + ") plus one ALLOW entry for syslog (gVisor dmesg). Generated, do not edit; "
        "regenerate with tier3s/spike/make-smoke-json.py. Original comment: " + d["comment"])
    d["syscalls"].append({
        "names": ["syslog"], "action": "SCMP_ACT_ALLOW",
        "comment": "tier3s Phase S: gVisor's dmesg uses syslog(2) "
                   "(pkg/sentry/syscalls/linux/sys_syslog.go); dmesg --syslog "
                   "probes the syscall, not /dev/kmsg"})
    return json.dumps(d, indent="\t") + "\n"


if __name__ == "__main__":
    out = render()
    if sys.argv[1:] == ["--check"]:
        sys.exit(0 if open(DST).read() == out else 1)
    open(DST, "w").write(out)
