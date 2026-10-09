#!/bin/bash
# tier3s-guest-setup.sh — GUEST side (root) provisioning of one qci bats worker
# for the tier 3s drivers s120-s122 (todo/paravirt 06 "Provisioning in the qci
# lane"). The host-side bats setup (tier3s.bash: t3s_setup_file) serves, on
# its private driver-staging HTTP server:
#   src.tar, commit.txt          `git archive HEAD` of the tested commit
#   gvisor.tar.zstd              the pinned runsc tarball from ~/.cache/qdistro/runsc
#   tier3s-headless-smoke.oci.tar, image-manifest.txt
#                                the workload image built once in a VM with
#                                registry access (tier3s/cache-image-archive.sh)
#
#   tier3s-guest-setup.sh <base-url> [--expect-fresh] [--gui <workloads>]
#
# --expect-fresh (every qci worker): the image was built WITHOUT
# QDISTRO_TIER3S=1, so no tier 3s file may exist before this script installs
# one (owner O10), and the installer run without the flag must leave it so.
#
# --gui <workloads> (Phase B, s123-s129): a comma list of GUI workload names
# (e.g. weston-terminal,foot). Each tier3s-<workload>.oci.tar is fetched from
# the staging server, its sha256 checked against the manifest's
# IMAGE_ARCHIVE_SHA256_<WORKLOAD>, loaded into admin's store, and its image ID
# asserted against IMAGE_ID_<WORKLOAD>. The admin qdwin session must already
# be up (tier3s.bash calls start_user_session first): the compositor socket,
# qdshell, and the bridge/attestation tools are asserted here.
#
# Order: stage the tested commit root-owned -> installer without the flag
# (nothing tier 3s) -> installer with QDISTRO_TIER3S=1 (every artifact equals
# the tested commit) -> runsc offline provision (sha512 against the pin) ->
# installed probe PASS -> load the image archive (sha256, input key, image ID
# against its manifest, snapshot pin) -> broker allow rule. One PASS/FAIL line
# per check; exits 1 on any failure.
set -u
U=${1:?usage: tier3s-guest-setup.sh <base-url> [--expect-fresh] [--gui <workloads>]}
FRESH=""
GUI_WL=""
shift
while [ $# -gt 0 ]; do
    case "$1" in
        --expect-fresh) FRESH=--expect-fresh ;;
        --gui) shift; GUI_WL="${1:?--gui needs a comma-separated workload list}" ;;
        *) echo "tier3s-guest-setup.sh: unknown argument '$1'" >&2; exit 2 ;;
    esac
    shift
done
T3S_TAG=t3s-setup
. "$(dirname "$0")/tier3s-guest-lib.sh"
SRC=/root/qdistro-src-t3s
T3S_PATHS="/usr/lib/qdistro/tier3s /usr/libexec/qdistro/qdistro-tier3s-scope
/usr/libexec/qdistro/qdistro-tier3s-cleanup /usr/libexec/qdistro/qdistro-tier3s-silo-launch
/usr/lib/tmpfiles.d/qdistro-tier3s.conf /etc/systemd/system/qdistro-tier3s-silo@.service
/run/qdistro-tier3s-runsc /run/qdistro-tier3s-ctl /run/qdistro-tier3s
/run/qdistro/tier3s-launch"
present_t3s() { local p; for p in $T3S_PATHS; do [ -e "$p" ] && echo "$p"; done; }

step "0. guest"
info "profile: $(cat /etc/qdistro/profile 2>/dev/null | tr '\n' ' ')"
info "kernel $(uname -r); $(podman --version); $(systemctl --version | head -1); selinux $(getenforce 2>/dev/null)"
if [ "$FRESH" = --expect-fresh ]; then
    left=$(present_t3s | tr '\n' ' ')
    if [ -z "$left" ]; then pass "o10: worker image (built without QDISTRO_TIER3S=1) carries no tier 3s file"
    else fail "o10: worker image already carries tier 3s files: $left"; fi
fi

step "1. stage the tested commit, root-owned"
rm -rf "$SRC"; mkdir -p "$SRC"
if curl -fsS "$U/src.tar" | tar -C "$SRC" -xf -; then pass "staged src.tar"
else fail "staged src.tar: download or extract failed"; finish; fi
chown -R root:root "$SRC"; chmod 0755 "$SRC"
want_commit=$(curl -fsS "$U/commit.txt")
info "tested commit $want_commit"
is "staged tree has the tier 3s sources" "$(yes_no test -f "$SRC/tier3s/spawn-tier3s.sh")" yes

step "2. installer WITHOUT QDISTRO_TIER3S (owner O10: installs nothing tier 3s)"
if [ "$FRESH" = --expect-fresh ]; then
    out=$(env -u QDISTRO_TIER3S bash "$SRC/scripts/install/install-session-manager.sh" "$SRC/session_manager" 2>&1); rc=$?
    printf '%s\n' "$out" | grep -i 'tier 3s' | sed 's/^/    /'
    is "o10: installer without the flag rc" "$rc" 0
    is "o10: installer without the flag says so" "$(printf '%s\n' "$out" | grep -c 'tier 3s not installed (QDISTRO_TIER3S is not 1')" 1
    left=$(present_t3s | tr '\n' ' ')
    if [ -z "$left" ]; then pass "o10: no tier 3s file after the installer ran without the flag"
    else fail "o10: installer without the flag installed: $left"; fi
else
    info "not a fresh worker: the O10 negative is not re-checked here"
fi

step "3. installer WITH QDISTRO_TIER3S=1 from the tested commit"
out=$(QDISTRO_TIER3S=1 bash "$SRC/scripts/install/install-session-manager.sh" "$SRC/session_manager" 2>&1); rc=$?
printf '%s\n' "$out" | tail -3 | sed 's/^/    /'
is "installer with QDISTRO_TIER3S=1 rc" "$rc" 0
chk_file() {   # chk_file <tree path> <installed path> <mode>
    is "installed $2 = tested commit" "$(sha256sum < "$2" 2>/dev/null | cut -d' ' -f1)" "$(sha256sum < "$SRC/$1" | cut -d' ' -f1)"
    is "installed $2 owner/mode" "$(stat -c '%U:%G %a' "$2" 2>/dev/null)" "root:root $3"
}
for f in spawn-tier3s.sh probe.sh tier3s-runsc; do chk_file "tier3s/$f" "/usr/lib/qdistro/tier3s/$f" 755; done
chk_file tier3s/RUNSC_RELEASE /usr/lib/qdistro/tier3s/RUNSC_RELEASE 644
for f in "$SRC"/tier3s/seccomp/*.json; do chk_file "tier3s/seccomp/${f##*/}" "/usr/lib/qdistro/tier3s/seccomp/${f##*/}" 644; done
chk_file tier3s/qdistro-tier3s-scope /usr/libexec/qdistro/qdistro-tier3s-scope 755
chk_file tier3s/qdistro-tier3s-cleanup /usr/libexec/qdistro/qdistro-tier3s-cleanup 755
chk_file tier3s/tmpfiles/qdistro-tier3s.conf /usr/lib/tmpfiles.d/qdistro-tier3s.conf 644
chk_file 'session_manager/qdistro-tier3s-silo@.service' '/etc/systemd/system/qdistro-tier3s-silo@.service' 644
chk_file session_manager/qdistro-tier3s-silo-launch /usr/libexec/qdistro/qdistro-tier3s-silo-launch 755
chk_file session_manager/qdistro_session_manager.py /usr/libexec/qdistro/qdistro_session_manager.py 755
is "installed broker = tested commit" "$(sha256sum < /usr/libexec/qdistro/qdistro_admin_broker.py | cut -d' ' -f1)" \
    "$(sha256sum < "$SRC/broker/qdistro_admin_broker.py" | cut -d' ' -f1)"
is "tmpfiles: runsc state-root base" "$(stat -c '%U:%G %a' "$RUNSC_BASE" 2>/dev/null)" "root:root 755"
is "tmpfiles: per-silo runtime base" "$(stat -c '%U:%G %a' "$RT_BASE" 2>/dev/null)" "root:root 755"
is "tmpfiles: control dir" "$(stat -c '%U:%G %a' "$CTL" 2>/dev/null)" "root:root 700"
is "tmpfiles: per-launch parent" "$(stat -c '%U:%G %a' "$LAUNCHES" 2>/dev/null)" "root:root 755"
is "tmpfiles: tier3s stanza dir" "$(stat -c '%U:%G %a' "$STANZA_DIR" 2>/dev/null)" "root:root 700"
[ "$FRESH" != --expect-fresh ] || is "runsc not installed by the installer" "$(yes_no test -e /usr/libexec/qdistro/runsc)" no
systemctl daemon-reload
is "unit loaded from /etc" "$(systemctl show -p FragmentPath --value qdistro-tier3s-silo@setup.service)" /etc/systemd/system/qdistro-tier3s-silo@.service
is "unit StopPropagatedFrom (O11)" "$(systemctl show -p StopPropagatedFrom --value qdistro-tier3s-silo@setup.service)" qdistro-session-manager.service
# the running manager must be the installed code
systemctl restart qdistro-session-manager.service
wait_for 30 manager_up
pid=$(systemctl show -p MainPID --value qdistro-session-manager.service)
# the bus name is owned before the object serves — under load a single
# introspect can outrun the manager's init (b28 s126: name listed,
# introspect timed out). Wait for the verb, not just the name.
serves_tier3s() {
    busctl introspect org.qdistro.SessionManager1 /org/qdistro/SessionManager1 2>/dev/null \
        | grep -q '^\.CreateTier3sSilo '
}
wait_for 45 serves_tier3s || :
if [ "${pid:-0}" -gt 0 ] && [ "$(stat -c %Y "/proc/$pid")" -ge "$(stat -c %Y /usr/libexec/qdistro/qdistro_session_manager.py)" ] \
   && serves_tier3s; then
    pass "session manager runs the installed code (pid $pid, serves CreateTier3sSilo)"
else fail "session manager is not running the installed code (pid ${pid:-?})"; fi
is "broker has the rules-only tier3s prefix" "$(grep -c '"qdistro.tier3s.spawn:",' /usr/libexec/qdistro/qdistro_admin_broker.py)" 1

step "3c. SELinux: install the tier3s module from the tested commit"
# The confined domain is part of the tier3s stack (Phase D): the s12x
# lanes must exercise it under whatever mode the VM runs. Built in-tree
# (checkmodule is in the image; make is not).
if command -v checkmodule >/dev/null 2>&1; then
    out=$(cd "$SRC/selinux/tier3s" && bash install-policy.sh 2>&1); rc=$?
    printf '%s\n' "$out" | tail -3 | sed 's/^/    /'
    is "install-policy.sh rc" "$rc" 0
    is "module loaded" "$(semodule -l | grep -c '^qdistro_tier3s\b')" 1
    # The SaveRule lanes need the broker rules.d write surface; on the
    # runtime-only bases (no selinux-policy-devel) the baked broker
    # module cannot be rebuilt, so the companion raw module carries it.
    out=$(cd "$SRC/selinux/broker-rules" && bash install-policy.sh 2>&1); rc=$?
    printf '%s\n' "$out" | tail -3 | sed 's/^/    /'
    is "broker-rules install-policy.sh rc" "$rc" 0
    is "broker-rules module loaded" "$(semodule -l | grep -c '^qdistro_broker_rules\b')" 1
else
    fail "checkmodule absent — the tier3s policy module cannot be built (bake regression)"
fi

step "4. runsc: offline provision from the staged, pin-checked tarball"
rel=$(sed -n 's/^release=//p' "$SRC/tier3s/RUNSC_RELEASE")
want=$(sed -n 's/^tarball_sha512=//p' "$SRC/tier3s/RUNSC_RELEASE")
mkdir -p "/var/cache/qdistro/runsc/$rel"
curl -fsS "$U/gvisor.tar.zstd" -o "/var/cache/qdistro/runsc/$rel/gvisor.tar.zstd"
is "runsc tarball sha512 = pin ($rel)" "$(sha512sum < "/var/cache/qdistro/runsc/$rel/gvisor.tar.zstd" | cut -d' ' -f1)" "$want"
out=$(cd "$SRC" && tier3s/provision-runsc.sh --offline --cache-dir /var/cache/qdistro/runsc 2>&1); rc=$?
printf '%s\n' "$out" | tail -3 | sed 's/^/    /'
is "provision-runsc.sh --offline rc" "$rc" 0
# The exec transition into qdistro_tier3s_t keys on this label — module
# loaded but binaries unlabelled would run every launch unconfined while
# looking identical to a confined one (astra P1).
is "runsc ELF carries qdistro_tier3s_exec_t" \
    "$(stat -c %C /usr/libexec/qdistro/runsc/runsc | grep -c ':qdistro_tier3s_exec_t:')" 1
is "all gvisor-bin sidecars carry qdistro_tier3s_exec_t" \
    "$(stat -c %C /usr/libexec/qdistro/runsc/gvisor-bin/* | grep -c ':qdistro_tier3s_exec_t:')" \
    "$(find /usr/libexec/qdistro/runsc/gvisor-bin -type f | wc -l)"
out=$(/usr/lib/qdistro/tier3s/probe.sh --user admin 2>&1); rc=$?
printf '%s\n' "$out" | sed 's/^/    /'
is "installed probe rc" "$rc" 0
is "installed probe RESULT" "$(printf '%s\n' "$out" | grep -c '^RESULT PASS: tier 3s prerequisites present')" 1
is "probe state_root" "$(printf '%s\n' "$out" | grep -c '^PASS state_root')" 1

step "5. workload image from the OCI archive (built once with registry access)"
d=/var/tmp/t3s-img; rm -rf "$d"; install -d -m 0755 "$d"
if curl -fsS "$U/image-manifest.txt" -o "$d/manifest.txt" && curl -fsS "$U/tier3s-headless-smoke.oci.tar" -o "$d/image.oci.tar"; then
    pass "image archive and manifest staged"
else
    fail "image archive not served (host: tier3s/cache-image-archive.sh <dev-vm> builds it once)"; finish
fi
chmod 0644 "$d"/*
sed 's/^/    /' "$d/manifest.txt"
m() { sed -n "s/^$1=//p" "$d/manifest.txt"; }
is "archive sha256 = manifest" "$(sha256sum < "$d/image.oci.tar" | cut -d' ' -f1)" "$(m IMAGE_ARCHIVE_SHA256)"
pin=$(sed -n 's/^snapshot=\([0-9]\{8\}\)$/\1/p' "$SRC/snapshot.conf" | head -1)
is "manifest snapshot = snapshot.conf pin" "$(m IMAGE_SNAPSHOT)" "$pin"
key=$(cd "$SRC" && bash tier3s/cache-image-archive.sh --key)
is "manifest input key = the tested commit's image inputs" "$(m INPUT_KEY)" "$key"
pm rmi -f "$IMAGE" > /dev/null 2>&1
out=$(pm load -i "$d/image.oci.tar" 2>&1); rc=$?
printf '%s\n' "$out" | tail -2 | sed 's/^/    /'
is "podman load (admin) rc" "$rc" 0
got_id=$(pm image inspect --format '{{.Id}}' "$IMAGE" 2>/dev/null)
is "loaded image ID = manifest IMAGE_ID" "$got_id" "$(m IMAGE_ID)"
is "image snapshot label = pin" "$(pm image inspect --format '{{index .Labels "org.qdistro.snapshot"}}' "$IMAGE" 2>/dev/null)" "$pin"
echo "IMAGE_ID=$got_id"
echo "IMAGE_DIGEST=$(pm image inspect --format '{{.Digest}}' "$IMAGE" 2>/dev/null)"
info "the manifest digest changes across the oci-archive round trip (manifest re-serialized; build VM: $(m IMAGE_DIGEST)); the asserted identity is the image ID (config digest)"

if [ -n "$GUI_WL" ]; then
    step "5b. Phase B: per-workload GUI image archives ($GUI_WL)"
    for w in ${GUI_WL//,/ }; do
        W=$(printf '%s' "$w" | tr 'a-z-' 'A-Z_')
        arch="tier3s-$w.oci.tar"
        if ! curl -fsS "$U/$arch" -o "$d/$arch"; then
            fail "GUI archive $arch not served (host: tier3s/cache-image-archive.sh <dev-vm> builds it)"; finish
        fi
        chmod 0644 "$d/$arch"
        is "$w archive sha256 = manifest" "$(sha256sum < "$d/$arch" | cut -d' ' -f1)" "$(m "IMAGE_ARCHIVE_SHA256_$W")"
        pm rmi -f "localhost/qdistro/tier3s-$w:latest" > /dev/null 2>&1
        out=$(pm load -i "$d/$arch" 2>&1); rc=$?
        printf '%s\n' "$out" | tail -2 | sed 's/^/    /'
        is "podman load $w (admin) rc" "$rc" 0
        is "loaded $w image ID = manifest" \
            "$(pm image inspect --format '{{.Id}}' "localhost/qdistro/tier3s-$w:latest" 2>/dev/null)" \
            "$(m "IMAGE_ID_$W")"
        is "$w image snapshot label = pin" \
            "$(pm image inspect --format '{{index .Labels "org.qdistro.snapshot"}}' "localhost/qdistro/tier3s-$w:latest" 2>/dev/null)" "$pin"
        is "$w image workload label" \
            "$(pm image inspect --format '{{index .Labels "org.qdistro.tier3s.workload"}}' "localhost/qdistro/tier3s-$w:latest" 2>/dev/null)" "$w"
        is "$w workload declaration installed (GUI=1)" \
            "$(sed -n 's/^GUI=//p' "/usr/lib/qdistro/tier3s/workloads/$w.env" 2>/dev/null)" "1"
    done
    # the waypipe bridge needs a live admin compositor and qdshell; the host
    # helper (tier3s.bash) started qdwin-session — assert it here so a broken
    # session never reads as an untestable driver precondition.
    is "admin compositor socket present" "$(yes_no test -S /run/user/1000/wayland-1)" yes
    is "qdshell is up" "$(runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user is-active qdshell.service 2>/dev/null)" active
    for t in runuser waypipe qdistro-secctx-exec dbus-send sqlite3 python3; do
        is "tool $t installed" "$(command -v "$t" >/dev/null && echo yes || echo no)" yes
    done
    for t in qs ydotool; do
        # as_admin execs through env(1), which cannot run the `command`
        # builtin — probe through a shell whose stdout stays silent.
        is "admin tool $t installed" "$(yes_no as_admin sh -c "command -v $t >/dev/null")" yes
    done
    is "clipboard-source helper installed" \
        "$(command -v qdistro-test-clipboard-source 2>/dev/null)" "/usr/bin/qdistro-test-clipboard-source"
    is "pywayland importable as admin" \
        "$(yes_no as_admin python3 -c 'import pywayland.client' 2>/dev/null)" yes
fi

step "6. broker allow rule for the smoke spawn"
is "broker without a rule" "$(set_rule none; broker_check "$ACTION")" unknown
set_rule allow
is "broker with the allow rule" "$(broker_check "$ACTION")" allow

# Model A canary (Phase C2): the workload runs as qt3s-<silo> out of the
# silo's own podman store — exercise the whole chain once here (account
# provisioning, per-uid dirs, per-silo image, silo-account probe, live
# launch, teardown) so a broken substrate fails setup, not a driver's first
# launch.
step "6b. Model A canary: silo account + store + probe + one live launch"
sm CreateTier3sSilo ssss t3setup headless-smoke t3setup none > /dev/null
is "canary: silo created" "$(silo_state t3setup)" Created
if ensure_silo_image t3setup headless-smoke; then
    pass "canary: qt3s-t3setup provisioned; $IMAGE in its own store"
else fail "canary: ensure_silo_image t3setup failed"; fi
SU=$(silo_uid t3setup); SG=$(silo_gid t3setup)
is "canary: silo uid:gid resolved and not admin's" "$(yes_no test -n "$SU" -a "$SU" != 1000)" yes
is "canary: per-uid runsc root + runtime dir (silo-owned 0700)" \
    "$(stat -c '%u %a' "$RUNSC_BASE/$SU" "$RT_BASE/$SU" 2>/dev/null | paste -sd' ' -)" "$SU 700 $SU 700"
out=$(/usr/lib/qdistro/tier3s/probe.sh --user "qt3s-t3setup" 2>&1); rc=$?
printf '%s\n' "$out" | grep -v '^PASS' | sed 's/^/    /'
is "canary: probe as the silo account rc" "$rc" 0
is "canary: admin still holds the staged archive copy" "$(yes_no pm image exists "$IMAGE")" yes
is "canary: silo store holds the workload image" "$(yes_no pm_s t3setup image exists "$IMAGE")" yes
set_argv t3setup=120 | sed 's/^/    /'
tok=$(up_silo t3setup)
if [ -n "$tok" ]; then pass "canary: launch $tok recorded running"; else fail "canary: launch did not come up"; finish; fi
# Live confinement proof: with the sandbox up, its sentry/gofer must be
# running inside qdistro_tier3s_t — module loaded + labels applied says
# only that the transition CAN engage, not that it DID (astra P1).
is "canary: sandbox processes confined to qdistro_tier3s_t" \
    "$(yes_no test "$(ps -eZ | grep -c ':qdistro_tier3s_t:')" -gt 0)" yes
is "canary: container runs in the silo store, not admin's" \
    "$(pm_s t3setup container exists "$(ctr_of t3setup)"; echo $?):$(pm container exists "$(ctr_of t3setup)" 2>/dev/null; echo $?)" "0:1"
sm StopSilo si t3setup 10 > /dev/null; is "canary: StopSilo" "$(silo_state t3setup)" Stopped
assert_launch_gone canary "$tok" t3setup
sm DeleteSilo s t3setup > /dev/null; is "canary: DeleteSilo" "$(silo_state t3setup)" absent
assert_all_clear setup
finish
