"""Regression contracts for tier-5 relaunch reuse (per-app VM adoption).

The per-app guest is shared across relaunches of the same catalogue
appId via an flock(2) ownership protocol on
/run/qdistro-tier5/hold-<vm_name>: a shared session hold (fd9), a short
exclusive decision hold for adopt-or-create, and a nonblocking exclusive
teardown claim (fd10) that the LAST holder out of the idle window wins.
These are source invariants — the lock protocol is what keeps an earlier
wrapper's exit from reaping a VM a relaunch adopted, and keeps create
races from double-defining the domain.
"""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
SPAWN = (ROOT / "tier5-vm/spawn-tier5.sh").read_text()
CLEANUP = (ROOT / "tier5-vm/qdistro-tier5-cleanup.sh").read_text()
VMAPS = (ROOT / "qdshell/Services/Qdistro/VMApps.qml").read_text()
PROVIDER = (
    ROOT / "qdshell/Modules/Panels/Launcher/Providers/VMAppsProvider.qml"
).read_text()


def _section(name):
    m = re.search(name + r"\s*\(\)\s*\{(?P<body>.*?)\n\}", SPAWN, re.S)
    assert m, f"{name} not found in spawn-tier5.sh"
    return m.group("body")


def test_hold_file_and_fds_are_per_vm_and_locked_shared():
    # ensures: every wrapper touching a domain coordinates on one
    #          per-VM lock file, never a global lock that would serialize
    #          unrelated apps
    assert 'HOLD_FILE="$HOLD_DIR/hold-$VM_NAME"' in SPAWN
    assert 'exec 9>>"$HOLD_FILE"' in SPAWN
    assert 'exec 10>>"$HOLD_FILE"' in SPAWN
    assert "flock -w 120 -s 9" in SPAWN


def test_create_path_serializes_under_exclusive_hold():
    # ensures: two racing cold-starts can never double-define the domain
    #          — the create runs under an exclusive lock and re-checks
    #          liveness (the qemu oracle, not bare domstate) after
    #          acquiring it
    assert "flock -u 9 2>/dev/null || true" in SPAWN
    assert "flock -w 120 -x 9" in SPAWN
    decision = SPAWN[SPAWN.index("ADOPTED=0"):]
    upgrade = decision.index("flock -w 120 -x 9")
    recheck = decision.index("domain_is_live", upgrade)
    create = decision.index("create_domain", upgrade)
    assert upgrade < recheck < create
    assert "flock -s 9" in decision[: decision.index("DOMAIN_DEFINED=1") if "DOMAIN_DEFINED=1" in decision else len(decision)]
    assert "[tier5] adopted running domain" in SPAWN


def test_liveness_decisions_use_the_qemu_oracle():
    # ensures: every spot that decides "is the guest alive" goes through
    #          domain_is_live (domstate hint + qemu-pid scan with an
    #          explicit cannot-determine rc), never bare `domstate|grep
    #          running` — nested-virt domstate lies (sol r6)
    body = _section("qemu_pids_for_domain")
    # the oracle distinguishes clean-empty from scan-failure
    assert "return 2" in body and "return 1" in body
    assert "run_as_admin true" in body  # runuser broken != "no qemu"
    # the match requires the discrete `-name guest=` argv token
    assert '"-name guest=$name,"' in body
    live = _section("domain_is_live")
    # the oracle alone is the liveness authority — no domstate hint can
    # override it in either direction (sol r8)
    assert "virsh domstate" not in live
    assert "qemu_pids_for_domain" in live
    # adopt, reclaim, idle-window, wait-loop, and the early check all
    # consume the oracle
    assert SPAWN.count("domain_is_live") >= 6


def test_teardown_is_claimed_by_the_last_holder_only():
    # ensures: a wrapper exiting while a relaunched session holds the VM
    #          leaves it running — teardown requires winning an
    #          exclusive claim, and losing the claim skips destroy. The
    #          claim is BOUNDED (-w) not -n: a transient foreign hold
    #          must not starve the last real owner (sol r4)
    body = _section("decide_teardown")
    assert "release_hold" in body
    assert "flock -w 2 -x 10" in body
    assert "flock -n -x 10" not in body
    cleanup = SPAWN[SPAWN.index("cleanup() {"):]
    assert "decide_teardown" in cleanup
    reap_pos = cleanup.index("reap_domain")
    assert cleanup.index('[ "$REAP" = "1" ]') < reap_pos


def test_idle_window_releases_the_session_hold_for_adoption():
    # ensures: the keep-warm window actually allows a relaunch to attach
    #          — the session hold is dropped BEFORE the window, and any
    #          lock contention during it ends teardown
    body = _section("idle_keep_warm")
    assert body.index("release_hold") < body.index("IDLE_SHUTDOWN_SECS")
    assert "flock -n -x 10" in body
    assert "adopted by a relaunch; leaving it running" in body
    assert "REAP=0" in body


def test_vmapps_vm_name_is_deterministic_per_appid():
    # ensures: relaunches of the same app name the same domain — that is
    #          the identity the lock protocol and the silo tag share
    m = re.search(
        r"function _vmNameForApp\(appId\)\s*\{(?P<body>.*?)\n    \}",
        VMAPS,
        re.S,
    )
    assert m, "_vmNameForApp not found in VMApps.qml"
    assert "Math.random" not in m.group("body")
    assert '"t5-" + ' in m.group("body")
    assert "_generateVmName" not in VMAPS
    assert "root._vmNameForApp(row.appId)" in VMAPS


def test_vmapps_vm_name_is_collision_resistant():
    # ensures: distinct appIds can never share a domain (sol r1/r2 P1) —
    #          the name carries a 64-bit hash of the FULL appId appended
    #          AFTER the truncated stub, and the catalogue-load guard is
    #          the hard guarantee: colliders are dropped, never launched
    body = re.search(
        r"function _vmNameForApp\(appId\)\s*\{(?P<body>.*?)\n    \}",
        VMAPS,
        re.S,
    ).group("body")
    assert "_appIdHash(id)" in body, (
        "the VM name must include a hash of the full appId, "
        "not just the sanitized stub"
    )
    stub_slice = body.index(".slice(")
    hash_call = body.index("_appIdHash(id)")
    assert stub_slice < hash_call, (
        "the hash must be computed over the full id and appended after "
        "the truncated stub"
    )
    h = re.search(
        r"function _appIdHash\(appId\)\s*\{(?P<body>.*?)\n    \}",
        VMAPS,
        re.S,
    )
    assert h, "_appIdHash not found in VMApps.qml"
    assert "appId.charCodeAt" in h.group("body"), (
        "the hash must iterate the full appId string"
    )
    assert "pad(fnv) + pad(djb)" in h.group("body"), (
        "the tag must combine two independent 32-bit hashes — a lone "
        "32-bit hash is collision-searchable (sol r2)"
    )


def test_catalogue_drops_domain_collisions_fail_closed():
    # ensures: the hard guarantee — whatever the hash, two appIds that
    #          derive one domain name are BOTH dropped at catalogue load
    #          and never reach launch(); an all-colliding manifest falls
    #          back to foot rather than launching nothing silently
    assert "function _dropDomainCollisions" in VMAPS
    assert "byName[n] !== r.appId" in VMAPS
    assert "root._dropDomainCollisions(rows)" in VMAPS
    assert "no launchable apps" in VMAPS


def test_domain_carries_its_bound_appid():
    # ensures: the domain XML records which appId it belongs to — the
    #          name alone is not identity across catalogue reloads
    template = (ROOT / "tier5-vm/domain-template.xml").read_text()
    assert "<qdistro:appId" in template and "__APP_ID__" in template
    assert 's|__APP_ID__|$POLICY_KEY|g' in SPAWN
    assert "domain_appid()" in SPAWN
    assert "<qdistro:appId" in SPAWN


def test_adoption_verifies_bound_appid():
    # ensures: a running domain is only adopted when its bound appId
    #          equals our POLICY_KEY — BOTH the fast path and the
    #          post-exclusive re-check gate on it (sol r3: a guest
    #          outlives the manifest it came from)
    decision = SPAWN[SPAWN.index("ADOPTED=0"):]
    # both adoption sites check the binding before setting ADOPTED
    assert decision.count('domain_appid)') >= 2
    assert decision.count("refusing cross-app adoption") >= 2
    for bound in (i for i in range(len(decision))
                  if decision.startswith("_bound=$(domain_appid)", i)):
        assert "ADOPTED=1" in decision[bound:], "binding check lost adopt"
    # a refusal pins REAP=0: a wrapper that never established ownership
    # must not tear down the foreign guest on its way out
    assert decision.count("REAP=0; exit 3") >= 2


def test_stopped_foreign_domain_is_reclaimed_not_adopted():
    # ensures: a foreign domain is reclaimed only when its backing qemu
    #          is proven DEAD by domain_is_live — domstate alone can
    #          report "shut off" for a live qemu under nested KVM, so it
    #          is only a belt on top of the oracle verdict (sol r5/r6).
    #          No virsh destroy ever runs against a foreign guest, and a
    #          settle + post-undefine liveness recheck protects the
    #          overlay from an out-of-band start in the probe gap.
    assert "reclaiming stopped foreign domain" in SPAWN
    start = SPAWN.index('if [ "$_ds" -eq 0 ] && [ "$(domain_appid)" != "$POLICY_KEY" ]')
    end = SPAWN.index("create_domain\n    fi\n    flock -s 9")
    reclaim = SPAWN[start:end]
    assert "shutoff|crashed" in reclaim
    assert "refusing to reclaim" in reclaim
    assert 'virsh destroy "$VM_NAME"' not in reclaim
    # metadata-aware undefine + proof the definition is gone (an
    # unverifiable dominfo is ambiguity, not absence)
    assert "undefine_domain" in reclaim
    assert "domain_defined_state; _ds=$?" in reclaim
    assert '[ "$_ds" -ne 1 ]' in reclaim
    assert "or unverifiable after undefine" in reclaim
    assert 'rm -f "$DISK"' in reclaim
    # settle + confirmed-dead recheck between undefine and unlink
    assert reclaim.index("sleep 0.3") < reclaim.index('rm -f "$DISK"')
    assert reclaim.index("domain_is_live", reclaim.index("undefine")) \
        < reclaim.index('rm -f "$DISK"')
    assert "live or" in reclaim  # the post-undefine refuse branch


def test_orphan_reaper_uses_qemu_liveness_not_domstate():
    # ensures: the startup orphan-overlay reaper cannot delete a live
    #          guest's disk — it takes the target domain's own hold
    #          (racing spawns serialize), requires a CONFIRMED-dead
    #          domain_is_live verdict, distinguishes "not found" from a
    #          libvirt outage via the dominfo error text, requires
    #          undefine success on a defined domain, and settles +
    #          re-verifies before the rm (sol r7)
    reaper = SPAWN[SPAWN.index("orphan-overlay reaper"):
                   SPAWN.index("reaped orphan overlay")]
    # per-target flock serializes against a wrapper spawning that domain
    assert 'hold-$base' in reaper
    assert 'flock -n -x "$hfd"' in reaper
    assert 'domain_is_live "$base"' in reaper
    assert reaper.count('_lv=$?') >= 2  # verdict before AND after undefine
    # dominfo uses the precise 3-state helper — "not found" vs outage
    assert 'domain_defined_state "$base"; _ds=$?' in reaper
    assert 'not found|no domain|does not exist' in SPAWN
    # a defined domain must be undefine'd SUCCESSFULLY (no || true)
    assert 'virsh undefine "$base"' in reaper
    assert 'virsh undefine "$base" >/dev/null 2>&1 || true' not in reaper
    # exact backing-file match via JSON field, not a substring grep
    assert '--output=json' in reaper
    assert '"backing-filename"' in reaper
    assert "came alive during reap" in SPAWN


def test_foreign_refusal_never_joins_the_lock_session_early():
    # ensures: the common foreign-running case is refused BEFORE the fd9
    #          session hold is taken, so a mismatched spawn can never
    #          starve the true owner's bounded teardown claim (sol r4)
    early = SPAWN.index("refusing cross-app adoption")
    hold = SPAWN.index('flock -w 120 -s 9')
    assert early < hold, "the unlocked foreign check must precede fd9"


def test_launch_requires_current_catalogue_membership():
    # ensures: a stale provider row cannot reach a domain its appId no
    #          longer owns — launch() gates on the CURRENT catalogue
    #          before deriving the name (sol r3)
    m = re.search(
        r"function launch\(row\)\s*\{(?P<body>.*?)\n    \}",
        VMAPS,
        re.S,
    )
    assert m, "launch(row) not found in VMApps.qml"
    body = m.group("body")
    assert "root.catalogue" in body
    assert "not in current catalogue" in body
    assert body.index("root.catalogue") < body.index("_vmNameForApp")


def test_launch_has_no_manifest_vmname_bypass():
    # ensures: the ONLY domain name source is the deterministic
    #          derivation — a manifest-supplied vmName would skip the
    #          collision guard and pin two apps to one guest (sol r2)
    m = re.search(
        r"function launch\(row\)\s*\{(?P<body>.*?)\n    \}",
        VMAPS,
        re.S,
    )
    assert m, "launch(row) not found in VMApps.qml"
    assert "row.vmName" not in m.group("body")


def test_invalid_explicit_policy_key_fails_not_falls_back():
    # ensures: an explicit --policy-key/env key outside the charset is a
    #          hard error — the old silent basename fallback let two
    #          distinct invalid appIds collapse onto one binding and
    #          share a guest (sol r4)
    keyblock = SPAWN[SPAWN.index('POLICY_KEY="${EXPLICIT_POLICY_KEY'):
                   SPAWN.index("_conf_file_ok")]
    assert "FAIL: invalid policy key" in keyblock
    assert "exit 3" in keyblock
    # the unkeyed basename default is sanitized, never left invalid
    assert "tr -c 'A-Za-z0-9._/-' '_'" in keyblock
    # and the wrapper never rewrites a BAD explicit key to the basename
    assert 'POLICY_KEY="$APP_BASENAME"' not in keyblock


def test_concurrent_session_logs_are_unique_per_launch():
    # ensures: two live wrappers of the same app never clobber each
    #          other's waypipe logs — log names carry a per-launch token
    assert "${LAUNCH_TOKEN:0:8}-client.log" in SPAWN
    assert "${LAUNCH_TOKEN:0:8}-server.log" in SPAWN


def test_provider_settings_action_targets_the_same_appid():
    # ensures: the launcher gear opens the app's own override row, so the
    #          appId stays the single identity across launch + settings
    assert "function getItemActions(item)" in PROVIDER
    assert "SettingsPanel.Tab.VMApps" in PROVIDER
    assert '"labelKey": item.appId' in PROVIDER
    assert "SettingsPanelService.openToEntry" in PROVIDER


def test_qemu_scan_unreadable_comm_is_uncertain_not_dead():
    # ensures: a pgrep hit whose /proc/<pid>/comm cannot be READ is
    #          ambiguity, never "confirmed dead" — the pid might be our
    #          qemu, so an all-unreadable candidate list must report
    #          unreliable (rc 2), not a clean empty scan (sol r7)
    for src, name in ((SPAWN, "spawn"), (CLEANUP, "cleanup")):
        body = re.search(
            r"qemu_pids_for_domain\(\)\s*\{(?P<body>.*?)\n\}",
            src, re.S,
        ).group("body")
        assert 'c=$(cat "/proc/$p/comm"' in body, name
        assert "unread" in body, name
        # an unreadable candidate while no qemu was confirmed -> rc 2
        assert '[ -n "$unread" ] && return 2' in body, name
        # and the confirmed-dead rc 1 sits BEHIND that uncertainty check
        # in the same conditional: unread candidates resolve to rc 2 first
        assert '&& return 2; return 1' in body, name


def test_wait_loop_breaks_only_on_confirmed_dead():
    # ensures: neither an ambiguous nor a LYING domstate can end or
    #          wedge the session watch — the loop consults the qemu
    #          oracle every iteration (a stale "running" claim for a
    #          dead guest cannot hide it), breaks only on
    #          confirmed-dead, and keeps waiting on live or
    #          unverifiable scans (sol r7/r8)
    loop = SPAWN[SPAWN.index("exec_gone=0\n    while :; do"):
                 SPAWN.index("wait loop ended")]
    # domstate is not consulted in the watch at all — it can go stale
    # "running" for a dead guest and wedge the loop (sol r8)
    assert "virsh domstate" not in loop
    # the oracle runs every iteration and only rc1 breaks
    assert "domain_is_live; _lv=$?" in loop
    assert '[ "$_lv" -eq 1 ] && break' in loop


def test_domain_is_live_is_qemu_oracle_only():
    # ensures: a stale "running" domstate cannot mark a dead guest live —
    #          liveness is the qemu-pid scan alone, and domstate is
    #          never consulted (it lies in both directions under
    #          nested virt, sol r8)
    body = _section("domain_is_live")
    assert "domstate" not in body
    assert "qemu_pids_for_domain" in body


def test_locked_decision_refuses_unverifiable_defined_state():
    # ensures: a dominfo outage cannot slip a stopped foreign domain
    #          past the binding check — the locked decision requires a
    #          precise defined-state, and an unverifiable query refuses
    #          to create (sol r8)
    decision = SPAWN[SPAWN.index("timed out waiting for the"):
                     SPAWN.index("create_domain\n    fi\n    flock -s 9")]
    assert "domain_defined_state; _ds=$?" in decision
    assert '[ "$_ds" -eq 2 ]' in decision
    assert "refusing to create over it" in decision
    # the foreign check only fires on a DEFINED result (rc 0)
    assert '[ "$_ds" -eq 0 ] && [ "$(domain_appid)" != "$POLICY_KEY" ]' \
        in decision


def test_create_domain_reverifies_binding_before_start():
    # ensures: an already-defined domain is re-bound-checked inside
    #          create_domain — a transient dominfo outage at the
    #          decision layer cannot route into `virsh start` on a
    #          stopped foreign guest (sol r8)
    body = _section("create_domain")
    assert "domain_defined_state; _ds=$?" in body
    # define happens ONLY on confirmed absence (rc 1)
    assert body.index("1)  _create_domain_define") \
        < body.index("0)  if")
    # a defined domain must carry our binding before start
    assert '[ "$(domain_appid)" != "$POLICY_KEY" ]' in body
    assert "refusing to start it" in body
    # unverifiable state refuses outright
    assert "cannot determine state" in body


def test_cleanup_unlink_requires_confirmed_dead():
    # ensures: the normal teardown path cannot unlink $DISK while a qemu
    #          still lives or the scan cannot say — reap_domain's own
    #          undefine/dominfo verdict is not the final word; the rm is
    #          gated on a confirmed-dead oracle of its own (sol r7)
    body = _section("cleanup")
    gate = body[body.index("reap_domain"):]
    assert "domain_is_live; _lv=$?" in gate
    assert '[ "$_lv" -eq 1 ]' in gate
    # the rm sits INSIDE the confirmed-dead branch, after the check
    assert gate.index('[ "$_lv" -eq 1 ]') < gate.index('rm -f "$DISK"')
    assert "live or unverifiable" in gate


def test_create_domain_fails_closed_on_unverifiable_liveness():
    # ensures: an unverifiable scan cannot route into `virsh start` —
    #          rc 2 fails closed (domain/disk retained, no start guess),
    #          only confirmed-dead (rc 1) starts; live (rc 0) proceeds
    #          to adoption-equivalent behaviour (sol r7)
    body = _section("create_domain")
    assert "domain_is_live; _lv=$?" in body
    assert '[ "$_lv" -eq 1 ]' in body
    assert '[ "$_lv" -eq 2 ]' in body
    assert "cannot determine liveness" in body
    # virsh start is only reachable from the confirmed-dead branch
    assert body.index('[ "$_lv" -eq 1 ]') \
        < body.index('run_as_admin virsh start')
    assert body.index('run_as_admin virsh start') \
        < body.index('[ "$_lv" -eq 2 ]')
    # the refuse path preserves everything — no teardown authorization
    refuse = body[body.index("cannot determine liveness"):]
    assert "REAP=0" in refuse


def test_cleanup_helper_serializes_on_the_domain_hold():
    # ensures: the standalone cleanup tool cannot tear down a domain a
    #          live wrapper owns nor race a spawn between its checks
    #          and the unlink — it claims the domain's hold file
    #          exclusively (bounded), and refuses while a session holds
    #          it (sol r8)
    assert 'HOLD_FILE="$HOLD_DIR/hold-$VM_NAME"' in CLEANUP
    assert 'exec 9>>"$HOLD_FILE"' in CLEANUP
    assert "flock -w 5 -x 9" in CLEANUP
    # the lock precedes every teardown step
    assert CLEANUP.index("flock -w 5 -x 9") \
        < CLEANUP.index('virsh dominfo "$VM_NAME"')
    assert "live wrapper session holds" in CLEANUP


def test_cleanup_helper_requires_absent_definition_for_unlink():
    # ensures: the standalone cleanup tool cannot leave a startable
    #          domain pointing at a deleted overlay — the rm requires a
    #          verified-absent definition (dominfo error text separates
    #          "not found" from a libvirt outage) AND confirmed-dead
    #          qemu (sol r7)
    tail = CLEANUP[CLEANUP.index('if [ -f "$DISK" ]'):]
    assert '_derr2=' in tail
    assert 'not found|no domain|does not exist' in tail
    assert "still defined" in tail
    assert "cannot verify" in tail
    assert "qemu_pids_for_domain; _lv=$?" in tail
    assert '[ "$_lv" -eq 1 ]' in tail
    assert 'rm -f "$DISK"' in tail
    # definition check precedes the qemu check precedes the rm
    assert tail.index("_derr2=") < tail.index("qemu_pids_for_domain")
    assert tail.index("qemu_pids_for_domain") < tail.index('rm -f "$DISK"')
    # undefine failure is surfaced, not swallowed into "not present"
    assert "WARN: undefine" in CLEANUP
