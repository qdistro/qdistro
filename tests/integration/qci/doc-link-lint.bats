#!/usr/bin/env bats

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    SCRIPT="$REPO_ROOT/ci/bin/doc-link-lint.py"
    TMP="$(mktemp -d)"
    FIXTURE="$TMP/repo"
    mkdir -p "$FIXTURE/doc" "$FIXTURE/ci"
    printf '# Fixture\n' > "$FIXTURE/README.md"
}

teardown() { rm -rf "$TMP"; }

run_lint() { run python3 "$SCRIPT" --root "$FIXTURE"; }

@test "doc-link-lint: accepts local files, generated headings, and explicit anchors" {
    cat > "$FIXTURE/doc/target.md" <<'EOF'
# Generated Heading

<a id="stable-contract"></a>
EOF
    cat > "$FIXTURE/doc/source.md" <<'EOF'
[file](target.md)
[heading](target.md#generated-heading)
[explicit](target.md#stable-contract)
EOF

    run_lint
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
    [[ "$output" == *"0 finding(s)"* ]]
}

@test "doc-link-lint: rejects a missing relative target" {
    printf '[missing](does-not-exist.md)\n' > "$FIXTURE/doc/source.md"

    run_lint
    [ "$status" -eq 1 ]
    [[ "$output" == *"missing local link target"* ]]
    [[ "$output" == *"doc/source.md:1"* ]]
}

@test "doc-link-lint: rejects a missing Markdown heading anchor" {
    printf '# Present heading\n' > "$FIXTURE/doc/target.md"
    printf '[wrong](target.md#absent-heading)\n' > "$FIXTURE/doc/source.md"

    run_lint
    [ "$status" -eq 1 ]
    [[ "$output" == *"missing Markdown anchor #absent-heading"* ]]
}

@test "doc-link-lint: ignores external URLs and links inside fenced examples" {
    cat > "$FIXTURE/doc/source.md" <<'EOF'
[external](https://example.invalid/missing)

```markdown
[example](not-a-real-file.md)
```
EOF

    run_lint
    [ "$status" -eq 0 ] || { echo "$output" >&2; return 1; }
}
