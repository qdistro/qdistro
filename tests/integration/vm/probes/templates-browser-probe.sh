#!/bin/bash
# templates-browser-probe.sh — the fableplan2 task 06 browser-rollback demo
# (doc/06-integration-tests.md). The slice exists for ONE end-to-end story on
# real rootless podman: update a browser silo, prove it still renders and
# fetches pages, roll it back WITH ITS STATE, prove again — and show the class
# of breakage (a post-update login regression) that pre-promotion probes
# cannot see, which is the honest motivation for rollback.
#
# This probe drives the REAL launch path: qdistro-silo-launch (D-Bus StartSilo)
# -> the tier-2 launcher unit -> spawn-tier2 -> resolve-binding -> the task-05
# state mount -> a detached `qdistro-silo-<name>` container we `podman exec`
# headless Chromium into. It therefore also closes the last task-05 VM-gated
# item (the full spawn-tier2 -> snapshot -> promote --rollback --restore-state
# path). Runs in the VM only: it needs the booted admin compositor (the outer
# wayland socket spawn-tier2 binds) and the system session manager.
#
# Privilege split (the .bats suite routes each scenario to the right user):
#   provision-silo / deprovision-silo  run as ROOT (edit silos.yaml + restart
#       the daemon — argv must be a multi-token holder, which CreateTemplateSilo
#       cannot express).
#   everything else                    runs as ADMIN (uid 1000: rootless
#       podman, the template CLIs, qdistro-silo-launch, the login checks).
#
# Every assertion is on a digest, a file's contents, or a DOM SENTINEL — never
# exit-code-only, never on a tag an earlier step removed (tests/AGENTS.md). The
# login site is the test-local fixture templates-browser-login-site.py.
#
# Usage: templates-browser-probe.sh <scenario>
set -uo pipefail

# --- shared config / cross-scenario state -------------------------------
SILO="browserdemo"                 # binding name AND session silo name
CONTAINER="qdistro-silo-$SILO"     # spawn-tier2 names it qdistro-silo-<name>
TEMPLATE="tier2-browser"
WORKLOAD="browser"
STATE="/tmp/fp2-browser"           # cross-scenario scratch (admin-writable)
LOGIN_PORT=8099
LOGIN_SITE="/root/templates-browser-login-site.py"
SILOS_YAML="/etc/qdistro/silos.yaml"
STATE_PATH_DEFAULT="/var/lib/qdistro/silos/$SILO/state"
PROFILE="/home/admin/profile"      # the chromium profile, inside the state bind

# Broker spawn gate. Since edb7f32 ("require broker allow for tier2 spawns") the
# silo spawn is default-deny: spawn-tier2 computes the action
# qdistro.tier2.spawn:<workload>/<app> and refuses unless a rule allows it. The
# silo's launch argv is ["sleep","infinity"] (see _silos_yaml_edit), so APP_NAME
# is "sleep" and the action is qdistro.tier2.spawn:browser/sleep. Author a
# test-owned allow rule (root, in provision-silo; removed in deprovision-silo),
# mirroring tier2-silo-secctx-wiretag-probe.sh. The gate runs AS ADMIN under the
# root-launcher, so a uid-unscoped action rule matches.
RULE_DIR=/etc/qdistro/rules.d
RULE_FILE="$RULE_DIR/zz-silo-browserdemo-allow.yaml"
SPAWN_ACTION="qdistro.tier2.spawn:$WORKLOAD/sleep"

# The template CLIs read their POLICY tree from QDISTRO_ETC_DIR but write the
# binding/generations/pins to the real (admin-owned) /var/lib/qdistro. The real
# /etc/qdistro/templates is root-owned 0755 (admin cannot write the per-build
# policies there), so the admin-side build/validate/promote/gc reads policy from
# an admin-writable private etc. The DAEMON's launch path resolves the binding
# (var, real) and reads the activation_snapshot policy by the binding's TEMPLATE
# name from the DEFAULT /etc/qdistro/templates — so every generation is built
# under the SAME template name "tier2-browser", and the installed (strict)
# policy stays the one the launch honours.
export QDISTRO_ETC_DIR="$STATE/etc"

# The pinned headless-Chromium arg set the slice reuses (mirror of
# qdistro_template_validate.CHROMIUM_HEADLESS_ARGS — kept in lockstep; the
# page-open gate and these checks must agree on the flags). Extra flags are
# runtime-robustness only (NOT render-behaviour changes):
#   --disable-dev-shm-usage: use /tmp instead of /dev/shm so a check does not
#     depend on the container's --shm-size (silo ships podman default 64m,
#     which a real render can exhaust).
#   --renderer-process-limit=1 / --disable-extensions: cap Chromium's process
#     fan-out. Under full-CI (many parallel 4 GiB bats VMs) the guest OOMs
#     mid dump-dom and update-flip reports "genB silo did not render" with an
#     empty DOM; these flags keep a single headless check under ~one renderer.
CHROMIUM_ARGS=(
    --headless=new --no-sandbox --disable-background-networking
    --disable-sync --disable-features=Translate --no-first-run
    --no-default-browser-check --window-size=1024,768 --disable-gpu
    --disable-dev-shm-usage --renderer-process-limit=1 --disable-extensions
)
# A realistic Chromium UA; the generation marker is appended as a suffix the
# login site keys breakage on (the site's JS must NOT UA-sniff — only the
# server reads the suffix). Full-UA replacement would be unrealistic.
UA_BASE="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1 ${2:-}"; exit 1; }

# CheckPermission as the admin uid (1000) — the identity the root-launcher drops
# the spawn-tier2 broker gate to. Used to confirm the test-owned allow rule loaded.
broker_check_admin() {
    runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 \
        dbus-send --system --print-reply=literal \
        --dest=org.qdistro.AdminBroker1 /org/qdistro/AdminBroker1 \
        org.qdistro.AdminBroker1.CheckPermission \
        "string:$1" "dict:string:string:" 2>/dev/null | tr -d ' \t\n'
}

# Resolve the template CLIs: installed wrappers (VM) else in-tree (dev).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." 2>/dev/null && pwd || true)"
TEMPLATES_SRC=""
for cand in /usr/libexec/qdistro "$REPO_ROOT/templates"; do
    [ -f "$cand/qdistro_template_build.py" ] && { TEMPLATES_SRC="$cand"; break; }
done
cli() {
    local tool="$1"; shift
    if command -v "qdistro-$tool" >/dev/null 2>&1; then
        "qdistro-$tool" "$@"
    else
        PYTHONPATH="$TEMPLATES_SRC:$REPO_ROOT/snapshots" \
            python3 "$TEMPLATES_SRC/qdistro_${tool//-/_}.py" "$@"
    fi
}

binding_file() { echo "/var/lib/qdistro/bindings/$SILO.toml"; }
active_gen() {
    python3 -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb"))["active_generation"])' \
        "$(binding_file)" 2>/dev/null
}
binding_get() {
    python3 - "$(binding_file)" "$1" <<'PY'
import sys, tomllib, re
data = tomllib.load(open(sys.argv[1], "rb"))
key = sys.argv[2]
m = re.match(r"^(\w+)\[(\d+)\]$", key)
print(data[m.group(1)][int(m.group(2))] if m else data[key])
PY
}

# audit_has <event> <column> <value> against the template audit DB.
audit_has() {
    python3 - "/var/lib/qdistro/audit/template_audit.sqlite" "$1" "$2" "$3" <<'PY'
import sys, sqlite3, os
db, event, col, val = sys.argv[1:5]
if not os.path.isfile(db):
    sys.exit(1)
con = sqlite3.connect(db)
n = con.execute(f"SELECT count(*) FROM template_audit WHERE event=? AND {col}=?",
                (event, val)).fetchone()[0]
sys.exit(0 if n else 1)
PY
}

# --- login site control (the site listens on the VM; reach it locally) ---
site_ctl() {  # site_ctl <path-with-query>
    python3 - "http://127.0.0.1:$LOGIN_PORT$1" <<'PY'
import sys, urllib.request
req = urllib.request.Request(sys.argv[1], data=b"", method="POST")
with urllib.request.urlopen(req, timeout=10) as r:
    sys.stdout.write(r.read().decode("utf-8", "replace"))
PY
}

ua_for() { printf '%s qdistro-mk/%s' "$UA_BASE" "$1"; }

# --- chromium drivers ----------------------------------------------------
# The image ENTRYPOINT starts a nested weston (a headless check must bypass it),
# Chromium wants a session bus (dbus-run-session) and a writable XDG_RUNTIME_DIR;
# this mirrors the page-open gate's runtime (qdistro_template_validate). All
# Chromium drivers wrap a wall-clock `timeout` (codex r5: --virtual-time-budget
# is virtual time and may stall on a pending request).
#
# Dual timeout (host + in-container): a host-side `timeout` kills the podman
# exec CLIENT, but under guest memory pressure podman can hang on cleanup and
# leave chromium alive (same rootless conmon reality breakage-matrix asserts).
# Nesting `timeout` inside the container (coreutils ships in the image) ensures
# the renderer is SIGKILL'd even when the client path wedges. QD_SILO_PROFILE
# overrides the user-data-dir (file:// render checks use an ephemeral dir so
# they do not pay the cost of loading the full persisted profile).
# QD_SILO_CHROMIUM_ERR, when set, receives chromium/podman stderr for diagnostics.
silo_chromium() {  # silo_chromium <timeout> <ua> <url>  [extra chromium args...]
    local to="$1" ua="$2" url="$3"; shift 3
    local profile="${QD_SILO_PROFILE:-$PROFILE}"
    local errf="${QD_SILO_CHROMIUM_ERR:-/dev/null}"
    # Inner budget slightly under the outer so the in-container kill fires first.
    local inner=$(( to > 15 ? to - 10 : to ))
    timeout --kill-after=10 "$to" \
        podman exec -e HOME=/home/admin "$CONTAINER" \
        dbus-run-session -- /bin/sh -c \
        'export XDG_RUNTIME_DIR=/tmp/cr; mkdir -p "$XDG_RUNTIME_DIR" "$1"; chmod 700 "$XDG_RUNTIME_DIR"; shift; exec timeout --kill-after=5 "$@"' \
        _ "$profile" "$inner" chromium \
        "${CHROMIUM_ARGS[@]}" --user-agent="$ua" --user-data-dir="$profile" \
        "$@" --dump-dom "$url" 2>"$errf"
}

# Assert a file:// RENDER-OK sentinel from the running silo. Retries under
# transient guest memory pressure (full-CI OOM of a single dump-dom attempt is
# the observed failure mode for update-flip). Uses an ephemeral profile so the
# check only proves the runtime can render, not that the persisted profile is
# healthy (session survival is asserted separately against $PROFILE).
assert_silo_file_render() {  # assert_silo_file_render <label> <ua> <sentinel> [attempts]
    local label="$1" ua="$2" sentinel="$3" attempts="${4:-3}"
    local html="<!doctype html><html><body><div id=s>${sentinel}</div></body></html>"
    local attempt=1 dom errf mem errsnip domsnip
    errf="$(mktemp)"
    while [ "$attempt" -le "$attempts" ]; do
        ensure_profile_free
        podman exec "$CONTAINER" sh -c 'printf "%s" "$1" > /tmp/probe.html' _ "$html" \
            || fail "$label" "could not write /tmp/probe.html for render check"
        # Ephemeral profile: render-only, no cookie DB load, no Singleton fights.
        dom="$(
            QD_SILO_PROFILE=/tmp/cr-render-profile \
            QD_SILO_CHROMIUM_ERR="$errf" \
            silo_chromium 90 "$ua" "file:///tmp/probe.html" || true
        )"
        if printf '%s' "$dom" | grep -qF "$sentinel"; then
            rm -f "$errf"
            return 0
        fi
        # Reclaim a wedged in-container chromium before the next attempt.
        ensure_profile_free
        sleep $(( attempt * 2 ))
        attempt=$(( attempt + 1 ))
    done
    mem="$(free -m 2>/dev/null | head -2 | tr '\n' ' ' || true)"
    errsnip="$(tr '\n' ' ' <"$errf" 2>/dev/null | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//' | head -c 300)"
    domsnip="$(printf '%s' "$dom" | tr -d '\n' | head -c 200)"
    rm -f "$errf"
    fail "$label" "silo did not render ${sentinel} after ${attempts} attempts (dom: ${domsnip:-<empty>}; stderr: ${errsnip:-<none>}; guest free: ${mem:-?})"
}

# Run headless Chromium in a THROWAWAY pasta container off an image digest
# with an EPHEMERAL profile (--entrypoint= bypasses the nested weston). Used for
# "a fresh login" / "gen A still works" / marker-keyed breakage checks that must
# not touch the silo profile. Remaining args are URLs visited in order through
# ONE profile (perform-login then read-/home share the cookie); only the LAST
# dump is emitted to stdout.
# shellcheck disable=SC2016  # Expanded by /bin/sh inside the container.
readonly THROWAWAY_CHROMIUM_SCRIPT='export HOME=/tmp/home; mkdir -p "$HOME" /tmp/cr; export XDG_RUNTIME_DIR=/tmp/cr; chmod 700 /tmp/cr; CA=($CA); out=""; for u in "$@"; do out="$(chromium "${CA[@]}" --user-agent="$UA" --user-data-dir=/tmp/home/profile --dump-dom "$u")"; done; printf "%s" "$out"'

throwaway_chromium() {  # throwaway_chromium <timeout> <image> <ua> <url...steps>
    local to="$1" image="$2" ua="$3"; shift 3
    local nameflag=()
    [ -n "${QD_TW_NAME:-}" ] && nameflag=(--name "$QD_TW_NAME")
    timeout --kill-after=10 "$to" \
        podman run --rm --network=pasta "${nameflag[@]}" \
        --cap-drop=ALL --security-opt=no-new-privileges --read-only \
        --tmpfs /tmp:rw,exec,size=512m,mode=1777 --shm-size=256m --entrypoint= \
        -e CA="${CHROMIUM_ARGS[*]}" -e UA="$ua" "$image" \
        dbus-run-session -- /bin/sh -c "$THROWAWAY_CHROMIUM_SCRIPT" _ "$@"
}

# Start the same throwaway browser check detached under an exact name. This is
# used by slow-auth so the wall-clock deadline can time out `podman wait`
# without signalling `podman run`: signalling the attached run client races
# Podman's --rm cleanup, so the container may disappear before the harness can
# prove that Chromium was still blocked in /auth.
start_throwaway_chromium() {  # start_throwaway_chromium <name> <image> <ua> <url...steps>
    local name="$1" image="$2" ua="$3"; shift 3
    podman run -d --name "$name" --network=pasta \
        --cap-drop=ALL --security-opt=no-new-privileges --read-only \
        --tmpfs /tmp:rw,exec,size=512m,mode=1777 --shm-size=256m --entrypoint= \
        -e CA="${CHROMIUM_ARGS[*]}" -e UA="$ua" "$image" \
        dbus-run-session -- /bin/sh -c "$THROWAWAY_CHROMIUM_SCRIPT" _ "$@"
}

# Remove the named slow-auth check and prove it is absent. Podman's
# `container exists` contract is tri-state: 0=present, 1=absent, 125=operational
# error. Only 1 establishes cleanup; treating every nonzero as "gone" would
# false-green when rootless storage or Podman itself is broken.
slowcheck_exists() {
    if [ "${QD_TB_INJECT_SLOWCHECK_EXISTS_ERROR:-0}" = 1 ]; then
        echo "injected podman container-exists operational error" >&2
        return 125
    fi
    podman container exists fp2-slowcheck
}

cleanup_slowcheck() {
    local rm_rc exists_rc
    podman rm -f fp2-slowcheck >/dev/null 2>&1
    rm_rc=$?
    slowcheck_exists >/dev/null 2>&1
    exists_rc=$?
    case "$exists_rc" in
        1) return 0 ;;
        0)
            echo "slow-auth cleanup failed: fp2-slowcheck still exists (rm_rc=$rm_rc)" >&2
            return 1
            ;;
        *)
            echo "slow-auth cleanup failed closed: podman container exists operational error rc=$exists_rc (rm_rc=$rm_rc)" >&2
            return 1
            ;;
    esac
}

cleanup_slowcheck_on_exit() {
    local original_rc=$?
    # Avoid recursive EXIT dispatch when this handler exits explicitly.
    trap - EXIT
    if ! cleanup_slowcheck; then
        echo "FAIL: slow-auth cleanup could not establish container absence" >&2
        exit 1
    fi
    exit "$original_rc"
}

# Regression for the cleanup contract itself. A failure immediately after the
# detached start must reap the container, while an operational error from the
# authoritative existence probe must make an otherwise-successful exit fail.
slowcheck_cleanup_regression() {  # slowcheck_cleanup_regression <image>
    local image="$1" rc errf
    cleanup_slowcheck \
        || fail breakage-matrix "slow-auth regression pre-clean could not establish absence"

    (
        podman run -d --name fp2-slowcheck --network=none --read-only \
            --entrypoint= "$image" sleep 300 >/dev/null \
            || exit 90
        trap cleanup_slowcheck_on_exit EXIT
        exit 91  # injected post-start readiness/assertion failure
    )
    rc=$?
    [ "$rc" -eq 91 ] \
        || fail breakage-matrix "slow-auth cleanup trap did not preserve injected failure rc=91 (rc=$rc)"
    cleanup_slowcheck \
        || fail breakage-matrix "slow-auth cleanup trap left the injected-failure container"

    errf="$(mktemp)" || fail breakage-matrix "could not allocate cleanup regression evidence"
    (
        podman run -d --name fp2-slowcheck --network=none --read-only \
            --entrypoint= "$image" sleep 300 >/dev/null \
            || exit 90
        trap cleanup_slowcheck_on_exit EXIT
        export QD_TB_INJECT_SLOWCHECK_EXISTS_ERROR=1
        exit 0
    ) 2>"$errf"
    rc=$?
    if [ "$rc" -eq 0 ] \
        || ! grep -q 'container exists operational error' "$errf"; then
        local errsnip
        errsnip="$(tr '\n' ' ' <"$errf" | head -c 300)"
        rm -f "$errf"
        fail breakage-matrix "slow-auth cleanup operational error did not fail closed (rc=$rc; stderr=${errsnip:-<none>})"
    fi
    rm -f "$errf"
    cleanup_slowcheck \
        || fail breakage-matrix "slow-auth operational-error regression left a container"
    echo "PASS: slowcheck-cleanup-regression"
}

# --- login-site reachability --------------------------------------------
# With the provision-silo podman shim adding pasta host-map options, a
# `--network=pasta` container reaches the VM host (where the login site
# binds 0.0.0.0) at 10.0.2.2 (mapped via pasta --map-host-loopback).
BASE_HOST="10.0.2.2"
base_url() { echo "http://$BASE_HOST:$LOGIN_PORT"; }

# Assert the running silo's egress (plain pasta + the nested-VM shim) can reach
# the login site. FAIL LOUD if not (tests/AGENTS.md: a missing prerequisite is a
# failure, not a skip) — a throwaway curl off the genA image (which ships curl).
# Stderr is captured so a podman refusal (e.g. missing backend) surfaces on fail.
assert_site_reachable() {  # assert_site_reachable <image> <label>
    local image="$1" label="$2" out err
    err="$(mktemp)"
    out="$(timeout 30 podman run --rm --network=pasta --entrypoint= "$image" \
           curl -fsS --max-time 12 "$(base_url)/healthz" 2>"$err" || true)"
    if printf '%s' "$out" | grep -q HEALTHZ-OK; then
        rm -f "$err"
        return 0
    fi
    local podman_err
    podman_err="$(tr '\n' ' ' <"$err" | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
    rm -f "$err"
    fail "$label" "login site unreachable from a pasta container at $(base_url) — the browser silo's egress cannot reach the test site${podman_err:+; podman/curl: $podman_err}"
}

# Assert no stray chromium owns the silo profile before a profile check
# (codex r5 profile-ownership invariant: the exec checks are the profile's
# only user, strictly serialized). Also reclaim orphans left when a host-side
# timeout killed the podman-exec client but left the in-container renderer
# alive (rootless conmon), and drop Singleton locks on both the persisted and
# the ephemeral render profile.
ensure_profile_free() {
    podman exec "$CONTAINER" sh -c '
        pkill -9 -f chromium 2>/dev/null || true
        pkill -9 -f chrome 2>/dev/null || true
        rm -f '"$PROFILE"'/Singleton* /tmp/cr-render-profile/Singleton* 2>/dev/null || true
        # Brief settle so the next chromium does not race a dying zygote.
        sleep 0.3
    ' >/dev/null 2>&1 || true
}

# --- recipe builders -----------------------------------------------------
# Write the private-etc tier2-browser policy with a chosen build recipe. Every
# generation is built under the SAME template name so the binding.template stays
# "tier2-browser" (the launch path reads the installed strict policy by that
# name). $1 = containerfile (a recipe name resolved under recipes/, or an
# absolute path); $2 = network_mode; $3 = context line (empty for derived
# FROM-digest recipes that COPY nothing).
write_browser_policy() {  # write_browser_policy <containerfile> <network> [context]
    local cf="$1" net="$2" ctx="${3:-}"
    install -d -m 0755 "$QDISTRO_ETC_DIR/templates"
    {
        echo '[template]'
        echo 'class = "derived"'
        echo 'activation_snapshot = "strict"'
        echo '[template.state_boundary]'
        echo 'class = "split-app-profile"'
        echo 'enforced = "partial"'
        echo '[template.build]'
        echo "containerfile = \"$cf\""
        [ -n "$ctx" ] && echo "context = \"$ctx\""
        echo "network_mode = \"$net\""
        echo '[[template.probe]]'
        echo 'name = "process-starts"'
        echo 'kind = "command"'
        echo 'class = "local-runtime"'
        echo 'command = "chromium --version"'
        echo 'required = true'
        echo 'timeout = 30'
        echo '[[template.probe]]'
        echo 'name = "page-open"'
        echo 'kind = "page-open"'
        echo 'class = "local-runtime"'
        echo 'required = true'
        echo 'timeout = 120'
    } > "$QDISTRO_ETC_DIR/templates/$TEMPLATE.toml"
}

ensure_browser_policy() {
    # The authored browser policy is installed by install-templates-for-vm.sh;
    # require it, then seed the private etc with the SHIPPED recipe + context so
    # gen A is the real authored image.
    [ -f "/etc/qdistro/templates/$TEMPLATE.toml" ] \
        || fail "setup" "no $TEMPLATE policy at /etc/qdistro/templates (install-templates-for-vm.sh)"
    write_browser_policy "Containerfile.$TEMPLATE" "unrestricted" "tier2"
}

# Build + validate a candidate of template tier2-browser from the policy
# currently written to the private etc, then promote it to the silo. Echoes
# the promoted generation digest. $1 = a label for failure messages.
build_validate_promote() {
    local label="$1" out rid
    out="$(cli template-build "$TEMPLATE" 2>&1)" || fail "$label" "build failed: $out"
    rid="$(echo "$out" | sed -n 's/^RUN_ID=//p')"
    [ -n "$rid" ] || fail "$label" "no RUN_ID from build"
    cli template-validate "$rid" >/dev/null 2>&1 || fail "$label" "validate failed ($rid)"
    cli template-promote "$SILO" "$rid" >/dev/null 2>&1 || fail "$label" "promote failed"
}

build_validate_promote_A() {
    write_browser_policy "Containerfile.$TEMPLATE" "unrestricted" "tier2"
    build_validate_promote setup
}

# Build gen B as a tiny deterministic layer FROM gen A's digest that OVERWRITES
# only /etc/qdistro-image-marker (codex r5: no second full zypper build), under
# the SAME template name. Promotes B.
build_validate_promote_B() {
    local genA bdir; genA="$(cat "$STATE/genA")"
    bdir="$STATE/recipeB"; mkdir -p "$bdir"
    cat > "$bdir/Containerfile.$TEMPLATE" <<CF
FROM $genA
USER root
RUN cat /proc/sys/kernel/random/uuid > /etc/qdistro-image-marker \
 && chmod 0644 /etc/qdistro-image-marker
USER 1000:1000
CF
    write_browser_policy "$bdir/Containerfile.$TEMPLATE" "unrestricted"
    build_validate_promote update-flip
}

marker_of_image() {  # marker_of_image <image-digest>
    podman run --rm --network=none --entrypoint= "$1" \
        cat /etc/qdistro-image-marker 2>/dev/null | tr -d '\r\n'
}

# Normalised sha256:<hex> of the image a running container was started from.
container_image_digest() {
    local id; id="$(podman inspect --format '{{.ImageID}}' "$CONTAINER" 2>/dev/null)"
    [ -n "$id" ] && echo "sha256:${id#sha256:}"
}

# --- silo lifecycle helpers (admin) -------------------------------------
launch_silo() {
    local label="${1:-launch}" out
    # Capture stderr instead of discarding it — when silo launch fails (e.g. a
    # polkit auth-agent NoReply, or the session manager being down) the real
    # reason is here, and suppressing it turned every downstream scenario into a
    # misleading cascade (state/cookie assertions that never had a browser).
    out="$(qdistro-silo-launch "$SILO" 2>&1)" \
        || fail "$label" "qdistro-silo-launch $SILO failed (session manager/compositor up?): ${out:-<no output>}"
    # Wait for the detached container to be running AND accept a trivial exec
    # (Running alone can race the entrypoint/conmon setup under load).
    local i
    for i in $(seq 1 60); do
        if [ "$(podman inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = true ] \
            && podman exec "$CONTAINER" true >/dev/null 2>&1; then
            ensure_profile_free
            return 0
        fi
        sleep 1
    done
    fail "$label" "container $CONTAINER never reached Running+exec-ready"
}
stop_silo() {
    # Kill any in-container chromium first so the unit's SIGTERM/stop does not
    # sit on a wedged dump-dom (observed: StopSignal SIGTERM failed, systemd
    # stop timed out, podman DB locked under OOM).
    if podman container exists "$CONTAINER" 2>/dev/null; then
        ensure_profile_free
    fi
    qdistro-silo-launch --stop "$SILO" >/dev/null 2>&1 || true
    local i
    for i in $(seq 1 30); do
        podman container exists "$CONTAINER" 2>/dev/null || return 0
        sleep 1
    done
    podman rm -f "$CONTAINER" >/dev/null 2>&1 || true
}

# =========================================================================
# ROOT scenarios: provision/deprovision the session silo via silos.yaml.
# =========================================================================
# Edit silos.yaml by TEXT BLOCKS (no daemon-module import — that is fragile to
# run standalone): keep the comment header + `silos:`, preserve every existing
# row block verbatim, drop any prior block for our silo (and the `[]` empty
# placeholder), then optionally append our canonical block. The daemon's
# tolerant loader re-parses (and re-validates) this on the next restart.
_silos_yaml_edit() {  # _silos_yaml_edit <add|remove>
    python3 - "$SILOS_YAML" "$SILO" "$WORKLOAD" "$1" <<'PY'
import sys, os, time
path, silo, workload, mode = sys.argv[1:5]
lines = open(path).read().splitlines() if os.path.isfile(path) else []
hdr, body = ["silos:"], []
for i, l in enumerate(lines):
    if l.strip() == "silos:":
        hdr = lines[:i + 1]
        body = lines[i + 1:]
        break
blocks, cur = [], None
for l in body:
    if l.strip() == "[]":
        continue
    if l.startswith("  - "):
        cur = {"name": None, "lines": [l]}
        blocks.append(cur)
        s = l[4:].strip()
        if s.startswith("name:"):
            cur["name"] = s.split(":", 1)[1].strip()
    elif cur is not None and l.startswith("    "):
        cur["lines"].append(l)
        s = l.strip()
        if cur["name"] is None and s.startswith("name:"):
            cur["name"] = s.split(":", 1)[1].strip()
keep = [b for b in blocks if b["name"] != silo]
out = list(hdr)
for b in keep:
    out += b["lines"]
if mode == "add":
    now = int(time.time())
    out += [
        f"  - name: {silo}", "    uid: 1000", "    state: Created",
        "    autostart: false", f"    created_at: {now}",
        f"    last_change: {now}", "    kind: tier2-template", "    launch:",
        f"      workload: {workload}", f"      template_silo: {silo}",
        "      network: pasta", '      argv: ["sleep", "infinity"]',
    ]
elif not keep:
    out.append("  []")
tmp = path + ".tmp"
open(tmp, "w").write("\n".join(out) + "\n")
os.replace(tmp, path)
PY
}

scenario_provision_silo() {
    [ "$(id -u)" = 0 ] || fail provision-silo "must run as root (silos.yaml + daemon restart)"
    [ -f "$LOGIN_SITE" ] || fail provision-silo "login site not staged at $LOGIN_SITE"

    # CI-network accommodation. The qci VM is itself a qemu user-net guest, so
    # a rootless podman `--network=pasta` container needs pasta options to
    # reach the VM host (where the test login site binds 0.0.0.0:8099).
    # `--map-gw` redirects the pasta gateway to host loopback; additionally
    # `--map-host-loopback=10.0.2.2` keeps the historical 10.0.2.2 address the
    # suite uses as BASE_HOST. A real silo on a bare-metal host needs no such
    # option (plain pasta egress works), so this is purely a nested-VM test
    # fixup: a tiny `podman` shim on PATH rewrites plain `--network=pasta` to
    # the mapped form (NetworkMode still contains `pasta`, so the suite's
    # config-claim assertion stays honest).
    cat > /usr/local/bin/podman <<'WRAP'
#!/bin/bash
# Nested-VM pasta host reachability: plain --network=pasta cannot reach the
# guest host listener; map gateway + 10.0.2.2 to host loopback. Pass
# everything else (--network=none / already-optioned pasta / unrestricted)
# through untouched.
args=()
for a in "$@"; do
    if [ "$a" = "--network=pasta" ]; then
        args+=("--network=pasta:--map-gw,--map-host-loopback=10.0.2.2")
    else
        args+=("$a")
    fi
done
exec /usr/bin/podman "${args[@]}"
WRAP
    chmod 0755 /usr/local/bin/podman

    # The admin-run template CLIs (build/validate/promote/gc) write
    # /var/lib/qdistro/audit/template_audit.sqlite, but that dir is the SHARED
    # security audit store, created 0700 qdistro-pwd by install-pwd-for-vm.sh
    # (the privileged daemons write their DBs there via root bypass; admin
    # cannot). Rather than STEAL it (chown admin → an unprivileged user could
    # unlink/replace the root/pwd audit DBs), make it sticky world-writable
    # (1777, like /tmp): admin can create template_audit.sqlite, while the sticky
    # bit stops it from unlinking pwd/broker DBs (still 0600, owner-only). Owner
    # is left unchanged. This is a TEST-only accommodation in a disposable VM —
    # production keeps the audit DB out of admin's hands (template audit there
    # uses a private QDISTRO_VAR_DIR or the daemon's root-bypass writes).
    install -d /var/lib/qdistro/audit
    chmod 1777 /var/lib/qdistro/audit

    # The login site is a REAL system unit (Restart=always, no start-limit) so it
    # survives every admin login session AND a session-manager restart, holding
    # its breakage + issued-session state for the whole suite (a per-scenario
    # restart would forget the A-era cookie the rollback scenario proves). A
    # transient systemd-run unit did NOT survive in practice.
    cat > /etc/systemd/system/fp2-login.service <<UNIT
[Unit]
Description=fableplan2 task-06 browser-demo login site (test fixture)
[Service]
ExecStart=/usr/bin/python3 $LOGIN_SITE --port $LOGIN_PORT --bind 0.0.0.0
Restart=always
RestartSec=1
StartLimitIntervalSec=0
[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl reset-failed fp2-login.service 2>/dev/null || true
    systemctl restart fp2-login.service \
        || fail provision-silo "could not start the login site service"
    local i
    for i in $(seq 1 20); do
        python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1);
sys.exit(0 if s.connect_ex(('127.0.0.1',$LOGIN_PORT))==0 else 1)" && break
        sleep 0.5
    done
    python3 -c "import socket,sys; s=socket.socket(); s.settimeout(1);
sys.exit(0 if s.connect_ex(('127.0.0.1',$LOGIN_PORT))==0 else 1)" \
        || fail provision-silo "login site not listening on 127.0.0.1:$LOGIN_PORT"

    systemctl stop qdistro-session-manager.service 2>/dev/null || true
    _silos_yaml_edit add || fail provision-silo "could not write silos.yaml row"
    systemctl start qdistro-session-manager.service \
        || fail provision-silo "session manager did not restart"
    local i
    for i in $(seq 1 30); do
        systemctl is-active --quiet qdistro-session-manager.service && break
        sleep 1
    done
    systemctl is-active --quiet qdistro-session-manager.service \
        || fail provision-silo "session manager not active after restart"

    # Broker: allow the silo spawn gate so the launch path can create the
    # qdistro-silo-browserdemo container (default-deny since edb7f32). Mirrors
    # tier2-silo-secctx-wiretag-probe.sh: write a test-owned allow rule, reload
    # the broker, and confirm CheckPermission flips to allow (as admin, the uid
    # the root-launcher runs the gate as) before any scenario launches the silo.
    install -d -m 0755 "$RULE_DIR"
    cat >"$RULE_FILE" <<EOF
# Test-authored (templates-browser): allow the tier-2 browser silo spawn so the
# browser-rollback demo can drive the real launch path. Removed in deprovision.
- name: silo-browserdemo-allow
  decision: allow
  match:
    action: $SPAWN_ACTION
EOF
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    local reply=""
    for i in $(seq 1 20); do
        reply=$(broker_check_admin "$SPAWN_ACTION")
        [ "$reply" = "allow" ] && break
        sleep 0.25
    done
    [ "$reply" = "allow" ] \
        || fail provision-silo "broker did not load the silo allow rule (CheckPermission='$reply' for $SPAWN_ACTION)"

    pass "provision-silo"
}

scenario_deprovision_silo() {
    [ "$(id -u)" = 0 ] || fail deprovision-silo "must run as root"
    systemctl stop fp2-login.service 2>/dev/null || true
    rm -f /etc/systemd/system/fp2-login.service /usr/local/bin/podman "$RULE_FILE"
    systemctl reload-or-restart qdistro-admin-broker.service 2>/dev/null || true
    systemctl daemon-reload 2>/dev/null || true
    systemctl stop qdistro-tier2-silo@"$SILO".service 2>/dev/null || true
    systemctl stop qdistro-session-manager.service 2>/dev/null || true
    _silos_yaml_edit remove 2>/dev/null || true
    systemctl start qdistro-session-manager.service 2>/dev/null || true
    pass "deprovision-silo"
}

# =========================================================================
# ADMIN scenarios.
# =========================================================================
scenario_setup() {
    [ "$(id -u)" = 1000 ] || fail setup "must run as admin (uid 1000)"
    command -v podman >/dev/null 2>&1 || fail setup "podman not available for admin"
    command -v chromium >/dev/null 2>&1 || true   # chromium lives in the image, not the host
    rm -rf "$STATE"; mkdir -p "$STATE/etc/templates"
    # The login site is a system service started by provision-silo; just confirm
    # it is reachable and clear any prior breakage/sessions for a clean run.
    site_ctl "/__reset" >/dev/null 2>&1 \
        || fail setup "login site not reachable on 127.0.0.1:$LOGIN_PORT (provision-silo starts it)"

    ensure_browser_policy
    build_validate_promote_A
    local genA; genA="$(active_gen)"
    case "$genA" in sha256:*) ;; *) fail setup "active_generation is not a digest: $genA" ;; esac
    echo "$genA" > "$STATE/genA"
    marker_of_image "$genA" > "$STATE/markerA"
    [ -s "$STATE/markerA" ] || fail setup "could not read genA image marker"

    assert_site_reachable "$genA" setup
    pass "setup (genA=$genA base=$(base_url))"
}

# Scenario 1 — Browser baseline.
scenario_baseline() {
    local genA markerA url; genA="$(cat "$STATE/genA")"; markerA="$(cat "$STATE/markerA")"
    launch_silo baseline
    # The running silo is bound to genA's DIGEST (never a tag).
    [ "$(container_image_digest)" = "$genA" ] \
        || fail baseline "silo not running genA digest (img=$(container_image_digest) genA=$genA)"
    # Config claim: pasta egress (Podman 6 rootless backend).
    podman inspect --format '{{.HostConfig.NetworkMode}}' "$CONTAINER" 2>/dev/null \
        | grep -q pasta || fail baseline "silo not on --network=pasta"
    # task-01 state mount: /home/admin is the state bind, .cache is tmpfs on top.
    local mi; mi="$(podman exec "$CONTAINER" cat /proc/self/mountinfo 2>/dev/null)"
    echo "$mi" | grep -qE ' /home/admin ' || fail baseline "/home/admin is not a mount (state bind missing)"
    echo "$mi" | grep -E ' /home/admin/\.cache ' | grep -qi tmpfs \
        || fail baseline "/home/admin/.cache is not tmpfs (shadowing regression)"

    url="$(base_url)"
    # Render check (file://): ephemeral profile + retries (see assert_silo_file_render).
    assert_silo_file_render baseline "$(ua_for "$markerA")" "RENDER-OK-BASELINE"

    # Log in to the login site through the running silo: perform the navigation
    # flow (cookie persisted into the profile under state_path), then read /home.
    local dom
    ensure_profile_free
    silo_chromium 60 "$(ua_for "$markerA")" "$url/login" >/dev/null 2>&1 || true
    ensure_profile_free
    dom="$(silo_chromium 60 "$(ua_for "$markerA")" "$url/home")"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' || fail baseline "fresh login on genA did not reach LOGIN-OK (dom: $(echo "$dom" | tr -d '\n' | head -c 200))"
    # The session cookie now lives in the profile under the persistent state_path.
    # Chromium's Cookies SQLite path drifts by version (Default/Cookies vs
    # Default/Network/Cookies); search for any non-empty one rather than pin it
    # (the LOGIN-OK DOM sentinel above is the behavioural proof; this asserts
    # persistence to disk).
    podman exec "$CONTAINER" sh -c 'find '"$PROFILE"' -name Cookies -type f -size +0c | grep -q .' \
        || fail baseline "no non-empty Cookies DB persisted in the profile"
    [ -d "$STATE_PATH_DEFAULT/profile" ] || fail baseline "profile dir not present under host state_path"
    pass "baseline"
}

# Scenario 2 — State isolation (a candidate/validate-style container cannot
# reach the silo's profile/cookie).
scenario_state_isolation() {
    local genA; genA="$(cat "$STATE/genA")"
    # Plant a sentinel inside the live silo profile.
    podman exec "$CONTAINER" sh -c 'echo SILO-SECRET-COOKIE > '"$PROFILE"'/sentinel' \
        || fail state-isolation "could not plant sentinel in silo profile"
    # A candidate-isolation launch (same flags validate uses: read-only,
    # network=none, NO state bind) must not see the silo state.
    local seen
    seen="$(podman run --rm --network=none --read-only --entrypoint= --tmpfs /tmp:rw,size=16m \
            --mount type=tmpfs,destination=/home/admin/.cache,tmpfs-size=8m,tmpfs-mode=0700,U \
            "$genA" sh -c 'cat /home/admin/profile/sentinel 2>/dev/null || echo NO-STATE')"
    [ "$seen" = "NO-STATE" ] || fail state-isolation "a candidate runtime read the silo profile sentinel: '$seen'"
    # And its mountinfo carries no qdistro state mount. Guard the run itself:
    # if the candidate container failed to start, $mounts would be empty and the
    # grep would miss, passing the isolation check vacuously.
    local mounts
    mounts="$(podman run --rm --network=none --read-only --entrypoint= --tmpfs /tmp:rw,exec,size=64m \
              "$genA" cat /proc/self/mountinfo 2>/dev/null)" \
        || fail state-isolation "candidate mountinfo run failed (cannot assert isolation)"
    [ -n "$mounts" ] || fail state-isolation "candidate mountinfo was empty (run did not produce /proc/self/mountinfo)"
    echo "$mounts" | grep -qiE 'qdistro/(silos|bindings|pins)|'"$SILO" \
        && fail state-isolation "a candidate runtime has a qdistro state mount"
    podman exec "$CONTAINER" sh -c 'rm -f '"$PROFILE"'/sentinel' >/dev/null 2>&1 || true
    pass "state-isolation"
}

# Scenario 3 — Update flip.
scenario_update_flip() {
    local genA markerA; genA="$(cat "$STATE/genA")"; markerA="$(cat "$STATE/markerA")"
    build_validate_promote_B
    local genB; genB="$(active_gen)"
    [ "$genB" != "$genA" ] || fail update-flip "genB digest == genA digest"
    echo "$genB" > "$STATE/genB"
    marker_of_image "$genB" > "$STATE/markerB"
    [ -s "$STATE/markerB" ] || fail update-flip "could not read genB marker"
    [ "$(cat "$STATE/markerA")" != "$(cat "$STATE/markerB")" ] \
        || fail update-flip "marker_A == marker_B (gen B did not change the marker)"
    # The already-running container is STILL genA (flip takes effect at restart).
    [ "$(container_image_digest)" = "$genA" ] \
        || fail update-flip "running silo flipped before restart (img=$(container_image_digest))"
    # The status CLI reports a pending restart for THIS silo (bound != running).
    cli template-status 2>/dev/null | grep "silo=$SILO " | grep -q "restart_pending=yes" \
        || fail update-flip "qdistro-template-status does not show restart_pending=yes for $SILO after promote-without-restart"
    # Restart the silo: B activates here. The pre-activation snapshot is taken on
    # THIS launch (resolve-binding --record), capturing the OUTGOING (A) state
    # BEFORE B's first write — NOT at promote time.
    stop_silo
    launch_silo update-flip
    [ "$(container_image_digest)" = "$genB" ] \
        || fail update-flip "silo did not flip to genB on restart (img=$(container_image_digest))"
    # The outgoing generation (A) now carries the pre-migration-snapshot pin and
    # a pre-activation state snapshot exists (task 05; the A-era cookie lives in
    # it, which is what rollback --restore-state brings back).
    [ -f "/var/lib/qdistro/pins/$TEMPLATE/$genA/pre-migration-snapshot.toml" ] \
        || fail update-flip "no pre-migration-snapshot pin on the outgoing generation A after B activated"
    ls -d /var/lib/qdistro/silos/"$SILO"/state-snapshots/*/snapshot >/dev/null 2>&1 \
        || fail update-flip "no pre-activation state snapshot was taken on B's activation"
    local url dom markerB; url="$(base_url)"; markerB="$(cat "$STATE/markerB")"
    # genB renders pages fine (ephemeral profile + retries; full-CI OOM of a
    # single dump-dom is the observed flake mode — keep the sentinel assertion).
    assert_silo_file_render update-flip "$(ua_for "$markerB")" "RENDER-OK-GENB"
    # The persisted A-era cookie still authenticates at /home under genB.
    ensure_profile_free
    dom="$(silo_chromium 90 "$(ua_for "$markerB")" "$url/home")"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' \
        || fail update-flip "A-era session cookie did not survive the flip to genB (dom: $(echo "$dom" | tr -d '\n' | head -c 200))"
    # Plant a B-ERA-ONLY sentinel in the live profile. The A-era snapshot was
    # taken BEFORE B's first write, so this file exists ONLY in B-era state —
    # the rollback scenario uses it to prove `--restore-state` actually swaps
    # content back to A (not merely leaves B-era state in place): it must be
    # ABSENT from the restored profile and PRESENT in the displaced
    # state-rejected-*. (The A-era cookie alone cannot prove this — it lives in
    # both A-era and B-era state.)
    podman exec "$CONTAINER" sh -c 'echo B-ERA-ONLY > '"$PROFILE"'/b-era-sentinel' \
        || fail update-flip "could not plant the B-era sentinel"
    pass "update-flip"
}

# Scenario 4 — Broken update never lands.
scenario_broken_update() {
    local genA genB; genA="$(cat "$STATE/genA")"; genB="$(cat "$STATE/genB")"
    local before; before="$(active_gen)"
    [ "$before" = "$genB" ] || fail broken-update "precondition: active should be genB"
    # A candidate with Chromium sabotaged: a layer FROM genB that makes chromium
    # exit nonzero (deterministic, unlike font removal). page-open must fail.
    local bdir="$STATE/recipeBroken"; mkdir -p "$bdir"
    cat > "$bdir/Containerfile.$TEMPLATE" <<CF
FROM $genB
USER root
RUN printf '#!/bin/sh\nexit 7\n' > /usr/bin/chromium && chmod 0755 /usr/bin/chromium
USER 1000:1000
CF
    # Same template name (so the candidate lives under tier2-browser); just a
    # different recipe. process-starts (chromium --version) exits 7 -> validation
    # fails before page-open even runs.
    write_browser_policy "$bdir/Containerfile.$TEMPLATE" "unrestricted"
    local out rid; out="$(cli template-build "$TEMPLATE" 2>&1)" || fail broken-update "broken build errored: $out"
    rid="$(echo "$out" | sed -n 's/^RUN_ID=//p')"
    [ -n "$rid" ] || fail broken-update "no RUN_ID for broken candidate"
    if cli template-validate "$rid" >/dev/null 2>&1; then
        fail broken-update "sabotaged candidate validated (page-open should fail)"
    fi
    local cdir="/var/lib/qdistro/templates/$TEMPLATE/candidates/$rid"
    [ "$(cat "$cdir/state" 2>/dev/null)" = failed ] || fail broken-update "candidate state != failed"
    [ -f "$cdir/evidence/validation.toml" ] || fail broken-update "no validation evidence for the failed candidate"
    echo "$rid" > "$STATE/broken_rid"
    # promote must REFUSE a non-validated candidate; the binding stays genB.
    if cli template-promote "$SILO" "$rid" >/dev/null 2>&1; then
        fail broken-update "promote accepted a failed candidate"
    fi
    [ "$(active_gen)" = "$genB" ] || fail broken-update "binding changed on a refused promote"
    audit_has template.promote.refused run_id "$rid" \
        || fail broken-update "no template.promote.refused audit row for $rid"
    # The running silo (genB) is untouched.
    [ "$(container_image_digest)" = "$genB" ] \
        || fail broken-update "running silo disturbed by a refused promote"
    pass "broken-update"
}

# Scenario 5 — Post-update login regression (the requested demo).
scenario_login_regression() {
    local genA genB markerA markerB url
    genA="$(cat "$STATE/genA")"; genB="$(cat "$STATE/genB")"
    markerA="$(cat "$STATE/markerA")"; markerB="$(cat "$STATE/markerB")"; url="$(base_url)"
    # genB passed validation and renders (proven in update-flip) — yet the
    # breakage lives server-side, INVISIBLE to pre-promotion probes.
    site_ctl "/__break?mode=reject-login&marker=$markerB" | grep -q BREAK-SET \
        || fail login-regression "could not arm reject-login for genB marker"
    # A fresh login as genB (new ephemeral profile) is REJECTED at /auth: the
    # distinct sentinel proves /auth ran and refused the updated browser (not a
    # generic flow failure).
    local dom
    dom="$(throwaway_chromium 90 "$genB" "$(ua_for "$markerB")" "$url/login" "$url/home")"
    echo "$dom" | grep -q 'LOGIN-FAILED-reject-login' \
        || fail login-regression "fresh genB login was not rejected at /auth (dom: $(echo "$dom" | tr -d '\n' | head -c 200))"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' \
        && fail login-regression "genB fresh login unexpectedly succeeded under reject-login"
    # The A-era session cookie in the SILO profile still reaches /home (the
    # breakage is the login flow, not the session) — the value of rollback.
    ensure_profile_free
    dom="$(silo_chromium 60 "$(ua_for "$markerB")" "$url/home")"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' \
        || fail login-regression "the A-era session cookie stopped reaching /home (dom: $(echo "$dom" | tr -d '\n' | head -c 200))"
    # And gen A (marker A) still logs in fresh against the SAME server (keyed,
    # not global breakage).
    dom="$(throwaway_chromium 90 "$genA" "$(ua_for "$markerA")" "$url/login" "$url/home")"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' \
        || fail login-regression "gen A stopped working — breakage is not keyed to the marker"
    pass "login-regression"
}

# Scenario 6 — Breakage matrix (js-break + slow-auth), keyed to genB.
scenario_breakage_matrix() {
    local genA genB markerA markerB url
    genA="$(cat "$STATE/genA")"; genB="$(cat "$STATE/genB")"
    markerA="$(cat "$STATE/markerA")"; markerB="$(cat "$STATE/markerB")"; url="$(base_url)"

    slowcheck_cleanup_regression "$genB"

    # js-break: the page RENDERS (banner present, screenshot non-uniform) but
    # the flow dies; only the DOM sentinel catches it.
    site_ctl "/__break?mode=js-break&marker=$markerB" | grep -q BREAK-SET \
        || fail breakage-matrix "could not arm js-break"
    local dom; dom="$(throwaway_chromium 90 "$genB" "$(ua_for "$markerB")" "$url/login" "$url/home")"
    echo "$dom" | grep -q 'LOGIN-FAILED' \
        || fail breakage-matrix "js-break: expected LOGIN-FAILED at /home (flow should not have navigated)"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' \
        && fail breakage-matrix "js-break: login unexpectedly succeeded"
    # Prove the /login page still RENDERS under js-break (screenshot non-uniform)
    # — DOM assertions catch what a screenshot cannot.
    local lpage; lpage="$(throwaway_chromium 60 "$genB" "$(ua_for "$markerB")" "$url/login")"
    echo "$lpage" | grep -q 'QDISTRO-LOGIN-FORM' \
        || fail breakage-matrix "js-break: the login page did not even render"
    # gen A still works under js-break (keyed).
    dom="$(throwaway_chromium 90 "$genA" "$(ua_for "$markerA")" "$url/login" "$url/home")"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' || fail breakage-matrix "js-break leaked onto gen A"

    # slow-auth: the check must TIME OUT (reported as a failure, not a hang) and
    # the HARNESS must leave no container behind. Start the named check detached
    # and first require the fixture's direct signal that /auth entered the
    # deliberate stall. Then bound `podman wait`, not `podman run`: killing an
    # attached run client races Podman's --rm cleanup and made the live-at-
    # deadline assertion depend on signal scheduling under full-CI load.
    site_ctl "/__break?mode=slow-auth&marker=$markerB" | grep -q BREAK-SET \
        || fail breakage-matrix "could not arm slow-auth"
    local rc status="" i logs
    status="$(site_ctl "/__slow_auth_status?marker=$markerB" 2>/dev/null || true)"
    [ "$status" = "SLOW-AUTH-WAITING-$markerB" ] \
        || fail breakage-matrix "slow-auth: readiness was stale before the browser started (status=${status:-<none>})"
    cleanup_slowcheck \
        || fail breakage-matrix "slow-auth pre-clean could not establish container absence"
    start_throwaway_chromium fp2-slowcheck "$genB" "$(ua_for "$markerB")" \
        "$url/login" "$url/home" >/dev/null \
        || fail breakage-matrix "slow-auth: could not start the named browser check"
    # From this exact point until verified removal, every exit path owns cleanup.
    trap cleanup_slowcheck_on_exit EXIT
    for ((i = 0; i < 40; i++)); do
        status="$(site_ctl "/__slow_auth_status?marker=$markerB" 2>/dev/null || true)"
        [ "$status" = "SLOW-AUTH-ENTERED-$markerB" ] && break
        if ! podman ps --filter "name=fp2-slowcheck" -q | grep -q .; then
            logs="$(podman logs fp2-slowcheck 2>&1 | tr '\n' ' ' | head -c 400)"
            fail breakage-matrix "slow-auth: browser exited before /auth entered the stall (status=${status:-<none>}; logs=${logs:-<none>})"
        fi
        sleep 0.25
    done
    [ "$status" = "SLOW-AUTH-ENTERED-$markerB" ] \
        || fail breakage-matrix "slow-auth: /auth did not enter the marker-keyed stall (status=${status:-<none>})"

    timeout --kill-after=2 5 podman wait fp2-slowcheck >/dev/null 2>&1
    rc=$?
    # (1) A stall surfaces as a FAILURE within the wall-clock budget, not an
    # infinite hang: the host-side deadline fires with timeout's exact status.
    [ "$rc" -eq 124 ] || fail breakage-matrix "slow-auth check did not time out (rc=$rc, expected 124) — a stall must surface as a failure"
    # (2) The stall held a LIVE container at the timeout (proof /auth really
    # stalled chromium mid-request, not that it exited early). The direct
    # fixture readiness signal above rules out a pre-request startup hang.
    podman ps --filter "name=fp2-slowcheck" -q | grep -q . \
        || fail breakage-matrix "slow-auth: expected a live stalled container at the timeout (the request did not stall mid-flight)"
    # (3) The harness reclaims it deterministically — `leaves no containers
    # behind` is achieved by the harness's teardown, and the container is not
    # wedged/unkillable.
    cleanup_slowcheck \
        || fail breakage-matrix "slow-auth container survived removal or absence could not be verified"
    # Disarm only after cleanup_slowcheck established the exact container is
    # absent (exists rc=1); later scenario failures no longer own this resource.
    trap - EXIT
    # gen A still logs in under slow-auth (keyed).
    dom="$(throwaway_chromium 90 "$genA" "$(ua_for "$markerA")" "$url/login" "$url/home")"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' || fail breakage-matrix "slow-auth leaked onto gen A"
    # Clear ONLY the breakage (keep the A-era session for the rollback proof).
    site_ctl "/__clearbreak" >/dev/null 2>&1 || true
    pass "breakage-matrix"
}

# Scenario 7 — Rollback with state restores the working pair.
scenario_rollback() {
    local genA genB markerA url
    genA="$(cat "$STATE/genA")"; genB="$(cat "$STATE/genB")"
    markerA="$(cat "$STATE/markerA")"; url="$(base_url)"
    # Prerequisite: update-flip must have planted the B-ERA-ONLY sentinel in the
    # LIVE state before we roll back. If it's absent here, update-flip never
    # completed (e.g. an upstream baseline/launch failure) — fail with THAT
    # prerequisite rather than the misleading "rollback lost displaced state"
    # assertion below, which would blame the rollback for a missing setup.
    [ -f "$STATE_PATH_DEFAULT/profile/b-era-sentinel" ] \
        || fail rollback "prerequisite: B-era sentinel absent from live state before rollback — update-flip did not complete (check baseline/update-flip scenarios upstream)"
    # Clear breakage but KEEP issued sessions — the restored A-era cookie must
    # still be honoured at /home to prove the rollback.
    site_ctl "/__clearbreak" >/dev/null 2>&1 || true
    local rev_before; rev_before="$(binding_get identity_revision)"
    # Snapshot the pre-existing state-rejected-* set so the content proof below
    # asserts on the dir THIS rollback creates — not a stale one from an earlier
    # run on a reused VM (deprovision edits silos.yaml only; it does not clean
    # /var/lib/qdistro/silos/<silo>).
    local rej_before; rej_before="$(ls -d "$STATE_PATH_DEFAULT"-rejected-* /var/lib/qdistro/silos/"$SILO"/state-rejected-* 2>/dev/null | sort)"
    # Stop the silo, then roll back to A WITH its state (the A-era snapshot).
    stop_silo
    cli template-promote "$SILO" --rollback "$genA" --restore-state >/dev/null 2>&1 \
        || fail rollback "rollback --restore-state to genA failed"
    [ "$(active_gen)" = "$genA" ] || fail rollback "active != genA after rollback"
    # identity_revision bumped.
    local rev_after; rev_after="$(binding_get identity_revision)"
    [ "$rev_after" = "$((rev_before + 1))" ] || fail rollback "identity_revision did not bump ($rev_before -> $rev_after)"
    # The displaced genB-era state is kept aside as a NEW state-rejected-* (the
    # one this rollback just created, not a stale leftover).
    local rej_after rej
    rej_after="$(ls -d "$STATE_PATH_DEFAULT"-rejected-* /var/lib/qdistro/silos/"$SILO"/state-rejected-* 2>/dev/null | sort)"
    rej="$(comm -13 <(printf '%s\n' "$rej_before") <(printf '%s\n' "$rej_after") | grep -v '^$' | head -1)"
    [ -n "$rej" ] || fail rollback "rollback did not create a NEW state-rejected-* (displaced genB state not preserved)"
    # CONTENT proof (not just displacement): the B-ERA-ONLY sentinel planted in
    # update-flip must be ABSENT from the restored (A-era) live state — proving
    # --restore-state actually swapped content back to the A-era snapshot, not
    # merely flipped the binding and left B-era state in place — and PRESENT in
    # the displaced state-rejected-* (B-era state preserved, not lost). The
    # A-era cookie alone can't prove this (it lives in both A- and B-era state).
    [ -f "$rej/profile/b-era-sentinel" ] \
        || fail rollback "B-era sentinel not in state-rejected-* (displaced B-era state was lost)"
    # Relaunch on genA: a FRESH login succeeds again AND the restored profile's
    # cookie reaches /home WITHOUT re-login.
    launch_silo rollback
    [ "$(container_image_digest)" = "$genA" ] \
        || fail rollback "silo not running genA after rollback (img=$(container_image_digest))"
    podman exec "$CONTAINER" sh -c 'test ! -e '"$PROFILE"'/b-era-sentinel' \
        || fail rollback "B-era sentinel leaked into the restored A-era profile (--restore-state did not swap content back to A)"
    # restored cookie -> /home OK without logging in:
    ensure_profile_free
    local dom; dom="$(silo_chromium 60 "$(ua_for "$markerA")" "$url/home")"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' \
        || fail rollback "restored A-era cookie did not reach /home after rollback (dom: $(echo "$dom" | tr -d '\n' | head -c 200))"
    # a fresh login also succeeds (software and matching state are back together):
    ensure_profile_free
    silo_chromium 60 "$(ua_for "$markerA")" "$url/login" >/dev/null 2>&1 || true
    ensure_profile_free
    dom="$(silo_chromium 60 "$(ua_for "$markerA")" "$url/home")"
    echo "$dom" | grep -qE 'LOGIN-OK-[0-9a-f]+' || fail rollback "fresh login on the rolled-back genA failed"
    # Audit rows for the WHOLE arc (spec scenario 7): promote.applied for both
    # the forward flip (->B) and the rollback (->A); the binding.activated that
    # fired when B first launched; the pre-activation snapshot created (incoming
    # B) and restored (outgoing A); and the earlier promote.refused.
    audit_has template.promote.applied new_generation "$genB" \
        || fail rollback "missing promote.applied audit row for the forward flip to genB"
    audit_has template.promote.applied new_generation "$genA" \
        || fail rollback "missing promote.applied audit row for the rollback to genA"
    audit_has template.binding.activated generation "$genB" \
        || fail rollback "missing binding.activated audit row for genB's first launch"
    audit_has template.state_snapshot.created new_generation "$genB" \
        || fail rollback "missing state_snapshot.created audit row (pre-activation snapshot on B)"
    audit_has template.state_snapshot.restored new_generation "$genA" \
        || fail rollback "missing state_snapshot.restored audit row (A-era state restored)"
    audit_has template.promote.refused run_id "$(cat "$STATE/broken_rid")" \
        || fail rollback "missing promote.refused audit row"
    pass "rollback"
}

# Scenario 8 — GC respects the story.
scenario_gc() {
    local genA genB brid; genA="$(cat "$STATE/genA")"; genB="$(cat "$STATE/genB")"
    brid="$(cat "$STATE/broken_rid")"
    cat > "$QDISTRO_ETC_DIR/template-retention.toml" <<'RET'
keep_promoted_generations = 0
keep_promoted_generations_vm = 0
failed_candidate_days = 0
build_log_days = 180
audit_evidence_years = 3
RET
    # The sabotaged candidate's payload is collected by DIGEST; its evidence +
    # any screenshot survive.
    local bcdir="/var/lib/qdistro/templates/$TEMPLATE/candidates/$brid"
    local bdigest; bdigest="$(python3 -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb"))["generation_ref"])' "$bcdir/manifest.toml" 2>/dev/null)" \
        || fail gc "no broken-candidate manifest digest"
    # The sabotaged payload must EXIST before gc, else its post-gc absence is a
    # vacuous pass (an unrelated earlier disappearance would also satisfy it).
    podman image exists "$bdigest" \
        || fail gc "broken-candidate image $bdigest is absent BEFORE gc (cannot prove collection)"
    # Spec scenario 8: an EXPIRED state snapshot is deleted (payload gone, only
    # audit metadata kept) and drops out of rollback choices. Back-date one
    # snapshot's OWN expiry to the past so gc collects it by its own window.
    local snapdir="/var/lib/qdistro/silos/$SILO/state-snapshots"
    local expid exppayload
    expid="$(python3 - "$snapdir" <<'PY'
import os, re, sys
root = sys.argv[1]
ids = sorted(d for d in os.listdir(root)
             if os.path.isfile(os.path.join(root, d, "meta.toml"))
             and os.path.isdir(os.path.join(root, d, "snapshot"))) \
      if os.path.isdir(root) else []
if not ids:
    sys.exit(0)
sid = ids[0]           # oldest snapshot whose payload still exists
meta = os.path.join(root, sid, "meta.toml")
txt = open(meta).read()
txt = re.sub(r'expires_at = "[^"]*"', 'expires_at = "2000-01-01T00:00:00Z"', txt)
open(meta, "w").write(txt)
print(sid)
PY
)"
    [ -n "$expid" ] || fail gc "no state snapshot present to expire-test"
    exppayload="$snapdir/$expid/snapshot"
    [ -e "$exppayload" ] || fail gc "expired snapshot payload missing before gc"

    cli template-gc >/dev/null 2>&1 || fail gc "template-gc errored"

    # A (rollback target / active) and B (rollback window) survive their pins —
    # NOT cascade-collected when the gen-B-derived broken candidate is rmi'd.
    podman image exists "$genA" || fail gc "active genA collected"
    podman image exists "$genB" || fail gc "pinned rollback target genB collected"
    if podman image exists "$bdigest" 2>/dev/null; then
        fail gc "sabotaged candidate payload (digest) was not collected"
    fi
    for ev in state evidence/validation.toml; do
        [ -e "$bcdir/$ev" ] || fail gc "broken-candidate evidence $ev was deleted"
    done
    # The expired snapshot's user-state PAYLOAD is gone, but its meta survives
    # marked dead (honesty: no "evidence outlives payload" for user state), and
    # it no longer offers itself as a restore choice.
    [ ! -e "$exppayload" ] || fail gc "expired state snapshot payload was not collected"
    python3 -c 'import sys,tomllib; m=tomllib.load(open(sys.argv[1],"rb")); sys.exit(0 if m.get("restore_eligible")=="false" and m.get("deleted_at") else 1)' \
        "$snapdir/$expid/meta.toml" \
        || fail gc "expired snapshot meta not marked restore_eligible=false + deleted_at"
    pass "gc"
}

scenario_teardown() {
    # The login site is a system service (stopped by deprovision-silo, root).
    stop_silo 2>/dev/null || true
    pass "teardown"
}

main() {
    case "${1:-}" in
        provision-silo)    scenario_provision_silo ;;
        deprovision-silo)  scenario_deprovision_silo ;;
        setup)             scenario_setup ;;
        baseline)          scenario_baseline ;;
        state-isolation)   scenario_state_isolation ;;
        update-flip)       scenario_update_flip ;;
        broken-update)     scenario_broken_update ;;
        login-regression)  scenario_login_regression ;;
        breakage-matrix)   scenario_breakage_matrix ;;
        rollback)          scenario_rollback ;;
        gc)                scenario_gc ;;
        teardown)          scenario_teardown ;;
        *) echo "unknown scenario: ${1:-}" >&2; exit 2 ;;
    esac
}

main "$@"
