#!/usr/bin/env bats
#
# Host-only: the development lanes in ci/bin/qci-lane must cover exactly what
# qci's own discovery schedules (agent_scenarios / bats_discover_files), and
# `qci replay` must know every GUI root agent_scenarios enumerates. A new
# scenario root added to one list but not the others breaks qci-lane
# check/audit/list/run before they do any work. No VM, no libvirt.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
}

@test "qci-lane lanes agree with qci discovery" {
    command -v python3 >/dev/null || skip "python3 absent"
    run python3 "$REPO_ROOT/ci/bin/qci-lane" check
    echo "$output"
    [ "$status" -eq 0 ]
    [[ "$output" == *"gui-presentation:"* ]]
}

@test "every agent_scenarios root is a qci replay root" {
    local root
    for root in $(grep -oE 'tests/integration/[a-z-]+gui|tests/integration/qdwin-noctalia|tests/gui|tests/apps' \
                      <(sed -n '/^agent_scenarios()/,/^}/p' "$REPO_ROOT/ci/lib/gates/gui.sh") | sort -u); do
        grep -q "$root\"" "$REPO_ROOT/ci/lib/gates/replay.sh" \
            || { echo "replay.sh lacks $root"; return 1; }
    done
}
