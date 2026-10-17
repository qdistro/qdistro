#!/usr/bin/env bats
#
# Host-only tests for the secondary ("advanced") GUI agent (ci/lib/gates/gui.sh,
# gui_agent_profile): scenarios listed in ci/gui-agent-advanced.tsv run under
# QCI_AGENT_ADVANCED_CMD when the operator sets it, every other scenario (and
# every scenario when it is unset) under QCI_AGENT_CMD, and each attempt log and
# the manifest record which agent drove it.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    KV_OUT="$BATS_TEST_TMPDIR/kv.txt"; : > "$KV_OUT"
    kv() { printf '%s=%s\n' "$1" "$2" >> "$KV_OUT"; }
    unset QCI_AGENT_CMD QCI_AGENT_MODEL QCI_AGENT_ADVANCED_CMD QCI_AGENT_ADVANCED_MODEL
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
    GUI_AGENT_ADVANCED_REGISTRY="$BATS_TEST_TMPDIR/advanced.tsv"
    printf '# comment\n\npermissions-gui/61-window-chrome-context-menu.md\tfixture reason\n' \
        > "$GUI_AGENT_ADVANCED_REGISTRY"
    LISTED=/repo/tests/integration/permissions-gui/61-window-chrome-context-menu.md
    UNLISTED=/repo/tests/integration/permissions-gui/35-tui-and-qt-concurrent.md
}

# run_agent_command needs bwrap; stand in a pass-through when the host has none
# (same shape as gui-agent-identity.bats).
use_bwrap_or_passthrough() {
    command -v bwrap >/dev/null 2>&1 && return 0
    mkdir -p "$BATS_TEST_TMPDIR/bwrap-bin"
    cat > "$BATS_TEST_TMPDIR/bwrap-bin/bwrap" <<'SH'
#!/usr/bin/env bash
while [ "$#" -gt 0 ]; do
    case "$1" in
        --die-with-parent) shift ;;
        --tmpfs) shift 2 ;;
        --dev-bind|--ro-bind) shift 3 ;;
        --*) echo "bwrap pass-through: unexpected option $1" >&2; exit 125 ;;
        *) break ;;
    esac
done
exec "$@"
SH
    chmod +x "$BATS_TEST_TMPDIR/bwrap-bin/bwrap"
    PATH="$BATS_TEST_TMPDIR/bwrap-bin:$PATH"
}

@test "profile: without QCI_AGENT_ADVANCED_CMD every scenario is default" {
    [ "$(gui_agent_profile "$LISTED")" = default ]
    [ "$(gui_agent_profile "$UNLISTED")" = default ]
}

@test "profile: a listed scenario is advanced once the advanced agent is pinned" {
    QCI_AGENT_ADVANCED_CMD='codex exec -m strong - < {prompt}'
    [ "$(gui_agent_profile "$LISTED")" = advanced ]
    [ "$(gui_agent_profile "$UNLISTED")" = default ]
    [ "$(gui_agent_profile "")" = default ]
}

@test "profile: the suffix matches at a path boundary only" {
    QCI_AGENT_ADVANCED_CMD='codex exec -m strong - < {prompt}'
    [ "$(gui_agent_profile /repo/other-permissions-gui/61-window-chrome-context-menu.md)" = default ]
    [ "$(gui_agent_profile /repo/permissions-gui/x61-window-chrome-context-menu.md)" = default ]
    [ "$(gui_agent_profile permissions-gui/61-window-chrome-context-menu.md)" = advanced ]
}

@test "profile: a missing registry leaves every scenario default" {
    QCI_AGENT_ADVANCED_CMD='codex exec -m strong - < {prompt}'
    GUI_AGENT_ADVANCED_REGISTRY="$BATS_TEST_TMPDIR/absent.tsv"
    [ "$(gui_agent_profile "$LISTED")" = default ]
}

@test "model: each profile reports its own model" {
    QCI_AGENT_CMD='codex exec -m gpt-6-luna - < {prompt}'
    QCI_AGENT_ADVANCED_CMD='wrapper {prompt}'
    [ "$(gui_agent_model_for default)" = gpt-6-luna ]
    [ "$(gui_agent_model_for advanced)" = unknown ]
    QCI_AGENT_ADVANCED_MODEL=gpt-6.1-sol
    [ "$(gui_agent_model_for advanced)" = gpt-6.1-sol ]
}

@test "run_agent_command: a listed scenario runs the advanced agent and the log says so" {
    use_bwrap_or_passthrough
    local prompt="$BATS_TEST_TMPDIR/prompt.md" log="$BATS_TEST_TMPDIR/agent.log"
    printf '# fixture\n' > "$prompt"
    export QCI_AGENT_CMD='echo DEFAULT-AGENT; # {prompt}'
    export QCI_AGENT_ADVANCED_CMD='echo ADVANCED-AGENT; # {prompt}'
    export QCI_AGENT_ADVANCED_MODEL=strong-model
    QCI_SCENARIO_FILE="$LISTED" run run_agent_command "$prompt" "$log"
    [ "$status" -eq 0 ]
    grep -qx ADVANCED-AGENT "$log"
    if grep -q DEFAULT-AGENT "$log"; then echo "default agent ran"; false; fi
    grep -qx "qci_agent_profile=advanced qci_agent_model=strong-model" "$log"

    QCI_SCENARIO_FILE="$UNLISTED" run run_agent_command "$prompt" "$log"
    [ "$status" -eq 0 ]
    grep -qx DEFAULT-AGENT "$log"
    if grep -q ADVANCED-AGENT "$log"; then echo "advanced agent ran"; false; fi
    grep -q "^qci_agent_profile=default " "$log"
}

@test "run_agent_command: the rollout observer sees the attempt's template; QCI_AGENT_CMD is restored" {
    use_bwrap_or_passthrough
    local prompt="$BATS_TEST_TMPDIR/prompt.md" log="$BATS_TEST_TMPDIR/agent.log"
    local seen="$BATS_TEST_TMPDIR/observer-saw"
    printf '# fixture\n' > "$prompt"
    gui_observe_attempt() { printf '%s\n' "$QCI_AGENT_CMD" >> "$seen"; }
    QCI_AGENT_CMD='true --ephemeral DEFAULT; # {prompt}'
    QCI_AGENT_ADVANCED_CMD='true ADVANCED; # {prompt}'
    QCI_SCENARIO_FILE="$LISTED" run_agent_command "$prompt" "$log" "$BATS_TEST_TMPDIR/obs1"
    [ "$QCI_AGENT_CMD" = 'true --ephemeral DEFAULT; # {prompt}' ]
    QCI_SCENARIO_FILE="$UNLISTED" run_agent_command "$prompt" "$log" "$BATS_TEST_TMPDIR/obs2"
    [ "$QCI_AGENT_CMD" = 'true --ephemeral DEFAULT; # {prompt}' ]
    [ "$(sed -n 1p "$seen")" = 'true ADVANCED; # {prompt}' ]
    [ "$(sed -n 2p "$seen")" = 'true --ephemeral DEFAULT; # {prompt}' ]
}

@test "manifest: the advanced agent's identity and scenarios are recorded" {
    QCI_AGENT_CMD='codex exec -m gpt-6-luna -c model_reasoning_effort=medium - < {prompt}'
    QCI_AGENT_ADVANCED_CMD='codex exec -m gpt-6.1-sol -c model_reasoning_effort=low - < {prompt}'
    record_advanced_agent_identity
    grep -qx 'qci_agent_advanced_model=gpt-6.1-sol' "$KV_OUT"
    grep -qx 'qci_agent_advanced_reasoning_effort=low' "$KV_OUT"
    grep -qx 'qci_agent_advanced_scenarios=permissions-gui/61-window-chrome-context-menu.md' "$KV_OUT"
}

@test "manifest: nothing is recorded without an advanced agent" {
    record_advanced_agent_identity
    [ ! -s "$KV_OUT" ]
}

@test "registry: every row of ci/gui-agent-advanced.tsv names one existing scenario with a reason" {
    local suffix reason n=0
    while IFS=$'\t' read -r suffix reason; do
        case "$suffix" in ''|'#'*) continue ;; esac
        n=$((n + 1))
        [ -n "$reason" ] || { echo "no reason: $suffix"; false; }
        # Tracked files only: a staged source copy (image/root/root/
        # qdistro-src in a checkout that built an image) is not a scenario.
        [ "$(git -C "$REPO_ROOT" ls-files | grep -cE "(^|/)${suffix//./\\.}\$")" -eq 1 ] \
            || { echo "not exactly one scenario: $suffix"; false; }
    done < "$REPO_ROOT/ci/gui-agent-advanced.tsv"
    [ "$n" -gt 0 ]
}
