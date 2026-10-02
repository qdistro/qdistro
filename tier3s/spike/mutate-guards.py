#!/usr/bin/env python3
"""tier3s/spike/mutate-guards.py - prove the tier3s guard tests can fail.

For each mutation: replace one exact snippet (must occur once) in the REAL
script, or apply a list of such edits for an order mutation, run the tests
that cover that guard, require every named test to be reported FAILED, then
restore the original bytes and re-check their sha256. IDs: P probe, V
provisioner, W wrapper (Phase 0); A launch path, G seccomp generator (Phase A).
A baseline run with no mutation must pass first. Run from the repo root:

    python3 tier3s/spike/mutate-guards.py [--only ID,ID...]   (ID = P1, V2, ...)

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
SPAWN = "tier3s/spawn-tier3s.sh"
CLEAN = "tier3s/qdistro-tier3s-cleanup"
HELP = "tier3s/qdistro-tier3s-scope"
MKP = "tier3s/seccomp/make-profiles.py"
TP = "tests/unit/test_tier3s_probe.py"
TV = "tests/unit/test_tier3s_provision.py"
TS = "tests/unit/test_tier3s_spawn.py"
GATE = 'broker_gate "$SPAWN_ACTION" "$WORKLOAD/$APP_BASE"\n'
DENY = f"{TS}::test_every_non_allow_reply_refuses_before_activation_and_podman[deny-decision=deny]"
ORDER = f"{TS}::test_gate_order_probe_resolve_gate_record_then_podman"

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
    ("P12 content read under an untrusted path", PROBE,
     'elif [ "${PATH_OK:-0}" -ne 1 ]; then\n        # no content reads',
     'elif false; then\n        # no content reads',
     [f"{TP}::test_fifo_swapped_in_under_untrusted_path_does_not_hang",
      f"{TP}::test_writable_ancestor_is_never_executed"]),
    ("P13 scratch image built in a temporary directory again", PROBE,
     "tar -C / --no-recursion --numeric-owner --owner=0 --group=0 --mode=0755 -cf - .",
     '{ empty="$(mktemp -d)"; chmod 0755 "$empty"; tar -C "$empty" -cf - .; rm -rf "$empty"; }',
     [f"{TP}::test_scratch_image_import_needs_no_temporary_path"]),
    ("P14 import failure ignored", PROBE,
     '    if [ -n "$import_err" ]; then',
     '    if false; then',
     [f"{TP}::test_scratch_image_import_failure_is_reported"]),
    ("P15 root runs from an untrusted checkout", PROBE,
     'if [ "$EUID" -eq 0 ] && why="$(checkout_untrusted)"; then',
     "if false; then",
     [f"{TP}::test_root_refuses_untrusted_checkout[dir]",
      f"{TP}::test_root_refuses_untrusted_checkout[pin]"]),
    ("P16 caller PATH used before the pin", PROBE,
     '[ -n "${QDISTRO_PROBE_ROOT:-}" ] || { PATH=/usr/sbin:/usr/bin:/sbin:/bin; export PATH; }',
     ":",
     [f"{TP}::test_probe_never_uses_caller_path_tools"]),
    ("V1 provisioning lock not taken", PROV,
     'if ! flock -n -x "$LOCKFD"; then',
     "if ! true; then",
     [f"{TV}::test_concurrent_provisions_are_serialized"]),
    ("V2 --pin guard removed", PROV,
     '    [ "$PIN_OVERRIDE" -eq 0 ] || die "--pin is a unit-test option',
     '    true || die "--pin is a unit-test option',
     [f"{TV}::test_pin_override_refused_for_root"]),
    ("V3 prefix-for-root guard removed", PROV,
     '[ "$EUID" -ne 0 ] || die "QDISTRO_RUNSC_PREFIX is a unit-test hook and is refused for root"',
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
     '    local d="$1" stop="${2:-$TRUST_STOP}" st owner mode\n    while :; do\n        if [ -e "$d" ]',
     '    return 0\n    local d="$1" stop="${2:-$TRUST_STOP}" st owner mode\n    while :; do\n        if [ -e "$d" ]',
     [f"{TV}::test_untrusted_lock_dir_is_refused",
      f"{TV}::test_symlinked_lock_dir_is_refused"]),
    ("V8 download published before verification", PROV,
     '[ "$got" = "${P[tarball_sha512]}" ] || die "tarball sha512 mismatch: got $got (download not cached)"',
     ":",
     [f"{TV}::test_download_is_verified_before_it_is_published"]),
    ("V9 download into an unchecked cache dir", PROV,
     '    trusted_chain "$SRC_DIR" "$CACHE_STOP"\n    log "downloading',
     '    log "downloading',
     [f"{TV}::test_download_refused_into_writable_cache_dir",
      f"{TV}::test_download_refused_through_symlinked_cache_dir"]),
    ("V10 root runs from an untrusted checkout", PROV,
     '    if why="$(checkout_untrusted)"; then\n        die "refusing to run as root',
     '    if false; then\n        die "refusing to run as root',
     [f"{TV}::test_root_refuses_untrusted_checkout[dir]",
      f"{TV}::test_root_refuses_untrusted_checkout[pin]"]),
    ("V11 caller PATH used before the pin", PROV,
     'if [ "$EUID" -eq 0 ] || [ -z "${QDISTRO_RUNSC_PREFIX:-}" ]; then PATH=/usr/sbin:/usr/bin:/sbin:/bin; export PATH; fi',
     ":",
     [f"{TV}::test_provision_never_uses_caller_path_tools"]),
    ("W2 wrapper finds env through the caller PATH", WRAP,
     "exec /usr/bin/env -i",
     "exec env -i",
     [f"{TV}::test_wrapper_never_uses_caller_path_env"]),
    ("W1 wrapper keeps the caller environment", WRAP,
     "exec /usr/bin/env -i PATH=/usr/bin:/bin",
     "exec /usr/bin/env PATH=/usr/bin:/bin",
     [f"{TV}::test_wrapper_scrubs_environment_and_fixes_flags"]),
    ("W3 wrapper creates a missing state root", WRAP,
     'if [ -L "$root" ] || [ ! -d "$root" ]; then die "state root $root is missing or not a directory"; fi',
     'mkdir -p "$root"',
     [f"{TV}::test_wrapper_never_creates_the_state_root",
      f"{TV}::test_wrapper_refuses_bad_state_root_without_creating_it[root-missing]"]),
    ("W4 wrapper accepts a caller --root", WRAP,
     '    case "$a" in --root|--root=*|-root|-root=*) die "caller-supplied $a" ;; esac',
     "    :",
     [f"{TV}::test_wrapper_refuses_caller_root[--root=/run/user/1000/runsc]"]),
    ("W5 wrapper state-root owner/mode check removed", WRAP,
     '[ "$meta" = "$euid 700" ] || die',
     "true || die",
     [f"{TV}::test_wrapper_refuses_bad_state_root_without_creating_it[root-mode]"]),
    ("P17 probe state_root check removed", PROBE,
     '    elif [ -L "$SR" ] || [ ! -d "$SR" ] || [ "$(stat -c \'%u %a\' -- "$SR")" != "$sr_uid 700" ]; then',
     "    elif false; then",
     [f"{TP}::test_state_root_missing_or_loose_fails[missing]",
      f"{TP}::test_state_root_missing_or_loose_fails[mode]"]),
    # --- Phase A launch path: gate order (broker before activation and podman run)
    ("A1 broker gate removed", SPAWN, GATE, ":\n", [DENY, ORDER]),
    ("A2 activation recorded before the broker gate", SPAWN, GATE,
     '[ -z "$GENERATION" ] || read_binding --record\n' + GATE,
     [DENY, ORDER]),
    ("A3 podman run before the broker gate", SPAWN,
     [(GATE, ":\n"), ('wait "$child"\nexit $?', GATE + 'wait "$child"\nexit $?')], None,
     [DENY, ORDER]),
    # --- control-dir location
    ("A4 control record inside the exported per-launch dir", SPAWN,
     'CTL_DIR="$CTL/$TOKEN"', 'CTL_DIR="$LAUNCH_PARENT/$TOKEN/ctl"',
     [f"{TS}::test_plan_control_record_is_outside_the_per_launch_dir",
      f"{TS}::test_launch_records_then_cleans_up_on_normal_exit"]),
    ("A5 cleanup reads records from the exported parent", CLEAN,
     'CTL="$T/run/qdistro-tier3s-ctl"', 'CTL="$T/run/qdistro-tier3s"',
     [f"{TS}::test_cleanup_tears_down_a_running_launch"]),
    # --- other launch guards
    ("A6 dev-profile gate removed", SPAWN,
     '[ "$profile" = dev ] \\\n', 'true \\\n',
     [f"{TS}::test_hardened_profile_refused[daily-driver]"]),
    ("A7 probe failure ignored", SPAWN,
     '[ "$probe_rc" -eq 0 ] || refuse', "true || refuse",
     [f"{TS}::test_probe_failure_refuses_with_its_result"]),
    ("A8 literal tmpfs uid= instead of U", SPAWN,
     "--tmpfs /run/user/1000:rw,U,mode=0700", "--tmpfs /run/user/1000:rw,uid=1000,gid=1000,mode=0700",
     [f"{TS}::test_plan_podman_command_shape"]),
    ("A9 scope placement check removed", SPAWN,
     'in_scope "$p" "$rel" || {', "true || {",
     [f"{TS}::test_sentry_outside_the_scope_tears_down"]),
    ("A10 failed podman query read as no container", CLEAN,
     '        1)  ;;\n        *)  say "$tok: podman query failed',
     '        *)  ;;\n        999)  say "$tok: podman query failed',
     [f"{TS}::test_cleanup_failed_podman_query_is_not_no_container"]),
    ("A11 cleanup ignores the state root", CLEAN,
     '    if [ -L "$root" ] || [ ! -d "$root" ] || [ "$(stat -c \'%u %a\' -- "$root")" != "$admin 700" ]; then',
     "    if false; then",
     [f"{TS}::test_cleanup_with_missing_state_root_preserves_record_and_scope",
      f"{TS}::test_cleanup_with_replaced_state_root_preserves_record[mode]",
      f"{TS}::test_cleanup_with_replaced_state_root_preserves_record[symlink]"]),
    ("A12 cleanup accepts any recorded runsc root", CLEAN,
     '    [ "${S[runsc_root]:-}" = "/run/qdistro-tier3s-runsc/${S[admin_uid]}" ] \\',
     "    true \\",
     [f"{TS}::test_cleanup_with_replaced_state_root_preserves_record[owner-record]"]),
    ("A13 record dropped after a failed stop", CLEAN,
     '|| { say "$tok: podman stop $ctr failed (record preserved)"; return 5; }', "|| true",
     [f"{TS}::test_cleanup_failed_stop_preserves_record_and_scope"]),
    ("A14 escaped-process check removed", CLEAN,
     '        if [ "$(starttime "${S[${p}_pid]}")" = "${S[${p}_starttime]:-x}" ]; then',
     "        if false; then",
     [f"{TS}::test_cleanup_reports_a_recorded_process_alive_outside_the_scope"]),
    ("A15 --unit tears down every record", CLEAN,
     '            [ "$(sed -n \'s/^unit=//p\' "$CTL/$tok/state" 2>/dev/null)" = "$u" ] || continue',
     "            :",
     [f"{TS}::test_two_launches_tearing_down_one_preserves_the_other"]),
    ("A16 failed label listing read as nothing to reap", CLEAN,
     '|| { say "podman listing of labelled containers FAILED; nothing reaped by label"; exit 1; }',
     "|| true",
     [f"{TS}::test_reap_stale_podman_listing_failure_is_an_error"]),
    ("A17 scope helper delegates the limit files too", HELP,
     'chown "$ADMIN_UID" -- "$DIR" "$DIR/cgroup.procs" "$DIR/cgroup.subtree_control" "$DIR/cgroup.threads"',
     'chown "$ADMIN_UID" -- "$DIR" "$DIR"/*',
     [f"{TS}::test_helper_delegates_exactly_four_paths_and_runs_podman_as_admin"]),
    ("A18 scope helper accepts a foreign scope", HELP,
     '[ "${REL##*/}" = "$UNIT" ] || die', "true || die",
     [f"{TS}::test_helper_refuses_bad_invocations[foreign-scope]"]),
    ("A19 scope helper accepts an occupied scope", HELP,
     '[ "${procs[*]}" = "$SELF_PID" ] || die', "true || die",
     [f"{TS}::test_helper_refuses_a_scope_that_already_holds_processes"]),
    ("A20 cleanup does not verify the scope target", CLEAN,
     '    if [ -n "$rel" ] && [ -n "${S[scope_cgroup]:-}" ] && [ "$rel" != "${S[scope_cgroup]}" ]; then',
     "    if false; then",
     [f"{TS}::test_cleanup_refuses_a_scope_at_another_cgroup"]),
    # --- seccomp generator
    ("G1 converter-drop guard removed", MKP,
     '        assert not (verdict == "ALLOW" and name in CONVERTER_DROPS), \\',
     '        assert True or not (verdict == "ALLOW" and name in CONVERTER_DROPS), \\',
     [f"{TS}::test_seccomp_generator_refuses_an_inert_allow"]),
    ("G2 decision flipped without a re-render", MKP,
     '"llistxattr": ("ALLOW",', '"llistxattr": ("DENY",',
     [f"{TS}::test_seccomp_profile_is_rendered_and_decided"]),
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
    only = None
    if len(sys.argv) == 3 and sys.argv[1] == "--only":
        only = set(sys.argv[2].split(","))
    elif len(sys.argv) != 1:
        print(__doc__)
        return 2
    muts = [m for m in MUTATIONS if only is None or m[0].split()[0] in only]
    if only is not None and len(muts) != len(only):
        print(f"unknown mutation id in {sorted(only)}")
        return 2
    files = {f: REPO / f for f in (PROBE, PROV, WRAP, SPAWN, CLEAN, HELP, MKP)}
    orig = {f: p.read_bytes() for f, p in files.items()}
    orig_sha = {f: sha(p) for f, p in files.items()}
    for f in orig_sha:
        print(f"original sha256 {orig_sha[f]}  {f}")
    rc, failed, skipped, summary = pytest([TP, TV, TS])
    print(f"BASELINE (no mutation): rc={rc} {summary}")
    if rc != 0:
        print("baseline must pass; aborting")
        return 1
    bad = 0
    try:
        for mid, f, old, new, tests in muts:
            src = orig[f].decode()
            edits = old if isinstance(old, list) else [(old, new)]
            harness_error = False
            for o, nw in edits:
                n = src.count(o)
                if n != 1:
                    print(f"MUTATION {mid}: snippet occurs {n} times in {f} -> HARNESS ERROR")
                    harness_error = True
                    break
                src = src.replace(o, nw)
            if harness_error:
                bad += 1
                continue
            files[f].write_text(src)
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
    rc, failed, skipped, summary = pytest([TP, TV, TS])
    print(f"AFTER RESTORE: rc={rc} {summary}")
    bad += 0 if rc == 0 else 1
    print(f"RESULT {'PASS' if bad == 0 else 'FAIL'}: {len(muts)} mutations, {bad} problem(s)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
