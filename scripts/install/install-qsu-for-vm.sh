#!/bin/bash
# Idempotent qsu install for fresh-vm-bootstrap. Takes the qsu/
# source dir as $1 (default /root/qdistro-src/qdistro/qsu), copies
# qsu.py + qdistro_root_exec.py + the systemd unit pair into place,
# and enables the socket-activated service so end-to-end qsu tests
# can drive the real /run/qdistro-root-exec/sock path.
#
# Pre-reqs: python313 + dbus-python (already baked into baseweed).
#
# This sits next to install-broker-for-qdwin.sh — the broker MUST be
# running before the root-exec service can issue RequestPermissionAs,
# so install order is broker → qsu.
set -eu

QSU_SRC=${1:-/root/qdistro-src/qdistro/qsu}
DEST_LIB=/usr/local/lib/qdistro
DEST_BIN=/usr/local/bin
SYSTEMD_DIR=/etc/systemd/system
SOCKET_UNIT=$SYSTEMD_DIR/qdistro-root-exec.socket
SERVICE_UNIT=$SYSTEMD_DIR/qdistro-root-exec.service

if [ ! -d "$QSU_SRC" ]; then
    echo "ERROR: qsu source not found at $QSU_SRC" >&2
    echo "       pass the qsu/ dir as \$1 or untar qdistro to /root/qdistro-src/qdistro/" >&2
    exit 2
fi

install -d -o root -g root -m 0755 "$DEST_LIB"
install -d -o root -g root -m 0755 "$DEST_BIN"

# 1. Privileged-exec service (root-side D-Bus delegator + subprocess
#    streamer). Same install layout as the broker — under
#    /usr/local/lib/qdistro/ so an unprivileged user can't replace it.
# Installed mode 0755 (executable) with a `#!/usr/bin/python3` shebang so
# the service unit can ExecStart it DIRECTLY. That makes the labelled
# script the first execve target, which is what triggers the SELinux
# init_daemon_domain transition into qdistro_root_exec_t (see
# selinux/qsu/qdistro_qsu.te). ExecStart-ing `/usr/bin/python3 <script>`
# instead would transition on the interpreter (bin_t), never the script.
install -o root -g root -m 0755 "$QSU_SRC/qdistro_root_exec.py" \
    "$DEST_LIB/qdistro_root_exec.py"

# 2. User-facing entry point. /usr/local/bin/qsu is what humans type.
#
#    We install the COMPILED qsu.c binary (not the old bash->python
#    wrapper) so that /proc/<pid>/exe resolves to /usr/local/bin/qsu for
#    the whole lifetime of the connection. That gives qdistro-root-exec
#    an unambiguous caller_exe for audit/forensics; the python wrapper's
#    /proc/<pid>/exe was always /usr/bin/python3.X, which defeated the
#    exe-based caller identity checks.
#
#    qsu.py is still installed to $DEST_LIB as the documented reference /
#    fallback implementation. The compiled binary is the default and only
#    user-facing entry point; the python wrapper is NEVER silently shipped
#    as /usr/local/bin/qsu, because that would let caller_exe resolve back
#    to python3 and defeat the audit anchor. The wrapper fallback is opt-in
#    only via QSU_ALLOW_PYTHON_FALLBACK=1 (e.g. a compiler-less recovery
#    image); without it, a missing/broken compiler is a hard error so an
#    install can never quietly regress to the non-ELF entry point.
install -o root -g root -m 0644 "$QSU_SRC/qsu.py" "$DEST_LIB/qsu.py"

# Pick a C compiler. cc is the POSIX-standard name; gcc/clang are the
# usual concrete implementations. install-deps.sh ships gcc (Tumbleweed)
# / build-essential (Ubuntu), so a compiler should normally be present.
QSU_CC=""
for _cc in "${CC:-}" cc gcc clang; do
    [ -n "$_cc" ] || continue
    if command -v "$_cc" >/dev/null 2>&1; then
        QSU_CC="$_cc"
        break
    fi
done

# Hardened build flags — kept in sync with qsu/Makefile so the installer
# and a hand-run `make` produce the same binary. -Wformat=2 +
# -Werror=format-security matter for a security-sensitive client.
QSU_CFLAGS="-O2 -Wall -Wextra -Wformat=2 -Werror=format-security"

build_and_install_binary() {
    if [ ! -f "$QSU_SRC/qsu.c" ]; then
        echo "ERROR: $QSU_SRC/qsu.c missing; cannot build the qsu binary" >&2
        echo "       (expected the compiled-client source next to qsu.py)" >&2
        return 1
    fi
    local build_dir
    build_dir=$(mktemp -d)
    # Clean the build dir on any exit so a failed compile leaves nothing
    # behind. Single-shot trap is fine — this is the only temp dir.
    trap 'rm -rf "$build_dir"' RETURN
    echo "compiling qsu.c with $QSU_CC ($QSU_CFLAGS) ..."
    # shellcheck disable=SC2086 -- QSU_CFLAGS is an intentional word list
    if ! "$QSU_CC" $QSU_CFLAGS -o "$build_dir/qsu" "$QSU_SRC/qsu.c"; then
        echo "ERROR: failed to compile $QSU_SRC/qsu.c with $QSU_CC" >&2
        return 1
    fi
    # install(1) atomically replaces the target, so this is idempotent and
    # safe even if an old wrapper/binary is already in place.
    install -o root -g root -m 0755 "$build_dir/qsu" "$DEST_BIN/qsu"
    echo "installed compiled qsu binary -> $DEST_BIN/qsu"
}

if [ -n "$QSU_CC" ]; then
    build_and_install_binary || exit 4
elif [ "${QSU_ALLOW_PYTHON_FALLBACK:-}" = "1" ]; then
    echo "WARN: no C compiler found (tried: cc gcc clang) and" >&2
    echo "WARN: QSU_ALLOW_PYTHON_FALLBACK=1 set — installing the python" >&2
    echo "WARN: wrapper for $DEST_BIN/qsu. /proc/<pid>/exe will resolve to" >&2
    echo "WARN: python3, WEAKENING caller_exe audit. This is a recovery-only" >&2
    echo "WARN: path; install a C compiler (gcc / build-essential) and re-run" >&2
    echo "WARN: to get the ELF binary." >&2
    QSU_FALLBACK=$(mktemp)
    cat >"$QSU_FALLBACK" <<'EOF'
#!/bin/bash
exec /usr/bin/python3 /usr/local/lib/qdistro/qsu.py "$@"
EOF
    install -o root -g root -m 0755 "$QSU_FALLBACK" "$DEST_BIN/qsu"
    rm -f "$QSU_FALLBACK"
else
    echo "ERROR: no C compiler found (tried: cc gcc clang); cannot build the" >&2
    echo "ERROR: qsu binary. /usr/local/bin/qsu MUST be the compiled qsu.c so" >&2
    echo "ERROR: /proc/<pid>/exe (caller_exe) is unambiguous for audit." >&2
    echo "ERROR: Install a C compiler (gcc on Tumbleweed, build-essential on" >&2
    echo "ERROR: Ubuntu) and re-run. To deliberately ship the python wrapper" >&2
    echo "ERROR: on a compiler-less recovery image, re-run with" >&2
    echo "ERROR: QSU_ALLOW_PYTHON_FALLBACK=1 (weakens the caller_exe audit)." >&2
    # Fail CLOSED on upgrade too: if a stale non-ELF wrapper from a previous
    # install is still sitting at $DEST_BIN/qsu, leaving it callable would
    # mean caller_exe keeps resolving to python3 — exactly what this change
    # fixes. Remove any non-ELF entry point so no insecure wrapper survives.
    # (An existing ELF binary is left intact: it is already the correct
    # artifact and a no-compiler re-run shouldn't delete a working qsu.)
    if [ -e "$DEST_BIN/qsu" ] && ! head -c 4 "$DEST_BIN/qsu" 2>/dev/null \
            | od -An -tx1 2>/dev/null | grep -q '7f 45 4c 46'; then
        echo "ERROR: removing stale non-ELF $DEST_BIN/qsu (was a python" >&2
        echo "ERROR: wrapper); refusing to leave an insecure entry point." >&2
        rm -f "$DEST_BIN/qsu"
    fi
    exit 4
fi

# 3. Systemd unit pair.
install -m 0644 "$QSU_SRC/qdistro-root-exec.socket"  "$SOCKET_UNIT"
install -m 0644 "$QSU_SRC/qdistro-root-exec.service" "$SERVICE_UNIT"

systemctl daemon-reload
systemctl enable --now qdistro-root-exec.socket >/dev/null

# 4. Verify the socket is listening. Service is socket-activated so
#    .service unit may be inactive until first connect — that's fine.
for _ in 1 2 3 4 5; do
    if [ -S /run/qdistro-root-exec/sock ]; then
        break
    fi
    sleep 0.5
done
if [ ! -S /run/qdistro-root-exec/sock ]; then
    echo "ERROR: /run/qdistro-root-exec/sock did not appear" >&2
    journalctl -u qdistro-root-exec.socket --no-pager -n 20 >&2 || true
    exit 3
fi

echo "qsu ready (socket /run/qdistro-root-exec/sock + /usr/local/bin/qsu)"
