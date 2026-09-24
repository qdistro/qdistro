#!/usr/bin/env bats
#
# Host-only tests for the `qci image` gate's source-ancestry check
# (ci/lib/gates/image.sh image_source_check). NO VM, NO libguestfs: the build
# dir is a fake with only a pre-extracted tree (extracted/etc/qdistro/release),
# so the gate takes its "no published artifact, fall back to the extracted
# tree" path. Drives the REAL qci runner copied into a throwaway git repo whose
# history the test controls (HEAD, an ancestor, and a side-branch commit), and
# asserts the verify-contents row in results.tsv.
#
# Why: a 2026-09-10 sibling-layout bundle left in /var/tmp/qdistro-build
# "failed" verify-contents on the monorepo with EXIT_BUILD, which read like a
# product regression. A bundle not built from this tree's history is BLOCKED
# (with both SHAs and the fix), never a fail.

setup() {
    SRC="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    T="$(mktemp -d)"
    REPO="$T/repo"
    mkdir -p "$REPO"
    cp -a "$SRC/ci" "$SRC/image" "$REPO/"
    rm -rf "$REPO/ci/runs"
    git -C "$REPO" init -q
    git -C "$REPO" config user.email t@t.invalid
    git -C "$REPO" config user.name t
    git -C "$REPO" add -A
    git -C "$REPO" commit -q -m base
    OLD=$(git -C "$REPO" rev-parse HEAD)
    local main
    main=$(git -C "$REPO" symbolic-ref --short HEAD)
    git -C "$REPO" checkout -q -b side
    echo side > "$REPO/side.txt"; git -C "$REPO" add side.txt
    git -C "$REPO" commit -q -m side
    SIDE=$(git -C "$REPO" rev-parse HEAD)
    git -C "$REPO" checkout -q "$main"
    echo head > "$REPO/head.txt"; git -C "$REPO" add head.txt
    git -C "$REPO" commit -q -m head
    HEAD_SHA=$(git -C "$REPO" rev-parse HEAD)

    BUILD="$T/build"
    mkdir -p "$BUILD/extracted/etc/qdistro"
    export QDISTRO_BUILD_DIR="$BUILD"
    export QCI_RUNS_DIR="$T/runs"
    unset QDISTRO_IMAGE QCI_RELEASE
}

teardown() {
    rm -rf "$T"
}

# stamp <SOURCE lines...> -- write the image's /etc/qdistro/release
stamp() {
    {
        echo 'VERSION=0.1.0'
        echo 'SNAPSHOT=20260902'
        echo 'PROFILE=dev'
        echo 'ARTIFACT=qdistro-0.1.0-20260902.raw.xz'
        printf '%s\n' "$@"
    } > "$BUILD/extracted/etc/qdistro/release"
}

run_gate() {
    run "$REPO/ci/bin/qci" image --no-boot
    ROW=$(awk -F'\t' '$1=="image" && $2=="verify-contents"' "$T"/runs/*/results.tsv)
    echo "status=$status row=$ROW" >&2
}

@test "image gate: a bundle built from a non-ancestor commit is BLOCKED naming both SHAs, not a fail" {
    stamp "SOURCE qdistro $SIDE clean"
    run_gate
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$ROW" | cut -f3)" = blocked ]
    [[ "$ROW" == *"not an ancestor"* ]]
    [[ "$ROW" == *"$SIDE"* ]]
    [[ "$ROW" == *"$HEAD_SHA"* ]]
    [[ "$ROW" == *"image/build-in-vm.sh"* ]]
    [[ "$ROW" == *"QDISTRO_BUILD_DIR"* ]]
    # The checker did not run against the unrelated tree.
    [ ! -e "$T"/runs/*/host/image-verify-contents.log ]
}

@test "image gate: a SHA that is not a commit in this repo is BLOCKED (other clone / unfetched branch)" {
    stamp "SOURCE qdistro 0123456789abcdef0123456789abcdef01234567 clean"
    run_gate
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$ROW" | cut -f3)" = blocked ]
    [[ "$ROW" == *"0123456789abcdef0123456789abcdef01234567"* ]]
    [[ "$ROW" == *"not a commit in the tree under test"* ]]
    [[ "$ROW" == *"$HEAD_SHA"* ]]
}

@test "image gate: a pre-monorepo sibling-layout stamp (several SOURCE lines) is BLOCKED even though its qdistro SHA is an ancestor" {
    # The real 2026-09-10 bundle: its qdistro SHA IS in the monorepo history,
    # so ancestry alone would have let it through to a fail.
    stamp "SOURCE qdistro $OLD clean" \
          "SOURCE qdwin 815c2876e58a7b1668c0e03a78bdc3e3d62ebf42 clean" \
          "SOURCE qdshell 43e4574511105ae188c961aca2380db8184d5286 clean"
    run_gate
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$ROW" | cut -f3)" = blocked ]
    [[ "$ROW" == *"3 SOURCE line(s)"* ]]
    [[ "$ROW" == *"sibling-layout"* ]]
}

@test "image gate: a missing /etc/qdistro/release is BLOCKED (source unknown), not a fail" {
    run_gate
    [ "$status" -eq 0 ]
    [ "$(printf '%s\n' "$ROW" | cut -f3)" = blocked ]
    [[ "$ROW" == *"image source unknown"* ]]
    [[ "$ROW" == *"missing or unreadable"* ]]
}

@test "image gate: an ancestor build still runs the checklist (a real failure stays a fail)" {
    stamp "SOURCE qdistro $OLD clean"
    run_gate
    # The fake tree fails the checklist: that verdict must still surface.
    [ "$status" -eq 20 ]
    [ "$(printf '%s\n' "$ROW" | cut -f3)" = fail ]
    [ -s "$(echo "$T"/runs/*/host/image-verify-contents.log)" ]
    # Allowed, but recorded and warned with the distance.
    grep -qx "image_source_relation=ancestor" "$T"/runs/*/manifest.txt
    grep -qx "image_source_behind=1" "$T"/runs/*/manifest.txt
    [[ "$output" == *"WARNING bundle built from $OLD (clean), an ancestor 1 commit(s) behind HEAD $HEAD_SHA"* ]]
}

@test "image gate: a bundle built from HEAD itself (DIRTY) runs the checklist" {
    stamp "SOURCE qdistro $HEAD_SHA DIRTY diff-sha256=0123456789abcdef untracked=0"
    run_gate
    [ "$status" -eq 20 ]
    [ "$(printf '%s\n' "$ROW" | cut -f3)" = fail ]
    grep -qx "image_source_relation=exact" "$T"/runs/*/manifest.txt
    grep -qx "image_source_state=DIRTY" "$T"/runs/*/manifest.txt
}

@test "image gate: an explicit --root is inspected as given (no source check)" {
    stamp "SOURCE qdistro $SIDE clean"
    run "$REPO/ci/bin/qci" image --no-boot --root "$BUILD/extracted"
    ROW=$(awk -F'\t' '$1=="image" && $2=="verify-contents"' "$T"/runs/*/results.tsv)
    [ "$status" -eq 20 ]
    [ "$(printf '%s\n' "$ROW" | cut -f3)" = fail ]
}
