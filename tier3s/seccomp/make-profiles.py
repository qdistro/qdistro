#!/usr/bin/env python3
"""Render tier3s/seccomp/<workload>.json from the tier-2 profile + per-workload decisions.

Tier 3s never shares a tier-2 profile file (06 D-A4): each workload profile is
derived here from tier2/seccomp/weston-terminal.json (sha256 recorded in the
output) plus an explicit decision for every call the Phase S spike saw the
runsc converter turn into a workload-visible EPERM. Under runsc
(--oci-seccomp) every ERRNO action, the default included, returns EPERM:
defaultErrnoRet=38 does NOT restore ENOSYS (runsc/specutils/seccomp/
seccomp.go hard-codes EPERM), so glibc's ENOSYS-keyed fallbacks do not run.
The converter also drops syscall names it does not know, silently; the A-iii
VM driver asserts every ALLOW below actually reaches the Sentry. A denial is
visible only in the Sentry debug log ("Syscall <nr>: denied by seccomp").

Rule for a decision: ALLOW only what the workload needs to behave correctly;
DENY (default EPERM) what it does not use or whose failure its callers ignore.
Never ALLOW a call just to keep the debug log quiet.

Run from the repo root; --check exits 1 if a checked-in profile differs.
"""
import hashlib
import json
import sys

SRC = "tier2/seccomp/weston-terminal.json"

# workload -> {syscall: (decision, reason)}; every key in DECIDE must be decided
DECIDE = ("fchmodat2", "llistxattr", "setfsuid", "setfsgid", "fadvise64", "link", "syslog")
WORKLOADS = {
    "headless-smoke": {
        "fchmodat2": ("DENY",
            "decided by the pin, not by us: runsc 20260928.0's converter logs 'OCI seccomp: "
            "ignoring syscall \"fchmodat2\"' and keeps the default, so an ALLOW entry would be "
            "inert (spike/logs/phase-a-20261002/feasibility/31). Effect: glibc lchmod(), "
            "fchmodat(AT_SYMLINK_NOFOLLOW) and coreutils chmod -h fail with EPERM (glibc falls "
            "back only on ENOSYS); plain chmod (fchmodat, nr 268) works. The smoke workload uses "
            "plain chmod only; the A-iii driver asserts both outcomes."),
        "llistxattr": ("ALLOW",
            "coreutils ls -l checks ACLs with llistxattr and prints 'Operation not permitted' "
            "on EPERM (Phase S C2): wrong output, not just log noise. Read-only metadata of "
            "files the workload can already stat, answered by the Sentry's VFS. The other "
            "*xattr calls stay denied: not used by this workload."),
        "setfsuid": ("DENY",
            "not used by the headless smoke workload (seen only from the Phase S terminals, "
            "non-fatal); identity-changing calls stay minimal. Terminal profiles decide in Phase B."),
        "setfsgid": ("DENY", "as setfsuid."),
        "fadvise64": ("DENY",
            "advisory only; coreutils (cat, cp) ignore its failure, so behaviour is "
            "correct without it. Expect 'Syscall 221: denied by seccomp' in the debug log."),
        "link": ("DENY",
            "hard links are not used by the headless smoke workload (Phase S saw link only "
            "from fontconfig's cache lock in the terminal images); linkat stays denied too."),
        "syslog": ("ALLOW",
            "dmesg --syslog reads the Sentry's synthetic kernel log through syslog(2) "
            "(pkg/sentry/syscalls/linux/sys_syslog.go): the in-sandbox banner the drivers "
            "record as corroboration. It never reaches the host kernel log."),
    },
    # The GUI terminal workloads (Phase B): the same derived profile, the same
    # seven decisions — a terminal inside the sandbox is also where the user
    # runs coreutils, so llistxattr/link stay ALLOW here too.
    "weston-terminal": {
        "fchmodat2": ("DENY",
            "decided by the pin, not by us: runsc 20260928.0's converter drops the name "
            "('OCI seccomp: ignoring syscall \"fchmodat2\"'), so an ALLOW entry would be "
            "inert — see the headless-smoke decision for the full effect."),
        "llistxattr": ("ALLOW",
            "as headless-smoke: `ls -l` inside the terminal checks ACLs with llistxattr and "
            "misprints 'Operation not permitted' on EPERM; read-only metadata, Sentry-answered."),
        "setfsuid": ("DENY",
            "Phase S saw setfsuid only from the terminal images' startup, non-fatally; the "
            "workload does not need identity-changing calls and they stay minimal."),
        "setfsgid": ("DENY", "as setfsuid."),
        "fadvise64": ("DENY",
            "advisory only; every Phase S caller (fontconfig, font rasterizers, coreutils) "
            "ignores its failure. Expect 'Syscall 221: denied by seccomp' in the debug log."),
        "link": ("ALLOW",
            "fontconfig takes its per-cache-dir lock with link/linkat inside the terminal "
            "images (Phase S); on EPERM it cannot write the font cache under "
            "/home/admin/.cache and the terminal's first render path breaks. Hard links "
            "between files the workload can already write inside the sandboxed VFS."),
        "syslog": ("DENY",
            "the GUI workloads have no dmesg banner step; a user typing dmesg in the "
            "terminal gets EPERM, which is correct posture, not a silent fallback."),
    },
    "foot": {
        "fchmodat2": ("DENY", "as weston-terminal (the pinned converter drops the name)."),
        "llistxattr": ("ALLOW", "as weston-terminal (ls -l inside the terminal)."),
        "setfsuid": ("DENY", "as weston-terminal (Phase S terminal startup only, non-fatal)."),
        "setfsgid": ("DENY", "as setfsuid."),
        "fadvise64": ("DENY", "as weston-terminal (advisory; callers ignore it)."),
        "link": ("ALLOW", "as weston-terminal (fontconfig cache lock)."),
        "syslog": ("DENY", "as weston-terminal (no dmesg banner step)."),
    },
}


# Names the pinned runsc's OCI-seccomp converter drops ("OCI seccomp: ignoring
# syscall"): an ALLOW for them is inert and would misstate the profile.
CONVERTER_DROPS = {"fchmodat2"}


def render(workload):
    raw = open(SRC, "rb").read()
    d = json.loads(raw)
    dec = WORKLOADS[workload]
    assert sorted(dec) == sorted(DECIDE), f"{workload}: undecided {set(DECIDE) - set(dec)}"
    present = {n for g in d["syscalls"] for n in g["names"]}
    for name, (verdict, _) in dec.items():
        assert verdict in ("ALLOW", "DENY")
        assert name not in present, f"{name} already in {SRC}; decide by editing this rule"
        assert not (verdict == "ALLOW" and name in CONVERTER_DROPS), \
            f"{workload}: ALLOW {name} is inert under the pinned runsc (converter drops it)"
    d["comment"] = (
        f"tier3s {workload} profile. Generated by tier3s/seccomp/make-profiles.py from {SRC} "
        f"(sha256 {hashlib.sha256(raw).hexdigest()}) plus per-workload decisions; do not edit. "
        "Under runsc every ERRNO action (default included) returns EPERM, not the "
        "defaultErrnoRet below, and unknown syscall names are dropped by the converter. "
        "Original comment: " + d["comment"])
    allow = sorted(n for n, (v, _) in dec.items() if v == "ALLOW")
    d["syscalls"].append({
        "comment": "tier3s " + workload + " decisions, ALLOW: " + " | ".join(
            f"{n}: {dec[n][1]}" for n in allow),
        "names": allow,
        "action": "SCMP_ACT_ALLOW",
    })
    d["tier3sDecisions"] = {n: {"decision": v, "reason": r} for n, (v, r) in sorted(dec.items())}
    return json.dumps(d, indent="\t") + "\n"


def main(argv):
    check = argv[1:] == ["--check"]
    bad = 0
    for w in WORKLOADS:
        out, dst = render(w), f"tier3s/seccomp/{w}.json"
        if check:
            try:
                same = open(dst).read() == out
            except FileNotFoundError:
                same = False
            if not same:
                print(f"{dst} differs from a fresh render (run tier3s/seccomp/make-profiles.py)")
                bad += 1
        else:
            open(dst, "w").write(out)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
