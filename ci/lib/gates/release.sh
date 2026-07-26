#!/usr/bin/env bash
# qci module: release-manifest + bootstrap-release-profile gates
# Extracted verbatim from bin/qci. SOURCED by bin/qci into the single
# CI-runner process (shared RDIR/CREATED_VMS/golden state/traps); it is
# NOT executed standalone. See ci/AGENTS.md for the module map.
# shellcheck shell=bash

# ---------------------------------------------------------------------------
# qci release-manifest gate (R1; 03-release-engineering.md + 05-agent-test-plan
# §A "release-manifest gate"). Asserts the source manifest is release-grade
# BEFORE an RC is cut: the signed manifest verifies, every listed repo checkout
# is at exactly its pinned commit (+tag if recorded) with a CLEAN tree, and any
# recorded release tags agree on one version. READ-ONLY — unlike the bootstrap's
# verify_repo_pin (which checks out the pin), this gate never mutates a checkout;
# it asserts the state a release build would consume.
#
# An UNPOPULATED manifest (all lines commented — the shipped default) is recorded
# `blocked`, NOT failed: a dev host legitimately has no release pins yet. The
# release-profile battery (QCI_RELEASE, 05 §A) is what makes such a blocked row
# fatal at RC. The signature sub-check is likewise `blocked` when no keyring/sig
# is provided, since the v1 release key is not published yet. A POPULATED
# manifest whose repos diverge (wrong commit, dirty tree, moved tag) is a hard
# FAIL (EXIT_RELEASE) — that is tamper/drift evidence, not a missing prereq.
#
# Inputs (all optional; sensible defaults):
#   QDISTRO_RELEASE_MANIFEST  manifest file (default scripts/install/source-manifest.txt)
#   QDISTRO_REPO_ROOT         dir holding the sibling repo checkouts (default: parent of the qdistro repo)
#   QDISTRO_RELEASE_KEYRING   gpgv keyring -> enables the signature sub-check
#   QDISTRO_MANIFEST_SIG      detached signature over the manifest
#   QDISTRO_RELEASE_SIGNER    expected 40-hex signer fingerprint (bound authoritatively)
# ---------------------------------------------------------------------------
gate_release_manifest() {
    qci_assert_run_dir || return $?
    local log_path="$RDIR/release-manifest/gate.log"
    mkdir -p "$(dirname "$log_path")"
    : > "$log_path"

    # Manifest path + detached signature: accept the bootstrap's env names as
    # aliases and default the signature to the manifest's adjacent `.sig`, so a
    # release host configured exactly like qdistro-bootstrap.sh is understood.
    local manifest="${QDISTRO_RELEASE_MANIFEST:-${QDISTRO_SOURCE_MANIFEST:-$QDISTRO_REPO/scripts/install/source-manifest.txt}}"
    local repo_root="${QDISTRO_REPO_ROOT:-$(dirname "$QDISTRO_REPO")}"
    local gen="$QDISTRO_REPO/scripts/install/gen-source-manifest.sh"
    local verify="$QDISTRO_REPO/scripts/install/verify-source-manifest.sh"
    # Release-grade completeness: the bootstrap's fatal fetch set must be pinned
    # (scripts/install/qdistro-bootstrap.sh: `for repo in qdistro qdwin qdshell`).
    local CORE_REPOS="qdistro qdwin qdshell"
    # The two extension repos are source-only optional fetches (R4): nothing is
    # built from them, but a v1 user hand-builds the extension they load out of
    # that checkout, so an unpinned extension repo is worth the same advisory
    # WARN as any other optional repo.
    local OPTIONAL_REPOS="qdlocker qdbrowser qdgreeter qterminator qnotebook qfileman qdchrome-extension qdfirefox-extension"

    {
        echo "## release-manifest gate (R1)"
        echo "manifest:  $manifest"
        echo "repo_root: $repo_root"
        echo
    } >> "$log_path"

    if [ ! -f "$manifest" ]; then
        record_blocked release-manifest manifest "$EXIT_RELEASE" release "manifest file not found: $manifest" "$log_path"
        log "release-manifest: no manifest at $manifest"
        return "$EXIT_OK"
    fi

    # Snapshot the manifest into the run dir and operate ONLY on the snapshot —
    # lint, signature-verify, and pin-parse all read the same captured bytes, so
    # a concurrent edit to the source file cannot make the signature pass while
    # the pin checks read different content (TOCTOU; mirrors the bootstrap's
    # copy-then-verify in verify_manifest_signature).
    local snap="$RDIR/release-manifest/manifest.snapshot"
    cp "$manifest" "$snap" 2>>"$log_path" || {
        record_result release-manifest snapshot fail "$EXIT_RELEASE" release release "$log_path" "could not snapshot manifest $manifest"
        log "release-manifest: snapshot failed"
        return "$EXIT_RELEASE"
    }
    echo "snapshot:  $snap" >> "$log_path"

    # Active lines = non-blank, non-comment. The shipped manifest is all comments
    # (unpopulated) until release tooling fills in the real pins.
    local active
    active=$(awk '
        /^[[:space:]]*#/ {next}
        /^[[:space:]]*$/ {next}
        {print}
    ' "$snap")

    if [ -z "$active" ]; then
        {
            echo "manifest is UNPOPULATED (no active <repo> <sha> lines)."
            echo "A dev host has no release pins yet; populate + sign for an RC."
        } >> "$log_path"
        record_blocked release-manifest unpopulated "$EXIT_RELEASE" release \
            "manifest unpopulated — no release pins (populate + sign for RC)" "$log_path"
        log "release-manifest: manifest unpopulated (blocked, not fatal on dev host)"
        return "$EXIT_OK"
    fi

    local fail=0

    # (1) Format lint — the manifest must be exactly the bootstrap pin grammar.
    #     On a POPULATED manifest a missing/non-executable linter is a tooling-
    #     integrity FAILURE, not a missing release prerequisite — fail closed
    #     (the bootstrap likewise `die`s when verification tooling is absent).
    if [ ! -x "$gen" ]; then
        record_result release-manifest lint fail "$EXIT_RELEASE" release release "$log_path" "linter not executable: $gen"
        fail=1
    elif "$gen" --lint "$snap" >> "$log_path" 2>&1; then
        record_result release-manifest lint pass 0 pass release "$log_path" "manifest format valid"
    else
        record_result release-manifest lint fail "$EXIT_RELEASE" release release "$log_path" "manifest failed gen-source-manifest --lint"
        fail=1
    fi

    # (2) Signature — gpgv + authoritative-signer binding. `blocked` ONLY when no
    #     keyring is supplied (dev host: the v1 key is unpublished). Once a
    #     keyring IS supplied, verification was requested: a missing signature, a
    #     missing/non-executable verifier, or a failed verify are all hard FAILs.
    local keyring="${QDISTRO_RELEASE_KEYRING:-}"
    local sig="${QDISTRO_MANIFEST_SIG:-${QDISTRO_SOURCE_MANIFEST_SIG:-$manifest.sig}}"
    local signer="${QDISTRO_RELEASE_SIGNER:-}"
    if [ -z "$keyring" ]; then
        record_blocked release-manifest signature "$EXIT_RELEASE" release \
            "no keyring provided (QDISTRO_RELEASE_KEYRING) — v1 key not published yet" "$log_path"
    elif [ ! -x "$verify" ]; then
        record_result release-manifest signature fail "$EXIT_RELEASE" release release "$log_path" "verifier not executable: $verify"
        fail=1
    elif [ ! -f "$sig" ]; then
        record_result release-manifest signature fail "$EXIT_RELEASE" release release "$log_path" "keyring supplied but signature file missing: $sig"
        fail=1
    else
        # Verify the SNAPSHOT bytes (== the bytes the pin checks parse). The
        # detached sig was made over the identical source bytes, so it validates.
        local vrc=0
        if [ -n "$signer" ]; then
            "$verify" "$snap" "$sig" "$keyring" "$signer" >> "$log_path" 2>&1 || vrc=$?
        else
            "$verify" "$snap" "$sig" "$keyring" >> "$log_path" 2>&1 || vrc=$?
        fi
        if [ "$vrc" -eq 0 ]; then
            record_result release-manifest signature pass 0 pass release "$log_path" "signature verifies${signer:+ (signer bound)}"
        else
            record_result release-manifest signature fail "$EXIT_RELEASE" release release "$log_path" "signature verification failed (rc=$vrc)"
            fail=1
        fi
    fi

    # (3) Per-repo pin + clean tree, and (4) version consistency across tags.
    #     READ-ONLY mirror of bootstrap verify_repo_pin invariants (NO checkout).
    local repo pin tag versions="" pinned_repos="" line
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        repo=$(printf '%s\n' "$line" | awk '{print $1}')
        pin=$(printf '%s\n' "$line" | awk '{print $2}')
        tag=$(printf '%s\n' "$line" | awk '{
            for (i=3;i<=NF;i++){e=index($i,"="); if(e>1 && substr($i,1,e-1)=="tag"){print substr($i,e+1); exit}}
        }')
        pinned_repos="$pinned_repos $repo"
        local dir="$repo_root/$repo" ok=1 detail=""
        if ! printf '%s' "$pin" | grep -qE '^[0-9a-f]{40}$'; then
            ok=0; detail="pin '$pin' is not a 40-hex commit SHA"
        elif [ ! -d "$dir/.git" ]; then
            ok=0; detail="no git checkout at $dir"
        else
            local head st_out st_rc
            head=$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)
            [ "$head" = "$pin" ] || { ok=0; detail="HEAD ($head) != pinned $pin"; }
            # Capture status + rc separately: a `git status | wc -l` pipe hides a
            # status failure as 0 lines (== clean), so an unreadable/corrupt tree
            # would fail OPEN. An errored status is itself a hard release failure.
            st_out=$(git -C "$dir" status --porcelain 2>>"$log_path"); st_rc=$?
            if [ "$st_rc" -ne 0 ]; then
                ok=0; detail="${detail:+$detail; }git status failed (rc=$st_rc — unreadable tree)"
            elif [ -n "$st_out" ]; then
                ok=0; detail="${detail:+$detail; }working tree not clean ($(printf '%s\n' "$st_out" | grep -c .) changed paths)"
            fi
            if [ -n "$tag" ]; then
                if ! printf '%s' "$tag" | grep -qE '^[A-Za-z0-9][A-Za-z0-9._/+-]*$' || [ "${tag#*..}" != "$tag" ]; then
                    ok=0; detail="${detail:+$detail; }unsafe tag name '$tag'"
                else
                    local tag_commit
                    tag_commit=$(git -C "$dir" rev-parse -q --verify "refs/tags/$tag^{commit}" 2>/dev/null || true)
                    if [ -z "$tag_commit" ]; then
                        detail="${detail:+$detail; }tag '$tag' absent (warn; commit pin enforced)"
                        printf 'WARN  %s: tag %s absent from checkout\n' "$repo" "$tag" >> "$log_path"
                    elif [ "$tag_commit" != "$pin" ]; then
                        ok=0; detail="${detail:+$detail; }tag '$tag' -> $tag_commit != pin $pin (tamper/moved tag)"
                    fi
                fi
            fi
        fi
        # Version-consistency core: extract the semver core (e.g. v1.0.0 / V1.0 /
        # qdwin-1.2.3 -> 1.0.0 / 1.0 / 1.2.3) so equal releases compare equal
        # regardless of a v/V prefix or repo namespacing; a tag with no semver
        # core falls back to its raw value so an odd tag still surfaces a mismatch.
        if [ -n "$tag" ]; then
            local core
            core=$(printf '%s' "$tag" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -1)
            [ -n "$core" ] || core="$tag"
            versions="${versions}${core}"$'\n'
        fi
        if [ "$ok" -eq 1 ]; then
            record_result release-manifest "pin:$repo" pass 0 pass release "$log_path" "at pinned $pin${tag:+ (tag $tag)}"
            printf 'OK    %s @ %s%s\n' "$repo" "$pin" "${tag:+ tag=$tag}" >> "$log_path"
        else
            record_result release-manifest "pin:$repo" fail "$EXIT_RELEASE" release release "$log_path" "$detail"
            printf 'FAIL  %s: %s\n' "$repo" "$detail" >> "$log_path"
            fail=1
        fi
    done <<EOF
$active
EOF

    # (3b) Completeness: a release-grade manifest must pin the bootstrap's fatal
    #      fetch set. Missing core repo => hard FAIL (the build would not be
    #      release-grade); a missing OPTIONAL repo is advisory (bootstrap treats
    #      those as non-fatal).
    local r
    for r in $CORE_REPOS; do
        case " $pinned_repos " in
            *" $r "*) ;;
            *) record_result release-manifest "completeness:$r" fail "$EXIT_RELEASE" release release "$log_path" "required core repo '$r' is not pinned in the manifest"
               printf 'FAIL  completeness: core repo %s not pinned\n' "$r" >> "$log_path"
               fail=1 ;;
        esac
    done
    for r in $OPTIONAL_REPOS; do
        case " $pinned_repos " in
            *" $r "*) ;;
            *) printf 'WARN  completeness: optional repo %s not pinned\n' "$r" >> "$log_path" ;;
        esac
    done

    # (4) version consistency: every recorded tag must map to ONE semver core.
    local vcount
    vcount=$(printf '%s' "$versions" | sed '/^$/d' | wc -l | tr -d ' ')
    if [ "${vcount:-0}" -gt 0 ]; then
        local distinct
        distinct=$(printf '%s\n' "$versions" | sed '/^$/d' | sort -u | wc -l | tr -d ' ')
        if [ "$distinct" -eq 1 ]; then
            record_result release-manifest version-consistency pass 0 pass release "$log_path" "all $vcount tags agree on one version"
        else
            record_result release-manifest version-consistency fail "$EXIT_RELEASE" release release "$log_path" "$distinct distinct versions across manifest tags"
            printf 'FAIL  version-consistency: %s distinct tag versions\n' "$distinct" >> "$log_path"
            fail=1
        fi
    fi

    log "release-manifest: $([ "$fail" -eq 0 ] && echo PASS || echo FAIL)"
    [ "$fail" -eq 0 ] && return "$EXIT_OK"
    return "$EXIT_RELEASE"
}

# Minimum non-skipped passing tests required from each SHIPPED bootstrap
# release-contract suite — an erosion guard so a 32-test contract suite cannot be
# silently gutted to a stub and still pass the gate. Tracks the current suites;
# bump alongside intentional test removals. Unknown names (synthetic test
# fixtures, or an operator-narrowed list) require only >=1 real pass.
release_contract_floor() {
    case "$1" in
        bootstrap-hardening.bats)        echo 32 ;;
        source-manifest-signature.bats)  echo 18 ;;
        gen-source-manifest.bats)        echo 32 ;;
        *)                               echo 1 ;;
    esac
}

# ---------------------------------------------------------------------------
# qci bootstrap-release-profile gate (R2; 05-agent-test-plan.md §A
# "bootstrap-release-profile gate"). Enforces the hardened/release bootstrap
# contract — no `--no-gpg-checks`, no `NOPASSWD: ALL` outside the dev profile,
# and source checkouts pinned + signature-verified BEFORE any root clone/build.
# Those invariants are already pinned by host-STATIC bats (no VM): this gate runs
# them HOST-ONLY so the release battery enforces the bootstrap contract without
# B1, distinct from the VM-bound `qci bats` lane. A `not ok`, a non-zero bats
# exit, OR a vacuous (0-test) run is a hard FAIL — a renamed/emptied contract
# suite must not pass silently.
# ---------------------------------------------------------------------------
gate_bootstrap_release_profile() {
    qci_assert_run_dir || return $?
    local log_path="$RDIR/bootstrap-release-profile/gate.log"
    mkdir -p "$(dirname "$log_path")"
    : > "$log_path"

    # The host-static bats that pin the bootstrap release contract. Dir + list
    # are env-overridable (tests point them at fixtures; an operator could narrow
    # the set), defaulting to the shipped release-contract suites.
    local bdir="${QCI_BOOTSTRAP_RELEASE_BATS_DIR:-$QDISTRO_REPO/tests/integration/vm}"
    local files="${QCI_BOOTSTRAP_RELEASE_BATS:-bootstrap-hardening.bats source-manifest-signature.bats gen-source-manifest.bats}"

    if ! command -v bats >/dev/null 2>&1; then
        record_blocked bootstrap-release-profile bats "$EXIT_PREFLIGHT" bats "bats executable not found" "$log_path"
        log "bootstrap-release-profile: bats not found"
        return "$EXIT_OK"
    fi

    local fail=0 seen=0 f path out brc plan npass nfail nskip nreal floor
    for f in $files; do
        seen=$((seen + 1))
        path="$bdir/$f"
        if [ ! -f "$path" ]; then
            record_result bootstrap-release-profile "$f" fail "$EXIT_RELEASE" release bats "$log_path" "release-contract bats missing: $path"
            printf 'FAIL  %s: file missing\n' "$f" >> "$log_path"
            fail=1; continue
        fi
        { echo; echo "## $f"; } >> "$log_path"
        out=$(VM_NAME="${VM_NAME:-qci-bootstrap-release}" bats --tap "$path" 2>>"$log_path"); brc=$?
        printf '%s\n' "$out" >> "$log_path"
        # TAP: a passing line is `ok N ...`; a skipped one is `ok N ... # skip`;
        # a failure is `not ok N ...`; the plan is `1..N`. Skips do NOT count as
        # verified passes for a RELEASE CONTRACT — a skipped assertion is not run.
        plan=$(printf '%s\n' "$out" | sed -n 's/^1\.\.\([0-9][0-9]*\)$/\1/p' | head -1)
        npass=$(printf '%s\n' "$out" | grep -cE '^ok ')
        nfail=$(printf '%s\n' "$out" | grep -cE '^not ok ')
        nskip=$(printf '%s\n' "$out" | grep -ciE '^ok [0-9]+.*# *skip')
        nreal=$((npass - nskip))
        floor=$(release_contract_floor "$f")
        if [ "$nfail" -ne 0 ]; then
            record_result bootstrap-release-profile "$f" fail "$EXIT_RELEASE" release bats "$log_path" "$nfail failing / $npass passing"
            fail=1
        elif [ "$brc" -ne 0 ]; then
            record_result bootstrap-release-profile "$f" fail "$EXIT_RELEASE" release bats "$log_path" "bats exited $brc with no parsed failures (crash/error?)"
            fail=1
        elif [ -z "$plan" ] || [ "$plan" -ne "$((npass + nfail))" ]; then
            # No plan, or fewer reported results than declared => a partial run or
            # a crash mid-suite. A release contract must run to completion.
            record_result bootstrap-release-profile "$f" fail "$EXIT_RELEASE" release bats "$log_path" "TAP plan '${plan:-none}' != reported $((npass + nfail)) (partial run / crash?)"
            fail=1
        elif [ "$nskip" -ne 0 ]; then
            record_result bootstrap-release-profile "$f" fail "$EXIT_RELEASE" release bats "$log_path" "$nskip skipped contract test(s) — a release contract must RUN, not skip"
            fail=1
        elif [ "$nreal" -lt "$floor" ]; then
            # Erosion guard: a shipped contract suite gutted down to a stub
            # (e.g. 32 tests -> 1) would otherwise pass. Pin a per-suite floor.
            record_result bootstrap-release-profile "$f" fail "$EXIT_RELEASE" release bats "$log_path" "$nreal passing < floor $floor — contract suite eroded (or bump the floor in release_contract_floor)"
            fail=1
        else
            record_result bootstrap-release-profile "$f" pass 0 pass bats "$log_path" "$nreal passing (>= floor $floor), 0 failing/skipped"
            printf 'OK    %s: %s passing (floor %s)\n' "$f" "$nreal" "$floor" >> "$log_path"
        fi
    done

    if [ "$seen" -eq 0 ]; then
        record_result bootstrap-release-profile suites fail "$EXIT_RELEASE" release bats "$log_path" "no contract suites selected (empty/whitespace QCI_BOOTSTRAP_RELEASE_BATS)"
        fail=1
    fi

    log "bootstrap-release-profile: $([ "$fail" -eq 0 ] && echo PASS || echo FAIL)"
    [ "$fail" -eq 0 ] && return "$EXIT_OK"
    return "$EXIT_RELEASE"
}
