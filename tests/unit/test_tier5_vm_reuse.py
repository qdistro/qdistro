"""Contracts around tier-5 relaunch reuse that live OUTSIDE the lifecycle.

qdshell's VMApps derives the per-app domain name, gates launches on the
current catalogue and starts the lifecycle directly as admin; tier-4 keeps
its own spawn invariants. The lifecycle itself (hold/create/live locks,
adoption, teardown, reaper, cleanup tool) is exercised by executing the
scripts in test_tier5_lifecycle.py.
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
T4SPAWN = (ROOT / "tier4-vm/spawn-tier4.sh").read_text()
VMAPS = (ROOT / "qdshell/Services/Qdistro/VMApps.qml").read_text()
PROVIDER = (
    ROOT / "qdshell/Modules/Panels/Launcher/Providers/VMAppsProvider.qml"
).read_text()


def test_vmapps_launches_the_lifecycle_directly_as_admin():
    # ensures: qdshell no longer pkexecs the VM lifecycle — the only
    #          privileged step is the bridge the lifecycle itself invokes
    m = re.search(
        r"function launch\(row\)\s*\{(?P<body>.*?)\n    \}",
        VMAPS,
        re.S,
    )
    assert m, "launch(row) not found in VMApps.qml"
    code = "\n".join(line for line in m.group("body").splitlines()
                     if not line.lstrip().startswith("//"))
    assert "pkexec" not in code and "spawnLauncher" not in VMAPS
    assert "const cmd = [root.spawnHelper," in code


def test_domain_template_carries_its_bound_appid():
    # ensures: the domain XML records which appId it belongs to — the
    #          name alone is not identity across catalogue reloads
    template = (ROOT / "tier5-vm/domain-template.xml").read_text()
    assert "<qdistro:appId" in template and "__APP_ID__" in template


def test_tier4_admin_paths_are_written_by_admin():
    # ensures: tier-4 (still root + runuser) never opens a write on a path
    #          inside admin's runtime dir from the root shell (sol r25)
    assert ': >"$CLIENT_LOG"' not in T4SPAWN
    assert '>"$TMP_XML"' not in T4SPAWN
    assert 'chown "$ADMIN_USER" "$CLIENT_LOG"' not in T4SPAWN
    assert 'chown "$ADMIN_USER" "$RDP_' not in T4SPAWN
    assert 'run_as_admin tee "$TMP_XML"' in T4SPAWN
    assert T4SPAWN.count("sh -c 'exec >>") >= 2


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


def test_provider_settings_action_targets_the_same_appid():
    # ensures: the launcher gear opens the app's own override row, so the
    #          appId stays the single identity across launch + settings
    assert "function getItemActions(item)" in PROVIDER
    assert "SettingsPanel.Tab.VMApps" in PROVIDER
    assert '"labelKey": item.appId' in PROVIDER
    assert "SettingsPanelService.openToEntry" in PROVIDER


def test_tier4_rdp_password_never_enters_argv():
    # ensures: the RDP secret reaches the admin writer through stdin,
    #          never through a process argument — a password in argv
    #          is visible in process listings and /proc to every local
    #          user (sol r26)
    assert 'printf \'%s\\n\' "$RDP_PASSWORD" | run_as_admin' in T4SPAWN
    assert "cat >\"$1\"" in T4SPAWN
    # no argv-bearing variant survives anywhere in tier4
    assert '"$RDP_PASSWORD" >"$1"' not in T4SPAWN
    assert '_ "$RDP_PASSWORD_FILE" "$RDP_PASSWORD"' not in T4SPAWN
