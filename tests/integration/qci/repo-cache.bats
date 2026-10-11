#!/usr/bin/env bats
# qci:host-only
#
# Host-only tests for scripts/vm/qd-repo-cache.py — the verifying
# pull-through cache (qd_serve proxy) for the pinned Tumbleweed history
# repos. A fixture "upstream" (repomd.xml chaining sha256s to metadata,
# primary.xml chaining sha256s to RPMs) is served by a localhost server
# that logs every request, so tests can assert exactly what reached
# upstream. No real network is touched.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    CACHE_PY="$REPO_ROOT/scripts/vm/qd-repo-cache.py"
    DEPS="$REPO_ROOT/scripts/vm/lib/deps-cache.sh"
    [ -f "$CACHE_PY" ] && [ -f "$DEPS" ]
    WORK="$BATS_TEST_TMPDIR/work"
    SNAP=20261003
    mkdir -p "$WORK/cache"
    unset QDISTRO_DEPS_OFFLINE QCI_OFFLINE QDISTRO_REPO_BASE QDISTRO_REPO_PROXY _QD_REPO_BASE_WIRED
    export QDISTRO_SERVE_PORT=0   # never collide with a live proxy on the stable port
    export QDISTRO_DEPS_CACHE_DIR="$WORK/cache"

    local r
    for r in oss non-oss; do make_repo "$r"; done

    HITS="$WORK/hits.log"; : > "$HITS"
    UP_PORT="$(free_port)"
    ( exec python3 - "$WORK/srv" "$HITS" "$UP_PORT" >/dev/null 2>&1 </dev/null <<'PY'
import functools, http.server, sys
root, hits, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
class H(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        with open(hits, "a") as f:
            f.write(self.path + "\n")
        super().do_GET()
    def log_message(self, *a):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", port),
    functools.partial(H, directory=root)).serve_forever()
PY
    ) &
    UP_PID=$!
    wait_http "http://127.0.0.1:$UP_PORT/"
    : > "$HITS"
    BASE="http://127.0.0.1:$UP_PORT/history"
}

teardown() {
    stop_proxy
    [ -n "${COUNT_PID:-}" ] && { kill "$COUNT_PID" 2>/dev/null; wait "$COUNT_PID" 2>/dev/null || true; }
    kill "$UP_PID" 2>/dev/null; wait "$UP_PID" 2>/dev/null || true
    bash -c ". '$DEPS'; qd_serve_stop" 2>/dev/null || true
}

free_port() {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

wait_http() {
    for _ in $(seq 1 50); do
        wget -q -O /dev/null "$1" && return 0
        sleep 0.1
    done
}

# make_repo <repo> — two RPMs (pkg-a, pkg-b) + repomd/primary with real
# sha256 chains under $WORK/srv/history/$SNAP/tumbleweed/repo/<repo>.
make_repo() {
    local d="$WORK/srv/history/$SNAP/tumbleweed/repo/$1"
    mkdir -p "$d/repodata" "$d/noarch"
    printf "$1-A-bytes" > "$d/noarch/pkg-a-1.0-1.noarch.rpm"
    printf "$1-B-bytes" > "$d/noarch/pkg-b-1.0-1.noarch.rpm"
    write_metadata "$1"
}

write_metadata() {
    local d="$WORK/srv/history/$SNAP/tumbleweed/repo/$1" p sa sb sp so
    sa="$(sha256sum "$d/noarch/pkg-a-1.0-1.noarch.rpm" | awk '{print $1}')"
    sb="$(sha256sum "$d/noarch/pkg-b-1.0-1.noarch.rpm" | awk '{print $1}')"
    [ -n "${PKG_A_SHA:-}" ] && sa="$PKG_A_SHA"
    cat > "$WORK/primary-$1.xml" <<EOF
<metadata xmlns="http://linux.duke.edu/metadata/common" packages="2">
 <package type="rpm"><name>pkg-a</name><arch>noarch</arch>
  <checksum type="sha256" pkgid="YES">$sa</checksum>
  <location href="noarch/pkg-a-1.0-1.noarch.rpm"/></package>
 <package type="rpm"><name>pkg-b</name><arch>noarch</arch>
  <checksum type="sha256" pkgid="YES">$sb</checksum>
  <location href="noarch/pkg-b-1.0-1.noarch.rpm"/></package>
</metadata>
EOF
    gzip -c "$WORK/primary-$1.xml" > "$d/repodata/fixture-primary.xml.gz"
    printf 'other-metadata' > "$d/repodata/other.xml"
    printf 'dummy-asc' > "$d/repodata/repomd.xml.asc"
    printf 'dummy-key' > "$d/repodata/repomd.xml.key"
    sp="$(sha256sum "$d/repodata/fixture-primary.xml.gz" | awk '{print $1}')"
    so="$(sha256sum "$d/repodata/other.xml" | awk '{print $1}')"
    cat > "$d/repodata/repomd.xml" <<EOF
<repomd xmlns="http://linux.duke.edu/metadata/repo">
 <data type="primary"><location href="repodata/fixture-primary.xml.gz"/>
  <checksum type="sha256">$sp</checksum></data>
 <data type="other"><location href="repodata/other.xml"/>
  <checksum type="sha256">$so</checksum></data>
</repomd>
EOF
}

start_proxy() {  # env passes through (QCI_OFFLINE etc.)
    PROXY_PORT_FILE="$WORK/proxy.port"; rm -f "$PROXY_PORT_FILE"
    ( exec python3 "$CACHE_PY" --root "$WORK/cache" --base "$BASE" serve \
        --port-file "$PROXY_PORT_FILE" >"$WORK/proxy.log" 2>&1 </dev/null ) &
    PROXY_PID=$!
    for _ in $(seq 1 50); do [ -s "$PROXY_PORT_FILE" ] && break; sleep 0.1; done
    PROXY="http://127.0.0.1:$(cat "$PROXY_PORT_FILE")/repos/$SNAP/tumbleweed/repo"
}

stop_proxy() {
    [ -n "${PROXY_PID:-}" ] || return 0
    kill "$PROXY_PID" 2>/dev/null; wait "$PROXY_PID" 2>/dev/null || true
    PROXY_PID=
}

cache_cmd() {
    run python3 "$CACHE_PY" --root "$WORK/cache" --base "$BASE" "$@"
}

C() { echo "$WORK/cache/repos/$SNAP/tumbleweed/repo/$1"; }

@test "proxy: a requested package is fetched once, verified, cached; nothing else is" {
    start_proxy
    run wget -q -O "$WORK/got" "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$status" -eq 0 ]
    [ "$(cat "$WORK/got")" = "oss-A-bytes" ]
    [ -f "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm" ]
    [ -f "$(C oss)/metadata.json" ]
    [ ! -e "$(C oss)/noarch/pkg-b-1.0-1.noarch.rpm" ]   # not a mirror
    # second request: served from cache, upstream not asked again
    run wget -q -O "$WORK/got2" "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$status" -eq 0 ]
    [ "$(grep -c 'pkg-a' "$HITS")" -eq 1 ]
    ! grep -q 'pkg-b' "$HITS"
    grep -qx 'oss pkg-a.noarch' "$(C oss)/used.txt"
}

@test "proxy: metadata (repomd, sidecars, primary — the package list) is cached and served" {
    start_proxy
    for f in repomd.xml repomd.xml.asc repomd.xml.key fixture-primary.xml.gz other.xml; do
        run wget -q -O /dev/null "$PROXY/oss/repodata/$f"
        [ "$status" -eq 0 ]
    done
    stop_proxy
    # offline, metadata is answered from disk with zero upstream contact
    : > "$HITS"
    QCI_OFFLINE=1 start_proxy
    run wget -q -O "$WORK/primary" "$PROXY/oss/repodata/fixture-primary.xml.gz"
    [ "$status" -eq 0 ]
    [ ! -s "$HITS" ]
}

@test "proxy offline: cache hits serve, a miss is 404 with zero upstream contact" {
    start_proxy
    wget -q -O /dev/null "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    stop_proxy
    : > "$HITS"
    QCI_OFFLINE=1 start_proxy
    run wget -q -O /dev/null "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$status" -eq 0 ]
    run curl -s -o /dev/null -w '%{http_code}' "$PROXY/oss/noarch/pkg-b-1.0-1.noarch.rpm"
    [ "$output" = 404 ]
    [ ! -s "$HITS" ]
    grep -q 'MISS (offline)' "$WORK/proxy.log"
    ! grep -q Traceback "$WORK/proxy.log"
}

@test "proxy offline with no cached metadata: 404, never upstream" {
    QCI_OFFLINE=1 start_proxy
    run curl -s -o /dev/null -w '%{http_code}' "$PROXY/oss/repodata/repomd.xml"
    [ "$output" = 404 ]
    run curl -s -o /dev/null -w '%{http_code}' "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$output" = 404 ]                            # cold offline miss: 404, no crash
    [ ! -s "$HITS" ]
    ! grep -q Traceback "$WORK/proxy.log"
}

@test "proxy: an upstream package that fails its digest is never cached or served" {
    PKG_A_SHA=0000000000000000000000000000000000000000000000000000000000000000 write_metadata oss
    start_proxy
    run wget -q -O /dev/null "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$status" -ne 0 ]
    [ ! -e "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm" ]
    ! ls "$(C oss)/noarch/" 2>/dev/null | grep -q '^\.dl\.'
}

@test "proxy: paths primary does not name, and traversal, are 404 without upstream fetch" {
    start_proxy
    wget -q -O /dev/null "$PROXY/oss/repodata/repomd.xml"
    : > "$HITS"
    run wget -q -O /dev/null "$PROXY/oss/noarch/evil-1.0-1.noarch.rpm"
    [ "$status" -ne 0 ]
    run curl -fsS --path-as-is -o /dev/null "$PROXY/oss/../../../../../etc/passwd"
    [ "$status" -ne 0 ]
    ! grep -q 'evil\|passwd' "$HITS"
}

@test "proxy: a corrupt cached file is re-verified and refetched, not served" {
    start_proxy
    wget -q -O /dev/null "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    printf 'tampered!!!' > "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm"   # same size
    run wget -q -O "$WORK/got" "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$status" -eq 0 ]
    [ "$(cat "$WORK/got")" = "oss-A-bytes" ]
}

@test "prefetch fetches exactly the list; audit passes, and fails on a gap" {
    printf '# c\noss pkg-a.noarch\nnon-oss pkg-b.noarch\n' > "$WORK/list"
    cache_cmd prefetch --snapshot "$SNAP" --list "$WORK/list"
    [ "$status" -eq 0 ]
    [ -f "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm" ]
    [ ! -e "$(C oss)/noarch/pkg-b-1.0-1.noarch.rpm" ]
    [ -f "$(C non-oss)/noarch/pkg-b-1.0-1.noarch.rpm" ]

    QCI_OFFLINE=1 cache_cmd audit --snapshot "$SNAP" --list "$WORK/list" --deep
    [ "$status" -eq 0 ]
    # audit is read-only
    local before after
    before="$(find "$WORK/cache" -printf '%p %s %T@\n' | sort)"
    QCI_OFFLINE=1 cache_cmd audit --snapshot "$SNAP" --list "$WORK/list"
    after="$(find "$WORK/cache" -printf '%p %s %T@\n' | sort)"
    [ "$before" = "$after" ]

    printf 'oss pkg-b.noarch\n' >> "$WORK/list"
    QCI_OFFLINE=1 cache_cmd audit --snapshot "$SNAP" --list "$WORK/list"
    [ "$status" -ne 0 ]
    [[ "$output" == *"not cached"* ]]
}

@test "audit --deep catches a corrupt package the index still vouches for" {
    printf 'oss pkg-a.noarch\n' > "$WORK/list"
    cache_cmd prefetch --snapshot "$SNAP" --repo oss --list "$WORK/list"
    [ "$status" -eq 0 ]
    printf 'tampered!!!' > "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm"
    cache_cmd audit --snapshot "$SNAP" --repo oss --list "$WORK/list" --deep
    [ "$status" -ne 0 ]
}

@test "audit fails on corrupt metadata and on a missing list" {
    printf 'oss pkg-a.noarch\n' > "$WORK/list"
    cache_cmd prefetch --snapshot "$SNAP" --repo oss --list "$WORK/list"
    cache_cmd audit --snapshot "$SNAP" --repo oss --list "$WORK/nope"
    [ "$status" -ne 0 ]
    [[ "$output" == *"package list not found"* ]]
    printf 'x' >> "$(C oss)/repodata/other.xml"
    cache_cmd audit --snapshot "$SNAP" --repo oss --list "$WORK/list"
    [ "$status" -ne 0 ]
    [[ "$output" == *"metadata corrupt"* ]]
}

@test "export-list records what the proxy served; prune drops the rest" {
    start_proxy
    wget -q -O /dev/null "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    wget -q -O /dev/null "$PROXY/non-oss/noarch/pkg-b-1.0-1.noarch.rpm"
    stop_proxy
    printf 'oss zzz.noarch\n' > "$WORK/old"
    cache_cmd export-list --snapshot "$SNAP" --merge "$WORK/old"
    [ "$status" -eq 0 ]
    [ "$output" = "$(printf 'non-oss pkg-b.noarch\noss pkg-a.noarch\noss zzz.noarch')" ]

    # A leftover from a pre-proxy full mirror: pruned against the list.
    cp "$WORK/srv/history/$SNAP/tumbleweed/repo/oss/noarch/pkg-b-1.0-1.noarch.rpm" "$(C oss)/noarch/"
    printf 'oss pkg-a.noarch\n' > "$WORK/list"
    cache_cmd prune --snapshot "$SNAP" --repo oss --list "$WORK/list"
    [ "$status" -eq 0 ]
    [ -f "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm" ]
    [ ! -e "$(C oss)/noarch/pkg-b-1.0-1.noarch.rpm" ]
}

@test "offline refuses the warming commands" {
    printf 'oss pkg-a.noarch\n' > "$WORK/list"
    QCI_OFFLINE=1 cache_cmd prefetch --snapshot "$SNAP" --list "$WORK/list"
    [ "$status" -ne 0 ]
    QDISTRO_DEPS_OFFLINE=1 cache_cmd metadata --snapshot "$SNAP"
    [ "$status" -ne 0 ]
    [ ! -s "$HITS" ]
}

@test "a pre-proxy mirror-index.json is adopted: indexed files serve without refetch" {
    printf 'oss pkg-a.noarch\n' > "$WORK/list"
    cache_cmd prefetch --snapshot "$SNAP" --repo oss --list "$WORK/list"
    mv "$(C oss)/cache-index.json" "$(C oss)/mirror-index.json"
    python3 - "$(C oss)/mirror-index.json" <<'PY'
import json, sys
p = sys.argv[1]; idx = json.load(open(p))
json.dump({"files": idx, "absent": []}, open(p, "w"))
PY
    : > "$HITS"
    QCI_OFFLINE=1 start_proxy
    run wget -q -O /dev/null "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$status" -eq 0 ]
    [ ! -s "$HITS" ]
}

@test "qd_serve proxy restarts when the offline mode flips" {
    run bash -c ". '$DEPS'; qd_serve_start"
    [ "$status" -eq 0 ]
    local p1="$output"
    run bash -c ". '$DEPS'; qd_serve_start"
    [ "$output" = "$p1" ]                         # same mode → reused
    run env QCI_OFFLINE=1 bash -c ". '$DEPS'; qd_serve_start"
    [ "$status" -eq 0 ]
    [ "$(cat "$WORK/cache/.serve.mode")" = offline ]
}

@test "proxy: a multi-chunk package streams cold, byte-exact, and is cached" {
    local up="$WORK/srv/history/$SNAP/tumbleweed/repo/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    head -c 3500000 /dev/urandom > "$up"
    write_metadata oss
    start_proxy
    run wget -q -O "$WORK/got" "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$status" -eq 0 ]
    cmp "$WORK/got" "$up"
    cmp "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm" "$up"
}

@test "proxy: a corrupt multi-chunk upstream body is truncated for the client, never cached" {
    local up="$WORK/srv/history/$SNAP/tumbleweed/repo/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    head -c 3500000 /dev/urandom > "$up"
    PKG_A_SHA=0000000000000000000000000000000000000000000000000000000000000000 write_metadata oss
    start_proxy
    run curl -fsS -o "$WORK/got" "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$status" -ne 0 ]
    [ "$(stat -c %s "$WORK/got" 2>/dev/null || echo 0)" -lt 3500000 ]
    [ ! -e "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm" ]
}

@test "proxy: percent-encoded paths get the same checks; nothing outside /repos is served" {
    start_proxy
    wget -q -O /dev/null "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    printf 'tampered!!!' > "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm"
    local port; port="$(cat "$PROXY_PORT_FILE")"
    # encoded spelling of the same package: verified (refetched), not raw bytes
    run curl -fsS "http://127.0.0.1:$port/%72epos/$SNAP/tumbleweed/repo/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$output" = "oss-A-bytes" ]
    # no static fallback: cache bookkeeping and other root paths are 404
    for p in / /.serve.log "/repos/$SNAP/tumbleweed/repo/oss/metadata.json" \
             "/repos/$SNAP/tumbleweed/repo/oss/cache-index.json"; do
        run curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port$p"
        [ "$output" = 404 ]
    done
}

@test "proxy: a symlink inside the cache pointing outside it is never served" {
    start_proxy
    wget -q -O /dev/null "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    stop_proxy
    # same digest as the real package, so only containment can stop it
    cp "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm" "$WORK/outside"
    rm "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm"
    ln -s "$WORK/outside" "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm"
    QCI_OFFLINE=1 start_proxy
    run curl -s -o /dev/null -w '%{http_code}' "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$output" = 404 ]
}

@test "proxy: a lying upstream Content-Length is never streamed as a complete body" {
    for lie in 1 0; do lying_length_case "$lie"; done
}

lying_length_case() {
    # custom upstream: advertises Content-Length $1 for RPMs, sends the body
    local port2; port2="$(free_port)"
    ( exec python3 "$BATS_TEST_DIRNAME/fixtures/lying-length-server.py" \
        "$WORK/srv" "$port2" "$1" >/dev/null 2>&1 </dev/null ) &
    COUNT_PID=$!
    wait_http "http://127.0.0.1:$port2/history/$SNAP/tumbleweed/repo/oss/repodata/repomd.xml"
    BASE="http://127.0.0.1:$port2/history"
    start_proxy
    run curl -fsS -o "$WORK/got" "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$status" -ne 0 ] || [ "$(cat "$WORK/got")" = "oss-A-bytes" ]   # never a "complete" short body
    [ ! -e "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm" ] || cmp -s "$(C oss)/noarch/pkg-a-1.0-1.noarch.rpm" <(printf oss-A-bytes)
    stop_proxy
    rm -rf "$WORK/cache"; mkdir -p "$WORK/cache"
    kill "$COUNT_PID"; wait "$COUNT_PID" 2>/dev/null || true; COUNT_PID=
}

@test "proxy: cached metadata that breaks its pin is re-warmed online, 404 offline" {
    start_proxy
    wget -q -O /dev/null "$PROXY/oss/repodata/other.xml"
    printf 'x' >> "$(C oss)/repodata/other.xml"
    run curl -fsS "$PROXY/oss/repodata/other.xml"
    [ "$output" = "other-metadata" ]
    stop_proxy
    printf 'x' >> "$(C oss)/repodata/other.xml"
    QCI_OFFLINE=1 start_proxy
    run curl -s -o /dev/null -w '%{http_code}' "$PROXY/oss/repodata/other.xml"
    [ "$output" = 404 ]
    printf '<repomd/>' > "$(C oss)/repodata/repomd.xml"   # repomd off its pin
    run curl -s -o /dev/null -w '%{http_code}' "$PROXY/oss/repodata/repomd.xml"
    [ "$output" = 404 ]
}

@test "audit: repomd off its pin, or a pinned sidecar gone, fails" {
    printf 'oss pkg-a.noarch\n' > "$WORK/list"
    cache_cmd prefetch --snapshot "$SNAP" --repo oss --list "$WORK/list"
    [ "$status" -eq 0 ]
    rm "$(C oss)/repodata/repomd.xml.asc"
    cache_cmd audit --snapshot "$SNAP" --repo oss --list "$WORK/list"
    [ "$status" -ne 0 ]
    [[ "$output" == *"repomd.xml.asc"* ]]
    cache_cmd metadata --snapshot "$SNAP" --repo oss --force
    printf ' ' >> "$(C oss)/repodata/repomd.xml"
    cache_cmd audit --snapshot "$SNAP" --repo oss --list "$WORK/list"
    [ "$status" -ne 0 ]
    [[ "$output" == *"repomd.xml does not match"* ]]
}

@test "a listed name the snapshot lacks fails prefetch and audit" {
    printf 'oss pkg-a.noarch\noss nonexistent.noarch\n' > "$WORK/list"
    cache_cmd prefetch --snapshot "$SNAP" --repo oss --list "$WORK/list"
    [ "$status" -ne 0 ]
    [[ "$output" == *"nonexistent.noarch: not in snapshot"* ]]
    cache_cmd audit --snapshot "$SNAP" --repo oss --list "$WORK/list" --deep
    [ "$status" -ne 0 ]
    [[ "$output" == *"re-record the list"* ]]
}

@test "proxy: a symlinked directory inside the cache never redirects a write outside it" {
    start_proxy
    wget -q -O /dev/null "$PROXY/oss/repodata/repomd.xml"     # metadata warm
    mkdir -p "$WORK/elsewhere"
    ln -s "$WORK/elsewhere" "$(C oss)/noarch"
    run curl -s -o /dev/null -w '%{http_code}' "$PROXY/oss/noarch/pkg-a-1.0-1.noarch.rpm"
    [ "$output" != 200 ]
    [ -z "$(ls -A "$WORK/elsewhere")" ]
}

@test "audit: a package list that names nothing fails (no vacuous readiness)" {
    printf '# only comments\n' > "$WORK/list"
    cache_cmd metadata --snapshot "$SNAP"
    cache_cmd audit --snapshot "$SNAP" --list "$WORK/list"
    [ "$status" -ne 0 ]
    [[ "$output" == *"names no packages"* ]]
}
