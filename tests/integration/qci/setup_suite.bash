# shellcheck shell=bash
# Suite-level environment for the qci self-test bats files. bats sources this
# file (it lives beside the .bats files) once per `bats` invocation, before any
# test file runs, and the test files inherit the environment it leaves.
#
# These are contract tests of the runner's DEFAULT behaviour. A developer
# shell commonly exports documented knobs (ci/README.md: QCI_AGENT_CMD,
# QCI_AGENT_MODEL, QCI_OFFLINE, QCI_SKIP_IMAGE, QCI_FLAKE_STRICT, ...), and a
# nested run inside qci inherits WORKSPACE and every <NAME>_REPO from
# bootstrap.sh. Any of them silently changes what the runner under test does,
# so the same file passed under `qci selftest` and failed when run directly
# with `bats` (or the reverse). Start every run from the same clean slate; a
# case that needs a knob sets it explicitly.
#
# Kept: QCI_IN_SELFTEST (the selftest gate's recursion guard) and
# QCI_RUNS_DIR (the selftest gate's isolated runs dir for nested qci runs).
setup_suite() {
    local name
    while IFS= read -r name; do
        case "$name" in
            QCI_IN_SELFTEST|QCI_RUNS_DIR) ;;
            QCI_*) unset "$name" ;;
        esac
    done < <(compgen -e)
    # The same scrub the selftest gate applies (ci/lib/gates/selftest.sh):
    # WORKSPACE and each PROJECTS-derived <NAME>_REPO.
    unset WORKSPACE
    local proj projects
    projects=$(sed -n '/^PROJECTS=(/,/^)/{/^ /p}' \
        "$(dirname "${BASH_SOURCE[0]}")/../../../ci/lib/bootstrap.sh")
    # Fail loudly rather than silently skip the scrub if bootstrap.sh changes.
    [[ $'\n'"$projects"$'\n' == *[[:space:]]qdistro[[:space:]]* ]] \
        || { echo "setup_suite: cannot read PROJECTS from ci/lib/bootstrap.sh" >&2; return 1; }
    for proj in $projects; do
        unset "$(printf '%s' "$proj" | tr '[:lower:]-' '[:upper:]_')_REPO"
    done
}
