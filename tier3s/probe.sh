#!/bin/bash
# probe.sh — tier 3s PREREQUISITE SCREEN (not a capability proof).
#
#   probe.sh [--user NAME]        (default user: admin)
#
# Prints one line per check: `PASS|FAIL|INFO <check>: <detail>`. Exits 0 only
# if every required check passes; otherwise exits 1 and the final line names
# the FIRST missing prerequisite. Dev profile only (README O4): on any other
# profile — or when the profile cannot be determined — it refuses (exit 2).
# It never starts a sandbox: the podman checks use `podman create`
# against an empty scratch image (no runtime invocation, nothing started)
# and remove the container again.
set -uo pipefail

USER_NAME=admin
while [ $# -gt 0 ]; do
    case "$1" in
        --user) shift; USER_NAME="${1:?--user needs a value}" ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "probe: unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

HERE="$(cd "$(dirname "$0")" && pwd)"
PIN="$HERE/RUNSC_RELEASE"               # authoritative pin (the checked-in one)
# Unit-test hook ONLY: alternate root for /etc + /usr/libexec. In this mode
# every run is labelled TEST and a clean result exits 3, never 0, so a
# redirected probe can never be read as a host PASS.
ROOT="${QDISTRO_PROBE_ROOT:-}"
[ -z "$ROOT" ] || printf 'TEST MODE: QDISTRO_PROBE_ROOT=%s (not a host verdict)\n' "$ROOT"
RUNSC_DIR="$ROOT/usr/libexec/qdistro/runsc"
WRAPPER="$ROOT/usr/libexec/qdistro/tier3s-runsc"
STAMP="$ROOT/etc/qdistro/runsc-release"
PROFILE_FILE="$ROOT/etc/qdistro/profile"

FIRST_FAIL=""
pass() { printf 'PASS %s: %s\n' "$1" "$2"; }
info() { printf 'INFO %s: %s\n' "$1" "$2"; }
fail() { printf 'FAIL %s: %s\n' "$1" "$2"; [ -n "$FIRST_FAIL" ] || FIRST_FAIL="$1 ($2)"; }

# --- profile gate (refuse, do not screen) ---------------------------------
profile=""
[ -r "$PROFILE_FILE" ] && profile="$(sed -n 's/^QDISTRO_PROFILE=//p' "$PROFILE_FILE" | tail -1)"
if [ "$profile" != "dev" ]; then
    printf 'REFUSE profile: tier 3s is dev-profile only (README O4); %s says %s\n' \
        "$PROFILE_FILE" "${profile:-<missing>}"
    exit 2
fi
pass profile "dev ($PROFILE_FILE)"

# --- kernel ---------------------------------------------------------------
kv="$(uname -r)"; kmaj="${kv%%.*}"; kmin="${kv#*.}"; kmin="${kmin%%[!0-9]*}"
if [ "$kmaj" -gt 5 ] || { [ "$kmaj" -eq 5 ] && [ "$kmin" -ge 6 ]; }; then
    pass kernel ">= 5.6 ($kv)"; else fail kernel "$kv < 5.6"; fi

if [ -e /proc/sys/kernel/seccomp ]; then
    pass seccomp "/proc/sys/kernel/seccomp present (actions: $(cat /proc/sys/kernel/seccomp/actions_avail 2>/dev/null))"
else fail seccomp "kernel seccomp filter support absent (/proc/sys/kernel/seccomp missing)"; fi

mun="$(cat /proc/sys/user/max_user_namespaces 2>/dev/null || echo 0)"
if [ "$mun" -ge 2 ]; then pass userns "max_user_namespaces=$mun (minimum, not capacity)"
else fail userns "max_user_namespaces=$mun < 2"; fi

ps_scope="$(cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null || echo absent)"
if [ "$ps_scope" = absent ] || [ "$ps_scope" -le 2 ]; then pass ptrace_scope "$ps_scope (<= 2)"
else fail ptrace_scope "$ps_scope > 2 (systrap needs ptrace)"; fi

# --- launching user's id mapping ------------------------------------------
if ! id "$USER_NAME" >/dev/null 2>&1; then
    fail user "$USER_NAME does not exist"
    for c in subuid subgid; do fail "$c" "not checked: user $USER_NAME missing"; done
    for t in newuidmap newgidmap; do
        p="$(command -v "$t" 2>/dev/null)"
        if [ -n "$p" ] && [ -x "$p" ]; then pass "$t" "$p"; else fail "$t" "not installed (shadow package)"; fi
    done
else
    for db in subuid subgid; do
        if grep -q "^$USER_NAME:[0-9]*:[1-9][0-9]*$" "/etc/$db" 2>/dev/null; then
            pass "$db" "$(grep "^$USER_NAME:" "/etc/$db" | head -1)"
        else fail "$db" "no range for $USER_NAME in /etc/$db"; fi
    done
    for t in newuidmap newgidmap; do
        p="$(command -v "$t" 2>/dev/null)"
        if [ -n "$p" ] && [ -x "$p" ]; then pass "$t" "$p"
        else fail "$t" "not installed (shadow package)"; fi
    done
fi

# --- runsc bundle: compared against the checked-in pin, not the stamp -----
declare -A WANT=()
pin_get() { sed -n "s/^$1=//p" "$PIN" | tail -1; }
if [ ! -r "$PIN" ]; then
    fail pin "$PIN unreadable (run probe.sh from the qdistro tree)"
else
    pass pin "$PIN release=$(pin_get release)"
    WANT[runsc]="$(pin_get runsc_sha512)"
    while IFS='=' read -r k v; do
        [[ "$k" =~ ^sidecar_(.+)_sha512$ ]] && WANT["gvisor-bin/${BASH_REMATCH[1]}"]="$v"
    done < <(grep '^sidecar_' "$PIN")
fi
sha() { sha512sum "$1" 2>/dev/null | cut -d' ' -f1; }
if [ ! -r "$PIN" ]; then
    fail runsc "not checked: no pin"
elif [ ! -e "$STAMP" ]; then
    fail runsc "not provisioned ($STAMP missing; run tier3s/provision-runsc.sh as root)"
elif ! cmp -s "$PIN" "$STAMP"; then
    fail runsc "installed release stamp $STAMP differs from pin $PIN (re-run provision-runsc.sh)"
elif [ ! -f "$RUNSC_DIR/runsc" ] || [ -L "$RUNSC_DIR/runsc" ] || [ ! -x "$RUNSC_DIR/runsc" ]; then
    fail runsc "$RUNSC_DIR/runsc missing (run tier3s/provision-runsc.sh as root)"
else
    want="$(pin_get version_string)"
    out="$(env -i PATH=/usr/bin:/bin "$RUNSC_DIR/runsc" --version 2>&1)"; rc=$?
    got="$(printf '%s\n' "$out" | head -1)"
    if [ "$rc" -eq 0 ] && [ -n "$want" ] && [ "$got" = "$want" ]; then pass runsc "$got (matches pin, rc=0)"
    else fail runsc "version '$got' rc=$rc != pin '$want'"; fi
fi
# Exact installed file set + per-file sha512 against the pin.
if [ "${#WANT[@]}" -lt 2 ]; then
    fail bundle "not checked: no pin"
elif [ ! -d "$RUNSC_DIR" ] || [ -L "$RUNSC_DIR" ]; then
    fail bundle "$RUNSC_DIR missing"
else
    exp="$({ echo "d gvisor-bin"; for f in "${!WANT[@]}"; do echo "f $f"; done; } | LC_ALL=C sort)"
    have="$(find "$RUNSC_DIR" -mindepth 1 -printf '%y %P\n' | LC_ALL=C sort)"
    bad=""
    if [ "$have" != "$exp" ]; then
        bad="file set differs: $(diff <(echo "$exp") <(echo "$have") | grep '^[<>]' | tr '\n' ' ')"
    else
        for f in "${!WANT[@]}"; do
            [ "$(sha "$RUNSC_DIR/$f")" = "${WANT[$f]}" ] || bad="$bad sha512:$f"
        done
    fi
    if [ -z "$bad" ]; then pass bundle "$RUNSC_DIR: ${#WANT[@]} files, exact set, sha512 match pin"
    else fail bundle "$bad"; fi
fi
if [ -f "$WRAPPER" ] && [ ! -L "$WRAPPER" ] && [ -x "$WRAPPER" ] && cmp -s "$HERE/tier3s-runsc" "$WRAPPER"; then
    pass wrapper "$WRAPPER (identical to $HERE/tier3s-runsc)"
else fail wrapper "$WRAPPER missing or differs from $HERE/tier3s-runsc"; fi

# --- podman as the launching user ------------------------------------------
as_user() {
    if [ "$(id -un)" = "$USER_NAME" ]; then "$@"
    else
        local uid; uid="$(id -u "$USER_NAME")"
        runuser -u "$USER_NAME" -- env -i PATH=/usr/bin:/bin HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)" \
            USER="$USER_NAME" XDG_RUNTIME_DIR="/run/user/$uid" "$@"
    fi
}
pv="$(as_user podman version --format '{{.Client.Version}}' 2>/dev/null)"
if [ -z "$pv" ]; then fail podman "podman not runnable as $USER_NAME"
elif [ "${pv%%.*}" -ge 6 ] 2>/dev/null; then pass podman "$pv (>= 6)"
else fail podman "$pv < 6"; fi

if [ -n "$pv" ] && [ -x "$WRAPPER" ]; then
    # An image-backed create, not `--rootfs /`: podman 6.0.2 silently DROPS
    # `--security-opt label=disable` on a --rootfs container (observed in the
    # dev VM, spike/logs/phase0-20261001/). The image is an empty scratch
    # import, kept in the user's store for reuse; nothing is ever started.
    img=localhost/tier3s-probe:empty
    if ! as_user podman image exists "$img" 2>/dev/null; then
        empty="$(mktemp -d)"; chmod 0755 "$empty"
        tar -C "$empty" -cf - . | as_user podman import -q - "$img" >/dev/null 2>&1
        rm -rf "$empty"
    fi
    name="tier3s-probe-$$"
    out="$(as_user podman --runtime "$WRAPPER" create --name "$name" \
            --security-opt label=disable --network=none "$img" /none 2>&1)"
    rc=$?
    if [ $rc -eq 0 ]; then
        rt="$(as_user podman inspect --format '{{.OCIRuntime}}' "$name" 2>&1)"
        lbl="$(as_user podman inspect --format '{{.ProcessLabel}}|{{.HostConfig.SecurityOpt}}' "$name" 2>&1)"
        as_user podman rm -f "$name" >/dev/null 2>&1
        if [ "$rt" = "$WRAPPER" ]; then pass podman_runtime "--runtime $WRAPPER recorded"
        else fail podman_runtime "inspect OCIRuntime='$rt', want $WRAPPER"; fi
        case "$lbl" in *label=disable*) pass label_disable "accepted ($lbl)" ;;
            *) fail label_disable "not recorded ($lbl)" ;; esac
    else
        fail podman_runtime "create with --runtime $WRAPPER --security-opt label=disable failed (rc=$rc): $(echo "$out" | tail -1)"
        fail label_disable "not checked: create failed"
    fi
else
    fail podman_runtime "not checked: podman or wrapper missing"
    fail label_disable "not checked: podman or wrapper missing"
fi

# --- reported, not required -------------------------------------------------
if [ -e /dev/kvm ]; then info kvm "/dev/kvm present (not used: --platform=systrap)"
else info kvm "/dev/kvm absent (not required)"; fi
info selinux "$(getenforce 2>/dev/null || echo 'getenforce unavailable')"

if [ -n "$FIRST_FAIL" ]; then
    printf 'RESULT FAIL: first missing prerequisite: %s\n' "$FIRST_FAIL"
    exit 1
fi
if [ -n "$ROOT" ]; then
    printf 'RESULT TEST-PASS: test root %s (exit 3, not a host verdict)\n' "$ROOT"
    exit 3
fi
printf 'RESULT PASS: tier 3s prerequisites present\n'
exit 0
