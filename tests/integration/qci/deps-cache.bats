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
    unset QDISTRO_DEPS_OFFLINE QCI_OFFLINE

    PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
    ( cd "$WORK/src" && python3 -m http.server "$PORT" >/dev/null 2>&1 ) &
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

@test "qd_path rejects traversal and malformed keys" {
    runq "qd_path blob '$GOOD_SHA' '../evil'"
    [ "$status" -ne 0 ]
    runq "qd_path blob 'notasha' 'x'"
    [ "$status" -ne 0 ]
    runq "qd_path 'bad class' '$GOOD_SHA' 'x'"
    [ "$status" -ne 0 ]
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

@test "root honors QDISTRO_DEPS_CACHE_DIR then QDWIN_CACHE_DIR then XDG default" {
    run env -u QDISTRO_DEPS_CACHE_DIR QDWIN_CACHE_DIR=/alt bash -c ". '$LIB'; qd_deps_root"
    [ "$output" = "/alt" ]
    run env -u QDISTRO_DEPS_CACHE_DIR -u QDWIN_CACHE_DIR HOME=/h bash -c ". '$LIB'; qd_deps_root"
    [ "$output" = "/h/.cache/qdistro" ]
}
