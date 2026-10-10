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
