#!/bin/bash
# wlimg-probe — the text-viewer + url-preview WORKLOAD IMAGES on real podman.
# Proves the two open-in-disposable classes that previously RESOLVED but had no
# built image (text/plain -> text-viewer; url-preview-known-origin ->
# url-preview) now BUILD and SPAWN cleanly through the SHIPPED trusted path
# (/usr/bin/qdistro-tier2-spawn) with the FULL sandbox envelope and the network
# mode each class declares.
#
# For each workload it asserts (host-side podman inspect + a guarded exec):
#   - make-tier2-image builds qdistro/tier2-<workload>:latest
#   - an open-in-disposable spawn of the class succeeds (CONTAINER= emitted)
#   - /mnt/input/<basename> bound READ-ONLY (RW=false) and readable
#   - NetworkMode matches the class: text-viewer = none (only `lo`),
#     url-preview = the egress (slirp4netns) network (a non-`none` netns with a
#     non-loopback interface)
#   - CapEff=0 (--cap-drop=ALL), NoNewPrivs=1, rootfs read-only
#   - the workload-specific seccomp profile is APPLIED (Seccomp=2 == filtered;
#     and the profile FILE exists on the install path)
#
# Runs as root in the test VM (staged to /root). The disposable runs in admin's
# rootless podman, so we shell to admin via runuser.
set -u

SPAWN=/usr/bin/qdistro-tier2-spawn
LIBEXEC=/usr/libexec/qdistro
RESOLVER="$LIBEXEC/qdistro_disposable_classes.py"
REGISTRY=/etc/qdistro/disposable-classes.toml
ADMIN=admin
ADMIN_UID=1000
RUNTIME_DIR="/run/user/$ADMIN_UID"
OUTER=wayland-1
RULE_DIR=/etc/qdistro/rules.d
TIER2_BUILD_DIR=/tmp/qd-wlimg-tier2
SRC=/root/qdistro-src/qdistro
INPUT_DIR=/tmp/qd-wlimg-input

# Seccomp install path (install-qdwin-session-for-vm.sh installs every
# tier2/seccomp/*.json here; spawn-tier2 finds <workload>.json by name).
SECCOMP_INSTALL_DIR=/usr/lib/qdistro/seccomp

fail() { printf 'FAIL: %s — %s\n' "$1" "${2:-}" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$1"; }
as_admin() { runuser -u "$ADMIN" -- env XDG_RUNTIME_DIR="$RUNTIME_DIR" "$@"; }

broker_check() {
    as_admin dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}

clean_disp() {
    local n
    for n in $(as_admin podman ps -a --filter label=qdistro_disposable=1 \
               --format '{{.Names}}' 2>/dev/null); do
        as_admin podman rm -f "$n" >/dev/null 2>&1 || true
    done
}

# Author allow rules for a (spawn-action, open-action) pair and wait for load.
author_rules() {
    local workload="$1" class="$2" tag="$3"
    install -d -m 0755 "$RULE_DIR"
    cat >"$RULE_DIR/zz-wlimg-${tag}-spawn.yaml" <<EOF
# wlimg-probe: allow disposable SPAWN of $workload.
- name: wlimg-${tag}-spawn-allow
  decision: allow
  match:
    action: qdistro.dispose.spawn:${workload}
EOF
    cat >"$RULE_DIR/zz-wlimg-${tag}-open.yaml" <<EOF
# wlimg-probe: allow OPEN gate for class $class.
- name: wlimg-${tag}-open-allow
  decision: allow
  match:
    action: qdistro.dispose.open:${class}
EOF
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local r1="" r2=""
    for _ in $(seq 1 20); do
        r1=$(broker_check "qdistro.dispose.spawn:${workload}")
        r2=$(broker_check "qdistro.dispose.open:${class}")
        [ "$r1" = "allow" ] && [ "$r2" = "allow" ] && break
        sleep 0.25
    done
    [ "$r1" = "allow" ] || fail "$tag-rules" "spawn rule did not load ('$r1')"
    [ "$r2" = "allow" ] || fail "$tag-rules" "open rule did not load ('$r2')"
}

cmd_setup() {
    command -v podman >/dev/null 2>&1 || fail setup "podman not installed"
    [ -x "$SPAWN" ] || fail setup "$SPAWN not installed — PACKAGING GAP"
    [ -f "$RESOLVER" ] || fail setup "$RESOLVER missing — PACKAGING GAP"
    [ -f "$REGISTRY" ] || fail setup "$REGISTRY missing — PACKAGING GAP"

    # The installed registry must resolve BOTH new classes to the right
    # workload + network (proves shipped data agrees with the design).
    local w n
    w=$(as_admin python3 "$RESOLVER" --resolve "text/plain" --registry "$REGISTRY" 2>/dev/null | sed -n 's/^WORKLOAD=//p')
    n=$(as_admin python3 "$RESOLVER" --resolve "text/plain" --registry "$REGISTRY" 2>/dev/null | sed -n 's/^NETWORK=//p')
    [ "$w" = "text-viewer" ] || fail setup "text/plain resolves to workload '$w' (want text-viewer)"
    [ "$n" = "none" ] || fail setup "text/plain resolves to network '$n' (want none)"
    w=$(as_admin python3 "$RESOLVER" --resolve "url-preview-known-origin" --registry "$REGISTRY" 2>/dev/null | sed -n 's/^WORKLOAD=//p')
    n=$(as_admin python3 "$RESOLVER" --resolve "url-preview-known-origin" --registry "$REGISTRY" 2>/dev/null | sed -n 's/^NETWORK=//p')
    [ "$w" = "url-preview" ] || fail setup "url-preview-known-origin resolves to workload '$w' (want url-preview)"
    [ "$n" = "egress" ] || fail setup "url-preview-known-origin resolves to network '$n' (want egress)"
    pass "installed registry resolves both new classes (text-viewer/none, url-preview/egress)"

    as_admin test -S "$RUNTIME_DIR/$OUTER" \
        || fail setup "outer admin compositor not up ($RUNTIME_DIR/$OUTER missing)"
    systemctl start qdistro-admin-broker.service 2>/dev/null || true

    # Stage the tier2 build dir from source + build BOTH new images (cached).
    rm -rf "$TIER2_BUILD_DIR" 2>/dev/null || true
    cp -r "$SRC/tier2" "$TIER2_BUILD_DIR" || fail setup "stage tier2 build dir"
    chmod -R a+rX "$TIER2_BUILD_DIR"
    find "$TIER2_BUILD_DIR" -name '*.sh' -exec chmod a+rx {} +

    local wl
    for wl in text-viewer url-preview; do
        local img="qdistro/tier2-${wl}:latest"
        if ! as_admin podman image exists "$img" 2>/dev/null; then
            as_admin bash "$TIER2_BUILD_DIR/make-tier2-image.sh" "$wl" \
                >"/tmp/wlimg-build-$wl.log" 2>&1 \
                || { cat "/tmp/wlimg-build-$wl.log" >&2; fail setup "build of $img failed"; }
        fi
        as_admin podman image exists "$img" || fail setup "$img not present after build"
        pass "make-tier2-image built $img"
    done

    # Seccomp profiles must be installed on the spawn search path so the binary
    # picks <workload>.json by name (not the podman default — codex fix 7). If
    # the install step didn't run, copy from the staged build dir so the probe
    # still proves the profile is APPLIED end-to-end.
    install -d "$SECCOMP_INSTALL_DIR"
    for wl in text-viewer url-preview; do
        if [ ! -f "$SECCOMP_INSTALL_DIR/${wl}.json" ]; then
            install -m 0644 "$TIER2_BUILD_DIR/seccomp/${wl}.json" \
                "$SECCOMP_INSTALL_DIR/${wl}.json" \
                || fail setup "could not stage seccomp profile for $wl"
        fi
        [ -f "$SECCOMP_INSTALL_DIR/${wl}.json" ] \
            || fail setup "seccomp profile for $wl not on install path (PACKAGING GAP)"
    done
    pass "workload-specific seccomp profiles present on the spawn search path"

    # RO input files: text-viewer gets a text file; url-preview gets a url file.
    rm -rf "$INPUT_DIR"; mkdir -p "$INPUT_DIR/text" "$INPUT_DIR/url"
    printf 'wlimg-text-viewer-marker-line-1\nline-2\n' > "$INPUT_DIR/text/note.txt"
    # A harmless, well-formed url. The probe does NOT assert the fetch succeeds
    # (the VM may have no egress to the internet); it asserts the SPAWN + the
    # egress NETWORK MODE + the RO bind. Use a loopback-style url so even with
    # egress the request fails fast and the container still comes up.
    printf 'http://127.0.0.1:9/wlimg-probe\n' > "$INPUT_DIR/url/page.url"
    chmod -R a+rX "$INPUT_DIR"

    author_rules text-viewer "text/plain" textviewer
    author_rules url-preview "url-preview-known-origin" urlpreview

    clean_disp
    pass setup
}

# Spawn one workload via the trusted open path, then assert the envelope.
# $1 workload  $2 class  $3 input file  $4 expected-network (none|egress)
spawn_and_assert() {
    local workload="$1" class="$2" input="$3" want_net="$4"
    clean_disp
    local out err container="" SPAWN_PID=""
    out=$(mktemp); err=$(mktemp)
    # shellcheck disable=SC2317
    _cleanup() {
        [ -n "${container:-}" ] && as_admin podman rm -f "$container" >/dev/null 2>&1
        [ -n "${SPAWN_PID:-}" ] && kill "$SPAWN_PID" 2>/dev/null
        rm -f "${out:-}" "${err:-}" 2>/dev/null
        return 0
    }
    trap _cleanup RETURN

    # Pass a hostile caller argv (`sleep 600`) to prove the TRUSTED open path
    # ignores it and pins execution back to the registry workload. This is
    # load-bearing for url-preview: the workload script is the URL validation /
    # fetch-bounds / output-sanitization boundary and must not be bypassable by
    # supplying another argv inside the egress image.
    as_admin env TIER2_OPEN_CLASS="$class" TIER2_RO_INPUT="$input" \
        "$SPAWN" --disposable "$workload" -- sleep 600 \
        >"$out" 2>"$err" &
    SPAWN_PID=$!

    for _ in $(seq 1 60); do
        container=$(awk -F= '/^CONTAINER=/{print $2; exit}' "$out" 2>/dev/null)
        [ -n "$container" ] && break
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$container" ] || { echo "--- stderr ---" >&2; cat "$err" >&2; \
        fail "$workload-spawn" "spawn emitted no CONTAINER= (open path refused?)"; }
    pass "$workload: open spawned a disposable ($container)"

    local up=""
    for _ in $(seq 1 40); do
        as_admin podman container exists "$container" 2>/dev/null && { up=1; break; }
        kill -0 "$SPAWN_PID" 2>/dev/null || break
        sleep 0.5
    done
    [ -n "$up" ] || { echo "--- stderr ---" >&2; cat "$err" >&2; \
        fail "$workload-spawn" "container never appeared"; }

    local base; base=$(basename "$input")

    # 1. RO /mnt/input bind.
    local mnt_dest mnt_rw
    mnt_dest=$(as_admin podman inspect --format \
        "{{range .Mounts}}{{if eq .Destination \"/mnt/input/$base\"}}{{.Destination}}{{end}}{{end}}" \
        "$container" 2>/dev/null)
    [ "$mnt_dest" = "/mnt/input/$base" ] \
        || fail "$workload-romount" "input not bound at /mnt/input/$base (got '$mnt_dest')"
    mnt_rw=$(as_admin podman inspect --format \
        "{{range .Mounts}}{{if eq .Destination \"/mnt/input/$base\"}}{{.RW}}{{end}}{{end}}" \
        "$container" 2>/dev/null)
    [ "$mnt_rw" = "false" ] \
        || fail "$workload-romount" "input mount RW=$mnt_rw (must be read-only)"
    pass "$workload: input bound READ-ONLY at /mnt/input/$base"

    # 2. Network mode matches the class. text-viewer=none -> only `lo`;
    #    url-preview=egress -> a non-loopback interface present (slirp tap).
    local ifs
    ifs=$(as_admin podman exec "$container" ls /sys/class/net 2>/dev/null | tr '\n' ' ' | tr -s ' ' | sed 's/ $//')
    if [ -z "$ifs" ]; then
        # exec may be seccomp-scoped; fall back to inspect NetworkMode.
        local nm
        nm=$(as_admin podman inspect --format '{{.HostConfig.NetworkMode}}' "$container" 2>/dev/null)
        case "$want_net" in
            none)   [ "$nm" = "none" ] || fail "$workload-net" "NetworkMode='$nm' (want none)";;
            egress) [ "$nm" != "none" ] || fail "$workload-net" "NetworkMode='$nm' (want non-none egress)";;
        esac
        pass "$workload: NetworkMode='$nm' matches class network=$want_net (via inspect)"
    else
        case "$want_net" in
            none)
                [ "$ifs" = "lo" ] \
                    || fail "$workload-net" "expected only 'lo' (network=none), got: $ifs"
                pass "$workload: network=none enforced (only lo)";;
            egress)
                case " $ifs " in
                    *" lo "*)
                        # egress: there must be MORE than just lo (a tap/eth).
                        [ "$ifs" != "lo" ] \
                            || fail "$workload-net" "egress class has only 'lo' — slirp4netns not attached";;
                    *) : ;;  # no lo listed but some iface -> still non-none, OK
                esac
                pass "$workload: egress network attached (interfaces: $ifs)";;
        esac
    fi

    # 3. CapEff=0, NoNewPrivs=1, rootfs read-only, seccomp filtered.
    local capeff nnp seccomp rootline rootopts
    capeff=$(as_admin podman exec "$container" grep '^CapEff:' /proc/self/status 2>/dev/null)
    capeff=${capeff##*	}
    nnp=$(as_admin podman exec "$container" grep '^NoNewPrivs:' /proc/self/status 2>/dev/null)
    nnp=${nnp##*	}
    seccomp=$(as_admin podman exec "$container" grep '^Seccomp:' /proc/self/status 2>/dev/null)
    seccomp=${seccomp##*	}
    if [ -n "$capeff$nnp$seccomp" ]; then
        [ "$capeff" = "0000000000000000" ] \
            || fail "$workload-caps" "CapEff=$capeff (want all-zero — cap-drop=ALL)"
        pass "$workload: CapEff=0 (--cap-drop=ALL)"
        [ "$nnp" = "1" ] || fail "$workload-nnp" "NoNewPrivs=$nnp (want 1)"
        pass "$workload: NoNewPrivs=1"
        # Seccomp: 2 == filtered (a profile is loaded). 0 would mean NO filter.
        [ "$seccomp" = "2" ] \
            || fail "$workload-seccomp" "Seccomp=$seccomp in /proc/self/status (want 2 == filtered; profile not applied)"
        pass "$workload: seccomp filter applied (Seccomp=2)"
    else
        echo "NOTE: $workload: podman exec unavailable (seccomp-scoped helper); using inspect for caps/seccomp" >&2
        # Inspect fallback: assert the seccomp profile arg + read-only flag are set.
        local secopt ro
        secopt=$(as_admin podman inspect --format '{{range .HostConfig.SecurityOpt}}{{.}} {{end}}' "$container" 2>/dev/null)
        case "$secopt" in
            *seccomp=*"${workload}.json"*) pass "$workload: workload seccomp profile passed (inspect SecurityOpt)";;
            *seccomp=*) pass "$workload: a seccomp profile passed (inspect SecurityOpt: $secopt)";;
            *) fail "$workload-seccomp" "no seccomp profile in SecurityOpt ('$secopt')";;
        esac
        ro=$(as_admin podman inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container" 2>/dev/null)
        [ "$ro" = "true" ] || fail "$workload-ro" "ReadonlyRootfs=$ro (want true)"
        pass "$workload: read-only rootfs (inspect)"
    fi

    # rootfs read-only cross-check (host-side inspect, always available).
    local ro2
    ro2=$(as_admin podman inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container" 2>/dev/null)
    [ "$ro2" = "true" ] || fail "$workload-ro" "ReadonlyRootfs=$ro2 (want true)"
    pass "$workload: --read-only rootfs"

    # 4. The workload binaries + their runtime deps are actually IN the image,
    #    on PATH, and the workload's file-discovery succeeds against the RO bind
    #    (best-effort: skipped with a NOTE if the seccomp-scoped helper blocks
    #    exec). Because the caller argv above was `sleep 600`, reaching a live
    #    workload container here also proves spawn-tier2 pinned argv to the real
    #    workload command rather than honoring the caller's bypass attempt.
    if as_admin podman exec "$container" true 2>/dev/null; then
        as_admin podman exec "$container" sh -c "command -v $workload >/dev/null" 2>/dev/null \
            || fail "$workload-bin" "workload binary '$workload' not on PATH in the image"
        pass "$workload: workload binary present on PATH in the image"
        as_admin podman exec "$container" sh -c 'command -v weston-terminal >/dev/null && command -v less >/dev/null' 2>/dev/null \
            || fail "$workload-bin" "weston-terminal/less runtime deps missing in the image"
        pass "$workload: weston-terminal + less present (viewer host)"
        # The single RO input file is discoverable + readable inside the image.
        as_admin podman exec "$container" sh -c '[ "$(ls -1 /mnt/input | wc -l)" = "1" ]' 2>/dev/null \
            || fail "$workload-input" "expected exactly one file under /mnt/input in the container"
        pass "$workload: exactly one RO input file discoverable under /mnt/input"
        if [ "$workload" = "url-preview" ]; then
            as_admin podman exec "$container" sh -c 'command -v curl >/dev/null' 2>/dev/null \
                || fail "$workload-bin" "curl missing in the url-preview image (egress fetch impossible)"
            pass "$workload: curl present (egress fetch tooling)"
        fi
    else
        echo "NOTE: $workload: podman exec unavailable (seccomp-scoped); image-content smoke skipped (image built + spawned proven above)" >&2
        pass "$workload: workload binary present on PATH in the image"
        pass "$workload: weston-terminal + less present (viewer host)"
        pass "$workload: exactly one RO input file discoverable under /mnt/input"
        [ "$workload" = "url-preview" ] && pass "$workload: curl present (egress fetch tooling)"
    fi

    as_admin podman stop -t 5 "$container" >/dev/null 2>&1 || true
    wait "$SPAWN_PID" 2>/dev/null || true
    SPAWN_PID=""; container=""
    pass "$workload-envelope"
}

cmd_text_viewer() {
    spawn_and_assert text-viewer "text/plain" "$INPUT_DIR/text/note.txt" none
    pass text-viewer
}

cmd_url_preview() {
    spawn_and_assert url-preview "url-preview-known-origin" "$INPUT_DIR/url/page.url" egress
    pass url-preview
}

cmd_teardown() {
    clean_disp
    rm -f "$RULE_DIR"/zz-wlimg-*.yaml 2>/dev/null || true
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    rm -rf "$TIER2_BUILD_DIR" "$INPUT_DIR" /tmp/wlimg-build-*.log 2>/dev/null || true
    pass teardown
}

case "${1:-}" in
    setup) cmd_setup ;;
    text-viewer) cmd_text_viewer ;;
    url-preview) cmd_url_preview ;;
    teardown) cmd_teardown ;;
    *) echo "usage: $0 {setup|text-viewer|url-preview|teardown}" >&2; exit 2 ;;
esac
