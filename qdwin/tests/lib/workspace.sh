# workspace.sh — shared workspace-root resolver for the qdwin test helpers.
#
# Sourced, not executed. Consumed by tests/gui/qdwin-helpers.sh and
# tests/apps/qdwin-apps-helpers.sh (previously a verbatim copy in each).
#
# Resolve the workspace root — since the monorepo migration, the qdistro
# monorepo root that holds qdwin/ (and qdshell/, ...) in-tree beside its own
# scripts/vm/vm-exec. Walk upward from $1 until a directory with both
# scripts/vm/vm-exec and qdwin/ is found: the checkout (or linked worktree)
# this qdwin copy belongs to. The old test ("$d/qdistro/scripts/vm/vm-exec",
# the sibling layout) would now match only by accident of the checkout's own
# directory name -- and from a worktree it would reach the MAIN checkout's
# vm-exec instead of the worktree's. Prints the root, or returns 1.
qdwin_find_workspace() {
    local d
    d=$(cd "${1:-.}" 2>/dev/null && pwd -P) || return 1
    while [ -n "$d" ] && [ "$d" != / ]; do
        if [ -e "$d/scripts/vm/vm-exec" ] && [ -d "$d/qdwin" ]; then
            printf '%s\n' "$d"
            return 0
        fi
        d=$(dirname "$d")
    done
    return 1
}
