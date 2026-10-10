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
    #          exclusive claim, and losing the claim skips destroy
    body = _section("decide_teardown")
    assert "release_hold" in body
    assert "flock -n -x 10" in body
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
