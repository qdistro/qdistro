#!/usr/bin/env python3
"""tier3s/spike/mutate-guards.py - prove the tier3s guard tests can fail.

For each mutation: replace one exact snippet (must occur once) in the REAL
script, or apply a list of such edits for an order mutation, run the tests
that cover that guard, require every named test to be reported FAILED, then
restore the original bytes and re-check their sha256. IDs: P probe, V
provisioner, W wrapper (Phase 0); A launch path, G seccomp generator (Phase A,
milestone A-i); S session manager, L root launch helper, B broker, I installer,
U launch unit, A21+ spawn deltas (milestone A-ii); I3-I5, U2, U3 the owner
answers O10/O11, R1-R5 the reaper/teardown fixes from the s122 VM and qci runs, R6/R7 the sol r1 P1, R8 the sol r2 P1, R9 the sol r3 P1 (milestone A-iii);
R10-R36, S13-S18, A23-A26, L6-L8, U4 the astra+fable A r1 fixes (the sol r4
P1 included). A3, A15, A20, R1, R2, R5-R7 and R9 were re-targeted at the r1
code: the same guard, new text. R36-R48, A27-A29 and U5 are the astra+fable
A r2 fixes; R1, R2, R6-R8, R11, R18, R20, R23, R24 and R26 were re-targeted at
the r2 code (calls in this shell, results in variables).
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
SM = "session_manager/qdistro_session_manager.py"
LH = "session_manager/qdistro-tier3s-silo-launch"
UNITF = "session_manager/qdistro-tier3s-silo@.service"
BRK = "broker/qdistro_admin_broker.py"
INST = "scripts/install/install-session-manager.sh"
TSM = "tests/unit/test_session_manager_tier3s.py"
TB = "tests/unit/test_broker_check_permission.py"
TBR = f"{TB}::TestCheckPermissionResolution"
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
     [(GATE, ":\n"), ('wait "$child"; rc=$?', GATE + 'wait "$child"; rc=$?')], None,
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
     [('            if [ "$ru" != "$u" ] && ! incomplete "$tok"; then continue; fi', "            :"),
      ('            elif read_record "$tok" && [ "${S[unit]}" = "$u" ]; then',
       '            elif read_record "$tok"; then')], None,
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
     '        if [ -n "$want" ] && [ "$rel" != "$want" ]; then',
     "        if false; then",
     [f"{TS}::test_cleanup_refuses_a_scope_at_another_cgroup"]),
    # --- seccomp generator
    ("G1 converter-drop guard removed", MKP,
     '        assert not (verdict == "ALLOW" and name in CONVERTER_DROPS), \\',
     '        assert True or not (verdict == "ALLOW" and name in CONVERTER_DROPS), \\',
     [f"{TS}::test_seccomp_generator_refuses_an_inert_allow"]),
    ("G2 decision flipped without a re-render", MKP,
     '"llistxattr": ("ALLOW",', '"llistxattr": ("DENY",',
     [f"{TS}::test_seccomp_profile_is_rendered_and_decided"]),
    # --- A-ii: spawn deltas
    ("A21 pod-app launch no longer refused", SPAWN,
     '[ -n "$SILO" ] || refuse "tier 3s pod apps', 'true || refuse "tier 3s pod apps',
     [f"{TS}::test_podapp_launch_is_refused_in_phase_a"]),
    ("A22 silo name resolved instead of the row's binding", SPAWN,
     'out="$(as_admin "${RESOLVER[@]}" "$BINDING" "$@" --launch-env)"',
     'out="$(as_admin "${RESOLVER[@]}" "$SILO" "$@" --launch-env)"',
     [f"{TS}::test_template_binding_is_resolved_instead_of_the_silo_name"]),
    # --- A-ii: session manager (no fallback, dev only, freeze/resume, fail-closed stop)
    ("S1 tier3s start branch removed (falls back to the tier-3 launcher)", SM,
     "                        if silo.kind == KIND_TIER3S:\n                            # Tier 3s: its own unit only.",
     "                        if False:\n                            # Tier 3s: its own unit only.",
     [f"{TSM}::test_start_exports_the_stanza_and_starts_only_the_tier3s_unit",
      f"{TSM}::test_failed_start_rolls_back_and_falls_back_to_nothing"]),
    ("S2 start no longer refuses a non-dev profile", SM,
     "# before any state change (paravirt O4)\n                        self._require_tier3s_profile()",
     "# before any state change (paravirt O4)\n                        pass",
     [f"{TSM}::test_start_on_a_non_dev_profile_is_refused_before_any_state_change"]),
    ("S3 create no longer refuses a non-dev profile", SM,
     "            if kind == KIND_TIER3S:\n                self._require_tier3s_profile()",
     "            if False:\n                self._require_tier3s_profile()",
     [f"{TSM}::test_create_refused_on_a_non_dev_profile[daily-driver]",
      f"{TSM}::test_create_refused_on_a_non_dev_profile[release]"]),
    ("S4 profile gate accepts any profile", SM,
     '        if profile != "dev":\n            raise BadArgument(TIER3S_PROFILE_REFUSAL',
     '        if False:\n            raise BadArgument(TIER3S_PROFILE_REFUSAL',
     [f"{TSM}::test_create_refused_on_a_non_dev_profile[release]",
      f"{TSM}::test_start_on_a_non_dev_profile_is_refused_before_any_state_change"]),
    ("S5 freeze refusal removed", SM,
     "                if silo.kind == KIND_TIER3S:\n                    # tier3s/CONTRACT.md §6: freezing",
     "                if False:\n                    # tier3s/CONTRACT.md §6: freezing",
     [f"{TSM}::test_freeze_and_resume_are_unsupported[freeze]"]),
    ("S6 resume refusal removed", SM,
     "                if silo.kind == KIND_TIER3S:\n                    raise BadArgument(\n"
     "                        \"freeze/resume is unsupported for tier3s silos\")\n"
     "                if silo.state == State.ACTIVE:",
     "                if False:\n                    raise BadArgument(\n"
     "                        \"freeze/resume is unsupported for tier3s silos\")\n"
     "                if silo.state == State.ACTIVE:",
     [f"{TSM}::test_freeze_and_resume_are_unsupported[resume]"]),
    ("S7 stop reports STOPPED over a surviving launch", SM,
     "        if survived or not stop_done:\n            with self._lock:",
     "        if not stop_done:\n            with self._lock:",
     [f"{TSM}::test_stop_fails_closed_when_the_launch_survives"]),
    ("S8 failed podman query read as stopped", SM,
     "        if proc.returncode != 1:\n            return True\n        try:\n            left",
     "        if proc.returncode == 0:\n            return True\n        try:\n            left",
     [f"{TSM}::test_running_true_when_the_container_exists_or_the_query_fails[125]"]),
    ("S9 surviving control record ignored", SM,
     "        if left:\n            log.warning", "        if False:\n            log.warning",
     [f"{TSM}::test_running_true_while_a_control_record_of_the_unit_survives"]),
    ("S10 startup reconciliation removed", SM,
     "            self.reconcile_tier3s_launches()\n        except",
     "            pass\n        except",
     [f"{TSM}::test_restart_reconciles_before_relaunching_with_a_fresh_token"]),
    ("S11 tier3s uid not checked", SM,
     '        if int(uid) != ADMIN_UID:\n            raise BadArgument(\n                f"tier3s silo uid',
     '        if False:\n            raise BadArgument(\n                f"tier3s silo uid',
     [f"{TSM}::test_uid_must_be_the_admin_launch_owner"]),
    ("S12 tier3s network other than none accepted", SM,
     '    if network != "none":\n        raise BadArgument(\n            f"tier3s launch.network',
     '    if False:\n        raise BadArgument(\n            f"tier3s launch.network',
     [f"{TSM}::test_launch_network_is_none_only[pasta]"]),
    # --- A-ii: root launch helper
    ("L1 trusted-stanza check removed", LH,
     'require_trusted_env "$ENV_FILE"\n', ":\n",
     [f"{TSM}::test_helper_refuses_a_writable_stanza[g+w]",
      f"{TSM}::test_helper_refuses_a_symlinked_stanza"]),
    ("L2 ambient environment reaches the spawn", LH,
     "exec env -i \\\n", "exec env \\\n",
     [f"{TSM}::test_helper_execs_the_spawn_with_exactly_the_stanza"]),
    ("L3 stanza of another silo accepted", LH,
     '[ "$SILO" = "$NAME" ] || {', 'true || {',
     [f"{TSM}::test_helper_refuses_a_bad_stanza[silo-mismatch]"]),
    ("L4 unknown stanza key accepted", LH,
     "        if k not in want:\n", "        if False:\n",
     [f"{TSM}::test_helper_refuses_a_bad_stanza[unknown-key]"]),
    ("L5 test overrides honoured for root", LH,
     'if [ "$EUID" -ne 0 ]; then\n    ENV_DIR=', 'if true; then\n    ENV_DIR=',
     [f"{TSM}::test_helper_test_overrides_are_ignored_for_root"]),
    # --- A-ii: broker, installer, unit
    ("B1 tier3s spawn prefix dropped from the rules-only set", BRK,
     '                                "qdistro.tier3s.spawn:",\n', "",
     [f"{TBR}::test_tier3s_spawn_is_rules_only",
      f"{TBR}::test_tier_spawn_ignores_cache_without_rule[qdistro.tier3s.spawn:headless-smoke/qdistro-tier3s-smoke]"]),
    ("I1 installer drops the cleanup helper", INST,
     '    install -o root -g root -m 0755 "$_qd_t3s_src/qdistro-tier3s-cleanup" "$DEST/qdistro-tier3s-cleanup"\n', "",
     [f"{TSM}::test_installer_installs_exactly_the_contract_paths"]),
    ("I2 installer skips the tmpfiles creation", INST,
     "        systemd-tmpfiles --create /usr/lib/tmpfiles.d/qdistro-tier3s.conf\n", "        true\n",
     [f"{TSM}::test_installer_installs_exactly_the_contract_paths"]),
    ("U1 unit loses its ExecStopPost cleanup", UNITF,
     "ExecStopPost=/usr/libexec/qdistro/qdistro-tier3s-cleanup --unit %n\n", "",
     [f"{TSM}::test_unit_file_shape"]),
    # --- A-iii: owner answers O10 (installer opt-in) and O11 (manager stop)
    ("I3 installer gate removed (tier3s installed without the flag)", INST,
     'if [ "$_qd_t3s" = 1 ]; then\n', "if true; then\n",
     [f"{TSM}::test_installer_installs_nothing_tier3s_without_the_flag[None]",
      f"{TSM}::test_installer_installs_nothing_tier3s_without_the_flag[0]"]),
    ("I4 unset flag treated as opt-in", INST,
     "    0|'') _qd_t3s=0 ;;\n", "    0|'') _qd_t3s=1 ;;\n",
     [f"{TSM}::test_installer_installs_nothing_tier3s_without_the_flag[None]"]),
    ("I5 unrecognised flag value silently skips", INST,
     "    *)    echo \"ERROR: QDISTRO_TIER3S must be 0 or 1 (got '${QDISTRO_TIER3S}')\" >&2\n          exit 2 ;;\n",
     "    *)    _qd_t3s=0 ;;\n",
     [f"{TSM}::test_installer_refuses_an_unrecognised_flag_value[yes]"]),
    ("U2 manager stop no longer stops the launch unit", UNITF,
     "StopPropagatedFrom=qdistro-session-manager.service\n", "",
     [f"{TSM}::test_unit_file_shape"]),
    # --- A-iii: VM findings in the reaper (s122)
    # R1 was `index .Labels` in a ps template (podman 6 rejects it). Since
    # fable A r1 P2-2 the listing is podman JSON; R1 now guards that format.
    ("R1 reaper lists labels with a template instead of podman JSON", CLEAN,
     'podman ps -a --filter "label=qdistro_tier3s_token${1:+=$1}" --format json \\\n',
     """podman ps -a --filter "label=qdistro_tier3s_token${1:+=$1}" --format '{{.Names}}' \\\n""",
     [f"{TS}::test_reap_stale_reaps_an_unrecorded_labelled_container[False]"]),
    ("R2 reaper never stops a stale scope (orphan per-launch dir left)", CLEAN,
     '; stopping it"\n            bounded "$SCOPE_STOP_TMO" systemctl --no-ask-password stop "$scope" 2>/dev/null\n', '; stopping it"\n',
     [f"{TS}::test_reap_stale_stops_a_stale_scope_then_removes_the_orphan_dir"]),
    ("R3 reaper ignores the scope's live launch unit", CLEAN,
     '        if [ "$bst" = live ]; then\n', '        if false; then\n',
     [f"{TS}::test_reap_stale_leaves_an_orphan_dir_whose_scope_serves_a_live_unit"]),
    ("R4 a container removed during inspect fails the teardown", CLEAN,
     '                vanished "$admin" "$ctr" || { say "$tok: podman inspect of $ctr failed (record preserved)"; return 4; }\n',
     '                say "$tok: podman inspect of $ctr failed (record preserved)"; return 4\n',
     [f"{TS}::test_cleanup_container_removed_concurrently_is_torn_down[inspect_vanish]"]),
    ("R5 a container removed during stop fails the teardown", CLEAN,
     '                    vanished "$admin" "$id" || { say "$tok: podman stop $ctr failed (record preserved)"; return 5; }\n',
     '                    say "$tok: podman stop $ctr failed (record preserved)"; return 5\n',
     [f"{TS}::test_cleanup_container_removed_concurrently_is_torn_down[stop_vanish]"]),
    ("R6 reaper trusts an unknown/empty BindsTo (sol r1 P1)", CLEAN,
     '        if [ -z "$bound" ] || ! [[ "$bound" =~ $UNIT_RE ]]; then\n',
     '        if false; then\n',
     [f"{TS}::test_reap_stale_preserves_a_live_scope_whose_owner_is_unknown[no-bindsto]",
      f"{TS}::test_reap_stale_preserves_a_live_scope_whose_owner_is_unknown[foreign-bindsto]"]),
    ("R7 reaper does not recheck the launch unit before the stop (sol r1 P1)", CLEAN,
     '            if [ "$own" = 0 ] && { unit_status "$bound"; [ "$US" != dead ]; }; then\n                say "$tok: $bound is live again or unknown',
     '            if false; then\n                say "$tok: $bound is live again or unknown',
     [f"{TS}::test_reap_stale_rechecks_the_launch_unit_before_stopping_the_scope"]),
    ("R8 a failed unit-state query reads as 'dead' (sol r2 P1)", CLEAN,
     '    US=unknown\n    prop "$1" ActiveState || return 0\n', '    US=dead\n    prop "$1" ActiveState || return 0\n',
     [f"{TS}::test_reap_stale_unknown_unit_state_preserves_a_recorded_launch",
      f"{TS}::test_reap_stale_unknown_unit_state_preserves_an_unrecorded_container",
      f"{TS}::test_reap_stale_unknown_state_preserves_an_orphan_scope[scope]",
      f"{TS}::test_reap_stale_unknown_state_preserves_an_orphan_scope[bound-unit]"]),
    ("R9 a container without a valid unit label is reaped (sol r3 P1)", CLEAN,
     'else "badunit" if not ok(UNIT, unit)\n', 'else "badunit" if False\n',
     [f"{TS}::test_reap_stale_preserves_a_scopeless_container_without_a_valid_unit_label[]",
      f"{TS}::test_reap_stale_preserves_a_scopeless_container_without_a_valid_unit_label[sshd.service\\n]"]),
    # --- astra + fable A r1 (the sol r4 P1 included)
    ("R10 a live scope's owner is not matched to the record/label unit (sol r4 P1)", CLEAN,
     '        if [ "$bound" != "$unit" ]; then\n', '        if false; then\n',
     [f"{TS}::test_reap_stale_refuses_a_stale_unit_name_on_a_live_scope_owned_by_another_unit[record]",
      f"{TS}::test_reap_stale_refuses_a_stale_unit_name_on_a_live_scope_owned_by_another_unit[label]"]),
    ("R11 the reaper does not recheck that the owner is dead before any stop", CLEAN,
     '        if [ "$dead" = 1 ] && { unit_status "$unit"; [ "$US" != dead ]; }; then\n', '        if false; then\n',
     [f"{TS}::test_reap_stale_rechecks_the_owner_before_any_stop"]),
    ("R12 the spawn's own NEW token is a reap candidate", CLEAN,
     '        for tok in $(tokens); do\n            [ "$tok" != "$EXCEPT_TOK" ] || continue\n',
     '        for tok in $(tokens); do\n            :\n',
     [f"{TS}::test_reap_stale_except_unit_reaps_only_an_older_token_of_the_spawn_unit"]),
    ("R13 --except-unit accepted without a valid --token", CLEAN,
     '            [[ "$EXCEPT_UNIT" =~ $UNIT_RE ]] && [[ "$EXCEPT_TOK" =~ $TOK_RE ]] || usage\n', '            :\n',
     [f"{TS}::test_reap_stale_option_validation[args0]", f"{TS}::test_reap_stale_option_validation[args1]",
      f"{TS}::test_reap_stale_option_validation[args2]"]),
    ("R14 the record dir is published before the lock (astra 1)", SPAWN,
     [('exec 9>"$CTL/.lock"\nflock -w 60 9 || refuse "cannot take $CTL/.lock"\n',
       'mkdir -m 0700 "$CTL_DIR"\nexec 9>"$CTL/.lock"\nflock -w 60 9 || refuse "cannot take $CTL/.lock"\n'),
      ('[ ! -e "$CTL_DIR" ] && [ ! -L "$CTL_DIR" ] && [ ! -e "$LAUNCH_DIR" ]', '[ ! -L "$CTL_DIR" ] && [ ! -e "$LAUNCH_DIR" ]'),
      ('    | LC_ALL=C sort > "$NEW/state" && mv -T -- "$NEW" "$CTL_DIR" || refuse',
       '    | LC_ALL=C sort > "$CTL_DIR/state" || refuse')], None,
     [f"{TS}::test_spawn_killed_before_publication_leaves_no_record"]),
    ("R15 --unit ignores incomplete records (astra 1)", CLEAN,
     '            if [ "$ru" != "$u" ] && ! incomplete "$tok"; then continue; fi',
     '            if [ "$ru" != "$u" ]; then continue; fi',
     [f"{TS}::test_an_incomplete_record_is_recovered_when_nothing_ran_under_it[how1]"]),
    ("R16 an incomplete record is removed while its scope is not dead", CLEAN,
     '    [ "$st" = dead ] || { say "$tok: incomplete control record (no state) while $scope is $st; preserved"; return 1; }\n', '',
     [f"{TS}::test_an_incomplete_record_is_preserved_without_positive_evidence[scope-live]",
      f"{TS}::test_an_incomplete_record_is_preserved_without_positive_evidence[scope-unknown]"]),
    ("R17 an incomplete record is removed while a container carries its token", CLEAN,
     '    [ "$n" -eq 0 ] || { say "$tok: incomplete control record but $n container(s) carry its token; preserved"; return 1; }\n', '',
     [f"{TS}::test_an_incomplete_record_is_preserved_without_positive_evidence[container]"]),
    ("R18 an incomplete record is removed on a failed podman listing", CLEAN,
     '    labelled "$tok" || { say "$tok: incomplete control record; podman listing failed; preserved"; return 1; }\n',
     '    labelled "$tok"\n',
     [f"{TS}::test_an_incomplete_record_is_preserved_without_positive_evidence[listing-fails]"]),
    ("R19 a dead spawn's unpublished record is never swept", CLEAN,
     '        sweep_unpublished || fails=$((fails + 1))\n', '',
     [f"{TS}::test_reap_stale_removes_a_dead_spawns_unpublished_record"]),
    ("R20 a failed ControlGroup query reads as 'no scope' (astra 2)", CLEAN,
     '        prop "$scope" ControlGroup || { say "$tok: ControlGroup query for $scope failed"; return 1; }\n',
     '        prop "$scope" ControlGroup || { SC_ST=dead; SC_REL=""; return 0; }\n',
     [f"{TS}::test_cleanup_a_failed_controlgroup_query_preserves[show_cg_fail]",
      f"{TS}::test_cleanup_a_failed_controlgroup_query_preserves[show_cg_fail_after_stop]"]),
    ("R21 a failed recursive find reads as empty (astra 2)", CLEAN,
     '    wait "$!" || return 1\n    [ "${#files[@]}" -gt 0 ] || return 1',
     '    wait "$!" || :\n    [ "${#files[@]}" -gt 0 ] || return 1',
     [f"{TS}::test_cleanup_a_failed_recursive_scan_is_not_empty[unreadable-dir]"]),
    ("R22 a failed cgroup.procs read reads as empty (astra 2)", CLEAN,
     '        { while read -r p; do [ -n "$p" ] && echo "$p"; done < "$f"; } 2>/dev/null || return 1\n',
     '        { while read -r p; do [ -n "$p" ] && echo "$p"; done < "$f"; } 2>/dev/null || :\n',
     [f"{TS}::test_cleanup_a_failed_recursive_scan_is_not_empty[unreadable-procs]"]),
    ("R23 a dead scope's populated cgroup is not checked", CLEAN,
     '        wait_empty "$CGROOT$c" 5 \\\n', '        true \\\n',
     [f"{TS}::test_cleanup_a_dead_scope_with_a_populated_cgroup_preserves"]),
    ("R24 external calls are unbounded (astra 5)", CLEAN,
     '    timeout -k "$KILL_AFTER" "$CAP" "$@" < "$CALL_IN"', '    "$@" < "$CALL_IN"',
     [f"{TS}::test_a_wedged_podman_call_is_bounded_and_blocks_no_other_launch"]),
    ("R25 one lock for every token (astra 5 / fable P3-1)", CLEAN,
     '    { exec 8<"$d"; } 2>/dev/null || return 1\n', '    { exec 8>"$LOCK"; } 2>/dev/null || return 1\n',
     [f"{TS}::test_a_wedged_podman_call_is_bounded_and_blocks_no_other_launch"]),
    ("R26 children inherit the lock fds", CLEAN,
     '2> "$WORKDIR/err" 8<&- 9>&- &\n', '2> "$WORKDIR/err" &\n',
     [f"{TS}::test_cleanup_tears_down_a_running_launch"]),
    ("R27 the reaper waits on a busy token", CLEAN,
     '            lock_token "$tok" 0\n', '            lock_token "$tok" "$LOCK_WAIT"\n',
     [f"{TS}::test_reap_stale_skips_a_token_another_teardown_holds"]),
    ("R28 the batch deadline is ignored", CLEAN,
     '            if past_deadline; then say "$tok: reap deadline reached; not processed (record preserved)"',
     '            if false; then say "$tok: reap deadline reached; not processed (record preserved)"',
     [f"{TS}::test_reap_stale_deadline_preserves_what_it_did_not_reach"]),
    ("R29 stop by container name instead of the inspected ID", CLEAN,
     'podman stop -t "$GRACE" "$id" >/dev/null', 'podman stop -t "$GRACE" "$ctr" >/dev/null',
     [f"{TS}::test_cleanup_tears_down_a_running_launch"]),
    ("R30 READY=1 before the broker gate (fable P2-1)", SPAWN,
     GATE, '[ -z "$NOTIFY_SOCK" ] || NOTIFY_SOCKET="$NOTIFY_SOCK" systemd-notify --ready\n' + GATE,
     [f"{TS}::test_a_refused_or_failed_launch_never_sends_ready[deny]"]),
    ("R31 a failed short workload sends READY=1", SPAWN,
     'if [ "$recorded" != 1 ] && [ "$rc" -eq 0 ]; then', 'if [ "$recorded" != 1 ]; then',
     [f"{TS}::test_a_refused_or_failed_launch_never_sends_ready[podman-fails]"]),
    ("R32 systemd's notify socket leaks to the probe and the scope", SPAWN,
     'NOTIFY_SOCK="${NOTIFY_SOCKET:-}"; unset NOTIFY_SOCKET', 'NOTIFY_SOCK="${NOTIFY_SOCKET:-}"',
     [f"{TS}::test_spawn_sends_ready_once_recorded_running_and_hides_the_socket"]),
    ("R33 a failed READY=1 keeps the launch", SPAWN,
     'notify_ready || { say "cannot send READY=1 to systemd; tearing down"; exit 2; }', 'notify_ready || :',
     [f"{TS}::test_a_failed_ready_tears_the_launch_down"]),
    ("R34 the label decoder passes any unit string (fable P2-2)", CLEAN,
     '    out += [v, tok if ok(TOK, tok) else "-", unit if ok(UNIT, unit) else "-",',
     '    v = "ok" if v == "badunit" else v\n    out += [v, tok if ok(TOK, tok) else "-", unit if isinstance(unit, str) else "-",',
     [f"{TS}::test_reap_stale_label_bytes_cannot_inject_or_shift_fields"]),
    ("R35 garbage listing JSON reads as no containers", CLEAN,
     '    sys.exit(f"podman ps JSON: {e}")', '    data = []',
     [f"{TS}::test_reap_stale_bad_listing_json_is_an_error"]),
    ("A23 the spawn's own-cgroup unit check removed (fable P3-6)", SPAWN,
     '[ "${own##*/}" = "$UNIT" ] || refuse "not running in $UNIT (own cgroup: ${own:-?})"', ':',
     [f"{TS}::test_launch_unit_must_be_our_own_cgroup"]),
    ("A24 the cleanup ignores a container's foreign token label (fable P3-6)", CLEAN,
     """[ "$want" = "$tok" ] || { say "$tok: container $ctr carries token '$want', not ours; refusing"; return 4; }""", ':',
     [f"{TS}::test_cleanup_refuses_a_container_with_another_token"]),
    ("A25 --except-unit does not reap the spawn unit's older token (fable P3-6)", CLEAN,
     '            if [ -n "$EXCEPT_UNIT" ] && [ "$u" = "$EXCEPT_UNIT" ]; then\n                dead=0',
     '            if false; then\n                dead=0',
     [f"{TS}::test_reap_stale_except_unit_reaps_only_an_older_token_of_the_spawn_unit"]),
    ("A26 the scope helper accepts a scope with child cgroups (fable P3-6)", HELP,
     '[ -z "$(find "$DIR" -mindepth 1 -type d -print -quit)" ] || die "scope $UNIT already has child cgroups"', ':',
     [f"{TS}::test_helper_refuses_a_scope_with_child_cgroups"]),
    ("L6 the launch helper accepts a malformed token (fable P3-6)", LH,
     'if not re.fullmatch(r"[0-9a-f]{32}", kv["TIER3S_LAUNCH_TOKEN"]):', 'if False:',
     [f"{TSM}::test_helper_refuses_a_bad_stanza[token]"]),
    ("L7 the launch helper passes a NUL through (fable P3-6)", LH,
     'if any("\\0" in x for x in out):', 'if False:',
     [f"{TSM}::test_helper_refuses_a_bad_stanza[nul]"]),
    ("L8 the launch helper drops systemd's notify socket", LH,
     '    NOTIFY_SOCKET="${NOTIFY_SOCKET:-}" \\\n', '',
     [f"{TSM}::test_helper_execs_the_spawn_with_exactly_the_stanza"]),
    ("S13 tier3s start through the generic 30 s start (no notify bound)", SM,
     '                            start_unit = self._ops.tier3s_systemctl_start\n', '',
     [f"{TSM}::test_start_exports_the_stanza_and_starts_only_the_tier3s_unit"]),
    ("S14 a failed tier3s start is recorded Stopped without verifying the launch gone", SM,
     '            survived = self._ops.tier3s_silo_running(silo.name)\n        except Exception as check_err:',
     '            survived = False\n        except Exception as check_err:',
     [f"{TSM}::test_a_failed_start_whose_launch_is_not_verified_gone_stays_active",
      f"{TSM}::test_a_refused_launch_fails_start_leaves_stopped_and_a_retry_starts"]),
    ("S15 the refused-start path of tier3s removed", SM,
     '                        if silo.kind == KIND_TIER3S:\n                            self._fail_tier3s_start(silo, e)\n', '',
     [f"{TSM}::test_a_refused_launch_fails_start_leaves_stopped_and_a_retry_starts",
      f"{TSM}::test_a_failed_start_whose_launch_is_not_verified_gone_stays_active"]),
    ("S16 the tier3s start bound below the unit's TimeoutStartSec", SM,
     '_T_TIER3S_START = 135', '_T_TIER3S_START = 100',
     [f"{TSM}::test_tier3s_start_uses_the_notify_bound"]),
    ("S17 reconciliation runs on a host without the tier3s install (fable P3-6)", SM,
     '        if not self._ops.tier3s_installed():\n            return []', '        if False:\n            return []',
     [f"{TSM}::test_reconciliation_is_skipped_without_the_tier3s_install"]),
    ("S18 a record dir without its state no longer counts (fable P3-6)", SM,
     '                # a record dir without its state file is still a record\n                tokens.append(d.name)',
     '                pass',
     [f"{TSM}::test_running_true_while_a_control_record_of_the_unit_survives",
      f"{TSM}::test_an_incomplete_record_blocks_no_unrelated_stop"]),
    ("U4 the launch unit acknowledges before the launch runs (Type=simple)", UNITF,
     "Type=notify\n", "Type=simple\n",
     [f"{TSM}::test_unit_file_shape"]),
    ("U3 stop propagation replaced by PartOf (restart would relaunch)", UNITF,
     "StopPropagatedFrom=qdistro-session-manager.service\n", "PartOf=qdistro-session-manager.service\n",
     [f"{TSM}::test_unit_file_shape"]),
    # --- astra + fable A r2
    ("R36 a state printed by a query that timed out or failed is evidence (astra r2 #1)", CLEAN,
     '    prop "$1" ActiveState || return 0\n', '    prop "$1" ActiveState\n',
     [f"{TS}::test_a_state_printed_before_a_timeout_or_error_is_no_evidence[{p}-{h}]"
      for p in ("record", "label", "incomplete", "scope") for h in ("hang_after", "fail_after")]),
    ("R37 a call's process group is not killed after the call (astra r2 #2)", CLEAN,
     '    [ -z "$CUR_PG" ] || kill -KILL -- "-$CUR_PG" 2>/dev/null\n', '    :\n',
     [f"{TS}::test_a_term_ignoring_helper_of_a_timed_out_call_is_killed"]),
    ("R38 a call's output is captured through a pipe a leftover can hold (astra r2 #2)", CLEAN,
     '    timeout -k "$KILL_AFTER" "$CAP" "$@" < "$CALL_IN" > "$OUTF" 2> "$WORKDIR/err" 8<&- 9>&- &\n',
     '    { timeout -k "$KILL_AFTER" "$CAP" "$@" < "$CALL_IN" 2> "$WORKDIR/err" 8<&- 9>&- | cat > "$OUTF"; } &\n',
     [f"{TS}::test_a_helper_holding_the_output_open_blocks_nothing"]),
    ("R39 a TERM to the cleanup leaves its call in flight running (astra r2 #2)", CLEAN,
     'trap on_signal TERM INT HUP\n', '',
     [f"{TS}::test_a_term_to_the_cleanup_kills_its_call_in_flight"]),
    ("R40 a call scope's cgroup is not killed after the call (astra r2 #2)", CLEAN,
     '    { echo 1 > "$1/cgroup.kill"; } 2>/dev/null\n', '',
     [f"{TS}::test_admin_calls_run_in_their_own_scope_killed_after_the_call"]),
    ("R41 admin calls run without their own scope (astra r2 #2)", CLEAN,
     '    bounded --scope "$t" runuser -u', '    bounded "$t" runuser -u',
     [f"{TS}::test_admin_calls_run_in_their_own_scope_killed_after_the_call"]),
    ("R42 a call scope that does not empty is ignored (astra r2 #2)", CLEAN,
     """    reap_call || { say "the call scope of '$*' did not empty after SIGKILL"; rc=124; }\n""", '    reap_call\n',
     [f"{TS}::test_a_call_scope_that_does_not_empty_is_a_failed_query"]),
    ("R43 the orphan-dir loop ignores the deadline (astra r2 #3)", CLEAN,
     '            if past_deadline; then say "$d: reap deadline reached; orphan per-launch dir not processed (preserved)"',
     '            if false; then say "$d: reap deadline reached; orphan per-launch dir not processed (preserved)"',
     [f"{TS}::test_reap_stale_deadline_covers_the_orphan_dirs"]),
    ("R44 calls are not capped by the batch deadline (astra r2 #3)", CLEAN,
     '    if [ -n "$END_US" ]; then\n        now_us; left=', '    if false; then\n        now_us; left=',
     [f"{TS}::test_a_query_is_cut_at_the_batch_deadline",
      f"{TS}::test_the_unit_lock_wait_is_capped_by_the_deadline"]),
    ("R45 systemd's BindsTo stop is waited for by query count (astra r2 #3)", CLEAN,
     '            until_us 20\n            while unit_status "$scope"; [ "$US" = live ]; do\n'
     '                now_us; [ "$NOW_US" -lt "$UNTIL" ] || break\n                sleep "$STEP"\n            done\n',
     '            for _ in $(seq 1 80); do unit_status "$scope"; [ "$US" = live ] || break; sleep "$STEP"; done\n',
     [f"{TS}::test_the_bindsto_wait_is_by_the_clock"]),
    ("R46 the token lock wait is not capped by the deadline (astra r2 #3)", CLEAN,
     '        cap "$2" || { exec 8<&-; return 2; }       # the batch deadline caps the wait\n'
     '        flock -w "$CAP" 8', '        flock -w "$2" 8',
     [f"{TS}::test_the_unit_lock_wait_is_capped_by_the_deadline"]),
    ("R47 a killed cleanup's work dir is never swept", CLEAN,
     '        for d in "$CTL"/.call-*; do', '        for d in ; do',
     [f"{TS}::test_reap_stale_sweeps_the_work_dir_of_a_killed_cleanup"]),
    ("R48 the label listing starts after the deadline (astra r2 #3)", CLEAN,
     '        if past_deadline; then\n            say "reap deadline reached; labelled containers',
     '        if false; then\n            say "reap deadline reached; labelled containers',
     [f"{TS}::test_reap_stale_deadline_preserves_what_it_did_not_reach"]),
    ("A27 the start poll is bounded by a query count (fable r2 P3-3)", SPAWN,
     'poll_end=$((SECONDS + POLL_S))\nwhile [ "$SECONDS" -lt "$poll_end" ]; do\n', 'for _ in $(seq 1 240); do\n',
     [f"{TS}::test_the_start_poll_is_bounded_by_the_clock"]),
    ("A28 READY=1 sent by a child of the spawn, not the spawn's own PID (astra r2 #4)", SPAWN,
     '    NOTIFY_SOCKET="$NOTIFY_SOCK" systemd-notify --ready --status="tier3s launch $TOKEN running"\n',
     """    NOTIFY_SOCKET="$NOTIFY_SOCK" sh -c 'systemd-notify "$@"; exit $?' sh --ready --status="tier3s launch $TOKEN running"\n""",
     [f"{TS}::test_ready_is_sent_by_the_spawn_process_itself"]),
    ("A29 .new-<token> removed after the global lock is released (fable r2 P3-6)", SPAWN,
     '    [ "$ARMED" = 0 ] || rm -rf -- "${CTL:?}/.new-$TOKEN" 2>/dev/null\n    exec 9>&-\n',
     '    exec 9>&-\n    [ "$ARMED" = 0 ] || rm -rf -- "${CTL:?}/.new-$TOKEN" 2>/dev/null\n',
     [f"{TS}::test_unpublished_record_is_removed_under_the_global_lock"]),
    ("U5 any process in the launch unit's cgroup may send READY=1 (astra r2 #4)", UNITF,
     "NotifyAccess=main\n", "NotifyAccess=all\n",
     [f"{TSM}::test_unit_file_shape"]),
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
    files = {f: REPO / f for f in (PROBE, PROV, WRAP, SPAWN, CLEAN, HELP, MKP,
                                   SM, LH, UNITF, BRK, INST)}
    orig = {f: p.read_bytes() for f, p in files.items()}
    orig_sha = {f: sha(p) for f, p in files.items()}
    for f in orig_sha:
        print(f"original sha256 {orig_sha[f]}  {f}")
    rc, failed, skipped, summary = pytest([TP, TV, TS, TSM, TB])
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
    rc, failed, skipped, summary = pytest([TP, TV, TS, TSM, TB])
    print(f"AFTER RESTORE: rc={rc} {summary}")
    bad += 0 if rc == 0 else 1
    print(f"RESULT {'PASS' if bad == 0 else 'FAIL'}: {len(muts)} mutations, {bad} problem(s)")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
