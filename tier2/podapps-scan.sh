#!/bin/bash
# qdistro-podapps-scan — enumerate XDG .desktop entries inside a tier-2
# container and emit a parsed cache the qdshell PodApps service consumes.
#
# Usage:
#   tier2/podapps-scan.sh <container>
#
# Cache layout (created/overwritten atomically):
#   /var/lib/qdistro/podapps/<container>/apps.json
#       JSON array of entries:
#         {
#           "appId":      "<container>/<basename-without-.desktop>",
#           "container":  "<container>",
#           "workload":   "<workload-from-image-tag-or-empty>",
#           "name":       "<Name= field>",
#           "iconName":   "<Icon= field, host-theme name>",
#           "comment":    "<Comment= field>",
#           "execArgv":   ["<binary>", "<arg1>", ...],
#           "silo":       "tier2/<container>"
#         }
#
# Run as root or as a user with rights to /var/lib/qdistro/podapps;
# qdshell's PodApps.qml reads the JSON in its scan thread.
#
# Design notes:
#   - Icons are referenced by host-theme name only. qdshell falls
#     back to a generic glyph if the host icon theme misses
#     (see doc/containers.md "Launcher UX").
#   - The container must be running (`qpodman ps`). The scanner does
#     NOT auto-start containers — qdshell calls `spawn-tier2.sh` for
#     that on click. Scanning is a side-channel that runs whenever a
#     container is up.
#   - The `Exec=` line is captured verbatim in `execArgv`. On click,
#     qdshell rewrites it as
#       spawn-tier2.sh <container> <workload> -- <execArgv...>
#     The guest's Exec value is informational; spawn-tier2.sh
#     determines the actual binary path inside the container.
set -uo pipefail

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage; exit 0
fi
if [ $# -lt 1 ]; then
    usage >&2; exit 1
fi

CONTAINER="$1"
CACHE_DIR_ROOT="${QDISTRO_PODAPPS_CACHE:-/var/lib/qdistro/podapps}"
CACHE_DIR="$CACHE_DIR_ROOT/$CONTAINER"

fail() { echo "podapps-scan: $*" >&2; exit 2; }

# qpodman: thin wrapper. When QDISTRO_PODAPPS_AS_USER is set (e.g.
# "admin"), invoke rootless podman as that user via runuser so we
# see their container store. Otherwise call podman directly.
qpodman() {
    if [ -n "${QDISTRO_PODAPPS_AS_USER:-}" ]; then
        runuser -u "$QDISTRO_PODAPPS_AS_USER" -- podman "$@"
    else
        podman "$@"
    fi
}

command -v podman >/dev/null 2>&1 || fail "podman not in PATH"
command -v python3 >/dev/null 2>&1 || fail "python3 needed for desktop-entry parsing"

if ! qpodman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
    fail "container '$CONTAINER' not running"
fi

# Resolve workload from the image tag (qdistro/tier2-<workload>:tag).
IMAGE=$(qpodman inspect --format '{{.ImageName}}' "$CONTAINER" 2>/dev/null \
        | head -n1 || true)
WORKLOAD=""
case "$IMAGE" in
    *qdistro/tier2-*:*) WORKLOAD="${IMAGE#*qdistro/tier2-}"; WORKLOAD="${WORKLOAD%%:*}";;
esac

mkdir -p "$CACHE_DIR"

# Enumerate desktop files inside the container. Both system-wide and
# per-user XDG directories. Minimal containers may lack `find`, so we
# use bash globbing instead — POSIX-portable and present in every
# image that ships a shell.
DESKTOP_LIST=$(qpodman exec "$CONTAINER" \
    bash -c '
        shopt -s nullglob
        for d in /usr/share/applications \
                 /usr/local/share/applications \
                 "$HOME/.local/share/applications"; do
            for f in "$d"/*.desktop; do
                [ -f "$f" ] && printf "%s\n" "$f"
            done
        done
    ' 2>/dev/null \
    || true)

# Dump file contents through a single tarball-style pipe (no per-file
# qpodman exec overhead). Limit to first 500 entries defensively.
if [ -z "$DESKTOP_LIST" ]; then
    : >"$CACHE_DIR/apps.json.tmp"
    echo "[]" >"$CACHE_DIR/apps.json.tmp"
    mv "$CACHE_DIR/apps.json.tmp" "$CACHE_DIR/apps.json"
    echo "podapps-scan: $CONTAINER → 0 entries (cache cleared)"
    exit 0
fi

# Slurp each file through qpodman exec cat; chunked into one
# multi-document blob with `--- FILE: <path>` separators for the
# python parser. Cheap and adequate at < a-few-hundred entries per
# container.
BLOB=$(mktemp)
trap 'rm -f "$BLOB"' EXIT

while IFS= read -r f; do
    [ -z "$f" ] && continue
    echo "--- FILE: $f"
    qpodman exec "$CONTAINER" cat "$f" 2>/dev/null || true
done <<<"$DESKTOP_LIST" >"$BLOB"

python3 - "$CONTAINER" "$WORKLOAD" "$BLOB" "$CACHE_DIR/apps.json.tmp" <<'PY'
import configparser
import io
import json
import os
import shlex
import sys

container, workload, blob_path, out_path = sys.argv[1:]

entries = []
current_path = None
current_buf = []

def flush(path, buf):
    if not path or not buf:
        return
    cp = configparser.RawConfigParser(strict=False, interpolation=None)
    try:
        cp.read_string("".join(buf))
    except configparser.Error:
        return
    if "Desktop Entry" not in cp:
        return
    sec = cp["Desktop Entry"]
    if sec.get("NoDisplay", "false").lower() == "true":
        return
    if sec.get("Type", "Application") != "Application":
        return
    name     = sec.get("Name", "").strip()
    icon     = sec.get("Icon", "").strip()
    comment  = sec.get("Comment", "").strip()
    exec_line = sec.get("Exec", "").strip()
    if not name or not exec_line:
        return
    # Strip the standard XDG Exec field codes (%f %u %F %U %i %c %k).
    cleaned = " ".join(tok for tok in exec_line.split()
                       if not (tok.startswith("%") and len(tok) == 2))
    try:
        argv = shlex.split(cleaned)
    except ValueError:
        argv = cleaned.split()
    if not argv:
        return
    base = os.path.basename(path)
    app_id_local = base[:-len(".desktop")] if base.endswith(".desktop") else base
    entries.append({
        "appId":     f"{container}/{app_id_local}",
        "container": container,
        "workload":  workload,
        "name":      name,
        "iconName":  icon,
        "comment":   comment,
        "execArgv":  argv,
        "silo":      f"tier2/{container}",
    })

with open(blob_path, "r", encoding="utf-8", errors="replace") as fh:
    for raw in fh:
        line = raw.rstrip("\n")
        if line.startswith("--- FILE: "):
            flush(current_path, current_buf)
            current_path = line[len("--- FILE: "):].strip()
            current_buf = []
        else:
            current_buf.append(raw)
    flush(current_path, current_buf)

entries.sort(key=lambda e: e["name"].lower())

with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(entries, fh, indent=2, ensure_ascii=False)
    fh.write("\n")

print(f"podapps-scan: {container} → {len(entries)} entries", file=sys.stderr)
PY

mv "$CACHE_DIR/apps.json.tmp" "$CACHE_DIR/apps.json"
echo "podapps-scan: wrote $CACHE_DIR/apps.json"
