#!/usr/bin/env python3
"""tier3s/spike/mutate-guards.py - prove the tier3s guard tests can fail.

For each mutation: replace one exact snippet (must occur once) in the REAL
script, run the tests that cover that guard, require every named test to be
reported FAILED, then restore the original bytes and re-check their sha256.
A baseline run with no mutation must pass first. Run from the repo root:

    python3 tier3s/spike/mutate-guards.py

Exit 0 only if the baseline passes, every mutation is caught by every named
test, and every file is byte-identical to its original at the end.
"""
import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROBE = "tier3s/probe.sh"
PROV = "tier3s/provision-runsc.sh"
WRAP = "tier3s/tier3s-runsc"
TP = "tests/unit/test_tier3s_probe.py"
TV = "tests/unit/test_tier3s_provision.py"

# (id, file, old, new, [tests that must FAIL])
MUTATIONS = [
    ("P1 exec gate removed", PROBE,
     'if [ "${STAMP_OK:-0}" -ne 1 ] || [ "${PATH_OK:-0}" -ne 1 ] || [ "${BUNDLE_OK:-0}" -ne 1 ]; then',
     "if false; then",
     [f"{TP}::test_byte_only_tamper_of_sidecar_hits_the_hash_loop",
      f"{TP}::test_writable_ancestor_is_never_executed"]),
    ("P2 fd identity re-check removed", PROBE,
     'if [ "$idnow" != "$RUNSC_ID regular file 755 $EXP_UID" ]; then',
     "if false; then",
     [f"{TP}::test_swap_between_validation_and_open_is_never_executed"]),
    ("P3 fd sha512 re-check removed", PROBE,
     'elif [ "$(sha "$fdp")" != "${WANT[runsc]}" ]; then',
     "elif false; then",
     [f"{TP}::test_in_place_rewrite_between_validation_and_open_is_never_executed"]),
    ("P4 exec by path instead of the verified fd", PROBE,
     '"/proc/self/fd/$RFD" --version',
     '"$RUNSC_DIR/runsc" --version',
     [f"{TP}::test_swap_between_verify_and_exec_runs_the_verified_inode"]),
    ("P5 TEST-PASS exits 0", PROBE,
     "    exit 3\n",
     "    exit 0\n",
     [f"{TP}::test_test_root_never_exits_zero"]),
    ("P6 per-file sha512 loop removed", PROBE,
     '[ "$(sha "$RUNSC_DIR/$f")" = "${WANT[$f]}" ] || bad="${bad:+$bad }sha512:$f"',
     ":",
     [f"{TP}::test_byte_only_tamper_of_sidecar_hits_the_hash_loop",
      f"{TP}::test_byte_only_tamper_of_runsc_is_never_executed",
      f"{TP}::test_replaced_runsc_with_correct_stamp_is_never_executed"]),
    ("P7 trusted-ancestor check removed", PROBE,
     'if why="$(untrusted_ancestor "$RUNSC_DIR")"; then',
     "if true; then",
     [f"{TP}::test_symlinked_runsc_dir_is_never_executed",
      f"{TP}::test_writable_ancestor_is_never_executed"]),
    ("P8 version rc ignored", PROBE,
     'if [ "$rc" -eq 0 ] && [ -n "$want" ] && [ "$got" = "$want" ]; then',
     'if [ -n "$want" ] && [ "$got" = "$want" ]; then',
     [f"{TP}::test_version_text_with_nonzero_exit_fails"]),
    ("P9 unverified wrapper handed to podman", PROBE,
     'if [ -n "$pv" ] && [ "${WRAPPER_OK:-0}" -eq 1 ]; then',
     'if [ -n "$pv" ] && [ -x "$WRAPPER" ]; then',
     [f"{TP}::test_unverified_wrapper_is_not_handed_to_podman"]),
    ("P10 test hooks honoured without the test root", PROBE,
     "    for h in QDISTRO_PROBE_PIN QDISTRO_PROBE_PAUSE_AT QDISTRO_PROBE_PAUSE_DIR; do",
     "    for h in ; do",
     [f"{TP}::test_pin_hook_refused_without_test_root",
      f"{TP}::test_test_hooks_refused_without_test_root"]),
    ("P11 exact file-set check removed", PROBE,
     'if [ "$have" != "$exp" ]; then',
     "if false; then",
     [f"{TP}::test_group_writable_runsc_is_never_executed"]),
    ("V1 provisioning lock not taken", PROV,
     'if ! flock -n -x "$LOCKFD"; then',
     "if ! true; then",
     [f"{TV}::test_concurrent_provisions_are_serialized"]),
    ("V2 --pin guard removed", PROV,
     '    [ "$PIN_OVERRIDE" -eq 0 ] || die "--pin is a unit-test option',
     '    true || die "--pin is a unit-test option',
     [f"{TV}::test_pin_override_refused_for_root"]),
    ("V3 prefix-for-root guard removed", PROV,
     '[ "$(id -u)" -ne 0 ] || die "QDISTRO_RUNSC_PREFIX is a unit-test hook and is refused for root"',
     "true",
     [f"{TV}::test_prefix_hook_refused_for_root"]),
    ("V4 test-hook refusal removed", PROV,
     "    for h in QDISTRO_RUNSC_FAIL_AFTER_SWAP QDISTRO_RUNSC_PAUSE_AFTER_SWAP; do",
     "    for h in ; do",
     [f"{TV}::test_test_hooks_refused_for_root[QDISTRO_RUNSC_FAIL_AFTER_SWAP]",
      f"{TV}::test_test_hooks_refused_for_root[QDISTRO_RUNSC_PAUSE_AFTER_SWAP]"]),
    ("V5 version rc ignored", PROV,
     '[ "$rc" -eq 0 ] && [ "$VER_SEEN" = "${P[version_string]}" ]',
     '[ "$VER_SEEN" = "${P[version_string]}" ]',
     [f"{TV}::test_version_text_with_nonzero_exit_fails_closed"]),
    ("V6 failed rollback cleans up anyway", PROV,
     "        if ! rollback; then",
     "        if ! rollback && false; then",
     [f"{TV}::test_failed_rollback_preserves_recovery_material"]),
    ("V7 trusted_chain disabled", PROV,
     '    local d="$1" st owner mode\n    while :; do\n        if [ -e "$d" ]',
     '    return 0\n    local d="$1" st owner mode\n    while :; do\n        if [ -e "$d" ]',
     [f"{TV}::test_untrusted_lock_dir_is_refused",
      f"{TV}::test_symlinked_lock_dir_is_refused"]),
    ("V8 download published before verification", PROV,
     '[ "$got" = "${P[tarball_sha512]}" ] || die "tarball sha512 mismatch: got $got (download not cached)"',
     ":",
     [f"{TV}::test_download_is_verified_before_it_is_published"]),
    ("W1 wrapper keeps the caller environment", WRAP,
     "exec env -i PATH=/usr/bin:/bin",
     "exec env PATH=/usr/bin:/bin",
     [f"{TV}::test_wrapper_scrubs_environment_and_fixes_flags"]),
]


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def pytest(nodes):
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
                        "--tb=no", "-rfs", *nodes], cwd=REPO, capture_output=True, text=True)
    failed = set(re.findall(r"^FAILED (\S+)", r.stdout, re.M))
    skipped = r.stdout.count("SKIPPED")
    return r.returncode, failed, skipped, r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""


def main():
    files = {f: REPO / f for f in (PROBE, PROV, WRAP)}
    orig = {f: p.read_bytes() for f, p in files.items()}
    orig_sha = {f: sha(p) for f, p in files.items()}
    for f in orig_sha:
        print(f"original sha256 {orig_sha[f]}  {f}")
    rc, failed, skipped, summary = pytest([TP, TV])
    print(f"BASELINE (no mutation): rc={rc} {summary}")
    if rc != 0:
        print("baseline must pass; aborting")
        return 1
    bad = 0
    try:
        for mid, f, old, new, tests in MUTATIONS:
            src = orig[f].decode()
            n = src.count(old)
            if n != 1:
                print(f"MUTATION {mid}: snippet occurs {n} times in {f} -> HARNESS ERROR")
                bad += 1
                continue
            files[f].write_text(src.replace(old, new))
            try:
                rc, failed, skipped, summary = pytest(tests)
            finally:
                files[f].write_bytes(orig[f])
            missing = [t for t in tests if t not in failed]
            verdict = "CAUGHT" if rc != 0 and not missing else "NOT CAUGHT"
            if verdict != "CAUGHT":
                bad += 1
            print(f"MUTATION {mid} [{f}]: {verdict}; pytest rc={rc}; {summary}")
            for t in tests:
                print(f"    {'FAILED' if t in failed else 'passed/skipped'}  {t.split('::')[1]}")
            if skipped:
                print(f"    note: {skipped} skipped")
    finally:
        for f, p in files.items():
            p.write_bytes(orig[f])
    for f, p in files.items():
        same = sha(p) == orig_sha[f]
        print(f"restored {'OK' if same else 'MISMATCH'} sha256 {sha(p)}  {f}")
        bad += 0 if same else 1
    rc, failed, skipped, summary = pytest([TP, TV])
    print(f"AFTER RESTORE: rc={rc} {summary}")
    bad += 0 if rc == 0 else 1
    print(f"RESULT {'PASS' if bad == 0 else 'FAIL'}: {len(MUTATIONS)} mutations, {bad} problem(s)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
