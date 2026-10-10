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
    #          domstate after acquiring it
    assert "flock -u 9 2>/dev/null || true" in SPAWN
    assert "flock -w 120 -x 9" in SPAWN
    decision = SPAWN[SPAWN.index("ADOPTED=0"):]
    upgrade = decision.index("flock -w 120 -x 9")
    recheck = decision.index("domstate", upgrade)
    create = decision.index("create_domain", upgrade)
    assert upgrade < recheck < create
    assert "flock -s 9" in decision[: decision.index("DOMAIN_DEFINED=1") if "DOMAIN_DEFINED=1" in decision else len(decision)]
    assert "[tier5] adopted running domain" in SPAWN


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
    # ensures: a foreign domain is only reclaimed after domstate confirms
    #          a TERMINAL state — an unknown/false state fails closed and
    #          no virsh destroy ever runs against another app's guest
    #          (nested-KVM domstate is documented flaky, sol r4)
    assert "reclaiming stopped foreign domain" in SPAWN
    start = SPAWN.index('if run_as_admin virsh dominfo "$VM_NAME" >/dev/null 2>&1 \\\n                && [ "$(domain_appid)" != "$POLICY_KEY" ]')
    end = SPAWN.index("create_domain\n    fi\n    flock -s 9")
    reclaim = SPAWN[start:end]
    assert "shutoff|crashed" in reclaim
    assert "refusing to reclaim" in reclaim
    assert 'virsh destroy "$VM_NAME"' not in reclaim
    assert 'virsh undefine "$VM_NAME"' in reclaim
    assert 'rm -f "$DISK"' in reclaim


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
