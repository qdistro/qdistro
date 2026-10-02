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
# The FIRST statement that matters (astra fix r3): before any external command
# runs, a real (non-test) run uses only the system tool dirs, so no directory
# on the caller's PATH can supply dirname/id/stat/... to a root probe. A TEST
# run (QDISTRO_PROBE_ROOT) keeps the trusted test operator's PATH for its fakes.
[ -n "${QDISTRO_PROBE_ROOT:-}" ] || { PATH=/usr/sbin:/usr/bin:/sbin:/bin; export PATH; }

USER_NAME=admin
while [ $# -gt 0 ]; do
    case "$1" in
        --user) shift; USER_NAME="${1:?--user needs a value}" ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "probe: unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

HERE="$(cd "$(dirname "$0")" && pwd -P)"   # physical path: its real ancestors are checked
PIN="$HERE/RUNSC_RELEASE"               # authoritative pin (the checked-in one)
# Unit-test hooks ONLY: an alternate root for /etc + /usr/libexec
# (QDISTRO_PROBE_ROOT) and, with it, an alternate pin (QDISTRO_PROBE_PIN). In
# this mode every run is labelled TEST and a clean result exits 3, never 0, so
# a redirected probe can never be read as a host PASS.
ROOT="${QDISTRO_PROBE_ROOT:-}"
if [ -n "$ROOT" ]; then
    case "$ROOT" in /*) ;; *) echo "probe: QDISTRO_PROBE_ROOT must be absolute: $ROOT" >&2; exit 2 ;; esac
    while [ "$ROOT" != "/" ] && [ "${ROOT%/}" != "$ROOT" ]; do ROOT="${ROOT%/}"; done
    [ "$ROOT" != "/" ] || { echo "probe: QDISTRO_PROBE_ROOT=/ is not a test root" >&2; exit 2; }
    printf 'TEST MODE: QDISTRO_PROBE_ROOT=%s (not a host verdict)\n' "$ROOT"
    [ -z "${QDISTRO_PROBE_PIN:-}" ] || PIN="$QDISTRO_PROBE_PIN"
else
    for h in QDISTRO_PROBE_PIN QDISTRO_PROBE_PAUSE_AT QDISTRO_PROBE_PAUSE_DIR; do
        if [ -n "${!h:-}" ]; then
            echo "probe: $h is a unit-test hook and needs QDISTRO_PROBE_ROOT" >&2
            exit 2
        fi
    done
fi
# Unit-test hook (test root only): stop at a named point until
# $QDISTRO_PROBE_PAUSE_DIR/<point>.release exists, announcing <point>.reached,
# so a test can swap files inside the validate->open and verify->exec windows.
test_pause() {
    [ -n "$ROOT" ] && [ "${QDISTRO_PROBE_PAUSE_AT:-}" = "$1" ] || return 0
    local d="${QDISTRO_PROBE_PAUSE_DIR:?QDISTRO_PROBE_PAUSE_DIR unset}"
    : > "$d/$1.reached"
    for _ in $(seq 1 600); do [ ! -e "$d/$1.release" ] || return 0; sleep 0.1; done
    echo "probe: TEST pause at $1 not released" >&2; exit 2
}
# --- root runs only a root-controlled checkout ------------------------------
# The pin, the wrapper and (bash reads scripts incrementally) this script's own
# code come from the checkout, so as root every one of them and every ancestor
# directory must be root-owned and not other-writable (group-writable only for
# gid 0: git archive extracts 0775 root:root). Otherwise another uid would
# decide what root executes or installs.
checkout_untrusted() {   # prints the first problem and returns 0, else 1
    local x st u g m
    if [ -L "$0" ]; then echo "$0 is a symlink"; return 0; fi
    perm_bad() {
        st="$(stat -c '%u %g %a' -- "$1")" || { echo "$1: stat failed"; return 0; }
        u="${st%% *}"; m="${st##* }"; g="${st#* }"; g="${g%% *}"
        if [ "$u" != 0 ]; then echo "$1 owned by uid $u"; return 0; fi
        if (( (8#$m & 8#002) != 0 )); then echo "$1 is other-writable (mode $m)"; return 0; fi
        if (( (8#$m & 8#020) != 0 )) && [ "$g" != 0 ]; then echo "$1 is writable by group $g (mode $m)"; return 0; fi
        return 1
    }
    for x in "$HERE/$(basename -- "$0")" "$HERE/RUNSC_RELEASE" "$HERE/tier3s-runsc"; do
        if [ -L "$x" ] || [ ! -f "$x" ]; then echo "$x is not a regular file"; return 0; fi
        perm_bad "$x" && return 0
    done
    x="$HERE"
    while :; do
        if [ -L "$x" ] || [ ! -d "$x" ]; then echo "$x is not a directory"; return 0; fi
        perm_bad "$x" && return 0
        [ "$x" != / ] || return 1
        x="$(dirname -- "$x")"
    done
}
if [ "$EUID" -eq 0 ] && why="$(checkout_untrusted)"; then
    printf 'REFUSE checkout: refusing to run as root from a checkout another user could modify: %s (use a root-owned copy)\n' "$why"
    exit 2
fi
unset TAR_OPTIONS
RUNSC_DIR="$ROOT/usr/libexec/qdistro/runsc"
WRAPPER="$ROOT/usr/libexec/qdistro/tier3s-runsc"
STAMP="$ROOT/etc/qdistro/runsc-release"
PROFILE_FILE="$ROOT/etc/qdistro/profile"
SUBID_DIR="$ROOT/etc"
# Expected owner of the installation and of its ancestors: root for a real
# install; the caller for a test root (ancestors checked up to the test root).
if [ -z "$ROOT" ]; then EXP_UID=0; EXP_OWN="root:root"; TRUST_STOP=/
else EXP_UID="$(id -u)"; EXP_OWN="$(id -un):$(id -gn)"; TRUST_STOP="$ROOT"; fi

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
        if grep -q "^$USER_NAME:[0-9]*:[1-9][0-9]*$" "$SUBID_DIR/$db" 2>/dev/null; then
            pass "$db" "$(grep "^$USER_NAME:" "$SUBID_DIR/$db" | head -1)"
        else fail "$db" "no range for $USER_NAME in $SUBID_DIR/$db"; fi
    done
    for t in newuidmap newgidmap; do
        p="$(command -v "$t" 2>/dev/null)"
        if [ -n "$p" ] && [ -x "$p" ]; then pass "$t" "$p"
        else fail "$t" "not installed (shadow package)"; fi
    done
fi

# --- runsc bundle: compared against the checked-in pin, not the stamp -----
# Integrity first, execution last (astra full-review P2): nothing from the
# installation is executed until the release stamp, the trusted ancestors, the
# exact file set (type/mode/owner) and every per-file sha512 have passed. Then
# runsc is opened ONCE, the open inode is re-verified (same dev:ino as the
# validated path, regular file, owner/mode, sha512 read through that fd) and
# `--version` executes that very inode via /proc/self/fd/N, so a rename or
# path swap after validation cannot change what runs. The inode's content can
# then only change through a write by its owner (root; mode 0755 checked on
# the fd), and execve refuses a file that is open for writing (ETXTBSY).
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

# (1) release stamp + presence (no execution)
if [ "${#WANT[@]}" -lt 2 ]; then
    fail runsc "not checked: no pin"
elif [ ! -e "$STAMP" ]; then
    fail runsc "not provisioned ($STAMP missing; run tier3s/provision-runsc.sh as root)"
elif ! cmp -s "$PIN" "$STAMP"; then
    fail runsc "installed release stamp $STAMP differs from pin $PIN (re-run provision-runsc.sh)"
elif [ ! -f "$RUNSC_DIR/runsc" ] || [ -L "$RUNSC_DIR/runsc" ]; then
    fail runsc "$RUNSC_DIR/runsc missing (run tier3s/provision-runsc.sh as root)"
else
    pass runsc "release stamp matches pin; $RUNSC_DIR/runsc present (not yet executed)"
    STAMP_OK=1
fi

# (2) trusted ancestors: RUNSC_DIR and every directory above it (to / for a
# real install, to the test root otherwise) is a real directory, owned by the
# expected owner, and not group/other-writable: no one else can rename or
# replace anything on the path between validation and execution.
untrusted_ancestor() {   # prints the first offending component, returns 1
    local d="$1" st owner mode
    while :; do
        if [ -L "$d" ]; then echo "$d is a symlink"; return 1; fi
        if [ ! -d "$d" ]; then echo "$d is not a directory"; return 1; fi
        st="$(stat -c '%u %a' -- "$d")" || { echo "$d: stat failed"; return 1; }
        owner="${st%% *}"; mode="${st#* }"
        if [ "$owner" != "$EXP_UID" ]; then echo "$d owned by uid $owner, want $EXP_UID"; return 1; fi
        if (( (8#$mode & 8#022) != 0 )); then echo "$d is group/other-writable (mode $mode)"; return 1; fi
        [ "$d" != "$TRUST_STOP" ] || return 0
        [ "$d" != / ] || { echo "walked past / without meeting $TRUST_STOP"; return 1; }
        d="$(dirname -- "$d")"
    done
}
if why="$(untrusted_ancestor "$RUNSC_DIR")"; then
    pass install_path "$RUNSC_DIR and ancestors up to $TRUST_STOP: real dirs, uid $EXP_UID, not group/other-writable"
    PATH_OK=1
else
    fail install_path "untrusted: $why"
fi

# (3) exact installed file set (type, mode, owner, path) + per-file sha512
if [ "${#WANT[@]}" -lt 2 ]; then
    fail bundle "not checked: no pin"
elif [ ! -d "$RUNSC_DIR" ] || [ -L "$RUNSC_DIR" ]; then
    fail bundle "$RUNSC_DIR missing or a symlink"
else
    exp="$({ echo "d 755 $EXP_OWN ."; echo "d 755 $EXP_OWN gvisor-bin"
             for f in "${!WANT[@]}"; do echo "f 755 $EXP_OWN $f"; done; } | LC_ALL=C sort)"
    have="$(find "$RUNSC_DIR" -printf '%y %m %u:%g %P\n' | sed 's/ $/ ./' | LC_ALL=C sort)"
    test_pause after-listing
    bad=""
    if [ "$have" != "$exp" ]; then
        bad="file set differs: missing=[$(LC_ALL=C comm -23 <(echo "$exp") <(echo "$have") | tr '\n' ',')] unexpected=[$(LC_ALL=C comm -13 <(echo "$exp") <(echo "$have") | tr '\n' ',')]"
    elif [ "${PATH_OK:-0}" -ne 1 ]; then
        # no content reads under an untrusted path (a FIFO swapped in there
        # would hang the probe); the listing above opens no file
        bad="sha512 not checked: install path untrusted"
    else
        # identity of the runsc inode whose bytes are hashed below
        RUNSC_ID="$(stat -c '%d:%i' -- "$RUNSC_DIR/runsc")"
        for f in "${!WANT[@]}"; do
            [ "$(sha "$RUNSC_DIR/$f")" = "${WANT[$f]}" ] || bad="${bad:+$bad }sha512:$f"
        done
    fi
    if [ -z "$bad" ]; then pass bundle "$RUNSC_DIR: ${#WANT[@]} files, exact set, sha512 match pin"; BUNDLE_OK=1
    else fail bundle "$bad"; fi
fi

# (4) only now: execute the verified inode
if [ "${STAMP_OK:-0}" -ne 1 ] || [ "${PATH_OK:-0}" -ne 1 ] || [ "${BUNDLE_OK:-0}" -ne 1 ]; then
    fail runsc_version "not executed: installation integrity checks failed (see above)"
elif ! test_pause before-open || ! exec {RFD}<"$RUNSC_DIR/runsc"; then
    fail runsc_version "not executed: cannot open $RUNSC_DIR/runsc"
else
    fdp="/proc/$$/fd/$RFD"
    idnow="$(stat -L -c '%d:%i %F %a %u' -- "$fdp" 2>/dev/null)"
    if [ "$idnow" != "$RUNSC_ID regular file 755 $EXP_UID" ]; then
        fail runsc_version "not executed: opened inode ($idnow) is not the validated one ($RUNSC_ID regular file 755 $EXP_UID)"
    elif [ "$(sha "$fdp")" != "${WANT[runsc]}" ]; then
        fail runsc_version "not executed: sha512 of the opened inode differs from the pin"
    else
        want="$(pin_get version_string)"
        test_pause before-exec
        out="$(env -i PATH=/usr/bin:/bin "/proc/self/fd/$RFD" --version 2>&1)"; rc=$?
        got="$(printf '%s\n' "$out" | head -1)"
        if [ "$rc" -eq 0 ] && [ -n "$want" ] && [ "$got" = "$want" ]; then
            pass runsc_version "$got (matches pin, rc=0; executed the verified inode $RUNSC_ID)"
        else fail runsc_version "version '$got' rc=$rc != pin '$want'"; fi
    fi
    exec {RFD}<&-
fi
if [ "${PATH_OK:-0}" -ne 1 ]; then
    fail wrapper "not checked: install path untrusted (see install_path)"
elif [ -f "$WRAPPER" ] && [ ! -L "$WRAPPER" ] && [ "$(stat -c '%a %U:%G' "$WRAPPER")" = "755 $EXP_OWN" ] \
   && cmp -s "$HERE/tier3s-runsc" "$WRAPPER"; then
    pass wrapper "$WRAPPER (identical to $HERE/tier3s-runsc)"
    WRAPPER_OK=1
else fail wrapper "$WRAPPER missing, not $EXP_OWN 0755, or differs from $HERE/tier3s-runsc"; fi

# --- runsc state root (tier3s/CONTRACT.md D-A1) -----------------------------
# The wrapper uses /run/qdistro-tier3s-runsc/<host uid> for every runsc call
# and refuses to create it; provisioning (tmpfiles.d/qdistro-tier3s.conf) does.
SR_BASE="$ROOT/run/qdistro-tier3s-runsc"
if ! id "$USER_NAME" >/dev/null 2>&1; then
    fail state_root "not checked: user $USER_NAME missing"
else
    sr_uid="$(id -u "$USER_NAME")"; SR="$SR_BASE/$sr_uid"
    if [ -L "$SR_BASE" ] || [ ! -d "$SR_BASE" ] || [ "$(stat -c '%u %a' -- "$SR_BASE")" != "$EXP_UID 755" ]; then
        fail state_root "$SR_BASE missing, a symlink or not uid $EXP_UID 0755 (systemd-tmpfiles --create qdistro-tier3s.conf)"
    elif [ -L "$SR" ] || [ ! -d "$SR" ] || [ "$(stat -c '%u %a' -- "$SR")" != "$sr_uid 700" ]; then
        fail state_root "$SR missing, a symlink or not uid $sr_uid 0700 (systemd-tmpfiles --create qdistro-tier3s.conf)"
    elif [ -z "$ROOT" ] && [ "${#SR}" -gt 31 ]; then
        fail state_root "$SR is longer than 31 bytes (runsc control socket path)"
    else
        pass state_root "$SR (uid $sr_uid 0700 under $SR_BASE)"
    fi
fi

# --- podman as the launching user ------------------------------------------
# A foreign user's passwd entry is resolved once here, bounded (fable A r3
# P3-2) and status-checked (sol r5 P3-4): a wedged lookup — even one that
# printed a complete line first — fails the probe instead of hanging the
# spawn that called it, and its printed prefix is not a result.
AS_UID=""; AS_HOME=""
if [ "$(id -un)" != "$USER_NAME" ]; then
    AS_UID="$(id -u "$USER_NAME" 2>/dev/null)"; puid=""; pw=""
    [ -n "$AS_UID" ] && pw="$(timeout 5 getent passwd "$USER_NAME")" \
        && puid="$(printf '%s\n' "$pw" | cut -d: -f3)" \
        && AS_HOME="$(printf '%s\n' "$pw" | cut -d: -f6)"
    [ -n "$AS_UID" ] && [ "$puid" = "$AS_UID" ] && [ "${AS_HOME#/}" != "$AS_HOME" ] \
        && [[ "$pw" != *$'\n'* ]] \
        || { fail nss "no passwd entry for $USER_NAME within the 5 s bound"; AS_UID=""; AS_HOME=""; }
fi
as_user() {
    if [ -z "$AS_UID" ]; then [ "$(id -un)" = "$USER_NAME" ] || return 1; "$@"
    else
        runuser -u "$USER_NAME" -- env -i PATH=/usr/bin:/bin HOME="$AS_HOME" \
            USER="$USER_NAME" XDG_RUNTIME_DIR="/run/user/$AS_UID" "$@"
    fi
}
pv="$(as_user podman version --format '{{.Client.Version}}' 2>/dev/null)"
if [ -z "$pv" ]; then fail podman "podman not runnable as $USER_NAME"
elif [ "${pv%%.*}" -ge 6 ] 2>/dev/null; then pass podman "$pv (>= 6)"
else fail podman "$pv < 6"; fi

# The wrapper is only handed to podman once it verified above (podman create
# records it and does not execute it; nothing is started).
if [ -n "$pv" ] && [ "${WRAPPER_OK:-0}" -eq 1 ]; then
    # An image-backed create, not `--rootfs /`: podman 6.0.2 silently DROPS
    # `--security-opt label=disable` on a --rootfs container (observed in the
    # dev VM, spike/logs/phase0-20261001/). The image is an empty scratch
    # import, kept in the user's store for reuse; nothing is ever started.
    img=localhost/tier3s-probe:empty
    import_err=""
    if ! as_user podman image exists "$img" 2>/dev/null; then
        # No temporary directory and no chmod (astra fix r2): a one-entry
        # archive ("./", 0755, uid/gid 0) built from /'s metadata alone with
        # --no-recursion, so nothing on the host is created or modified and no
        # attacker-replaceable pathname (e.g. under a hostile $TMPDIR) is used.
        imp="$(tar -C / --no-recursion --numeric-owner --owner=0 --group=0 --mode=0755 -cf - . \
               | as_user podman import -q - "$img" 2>&1)" \
            || import_err="rc=$?: $(printf '%s\n' "$imp" | tail -1)"
    fi
    if [ -n "$import_err" ]; then
        fail podman_runtime "could not import the empty scratch image $img ($import_err)"
        fail label_disable "not checked: scratch image import failed"
    else
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
    fi
else
    fail podman_runtime "not checked: podman missing or wrapper not verified"
    fail label_disable "not checked: podman missing or wrapper not verified"
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
