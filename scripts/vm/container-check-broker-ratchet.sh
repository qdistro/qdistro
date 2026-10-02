#!/usr/bin/env bash
# Run inside the disposable rootless native builder, after snapshot RPM setup.
# semodule -n checks the policy store without trying to load the host kernel.
set -euo pipefail

src=${1:-/src/selinux}
devel=/usr/share/selinux/devel/Makefile
conf=/etc/selinux/semanage.conf
test -f "$devel"
test -f "$conf"
test -f "$src/pwd/qdistro_pwd.pp"
test -f "$src/broker/qdistro_broker.te"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# The broker refers to qdistro_pwd_audit_t. Register the freshly built pwd
# module first, with assertion checking off; Tumbleweed's base policy has
# unrelated neverallow violations when expand-check is enabled.
if grep -q '^expand-check=' "$conf"; then
    sed -i 's/^expand-check=.*/expand-check=0/' "$conf"
else
    printf 'expand-check=0\n' >> "$conf"
fi
semodule -n -i "$src/pwd/qdistro_pwd.pp"
sed -i 's/^expand-check=.*/expand-check=1/' "$conf"
grep -qx 'expand-check=1' "$conf"

for variant in bad clean; do
    mkdir -p "$work/$variant"
    cp "$src"/broker/qdistro_broker.{te,if,fc} "$work/$variant/"
done
printf '\nallow qdistro_broker_t self:rawip_socket create;\n' \
    >> "$work/bad/qdistro_broker.te"

for variant in bad clean; do
    make -s -C "$work/$variant" -f "$devel" qdistro_broker.pp
    test -s "$work/$variant/qdistro_broker.pp"
    # A nonzero result is expected for the bad module, and can also occur for
    # the clean module because of unrelated base-policy assertions. The
    # negative control below proves this semodule invocation checked ours.
    semodule -n -i "$work/$variant/qdistro_broker.pp" \
        > "$work/$variant/semodule.log" 2>&1 || true
done

if ! grep -Fq 'neverallow qdistro_broker_t self (rawip_socket' \
    "$work/bad/semodule.log"; then
    echo 'ERROR: broker SELinux negative control was not rejected' >&2
    cat "$work/bad/semodule.log" >&2
    exit 1
fi
if grep -Fq 'neverallow qdistro_broker_t' "$work/clean/semodule.log"; then
    echo 'ERROR: clean broker policy violates its neverallow ratchet' >&2
    grep -F 'neverallow qdistro_broker_t' "$work/clean/semodule.log" >&2
    exit 1
fi
echo '[native-podman] broker SELinux ratchet: injected rule rejected; clean broker has 0 violations'
