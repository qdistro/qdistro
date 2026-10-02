#!/bin/bash
# tier3s/spike/run-phase0-fix.sh — HOST driver: Phase 0 evidence re-run for the
# astra full-review fixes (probe integrity-before-exec, provisioning lock,
# guard tests). Every step runs INSIDE the VM through vmlog.sh (vm-exec); the
# host only serves `git archive HEAD` + the sha512-checked runsc tarball on
# 127.0.0.1 (the guest's 10.0.2.2) and stops that server on exit. Each guest
# step is asserting (phase0-fix-lib.sh): its exit status is its BAD count.
#
#   run-phase0-fix.sh <vm> [<logdir>]     (refuses a non-empty logdir)
set -u
vm=${1:?usage: run-phase0-fix.sh <vm> [<logdir>]}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
out=${2:-$here/logs/phase0-fix-$(date +%Y%m%d)}
port=${T3S_PORT:-42931}
if [ -d "$out" ] && [ -n "$(ls -A "$out")" ]; then echo "refusing: $out is not empty" >&2; exit 2; fi
mkdir -p "$out"
pin=$repo/tier3s/RUNSC_RELEASE
rel=$(sed -n 's/^release=//p' "$pin")
want=$(sed -n 's/^tarball_sha512=//p' "$pin")
tarball=$HOME/.cache/qdistro/runsc/$rel/gvisor.tar.zstd
[ "$(sha512sum < "$tarball" | cut -d' ' -f1)" = "$want" ] || { echo "host tarball sha512 != pin" >&2; exit 2; }
serve=$(mktemp -d /var/tmp/t3s-serve.XXXXXX)
git -C "$repo" archive --format=tar HEAD > "$serve/src.tar"
git -C "$repo" rev-parse HEAD > "$serve/commit.txt"
cp "$tarball" "$serve/gvisor.tar.zstd"
python3 -m http.server --bind 127.0.0.1 "$port" --directory "$serve" > /dev/null 2>&1 &
srv=$!
trap 'kill "$srv" 2>/dev/null; wait "$srv" 2>/dev/null; rm -rf "$serve"; echo "host file server stopped"' EXIT
sleep 1
fails=0
step() {   # step <log name> <guest command>
    local rc
    VMLOG_TIMEOUT=${VMLOG_TIMEOUT:-1200} VMLOG_TAIL=0 "$here/vmlog.sh" "$out/$1.log" "$vm" "$2" > /dev/null
    rc=$?
    printf '%-48s rc=%s %s\n' "$1" "$rc" "$(grep -c ': BAD' "$out/$1.log") BAD"
    [ "$rc" -eq 0 ] || fails=$((fails + 1))
}
L='. /root/qdistro-src/tier3s/spike/phase0-fix-lib.sh'
U=http://10.0.2.2:$port

{ echo "### host: staged commit $(cat "$serve/commit.txt"); host sha256 of the staged files:"
  for f in tier3s/RUNSC_RELEASE tier3s/probe.sh tier3s/provision-runsc.sh tier3s/tier3s-runsc \
           tier3s/spike/phase0-fix-lib.sh tier3s/spike/mutate-guards.py \
           tests/unit/test_tier3s_probe.py tests/unit/test_tier3s_provision.py; do
      printf '%s  %s\n' "$(git -C "$repo" show "HEAD:$f" | sha256sum | cut -d' ' -f1)" "$f"; done
} > "$out/00-stage.log"
step 00-stage "set -e; cd /root/qdistro-src && curl -fsS $U/src.tar | tar -xf - && echo \"staged commit \$(curl -fsS $U/commit.txt)\"
set +e; $L
sha256sum tier3s/RUNSC_RELEASE tier3s/probe.sh tier3s/provision-runsc.sh tier3s/tier3s-runsc tier3s/spike/phase0-fix-lib.sh tier3s/spike/mutate-guards.py tests/unit/test_tier3s_probe.py tests/unit/test_tier3s_provision.py
mkdir -p \$CACHE/$rel && curl -fsS $U/gvisor.tar.zstd -o \$CACHE/$rel/gvisor.tar.zstd
is cache-tarball-sha512 \"\$(sha512sum < \$CACHE/$rel/gvisor.tar.zstd | cut -d' ' -f1)\" \"\$(sed -n 's/^tarball_sha512=//p' tier3s/RUNSC_RELEASE)\"
is profile \"\$(cat /etc/qdistro/profile)\" QDISTRO_PROFILE=dev
uname -r; podman --version; getenforce
zypper -n in --no-recommends python313-pytest strace > /var/tmp/t3s-zypper.log 2>&1; expect_rc zypper-pytest-strace 0 \$?; tail -3 /var/tmp/t3s-zypper.log
python3 -m pytest --version; strace -V | head -1
echo \"prior install before reset: \$(ls -d /usr/libexec/qdistro/runsc /usr/libexec/qdistro/tier3s-runsc /etc/qdistro/runsc-release /run/qdistro-runsc 2>/dev/null | tr '\\n' ' ')\"
rm -rf /usr/libexec/qdistro/runsc /usr/libexec/qdistro/tier3s-runsc /etc/qdistro/runsc-release /run/qdistro-runsc /usr/libexec/t3s-elsewhere /var/tmp/t3s-*
is no-install-after-reset \"\$(ls -d /usr/libexec/qdistro/runsc /usr/libexec/qdistro/tier3s-runsc /etc/qdistro/runsc-release /run/qdistro-runsc 2>/dev/null | wc -l)\" 0
finish"

step 01-probe-before-provision "$L; probe_strace; expect_rc probe-exit 1 \$PRC
has first-missing-is-runsc \$PO 'RESULT FAIL: first missing prerequisite: runsc (not provisioned'
never_executed unprovisioned; finish"

step 02-provision-offline "$L; provision provision-exit 0
has took-lock \$VO 'holding the provisioning lock /run/qdistro-runsc/provision.lock'
has verified-private-copy \$VO 'verified private copy'
has installed \$VO 'PASS: installed runsc $rel to /usr/libexec/qdistro/runsc'
ls -lR /usr/libexec/qdistro/runsc /usr/libexec/qdistro/tier3s-runsc /etc/qdistro/runsc-release
stat -c '%n %a %U:%G %F' /run/qdistro-runsc /run/qdistro-runsc/provision.lock
is lock-dir-root-0700 \"\$(stat -c '%a %U:%G' /run/qdistro-runsc)\" '700 root:root'
is no-leftovers \"\$(leftovers)\" 0; finish"

step 03-provision-idempotent "$L; provision provision-exit 0
has nothing-to-do \$VO 'already installed and matching pin $rel; nothing to do'; finish"

step 04-probe-pass "$L; probe_strace; expect_rc probe-exit 0 \$PRC
has result-pass \$PO 'RESULT PASS: tier 3s prerequisites present'
has executed-verified-inode \$PO 'PASS runsc_version: runsc version release-$rel (matches pin, rc=0; executed the verified inode'
is strace-one-runsc-exec \"\$NEXEC\" 1
is exec-was-via-fd \"\$(runsc_execs /var/tmp/t3s-tr | grep -c 'execve(\"/proc/self/fd/')\" 1; finish"

step 05-negative-runsc-removed "$L; mv \$R /var/tmp/t3s-runsc.aside
probe_strace; expect_rc probe-exit 1 \$PRC
has names-runsc \$PO 'RESULT FAIL: first missing prerequisite: runsc (/usr/libexec/qdistro/runsc/runsc missing'
never_executed removed
mv /var/tmp/t3s-runsc.aside \$R; probe_tail_pass restored; finish"

step 06-negative-tampered-cache "$L; T=\$CACHE/$rel/gvisor.tar.zstd
cp \$T /var/tmp/t3s-good.tar.zstd; printf x >> \$T
echo '## (a) live install present but differing (sidecar 0644): provision must fail and leave it untouched'
chmod 0644 \$SIDE/gvisor_sentry
provision a-provision-exit 1; has a-tarball-mismatch \$VO 'tarball sha512 mismatch'
is a-live-sidecar-untouched \"\$(stat -c %a \$SIDE/gvisor_sentry)\" 644
echo '## (b) no install: provision must fail and install nothing'
mkdir -p /var/tmp/t3s-saved; mv /usr/libexec/qdistro/runsc /usr/libexec/qdistro/tier3s-runsc /etc/qdistro/runsc-release /var/tmp/t3s-saved/
provision b-provision-exit 1; has b-tarball-mismatch \$VO 'tarball sha512 mismatch'
is b-nothing-installed \"\$(ls -d /usr/libexec/qdistro/runsc /usr/libexec/qdistro/tier3s-runsc /etc/qdistro/runsc-release 2>/dev/null | wc -l)\" 0
mv /var/tmp/t3s-saved/runsc /var/tmp/t3s-saved/tier3s-runsc /usr/libexec/qdistro/; mv /var/tmp/t3s-saved/runsc-release /etc/qdistro/; rmdir /var/tmp/t3s-saved
cp /var/tmp/t3s-good.tar.zstd \$T; rm -f /var/tmp/t3s-good.tar.zstd
is cache-restored \"\$(sha512sum < \$T | cut -d' ' -f1)\" \"\$(sed -n 's/^tarball_sha512=//p' tier3s/RUNSC_RELEASE)\"
is no-leftovers \"\$(leftovers)\" 0; finish"

step 07a-negative-sidecar-mode-then-repair "$L; is sidecar-still-644 \"\$(stat -c %a \$SIDE/gvisor_sentry)\" 644
probe_strace; expect_rc probe-exit 1 \$PRC
has names-bundle-mode \$PO 'f 644 root:root gvisor-bin/gvisor_sentry'
never_executed mode
provision repair-exit 0; has repaired \$VO 'PASS: installed runsc'
is sidecar-755 \"\$(stat -c %a \$SIDE/gvisor_sentry)\" 755; probe_tail_pass repaired; finish"

step 07-negative-hardened-profile "$L; cp -a /etc/qdistro/profile /var/tmp/t3s-profile.bak
trap 'cp -a /var/tmp/t3s-profile.bak /etc/qdistro/profile' EXIT
sed -i 's/^QDISTRO_PROFILE=.*/QDISTRO_PROFILE=release/' /etc/qdistro/profile
probe_strace; expect_rc probe-exit 2 \$PRC
has refuses \$PO 'REFUSE profile: tier 3s is dev-profile only (README O4); /etc/qdistro/profile says release'
is no-runsc-exec \"\$NEXEC\" 0
cp -a /var/tmp/t3s-profile.bak /etc/qdistro/profile; is profile-restored \"\$(cat /etc/qdistro/profile)\" QDISTRO_PROFILE=dev; finish"

step 07b-negative-tampered-install-then-repair "$L; printf x >> \$SIDE/runsc-fd-parking; ln -s /bin/sh \$SIDE/extra
probe_strace; expect_rc probe-exit 1 \$PRC
has names-extra-symlink \$PO 'unexpected=[l 777 root:root gvisor-bin/extra,]'
never_executed tampered
provision repair-exit 0; has repaired \$VO 'PASS: installed runsc'
is no-leftovers \"\$(leftovers)\" 0; probe_tail_pass repaired; finish"

step 07c-negative-root-refusals "$L; cp tier3s/RUNSC_RELEASE /var/tmp/t3s-alt-pin; chmod 0644 /var/tmp/t3s-alt-pin; ls -l /var/tmp/t3s-alt-pin
if [ -r /var/tmp/t3s-alt-pin ]; then ok alt-pin-readable; else bad alt-pin-readable; fi
before=\$(sha512sum /etc/qdistro/runsc-release /usr/libexec/qdistro/tier3s-runsc \$R | sha512sum)
refused() { # refused <name> <exact stderr> <cmd...>
  \"\${@:3}\" > /var/tmp/t3s-o 2> /var/tmp/t3s-e; local rc=\$?; cat /var/tmp/t3s-o /var/tmp/t3s-e
  expect_rc \$1-exit 1 \$rc; is \$1-message \"\$(cat /var/tmp/t3s-e)\" \"\$2\"; }
refused prefix-hook 'provision-runsc: FAIL: QDISTRO_RUNSC_PREFIX is a unit-test hook and is refused for root' env QDISTRO_RUNSC_PREFIX=/var/tmp/t3s-x tier3s/provision-runsc.sh --offline
refused pin-override 'provision-runsc: FAIL: --pin is a unit-test option (needs QDISTRO_RUNSC_PREFIX); a real install uses /root/qdistro-src/tier3s/RUNSC_RELEASE' tier3s/provision-runsc.sh --pin /var/tmp/t3s-alt-pin --offline
refused fail-hook 'provision-runsc: FAIL: QDISTRO_RUNSC_FAIL_AFTER_SWAP is a unit-test hook (needs QDISTRO_RUNSC_PREFIX)' env QDISTRO_RUNSC_FAIL_AFTER_SWAP=1 tier3s/provision-runsc.sh --offline
refused pause-hook 'provision-runsc: FAIL: QDISTRO_RUNSC_PAUSE_AFTER_SWAP is a unit-test hook (needs QDISTRO_RUNSC_PREFIX)' env QDISTRO_RUNSC_PAUSE_AFTER_SWAP=/var/tmp tier3s/provision-runsc.sh --offline
env QDISTRO_PROBE_PIN=/var/tmp/t3s-alt-pin tier3s/probe.sh > /var/tmp/t3s-o 2> /var/tmp/t3s-e; rc=\$?; cat /var/tmp/t3s-e
expect_rc probe-pin-hook-refused 2 \$rc; is probe-pin-hook-message \"\$(cat /var/tmp/t3s-e)\" 'probe: QDISTRO_PROBE_PIN is a unit-test hook and needs QDISTRO_PROBE_ROOT'
is install-unchanged \"\$(sha512sum /etc/qdistro/runsc-release /usr/libexec/qdistro/tier3s-runsc \$R | sha512sum)\" \"\$before\"
is no-test-prefix-created \"\$(ls -d /var/tmp/t3s-x 2>/dev/null | wc -l)\" 0; finish"

step 11-negative-byte-only-sidecar-then-repair "$L; f=\$SIDE/runsc-fd-parking
s0=\$(stat -c '%s %a %U:%G %i' \$f); h0=\$(sha512sum < \$f)
b=\$(dd if=\$f bs=1 skip=200 count=1 2>/dev/null); if [ \"\$b\" = Z ]; then n=Y; else n=Z; fi
printf %s \"\$n\" | dd of=\$f bs=1 seek=200 conv=notrunc 2>/dev/null
s1=\$(stat -c '%s %a %U:%G %i' \$f); h1=\$(sha512sum < \$f)
echo \"before: \$s0  after: \$s1\"
is same-size-mode-owner-inode \"\$s1\" \"\$s0\"
if [ \"\$h1\" != \"\$h0\" ]; then ok bytes-changed; else bad bytes-changed; fi
probe_strace; expect_rc probe-exit 1 \$PRC
has hash-loop-names-sidecar \$PO 'FAIL bundle: sha512:gvisor-bin/runsc-fd-parking'
hasnt not-a-file-set-failure \$PO 'file set differs'
never_executed byte-only
provision repair-exit 0; has repaired \$VO 'PASS: installed runsc'; probe_tail_pass repaired; finish"

step 12-negative-replaced-runsc-marker "$L; M=/var/tmp/t3s-MARKER; rm -f \$M
printf '#!/bin/sh\ntouch /var/tmp/t3s-MARKER\necho \"runsc version release-$rel\"\n' > /var/tmp/t3s-fake-runsc
install -m 0755 -o root -g root /var/tmp/t3s-fake-runsc \$R; ls -l \$R
if cmp -s /etc/qdistro/runsc-release tier3s/RUNSC_RELEASE; then ok stamp-intact; else bad stamp-intact; fi
probe_strace; expect_rc probe-exit 1 \$PRC
has hash-mismatch \$PO 'FAIL bundle: sha512:runsc'
never_executed replaced
if [ -e \$M ]; then bad marker-absent 'the probe executed the fake'; else ok marker-absent; fi
echo '## positive control: the fake does write the marker when executed directly'
\$R; if [ -e \$M ]; then ok control-marker-written; else bad control-marker-written; fi; rm -f \$M
provision repair-exit 0; has repaired \$VO 'PASS: installed runsc'; probe_tail_pass repaired; finish"

step 13-negative-foreign-owner-runsc "$L; chown admin \$R; ls -l \$R
probe_strace; expect_rc probe-exit 1 \$PRC
has names-owner \$PO 'f 755 admin:root runsc'
never_executed foreign-owner
provision repair-exit 0; has repaired \$VO 'PASS: installed runsc'
is owner-root \"\$(stat -c %U:%G \$R)\" root:root; probe_tail_pass repaired; finish"

step 14-negative-symlinked-runsc-dir "$L; mkdir -m 0755 /usr/libexec/t3s-elsewhere
mv /usr/libexec/qdistro/runsc /usr/libexec/t3s-elsewhere/runsc; ln -s /usr/libexec/t3s-elsewhere/runsc /usr/libexec/qdistro/runsc
ls -l /usr/libexec/qdistro/
probe_strace; expect_rc probe-exit 1 \$PRC
has names-symlink \$PO 'FAIL install_path: untrusted: /usr/libexec/qdistro/runsc is a symlink'
never_executed symlinked-dir
rm /usr/libexec/qdistro/runsc; mv /usr/libexec/t3s-elsewhere/runsc /usr/libexec/qdistro/runsc; rmdir /usr/libexec/t3s-elsewhere
probe_tail_pass restored; finish"

step 15-negative-writable-ancestor "$L; chmod 0775 /usr/libexec/qdistro
probe_strace; expect_rc probe-exit 1 \$PRC
has names-ancestor \$PO 'FAIL install_path: untrusted: /usr/libexec/qdistro is group/other-writable (mode 775)'
has bundle-itself-fine \$PO 'PASS bundle:'
never_executed writable-ancestor
provision provision-refuses 1; has provision-names-ancestor \$VO 'untrusted path: /usr/libexec/qdistro is group/other-writable (mode 775)'
chmod 0755 /usr/libexec/qdistro; probe_tail_pass restored; finish"

step 16-concurrent-provisions "$L; chmod 0700 \$SIDE/gvisor_sentry; rm -f /var/tmp/t3s-A.log /var/tmp/t3s-B.log
( \"\${PROVISION[@]}\" > /var/tmp/t3s-A.log 2>&1; echo \"rc=\$?\" >> /var/tmp/t3s-A.log ) & a=\$!
for i in \$(seq 1 200); do grep -q 'holding the provisioning lock' /var/tmp/t3s-A.log && break; sleep 0.05; done
( \"\${PROVISION[@]}\" > /var/tmp/t3s-B.log 2>&1; echo \"rc=\$?\" >> /var/tmp/t3s-B.log ) & b=\$!
wait \$a; wait \$b
echo '## A'; cat /var/tmp/t3s-A.log; echo '## B'; cat /var/tmp/t3s-B.log
has A-installed /var/tmp/t3s-A.log 'PASS: installed runsc'; has A-rc0 /var/tmp/t3s-A.log 'rc=0'
has B-waited /var/tmp/t3s-B.log 'waiting for the provisioning lock /run/qdistro-runsc/provision.lock'
has B-then-saw-installed /var/tmp/t3s-B.log 'already installed and matching pin $rel; nothing to do'
has B-rc0 /var/tmp/t3s-B.log 'rc=0'
is sidecar-755 \"\$(stat -c %a \$SIDE/gvisor_sentry)\" 755
is no-leftovers \"\$(leftovers)\" 0; probe_tail_pass after; finish"

step 08-probe-pass-after-negatives "$L; provision idempotent-exit 0; has nothing-to-do \$VO 'already installed and matching pin $rel; nothing to do'
probe_strace; expect_rc probe-exit 0 \$PRC; has result-pass \$PO 'RESULT PASS: tier 3s prerequisites present'
is strace-one-runsc-exec \"\$NEXEC\" 1; is no-leftovers \"\$(leftovers)\" 0; finish"

step 09-unit-tests "$L
echo '## as root (root-only guards run for real; prefix-hook tests skip by design)'
python3 -m pytest -p no:cacheprovider -v -rs tests/unit/test_tier3s_probe.py tests/unit/test_tier3s_provision.py; expect_rc pytest-root 0 \$?
rm -rf /var/tmp/t3s-unit; mkdir -p /var/tmp/t3s-unit/tests/unit
cp -a tier3s /var/tmp/t3s-unit/; cp tests/unit/test_tier3s_*.py /var/tmp/t3s-unit/tests/unit/; chown -R admin: /var/tmp/t3s-unit
echo '## as admin (prefix-hook tests; root guards via an unprivileged user namespace)'
cd /var/tmp/t3s-unit && runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin python3 -m pytest -p no:cacheprovider -v -rs tests/unit/test_tier3s_probe.py tests/unit/test_tier3s_provision.py
expect_rc pytest-admin 0 \$?; finish"

step 10-mutation-harness "$L
echo '## as admin: all mutations (copy in /var/tmp/t3s-unit)'
( cd /var/tmp/t3s-unit && runuser -u admin -- env -i PATH=/usr/bin:/bin HOME=/home/admin python3 tier3s/spike/mutate-guards.py ); expect_rc harness-admin 0 \$?
echo '## as root: the root guards reached with real euid 0'
python3 tier3s/spike/mutate-guards.py --only V2,V3,V4; expect_rc harness-root 0 \$?
sha256sum tier3s/probe.sh tier3s/provision-runsc.sh tier3s/tier3s-runsc
rm -rf /var/tmp/t3s-unit; finish"

echo "steps with a nonzero exit: $fails"
exit "$fails"
