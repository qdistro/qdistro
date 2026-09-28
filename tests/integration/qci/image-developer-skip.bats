#!/usr/bin/env bats
# Host-only contracts for the developer full-run image omission.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    T="$BATS_TEST_TMPDIR"
    mkdir -p "$T/build/bundle" "$T/runs"
    export QCI_RUNS_DIR="$T/runs"
}

skip_identity() {
    IMAGE_DIR="$REPO/image" QDISTRO_BUILD_DIR="$T/build" bash -c '
        source "$1/ci/lib/gates/image.sh"
        kv() { printf "%s=%s\n" "$1" "$2"; }
        record_skip() { printf "row=%s/%s:%s\n" "$1" "$2" "$4"; }
        gate_image_developer_skip
    ' _ "$REPO"
}

@test "release full refuses image skip before it creates a run or VM" {
    # The timeout bounds a regressed guard: a real release full run would
    # otherwise start on the shared host instead of failing this test.
    run timeout 60 env QCI_SKIP_IMAGE=1 QCI_RELEASE=1 "$REPO/ci/bin/qci" full
    [ "$status" -eq 2 ]
    [[ "$output" == *"forbidden with QCI_RELEASE=1"* ]]
    [ -z "$(find "$T/runs" -mindepth 1 -print -quit)" ]
}

@test "developer image skip reads the selected xz sidecar without hashing or decompressing" {
    local xz="$T/build/bundle/qdistro-test.raw.xz"
    printf 'not an xz stream' > "$xz"
    printf '%064d  %s\n' 1 "$(basename "$xz")" > "$xz.sha256"
    run skip_identity
    [ "$status" -eq 0 ]
    [[ "$output" == *"image_gate=skipped"* ]]
    [[ "$output" == *"image_published=$xz"* ]]
    [[ "$output" == *"image_digest=$(printf '%064d' 1)"* ]]
    [[ "$output" == *"row=image/developer-omission:"* ]]
    [[ "$output" == *"were not run"* ]]
}

@test "developer skip says none when no published artifact or sidecar exists" {
    run skip_identity
    [ "$status" -eq 0 ]
    [[ "$output" == *"image_published=none"* ]]
    [[ "$output" == *"image_digest=none"* ]]
    local xz="$T/build/bundle/qdistro-test.raw.xz"
    : > "$xz"
    run skip_identity
    [[ "$output" == *"image_published=$xz"* ]]
    [[ "$output" == *"image_digest=none"* ]]
}

@test "developer skip honors the requested digest when several bundles exist" {
    local chosen="$T/build/bundle/chosen.raw.xz" other="$T/build/bundle/other.raw.xz"
    : > "$chosen"; : > "$other"
    printf '%064d  %s\n' 1 "$(basename "$chosen")" > "$chosen.sha256"
    printf '%064d  %s\n' 2 "$(basename "$other")" > "$other.sha256"
    QDISTRO_IMAGE_SHA256="$(printf '%064d' 2)" run skip_identity
    [ "$status" -eq 0 ]
    [[ "$output" == *"image_published=$other"* ]]
    [[ "$output" == *"image_digest=$(printf '%064d' 2)"* ]]
}

@test "developer skip marks path and digest selector conflicts as unavailable" {
    local xz="$T/build/bundle/chosen.raw.xz" a b
    a="$(printf '%064d' 1)"; b="$(printf '%064d' 2)"
    : > "$xz"
    printf '%s  %s\n' "$a" "$(basename "$xz")" > "$xz.sha256"
    QDISTRO_IMAGE="$xz" QDISTRO_IMAGE_SHA256="$b" run skip_identity
    [ "$status" -eq 0 ]
    [[ "$output" == *"image_published=none"* ]]
    [[ "$output" == *"image_digest=none"* ]]
    [[ "$output" == *"image_identity_status=selector-conflict"* ]]
    QDISTRO_IMAGE="$a" QDISTRO_IMAGE_SHA256="$b" run skip_identity
    [[ "$output" == *"image_identity_status=selector-conflict"* ]]
    [[ "$output" == *"image_published=none"* ]]
}

@test "developer skip distinguishes missing and malformed sidecars" {
    local xz="$T/build/bundle/chosen.raw.xz"
    : > "$xz"
    run skip_identity
    [[ "$output" == *"image_identity_status=missing-sidecar"* ]]
    [[ "$output" == *"image_digest=none"* ]]
    printf 'garbage  chosen.raw.xz\n' > "$xz.sha256"
    run skip_identity
    [[ "$output" == *"image_identity_status=malformed-sidecar"* ]]
    [[ "$output" == *"image_digest=none"* ]]
    printf '%064d  wrong.raw.xz\n' 1 > "$xz.sha256"
    run skip_identity
    [[ "$output" == *"image_identity_status=malformed-sidecar"* ]]
}

@test "full dispatcher calls one skip and no image gate; default still calls image" {
    run bash -c '
        source "$1/ci/lib/dispatch.sh"
        EXIT_OK=0 EXIT_VM_PROVISION=40 QCI_SKIP_IMAGE=1
        qci_assert_run_dir() { return 0; }
        gate_image_developer_skip() { echo image-skip; }
        gate_preflight() { echo preflight; }
        gate_host() { echo host; }
        gate_release_manifest() { echo release-manifest; }
        gate_bootstrap_release_profile() { echo bootstrap-release-profile; }
        gate_image() { echo image-gate; }
        gate_vm_smoke() { echo vm-smoke; }
        gate_bats() { echo bats; }
        gate_gui() { echo gui; }
        gate_full
    ' _ "$REPO"
    [ "$status" -eq 0 ]
    [[ "$output" == *"image-skip"* ]]
    [[ "$output" != *"image-gate"* ]]
    [[ "$output" == *"vm-smoke"* ]]
    run bash -c '
        source "$1/ci/lib/dispatch.sh"
        EXIT_OK=0 EXIT_VM_PROVISION=40 QCI_SKIP_IMAGE=0
        qci_assert_run_dir() { return 0; }
        gate_preflight() { :; }; gate_host() { :; }
        gate_release_manifest() { :; }; gate_bootstrap_release_profile() { :; }
        gate_image() { echo image-gate; }
        gate_vm_smoke() { :; }; gate_bats() { :; }; gate_gui() { :; }
        gate_full
    ' _ "$REPO"
    [ "$status" -eq 0 ]
    [ "$output" = image-gate ]
}

@test "report Summary names the omission, artifact and digest" {
    local r="$T/report"
    mkdir -p "$r"
    printf 'run_id=test\ngate=full\nimage_gate=skipped\nimage_published=/tmp/test.raw.xz\nimage_digest=%064d\n' 1 > "$r/manifest.txt"
    printf 'gate\tsubject\tstatus\texit_code\texit_class\tkind\tlog\tnotes\tcategory\nimage\tdeveloper-omission\tskip\t0\tpass\timage\t\tdeveloper skip\t\n' > "$r/results.tsv"
    run python3 "$REPO/ci/lib/report.py" "$r"
    [ "$status" -eq 0 ]
    grep -q '^## Summary$' "$r/report.md"
    grep -q '^\- \*\*Scope\*\*: Image gate skipped by QCI_SKIP_IMAGE=1;' "$r/report.md"
    grep -q '/tmp/test.raw.xz' "$r/report.md"
    grep -q "$(printf '%064d' 1)" "$r/report.md"
    grep -q 'not full image qualification or P8 green full evidence' "$r/report.md"
}

@test "developer skip records a symlinked image by its target and the target's sidecar" {
    local real="$T/build/bundle/real.raw.xz" alias="$T/alias.raw.xz"
    : > "$real"
    printf '%064d  %s\n' 3 "$(basename "$real")" > "$real.sha256"
    ln -s "$real" "$alias"
    printf '%064d  %s\n' 4 "$(basename "$alias")" > "$alias.sha256"
    QDISTRO_IMAGE="$alias" run skip_identity
    [ "$status" -eq 0 ]
    [[ "$output" == *"image_published=$real"* ]]
    [[ "$output" == *"image_digest=$(printf '%064d' 3)"* ]]
}
