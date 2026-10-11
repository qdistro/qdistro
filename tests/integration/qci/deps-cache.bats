#!/usr/bin/env bats
# qci:host-only
#
# Host-only contract tests for scripts/vm/lib/deps-cache.sh — the local
# dependency cache that replaces version pinning as the reproducibility
# mechanism. Covers the parts that must hold for the cache to be
# trustworthy at all:
#   - wrong-digest content is NEVER promoted into the cache
#   - offline mode never touches the network and names the missing key
#   - atomic promote leaves no .partial residue
#   - manifest + path resolution contract
# Fixtures are served by a localhost http.server (the same transport the
# host→guest staging uses); no real network is touched.

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    LIB="$REPO_ROOT/scripts/vm/lib/deps-cache.sh"
    [ -f "$LIB" ]
    WORK="$BATS_TEST_TMPDIR/work"
    mkdir -p "$WORK/src" "$WORK/cache"
    printf 'hello artifact' > "$WORK/src/blob.bin"
    GOOD_SHA="$(sha256sum "$WORK/src/blob.bin" | awk '{print $1}')"
    BAD_SHA="0000000000000000000000000000000000000000000000000000000000000000"
    export QDISTRO_DEPS_CACHE_DIR="$WORK/cache"
    unset QDISTRO_DEPS_OFFLINE QCI_OFFLINE QDISTRO_REPO_BASE

    PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    # exec so $! is python's own pid — without it the subshell may fork a
    # grandchild that survives `kill`, keeps bats' command pipe open and
    # hangs the whole run.
    ( cd "$WORK/src" && exec python3 -m http.server "$PORT" >/dev/null 2>&1 </dev/null ) &
    SERVER_PID=$!
    for _ in $(seq 1 50); do
        wget -q -O /dev/null "http://127.0.0.1:$PORT/" && break
        sleep 0.1
    done
    URL="http://127.0.0.1:$PORT"
}

teardown() {
    kill "$SERVER_PID" 2>/dev/null
    wait "$SERVER_PID" 2>/dev/null || true
    if [ -n "${COUNT_PID:-}" ]; then
        kill "$COUNT_PID" 2>/dev/null
        wait "$COUNT_PID" 2>/dev/null || true
    fi
    # Always: any test may start the proxy (online wiring always does).
    bash -c '. "$1"; qd_serve_stop' _ "$LIB" 2>/dev/null || true
}

# start_counting_server <serve-dir> <hit-log> <delay-s>
# A threaded http.server that appends every GET path to <hit-log> and
# sleeps <delay-s> per request — the oracle for "exactly one upstream
# pass" when N cache clients race (a serialization failure shows up as
# duplicate hits or a torn entry). Prints nothing; sets COUNT_URL/COUNT_PID.
start_counting_server() {
    local root="$1" hits="$2" delay="$3" port
    : > "$hits"
    port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    ( exec python3 - "$root" "$hits" "$delay" "$port" <<'PY'
import functools, http.server, sys, time
root, hits, delay, port = sys.argv[1], sys.argv[2], float(sys.argv[3]), int(sys.argv[4])
class H(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        with open(hits, "a") as f:
            f.write(self.path + "\n")
        if delay:
            time.sleep(delay)
        super().do_GET()
    def log_message(self, *a):
        pass
h = functools.partial(H, directory=root)
http.server.ThreadingHTTPServer(("127.0.0.1", port), h).serve_forever()
PY
    ) &
    COUNT_PID=$!
    for _ in $(seq 1 50); do
        wget -q -O /dev/null "http://127.0.0.1:$port/" && break
        sleep 0.1
    done
    COUNT_URL="http://127.0.0.1:$port"
}

# Run a function from the lib in a clean shell with the cache root set;
# extra args become env vars (runq "qd_fetch ..." QDISTRO_DEPS_OFFLINE=1).
runq() {
    run env QDISTRO_DEPS_CACHE_DIR="$WORK/cache" "${@:2}" \
        bash -c ". '$LIB'; $1" --
}

@test "qd_fetch cold-stores an artifact under class/sha256/basename" {
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    # `run` merges stderr ([deps-cache] progress) — the path is the last line
    local path="${lines[-1]}"
    [ "$path" = "$WORK/cache/blob/$GOOD_SHA/blob.bin" ]
    [ -s "$path" ]
    [ "$(cat "$path")" = "hello artifact" ]
    [ -f "$WORK/cache/blob/$GOOD_SHA/manifest.txt" ]
    grep -Fxq "sha256=$GOOD_SHA" "$WORK/cache/blob/$GOOD_SHA/manifest.txt"
    grep -Fxq "class=blob" "$WORK/cache/blob/$GOOD_SHA/manifest.txt"
    grep -Fxq "url=$URL/blob.bin" "$WORK/cache/blob/$GOOD_SHA/manifest.txt"
}

@test "qd_fetch warm-hit returns the path without touching the URL" {
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    rm "$WORK/src/blob.bin"
    runq "qd_fetch blob '$URL/deleted.bin' '$GOOD_SHA' blob.bin"
    [ "$status" -eq 0 ]
    [ -s "$output" ]
}

@test "qd_fetch refuses a digest mismatch and promotes nothing" {
    runq "qd_fetch blob '$URL/blob.bin' '$BAD_SHA'"
    [ "$status" -ne 0 ]
    [[ "$output" == *"digest mismatch"* ]]
    [ ! -e "$WORK/cache/blob/$BAD_SHA/blob.bin" ]
    [ -z "$(find "$WORK/cache/.partial" -mindepth 1 -print -quit 2>/dev/null)" ]
}

@test "offline mode fails closed and names the missing key" {
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'" QDISTRO_DEPS_OFFLINE=1
    [ "$status" -ne 0 ]
    [[ "$output" == *"offline: missing dependency blob/$GOOD_SHA"* ]]
    [ ! -d "$WORK/cache/blob" ]
}

@test "QCI_OFFLINE=1 implies deps offline" {
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'" QCI_OFFLINE=1
    [ "$status" -ne 0 ]
    [[ "$output" == *"offline: missing dependency"* ]]
}

@test "offline mode succeeds on a warm cache" {
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    rm "$WORK/src/blob.bin"
    runq "qd_fetch blob '$URL/deleted.bin' '$GOOD_SHA' blob.bin" QDISTRO_DEPS_OFFLINE=1
    [ "$status" -eq 0 ]
    [ -s "$output" ]
}

@test "qd_path rejects traversal, reserved names and malformed keys" {
    runq "qd_path blob '$GOOD_SHA' '../evil'"
    [ "$status" -ne 0 ]
    runq "qd_path blob 'notasha' 'x'"
    [ "$status" -ne 0 ]
    runq "qd_path 'bad class' '$GOOD_SHA' 'x'"
    [ "$status" -ne 0 ]
    # containment: dot-path classes and metadata/lock/tmp basenames are refused
    runq "qd_path '..' '$GOOD_SHA' 'x'";          [ "$status" -ne 0 ]
    runq "qd_path '.' '$GOOD_SHA' 'x'";           [ "$status" -ne 0 ]
    runq "qd_path 'a/b' '$GOOD_SHA' 'x'";         [ "$status" -ne 0 ]
    runq "qd_path blob '$GOOD_SHA' 'manifest.txt'"; [ "$status" -ne 0 ]
    runq "qd_path blob '$GOOD_SHA' '.hidden'";    [ "$status" -ne 0 ]
    runq "qd_path blob '$GOOD_SHA' 'x.lock'";     [ "$status" -ne 0 ]
    # reserved basename rejected BEFORE any store — a manifest.txt-named
    # artifact can never be overwritten by its own manifest write
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA' manifest.txt"
    [ "$status" -ne 0 ]
}

@test "hostile manifest basename cannot escape the entry dir" {
    # A manifest naming ../anything is ignored; resolution falls through
    # to the filtered entry scan instead of escaping the cache root.
    local dir="$WORK/cache/blob/$GOOD_SHA"
    mkdir -p "$dir"
    printf 'real' > "$dir/real.bin"
    printf 'class=blob\nbasename=../MANIFEST\n' > "$dir/manifest.txt"
    runq "qd_entry_name blob '$GOOD_SHA'"
    [ "$status" -eq 0 ]; [ "$output" = "real.bin" ]
    # qd_has with an explicit traversal basename is refused outright
    runq "qd_has blob '$GOOD_SHA' '../MANIFEST'"; [ "$status" -ne 0 ]
}

@test "osci helper refuses network branches under offline mode" {
    # The signature-verified cloud path shares the offline contract: a
    # missing entry fails closed naming the cache, warm verification
    # stays local. Exercised directly (fixture crypto not needed — the
    # failure must precede any download attempt).
    local osci="$REPO_ROOT/scripts/vm/lib/opensuse-cloud-image.sh"
    run env QDISTRO_DEPS_OFFLINE=1 bash -c \
        ". '$osci'; download_verified_cloud_image 'http://127.0.0.1:1/x.qcow2' '$WORK/cache/cloud/x/x.qcow2' 2>&1"
    [ "$status" -ne 0 ]
    [[ "$output" == *"offline:"* ]]
    # Nothing was fetched or created at the artifact path.
    [ ! -e "$WORK/cache/cloud/x/x.qcow2" ]
}

@test "qd_has and qd_entry_name resolve the stored artifact" {
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    runq "qd_has blob '$GOOD_SHA' blob.bin";            [ "$status" -eq 0 ]
    runq "qd_has blob '$GOOD_SHA' missing.bin";          [ "$status" -eq 1 ]
    runq "qd_has blob '$BAD_SHA'";                       [ "$status" -eq 1 ]
    runq "qd_entry_name blob '$GOOD_SHA'"
    [ "$status" -eq 0 ]; [ "$output" = "blob.bin" ]
}

@test "qd_verify re-hashes the stored artifact against its key" {
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    runq "qd_verify blob '$GOOD_SHA'";                   [ "$status" -eq 0 ]
    printf 'tampered' > "$WORK/cache/blob/$GOOD_SHA/blob.bin"
    runq "qd_verify blob '$GOOD_SHA'"
    [ "$status" -ne 0 ]
    [[ "$output" == *"corrupt"* ]]
}

@test "qd_store_file adopts a local file and writes the manifest" {
    printf 'adopted' > "$WORK/src/adopt.bin"
    local sha; sha="$(sha256sum "$WORK/src/adopt.bin" | awk '{print $1}')"
    runq "qd_store_file rpm '$sha' '$WORK/src/adopt.bin' 'local://adopt'"
    [ "$status" -eq 0 ]
    [ "$output" = "$WORK/cache/rpm/$sha/adopt.bin" ]
    [ ! -e "$WORK/src/adopt.bin" ]
    grep -Fxq "url=local://adopt" "$WORK/cache/rpm/$sha/manifest.txt"
}

@test "qd_offline_check reports every missing entry" {
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    runq "printf '%s\n' 'blob $GOOD_SHA blob.bin' 'rpm $BAD_SHA x.rpm' | qd_offline_check"
    [ "$status" -ne 0 ]
    [[ "$output" == *"MISSING rpm $BAD_SHA x.rpm"* ]]
    [[ "$output" != *"MISSING blob"* ]]
}

@test "qd_fetch_sidecar stores .sha256 next to the artifact" {
    printf 'deadbeef  blob.bin' > "$WORK/src/blob.bin.sha256"
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    runq "qd_fetch_sidecar blob '$GOOD_SHA' '$URL/blob.bin' '.sha256'"
    [ "$status" -eq 0 ]
    [ "$output" = "$WORK/cache/blob/$GOOD_SHA/blob.bin.sha256" ]
    [ -s "$output" ]
}

@test "sidecar-only entry does not count as a cached artifact" {
    # Regression: a sidecar/lock file beside a missing artifact must not
    # satisfy qd_has — qd_adopt and qd_offline_check gate on it.
    local dir="$WORK/cache/blob/$GOOD_SHA"
    mkdir -p "$dir"
    printf 'deadbeef  blob.bin' > "$dir/blob.bin.sha256"
    : > "$dir/blob.bin.lock"
    printf 'class=blob\nsha256=%s\nbasename=blob.bin\n' "$GOOD_SHA" > "$dir/manifest.txt"
    runq "qd_has blob '$GOOD_SHA'";                       [ "$status" -eq 1 ]
    runq "qd_entry_name blob '$GOOD_SHA'";                [ "$status" -ne 0 ]
    runq "printf '%s\n' 'blob $GOOD_SHA -' | qd_offline_check"
    [ "$status" -ne 0 ]
    [[ "$output" == *"MISSING blob"* ]]
}

@test "qd_fetch resolves same digest under a different basename without fetching" {
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    rm "$WORK/src/blob.bin"
    # A URL whose basename differs from the stored artifact's: the content
    # key (sha256) must still warm-hit and return the stored name.
    runq "qd_fetch blob '$URL/other-name.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "$WORK/cache/blob/$GOOD_SHA/blob.bin" ]
}

@test "qd_adopt moves a legacy file + sidecars under the upstream basename" {
    printf 'legacy bytes' > "$WORK/src/legacy.img"
    printf 'sig' > "$WORK/src/legacy.img.sha256"
    printf 'ascsig' > "$WORK/src/legacy.img.sha256.asc"
    local sha; sha="$(sha256sum "$WORK/src/legacy.img" | awk '{print $1}')"
    runq "qd_adopt cloud '$sha' '$WORK/src/legacy.img' 'https://example.test/dir/upstream.img'"
    [ "$status" -eq 0 ]
    [ -s "$WORK/cache/cloud/$sha/upstream.img" ]
    [ -s "$WORK/cache/cloud/$sha/upstream.img.sha256" ]
    [ -s "$WORK/cache/cloud/$sha/upstream.img.sha256.asc" ]
    [ ! -e "$WORK/src/legacy.img" ]
    grep -Fxq "basename=upstream.img" "$WORK/cache/cloud/$sha/manifest.txt"
    # A wrong-digest legacy file is left in place with a warning.
    printf 'other' > "$WORK/src/wrong.img"
    runq "qd_adopt cloud '$BAD_SHA' '$WORK/src/wrong.img' 'https://example.test/x.img'"
    [ "$status" -eq 0 ]
    [ -e "$WORK/src/wrong.img" ]
    # no artifact is stored (the dir may hold just the artifact .lock —
    # lockfiles persist deliberately; qd_has ignores lock-only entries)
    [ ! -f "$WORK/cache/cloud/$BAD_SHA/x.img" ]
    runq "qd_has cloud '$BAD_SHA'";   [ "$status" -eq 1 ]
}

@test "root honors QDISTRO_DEPS_CACHE_DIR then QDWIN_CACHE_DIR then XDG default" {
    run env -u QDISTRO_DEPS_CACHE_DIR QDWIN_CACHE_DIR=/alt bash -c ". '$LIB'; qd_deps_root"
    [ "$output" = "/alt" ]
    run env -u QDISTRO_DEPS_CACHE_DIR -u QDWIN_CACHE_DIR HOME=/h bash -c ". '$LIB'; qd_deps_root"
    [ "$output" = "/h/.cache/qdistro" ]
}

@test "qd_display_info_tarball: warm seed returns path offline, honors env pin override" {
    local fixture_sha other_sha
    fixture_sha="$(printf 'fake-ldi-tarball' | sha256sum | awk '{print $1}')"
    printf 'fake-ldi-tarball' > "$WORK/src/ldi.tgz"
    # Warm the entry under an overridden pin (no network anywhere).
    run bash -c ". '$LIB'; qd_store_file src '$fixture_sha' '$WORK/src/ldi.tgz' 'https://example.test/ldi.tgz'"
    [ "$status" -eq 0 ]
    # Offline warm hit returns the cached path, never the network.
    run env QDISTRO_DEPS_OFFLINE=1 QDISTRO_DISPLAY_INFO_SHA256="$fixture_sha" \
        QDISTRO_DISPLAY_INFO_URL=https://example.test/ldi.tgz \
        bash -c ". '$LIB'; qd_display_info_tarball"
    [ "$status" -eq 0 ]
    [ -f "$output" ]
    # Offline cold miss fails closed.
    other_sha="$(printf 'other' | sha256sum | awk '{print $1}')"
    run env QDISTRO_DEPS_OFFLINE=1 QDISTRO_DISPLAY_INFO_SHA256="$other_sha" \
        bash -c ". '$LIB'; qd_display_info_tarball"
    [ "$status" -ne 0 ]
    [[ "$output" == *"src/"*"$other_sha"* ]] || [[ "$output" == *offline* ]]
}

# ---- OCI images (qd_oci_ensure) -------------------------------------------
# A PATH-shimmed fake podman backs every assertion — no real registry, no
# real image store. FAKE_PODMAN_DIR/images/<sha256-of-ref> marks a local
# image; calls.log records every invocation so "no pull" is checkable.

oci_setup_fake() {
    mkdir -p "$WORK/bin"
    cat > "$WORK/bin/podman" <<'POD'
#!/bin/bash
d="$FAKE_PODMAN_DIR"; mkdir -p "$d/images"
echo "podman $*" >> "$d/calls.log"
enc() { printf '%s' "$1" | sha256sum | awk '{print $1}'; }
case "$1 $2" in
    "image exists")
        [ -f "$d/images/$(enc "$3")" ] && exit 0 || exit 1 ;;
    "image inspect")
        r="$3"
        [ -f "$d/images/$(enc "$r")" ] || exit 1
        echo "sha256:$(enc "$r")"; exit 0 ;;
esac
case "$1" in
    pull)
        r="${!#}"
        [ -f "$d/block-pull" ] && { echo "FAKE: pull attempted" >&2; exit 99; }
        printf 'ref=%s\n' "$r" > "$d/images/$(enc "$r")"
        echo "sha256:$(enc "$r")"; exit 0 ;;
    save)
        out=""; r=""
        shift
        while [ $# -gt 0 ]; do
            case "$1" in -o) out="$2"; shift 2 ;; *) r="$1"; shift ;; esac
        done
        [ -f "$d/images/$(enc "$r")" ] || exit 1
        printf 'ref=%s\n' "$r" > "$out"; exit 0 ;;
    load)
        arc="${!#}"
        r="$(sed -n 's/^ref=//p' "$arc" 2>/dev/null)"
        [ -n "$r" ] || exit 1
        printf 'ref=%s\n' "$r" > "$d/images/$(enc "$r")"; exit 0 ;;
esac
exit 2
POD
    chmod +x "$WORK/bin/podman"
    export FAKE_PODMAN_DIR="$WORK/podman"
    mkdir -p "$FAKE_PODMAN_DIR"
    REF="registry.test/opensuse/tumbleweed:20990101"
    REF_SHA="$(printf '%s' "$REF" | sha256sum | awk '{print $1}')"
    runoci() {
        env PATH="$WORK/bin:$PATH" FAKE_PODMAN_DIR="$FAKE_PODMAN_DIR" \
            QDISTRO_DEPS_CACHE_DIR="$WORK/cache" "$@" \
            bash -c ". '$LIB'; qd_oci_ensure '$REF'"
    }
}

oci_img_present() {
    local s; s="$(printf '%s' "$REF" | sha256sum | awk '{print $1}')"
    mkdir -p "$FAKE_PODMAN_DIR/images"
    printf 'ref=%s\n' "$REF" > "$FAKE_PODMAN_DIR/images/$s"
}

@test "qd_oci_ensure cold: pulls, saves digest archive, maps the ref" {
    oci_setup_fake
    run runoci
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "sha256:$REF_SHA" ]
    [ -f "$WORK/cache/oci/$REF_SHA/image.tar" ]
    grep -q "url=$REF" "$WORK/cache/oci/$REF_SHA/manifest.txt"
    local refkey
    refkey="$(printf '%s' "$REF" | sha256sum | awk '{print $1}')"
    grep -qx "sha256:$REF_SHA" "$WORK/cache/oci/refs/$refkey.digest"
    grep -q 'pull' "$FAKE_PODMAN_DIR/calls.log"
    grep -q 'save' "$FAKE_PODMAN_DIR/calls.log"
}

@test "qd_oci_ensure warm: local image, no pull, archive still ensured once" {
    oci_setup_fake
    oci_img_present
    run runoci
    [ "$status" -eq 0 ]
    ! grep -q 'pull' "$FAKE_PODMAN_DIR/calls.log"
    [ -f "$WORK/cache/oci/$REF_SHA/image.tar" ]   # saved once, online
    # Second run: everything warm — no pull, no save.
    : > "$FAKE_PODMAN_DIR/calls.log"
    run runoci
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "sha256:$REF_SHA" ]
    ! grep -q 'pull\|save\|load' "$FAKE_PODMAN_DIR/calls.log"
}

@test "qd_oci_ensure offline: cached archive loads without a pull" {
    oci_setup_fake
    run runoci   # warm the cache online first
    [ "$status" -eq 0 ]
    # Simulate `podman system prune`: image gone from the local store.
    rm -f "$FAKE_PODMAN_DIR/images/$REF_SHA"
    : > "$FAKE_PODMAN_DIR/calls.log"
    touch "$FAKE_PODMAN_DIR/block-pull"           # a pull would be a bug
    export QDISTRO_DEPS_OFFLINE=1
    run runoci
    unset QDISTRO_DEPS_OFFLINE
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "sha256:$REF_SHA" ]
    grep -q 'load' "$FAKE_PODMAN_DIR/calls.log"
    ! grep -q 'pull' "$FAKE_PODMAN_DIR/calls.log"
    oci_img_present   # load restored the image marker (fake side effect check)
    [ -f "$FAKE_PODMAN_DIR/images/$REF_SHA" ]
}

@test "qd_oci_ensure offline: local image returns digest, zero podman save/pull" {
    oci_setup_fake
    oci_img_present
    touch "$FAKE_PODMAN_DIR/block-pull"
    export QDISTRO_DEPS_OFFLINE=1
    run runoci
    unset QDISTRO_DEPS_OFFLINE
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "sha256:$REF_SHA" ]
    ! grep -q 'pull\|save\|load' "$FAKE_PODMAN_DIR/calls.log"
}

@test "qd_oci_ensure offline: nothing cached fails closed naming the ref" {
    oci_setup_fake
    export QDISTRO_DEPS_OFFLINE=1
    run runoci
    unset QDISTRO_DEPS_OFFLINE
    [ "$status" -ne 0 ]
    [[ "$output" == *"$REF"* ]]
    [[ "$output" == *offline* ]]
}

@test "qd_oci_ensure offline: corrupt cached archive fails the sha256 check" {
    oci_setup_fake
    run runoci   # warm
    [ "$status" -eq 0 ]
    rm -f "$FAKE_PODMAN_DIR/images/$REF_SHA"
    printf 'corrupt' >> "$WORK/cache/oci/$REF_SHA/image.tar"
    export QDISTRO_DEPS_OFFLINE=1
    run runoci
    unset QDISTRO_DEPS_OFFLINE
    [ "$status" -ne 0 ]
    [[ "$output" == *"fails sha256"* ]]
    ! grep -q 'load' "$FAKE_PODMAN_DIR/calls.log"
}

@test "qd_oci_ensure: interrupted save (tar without sidecar) is not a warm hit" {
    oci_setup_fake
    run runoci
    [ "$status" -eq 0 ]
    rm -f "$FAKE_PODMAN_DIR/images/$REF_SHA"
    # Simulate an interrupted _qd_oci_save: archive published, no sidecar.
    rm -f "$WORK/cache/oci/$REF_SHA/image.tar.sha256"
    export QDISTRO_DEPS_OFFLINE=1
    run runoci
    [ "$status" -ne 0 ]
    [[ "$output" == *"no complete cached OCI archive"* ]]
    ! grep -q 'load' "$FAKE_PODMAN_DIR/calls.log"
    unset QDISTRO_DEPS_OFFLINE
    # Online the incomplete state repairs via pull + resave.
    run runoci
    [ "$status" -eq 0 ]
    [ -f "$WORK/cache/oci/$REF_SHA/image.tar.sha256" ]
}

# ---- concurrency --------------------------------------------------------
# The locking contract under real parallelism: the per-key flock +
# .partial staging must give exactly-once publish, and a reader must
# never observe torn bytes at a final path. The counting server is the
# oracle — N parallel cold warmers may cause ONE upstream hit for the
# artifact; more means the lock or the locked recheck is broken.

@test "parallel qd_fetch on a cold key: exactly one download, all callers converge" {
    start_counting_server "$WORK/src" "$WORK/hits.log" 0.2
    local pids=() i rc
    for i in 1 2 3; do
        env QDISTRO_DEPS_CACHE_DIR="$WORK/cache" bash -c \
            ". '$LIB'; qd_fetch blob '$COUNT_URL/blob.bin' '$GOOD_SHA'" \
            >"$WORK/out.$i" 2>/dev/null &
        pids+=("$!")
    done
    rc=0
    for i in "${pids[@]}"; do wait "$i" || rc=$((rc + 1)); done
    [ "$rc" -eq 0 ]
    # All callers converge on the same promoted path.
    for i in 1 2 3; do
        [ "$(cat "$WORK/out.$i")" = "$WORK/cache/blob/$GOOD_SHA/blob.bin" ]
    done
    # Exactly one upstream GET: losers of the flock must see the warm
    # entry in the locked recheck, never re-download.
    [ "$(grep -c '/blob\.bin$' "$WORK/hits.log")" -eq 1 ]
    runq "qd_verify blob '$GOOD_SHA'"; [ "$status" -eq 0 ]
    [ -f "$WORK/cache/blob/$GOOD_SHA/manifest.txt" ]
    # No staging residue: .partial drained, no hidden promote tmp in the entry.
    [ -z "$(find "$WORK/cache/.partial" -mindepth 1 -print -quit 2>/dev/null)" ]
    [ -z "$(find "$WORK/cache/blob/$GOOD_SHA" -name '.*' -print -quit)" ]
}

@test "qd_adopt racing qd_fetch: whichever wins, the entry is verified and fetched at most once" {
    printf 'hello artifact' > "$WORK/src/legacy.img"
    start_counting_server "$WORK/src" "$WORK/hits.log" 0.2
    env QDISTRO_DEPS_CACHE_DIR="$WORK/cache" bash -c \
        ". '$LIB'; qd_adopt blob '$GOOD_SHA' '$WORK/src/legacy.img' '$COUNT_URL/legacy.img'" \
        >/dev/null 2>&1 &
    local adopt_pid=$!
    env QDISTRO_DEPS_CACHE_DIR="$WORK/cache" bash -c \
        ". '$LIB'; qd_fetch blob '$COUNT_URL/blob.bin' '$GOOD_SHA' >/dev/null" &
    local fetch_pid=$!
    local rc_a=0 rc_f=0
    wait "$adopt_pid" || rc_a=$?
    wait "$fetch_pid" || rc_f=$?
    [ "$rc_a" -eq 0 ] && [ "$rc_f" -eq 0 ]
    # If adopt won the entry, the fetch never downloaded; if fetch won,
    # it downloaded exactly once. There is no interleaving with two.
    [ "$(grep -c '/blob\.bin$' "$WORK/hits.log")" -le 1 ]
    runq "qd_verify blob '$GOOD_SHA'"; [ "$status" -eq 0 ]
}

@test "a reader never observes torn bytes while writers publish" {
    # Eight writers publish DISTINCT keys through .partial; a reader
    # sweeps the entry tree the whole time and requires every artifact
    # visible at a final path to hash to its directory key — a torn or
    # half-promoted file is a hard fail.
    start_counting_server "$WORK/src" "$WORK/hits.log" 0.05
    local keys=() pids=() i sha
    for i in 1 2 3 4 5 6 7 8; do
        printf 'payload-%s' "$i" > "$WORK/src/blob-$i.bin"
        sha="$(sha256sum "$WORK/src/blob-$i.bin" | awk '{print $1}')"
        keys+=("$sha")
    done
    (
        for _ in $(seq 1 400); do
            for f in "$WORK/cache/blob"/*/*; do
                [ -f "$f" ] || continue
                case "$(basename "$f")" in
                    manifest.txt|*.lock|*.sha256|*.asc|*.sig) continue ;;
                esac
                local_key="$(basename "$(dirname "$f")")"
                [ "$(sha256sum "$f" 2>/dev/null | awk '{print $1}')" = "$local_key" ] \
                    || echo "TORN $f" >> "$WORK/torn.log"
            done
        done
    ) &
    local reader_pid=$!
    for i in "${!keys[@]}"; do
        env QDISTRO_DEPS_CACHE_DIR="$WORK/cache" bash -c \
            ". '$LIB'; qd_fetch blob '$COUNT_URL/blob-$((i + 1)).bin' '${keys[$i]}' >/dev/null" &
        pids+=("$!")
    done
    local rc=0
    for i in "${pids[@]}"; do wait "$i" || rc=$((rc + 1)); done
    wait "$reader_pid"
    [ "$rc" -eq 0 ]
    [ ! -s "$WORK/torn.log" ]
    for i in "${keys[@]}"; do
        runq "qd_verify blob '$i'"; [ "$status" -eq 0 ]
    done
}

@test "a dead writer's residue never blocks or fools the next caller" {
    # kill -9 mid-publish leaves: the per-key lockfile (flock dies with
    # the process, the file persists), a private .partial staging dir,
    # and possibly an EMPTY entry dir. None of it may count as warm or
    # stall the next fetcher.
    mkdir -p "$WORK/cache/.locks" \
        "$WORK/cache/.partial/blob-${GOOD_SHA:0:12}.dead9" \
        "$WORK/cache/blob/$GOOD_SHA"
    : > "$WORK/cache/.locks/blob-$GOOD_SHA.lock"
    printf 'half-downloaded' \
        > "$WORK/cache/.partial/blob-${GOOD_SHA:0:12}.dead9/blob.bin"
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    [ "${lines[-1]}" = "$WORK/cache/blob/$GOOD_SHA/blob.bin" ]
    [ "$(cat "$WORK/cache/blob/$GOOD_SHA/blob.bin")" = "hello artifact" ]
    runq "qd_verify blob '$GOOD_SHA'"; [ "$status" -eq 0 ]
}

@test "parallel qd_fetch_sidecar: no temp collision, no residue, one file" {
    # Regression for the pre-mktemp temp-name collision: concurrent
    # sidecar fetches are deliberately UNLOCKED (mktemp + mv is the
    # atomicity mechanism); both may download but the final file must
    # be whole and no .sidecar.* staging may leak.
    printf 'deadbeef  blob.bin' > "$WORK/src/blob.bin.sha256"
    runq "qd_fetch blob '$URL/blob.bin' '$GOOD_SHA'"
    [ "$status" -eq 0 ]
    local pids=() i rc
    for i in 1 2; do
        env QDISTRO_DEPS_CACHE_DIR="$WORK/cache" bash -c \
            ". '$LIB'; qd_fetch_sidecar blob '$GOOD_SHA' '$URL/blob.bin' '.sha256'" \
            >"$WORK/sc-out.$i" 2>/dev/null &
        pids+=("$!")
    done
    rc=0
    for i in "${pids[@]}"; do wait "$i" || rc=$((rc + 1)); done
    [ "$rc" -eq 0 ]
    for i in 1 2; do
        [ "$(cat "$WORK/sc-out.$i")" = "$WORK/cache/blob/$GOOD_SHA/blob.bin.sha256" ]
    done
    [ "$(cat "$WORK/cache/blob/$GOOD_SHA/blob.bin.sha256")" = "deadbeef  blob.bin" ]
    [ -z "$(find "$WORK/cache/blob/$GOOD_SHA" -name '.sidecar.*' -print -quit)" ]
}

@test "parallel cold qd_oci_ensure: one save, one archive, same digest to all" {
    oci_setup_fake
    local i rc pids=()
    for i in 1 2; do
        env PATH="$WORK/bin:$PATH" FAKE_PODMAN_DIR="$FAKE_PODMAN_DIR" \
            QDISTRO_DEPS_CACHE_DIR="$WORK/cache" \
            bash -c ". '$LIB'; qd_oci_ensure '$REF'" \
            >"$WORK/oci-out.$i" 2>"$WORK/oci-err.$i" &
        pids+=("$!")
    done
    rc=0
    for i in "${pids[@]}"; do wait "$i" || rc=$((rc + 1)); done
    [ "$rc" -eq 0 ]
    for i in 1 2; do
        [ "$(tail -1 "$WORK/oci-out.$i")" = "sha256:$REF_SHA" ]
    done
    # Pulls may legitimately race (pull is outside the lock), but the
    # archive is written under oci-<hex>.lock: exactly ONE podman save.
    [ "$(grep -c 'podman save' "$FAKE_PODMAN_DIR/calls.log")" -eq 1 ]
    [ -f "$WORK/cache/oci/$REF_SHA/image.tar" ]
    [ -f "$WORK/cache/oci/$REF_SHA/image.tar.sha256" ]
    [ -f "$WORK/cache/oci/$REF_SHA/manifest.txt" ]
    grep -qx "sha256:$REF_SHA" "$WORK/cache/oci/refs/$REF_SHA.digest"
    # No staged tmp leftovers inside the entry dir.
    [ -z "$(find "$WORK/cache/oci/$REF_SHA" -name '.*' -type f -print -quit)" ]
}

# ---- lane wiring: qd_repo_wire_guest / qd_repo_wire_container ----------------
# Cached metadata for wiring tests — qd_repo_present checks only the marker
# qd-repo-cache.py publishes after repomd + every metadata file verified.
fab_mirror() {
    local snap="$1"
    mkdir -p "$WORK/cache/repos/$snap/tumbleweed/repo/oss" \
             "$WORK/cache/repos/$snap/tumbleweed/repo/non-oss"
    echo '{"repo":"oss"}' > "$WORK/cache/repos/$snap/tumbleweed/repo/oss/metadata.json"
    echo '{"repo":"non-oss"}' > "$WORK/cache/repos/$snap/tumbleweed/repo/non-oss/metadata.json"
}

@test "qd_repo_wire_guest online: cold cache still wires the pull-through proxy" {
    SERVE_STARTED=1
    run bash -c ". '$LIB'; qd_repo_wire_guest 20260101 10.0.2.2; echo base=\$QDISTRO_REPO_BASE"
    [ "$status" -eq 0 ]
    [[ "$output" =~ base=http://10\.0\.2\.2:[0-9]+/repos ]]
    [ "$(cat "$WORK/cache/.serve.mode")" = online ]
}

@test "qd_repo_wire_guest offline: no cached metadata → no-op, no server, no base" {
    run env QCI_OFFLINE=1 bash -c ". '$LIB'; qd_repo_wire_guest 20260101 10.0.2.2; echo base=\${QDISTRO_REPO_BASE:-unset}"
    [ "$status" -eq 0 ]
    [ "$output" = "base=unset" ]
    [ ! -s "$WORK/cache/.serve.port" ]
}

@test "qd_repo_wire_guest offline: cached metadata → cache-only proxy + guest base" {
    fab_mirror 20260101
    mkdir -p "$WORK/cache/repos/20260101/tumbleweed/repo/oss/repodata"
    echo '<repomd/>' > "$WORK/cache/repos/20260101/tumbleweed/repo/oss/repodata/repomd.xml"
    SERVE_STARTED=1
    run env QCI_OFFLINE=1 bash -c ". '$LIB'; qd_repo_wire_guest 20260101 169.254.2.2; echo base=\$QDISTRO_REPO_BASE"
    [ "$status" -eq 0 ]
    [[ "$output" =~ base=http://169\.254\.2\.2:[0-9]+/repos ]]
    [ "$(cat "$WORK/cache/.serve.mode")" = offline ]
    # the proxy serves the cached tree
    port="$(bash -c '. "$1"; qd_serve_port' _ "$LIB")"
    wget -q -O - "http://127.0.0.1:$port/repos/20260101/tumbleweed/repo/oss/repodata/repomd.xml" \
        | grep -q '<repomd/>'
}

@test "qd_repo_wire_guest: explicit QDISTRO_REPO_BASE wins, no server" {
    run bash -c ". '$LIB'; export QDISTRO_REPO_BASE=http://hand.set/base; qd_repo_wire_guest 20260101; echo base=\$QDISTRO_REPO_BASE"
    [ "$status" -eq 0 ]
    [ "$output" = "base=http://hand.set/base" ]
    [ ! -s "$WORK/cache/.serve.port" ]
}

@test "qd_repo_wire_guest: QDISTRO_REPO_PROXY=0 bypasses the proxy" {
    run env QDISTRO_REPO_PROXY=0 bash -c ". '$LIB'; qd_repo_wire_guest 20260101; echo base=\${QDISTRO_REPO_BASE:-unset}"
    [ "$status" -eq 0 ]
    [ "$output" = "base=unset" ]
    [ ! -s "$WORK/cache/.serve.port" ]
}

@test "qd_repo_wire_guest offline: only-oss metadata does not wire" {
    mkdir -p "$WORK/cache/repos/20260101/tumbleweed/repo/oss"
    echo '{}' > "$WORK/cache/repos/20260101/tumbleweed/repo/oss/metadata.json"
    run env QCI_OFFLINE=1 bash -c ". '$LIB'; qd_repo_wire_guest 20260101; echo base=\${QDISTRO_REPO_BASE:-unset}"
    [ "$status" -eq 0 ]
    [ "$output" = "base=unset" ]
}

@test "qd_repo_wire_container offline: ro file:// mount + --network=none, in THIS shell" {
    fab_mirror 20260101
    # The contract is that the export reaches the CALLER — invoking
    # through `run`/a subshell would hide exactly the bug this pins.
    . "$LIB"
    export QCI_OFFLINE=1
    qd_repo_wire_container 20260101 /qdistro-mirror
    [ "$QDISTRO_REPO_BASE" = "file:///qdistro-mirror" ]
    [ "${QD_REPO_CONTAINER_ARGS[*]}" = "--volume $WORK/cache/repos:/qdistro-mirror:ro,z --network=none" ]
}

@test "qd_repo_wire_container online: proxy via host.containers.internal, no extra args" {
    SERVE_STARTED=1
    . "$LIB"
    qd_repo_wire_container 20260101 /qdistro-mirror
    [[ "$QDISTRO_REPO_BASE" =~ ^http://host\.containers\.internal:[0-9]+/repos$ ]]
    [ "${#QD_REPO_CONTAINER_ARGS[@]}" -eq 0 ]
}

@test "qd_repo_wire_container offline: no metadata → rc 1, no base" {
    . "$LIB"
    export QCI_OFFLINE=1
    run qd_repo_wire_container 20260101 /qdistro-mirror
    [ "$status" -eq 1 ]
    [ -z "${QDISTRO_REPO_BASE:-}" ]
}

@test "qd_repo_wire_container: explicit base wins" {
    fab_mirror 20260101
    . "$LIB"
    export QDISTRO_REPO_BASE=http://hand.set/base
    run qd_repo_wire_container 20260101 /qdistro-mirror
    [ "$status" -eq 1 ]
    [ "$QDISTRO_REPO_BASE" = "http://hand.set/base" ]
}

@test "qd_repo_base_safe rejects whitespace/metachar injection" {
    . "$LIB"
    run qd_repo_base_safe 'http://10.0.2.2:8000/repos'; [ "$status" -eq 0 ]
    run qd_repo_base_safe 'file:///qdistro-mirror'; [ "$status" -eq 0 ]
    run qd_repo_base_safe 'http://x/; rm -rf /'; [ "$status" -eq 1 ]
    run qd_repo_base_safe "http://x/
[evil]"; [ "$status" -eq 1 ]
    run qd_repo_base_safe 'http://x/$(id)'; [ "$status" -eq 1 ]
    run qd_repo_base_safe 'http://x/a&rm -rf /'; [ "$status" -eq 1 ]
    run qd_repo_base_safe 'not-a-url'; [ "$status" -eq 1 ]
}

@test "qd_guestfs_host_addr: passt-era libguestfs + passt → the host's default gateway" {
    mkdir -p "$WORK/fakebin"
    # Real `guestfish version` output is multi-line key: value — NOT
    # "guestfish 1.60.2".
    printf '#!/bin/sh\nprintf "major: 1\\nminor: 60\\nrelease: 1\\nextra: \\n"\n' > "$WORK/fakebin/guestfish"
    printf '#!/bin/sh\nexit 0\n' > "$WORK/fakebin/passt"
    # passt maps the guest's view of the host's default gateway to the host.
    printf '#!/bin/sh\necho "default via 192.0.2.77 dev eth0 proto dhcp"\n' > "$WORK/fakebin/ip"
    chmod +x "$WORK/fakebin/guestfish" "$WORK/fakebin/passt" "$WORK/fakebin/ip"
    run bash -c "export PATH='$WORK/fakebin':\"\$PATH\"; . '$LIB'; qd_guestfs_host_addr"
    [ "$output" = "192.0.2.77" ]
    run bash -c "export PATH='$WORK/fakebin':\"\$PATH\" QDISTRO_PASST_HOST_ADDR=198.51.100.9; . '$LIB'; qd_guestfs_host_addr"
    [ "$output" = "198.51.100.9" ]
}

@test "qd_passt_host_addr: no default route fails closed" {
    mkdir -p "$WORK/fakebin"
    printf '#!/bin/sh\nexit 0\n' > "$WORK/fakebin/ip"
    chmod +x "$WORK/fakebin/ip"
    run bash -c "export PATH='$WORK/fakebin':\"\$PATH\"; . '$LIB'; qd_passt_host_addr"
    [ "$status" -ne 0 ]
    [[ "$output" == *"no IPv4 default gateway"* ]]
}

@test "qd_guestfs_host_addr: no passt → 10.0.2.2" {
    mkdir -p "$WORK/fakebin"
    printf '#!/bin/sh\nprintf "major: 1\\nminor: 60\\nrelease: 1\\nextra: \\n"\n' > "$WORK/fakebin/guestfish"
    chmod +x "$WORK/fakebin/guestfish"
    # PATH restricted to the fake: the real passt must not be findable.
    run bash -c "export PATH='$WORK/fakebin'; . '$LIB'; qd_guestfs_host_addr"
    [ "$output" = "10.0.2.2" ]
}

@test "qd_guestfs_host_addr: pre-1.60 libguestfs + passt → 10.0.2.2" {
    mkdir -p "$WORK/fakebin"
    printf '#!/bin/sh\nprintf "major: 1\\nminor: 44\\nrelease: 3\\nextra: \\n"\n' > "$WORK/fakebin/guestfish"
    printf '#!/bin/sh\nexit 0\n' > "$WORK/fakebin/passt"
    chmod +x "$WORK/fakebin/guestfish" "$WORK/fakebin/passt"
    run bash -c "export PATH='$WORK/fakebin':\"\$PATH\"; . '$LIB'; qd_guestfs_host_addr"
    [ "$output" = "10.0.2.2" ]
}

@test "qd_oci_cached: refmap + complete entry → warm; missing sidecar → cold" {
    mkdir -p "$WORK/oci-bin" "$WORK/cache/oci/refs"
    REF_SHA="$(printf 'a%.0s' {1..64})"
    printf 'sha256:%s\n' "$REF_SHA" > "$WORK/cache/oci/refs/$(printf 'oci.local/base:latest' | sha256sum | awk '{print $1}').digest"
    run bash -c ". '$LIB'; qd_oci_cached oci.local/base:latest"
    [ "$status" -eq 1 ]
    mkdir -p "$WORK/cache/oci/$REF_SHA"
    # Empty files are INCOMPLETE, not warm (interrupted-save shape).
    touch "$WORK/cache/oci/$REF_SHA/image.tar"
    run bash -c ". '$LIB'; qd_oci_cached oci.local/base:latest"
    [ "$status" -eq 1 ]
    printf 'fake-archive-bytes' > "$WORK/cache/oci/$REF_SHA/image.tar"
    echo x > "$WORK/cache/oci/$REF_SHA/manifest.txt"
    (cd "$WORK/cache/oci/$REF_SHA" && sha256sum image.tar > image.tar.sha256)
    run bash -c ". '$LIB'; qd_oci_cached oci.local/base:latest"
    [ "$status" -eq 0 ]
    run bash -c ". '$LIB'; qd_oci_cached oci.local/base:latest --verify"
    [ "$status" -eq 0 ]
    # Corrupt bytes: structure still complete, --verify fails.
    printf 'tampered' >> "$WORK/cache/oci/$REF_SHA/image.tar"
    run bash -c ". '$LIB'; qd_oci_cached oci.local/base:latest"
    [ "$status" -eq 0 ]
    run bash -c ". '$LIB'; qd_oci_cached oci.local/base:latest --verify"
    [ "$status" -eq 1 ]
}

@test "parallel qd_serve_start: one server, one port, all callers agree" {
    fab_mirror 20260101
    SERVE_STARTED=1
    local pids=()
    for i in 1 2 3; do
        bash -c ". '$LIB'; qd_serve_start" >"$WORK/serve-out.$i" 2>"$WORK/serve-err.$i" &
        pids+=("$!")
    done
    local rc=0
    for i in "${pids[@]}"; do wait "$i" || rc=$((rc + 1)); done
    [ "$rc" -eq 0 ]
    # All three starters report the SAME bound port.
    [ "$(sort -u "$WORK/serve-out".[123] | wc -l)" -eq 1 ]
    # State files record a live server on that port, and it answers.
    local port pid
    port="$(cat "$WORK/cache/.serve.port")"
    pid="$(cat "$WORK/cache/.serve.pid")"
    kill -0 "$pid" 2>/dev/null
    wget -q -O - "http://127.0.0.1:$port/" >/dev/null
}

# Stub podman for the native-builder offline contract: records every call,
# reports the builder image absent, and "builds" successfully.
_native_builder_stub() {
    mkdir -p "$WORK/bin"
    cat > "$WORK/bin/podman" <<'SH'
#!/bin/bash
echo "podman $*" >> "$PODMAN_LOG"
case "$1 $2" in
    "info --format") echo true ;;
    "image exists") exit 1 ;;
    "image inspect")
        case "$*" in *Labels*) echo 20260101 ;; *) echo sha256:base ;; esac ;;
    build*) exit 0 ;;
esac
SH
    chmod +x "$WORK/bin/podman"
    export PATH="$WORK/bin:$PATH" PODMAN_LOG="$WORK/podman.log"
    : > "$PODMAN_LOG"
}

_run_native_builder() {
    run bash -c "
        . '$REPO_ROOT/scripts/vm/lib/native-builder.sh'
        . '$LIB'
        qdistro_native_builder_base_image() { echo registry.example/tw:20260101; }
        QDISTRO_SUBSTRATE_SNAPSHOT=20260101 QDISTRO_SUBSTRATE_ARCH=x86_64
        QDWIN_CACHE_DIR='$WORK/qdwin'
        qdistro_ensure_native_builder_image"
}

@test "native builder: offline with no cached repo metadata fails closed, never builds" {
    _native_builder_stub
    export QCI_OFFLINE=1
    _run_native_builder
    [ "$status" -eq 3 ]
    [[ "$output" == *"no cached repo metadata"* ]]
    ! grep -q 'podman build' "$PODMAN_LOG"
}

@test "native builder: offline with cached metadata builds from cache under --network=none" {
    _native_builder_stub
    local snap=20260101
    mkdir -p "$WORK/cache/repos/$snap/tumbleweed/repo/oss" "$WORK/cache/repos/$snap/tumbleweed/repo/non-oss"
    echo '{"repo":"oss"}' > "$WORK/cache/repos/$snap/tumbleweed/repo/oss/metadata.json"
    echo '{"repo":"non-oss"}' > "$WORK/cache/repos/$snap/tumbleweed/repo/non-oss/metadata.json"
    export QCI_OFFLINE=1
    _run_native_builder
    [ "$status" -eq 0 ]
    [[ "$output" == *"localhost/qdistro/native-builder:"* ]]
    grep 'podman build' "$PODMAN_LOG" | grep -q -- '--network=none'
    grep 'podman build' "$PODMAN_LOG" | grep -q -- "$WORK/cache/repos:/qdistro-mirror:ro,z"
    grep 'podman build' "$PODMAN_LOG" | grep -q -- 'QDISTRO_REPO_BASE=file:///qdistro-mirror'
}

@test "native builder: offline ignores an exported external repo base — mirror + no network, or fail closed" {
    _native_builder_stub
    export QCI_OFFLINE=1 QDISTRO_REPO_BASE=https://download.example.org/history
    _run_native_builder
    [ "$status" -eq 3 ]
    [[ "$output" == *"no cached repo metadata"* ]]
    ! grep -q 'podman build' "$PODMAN_LOG"

    local snap=20260101
    mkdir -p "$WORK/cache/repos/$snap/tumbleweed/repo/oss" "$WORK/cache/repos/$snap/tumbleweed/repo/non-oss"
    echo '{"repo":"oss"}' > "$WORK/cache/repos/$snap/tumbleweed/repo/oss/metadata.json"
    echo '{"repo":"non-oss"}' > "$WORK/cache/repos/$snap/tumbleweed/repo/non-oss/metadata.json"
    _run_native_builder
    [ "$status" -eq 0 ]
    grep 'podman build' "$PODMAN_LOG" | grep -q -- '--network=none'
    grep 'podman build' "$PODMAN_LOG" | grep -q -- 'QDISTRO_REPO_BASE=file:///qdistro-mirror'
    ! grep 'podman build' "$PODMAN_LOG" | grep -q 'download.example.org'
}
