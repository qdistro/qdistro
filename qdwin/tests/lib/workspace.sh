# workspace.sh — shared workspace-root resolver for the qdwin test helpers.
#
# Sourced, not executed. Consumed by tests/gui/qdwin-helpers.sh and
# tests/apps/qdwin-apps-helpers.sh (previously a verbatim copy in each).
#
# Resolve the workspace root — the directory that holds the sibling product
# repos (qdistro, qdshell, ...). Walk upward from $1 until a checkout with
# qdistro/scripts/vm/vm-exec is found. This works both in the normal layout
# (qdwin a sibling of qdistro under the project root) AND from a git worktree
# under .worktrees/<name>/, where $repo/.. is the worktrees dir — not the
# project root — so the old "$ROOT/.." derivation pointed vm-exec at a path
# that does not exist. Prints the workspace dir, or returns 1 if none found.
qdwin_find_workspace() {
    local d
    d=$(cd "${1:-.}" 2>/dev/null && pwd -P) || return 1
    while [ -n "$d" ] && [ "$d" != / ]; do
        if [ -e "$d/qdistro/scripts/vm/vm-exec" ]; then
            printf '%s\n' "$d"
            return 0
        fi
        d=$(dirname "$d")
    done
    return 1
}
