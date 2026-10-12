#!/usr/bin/env bats
# qci:host-only — runs on the host in the bats gate, no VM (ci/lib/gates/bats.sh).
# tier5-vmapps-reconcile — the reconcile contract of
# tier5-vm/vmapps-reconcile.sh (vmapps-dynamic track):
#
#   * absent base            -> manifest removed, exit 0
#   * unreadable-by-admin    -> stale manifest published, exit 0
#   * fresh manifest + same identity -> fast path, scanner NOT run
#   * changed base           -> scanner runs, manifest republished
#   * base replaced mid-scan -> stale marker, then rescan
#   * scan failure           -> stale marker + retried, then real manifest
#   * attempt exhaustion     -> stale manifest stays, still exit 0
#
# Everything is driven through env overrides: a temp file stands in for
# the base qcow2, a PATH-shimmed or flag-controlled scanner stands in for
# guestfish, and VMAPPS_ADMIN_USER makes runuser's test -r resolvable.

setup() {
    REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    RECONCILE="$REPO_ROOT/tier5-vm/vmapps-reconcile.sh"
    SCANNER="$REPO_ROOT/tier5-vm/vmapps-scan.py"
    [ -x "$RECONCILE" ] || { echo "vmapps-reconcile.sh missing/not executable"; return 1; }

    export VMAPPS_BASE="$BATS_TEST_TMPDIR/base.qcow2"
    export VMAPPS_MANIFEST="$BATS_TEST_TMPDIR/out/base.json"
    export VMAPPS_ADMIN_USER="$USER"
    export VMAPPS_SCANNER="$SCANNER"
    export VMAPPS_MAX_ATTEMPTS=3
    export VMAPPS_BACKOFF_SECS=0
    mkdir -p "$BATS_TEST_TMPDIR/out" "$BATS_TEST_TMPDIR/root"

    # runuser needs root, which the host-side fixture does not have. Shim
    # it with the same contract: fail for unknown users, otherwise exec
    # the command as the current user (the base file is created by that
    # same user, so `test -r` answers for them).
    mkdir -p "$BATS_TEST_TMPDIR/shims"
    cat >"$BATS_TEST_TMPDIR/shims/runuser" <<'EOS'
#!/bin/bash
user="$2"; shift 3
getent passwd "$user" >/dev/null 2>&1 || exit 1
exec "$@"
EOS
    chmod +x "$BATS_TEST_TMPDIR/shims/runuser"
    export PATH="$BATS_TEST_TMPDIR/shims:$PATH"
}

is_stale() {
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if d.get('stale') and d['apps'] == [] else 1)" "$VMAPPS_MANIFEST"
}

has_apps() {
    python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if d.get('apps') else 1)" "$VMAPPS_MANIFEST"
}

# A --root fixture scanner: replaces the real scanner so reconcile's
# scan step reads fixture desktop files instead of guestfish.
root_scanner() {
    cat >"$BATS_TEST_TMPDIR/root-scan.sh" <<EOS
#!/bin/bash
exec "$SCANNER" --root "$BATS_TEST_TMPDIR/root" "\$@"
EOS
    chmod +x "$BATS_TEST_TMPDIR/root-scan.sh"
    export VMAPPS_SCANNER="$BATS_TEST_TMPDIR/root-scan.sh"
}

# A scanner shim that fails N times (counted in a state file) then
# delegates to the real --root scanner.
flaky_scanner() {
    local fails="$1"
    cat >"$BATS_TEST_TMPDIR/flaky-scan.sh" <<EOS
#!/bin/bash
state="$BATS_TEST_TMPDIR/flaky.count"
n=\$(cat "\$state" 2>/dev/null || echo 0)
echo \$((n + 1)) >"\$state"
if [ "\$n" -lt $fails ]; then exit 9; fi
exec "$SCANNER" --root "$BATS_TEST_TMPDIR/root" "\$@"
EOS
    chmod +x "$BATS_TEST_TMPDIR/flaky-scan.sh"
    export VMAPPS_SCANNER="$BATS_TEST_TMPDIR/flaky-scan.sh"
}

fixture_guest() {
    mkdir -p "$BATS_TEST_TMPDIR/root/usr/share/applications" \
             "$BATS_TEST_TMPDIR/root/usr/bin"
    printf '#!/bin/sh\n' >"$BATS_TEST_TMPDIR/root/usr/bin/foot"
    chmod 0755 "$BATS_TEST_TMPDIR/root/usr/bin/foot"
    cat >"$BATS_TEST_TMPDIR/root/usr/share/applications/org.kde.konsole.desktop" <<'EOS'
[Desktop Entry]
Type=Application
Name=Konsole
Exec=konsole
EOS
}

@test "absent base removes any manifest and exits 0" {
    printf '{old}\n' >"$VMAPPS_MANIFEST"
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    [ ! -e "$VMAPPS_MANIFEST" ]
    [[ "$output" == *"absent"* ]]
}

@test "unreadable base publishes a stale manifest and exits 0" {
    printf 'x' >"$VMAPPS_BASE"
    # A user that cannot exist: runuser fails under any euid, so the
    # admin-can-read check deterministically reports false.
    export VMAPPS_ADMIN_USER=qdistro-no-such-user-9x
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    is_stale || { echo "manifest is not a stale marker:"; cat "$VMAPPS_MANIFEST"; return 1; }
}

@test "fresh manifest with unchanged base takes the fast path" {
    fixture_guest
    root_scanner
    printf 'x' >"$VMAPPS_BASE"
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    has_apps || { echo "first scan did not publish apps:"; cat "$VMAPPS_MANIFEST"; return 1; }
    cp "$VMAPPS_MANIFEST" "$BATS_TEST_TMPDIR/first.json"
    # Record every scanner argv; delegate the read-only verbs (--check /
    # --emit-stale) to the real scanner, but a real scan (--base) is a
    # fast-path violation and must fail loudly.
    cat >"$BATS_TEST_TMPDIR/record-scan.sh" <<EOS
#!/bin/bash
echo "\$*" >>"$BATS_TEST_TMPDIR/scan-calls.log"
case "\$1" in
    --check|--emit-stale) exec "$SCANNER" "\$@" ;;
    *) echo "SCAN-RAN" >&2; exit 42 ;;
esac
EOS
    chmod +x "$BATS_TEST_TMPDIR/record-scan.sh"
    export VMAPPS_SCANNER="$BATS_TEST_TMPDIR/record-scan.sh"
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"fast path"* ]]
    [[ "$output" != *"SCAN-RAN"* ]]
    # Only the fast-path --check ran; nothing rescanned.
    run grep -c -- '--check' "$BATS_TEST_TMPDIR/scan-calls.log"
    [ "$output" = "1" ]
    ! grep -q -- '--out' "$BATS_TEST_TMPDIR/scan-calls.log"
    cmp -s "$VMAPPS_MANIFEST" "$BATS_TEST_TMPDIR/first.json"
}

@test "changed base forces a rescan and republish" {
    fixture_guest
    root_scanner
    printf 'one' >"$VMAPPS_BASE"
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    ino1=$(python3 -c "import json; print(json.load(open('$VMAPPS_MANIFEST'))['baseIno'])")
    rm "$VMAPPS_BASE"; printf 'two-two-two' >"$VMAPPS_BASE"; sleep 1
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"manifest published"* ]]
    ino2=$(python3 -c "import json; print(json.load(open('$VMAPPS_MANIFEST'))['baseIno'])")
    [ "$ino1" != "$ino2" ]
}

@test "base replaced mid-scan publishes stale then rescans" {
    fixture_guest
    cat >"$BATS_TEST_TMPDIR/replace-scan.sh" <<EOS
#!/bin/bash
case "\$1" in
    --check|--emit-stale) exec "$SCANNER" "\$@" ;;
esac
if [ ! -f "$BATS_TEST_TMPDIR/swapped" ]; then
    touch "$BATS_TEST_TMPDIR/swapped"
    rm -f "$VMAPPS_BASE"
    # Different size AND a bumped mtime: the identity tuple must differ
    # even if the kernel reuses the inode number.
    printf 'replaced-different-size' >"$VMAPPS_BASE"
    touch -m -d '+2 hours' "$VMAPPS_BASE"
fi
exec "$SCANNER" --root "$BATS_TEST_TMPDIR/root" "\$@"
EOS
    chmod +x "$BATS_TEST_TMPDIR/replace-scan.sh"
    export VMAPPS_SCANNER="$BATS_TEST_TMPDIR/replace-scan.sh"
    printf 'original' >"$VMAPPS_BASE"
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"base replaced during scan"* ]]
    [[ "$output" == *"manifest published"* ]]
    has_apps || { echo "no real manifest after mid-scan replace:"; cat "$VMAPPS_MANIFEST"; return 1; }
}

@test "scan failure retries then publishes a real manifest" {
    fixture_guest
    flaky_scanner 2
    printf 'x' >"$VMAPPS_BASE"
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    [ "$(cat "$BATS_TEST_TMPDIR/flaky.count")" -eq 3 ]
    has_apps || { echo "no real manifest after retries:"; cat "$VMAPPS_MANIFEST"; return 1; }
}

@test "exhausted retries keep the stale marker and exit 0" {
    # Scan always fails; --emit-stale must still reach the real scanner
    # or the stale publish itself would be broken by the shim.
    cat >"$BATS_TEST_TMPDIR/boom.sh" <<EOS
#!/bin/bash
case "\$1" in
    --check|--emit-stale) exec "$SCANNER" "\$@" ;;
    *) exit 9 ;;
esac
EOS
    chmod +x "$BATS_TEST_TMPDIR/boom.sh"
    export VMAPPS_SCANNER="$BATS_TEST_TMPDIR/boom.sh"
    export VMAPPS_MAX_ATTEMPTS=2
    printf 'x' >"$VMAPPS_BASE"
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    [[ "$output" == *"reconcile exhausted"* ]]
    is_stale || { echo "manifest is not a stale marker:"; cat "$VMAPPS_MANIFEST"; return 1; }
}

@test "catalogue without guest foot surfaces as stale after retries" {
    mkdir -p "$BATS_TEST_TMPDIR/root/usr/share/applications"
    cat >"$BATS_TEST_TMPDIR/root/usr/share/applications/firefox.desktop" <<'EOS'
[Desktop Entry]
Type=Application
Name=Firefox
Exec=firefox
EOS
    root_scanner
    printf 'x' >"$VMAPPS_BASE"
    run "$RECONCILE"
    [ "$status" -eq 0 ]
    is_stale || { echo "manifest is not a stale marker:"; cat "$VMAPPS_MANIFEST"; return 1; }
}
