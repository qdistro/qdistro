# tier3s/spike/phase0-fix-lib.sh — GUEST side, sourced (as root, in the dev
# test VM) by every step of run-phase0-fix.sh. Asserting helpers: each check
# prints `CHECK <name>: OK|BAD …`; `finish` prints the BAD count and exits
# with it, so a step's exit status is its verdict (not a trailing echo).
cd /root/qdistro-src || exit 99
BAD=0
R=/usr/libexec/qdistro/runsc/runsc
SIDE=/usr/libexec/qdistro/runsc/gvisor-bin
CACHE=/var/cache/qdistro/runsc
PROVISION=(tier3s/provision-runsc.sh --offline --cache-dir "$CACHE")
ok()  { echo "CHECK $1: OK${2:+ ($2)}"; }
bad() { echo "CHECK $1: BAD${2:+ ($2)}"; BAD=$((BAD + 1)); }
expect_rc() {   # expect_rc <name> <want> <got>
    if [ "$3" = "$2" ]; then ok "$1" "rc=$3"; else bad "$1" "rc=$3, want $2"; fi
}
has() {         # has <name> <file> <fixed string>
    if grep -qF -- "$3" "$2"; then ok "$1"; else bad "$1" "missing: $3"; fi
}
hasnt() {
    if grep -qF -- "$3" "$2"; then bad "$1" "present: $3"; else ok "$1"; fi
}
is() {          # is <name> <got> <want>
    if [ "$2" = "$3" ]; then ok "$1" "$2"; else bad "$1" "got '$2', want '$3'"; fi
}
# Successful execve()s of a runsc-bundle file or of a /proc/self/fd path, from
# an strace -f -e trace=execve transcript (the probe's only exec route).
runsc_execs() {
    grep -E 'execve\("(/proc/self/fd/[0-9]+|/usr/libexec/qdistro/runsc/[^"]*)"' "$1" | grep -v ' = -1 '
}
# probe_strace: run the real probe as root under strace; output in $PO,
# status in $PRC, the runsc executions it made in $NEXEC.
PO=/var/tmp/t3s-probe.out
probe_strace() {
    rm -f /var/tmp/t3s-tr "$PO"
    strace -f -qq -e trace=execve -o /var/tmp/t3s-tr tier3s/probe.sh --user admin > "$PO" 2>&1
    PRC=$?
    cat "$PO"
    echo "--- runsc-bundle / fd executions seen by strace -f -e trace=execve:"
    runsc_execs /var/tmp/t3s-tr || echo "(none)"
    NEXEC=$(runsc_execs /var/tmp/t3s-tr | wc -l)
}
never_executed() {   # after probe_strace: the negative oracle
    has "$1-reported-not-executed" "$PO" "FAIL runsc_version: not executed:"
    is "$1-strace-runsc-execs" "$NEXEC" 0
}
provision() {        # provision <name> <want rc>  (output in $VO)
    VO=/var/tmp/t3s-prov.out
    "${PROVISION[@]}" > "$VO" 2>&1
    local rc=$?
    cat "$VO"
    expect_rc "$1" "$2" "$rc"
}
probe_tail_pass() {  # quick restored-state check (no strace)
    tier3s/probe.sh --user admin > "$PO" 2>&1
    local rc=$?
    tail -1 "$PO"
    expect_rc "$1-probe-exit" 0 "$rc"
    has "$1-probe-pass" "$PO" "RESULT PASS: tier 3s prerequisites present"
}
leftovers() { find /usr/libexec/qdistro /etc/qdistro -maxdepth 1 \( -name '*.new.*' -o -name '*.old.*' \) | wc -l; }
finish() { echo "### checks failed: $BAD"; exit "$BAD"; }
