#!/bin/bash
# Install the tier-5b (per-app VM, waypipe-over-AF_VSOCK, `direct`
# publisher shape) launch infrastructure into a qdistro VM. Idempotent.
#
# Lands:
#   - /usr/local/bin/qdistro-tier5b-spawn  → wrapper around the source
#     tree's tier5b-vm/spawn-tier5b.sh (or /usr/share/qdistro/tier5b/
#     when installed from an RPM).
#   - /usr/local/bin/qdistro-tier5b-cleanup → tier5b-vm/qdistro-tier5b-cleanup.sh
#   - /usr/local/bin/qdistro-tier5b-build-guest-image →
#     tier5b-vm/build-guest-image.sh
#   - /usr/share/qdistro/tier5b/domain-template.xml — the libvirt domain
#     template spawn-tier5b.sh falls back to when not run from the source
#     tree (see spawn-tier5b.sh: the `/usr/share/qdistro/tier5b/
#     domain-template.xml` branch of the TMPL lookup).
#
# Note (polkit): unlike tier-5, the tier-5b launcher does not pkexec a
# tier-5b-specific action — there is no org.qdistro.tier5b.policy in the
# source tree and spawn-tier5b.sh / qdistro-tier5b-cleanup.sh contain no
# pkexec/polkit references. We therefore install no polkit policy here.
# Do not fabricate one; if a tier-5b policy is ever added to the source
# tree, install it the way install-tier5-for-vm.sh installs
# org.qdistro.tier5.policy.
#
# Usage:
#   bash install-tier5b-for-vm.sh <qdistro-src-root>
#
# Where <qdistro-src-root> is the directory containing tier5b-vm/.
# Typically /root/qdistro-src/qdistro in the bats VM (per
# fresh-vm-bootstrap.sh's $SRC).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[install-tier5b] must run as root" >&2
    exit 2
fi

SRC_ROOT="${1:?usage: $0 <qdistro-src-root>}"
TIER5B_DIR="$SRC_ROOT/tier5b-vm"

if [ ! -d "$TIER5B_DIR" ]; then
    echo "[install-tier5b] FAIL: $TIER5B_DIR not found" >&2
    exit 3
fi

# --- wrappers for the spawn helpers ---
#
# Do not symlink directly into the source tree: spawn-tier5b.sh resolves
# sibling files via dirname("$0") (e.g. qdistro-tier5b-publisher.sh in
# loopback mode and domain-template.xml), and build-guest-image.sh reads
# its sibling qdistro-tier5b-publisher.sh the same way. A /usr/local/bin
# symlink makes those relative lookups land under /usr/local/bin, which
# breaks the launcher path. A wrapper that execs the script in place
# keeps every sibling lookup resolving inside the source tree.
install -d /usr/local/bin
for tool in spawn-tier5b.sh:qdistro-tier5b-spawn \
            qdistro-tier5b-cleanup.sh:qdistro-tier5b-cleanup \
            build-guest-image.sh:qdistro-tier5b-build-guest-image; do
    src_basename="${tool%%:*}"
    dst_name="${tool##*:}"
    src="$TIER5B_DIR/$src_basename"
    dst="/usr/local/bin/$dst_name"
    [ -x "$src" ] || { echo "[install-tier5b] WARN: $src missing or not executable"; continue; }
    cat > "$dst" <<EOF
#!/bin/bash
exec "$src" "\$@"
EOF
    chmod 0755 "$dst"
    echo "[install-tier5b] installed wrapper $dst → $src"
done

install -d /usr/share/qdistro/tier5b
install -m 0644 "$TIER5B_DIR/domain-template.xml" \
    /usr/share/qdistro/tier5b/domain-template.xml
echo "[install-tier5b] installed /usr/share/qdistro/tier5b/domain-template.xml"

echo "[install-tier5b] done."
