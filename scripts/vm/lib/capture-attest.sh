# shellcheck shell=bash
#
# capture-attest.sh — the harness side of the qci GUI VISUAL-EVIDENCE CONTRACT.
#
# Sourced by every tool that CAPTURES A FRAME FROM A TEST VM:
#   * qdistro/scripts/vm/vm-gui              (virsh screenshot: labwc/admin lane)
#   * qdwin/tests/gui/qdwin-helpers.sh       (in-guest qdshell capture: qdwin lane)
#   * qdwin/tests/apps/qdwin-apps-helpers.sh (virsh screenshot: qdwin apps lane)
#
# Each successful capture appends one row to the capture ledger the GUI gate
# provisioned for this scenario attempt (QCI_GUI_CAPTURE_LOG). The gate grades
# ONLY the frames that appear in that ledger.
#
# ---------------------------------------------------------------------------
# WHAT THIS IS AND IS NOT — read this before extending it.
#
# THE TRUST MODEL (owner decision, 2026-09-16): the scenario driver is TRUSTED.
# This library is an ACCIDENT DETECTOR and a FORENSIC RECORD, not an adversarial
# boundary, and must never be described as one.
#
# The scenario agent runs as the SAME HOST UID as the gate, so nothing here is
# unforgeable AS BUILT:
#
#   * any file this library reads (the ledger, its seed) the agent can read;
#   * any function this library exposes the agent can call;
#   * any secret held in this process is readable at the same uid;
#   * therefore an agent willing to re-implement `sha256sum` over the ledger's
#     own published format can append a row for bytes it invented.
#
# An earlier version of this comment went further and said no in-process scheme
# could ever tell that row from a real one. That was FALSE. A pre-agent
# supervisor can set PR_SET_DUMPABLE=0, hold a key in a sealed memfd across a
# close-on-exec agent launch, and MAC each row; without CAP_SYS_PTRACE the agent
# cannot reach that key. Such a boundary is BUILDABLE and was DECLINED, because
# every visual failure this project has actually had was an honest driver taking
# a bad shortcut or a flaky capture -- never forgery -- and a record you can
# diagnose from was judged worth more than a barrier. Write "declined", not
# "impossible"; the difference is the whole review history of this file.
#
# So this library does NOT claim to prove a frame came from a VM. It claims,
# exactly:
#
#   1. THE PREFERRED ENTRY POINT TAKES THE PICTURE ITSELF. The virsh lanes go
#      through capture_virsh_screenshot, which runs `virsh screenshot` and
#      attests only the file it just wrote; nothing chooses those bytes.
#
#      capture_attest_frame IS A CALLER-SUPPLIED-BYTES ENTRY POINT, and this
#      is stated plainly because an earlier version of this bullet claimed no
#      entry point here blesses arbitrary bytes on request, which BOTH B-round-1
#      reviewers refuted in one call: hand it any readable file and the right VM
#      name and it writes a row. It exists because the in-guest qdwin lane's
#      capture is a multi-step guest protocol (qdshell request,
#      compositor-identity check, fetch, publish) that cannot be performed from
#      here, so that tool must hand the finished file over. What it checks is
#      the ledger's bound VM (claim 2) and nothing about provenance: VM-name
#      equality is not evidence of capture. It is a convenience for the capture
#      tools, NOT a boundary. There is no command-line "attest this file"
#      subcommand, so reaching it means calling a shell function from inside
#      the harness — which, under the trust model above, is an accident to
#      diagnose rather than an attack to stop.
#   2. EVERY ROW IS BOUND TO ONE VM. The gate writes the VM it assigned into
#      the ledger header; this library REFUSES to append a row for any other
#      VM. Under QCI_JOBS=8 the official helpers cannot be pointed at a
#      neighbour's healthy VM to manufacture evidence.
#   3. THE LEDGER IS TAMPER-EVIDENT. Rows are hash-chained from a seed, so
#      interior deletion, reordering, substitution and reseeding are all
#      detected by the gate.
#   4. AFTER THE AGENT EXITS THE LEDGER IS FROZEN. The gate appends a SEAL row
#      containing a nonce it generated in its own process memory and keeps the
#      resulting chain head in memory only. Any later edit — including tail
#      truncation — changes the head and is detected.
#
# What is NOT claimed, anywhere: that a row proves a capture happened. See the
# VISUAL-EVIDENCE CONTRACT block in ci/lib/gates/gui.sh for the full statement
# of the residual and what would close it.
# ---------------------------------------------------------------------------
#
# Failure policy: a REFUSAL (wrong VM, unusable ledger) is LOUD on stderr and
# returns non-zero, so a capture tool fails rather than returning an
# unattested frame as if it were evidence. Absence of a ledger (an ordinary
# by-hand run outside qci) is a silent success: these tools stay usable.

QCI_CAPTURE_LOG_MAGIC='#qci-capture-log'
QCI_CAPTURE_LOG_VERSION=2

# The VM a ledger is bound to (header field 4), or empty when the header is
# missing, the wrong version, or unbound. Args: log_path.
# The magic/version are spelled out here rather than read from the globals
# above: this function is EXPORTED and runs inside the `flock bash -c` child,
# which inherits exported functions but not unexported variables. Reading them
# from globals silently yielded an empty version there, which made every row
# refusal read "not a v capture ledger".
_qci_capture_bound_vm() {
    head -1 -- "${1:-}" 2>/dev/null | awk -F'\t' \
        '$1 == "#qci-capture-log" && $2 == "2" { print $4 }'
}

# INTERNAL. Append one chained row. Never call this from a scenario or a tool:
# it is the raw row writer, and the public entry points above it are where the
# VM gating and the capture live.
# Args: log_path captured_file vm_name [artifact_root] [scope_override]
_qci_capture_attest_row() {
    local log=$1 out=$2 vm=$3 root=${4:-} forced=${5:-}
    local bound sum bytes abs scope prev seq ts chain payload rroot
    bound=$(_qci_capture_bound_vm "$log")
    if [ -z "$bound" ]; then
        printf 'capture-attest: REFUSED — %s is not a v%s capture ledger bound to a VM\n' \
            "$log" "2" >&2
        return 1
    fi
    # VM BINDING. The ledger says which VM this scenario attempt was assigned;
    # a capture of any other VM is not evidence for this scenario, however
    # honestly it was taken.
    if [ "$vm" != "$bound" ]; then
        printf 'capture-attest: REFUSED — this scenario'"'"'s capture ledger is bound to VM %s, but the capture was of %s. Capture the VM this scenario was assigned, from VMNAME.\n' \
            "$bound" "${vm:-<none>}" >&2
        return 1
    fi
    sum=$(sha256sum "$out" 2>/dev/null | awk '{print $1}') || return 1
    [ -n "$sum" ] || return 1
    bytes=$(stat -c %s "$out" 2>/dev/null || echo 0)
    abs=$(readlink -f "$out" 2>/dev/null || printf '%s' "$out")
    # `scope` decides whether a later absence is OMISSION or merely an
    # un-harvested scratch capture: in-tree means the frame was written where
    # the gate collects evidence, so removing it is the omission attack.
    scope='out-of-tree'
    if [ -n "$root" ]; then
        rroot=$(readlink -f "$root" 2>/dev/null || printf '%s' "$root")
        case "$abs" in "$rroot"/*) scope='in-tree' ;; esac
    fi
    [ -z "$forced" ] || scope=$forced
    # Tabs and newlines would corrupt the TSV; strip them from the only
    # caller-influenced field.
    abs=${abs//$'\t'/ }; abs=${abs//$'\n'/ }; abs=${abs//$'\r'/ }
    # `grep -c` exits 1 on a zero count while still printing "0"; `|| echo 0`
    # would append a SECOND line and break the arithmetic below.
    seq=$(tail -n +3 -- "$log" 2>/dev/null | grep -c . || true)
    [ -n "$seq" ] || seq=0
    seq=$((seq + 1))
    if [ "$seq" -eq 1 ]; then
        prev=$(head -1 -- "$log" | awk -F'\t' '{print $3}')
    else
        prev=$(tail -1 -- "$log" | awk -F'\t' '{print $8}')
    fi
    [ -n "$prev" ] || return 1
    ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    payload=$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s' "$seq" "$ts" "$vm" "$scope" "$bytes" "$sum" "$abs")
    chain=$(printf '%s\t%s' "$prev" "$payload" | sha256sum | awk '{print $1}')
    printf '%s\t%s\n' "$payload" "$chain" >> "$log" || return 1
    return 0
}
export -f _qci_capture_bound_vm 2>/dev/null || true
export -f _qci_capture_attest_row 2>/dev/null || true

# Attest a frame the CALLER just captured from the ledger's bound VM.
#
# This is the in-guest qdwin lane's entry point: that lane's capture is a
# multi-step guest protocol (qdshell request, compositor-identity check, fetch,
# publish) that cannot be performed from here, so the capture tool must hand
# the finished file over. It is GATED on the bound VM and it is NOT a security
# boundary — see the header. Prefer capture_virsh_screenshot wherever the
# capture is a plain `virsh screenshot`.
#
# Args: captured_file vm_name (vm_name defaults to $VM / $VMNAME).
# Returns 0 when there is no ledger (ordinary by-hand use) or the row was
# written; non-zero (loudly) when the ledger refused the row.
capture_attest_frame() {
    _qci_capture_write_row "${1:-}" "${2:-${VM:-${VMNAME:-}}}" ""
}

# INTERNAL. The locked row write shared by every public entry point.
# Args: captured_file vm_name [scope_override]
_qci_capture_write_row() {
    local out=${1:-} vm=${2:-} forced=${3:-} log=${QCI_GUI_CAPTURE_LOG:-} rc=0
    [ -n "$log" ] || return 0
    [ -f "$log" ] || return 0
    [ -f "$out" ] || return 0
    if [ -z "$vm" ]; then
        printf 'capture-attest: REFUSED — no VM name for %s; pass the scenario VM from VMNAME\n' \
            "$out" >&2
        return 1
    fi
    if command -v flock >/dev/null 2>&1; then
        flock "$log" bash -c '_qci_capture_attest_row "$@"' _ \
            "$log" "$out" "$vm" "${QCI_GUI_ARTIFACT_DIR:-}" "$forced" || rc=$?
    else
        _qci_capture_attest_row "$log" "$out" "$vm" "${QCI_GUI_ARTIFACT_DIR:-}" "$forced" || rc=$?
    fi
    return "$rc"
}

# ONE ROW PER DELIVERED FRAME. Take a screenshot WITHOUT attesting it.
#
# A retry loop captures candidates it may throw away, and attesting each one
# made every delivered frame carry TWO rows (the scratch capture and the
# in-tree publication). Three rounds of review went into trying to pair those
# two rows back up from their bytes, and bytes cannot carry capture identity:
# same bytes are not the same capture, and a different destination is not a
# different capture. The inference is removed rather than improved -- a
# candidate gets no row until it is published or explicitly recorded as
# rejected. Args: vm out [libvirt_uri].
capture_virsh_shot() {
    local vm=${1:?capture_virsh_shot: vm} out=${2:?capture_virsh_shot: out}
    local uri=${3:-${LIBVIRT_DEFAULT_URI:-qemu:///session}}
    virsh -c "$uri" screenshot "$vm" "$out" >/dev/null || return $?
    return 0
}

# Publish a candidate this process captured to its final path and write THE row
# for it. `-T` matters: plain `cp SRC DEST` puts SRC's basename INSIDE DEST when
# DEST already exists as a directory, which silently produced a graded frame at
# a path nobody intended (sol, B round 3). Args: src dst [vm].
capture_publish_frame() {
    local src=${1:?capture_publish_frame: src} dst=${2:?capture_publish_frame: dst}
    local vm=${3:-${VM:-${VMNAME:-}}} rc=0
    cp -T -- "$src" "$dst" || return 1
    _qci_capture_write_row "$dst" "$vm" "" || rc=$?
    return "$rc"
}

# Record a candidate the capture tool JUDGED UNUSABLE (blank, stale, wrong
# window). It keeps the ledger a complete record of every frame the harness
# took, which is its forensic purpose, while the `rejected` scope tells the gate
# to ignore the row entirely: a frame the harness already refused is not
# evidence, and must never satisfy an evidence floor. Args: file [vm].
capture_attest_rejected() {
    _qci_capture_write_row "${1:-}" "${2:-${VM:-${VMNAME:-}}}" rejected
}

# THE PREFERRED PRODUCER for a lane that captures STRAIGHT to its final path
# (the qdwin apps lane): take the screenshot HERE and attest the file this
# function just wrote, one row, no publication step. There is no way to use it
# to bless bytes it did not produce, and it cannot be pointed at another
# worker's VM (the row is refused). A lane with a retry loop wants
# capture_virsh_shot + capture_publish_frame instead. Args: vm out [uri].
capture_virsh_screenshot() {
    local vm=${1:?capture_virsh_screenshot: vm} out=${2:?capture_virsh_screenshot: out}
    local uri=${3:-${LIBVIRT_DEFAULT_URI:-qemu:///session}} rc=0
    virsh -c "$uri" screenshot "$vm" "$out" >/dev/null || return $?
    capture_attest_frame "$out" "$vm" || rc=$?
    return "$rc"
}
