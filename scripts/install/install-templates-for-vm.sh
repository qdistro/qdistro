#!/bin/bash
# install-templates-for-vm.sh — idempotent install of the qdistro template/
# promotion slice (todo/fableplan) onto a fresh-clone VM.
#
# Takes the umbrella root as $1 (default /root/qdistro-src/qdistro).
#
# Layout:
#   /usr/libexec/qdistro/qdistro_templates.py            shared lib
#   /usr/libexec/qdistro/qdistro_template_*.py           modules
#   /usr/libexec/qdistro/qdistro_resolve_binding.py
#   /usr/libexec/qdistro/qdistro-template-build          CLI wrappers
#   /usr/libexec/qdistro/qdistro-template-validate
#   /usr/libexec/qdistro/qdistro-template-promote(+-unit)
#   /usr/libexec/qdistro/qdistro-template-gc
#   /usr/libexec/qdistro/qdistro-template-freshness
#   /usr/libexec/qdistro/qdistro-resolve-binding
#   /usr/local/bin/qdistro-resolve-binding               on PATH for spawn-tier2
#   /usr/lib/qdistro/templates/recipes/Containerfile.*   recipes
#   /etc/systemd/system/qdistro-template-*.{service,timer}
#   /etc/qdistro/templates/tier2-dev.toml                example policy
#   /etc/qdistro/template-retention.toml                 retention defaults
#   /var/lib/qdistro/{templates,bindings,pins,identity}  on-disk model
set -euo pipefail

UMBRELLA=${1:-/root/qdistro-src/qdistro}
SRC="$UMBRELLA/templates"
if [ ! -d "$SRC" ]; then
    echo "[install-templates] no templates/ tree at $UMBRELLA" >&2
    exit 2
fi

LIBEXEC=/usr/libexec/qdistro
BIN=/usr/local/bin
SYSD=/etc/systemd/system
RECIPES=/usr/lib/qdistro/templates/recipes

install -d -m 0755 "$LIBEXEC" "$BIN" "$SYSD" "$RECIPES"

# Python modules (flat — each CLI runs python3 on its module, and Python puts
# the module's own dir on sys.path so sibling imports resolve).
for mod in qdistro_templates.py qdistro_template_audit.py \
           qdistro_template_build.py qdistro_template_validate.py \
           qdistro_template_promote.py qdistro_template_gc.py \
           qdistro_template_freshness.py qdistro_resolve_binding.py \
           qdistro_template_status.py qdistro_state_snapshot.py; do
    install -m 0644 "$SRC/$mod" "$LIBEXEC/$mod"
done

# fableplan2 task 05: qdistro-snap-swap (the crash-consistent state-restore
# primitive) lives under snapshots/ but is imported by qdistro_state_snapshot
# and run as a CLI by the rollback flow + the admin app, so it ships flat into
# the same libexec dir (so the sibling import resolves).
install -m 0644 "$UMBRELLA/snapshots/qdistro_snap_swap.py" \
    "$LIBEXEC/qdistro_snap_swap.py"

# CLI wrappers.
make_wrapper() {
    local name="$1" module="$2"
    cat >"$LIBEXEC/$name" <<EOF
#!/bin/bash
exec /usr/bin/python3 $LIBEXEC/$module "\$@"
EOF
    chmod 0755 "$LIBEXEC/$name"
}
make_wrapper qdistro-template-build     qdistro_template_build.py
make_wrapper qdistro-template-validate  qdistro_template_validate.py
make_wrapper qdistro-template-promote   qdistro_template_promote.py
make_wrapper qdistro-template-gc        qdistro_template_gc.py
make_wrapper qdistro-template-freshness qdistro_template_freshness.py
make_wrapper qdistro-resolve-binding    qdistro_resolve_binding.py
make_wrapper qdistro-template-status    qdistro_template_status.py
make_wrapper qdistro-snap-swap          qdistro_snap_swap.py

# Expose all template CLIs on PATH (resolve-binding is required there for
# spawn-tier2; the rest are admin conveniences).
for w in qdistro-template-build qdistro-template-validate \
         qdistro-template-promote qdistro-template-gc \
         qdistro-template-freshness qdistro-resolve-binding \
         qdistro-template-status qdistro-snap-swap; do
    ln -sf "$LIBEXEC/$w" "$BIN/$w"
done

# The promote systemd wrapper (forwards one named arg).
install -m 0755 "$SRC/systemd/qdistro-template-promote-unit" \
    "$LIBEXEC/qdistro-template-promote-unit"

# Recipes.
for cf in "$SRC"/recipes/Containerfile.*; do
    [ -e "$cf" ] || continue
    install -m 0644 "$cf" "$RECIPES/$(basename "$cf")"
done

# systemd units.
for unit in "$SRC"/systemd/qdistro-template-*.service \
            "$SRC"/systemd/qdistro-template-*.timer; do
    [ -e "$unit" ] || continue
    install -m 0644 "$unit" "$SYSD/$(basename "$unit")"
done

# tmpfiles.d: /run/qdistro/silo-generation must exist admin-owned on every
# boot so qdistro-resolve-binding --record can write the per-boot runtime
# status + commit the activation marker (M1 review). Materialize it now too.
if [ -f "$SRC/systemd/qdistro-templates-tmpfiles.conf" ]; then
    install -d -m 0755 /usr/lib/tmpfiles.d
    install -m 0644 "$SRC/systemd/qdistro-templates-tmpfiles.conf" \
        /usr/lib/tmpfiles.d/qdistro-templates.conf
    systemd-tmpfiles --create /usr/lib/tmpfiles.d/qdistro-templates.conf \
        2>/dev/null || true
fi

# On-disk model: dirs (security trees owner-only) + defaults.
install -d -m 0755 /etc/qdistro/templates /var/lib/qdistro/templates
install -d -m 0700 /var/lib/qdistro/bindings /var/lib/qdistro/pins \
    /var/lib/qdistro/identity
# Silo state trees (fableplan2 task 01): admin-owned 0700 parent so the
# first promote can create <silo>/state under it (a templated launch
# hard-fails on a missing state_path — no silent tmpfs fallback).
install -d -m 0700 /var/lib/qdistro/silos
# admin owns the state trees (rootless podman + promote run as admin).
# Recursive so a prior root-created nested path does not lock admin out.
chown -R admin:admin /var/lib/qdistro/templates /var/lib/qdistro/bindings \
    /var/lib/qdistro/pins /var/lib/qdistro/identity \
    /var/lib/qdistro/silos 2>/dev/null || true

if [ ! -f /etc/qdistro/template-retention.toml ] \
        && [ -f "$UMBRELLA/deploy/etc/qdistro/template-retention.toml" ]; then
    install -m 0644 "$UMBRELLA/deploy/etc/qdistro/template-retention.toml" \
        /etc/qdistro/template-retention.toml
fi
if [ ! -f /etc/qdistro/templates/tier2-dev.toml ] \
        && [ -f "$SRC/examples/tier2-dev.toml" ]; then
    install -m 0644 "$SRC/examples/tier2-dev.toml" \
        /etc/qdistro/templates/tier2-dev.toml
fi

# tier2-browser recipe build context (fableplan2 task 02): the recipe COPYs
# the SHARED tier2/entrypoint.sh + tier2/weston.ini, and its policy declares
# [template.build].context = "tier2", which qdistro-template-build resolves
# to /usr/lib/qdistro/tier2. Install those assets from the single source
# (tier2/) so the browser candidate builds without hand-duplicated copies.
if [ -d "$UMBRELLA/tier2" ]; then
    install -d -m 0755 /usr/lib/qdistro/tier2
    for asset in weston.ini entrypoint.sh; do
        [ -f "$UMBRELLA/tier2/$asset" ] \
            && install -m 0644 "$UMBRELLA/tier2/$asset" "/usr/lib/qdistro/tier2/$asset"
    done
fi
if [ ! -f /etc/qdistro/templates/tier2-browser.toml ] \
        && [ -f "$SRC/examples/tier2-browser.toml" ]; then
    install -m 0644 "$SRC/examples/tier2-browser.toml" \
        /etc/qdistro/templates/tier2-browser.toml
fi

systemctl daemon-reload 2>/dev/null || true
echo "[install-templates] installed template/promotion slice"
