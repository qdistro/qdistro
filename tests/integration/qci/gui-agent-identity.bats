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
    # shellcheck disable=SC1090
    source "$REPO_ROOT/ci/lib/gates/gui.sh"
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
    QCI_AGENT_CMD='claude --model haiku' QCI_AGENT_MODEL=sonnet \
        record_agent_identity
    grep -q '^qci_agent_model=sonnet' "$KV_OUT"
}

@test "record_agent_identity: no QCI_AGENT_CMD -> no-op" {
    unset QCI_AGENT_CMD
    record_agent_identity
    [ ! -s "$KV_OUT" ]
}

@test "record_agent_identity: unknown model when none parseable" {
    QCI_AGENT_CMD='myagent --run {prompt}' record_agent_identity
    grep -q '^qci_agent_model=unknown' "$KV_OUT"
}
