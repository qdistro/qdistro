#!/usr/bin/env bats
#
# Host-only tests for the agent-identity recorder (ci/lib/gates/gui.sh, H6a): the
# model/agent identity must be recorded so a debug rerun with a stronger model is
# never confused with a CI row. Covers the pure model parser and that
# record_agent_identity writes the expected manifest keys (kv stubbed).

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    KV_OUT="$BATS_TEST_TMPDIR/kv.txt"; : > "$KV_OUT"
    kv() { printf '%s=%s\n' "$1" "$2" >> "$KV_OUT"; }
    # The surrounding qci invocation may select a different visual model. These
    # unit cases exercise their own explicit inputs and must not inherit it.
    unset QCI_AGENT_CMD QCI_AGENT_MODEL
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
}

teardown() {
    if [ -n "${PRESERVED_AGENT_CWD:-}" ]; then
        rm -rf -- "$PRESERVED_AGENT_CWD"
    fi
}

@test "model parse: --model sonnet" {
    [ "$(gui_agent_model_from_cmd 'claude -p x --model sonnet')" = sonnet ]
}

@test "model parse: --model=us.anthropic.claude-x form" {
    [ "$(gui_agent_model_from_cmd 'claude --model=us.anthropic.claude-3-5')" = "us.anthropic.claude-3-5" ]
}

@test "model parse: codex -m short form" {
    [ "$(gui_agent_model_from_cmd 'codex exec -m gpt-5.4-mini --dangerously-bypass-approvals-and-sandbox')" = "gpt-5.4-mini" ]
}

@test "model parse: no --model -> empty" {
    [ -z "$(gui_agent_model_from_cmd 'claude -p x --dangerously-skip-permissions')" ]
}

@test "record_agent_identity: records cmd + model keys" {
    QCI_AGENT_CMD='timeout 1800 claude -p "$(cat {prompt})" --model haiku' \
        record_agent_identity
    grep -q '^qci_agent_cmd=' "$KV_OUT"
    grep -q '^qci_agent_model=haiku' "$KV_OUT"
}

@test "record_agent_identity: QCI_AGENT_MODEL overrides parsed model" {
    QCI_AGENT_CMD='claude --model haiku' QCI_AGENT_MODEL=gpt-5.6-luna \
        record_agent_identity
    grep -q '^qci_agent_model=gpt-5.6-luna' "$KV_OUT"
}

@test "record_agent_identity: no QCI_AGENT_CMD -> no-op" {
    unset QCI_AGENT_CMD
    record_agent_identity
    [ ! -s "$KV_OUT" ]
}

@test "record_agent_identity: Haiku is the fallback when no model is specified" {
    QCI_AGENT_CMD='myagent --run {prompt}' record_agent_identity
    grep -q '^qci_agent_model=haiku' "$KV_OUT"
}

@test "run_agent_command: relative tool outputs stay in a cleaned temporary cwd" {
    local prompt="$BATS_TEST_TMPDIR/prompt.md"
    local log="$BATS_TEST_TMPDIR/agent.log"
    local cwd_record="$BATS_TEST_TMPDIR/agent-cwd.txt"
    printf '# fixture\n' > "$prompt"
    export QCI_AGENT_CWD_RECORD="$cwd_record"

    QCI_AGENT_CMD='printf "%s\n" "$PWD" > "$QCI_AGENT_CWD_RECORD"; : > "txt:-"; test -f "txt:-"; # {prompt}' \
        run run_agent_command "$prompt" "$log"

    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    local agent_cwd
    agent_cwd=$(cat "$cwd_record")
    [[ "$agent_cwd" == "${TMPDIR:-/tmp}"/qci-agent.* ]]
    [ ! -d "$agent_cwd" ]
    grep -Fxq "qci_agent_workdir=$agent_cwd (removed after success)" "$log"
    [ ! -e "$REPO_ROOT/txt:-" ]
}

@test "run_agent_command: failed agent preserves temporary cwd and logs its path" {
    local prompt="$BATS_TEST_TMPDIR/prompt.md"
    local log="$BATS_TEST_TMPDIR/agent.log"
    local cwd_record="$BATS_TEST_TMPDIR/agent-cwd.txt"
    printf '# fixture\n' > "$prompt"
    export QCI_AGENT_CWD_RECORD="$cwd_record"

    QCI_AGENT_CMD='printf "%s\n" "$PWD" > "$QCI_AGENT_CWD_RECORD"; : > "failure-evidence.txt"; exit 7; # {prompt}' \
        run run_agent_command "$prompt" "$log"

    [ "$status" -eq 7 ] || { echo "$output" >&2; return 1; }
    PRESERVED_AGENT_CWD=$(cat "$cwd_record")
    [[ "$PRESERVED_AGENT_CWD" == "${TMPDIR:-/tmp}"/qci-agent.* ]]
    [ -f "$PRESERVED_AGENT_CWD/failure-evidence.txt" ]
    grep -Fxq \
        "qci_agent_workdir=$PRESERVED_AGENT_CWD (preserved after agent exit 7)" \
        "$log"
    [ ! -e "$REPO_ROOT/failure-evidence.txt" ]
}

@test "run_agent_command: host desktop sockets and activation channels are isolated" {
    command -v bwrap >/dev/null 2>&1 || skip "bubblewrap not installed"
    local prompt="$BATS_TEST_TMPDIR/prompt.md"
    local log="$BATS_TEST_TMPDIR/agent.log"
    local env_record="$BATS_TEST_TMPDIR/agent-env.txt"
    printf '# fixture\n' > "$prompt"
    export QCI_AGENT_ENV_RECORD="$env_record"

    DISPLAY=:99 \
    WAYLAND_DISPLAY=wayland-0 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/999/bus \
    QCI_AGENT_CMD='printf "DISPLAY=%s\nWAYLAND_DISPLAY=%s\nDBUS_SESSION_BUS_ADDRESS=%s\nBROWSER=%s\nQCI_HOST_GUI_ISOLATED=%s\n" "$DISPLAY" "$WAYLAND_DISPLAY" "$DBUS_SESSION_BUS_ADDRESS" "$BROWSER" "$QCI_HOST_GUI_ISOLATED" > "$QCI_AGENT_ENV_RECORD"; test ! -S /tmp/.X11-unix/X0; test ! -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/wayland-0"; test ! -S "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/bus"; # {prompt}' \
        run run_agent_command "$prompt" "$log"

    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    grep -Fxq 'DISPLAY=' "$env_record"
    grep -Fxq 'WAYLAND_DISPLAY=qci-host-display-disabled' "$env_record"
    grep -Fxq 'DBUS_SESSION_BUS_ADDRESS=unix:path=/dev/null' "$env_record"
    grep -Fxq 'BROWSER=/bin/false' "$env_record"
    grep -Fxq 'QCI_HOST_GUI_ISOLATED=1' "$env_record"
}
