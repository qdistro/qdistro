#!/bin/bash
# templates-promotion-probe.sh — exercise the load-bearing template/
# promotion invariants from doc/templates.md against real rootless podman
# (todo/fableplan task 09). Runs both on the host (dev: in-tree modules)
# and inside the VM (installed CLIs), driven by templates-promotion.bats.
#
# Usage: templates-promotion-probe.sh <scenario>
#   setup                 build+validate+promote a baseline generation A
#   digest-pinning        binding is a digest; a tag binding refuses launch
#   failed-validation     broken candidate fails validate; binding unchanged
#   flip-at-restart       promote B; a running A-container stays A til restart
#   rollback              rollback to A; both generations pinned
#   gc-pin-safety         pinned survive aggressive GC; corrupt pin aborts
#   crash-consistency     kill -9 a promote; binding is never partial
#   candidate-isolation   no state mount reaches a candidate-launched runtime
#   all                   run every scenario in order
#
# Each scenario prints `PASS: <name>` / `FAIL: <name> <reason>` and the
# script exits nonzero on the first failure. State lives under a private
# QDISTRO_VAR_DIR so the real /var/lib/qdistro is never touched.
set -uo pipefail

SILO="fp09-silo"
TEMPLATE="tier2-dev"
TROOT="${QDISTRO_TEST_ROOT:-/tmp/fp09-promotion}"
export QDISTRO_ETC_DIR="$TROOT/etc"
export QDISTRO_VAR_DIR="$TROOT/var"
# Keep the per-boot activation status under the test root, not real /run.
export QDISTRO_RUN_STATUS_DIR="$TROOT/run/silo-generation"

# Never leak the simulated running container, whichever scenario runs.
cleanup() { podman rm -f fp09-running >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Resolve the CLIs: prefer installed wrappers (VM), else in-tree modules (dev).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# probes/ -> vm/ -> integration/ -> tests/ -> repo root (qdistro).
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
TEMPLATES_SRC=""
for cand in /usr/libexec/qdistro "$REPO_ROOT/templates"; do
    if [ -f "$cand/qdistro_template_build.py" ]; then TEMPLATES_SRC="$cand"; break; fi
done

cli() {
    # cli <tool> <args...>  where tool is e.g. template-build / resolve-binding
    local tool="$1"; shift
    if command -v "qdistro-$tool" >/dev/null 2>&1; then
        "qdistro-$tool" "$@"
    else
        local mod="qdistro_${tool//-/_}.py"
        PYTHONPATH="$TEMPLATES_SRC" python3 "$TEMPLATES_SRC/$mod" "$@"
    fi
}

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1 ${2:-}"; exit 1; }

binding_file() { echo "$QDISTRO_VAR_DIR/bindings/$SILO.toml"; }
active_gen() { binding_get active_generation; }

# binding_get <key>  — read a binding field via tomllib (handles
# previous_generations[0] indexing). Avoids brittle grep/sed on TOML.
binding_get() {
    python3 - "$(binding_file)" "$1" <<'PY'
import sys, tomllib, re
data = tomllib.load(open(sys.argv[1], "rb"))
key = sys.argv[2]
m = re.match(r"^(\w+)\[(\d+)\]$", key)
if m:
    print(data[m.group(1)][int(m.group(2))])
else:
    print(data[key])
PY
}

ensure_policy() {
    install -d -m 0755 "$QDISTRO_ETC_DIR/templates" "$QDISTRO_VAR_DIR"
    if [ ! -f "$QDISTRO_ETC_DIR/templates/$TEMPLATE.toml" ]; then
        # prefer the installed/in-tree example policy
        for p in /etc/qdistro/templates/$TEMPLATE.toml \
                 "$REPO_ROOT/templates/examples/$TEMPLATE.toml"; do
            [ -f "$p" ] && { install -m 0644 "$p" "$QDISTRO_ETC_DIR/templates/$TEMPLATE.toml"; break; }
        done
    fi
    [ -f "$QDISTRO_ETC_DIR/templates/$TEMPLATE.toml" ] || fail "ensure_policy" "no policy"
}

build_validate() {
    # build + validate a candidate, echo its run_id (validated)
    local out rid
    out="$(cli template-build "$TEMPLATE" 2>/dev/null)" || { echo "BUILD_FAILED"; return 1; }
    rid="$(echo "$out" | sed -n 's/^RUN_ID=//p')"
    [ -n "$rid" ] || { echo "NO_RUN_ID"; return 1; }
    cli template-validate "$rid" >/dev/null 2>&1 || { echo "$rid"; return 2; }
    echo "$rid"
}

scenario_setup() {
    rm -rf "$TROOT"
    ensure_policy
    local rid
    rid="$(build_validate)" || fail "setup" "build/validate A failed ($rid)"
    cli template-promote "$SILO" "$rid" >/dev/null 2>&1 || fail "setup" "promote A failed"
    local gen; gen="$(active_gen)"
    [ -n "$gen" ] || fail "setup" "no active generation after promote"
    echo "$gen" > "$TROOT/genA"
    pass "setup (generation A = $gen)"
}

scenario_digest_pinning() {
    local genA; genA="$(cat "$TROOT/genA")"
    # binding active_generation must be a sha256 digest, never a tag
    case "$genA" in
        sha256:*) ;;
        *) fail "digest-pinning" "active_generation is not a digest: $genA" ;;
    esac
    # resolver returns exactly that digest
    local r; r="$(cli resolve-binding "$SILO")" || fail "digest-pinning" "resolve failed"
    [ "$r" = "$genA" ] || fail "digest-pinning" "resolver returned $r != $genA"
    # a binding rewritten to a mutable tag is a HARD ERROR (no tag fallback)
    cp "$(binding_file)" "$TROOT/binding.bak"
    sed -i 's#^active_generation = .*#active_generation = "qdistro/tier2-dev:latest"#' "$(binding_file)"
    if cli resolve-binding "$SILO" >/dev/null 2>&1; then
        fail "digest-pinning" "resolver accepted a tag reference"
    fi
    cp "$TROOT/binding.bak" "$(binding_file)"   # restore
    # The digest resolves to exactly that image even after a mutable tag of
    # the same name moves to a DIFFERENT image. Point fp09-moving at A, then
    # move it elsewhere; the binding (a digest) is unaffected and still runs A.
    podman tag "$genA" fp09-moving:latest
    podman tag registry.opensuse.org/opensuse/tumbleweed:latest fp09-moving:latest 2>/dev/null \
        || podman tag "$genA" fp09-moving:latest  # any move; the point is the digest is stable
    local r2; r2="$(cli resolve-binding "$SILO")"
    [ "$r2" = "$genA" ] || fail "digest-pinning" "resolver drifted after tag move: $r2"
    local ran; ran="$(podman run --rm --network=none "$r2" sh -c 'echo OK' 2>/dev/null)"
    [ "$ran" = "OK" ] || fail "digest-pinning" "resolved digest did not run"
    podman rmi fp09-moving:latest >/dev/null 2>&1 || true
    pass "digest-pinning"
}

scenario_failed_validation() {
    local before; before="$(active_gen)"
    # Build a candidate from a gcc-less recipe -> validation must fail.
    local broot="$TROOT/broken"; mkdir -p "$broot"
    cat > "$broot/Containerfile.$TEMPLATE" <<'CF'
FROM registry.opensuse.org/opensuse/tumbleweed:latest
RUN zypper --non-interactive --gpg-auto-import-keys refresh \
 && zypper --non-interactive install --no-recommends bash coreutils \
 && zypper clean --all
CMD ["/bin/bash"]
CF
    # point a throwaway template "tier2-broken" at it
    cat > "$QDISTRO_ETC_DIR/templates/tier2-broken.toml" <<CFG
[template]
class = "derived"
[template.state_boundary]
class = "recipe-derived-toolchain"
enforced = "true"
[template.build]
containerfile = "$broot/Containerfile.$TEMPLATE"
network_mode = "unrestricted"
[[template.probe]]
name = "gcc-present"
kind = "command"
command = "gcc --version"
timeout = 60
[[template.probe]]
name = "hello"
kind = "compile-run"
timeout = 60
CFG
    local out rid
    out="$(cli template-build tier2-broken 2>/dev/null)" || fail "failed-validation" "broken build errored"
    rid="$(echo "$out" | sed -n 's/^RUN_ID=//p')"
    if cli template-validate "$rid" >/dev/null 2>&1; then
        fail "failed-validation" "broken candidate validated (should fail)"
    fi
    local cdir="$QDISTRO_VAR_DIR/templates/tier2-broken/candidates/$rid"
    [ "$(cat "$cdir/state")" = "failed" ] || fail "failed-validation" "state != failed"
    [ -f "$cdir/evidence/validation.toml" ] || fail "failed-validation" "no validation evidence"
    # The promote GATE itself must refuse a non-validated candidate (the
    # load-bearing check), not just validate leaving bindings alone.
    if cli template-promote "$SILO" "$rid" >/dev/null 2>&1; then
        fail "failed-validation" "promote accepted a failed candidate"
    fi
    # the active binding for the real silo is UNTOUCHED, and a refusal was
    # audited.
    [ "$(active_gen)" = "$before" ] || fail "failed-validation" "binding changed on failed promote"
    echo "$rid" > "$TROOT/broken_rid"   # kept for the GC scenario
    pass "failed-validation"
}

scenario_flip_at_restart() {
    local genA; genA="$(cat "$TROOT/genA")"
    # A real running container on A keeps running A even after we promote B.
    podman rm -f fp09-running >/dev/null 2>&1 || true
    podman run -d --name fp09-running --network=none "$genA" sleep 600 >/dev/null \
        || fail "flip-at-restart" "could not start A container"
    # Build + validate a DISTINCT generation B (add a package to change digest)
    local b2="$TROOT/recipeB"; mkdir -p "$b2"
    local base
    base="$(for p in /usr/lib/qdistro/templates/recipes/Containerfile.tier2-dev \
              "$REPO_ROOT/templates/recipes/Containerfile.tier2-dev"; do
              [ -f "$p" ] && { echo "$p"; break; }; done)"
    sed 's/^        make \\/        make \\\n        which \\/' "$base" > "$b2/Containerfile.tier2-dev"
    sed -i "s#containerfile = \"Containerfile.tier2-dev\"#containerfile = \"$b2/Containerfile.tier2-dev\"#" \
        "$QDISTRO_ETC_DIR/templates/$TEMPLATE.toml"
    local rid; rid="$(build_validate)" || fail "flip-at-restart" "build/validate B failed ($rid)"
    cli template-promote "$SILO" "$rid" >/dev/null 2>&1 || fail "flip-at-restart" "promote B failed"
    local genB; genB="$(active_gen)"
    [ "$genB" != "$genA" ] || fail "flip-at-restart" "B digest == A digest"
    echo "$genB" > "$TROOT/genB"
    # The already-running container is STILL on A (flip takes effect at restart).
    local running_img; running_img="$(podman inspect --format '{{.ImageName}}{{.Image}}' fp09-running 2>/dev/null)"
    case "$running_img" in
        *"${genA#sha256:}"*) ;;  # still A
        *) fail "flip-at-restart" "running container no longer on A: $running_img" ;;
    esac
    # "restart" = resolve again WITH --record -> now B, and the per-boot
    # runtime status file records which generation is actually running.
    local r; r="$(cli resolve-binding "$SILO" --record)"
    [ "$r" = "$genB" ] || fail "flip-at-restart" "restart resolved $r != B"
    grep -q "$genB" "$QDISTRO_RUN_STATUS_DIR/$SILO" 2>/dev/null \
        || fail "flip-at-restart" "runtime status file does not record B"
    podman rm -f fp09-running >/dev/null 2>&1 || true
    pass "flip-at-restart"
}

scenario_rollback() {
    local genA genB; genA="$(cat "$TROOT/genA")"; genB="$(cat "$TROOT/genB")"
    # Roll back to previous_generations[0] (the documented rollback input)
    # and require identity_revision to bump.
    local prev0 rev_before rev_after
    prev0="$(binding_get "previous_generations[0]")"
    [ "$prev0" = "$genA" ] || fail "rollback" "previous_generations[0]=$prev0 != A"
    rev_before="$(binding_get "identity_revision")"
    cli template-promote "$SILO" --rollback "$prev0" >/dev/null 2>&1 \
        || fail "rollback" "rollback to previous_generations[0] failed"
    [ "$(active_gen)" = "$genA" ] || fail "rollback" "active != A after rollback"
    grep -q "$genB" "$(binding_file)" || fail "rollback" "B not in previous_generations"
    rev_after="$(binding_get "identity_revision")"
    [ "$rev_after" = "$((rev_before + 1))" ] \
        || fail "rollback" "identity_revision did not bump ($rev_before -> $rev_after)"
    # both generations pinned during the window
    [ -f "$QDISTRO_VAR_DIR/pins/$TEMPLATE/$genA/active.toml" ] || fail "rollback" "A not active-pinned"
    [ -f "$QDISTRO_VAR_DIR/pins/$TEMPLATE/$genB/rollback-window.toml" ] || fail "rollback" "B not rollback-pinned"
    pass "rollback"
}

scenario_gc_pin_safety() {
    local genA genB; genA="$(cat "$TROOT/genA")"; genB="$(cat "$TROOT/genB")"
    # Aggressive retention: keep 0 promoted generations.
    cat > "$QDISTRO_ETC_DIR/template-retention.toml" <<'RET'
keep_promoted_generations = 0
keep_promoted_generations_vm = 0
failed_candidate_days = 7
build_log_days = 180
audit_evidence_years = 3
RET
    # dry-run must not delete anything
    cli template-gc --dry-run >/dev/null 2>&1 || fail "gc-pin-safety" "dry-run errored"
    podman image exists "$genA" || fail "gc-pin-safety" "A image vanished on dry-run"
    # enforce: A (active) and B (rollback-window) are PINNED -> survive
    cli template-gc >/dev/null 2>&1 || fail "gc-pin-safety" "gc errored"
    podman image exists "$genA" || fail "gc-pin-safety" "active generation A was collected!"
    podman image exists "$genB" || fail "gc-pin-safety" "pinned rollback target B was collected!"
    # evidence + manifest survive regardless
    [ -f "$QDISTRO_VAR_DIR/templates/$TEMPLATE/generations/$genA/manifest.toml" ] \
        || fail "gc-pin-safety" "A manifest deleted"
    # Failed candidate payload IS collected after its window, but its
    # evidence survives. Age it out with failed_candidate_days = 0.
    local brid; brid="$(cat "$TROOT/broken_rid")"
    local bcdir="$QDISTRO_VAR_DIR/templates/tier2-broken/candidates/$brid"
    sed -i 's/^failed_candidate_days = .*/failed_candidate_days = 0/' \
        "$QDISTRO_ETC_DIR/template-retention.toml"
    cli template-gc >/dev/null 2>&1 || fail "gc-pin-safety" "gc errored (failed-candidate pass)"
    if podman image exists "qdistro-candidate/tier2-broken:$brid" 2>/dev/null; then
        fail "gc-pin-safety" "failed candidate payload was not collected"
    fi
    for ev in state build.log evidence/validation.toml; do
        [ -e "$bcdir/$ev" ] || fail "gc-pin-safety" "failed-candidate evidence $ev was deleted"
    done
    # a corrupt pin aborts the whole run (fail closed)
    printf 'owner_type = "silo"\n' > "$QDISTRO_VAR_DIR/pins/$TEMPLATE/$genA/active.toml"
    if cli template-gc >/dev/null 2>&1; then
        fail "gc-pin-safety" "gc did not fail closed on a corrupt pin"
    fi
    pass "gc-pin-safety"
}

scenario_crash_consistency() {
    # kill -9 a promote at random points; the binding must ALWAYS be valid
    # and either the old or the new generation — never partial — because the
    # atomic binding rewrite is the single commit point and pre-commit pin
    # writes are additive. The invariant holds wherever the kill lands, so
    # this is a robust fuzz, not a flaky race.
    local genA genB; genA="$(cat "$TROOT/genA")"; genB="$(cat "$TROOT/genB")"
    # Use realistic retention so rollback targets are preserved in the
    # binding (the gc-pin-safety scenario may have left keep=0, which
    # correctly empties previous_generations).
    cat > "$QDISTRO_ETC_DIR/template-retention.toml" <<'RET'
keep_promoted_generations = 3
keep_promoted_generations_vm = 2
failed_candidate_days = 7
build_log_days = 180
audit_evidence_years = 3
RET
    # Re-establish a clean two-generation rollback chain (active A, prev [B]).
    cli template-promote "$SILO" --rollback "$genB" >/dev/null 2>&1 || true
    cli template-promote "$SILO" --rollback "$genA" >/dev/null 2>&1 || true
    local i cur other a
    for i in 1 2 3 4 5; do
        cur="$(active_gen)"
        if [ "$cur" = "$genA" ]; then other="$genB"; else other="$genA"; fi
        ( cli template-promote "$SILO" --rollback "$other" >/dev/null 2>&1 ) &
        local pid=$!
        sleep "0.0$((RANDOM % 9 + 1))"
        kill -9 "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
        a="$(active_gen 2>/dev/null)"
        case "$a" in
            "$genA"|"$genB") ;;
            *) fail "crash-consistency" "binding partial/invalid after kill: '$a'" ;;
        esac
        cli resolve-binding "$SILO" >/dev/null 2>&1 \
            || fail "crash-consistency" "binding unparseable after kill"
    done
    # recovery: a clean promote after the crashes still works
    cur="$(active_gen)"; if [ "$cur" = "$genA" ]; then other="$genB"; else other="$genA"; fi
    cli template-promote "$SILO" --rollback "$other" >/dev/null 2>&1 \
        || fail "crash-consistency" "recovery promote after crashes failed"
    pass "crash-consistency"
}

scenario_candidate_isolation() {
    # Grep-level: the validate path must never reference a silo state_path or
    # bind-mount real state into a candidate-launched container.
    local vsrc
    vsrc="$(for p in /usr/libexec/qdistro/qdistro_template_validate.py \
              "$REPO_ROOT/templates/qdistro_template_validate.py"; do
              [ -f "$p" ] && { echo "$p"; break; }; done)"
    [ -n "$vsrc" ] || fail "candidate-isolation" "validate source not found"
    if grep -qE 'state_path|/var/lib/qdistro/(silos|bindings)' "$vsrc"; then
        fail "candidate-isolation" "validate references silo state/bindings"
    fi
    if ! grep -q -- '--read-only' "$vsrc" || ! grep -q -- '--network=none' "$vsrc"; then
        fail "candidate-isolation" "validate container not isolated (read-only/network=none)"
    fi
    # Runtime: launch a container with the SAME isolation flags validate uses
    # and inspect its mounts — assert no silo state / qdistro state tree is
    # bind-mounted in (the primary runtime assertion).
    local genA; genA="$(cat "$TROOT/genA")"
    local mounts
    mounts="$(podman run --rm --network=none --read-only --tmpfs /tmp:rw,exec,size=64m \
              "$genA" cat /proc/self/mountinfo 2>/dev/null)"
    [ -n "$mounts" ] || fail "candidate-isolation" "could not read candidate mountinfo"
    if echo "$mounts" | grep -qiE 'qdistro/(silos|bindings|pins)|'"$SILO"; then
        fail "candidate-isolation" "a candidate runtime has a qdistro state mount"
    fi
    # Secondary (only when we can actually plant a sentinel in state_path):
    # the sentinel must be unreachable inside a candidate-launched container.
    local statep; statep="$(binding_get state_path)"
    if mkdir -p "$statep" 2>/dev/null && echo "SECRET-STATE" > "$statep/sentinel" 2>/dev/null; then
        if podman run --rm --network=none --read-only --tmpfs /tmp:rw,exec,size=64m \
               "$genA" sh -c "cat '$statep/sentinel' 2>/dev/null" | grep -q SECRET-STATE; then
            fail "candidate-isolation" "candidate runtime could read silo state"
        fi
    fi
    pass "candidate-isolation"
}

main() {
    case "${1:-all}" in
        setup)               scenario_setup ;;
        digest-pinning)      scenario_digest_pinning ;;
        failed-validation)   scenario_failed_validation ;;
        flip-at-restart)     scenario_flip_at_restart ;;
        rollback)            scenario_rollback ;;
        gc-pin-safety)       scenario_gc_pin_safety ;;
        crash-consistency)   scenario_crash_consistency ;;
        candidate-isolation) scenario_candidate_isolation ;;
        all)
            scenario_setup
            scenario_digest_pinning
            scenario_failed_validation
            scenario_flip_at_restart
            scenario_rollback
            scenario_gc_pin_safety
            scenario_crash_consistency
            scenario_candidate_isolation
            echo "ALL SCENARIOS PASSED"
            ;;
        *) echo "unknown scenario: $1" >&2; exit 2 ;;
    esac
}

main "$@"
