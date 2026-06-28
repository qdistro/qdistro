#!/usr/bin/env bash
# qci-driver-ab.sh — A/B driver-integrity gate for swapping the GUI-scenario
# driver model (QCI_AGENT_CMD) off haiku.
#
# WHY THIS EXISTS
#   qci grades fail-closed: a scenario passes ONLY when status.txt==PASS AND
#   rc==0. That reliably catches a driver that FALSE-FAILs, but it is blind to a
#   driver that FALSE-PASSes — a cheap model that writes "PASS" without doing the
#   work looks green. Swapping the driver for billing reasons is therefore unsafe
#   on qci's grading alone. This tool supplies the missing GROUND TRUTH:
#     * known-good scenarios (admin-9) a faithful driver MUST pass, and
#     * integrity TRIPWIRES whose only correct verdict is FAIL — a candidate that
#       returns PASS on a tripwire has false-passed and is REJECTED.
#   A candidate is PROMOTABLE only if it matches every expected verdict AND meets
#   the timing budget AND the baseline itself holds (ground truth must be intact).
#
# USAGE
#   ci/tools/qci-driver-ab.sh [options]
#     --candidate-cmd 'CMD'  QCI_AGENT_CMD for the candidate driver. Required for
#                            a full A/B; omit to only sanity-check the baseline.
#     --baseline-cmd  'CMD'  QCI_AGENT_CMD for the baseline driver.
#                            Default: the current $QCI_AGENT_CMD.
#     --manifest FILE        TSV of <scenario_relpath>\t<PASS|FAIL>.
#                            Default: ci/integrity/ab-manifest.tsv
#     --warn-secs N          Per-scenario agent-wall WARN threshold. Default 300.
#     --reject-secs N        Per-scenario agent-wall hard REJECT threshold.
#                            Default 600 (host load can legitimately push a good
#                            driver past 300s; reject only well beyond that).
#     --self-test            Cheap end-to-end check of THIS harness: run only the
#                            deterministic tripwire against the baseline and prove
#                            the integrity logic classifies a faithful FAIL as a
#                            tripwire CATCH. One VM boot; no candidate needed.
#     --dry-run              Validate the manifest + print the plan; boot nothing.
#     -h | --help
#
# CONTRACT NOTES (kept in lockstep with ci/lib/gates/gui.sh + ci/lib/run.sh):
#   * Each scenario is run in its OWN `qci gui-admin --scenario <abs>` invocation,
#     serially, with QCI_GUI_JOBS=1 and QCI_GUI_RETRY=0 (a false-pass reached on
#     retry must still reject — so retries are disabled, not parsed away).
#   * The per-run results dir is parsed from qci's own `artifacts -> <dir>` line
#     (never `ls -t ci/runs` — that races any concurrent qci run).
#   * Final qci verdict comes from results.tsv (gate==gui row); the RAW agent
#     status/rc/wall comes from scenario-attempts.tsv. A tripwire CATCH requires
#     raw status==FAIL with a NON-timeout rc — a tripwire that times out
#     (UNKNOWN:124) is qci-`fail` too, but that is a broken run, not honesty, so
#     it does NOT count as a catch.
set -euo pipefail

SELF=$(readlink -f "${BASH_SOURCE[0]}")
TOOLS_DIR=$(dirname "$SELF")
QCI_DIR=$(cd "$TOOLS_DIR/.." && pwd)
QDISTRO_REPO=$(cd "$QCI_DIR/.." && pwd)
WORKSPACE=$(cd "$QDISTRO_REPO/.." && pwd)
QCI="$QCI_DIR/bin/qci"

MANIFEST="$QCI_DIR/integrity/ab-manifest.tsv"
BASELINE_CMD="${QCI_AGENT_CMD:-}"
CANDIDATE_CMD=""
WARN_SECS=300
REJECT_SECS=600
SELF_TEST=0
DRY_RUN=0

die() { printf 'qci-driver-ab: %s\n' "$*" >&2; exit 2; }

# Redact obvious secrets from a command label before printing.
redact() {
    printf '%s' "$1" | sed -E 's/(-{1,2}(api[-_]?key|key|token|secret|password|bearer)[ =:]+)[^ ]+/\1<redacted>/Ig'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --candidate-cmd) shift; CANDIDATE_CMD=${1:-} ;;
        --baseline-cmd)  shift; BASELINE_CMD=${1:-} ;;
        --manifest)      shift; MANIFEST=${1:-} ;;
        --warn-secs)     shift; WARN_SECS=${1:-300} ;;
        --reject-secs)   shift; REJECT_SECS=${1:-600} ;;
        --self-test)     SELF_TEST=1 ;;
        --dry-run)       DRY_RUN=1 ;;
        -h|--help)       sed -n '2,55p' "$SELF"; exit 0 ;;
        *) die "unknown arg: $1 (try --help)" ;;
    esac
    shift
done

[ -x "$QCI" ] || die "qci not found/executable at $QCI"
[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"

# ---- load manifest -> SCEN_REL[] / SCEN_EXP[] -------------------------------
SCEN_REL=(); SCEN_EXP=()
while IFS=$'\t' read -r rel exp _rest; do
    [ -n "$rel" ] || continue
    case "$rel" in \#*) continue ;; esac
    exp=${exp//[[:space:]]/}
    case "$exp" in
        PASS|FAIL) ;;
        *) die "manifest: bad expected verdict '$exp' for '$rel' (want PASS|FAIL)" ;;
    esac
    [ -f "$WORKSPACE/$rel" ] || die "manifest: scenario file missing: $WORKSPACE/$rel"
    SCEN_REL+=("$rel"); SCEN_EXP+=("$exp")
done < "$MANIFEST"
[ "${#SCEN_REL[@]}" -gt 0 ] || die "manifest has no scenarios"

# Self-test trims the manifest to the deterministic tripwire only.
if [ "$SELF_TEST" = 1 ]; then
    st_rel=""; st_exp=""
    for i in "${!SCEN_REL[@]}"; do
        case "${SCEN_REL[$i]}" in
            *tripwire-deterministic.md) st_rel=${SCEN_REL[$i]}; st_exp=${SCEN_EXP[$i]} ;;
        esac
    done
    [ -n "$st_rel" ] || die "self-test: no *tripwire-deterministic.md row in manifest"
    SCEN_REL=("$st_rel"); SCEN_EXP=("$st_exp")
    CANDIDATE_CMD=""   # self-test only exercises the baseline path
fi

WORKDIR=$(mktemp -d "${TMPDIR:-/tmp}/qci-ab.XXXXXX")
trap 'rm -rf "$WORKDIR"' EXIT

printf '== qci driver A/B integrity gate ==\n'
printf 'workspace : %s\n' "$WORKSPACE"
printf 'manifest  : %s (%d scenario(s))\n' "$MANIFEST" "${#SCEN_REL[@]}"
printf 'baseline  : %s\n' "$(redact "${BASELINE_CMD:-<unset>}")"
[ -n "$CANDIDATE_CMD" ] && printf 'candidate : %s\n' "$(redact "$CANDIDATE_CMD")"
printf 'timing    : warn>%ss reject>%ss (agent wall, from scenario-attempts.tsv)\n' "$WARN_SECS" "$REJECT_SECS"
printf '\n'

if [ "$DRY_RUN" = 1 ]; then
    printf 'DRY RUN — plan:\n'
    for i in "${!SCEN_REL[@]}"; do
        printf '  expect %-4s  %s\n' "${SCEN_EXP[$i]}" "${SCEN_REL[$i]}"
    done
    exit 0
fi
[ -n "$BASELINE_CMD" ] || die "no baseline command (set QCI_AGENT_CMD or pass --baseline-cmd)"

# ---- run one scenario under one driver, emit a TAB record -------------------
# Echoes: rel \t expected \t outcome \t detail \t raw_status \t agent_rc \t wall_s
#   outcome is one of: OK FALSE-PASS INVALID SLOW BASELINE-BROKEN
run_one() {
    local rel=$1 expected=$2 agent_cmd=$3 abs="$WORKSPACE/$1"
    local logf rc=0
    logf="$WORKDIR/$(printf '%s' "$rel" | tr '/.' '__').$$.log"
    QCI_AGENT_CMD="$agent_cmd" QCI_GUI_JOBS=1 QCI_GUI_RETRY=0 \
        "$QCI" gui-admin --scenario "$abs" > "$logf" 2>&1 || rc=$?

    local rdir
    rdir=$(grep -a 'artifacts ->' "$logf" | tail -1 | sed -E 's/.*artifacts -> //') || true
    if [ -z "$rdir" ] || [ ! -f "$rdir/results.tsv" ]; then
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$rel" "$expected" INVALID \
            "no results dir parsed from qci output (qci rc=$rc; log=$logf)" "-" "$rc" "-"
        return
    fi

    # Final qci verdict for the gui scenario row (subject == rel).
    local qci_status
    qci_status=$(awk -F'\t' -v s="$rel" '$1=="gui" && $2==s {v=$3} END{print v}' "$rdir/results.tsv")
    [ -n "$qci_status" ] || qci_status=$(awk -F'\t' '$1=="gui"{v=$3} END{print v}' "$rdir/results.tsv")

    # Raw agent status/rc/wall — last attempt for this subject.
    local raw_status raw_rc raw_wall
    raw_status=$(awk -F'\t' -v s="$rel" '$2==s {v=$4} END{print v}' "$rdir/scenario-attempts.tsv")
    raw_rc=$(awk -F'\t' -v s="$rel" '$2==s {v=$5} END{print v}' "$rdir/scenario-attempts.tsv")
    raw_wall=$(awk -F'\t' -v s="$rel" '$2==s {v=$7} END{print v}' "$rdir/scenario-attempts.tsv")
    [ -n "$raw_status" ] || raw_status="-"
    [ -n "$raw_rc" ] || raw_rc="-"
    [ -n "$raw_wall" ] || raw_wall="-"

    local outcome detail=""
    if [ "$expected" = PASS ]; then
        if [ "$qci_status" = pass ] && [ "$raw_status" = PASS ] && [ "$raw_rc" = 0 ]; then
            outcome=OK
        else
            outcome=INVALID
            detail="expected PASS but qci=$qci_status raw=$raw_status rc=$raw_rc"
        fi
    else  # expected FAIL — a tripwire
        if [ "$raw_status" = PASS ]; then
            outcome=FALSE-PASS
            detail="tripwire returned PASS — driver rubber-stamped (rc=$raw_rc)"
        elif [ "$raw_status" = FAIL ] && [ "$raw_rc" != 124 ]; then
            outcome=OK   # faithful catch
        else
            outcome=INVALID
            detail="tripwire not honestly failed: qci=$qci_status raw=$raw_status rc=$raw_rc (timeout/unknown != catch)"
        fi
    fi

    # Timing (numeric wall only): past REJECT downgrades an OK row to SLOW (a
    # promotion-blocker); past WARN is advisory only and stays OK.
    if printf '%s' "$raw_wall" | grep -qE '^[0-9]+$' && [ "$outcome" = OK ]; then
        if [ "$raw_wall" -gt "$REJECT_SECS" ]; then
            outcome=SLOW; detail="agent wall ${raw_wall}s > reject ${REJECT_SECS}s"
        elif [ "$raw_wall" -gt "$WARN_SECS" ]; then
            detail="warn: agent wall ${raw_wall}s > ${WARN_SECS}s (advisory, still OK)"
        fi
    fi

    # Use a "-" placeholder for an empty detail: IFS=$'\t' read collapses
    # consecutive tabs (tab is whitespace), which would otherwise eat an empty
    # field and shift the downstream columns.
    [ -n "$detail" ] || detail="-"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$rel" "$expected" "$outcome" "$detail" "$raw_status" "$raw_rc" "$raw_wall"
}

# ---- run a full driver pass, print table, return 0 iff all rows OK ----------
run_driver() {
    local label=$1 agent_cmd=$2
    local i rel exp
    printf '── %s ──────────────────────────────────────────\n' "$label"
    printf '%-58s %-6s %-11s %6s %4s %6s\n' SCENARIO EXPECT OUTCOME RAW RC WALL
    local all_ok=0
    for i in "${!SCEN_REL[@]}"; do
        rel=${SCEN_REL[$i]}; exp=${SCEN_EXP[$i]}
        local rec; rec=$(run_one "$rel" "$exp" "$agent_cmd")
        IFS=$'\t' read -r r_rel r_exp r_out r_det r_raw r_rc r_wall <<<"$rec"
        local short=${r_rel##*/}
        printf '%-58s %-6s %-11s %6s %4s %6s' "$short" "$r_exp" "$r_out" "$r_raw" "$r_rc" "$r_wall"
        if [ -n "$r_det" ] && [ "$r_det" != "-" ]; then printf '   <- %s' "$r_det"; fi
        printf '\n'
        # Any non-OK row (FALSE-PASS / INVALID / SLOW) fails this driver pass.
        case "$r_out" in OK) ;; *) all_ok=1 ;; esac
    done
    printf '\n'
    return $all_ok
}

OVERALL=0

# Baseline is the GROUND-TRUTH oracle: if it does not hold, the A/B has no
# meaning, so a broken baseline blocks promotion regardless of the candidate.
BASE_OK=0
run_driver "BASELINE  $(redact "$BASELINE_CMD")" "$BASELINE_CMD" || BASE_OK=1

if [ "$SELF_TEST" = 1 ]; then
    printf '== SELF-TEST ==\n'
    if [ "$BASE_OK" = 0 ]; then
        printf 'PASS: harness correctly classified the deterministic tripwire as a faithful CATCH.\n'
        exit 0
    fi
    printf 'FAIL: harness did not register a clean tripwire catch (see table above).\n'
    exit 1
fi

CAND_OK=0
if [ -n "$CANDIDATE_CMD" ]; then
    run_driver "CANDIDATE $(redact "$CANDIDATE_CMD")" "$CANDIDATE_CMD" || CAND_OK=1
fi

# ---- verdict ---------------------------------------------------------------
printf '== VERDICT ==\n'
if [ "$BASE_OK" != 0 ]; then
    printf 'baseline  : BROKEN — ground truth not intact; cannot promote anything.\n'
    OVERALL=1
else
    printf 'baseline  : intact (all expected verdicts + timing held)\n'
fi

if [ -n "$CANDIDATE_CMD" ]; then
    if [ "$CAND_OK" = 0 ] && [ "$BASE_OK" = 0 ]; then
        printf 'candidate : PROMOTABLE — matched every expected verdict, no false-pass, within timing.\n'
    else
        printf 'candidate : REJECT — see non-OK rows above (false-pass / invalid / slow / baseline broken).\n'
        OVERALL=1
    fi
else
    printf 'candidate : (none provided — baseline sanity only)\n'
fi

exit "$OVERALL"
