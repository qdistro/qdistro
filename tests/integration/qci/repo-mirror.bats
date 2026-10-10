#!/usr/bin/env bats
# qci:host-only
#
# Host-only tests for scripts/vm/qd-repo-mirror.py + the QDISTRO_REPO_BASE
# consumer override — the verified mirror of the pinned Tumbleweed history
# repos. A fixture repo (repomd.xml chaining sha256s to metadata, and a
# primary.xml chaining sha256s to RPMs) is served over localhost http; the
# mirror must land verbatim, verify every checksum, resume, and refuse
# corrupt content. No real network is touched.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    MIRROR="$REPO_ROOT/scripts/vm/qd-repo-mirror.py"
    SUBSTRATE="$REPO_ROOT/scripts/vm/lib/test-substrate.sh"
    DEPS="$REPO_ROOT/scripts/vm/lib/deps-cache.sh"
    [ -f "$MIRROR" ] && [ -f "$SUBSTRATE" ] && [ -f "$DEPS" ]
    WORK="$BATS_TEST_TMPDIR/work"
    SNAP=20261003
    SNAP_DIR="$WORK/srv/history/$SNAP/tumbleweed/repo"
    mkdir -p "$WORK/srv" "$WORK/cache"

    # Fixture: two RPMs + repodata with real sha256 chains.
    local oss="$SNAP_DIR/oss" arch=noarch
    mkdir -p "$oss/repodata" "$oss/$arch"
    printf 'rpm-A-bytes' > "$oss/$arch/pkg-a-1.0-1.noarch.rpm"
    printf 'rpm-B-bytes' > "$oss/$arch/pkg-b-1.0-1.noarch.rpm"
    local sha_a sha_b
    sha_a="$(sha256sum "$oss/$arch/pkg-a-1.0-1.noarch.rpm" | awk '{print $1}')"
    sha_b="$(sha256sum "$oss/$arch/pkg-b-1.0-1.noarch.rpm" | awk '{print $1}')"
    cat > "$WORK/primary.xml" <<EOF
<metadata xmlns="http://linux.duke.edu/metadata/common" packages="2">
 <package type="rpm"><name>pkg-a</name><arch>noarch</arch>
  <checksum type="sha256" pkgid="YES">$sha_a</checksum>
  <location href="$arch/pkg-a-1.0-1.noarch.rpm"/></package>
 <package type="rpm"><name>pkg-b</name><arch>noarch</arch>
  <checksum type="sha256" pkgid="YES">$sha_b</checksum>
  <location href="$arch/pkg-b-1.0-1.noarch.rpm"/></package>
</metadata>
EOF
    gzip -c "$WORK/primary.xml" > "$oss/repodata/fixture-primary.xml.gz"
    printf 'other-metadata' > "$oss/repodata/other.xml"
    printf 'dummy-asc' > "$oss/repodata/repomd.xml.asc"
    printf 'dummy-key' > "$oss/repodata/repomd.xml.key"
    local sha_p sha_o
    sha_p="$(sha256sum "$oss/repodata/fixture-primary.xml.gz" | awk '{print $1}')"
    sha_o="$(sha256sum "$oss/repodata/other.xml" | awk '{print $1}')"
    cat > "$oss/repodata/repomd.xml" <<EOF
<repomd xmlns="http://linux.duke.edu/metadata/repo">
 <data type="primary">
  <location href="repodata/fixture-primary.xml.gz"/>
  <checksum type="sha256">$sha_p</checksum>
 </data>
 <data type="other">
  <location href="repodata/other.xml"/>
  <checksum type="sha256">$sha_o</checksum>
 </data>
</repomd>
EOF

    PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    # exec so $! is python's own pid — without it the subshell may fork a
    # grandchild that survives `kill`, keeps bats' command pipe open and
    # hangs the whole run.
    ( cd "$WORK/srv" && exec python3 -m http.server "$PORT" >/dev/null 2>&1 </dev/null ) &
    SERVER_PID=$!
    for _ in $(seq 1 50); do
        wget -q -O /dev/null "http://127.0.0.1:$PORT/" && break
        sleep 0.1
    done
    BASE="http://127.0.0.1:$PORT/history"
    export QDISTRO_DEPS_CACHE_DIR="$WORK/cache"
    unset QDISTRO_DEPS_OFFLINE QCI_OFFLINE QDISTRO_REPO_BASE
}

teardown() {
    bash -c ". '$DEPS'; qd_serve_stop" 2>/dev/null || true
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null || true
}

run_mirror() {  # extra args pass through to the worker
    run python3 "$MIRROR" --root "$WORK/cache" --snapshot "$SNAP" \
        --repo oss --base "$BASE" "$@"
}

@test "mirror lands verbatim layout, verifies checksums, writes index+manifest" {
    run_mirror
    [ "$status" -eq 0 ]
    local d="$WORK/cache/repos/$SNAP/tumbleweed/repo/oss"
    [ -s "$d/repodata/repomd.xml" ]
    [ -s "$d/repodata/repomd.xml.asc" ]
    [ -s "$d/repodata/fixture-primary.xml.gz" ]
    [ -s "$d/noarch/pkg-a-1.0-1.noarch.rpm" ]
    [ -s "$d/noarch/pkg-b-1.0-1.noarch.rpm" ]
    [ "$(cat "$d/noarch/pkg-a-1.0-1.noarch.rpm")" = "rpm-A-bytes" ]
    grep -q '"repo": "oss"' "$d/mirror.json"
    grep -q 'pkg-a' "$d/mirror-index.json"
}

@test "a complete mirror needs zero upstream contact" {
    run_mirror; [ "$status" -eq 0 ]
    rm -rf "$SNAP_DIR"   # upstream gone entirely — the disappearing-repo case
    run_mirror
    [ "$status" -eq 0 ]
    [[ "$output" == *"complete mirror present"* ]]
}

@test "--verify audits locally, repairs online, and fails closed offline" {
    run_mirror; [ "$status" -eq 0 ]
    local d="$WORK/cache/repos/$SNAP/tumbleweed/repo/oss"
    printf 'tampered' > "$d/noarch/pkg-a-1.0-1.noarch.rpm"
    # Online verify self-heals from the fixture server.
    run_mirror --verify
    [ "$status" -eq 0 ]
    [ "$(cat "$d/noarch/pkg-a-1.0-1.noarch.rpm")" = "rpm-A-bytes" ]
    # Corrupt again; upstream + offline => verify names the bad file and dies.
    printf 'tampered' > "$d/noarch/pkg-a-1.0-1.noarch.rpm"
    run env QDISTRO_DEPS_OFFLINE=1 python3 "$MIRROR" --root "$WORK/cache" \
        --snapshot "$SNAP" --repo oss --base "$BASE" --verify
    [ "$status" -ne 0 ]
    [[ "$output" == *"pkg-a-1.0-1.noarch.rpm"* ]]
    # Warm verify with a clean mirror works fully offline.
    printf 'rpm-A-bytes' > "$d/noarch/pkg-a-1.0-1.noarch.rpm"
    run env QDISTRO_DEPS_OFFLINE=1 python3 "$MIRROR" --root "$WORK/cache" \
        --snapshot "$SNAP" --repo oss --base "$BASE" --verify
    [ "$status" -eq 0 ]
}

@test "corrupt upstream RPM never reaches the mirror" {
    printf 'tampered' > "$SNAP_DIR/oss/noarch/pkg-b-1.0-1.noarch.rpm"
    run_mirror
    [ "$status" -ne 0 ]
    [[ "$output" == *"digest mismatch"* ]]
    local d="$WORK/cache/repos/$SNAP/tumbleweed/repo/oss"
    [ ! -f "$d/noarch/pkg-b-1.0-1.noarch.rpm" ]
    [ -f "$d/noarch/pkg-a-1.0-1.noarch.rpm" ]  # the good file still landed
}

@test "a stale local file not named by repodata is cleaned up" {
    run_mirror; [ "$status" -eq 0 ]
    local d="$WORK/cache/repos/$SNAP/tumbleweed/repo/oss"
    printf 'stale' > "$d/noarch/stray-9.9-1.noarch.rpm"
    run_mirror --force   # complete mirrors fast-path; --force runs cleanup
    [ "$status" -eq 0 ]
    [ ! -e "$d/noarch/stray-9.9-1.noarch.rpm" ]
}

@test "metadata-only mode mirrors repodata without RPMs" {
    run_mirror --metadata-only
    [ "$status" -eq 0 ]
    local d="$WORK/cache/repos/$SNAP/tumbleweed/repo/oss"
    [ -s "$d/repodata/repomd.xml" ]
    [ ! -e "$d/noarch/pkg-a-1.0-1.noarch.rpm" ]
}

@test "offline mode refuses to run the warmer" {
    run env QDISTRO_DEPS_OFFLINE=1 python3 "$MIRROR" \
        --root "$WORK/cache" --snapshot "$SNAP" --repo oss --base "$BASE"
    [ "$status" -ne 0 ]
    [[ "$output" == *"offline"* ]]
}

@test "qd_repo_base picks file:// for a complete mirror, upstream else, fails closed offline" {
    # Both repos' completion markers must exist for the file:// pick
    # (a repodata-only tree is NOT a usable mirror).
    for r in oss non-oss; do
        mkdir -p "$WORK/cache/repos/$SNAP/tumbleweed/repo/$r/repodata"
        : > "$WORK/cache/repos/$SNAP/tumbleweed/repo/$r/repodata/repomd.xml"
        printf '{}' > "$WORK/cache/repos/$SNAP/tumbleweed/repo/$r/mirror.json"
    done
    run bash -c ". '$DEPS'; qd_repo_base $SNAP"
    [ "$status" -eq 0 ]
    [ "$output" = "file://$WORK/cache/repos" ]
    # The returned base + the standard consumer suffix must resolve to a
    # real mirrored file — guards the snapshot-segment-doubling bug.
    [ -f "${output#file://}/$SNAP/tumbleweed/repo/oss/mirror.json" ]
    # Explicit override wins.
    run env QDISTRO_REPO_BASE=http://10.0.2.2:9 bash -c ". '$DEPS'; qd_repo_base $SNAP"
    [ "$output" = "http://10.0.2.2:9" ]
    # Incomplete mirror (non-oss missing) + no override -> upstream.
    rm -rf "$WORK/cache/repos/$SNAP/tumbleweed/repo/non-oss"
    run bash -c ". '$DEPS'; qd_repo_base $SNAP"
    [ "$status" -eq 0 ]
    [[ "$output" == https://download.opensuse.org/history ]]
    # ...but offline the incomplete mirror is a named hard miss.
    run env QDISTRO_DEPS_OFFLINE=1 bash -c ". '$DEPS'; qd_repo_base $SNAP"
    [ "$status" -ne 0 ]
    [[ "$output" == *"no complete repo mirror"* ]]
}

@test "qd_serve exports the root; guest base URL serves real mirror files" {
    # Complete mirror fixture.
    for r in oss non-oss; do
        mkdir -p "$WORK/cache/repos/$SNAP/tumbleweed/repo/$r/repodata"
        printf 'sentinel-%s' "$r" > "$WORK/cache/repos/$SNAP/tumbleweed/repo/$r/mirror.json"
    done
    run bash -c ". '$DEPS'; qd_serve_start"
    [ "$status" -eq 0 ]
    port="$output"
    # A guest-visible URL actually serves the mirrored file.
    run python3 -c "
import urllib.request
print(urllib.request.urlopen('http://127.0.0.1:$port/repos/$SNAP/tumbleweed/repo/oss/mirror.json', timeout=5).read().decode())"
    [ "$status" -eq 0 ]
    [ "$output" = "sentinel-oss" ]
    # guest_base composes the QDISTRO_REPO_BASE value for a slirp guest...
    run bash -c ". '$DEPS'; qd_serve_guest_base $SNAP"
    [ "$output" = "http://10.0.2.2:$port/repos" ]
    # ...and a passt one.
    run bash -c ". '$DEPS'; qd_serve_guest_base $SNAP 169.254.2.2"
    [ "$output" = "http://169.254.2.2:$port/repos" ]
    # A second start is idempotent — same port, same server.
    run bash -c ". '$DEPS'; qd_serve_start"
    [ "$output" = "$port" ]
    # Stop kills it; guest_base then fails closed.
    run bash -c ". '$DEPS'; qd_serve_stop; qd_serve_port"
    [ "$status" -ne 0 ]
    run bash -c ". '$DEPS'; qd_serve_guest_base $SNAP"
    [ "$status" -ne 0 ]
    [[ "$output" == *"not running"* ]]
}

@test "qd_serve_guest_base fails closed with an incomplete mirror" {
    run bash -c ". '$DEPS'; qd_serve_start" # server up, mirror absent
    [ "$status" -eq 0 ]
    run bash -c ". '$DEPS'; qd_serve_guest_base $SNAP"
    [ "$status" -ne 0 ]
    [[ "$output" == *"no complete mirror"* ]]
    run bash -c ". '$DEPS'; qd_serve_stop"
    [ "$status" -eq 0 ]
}

@test "qdistro_substrate_repo_command honours QDISTRO_REPO_BASE" {
    run bash -c "
        . '$SUBSTRATE'
        export QDISTRO_SUBSTRATE_SNAPSHOT=$SNAP
        export QDISTRO_SUBSTRATE_CLOUD_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        export QDISTRO_REPO_BASE='file:///mirror'
        qdistro_substrate_repo_command"
    [ "$status" -eq 0 ]
    [[ "$output" == *"baseurl=file:///mirror/$SNAP/tumbleweed/repo/oss/"* ]]
    [[ "$output" == *"baseurl=file:///mirror/$SNAP/tumbleweed/repo/non-oss/"* ]]
    # ...and defaults to upstream when unset.
    run bash -c "
        . '$SUBSTRATE'
        export QDISTRO_SUBSTRATE_SNAPSHOT=$SNAP
        export QDISTRO_SUBSTRATE_CLOUD_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        qdistro_substrate_repo_command"
    [ "$status" -eq 0 ]
    [[ "$output" == *"baseurl=https://download.opensuse.org/history/$SNAP/tumbleweed/repo/oss/"* ]]
}

@test "--verify audits every primary package, not just the index" {
    # A --limit-truncated mirror has a happy index but is INCOMPLETE.
    run_mirror --limit 1
    [ "$status" -eq 0 ]
    local d="$WORK/cache/repos/$SNAP/tumbleweed/repo/oss"
    [ ! -f "$d/noarch/pkg-b-1.0-1.noarch.rpm" ]
    [ ! -f "$d/mirror.json" ]
    # Offline verify must name the package primary demands, not pass the index.
    run env QDISTRO_DEPS_OFFLINE=1 python3 "$MIRROR" --root "$WORK/cache" \
        --snapshot "$SNAP" --repo oss --base "$BASE" --verify
    [ "$status" -ne 0 ]
    [[ "$output" == *"pkg-b-1.0-1.noarch.rpm"* ]]
    # Online verify self-heals the gap and a clean audit publishes the marker.
    run_mirror --verify
    [ "$status" -eq 0 ]
    [ -f "$d/noarch/pkg-b-1.0-1.noarch.rpm" ]
    run_mirror --verify
    [ "$status" -eq 0 ]
    [ -f "$d/mirror.json" ]
}

@test "a killed re-mirror drops the stale completion marker" {
    run_mirror; [ "$status" -eq 0 ]
    local d="$WORK/cache/repos/$SNAP/tumbleweed/repo/oss"
    [ -f "$d/mirror.json" ]
    # A force re-run that dies mid-flight must not leave "complete" behind:
    # corrupt the local copy (so it is not reused) AND upstream (so the
    # refetch fails the repomd->primary chain verification).
    printf 'tampered' > "$d/noarch/pkg-a-1.0-1.noarch.rpm"
    printf 'tampered' > "$SNAP_DIR/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    run_mirror --force
    [ "$status" -ne 0 ]
    [ ! -f "$d/mirror.json" ]
}

@test "qdistro_substrate_repo_command fails closed offline without a base" {
    run env QCI_OFFLINE=1 bash -c "
        . '$SUBSTRATE'
        export QDISTRO_SUBSTRATE_SNAPSHOT=$SNAP
        export QDISTRO_SUBSTRATE_CLOUD_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        unset QDISTRO_REPO_BASE
        qdistro_substrate_repo_command"
    [ "$status" -ne 0 ]
    [[ "$output" == *"QDISTRO_REPO_BASE"* ]]
    [[ "$output" == *"offline"* ]]
}
