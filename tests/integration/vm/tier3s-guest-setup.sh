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
#   tier3s-guest-setup.sh <base-url> [--expect-fresh]
#
# --expect-fresh (every qci worker): the image was built WITHOUT
# QDISTRO_TIER3S=1, so no tier 3s file may exist before this script installs
# one (owner O10), and the installer run without the flag must leave it so.
#
# Order: stage the tested commit root-owned -> installer without the flag
# (nothing tier 3s) -> installer with QDISTRO_TIER3S=1 (every artifact equals
# the tested commit) -> runsc offline provision (sha512 against the pin) ->
# installed probe PASS -> load the image archive (sha256, input key, image ID
# against its manifest, snapshot pin) -> broker allow rule. One PASS/FAIL line
# per check; exits 1 on any failure.
set -u
U=${1:?usage: tier3s-guest-setup.sh <base-url> [--expect-fresh]}
FRESH=${2:-}
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
is "tmpfiles: state root" "$(stat -c '%u:%g %a' "$SROOT" 2>/dev/null)" "1000:1000 700"
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
if [ "${pid:-0}" -gt 0 ] && [ "$(stat -c %Y "/proc/$pid")" -ge "$(stat -c %Y /usr/libexec/qdistro/qdistro_session_manager.py)" ] \
   && busctl introspect org.qdistro.SessionManager1 /org/qdistro/SessionManager1 | grep -q '^\.CreateTier3sSilo '; then
    pass "session manager runs the installed code (pid $pid, serves CreateTier3sSilo)"
else fail "session manager is not running the installed code (pid ${pid:-?})"; fi
is "broker has the rules-only tier3s prefix" "$(grep -c '"qdistro.tier3s.spawn:",' /usr/libexec/qdistro/qdistro_admin_broker.py)" 1

step "4. runsc: offline provision from the staged, pin-checked tarball"
rel=$(sed -n 's/^release=//p' "$SRC/tier3s/RUNSC_RELEASE")
want=$(sed -n 's/^tarball_sha512=//p' "$SRC/tier3s/RUNSC_RELEASE")
mkdir -p "/var/cache/qdistro/runsc/$rel"
curl -fsS "$U/gvisor.tar.zstd" -o "/var/cache/qdistro/runsc/$rel/gvisor.tar.zstd"
is "runsc tarball sha512 = pin ($rel)" "$(sha512sum < "/var/cache/qdistro/runsc/$rel/gvisor.tar.zstd" | cut -d' ' -f1)" "$want"
out=$(cd "$SRC" && tier3s/provision-runsc.sh --offline --cache-dir /var/cache/qdistro/runsc 2>&1); rc=$?
printf '%s\n' "$out" | tail -3 | sed 's/^/    /'
is "provision-runsc.sh --offline rc" "$rc" 0
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

step "6. broker allow rule for the smoke spawn"
is "broker without a rule" "$(set_rule none; broker_check "$ACTION")" unknown
set_rule allow
is "broker with the allow rule" "$(broker_check "$ACTION")" allow
assert_all_clear setup
finish
