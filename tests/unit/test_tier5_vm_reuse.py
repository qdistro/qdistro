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
    # the name is matched LITERALLY — pgrep -f treats it as ERE, so
    # `t5.a` would match `t5xa` and hand the caller foreign pids to
    # signal (sol r15); an awk index() substring check never widens
    assert "pgrep -f --" not in body
    assert 'ENVIRON["T5N"]' in body
    live = _section("domain_is_live")
    # the oracle alone is the liveness authority — no domstate hint can
    # override it in either direction (sol r8)
    assert "virsh domstate" not in live
    assert "qemu_pids_for_domain" in live
    # adopt, refuse, idle-window, wait-loop, and the early check all
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


def test_stopped_foreign_domain_is_refused_not_reclaimed():
    # ensures: a defined domain bound to another (or no) app can NEVER
    #          be destroyed by this wrapper — confirmed qemu death only
    #          proves no live guest is killed; it does not make the
    #          foreign definition or disk ours (sol r11). The launch
    #          refuses outright: no domstate terminal check, no
    #          undefine, no overlay rm against the foreign guest.
    start = SPAWN.index('if [ "$_ds" -eq 0 ] && [ "$(domain_appid)" != "$POLICY_KEY" ]')
    end = SPAWN.index("create_domain\n    fi\n    flock -s 9")
    block = SPAWN[start:end]
    assert "refusing to reclaim another app's guest" in block
    assert "REAP=0; exit 3" in block
    # nothing destructive may remain in the foreign path
    assert "undefine_domain" not in block
    assert "virsh destroy" not in block
    assert 'rm -f "$DISK"' not in block
    assert "virsh domstate" not in block


def test_orphan_reaper_uses_qemu_liveness_not_domstate():
    # ensures: the startup orphan-overlay reaper cannot delete a live
    #          guest's disk OR a defined domain's overlay — it takes the
    #          target domain's own hold (racing spawns serialize),
    #          requires a CONFIRMED-dead domain_is_live verdict, and
    #          only reaps overlays whose domain is CONFIRMED ABSENT. It
    #          never undefines: a defined-but-dead domain has an owner
    #          record the reaper cannot adjudicate (sol r11).
    reaper = SPAWN[SPAWN.index("orphan-overlay reaper"):
                   SPAWN.index("reaped orphan overlay")]
    # per-target flock serializes against a wrapper spawning that domain
    assert 'hold-$base' in reaper
    assert 'flock -n -x "$hfd"' in reaper
    assert 'domain_is_live "$base"' in reaper
    assert reaper.count('_lv=$?') >= 2  # verdict before AND after settle
    # dominfo uses the precise 3-state helper — and only a CONFIRMED
    # absence (rc 1) may proceed to the rm
    assert 'domain_defined_state "$base"; _ds=$?' in reaper
    assert '[ "$_ds" -ne 1 ]' in reaper
    # no undefine ever: deleting a startable definition's disk is not
    # the reaper's call to make
    assert "virsh undefine" not in reaper
    assert "undefine_domain" not in reaper
    # exact backing-file match via JSON field, not a substring grep
    assert '--output=json' in reaper
    assert '"backing-filename"' in reaper
    assert "came alive during reap" in SPAWN


def test_defined_state_helper_is_precise_and_defined_before_first_call():
    # ensures: domain absence comes from a SUCCESSFUL enumeration, not
    #          dominfo stderr — error-text absence detection drifts
    #          across libvirt versions (12.x prints only "failed to get
    #          domain 'X'") and loose substrings match outage text like
    #          "libvirt socket not found" (sol r11). A successful
    #          `virsh list --all --name` without the name is confirmed
    #          absence; ANY query failure is unverifiable. And the
    #          helper must be DEFINED before the startup reaper's call
    #          site — bash resolves functions at call time; a later
    #          definition returns 127 and silently skips every orphan.
    body = _section("domain_defined_state")
    assert "virsh list --all --name" in body
    assert 'grep -Fxq -- "$name"' in body
    assert "dominfo" not in body
    # defined before the reaper calls it
    assert SPAWN.index("domain_defined_state()") \
        < SPAWN.index('domain_defined_state "$base"')
    # the cleanup helper uses the same enumeration oracle
    cb = re.search(
        r"domain_defined_state\(\)\s*\{(?P<body>.*?)\n\}",
        CLEANUP, re.S,
    ).group("body")
    assert "virsh list --all --name" in cb
    assert "dominfo" not in cb


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


def test_qemu_scan_requires_the_name_argv_token():
    # ensures: the ps-line substring is only a CANDIDATE filter — the
    #          verdict also requires the guest=<name>, token to sit as
    #          the argument right after `-name` in the pid's OWN argv.
    #          A foreign qemu carrying the string inside another
    #          argument (file=/tmp/-name guest=t5-x,b.qcow2) must never
    #          be handed to a caller that signals it (sol r18). The
    #          argv walk parses on NUL — the only delimiter an argv
    #          entry cannot contain: a newline-folding tr would let a
    #          single crafted arg manufacture the fake pair (sol r19)
    for src, name in ((SPAWN, "spawn"), (CLEANUP, "cleanup")):
        body = re.search(
            r"qemu_pids_for_domain\(\)\s*\{(?P<body>.*?)\n\}",
            src, re.S,
        ).group("body")
        assert 'cmdline", "rb"' in body, name
        assert '.split(b"\\0")' in body, name
        assert 'a == b"-name"' in body, name
        assert 'argv[i + 1].startswith(needle)' in body, name
        assert "tr '\\0' '\\n'" not in body, name
        # the argv check sits inside the candidate loop, after comm
        assert body.index('c=$(cat "/proc/$p/comm"') \
            < body.index('python3 - "$p"'), name


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
    #          create_domain — a transient query outage at the
    #          decision layer cannot route into `virsh start` on a
    #          stopped foreign guest (sol r8)
    body = _section("create_domain")
    assert "domain_defined_state; _ds=$?" in body
    # define happens ONLY on confirmed absence (rc 1)
    assert '[ "$_ds" -eq 1 ] && _create_domain_define' in body
    # a defined domain must carry our binding before start
    assert '[ "$(domain_appid)" != "$POLICY_KEY" ]' in body
    assert "refusing to start it" in body
    # unverifiable state refuses outright
    assert "cannot determine state" in body


def test_overlay_reuse_requires_an_inode_bound_owner_record():
    # ensures: an overlay outliving its domain can be booted only when
    #          its <disk>.appid sidecar records OUR appId AND the
    #          dev/ino of THE file it was stamped for — a lone sidecar
    #          left by a crashed create, or one copied beside a planted
    #          disk, can never authorize that disk (sol r12/r19). Every
    #          unproven state REFUSES AND PRESERVES: the create goes
    #          through a .partial name, so a file at $DISK without its
    #          record is never our residue — not ours to boot OR to
    #          delete (sol r20)
    body = _section("create_domain")
    assert 'read -r _sapp _sident' in body
    assert '[ "$_sident" != "$(stat -c \'%d %i\' "$DISK"' in body
    assert '"backing-filename"' in body
    assert 'refusing to boot an unproven disk' in body
    assert '[ "$_sapp" != "$POLICY_KEY" ]' in body
    # no unproven file is ever deleted by the launch path
    assert 'unproven — replacing it' not in body
    assert 'rm -f "$DISK" "$DISK.appid"' not in body
    # the gate is unconditional — a defined-ours domain's binding
    # vouches for the PATH, not the inode: a file swapped in while the
    # domain was stopped must still refuse (sol r21)
    gate = body[body.index('if [ -f "$DISK" ]'):body.index('$DISK absent')]
    assert '_ds' not in gate
    # teardown's unlink requires the inode-bound record in EVERY case —
    # the claim never vouched for the current file either (sol r21) —
    # and a LONE sidecar gets the same check: only our regular-file
    # record is removed; a foreign, symlinked, or unreadable record is
    # preserved, never unlinked (sol r22)
    body = _section("cleanup")
    assert "overlay owner unproven" in body
    assert '[ "$_sapp" != "$POLICY_KEY" ]' in body
    assert '_claim_ds' not in body
    assert 'keeping $DISK.appid' in body
    assert 'foreign or unreadable record' in body
    assert '[ ! -L "$DISK.appid" ]' in body
    assert 'rm -f "$DISK.appid"' in body
    # a symlinked or non-regular $DISK is foreign construction — a
    # symlink to the bound inode would satisfy the dev/ino check while
    # the path is unowned: preserve BOTH the path and its record
    # (sol r24)
    assert '[ -f "$DISK" ] && [ ! -L "$DISK" ]' in body
    assert 'foreign construction' in body


def test_create_goes_through_a_recoverable_partial():
    # ensures: a crashed create leaves only provable residue — the
    #          overlay is built under a .partial name, the sidecar
    #          binds that inode, and the link into place is a
    #          no-clobber ln. A sidecar+matching-partial pair is
    #          provably ours and is recovered; a bare $DISK without
    #          its record can then NEVER be our crash residue, so
    #          refusing it is correct (sol r19/r20)
    create = _section("_create_overlay_disk")
    # mktemp claims BOTH temp names with O_EXCL — a foreign file at a
    # predictable partial or sidecar-temp path can never be
    # overwritten or unlinked by our create (sol r21/r22)
    assert 'mktemp "$DISK.partial.XXXXXX"' in create
    assert 'mktemp "$DISK.appid.tmp.XXXXXX"' in create
    assert 'qemu-img create -f qcow2 -F qcow2 -b "$DISK_BASE" "$_tmp"' \
        in create
    assert 'stat -c \'%d %i\'' in create
    # the record installs BEFORE the disk link, and both install via
    # no-clobber ln -T — a directory/symlink at the destination is
    # never a sink, and a file appearing mid-create is never
    # clobbered; the -ef verify proves the intended file landed
    # (sol r23)
    assert create.index('ln -T "$_stmp" "$DISK.appid"') \
        < create.index('ln -T "$_tmp" "$DISK"')
    assert create.count('"$DISK" -ef') + create.count('$DISK.appid" -ef') \
        >= 2
    assert 'mv -f' not in create
    # ANY pre-existing record refuses — a matching appId alone is not
    # ownership: a lone record binds no inode this create can verify
    # (the recoverable pair was already finished by the caller), so
    # removing it would convert unproven ownership into permission to
    # unlink. The create never removes a present record at all — its
    # failure paths only ever remove OUR O_EXCL temps (sol r24)
    assert 'holds an unproven record' in create
    assert '[ -L "$DISK.appid" ]' in create
    assert 'rm -f "$DISK.appid"' not in create
    assert 'rm -f "$DISK"' not in create
    # the record temp is written AS ADMIN (tee), never reopened by the
    # root shell — a swapped symlink cannot make root truncate a
    # foreign file (sol r24)
    assert 'run_as_admin tee "$_stmp"' in create
    assert '> "$_stmp"' not in create
    assert '[ -L "$_stmp" ]' in create
    assert '[ -L "$_tmp" ]' in create
    # recovery: only a partial whose dev/ino the sidecar binds is
    # finished — dev+ino cannot be forged by a planted file — and a
    # non-regular/foreign/unreadable record is preserved, never
    # overwritten (sol r21/r22)
    body = _section("create_domain")
    rec = body[body.index("$DISK absent:"):]
    assert '"$DISK".partial.*' in rec
    assert 'stat -c \'%d %i\' "$_cand"' in rec
    assert 'ln -T "$_recover" "$DISK"' in rec
    assert 'refusing to clobber' in rec
    assert 'holds a foreign or' in rec
    assert '[ ! -L "$DISK.appid" ]' in rec
    # the record refuse precedes any write to $DISK.appid
    assert rec.index('holds a foreign or') \
        < rec.index('_create_overlay_disk')
    assert 'rm -f "$DISK" "$DISK.appid"' in SPAWN
    assert 'rm -f "$overlay" "$overlay.appid"' in SPAWN
    assert 'rm -f "$DISK" "$DISK.appid"' in CLEANUP


def test_reaper_preserves_owned_orphan_overlays():
    # ensures: a nonempty <overlay>.appid sidecar is the ONLY owner
    #          record — the reaper reaps only disks whose sidecar
    #          re-derives to their own t5 name AND names an appId the
    #          CURRENT non-stale manifest no longer publishes (a
    #          retired app's disk is provably stale). A missing
    #          sidecar is unproven ownership, not "no owner" — a
    #          hand-made t5-shaped name is only a shape (sol r17). A
    #          stale-marker manifest (the reconciler publishes a
    #          READABLE empty one on scan failure), an unparseable
    #          one, an unreadable sidecar, or an unverifiable verdict
    #          are all ambiguity -> preserve (sol r12/r13)
    reaper = SPAWN[SPAWN.index("orphan-overlay reaper"):
                   SPAWN.index("reaped orphan overlay")]
    assert 'manifest_app_state "$_sapp" "$DISK_BASE"; _ms=$?' in reaper
    assert '[ "$_ms" -eq 1 ]' in reaper
    # the sidecar is REQUIRED, not optional: a missing, unreadable, or
    # empty one preserves, and it must BOTH bind the overlay's own
    # dev/ino (a copied record is not proof — sol r19) AND re-derive
    # to the disk's name — a t5-shaped name is only a shape (sol r17)
    assert 'read -r _sapp _sident' in reaper
    assert '[ -n "$_sapp" ]' in reaper
    assert '[ "$_sident" = "$(stat -c \'%d %i\' "$overlay"' in reaper
    assert '[ "$(tier5_derived_name "$_sapp")" = "$base" ]' in reaper
    # a symlinked overlay or record is foreign construction, not
    # proof — preserve (sol r23)
    assert '[ ! -L "$overlay" ]' in reaper
    assert '[ ! -L "$overlay.appid" ]' in reaper
    # the ownership gate sits between confirmed-absent and the rm
    assert reaper.index('manifest_app_state') \
        < reaper.index('rm -f "$overlay"')


def test_reaper_only_touches_tier5_namespace():
    # ensures: a backing-file match alone never proves tier-5
    #          ownership — a manually created overlay of our base in
    #          the shared image dir keeps its disk. Only names in the
    #          VMApps._vmNameForApp namespace (t5-<stub>-<16hex>) are
    #          reapable at all (sol r16)
    reaper = SPAWN[SPAWN.index("orphan-overlay reaper"):
                   SPAWN.index("reaped orphan overlay")]
    assert '[[ "$base" =~ ^t5-' in reaper
    assert '[0-9a-f]{16}' in reaper
    # the namespace gate precedes the backing probe and the rm
    assert reaper.index('$base" =~') < reaper.index('qemu-img info')
    assert reaper.index('$base" =~') < reaper.index('rm -f "$overlay"')


def test_reaper_reverifies_after_settle():
    # ensures: the settle window cannot invalidate the earlier
    #          verdicts — defined-state and the owner record are
    #          re-checked after the sleep and before the unlink, so an
    #          out-of-band define, a dropped sidecar, or a fresh
    #          catalogue publication preserves the overlay (sol r16)
    reaper = SPAWN[SPAWN.index("orphan-overlay reaper"):
                   SPAWN.index("reaped orphan overlay")]
    sleep_at = reaper.index("sleep 0.3")
    tail = reaper[sleep_at:]
    assert tail.count("domain_defined_state") >= 1
    assert tail.count("manifest_app_state") >= 1
    assert tail.index("domain_defined_state") \
        < tail.index('rm -f "$overlay"')
    assert tail.index("manifest_app_state") \
        < tail.index('rm -f "$overlay"')
    # a sidecar that APPEARED/changed/vanished during the settle is
    # re-read and re-validated, not assumed — inode binding included
    # (sol r16/r17/r19)
    assert tail.count('read -r _sapp _sident') >= 1
    assert tail.count('tier5_derived_name "$_sapp"') >= 1
    assert tail.count('stat -c \'%d %i\' "$overlay"') >= 1
    # sidecar removal rides along with the overlay rm
    assert 'rm -f "$overlay" "$overlay.appid"' in reaper


def test_manifest_app_state_is_exact_parse_with_stale_ambiguity():
    # ensures: retirement is decided by an EXACT appId match in a
    #          parsed, non-stale, schema-current manifest whose
    #          recorded base dev/ino/mtime/size matches the CURRENT
    #          base — never a quoted substring (could appear in
    #          another field), never a stale manifest the reconciler
    #          publishes READABLE on scan failure, and never a
    #          non-stale manifest left over from a swapped-out base
    #          (it omits the new base's apps — sol r13/r14)
    body = _section("manifest_app_state")
    assert "json.load" in body
    assert "os.stat" in body
    assert 'doc.get("stale")' in body
    assert 'doc.get("schema") != 1' in body
    assert "baseDev" in body and "baseIno" in body
    assert "baseMtime" in body and "baseSize" in body
    assert '"$DISK_BASE"' in SPAWN[SPAWN.index("manifest_app_state \"$"):]
    assert "sys.exit(2)" in body
    assert 'sys.exit(0 if any(r["appId"] == side for r in apps) else 1)' \
        in body
    # grep-based substring matching would be imprecise
    assert "grep" not in body
    # every row must pass the scanner's own _row_ok strictness — an
    # appId-only row or an empty apps list is a transient/invalid
    # catalogue, not proof of retirement (sol r15)
    assert '_row_ok' in body
    for k in ('"appId"', '"desktopId"', '"name"', '"iconName"',
              '"comment"', '"execArgv"'):
        assert k in body
    assert 'json.loads' in body  # execArgv must parse like the scanner
    assert 'or not apps or' in body  # empty catalogue != "all retired"
    # int-mtime granularity + inode reuse let a DIFFERENT base carry the
    # same identity tuple — the manifest must also pin the nanosecond
    # mtime and not be older than the current base's last write
    # (sol r15/r16)
    assert 'baseMtimeNs' in body
    assert 'st_mtime_ns' in body
    assert 'mst.st_mtime_ns < st.st_mtime_ns' in body


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
        < CLEANUP.index("domain_defined_state; _ds=$?")
    assert "live wrapper session holds" in CLEANUP


def test_cleanup_helper_requires_absent_definition_for_unlink():
    # ensures: the standalone cleanup tool cannot leave a startable
    #          domain pointing at a deleted overlay — the rm requires a
    #          verified-absent definition (a successful enumeration
    #          without the name — a query failure is ambiguity, never
    #          absence) AND confirmed-dead qemu (sol r7/r11)
    tail = CLEANUP[CLEANUP.index('if [ -e "$DISK" ]'):]
    assert "domain_defined_state; _ds=$?" in tail
    assert '[ "$_ds" -eq 0 ]' in tail
    assert '[ "$_ds" -ne 1 ]' in tail
    assert "still defined" in tail
    assert "cannot verify" in tail
    assert "qemu_pids_for_domain; _lv=$?" in tail
    assert '[ "$_lv" -eq 1 ]' in tail
    assert 'rm -f "$DISK"' in tail
    # definition check precedes the qemu check precedes the rm
    assert tail.index("domain_defined_state") \
        < tail.index("qemu_pids_for_domain")
    assert tail.index("qemu_pids_for_domain") < tail.index('rm -f "$DISK"')
    # an orphan disk has no definition to carry the binding — the rm
    # additionally requires tier-5 provenance (backing == our base)
    # and surfaces the sidecar's bound appId (sol r13)
    assert '"backing-filename"' in tail
    assert "not a tier-5 overlay" in tail
    assert '"$DISK.appid"' in tail
    assert tail.index('"backing-filename"') \
        < tail.index('rm -f "$DISK"')
    # undefine failure is surfaced, not swallowed into "not present"
    assert "WARN: undefine" in CLEANUP


def test_teardown_reverifies_binding_after_winning_the_claim():
    # ensures: winning the teardown claim only proves no wrapper holds
    #          the domain NOW — if the original guest died and another
    #          app reclaimed the name, the last holder must not destroy
    #          the foreign replacement (sol r9). A defined domain must
    #          re-verify its appId binding before reap_domain runs;
    #          unverifiable state keeps everything.
    body = _section("cleanup")
    gate = body[body.index('[ "$REAP" = "1" ]'):body.index("reap_domain")]
    assert "domain_defined_state; _ds=$?" in gate
    assert "_bound=$(domain_appid)" in gate
    assert '[ "$_ds" -eq 0 ] && [ "$_bound" != "$POLICY_KEY" ]' in gate
    assert "re-bound" in gate
    assert '[ "$_ds" -eq 2 ]' in gate
    # both keep-branches leave domain_reaped=0 so nothing is destroyed
    # or unlinked
    assert gate.count("domain_reaped=0") >= 2


def test_cleanup_helper_requires_tier5_binding_to_destroy():
    # ensures: the standalone tool cannot destroy a domain that is not
    #          provably tier-5's — the hold serializes wrappers, but
    #          only the appId binding stamped at define time proves
    #          ownership; unbound or unreadable domains are refused
    #          before any virsh destroy (sol r9)
    head = CLEANUP[:CLEANUP.index('virsh destroy "$VM_NAME"')]
    assert "qdistro:appId" in head
    assert "virsh dumpxml" in head
    assert "not provably ours" in head
    assert "exit 3" in head
    # the binding check runs INSIDE the hold and BEFORE destroy
    assert head.index("flock -w 5 -x 9") < head.index("qdistro:appId")


def test_owner_records_must_rederive_to_the_artifact_name():
    # ensures: a t5-shaped name is only a SHAPE — the owner record on
    #          the domain (binding) or disk (sidecar) must re-derive to
    #          the requested name via tier5_derived_name, the port of
    #          VMApps._vmNameForApp, or the tool refuses (sol r17)
    assert "tier5_derived_name()" in SPAWN and "tier5_derived_name()" \
        in CLEANUP
    # cleanup: a defined domain whose binding derives a DIFFERENT
    # guest is refused before destroy
    head = CLEANUP[:CLEANUP.index('virsh destroy "$VM_NAME"')]
    assert '[[ "$VM_NAME" =~ ^t5- ]]' in head
    assert 'tier5_derived_name "$_bound")" != "$VM_NAME"' in head
    assert "derives a different guest" in head
    # cleanup: an orphan disk needs an inode-bound sidecar (or the
    # explicit ownerless opt-in), and a t5 name demands derivation
    # coherence — a record copied beside a different disk is refused
    # even WITH the opt-in (sol r19)
    tail = CLEANUP[CLEANUP.index('if [ -e "$DISK" ]'):]
    assert "TIER5_CLEANUP_ALLOW_OWNERLESS" in tail
    assert 'tier5_derived_name "$_sapp")" != "$VM_NAME"' in tail
    assert 'stat -c \'%d %i\' "$DISK"' in tail
    assert "does not bind this" in tail
    # a present-but-incoherent record (symlink, unreadable) is kept
    # even with the opt-in — only a genuinely ABSENT sidecar qualifies
    assert '[ ! -L "$DISK.appid" ]' in tail
    assert "not a readable" in tail
    # and a symlinked/non-regular $DISK itself is foreign construction —
    # kept before any provenance read (sol r24)
    assert '[ ! -f "$DISK" ] || [ -L "$DISK" ]' in tail
    assert 'not a regular file' in tail
    assert tail.index("TIER5_CLEANUP_ALLOW_OWNERLESS") \
        < tail.index('rm -f "$DISK"')
