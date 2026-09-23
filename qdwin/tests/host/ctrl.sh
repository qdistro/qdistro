#!/bin/bash
# Send a single command to qdwin-bystander's command FIFO, or query
# the most recent state line for a given handle.
#
# Usage: ctrl.sh <test-id> <cmd> [args...]
#   e.g. ctrl.sh foo max 1
#        ctrl.sh foo list
#        ctrl.sh foo state 1
#
# The bystander is write-only on its command FIFO and reports everything
# it does on stderr, which start.sh redirects into qdshell.log. So:
#
#   - For action commands (max/min/restore/raise/close/focus/subscribe/
#     subscribelast/list), this script writes the command line to the
#     FIFO and prints `ok`. The bystander's own log line in qdshell.log
#     is the side-channel "reply" for those that need one (e.g. `list`).
#
#   - `state <handle>` is a pseudo-command synthesised here: it does NOT
#     go to the FIFO. Instead we scrape qdshell.log for the most recent
#     `toplevel_state handle=<H> state=0x..` line emitted by the
#     bystander's `l_state` callback whenever the compositor changes
#     state. The script prints `ok state=0x..` so callers can grep the
#     stdout for `0x1`, `0x4`, etc. (matches the scenario .md contracts).
#
# Writes to the FIFO are wrapped in `timeout 5` so a dead bystander
# (whose FIFO file still exists) turns into a fast non-zero exit instead
# of an infinite hang.

set -eo pipefail
. "$(dirname "$0")/lib.sh"

TEST_ID=${1:?usage: ctrl.sh <test-id> <cmd> [args...]}
shift
CMD="$*"
[ -n "$CMD" ] || { echo "[ctrl.sh] empty command" >&2; exit 2; }

CTRL=$(ht_ctrl "$TEST_ID")
SLOG=$(ht_log_qdshell "$TEST_ID")

# `state <handle>` — scrape, don't send.
# Use `read` rather than the literal token so we tolerate `state` with
# extra whitespace, and so a future caller passing `state` alone still
# fails the regex match cleanly below.
read -r VERB ARG REST <<<"$CMD"
if [ "$VERB" = "state" ]; then
    [ -n "$ARG" ] || { echo "[ctrl.sh] state requires a handle" >&2; exit 2; }
    [ -f "$SLOG" ] || { echo "[ctrl.sh] qdshell.log missing at $SLOG" >&2; exit 4; }
    # Grab the last toplevel_state line for this handle. The bystander
    # logs `qdwin-bystander: toplevel_state handle=<H> state=0x<HEX>`.
    LINE=$(grep -E "toplevel_state handle=$ARG state=0x[0-9a-fA-F]+" "$SLOG" \
           | tail -n 1 || true)
    if [ -z "$LINE" ]; then
        echo "[ctrl.sh] no toplevel_state line for handle=$ARG in $SLOG" >&2
        exit 5
    fi
    # Extract just the hex state.
    STATE=$(echo "$LINE" | sed -n 's/.*state=\(0x[0-9a-fA-F]\+\).*/\1/p')
    echo "ok state=$STATE"
    exit 0
fi

[ -p "$CTRL" ] || { echo "[ctrl.sh] no command FIFO at $CTRL — start.sh not run?" >&2; exit 3; }

# Wrap the FIFO write in a timeout: if the bystander died but its FIFO
# inode is still on disk, an unconstrained `> "$CTRL"` blocks forever
# waiting for a reader. 5s is generous for a sane bystander (it just
# does a poll/read loop) and fast enough to surface a dead one.
if ! timeout 5 sh -c "printf '%s\n' \"\$1\" > \"\$2\"" _ "$CMD" "$CTRL"; then
    echo "[ctrl.sh] bystander not reading FIFO within 5s (dead?)" >&2
    exit 2
fi
echo "ok"
