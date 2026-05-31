#!/bin/bash
# qdistro-tier1-spawn — launch a sandboxed app under SELinux Tier-1.
# Skeleton (spec/30). Implementation pass fills in the TODO blocks.
#
# Architecture:
#
#   admin uid                                       <- caller, e.g. admin
#     │
#     ▼ qdistro-tier1-spawn <silo> -- <app...>
#     │
#     ▼ qdistro-secctx-exec --sandbox-engine qdistro.tier1
#     │                     --app-id qdistro.tier1.<silo>
#     │                     --instance-id tier1-<silo>-<pid>
#     │
#     ▼ qdistro-tier1-exec  (setexeccon staff_u:staff_r:qdistro_tier1_t)
#     │
#     ▼ <app...>            (running in qdistro_tier1_t)
#
# Two attestations: SELinux type for enforcement, secctx tag for
# routing (qdshell silo resolution).
#
# Usage:
#   qdistro-tier1-spawn <silo> -- <app...>
#
# Environment:
#   TIER1_TITLE_PREFIX     window title prefix (default "[tier1:<silo>] ")
#   TIER1_USE_SECCTX       1 (default) wraps via qdistro-secctx-exec.
#                          0 runs without a Wayland secctx tag (qdshell
#                          falls back to title-prefix silo resolution).
#   TIER1_SECCTX_ENGINE    override sandbox_engine (default qdistro.tier1)
#   TIER1_SECCTX_APPID     override app_id (default qdistro.tier1.<silo>)
#   TIER1_DEBUG=1          print resolved command before exec
#
# Exit code: app's natural exit code, or non-zero on bring-up failure.
#
# SPDX-License-Identifier: MIT
set -eo pipefail

if [ "$#" -lt 3 ] || [ "$2" != "--" ]; then
    cat >&2 <<EOF
usage: $0 <silo> -- <app...>

  <silo>  Free-form silo identifier (e.g. work, scratch).
          Must match qdshell's --silo-colors keys for chrome
          differentiation; see spec/30 §"Compatibility with
          wp_security_context_v1".
  <app>   The command + arguments to run inside the sandbox.

example:
  qdistro-tier1-spawn scratch -- /usr/bin/firefox --safe-mode
EOF
    exit 64
fi

SILO="$1"; shift
shift  # eat the literal "--"

if ! [[ "$SILO" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$ ]]; then
    echo "[tier1] FAIL: silo '$SILO' violates [A-Za-z0-9][A-Za-z0-9_-]{0,62}" >&2
    exit 1
fi

USE_SECCTX="${TIER1_USE_SECCTX:-1}"
ENGINE="${TIER1_SECCTX_ENGINE:-qdistro.tier1}"
APPID="${TIER1_SECCTX_APPID:-qdistro.tier1.$SILO}"
TITLE_PREFIX="${TIER1_TITLE_PREFIX:-[tier1:$SILO] }"

# Per-app private state directory, relabelled to qdistro_tier1_config_t
# by restorecon (spec/30 §"Filesystem labelling strategy" type-not-mount).
APP_STATE_DIR="$HOME/.local/share/qdistro/tier1/$SILO"
mkdir -p "$APP_STATE_DIR"
if command -v restorecon >/dev/null 2>&1; then
    restorecon -R "$APP_STATE_DIR" 2>/dev/null || true
fi

# Sanity: warn if SELinux is not in enforcing mode. Tier-1 still works
# in permissive (denials are logged but not enforced) which matches
# the fresh-clone bootstrap state — spec/30 §"Decision-blocking spikes"
# lists this as Spike 1 to verify on Tumbleweed.
# getenforce lives in /usr/sbin which isn't in non-admin users' default
# PATH on Tumbleweed; the absolute path makes the warning honest.
SE_MODE=$(/usr/sbin/getenforce 2>/dev/null || getenforce 2>/dev/null \
    || echo "Disabled")
if [ "$SE_MODE" = "Disabled" ]; then
    echo "[tier1] WARN: SELinux is Disabled — Tier-1 is no-op" >&2
elif [ "$SE_MODE" = "Permissive" ]; then
    echo "[tier1] WARN: SELinux is Permissive — denials logged not enforced" >&2
fi

# Verify the policy module is loaded.
if command -v semodule >/dev/null 2>&1; then
    if ! semodule -l 2>/dev/null | grep -q '^qdistro_tier1\b'; then
        echo "[tier1] WARN: qdistro_tier1 policy module not loaded;" \
             "spawning in inherited type" >&2
    fi
fi

# Mandatory broker spawn-action gate (spec/30 §"Phase plan" step 6).
# Admin must author explicit allow rules in /etc/qdistro/rules.d/ keyed
# on `qdistro.tier1.spawn:<canonical-app-path>`. Tier-1 launch is a
# security boundary: broker errors, empty replies, "unknown", or any
# verdict other than "allow" fail closed before qdistro-tier1-exec is
# reached. Resolve the executable before asking so an allow for
# /usr/bin/firefox does not also cover ./firefox or /tmp/firefox.
ORIG_APP="$1"
if [[ "$ORIG_APP" == */* ]]; then
    APP_CANDIDATE="$ORIG_APP"
else
    APP_CANDIDATE=$(command -v -- "$ORIG_APP" 2>/dev/null || true)
fi
if [ -z "$APP_CANDIDATE" ] || [ ! -x "$APP_CANDIDATE" ]; then
    echo "[tier1] FAIL: target executable not found or not executable: '$ORIG_APP'" >&2
    exit 1
fi
APP_PATH=$(readlink -f -- "$APP_CANDIDATE" 2>/dev/null || true)
if [ -z "$APP_PATH" ] || [ ! -f "$APP_PATH" ] || [ ! -x "$APP_PATH" ]; then
    echo "[tier1] FAIL: target executable could not be canonicalized: '$ORIG_APP'" >&2
    exit 1
fi
APP_BASENAME=$(basename -- "$APP_PATH")
SPAWN_ACTION="qdistro.tier1.spawn:$APP_PATH"
if ! command -v dbus-send >/dev/null 2>&1; then
    echo "[tier1] FAIL: dbus-send not found; broker authorization required" >&2
    exit 1
fi
set +e
BROKER_OUTPUT=$(dbus-send --system --print-reply=literal \
    --dest=org.qdistro.AdminBroker1 \
    /org/qdistro/AdminBroker1 \
    org.qdistro.AdminBroker1.CheckPermission \
    "string:$SPAWN_ACTION" \
    "dict:string:string:" 2>&1)
BROKER_STATUS=$?
set -e
BROKER_REPLY=$(printf '%s' "$BROKER_OUTPUT" | tr -d ' \t\n')
if [ "$BROKER_STATUS" -ne 0 ]; then
    echo "[tier1] FAIL: broker authorization failed for '$APP_BASENAME'" \
        "(action='$SPAWN_ACTION')" >&2
    [ "${TIER1_DEBUG:-0}" = "1" ] && printf '%s\n' "$BROKER_OUTPUT" >&2
    exit 1
fi
case "$BROKER_REPLY" in
    allow|string\"allow\")
        [ "${TIER1_DEBUG:-0}" = "1" ] && \
            echo "[tier1] broker allowed spawn of '$APP_BASENAME'" >&2
        ;;
    deny|string\"deny\")
        echo "[tier1] FAIL: broker denied spawn of '$APP_BASENAME'" \
            "(action='$SPAWN_ACTION' decision=deny)" >&2
        exit 1
        ;;
    unknown|string\"unknown\"|"")
        echo "[tier1] FAIL: broker has no allow rule for '$APP_BASENAME'" \
            "(action='$SPAWN_ACTION' decision=unknown)" >&2
        exit 1
        ;;
    *)
        echo "[tier1] FAIL: broker returned unsupported verdict for '$APP_BASENAME'" \
            "(action='$SPAWN_ACTION' reply='$BROKER_REPLY')" >&2
        exit 1
        ;;
esac

# qdistro-tier1-exec installs to libexecdir (meson default
# /usr/libexec), which isn't on $PATH for ordinary users. Resolve
# absolutely so the wrapper works regardless of who's invoking us.
TIER1_EXEC=""
for cand in \
    "${QDISTRO_TIER1_EXEC:-}" \
    "$(command -v qdistro-tier1-exec 2>/dev/null)" \
    /usr/libexec/qdistro-tier1-exec \
    /usr/local/libexec/qdistro-tier1-exec; do
    [ -n "$cand" ] && [ -x "$cand" ] && TIER1_EXEC="$cand" && break
done
if [ -z "$TIER1_EXEC" ]; then
    echo "[tier1] FAIL: qdistro-tier1-exec not found (PATH or libexec)" >&2
    exit 1
fi

CMD=("$TIER1_EXEC" --)
CMD+=("$@")

if [ "$USE_SECCTX" = "1" ] && command -v qdistro-secctx-exec >/dev/null 2>&1; then
    if [ "$(id -u)" -ne 0 ] && [ "${QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED:-0}" != "1" ]; then
        echo "[tier1] WARN: secctx stamping requires a direct root launcher parent;" >&2
        echo "        running untagged. Use the root launcher/broker path, or set" >&2
        echo "        QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1 only with QDWIN_SECCTX_OPEN=1 for dev tests." >&2
    else
        SECCTX_CMD=(
            env QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER=1
            qdistro-secctx-exec
            --sandbox-engine "$ENGINE"
            --app-id "$APPID"
            --instance-id "tier1-$SILO-$$"
            --
        )
        CMD=("${SECCTX_CMD[@]}" "${CMD[@]}")
    fi
fi

[ "${TIER1_DEBUG:-0}" = "1" ] && printf '[tier1] exec: %q ' "${CMD[@]}" >&2 \
    && echo >&2

# Title prefix for chrome differentiation when secctx isn't used or
# qdshell hasn't been extended with the tier-1 silo regex yet.
# qdshell parse_silo_from_title fallback consumes "[<silo>] " prefix.
exec env QDISTRO_TIER1_TITLE_PREFIX="$TITLE_PREFIX" \
    "${CMD[@]}"
