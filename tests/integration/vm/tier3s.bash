# tier3s.bash — HOST-side bats helpers for the tier 3s (gVisor runsc) qci
# lane: phase7-tier3s-{headless,denied,sigkill-cleanup}.bats. Load after
# helpers (`load helpers; load tier3s`). todo/paravirt 06 "Provisioning in the
# qci lane": each bats worker is a fresh VM whose image was built WITHOUT
# QDISTRO_TIER3S=1 (owner O10). setup_file stages, on the file's private
# driver-staging HTTP server (stage_vm_driver):
#   - the tested commit (`git archive HEAD`; a dirty installed tree refuses);
#   - the pinned runsc tarball from ~/.cache/qdistro/runsc/<release>/, fetched
#     on a cache miss and sha512 checked against tier3s/RUNSC_RELEASE here AND
#     in the guest;
#   - the workload image as an OCI archive built once in a qci worker VM on a
#     cache miss (tier3s/cache-image-archive.sh <vm>), its sha256 checked here
#     against its manifest and its image ID asserted in the guest;
# then runs tests/integration/vm/tier3s-guest-setup.sh in the guest
# (installer with QDISTRO_TIER3S=1 from the tested commit, offline provision,
# probe PASS, image load, broker allow rule). No podman or runsc runs on the
# host.
#
# Evidence: bats prints a passing test's output nowhere, so every guest
# transcript is written to the TAP stream (fd 3, `# `-prefixed: it lands in
# qci's per-file log) and to <scratch>/<name>.log (QCI_SCENARIO_TMPDIR, kept
# in the qci run directory).

t3s_repo() { git -C "$(dirname "$BATS_TEST_FILENAME")" rev-parse --show-toplevel; }

# t3s_log <name>: keep $output (the last vm_run) as evidence.
t3s_log() {
    local dir="${QCI_SCENARIO_TMPDIR:-${BATS_FILE_TMPDIR:-/tmp}}"
    mkdir -p "$dir"
    printf '%s\n' "$output" > "$dir/$1.log"
    printf '%s\n' "$output" | sed 's/^/# /' >&3
    printf '# [tier3s] %s transcript: %s\n' "$1" "$dir/$1.log" >&3
}

# The three files run in parallel against separate workers. Only one may fill
# the shared host caches; the others recheck the pinned inputs after the lock.
# Building the OCI archive in a worker changes its rootless podman image store,
# but the guest setup below removes that tag and loads the checked archive.
t3s_prepare_inputs() {
    local repo=$1 rel=$2 want=$3 cdir=$4 cache tar lock tmp="" url key log
    cache="$HOME/.cache/qdistro/runsc/$rel"
    tar="$cache/gvisor.tar.zstd"
    lock="$HOME/.cache/qdistro/tier3s-bootstrap.lock"
    mkdir -p "$cache" "${QCI_SCENARIO_TMPDIR:-${BATS_FILE_TMPDIR:-/tmp}}" || return 1
    log="${QCI_SCENARIO_TMPDIR:-${BATS_FILE_TMPDIR:-/tmp}}/tier3s-cache-build.log"
    (
        flock -w 4800 9 || { echo "tier3s: timed out waiting for the shared cache lock" >&2; exit 1; }
        if [ "$(sha512sum "$tar" 2>/dev/null | cut -d' ' -f1)" != "$want" ]; then
            if [ "${QCI_OFFLINE:-0}" = 1 ]; then
                echo "tier3s: pinned runsc tarball missing or invalid in offline mode: $tar" >&2
                exit 1
            fi
            url=$(sed -n 's/^base_url=//p' "$repo/tier3s/RUNSC_RELEASE")
            [ -n "$url" ] || { echo "tier3s: pin has no base_url" >&2; exit 1; }
            tmp=$(mktemp "$cache/.gvisor.tar.zstd.XXXXXXXX") || exit 1
            trap 'rm -f -- "$tmp"' EXIT
            curl -fLSs --connect-timeout 30 --max-time 900 "$url/gvisor.tar.zstd" -o "$tmp" || exit 1
            [ "$(sha512sum < "$tmp" | cut -d' ' -f1)" = "$want" ] || {
                echo "tier3s: downloaded runsc tarball sha512 differs from RUNSC_RELEASE" >&2; exit 1;
            }
            mv -f -- "$tmp" "$tar" || exit 1
            tmp=""
        fi
        key=$(cd "$repo" && bash tier3s/cache-image-archive.sh --key) || exit 1
        if [ ! -s "$cdir/manifest.txt" ] || [ ! -s "$cdir/tier3s-headless-smoke.oci.tar" ] ||
           [ "$(sed -n 's/^INPUT_KEY=//p' "$cdir/manifest.txt" 2>/dev/null)" != "$key" ] ||
           [ "$(sha256sum "$cdir/tier3s-headless-smoke.oci.tar" 2>/dev/null | cut -d' ' -f1)" != "$(sed -n 's/^IMAGE_ARCHIVE_SHA256=//p' "$cdir/manifest.txt" 2>/dev/null)" ]; then
            if [ "${QCI_OFFLINE:-0}" = 1 ]; then
                echo "tier3s: pinned OCI archive missing or invalid in offline mode: $cdir" >&2
                exit 1
            fi
            echo "tier3s: building pinned OCI archive in worker $VM_NAME; log: $log" >&2
            if ! (cd "$repo" && bash tier3s/cache-image-archive.sh "$VM_NAME" --force) >"$log" 2>&1; then
                cat "$log" >&2
                exit 1
            fi
        fi
    ) 9>"$lock" || { fail_loud "tier 3s host inputs could not be prepared (see cache diagnostics above)"; return 1; }
}

# t3s_stage <driver> [gui-workloads]: serve the driver, the guest lib/setup
# and the inputs. <gui-workloads> is a comma list of GUI workload names
# (weston-terminal,foot) whose OCI archives are staged alongside headless
# (Phase B, s123-s129); their per-workload manifest keys
# IMAGE_ARCHIVE_SHA256_<WORKLOAD> are checked here.
t3s_stage() {
    local repo stage rel want got cdir man arch dirty w W gui="${2:-}"
    repo=$(t3s_repo) || fail_loud "cannot find the repo root" || return 1
    dirty=$(git -C "$repo" status --porcelain -- tier3s session_manager broker templates scripts/install snapshot.conf)
    if [ -n "$dirty" ]; then
        fail_loud "uncommitted changes under the installed trees; the worker installs git archive HEAD (the tested commit): $dirty"
        return 1
    fi
    stage_vm_driver tier3s-guest-lib.sh || return 1
    stage_vm_driver tier3s-guest-setup.sh || return 1
    stage_vm_driver "$1" || return 1
    stage="$(_qd_driver_stage_dir)"
    git -C "$repo" archive --format=tar HEAD > "$stage/src.tar" || { fail_loud "git archive HEAD failed"; return 1; }
    git -C "$repo" rev-parse HEAD > "$stage/commit.txt"
    rel=$(sed -n 's/^release=//p' "$repo/tier3s/RUNSC_RELEASE")
    want=$(sed -n 's/^tarball_sha512=//p' "$repo/tier3s/RUNSC_RELEASE")
    cdir=$(cd "$repo" && bash tier3s/cache-image-archive.sh --dir)
    t3s_prepare_inputs "$repo" "$rel" "$want" "$cdir" || return 1
    got=$(sha512sum "$HOME/.cache/qdistro/runsc/$rel/gvisor.tar.zstd" 2>/dev/null | cut -d' ' -f1)
    if [ -z "$want" ] || [ "$got" != "$want" ]; then
        fail_loud "host runsc cache ~/.cache/qdistro/runsc/$rel/gvisor.tar.zstd missing or sha512 != tier3s/RUNSC_RELEASE"
        return 1
    fi
    ln -sf "$HOME/.cache/qdistro/runsc/$rel/gvisor.tar.zstd" "$stage/gvisor.tar.zstd"
    man="$cdir/manifest.txt"; arch="$cdir/tier3s-headless-smoke.oci.tar"
    if [ ! -s "$man" ] || [ ! -s "$arch" ]; then
        fail_loud "no workload image archive for this commit's image inputs at $cdir after cache preparation"
        return 1
    fi
    if [ "$(sha256sum < "$arch" | cut -d' ' -f1)" != "$(sed -n 's/^IMAGE_ARCHIVE_SHA256=//p' "$man")" ]; then
        fail_loud "image archive sha256 != its manifest ($cdir)"
        return 1
    fi
    ln -sf "$arch" "$stage/tier3s-headless-smoke.oci.tar"
    ln -sf "$man" "$stage/image-manifest.txt"
    printf '# [tier3s] tested commit %s; runsc %s (sha512 ok); image %s\n' \
        "$(cat "$stage/commit.txt")" "$rel" "$(sed -n 's/^IMAGE_ID=//p' "$man")" >&3
    for w in ${gui//,/ }; do
        W=$(printf '%s' "$w" | tr 'a-z-' 'A-Z_')
        arch="$cdir/tier3s-$w.oci.tar"
        if [ ! -s "$arch" ]; then
            fail_loud "no $w image archive for this commit's image inputs at $cdir (rebuild: tier3s/cache-image-archive.sh <dev-vm>)"
            return 1
        fi
        if [ "$(sha256sum < "$arch" | cut -d' ' -f1)" != "$(sed -n "s/^IMAGE_ARCHIVE_SHA256_${W}=//p" "$man")" ]; then
            fail_loud "image archive tier3s-$w.oci.tar sha256 != its manifest ($cdir)"
            return 1
        fi
        ln -sf "$arch" "$stage/tier3s-$w.oci.tar"
        printf '# [tier3s] staged GUI image %s (%s)\n' "$w" "$(sed -n "s/^IMAGE_ID_${W}=//p" "$man")" >&3
    done
}

# t3s_setup_file <driver> [gui-workloads]: stage, then provision the fresh
# worker. With a non-empty <gui-workloads> the worker is brought to a live
# admin qdwin session (the GUI bridge needs the compositor + qdshell) and the
# guest setup loads each named GUI image archive.
t3s_setup_file() {
    t3s_stage "$@" || return 1
    local guiarg=""
    [ -n "${2:-}" ] && guiarg=" --gui $2"
    if [ -n "${2:-}" ]; then
        # the waypipe bridge needs the real compositor session up BEFORE the
        # guest-side checks (wayland-1 socket, qdshell) run
        start_user_session || fail_loud "admin user session (qdwin/qdshell) did not come up" || return 1
        # IPC injectFocus is not user activity. The stock 300s idle lock
        # fires during guest-setup and the later driver, and qdwin then
        # (correctly) posts ERROR_LOCKED on set_keyboard_focus. Hold the
        # locker off for the life of this disposable worker — same
        # QDLOCKER_IDLE_MS drop-in as tiered-isolation.bats / gui.sh.
        vm_run "$(cat <<'IDLE'
set -e
d=/etc/systemd/user/qdlocker.service.d
install -d -m0755 "$d"
printf '[Service]\nEnvironment=QDLOCKER_IDLE_MS=86400000\n' > "$d/99-qci-no-idle-lock.conf"
install -d -m0755 /etc/qdistro
: > /etc/qdistro/locker-ctrl-introspection
chown 0:0 /etc/qdistro/locker-ctrl-introspection
chmod 0644 /etc/qdistro/locker-ctrl-introspection
adm_uctl() {
    systemctl --user --machine=admin@.host "$@" 2>/dev/null \
        || runuser -l admin -c "systemctl --user $*" 2>/dev/null
}
adm_uctl daemon-reload
if ! adm_uctl is-active qdlocker.service >/dev/null; then
    echo "FAIL: qdlocker.service is not active after start_user_session" >&2
    exit 1
fi
adm_uctl restart qdlocker.service
env=$(adm_uctl show qdlocker.service -p Environment --value || true)
case "$env" in
    *QDLOCKER_IDLE_MS=86400000*) ;;
    *)
        echo "FAIL: running qdlocker lacks QDLOCKER_IDLE_MS=86400000 (Environment=$env)" >&2
        exit 1
        ;;
esac
echo "PASS: qdlocker idle-auto-lock disabled for GUI worker"
IDLE
)"
        assert_success || fail_loud "could not disable qdlocker idle-auto-lock for the GUI worker" || return 1
        assert_output_contains "PASS: qdlocker idle-auto-lock disabled for GUI worker" || return 1
    fi
    vm_run "mkdir -p /var/tmp/t3s-dl && cd /var/tmp/t3s-dl && for f in tier3s-guest-lib.sh tier3s-guest-setup.sh; do curl -fsS -o \$f http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/\$f || exit 97; done && bash tier3s-guest-setup.sh http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT} --expect-fresh$guiarg"
    t3s_log t3s-setup
    assert_success || fail_loud "tier 3s worker setup failed (see the t3s-setup transcript)" || return 1
    assert_output_contains "[t3s-setup] " || return 1
    t3s_no_failures t3s-setup || return 1
    assert_output_contains "PASS: o10: worker image (built without QDISTRO_TIER3S=1) carries no tier 3s file" || return 1
    assert_output_contains "PASS: o10: no tier 3s file after the installer ran without the flag" || return 1
    assert_output_contains "PASS: installed probe RESULT" || return 1
    assert_output_contains "PASS: loaded image ID = manifest IMAGE_ID" || return 1
    if [ -n "${2:-}" ]; then
        local w
        for w in ${2//,/ }; do
            assert_output_contains "PASS: loaded $w image ID = manifest" || return 1
        done
        assert_output_contains "PASS: admin compositor socket present" || return 1
        assert_output_contains "PASS: qdshell is up" || return 1
        assert_output_contains "PASS: qdlocker idle-auto-lock held off" || return 1
    fi
}

# t3s_run_driver <driver>: fetch the lib + driver, run it as root.
t3s_run_driver() {
    vm_run "cd /var/tmp/t3s-dl && for f in tier3s-guest-lib.sh $1; do curl -fsS -o \$f http://10.0.2.2:${QDISTRO_BATS_HTTP_PORT}/\$f || exit 97; done && bash $1"
}

# t3s_no_failures <tag>: the driver's own summary says 0 failures and no FAIL
# line was printed (a missing summary = the driver died early = failure).
t3s_no_failures() {
    if ! grep -qE "^\[$1\] [0-9]+ passes, 0 failures$" <<<"$output"; then
        echo "--- [$1] summary missing or failures reported ---" >&2
        grep -E "^FAIL|^\[$1\]" <<<"$output" >&2
        return 1
    fi
    if grep -q '^FAIL' <<<"$output"; then
        echo "--- FAIL lines in the [$1] transcript ---" >&2
        grep '^FAIL' <<<"$output" >&2
        return 1
    fi
}

t3s_teardown_file() {
    local rc=0
    reap_vm_drivers || { fail_loud "could not reap the driver-staging http server" || rc=1; }
    # the worker VM is disposable; drop the test-authored rule anyway
    vm_run "rm -f /etc/qdistro/rules.d/50-tier3s-qci.yaml"
    return "$rc"
}
