# shellcheck shell=bash
# deps-cache.sh — the qdistro local dependency cache. Content-addressed
# storage for every artifact a build or test lane would otherwise fetch from
# the network: cloud qcow2s, OCI archives, RPM repo mirrors, ISOs, tarballs,
# git bundles, misc blobs.
#
# Replaces version pinning as the reproducibility mechanism: a populated
# cache IS the source of truth; upstream is a cold-warm source only. An
# empty cache folder is the definition of "clean build from scratch".
# Design + fetch-surface inventory: todo/devin-status-10-10/13 (tracker).
#
# Layout under the root (default $QDWIN_CACHE_DIR or ~/.cache/qdistro):
#
#   MANIFEST                         schema=1, created, last_warm
#   <class>/<sha256>/<basename>      the artifact, content-keyed
#   <class>/<sha256>/manifest.txt    url=, sha256=, basename=, fetched_at=, class=
#   <class>/<sha256>/<basename>.*    sidecars (.sha256, .sha256.asc, ...)
#   .locks/<class>-<sha256>.lock     per-entry flock
#   .partial/                        tmp space; atomic mv on promote
#
# Classes are caller-defined directory names (cloud, oci, agama, openwrt,
# rpm, repo, python, sources, blob, ...). Content key is ALWAYS the sha256
# of the artifact bytes; a wrong-digest download is never promoted.
#
# Offline contract: QDISTRO_DEPS_OFFLINE=1 or QCI_OFFLINE=1 forbids network.
# A missing artifact fails loudly naming class+key — no silent fallback.
#
# Sourced, not executed:
#   . "<lib>/deps-cache.sh"
#   path="$(qd_fetch cloud "$CLOUD_URL" "$CLOUD_SHA256")" || exit 1

_qdc_die() { echo "ERROR: $*" >&2; return 1; }

# ---- root ---------------------------------------------------------------

# Cache root: explicit override wins, then the established qdistro cache
# root (shared with the rpm/npm/baseweed caches), then the XDG default.
qd_deps_root() {
    printf '%s\n' "${QDISTRO_DEPS_CACHE_DIR:-${QDWIN_CACHE_DIR:-$HOME/.cache/qdistro}}"
}

qd_deps_offline() {
    [ "${QDISTRO_DEPS_OFFLINE:-0}" = 1 ] || [ "${QCI_OFFLINE:-0}" = 1 ]
}

# Write/refresh the root MANIFEST (schema marker; cheap, idempotent).
qd_deps_init() {
    local root
    root="$(qd_deps_root)"
    install -d "$root" "$root/.locks" "$root/.partial" || return 1
    if [ ! -s "$root/MANIFEST" ]; then
        printf 'schema=1\ncreated=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$root/MANIFEST" || return 1
    fi
}

# ---- path resolution ----------------------------------------------------

# qd_path <class> <sha256> [basename]
# Print the canonical cache path for an artifact. With no basename, resolves
# to the entry directory (which also holds manifest.txt and sidecars).
qd_path() {
    local class="$1" key="$2" base="${3:-}"
    case "$class" in *[!a-z0-9._-]*|'') _qdc_die "qd_path: bad class '$class'"; return 1 ;; esac
    [[ "$key" =~ ^[0-9a-f]{64}$ ]] || { _qdc_die "qd_path: bad sha256 key '$key'"; return 1; }
    case "$base" in
        '') printf '%s/%s/%s\n' "$(qd_deps_root)" "$class" "$key" ;;
        */*|'..'*) _qdc_die "qd_path: bad basename '$base'"; return 1 ;;
        *) printf '%s/%s/%s/%s\n' "$(qd_deps_root)" "$class" "$key" "$base" ;;
    esac
}

# qd_has <class> <sha256> [basename] — 0 iff the artifact file is present
# and non-empty. With no basename, true iff the entry dir holds at least one
# artifact file (anything that is not manifest.txt or a dotfile).
qd_has() {
    local class="$1" key="$2" base="${3:-}" dir f
    dir="$(qd_path "$class" "$key")" || return 1
    if [ -n "$base" ]; then
        [ -s "$dir/$base" ]; return
    fi
    [ -d "$dir" ] || return 1
    for f in "$dir"/*; do
        [ -f "$f" ] || continue
        case "$(basename "$f")" in manifest.txt|.*) continue ;; esac
        [ -s "$f" ] && return 0
    done
    return 1
}

# qd_entry_name <class> <sha256> — print the stored artifact's basename
# (first non-manifest, non-dotfile regular file in the entry dir).
qd_entry_name() {
    local class="$1" key="$2" dir f
    dir="$(qd_path "$class" "$key")" || return 1
    for f in "$dir"/*; do
        [ -f "$f" ] || continue
        case "$(basename "$f")" in manifest.txt|.*) continue ;; esac
        basename "$f"; return 0
    done
    _qdc_die "qd_entry_name: no artifact under $dir"; return 1
}

# ---- store / verify -----------------------------------------------------

_qdc_write_manifest() {
    local dir="$1" class="$2" url="$3" key="$4" base="$5"
    printf 'class=%s\nurl=%s\nsha256=%s\nbasename=%s\nfetched_at=%s\n' \
        "$class" "$url" "$key" "$base" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > "$dir/manifest.txt" || return 1
}

# qd_store_file <class> <sha256> <srcfile> <url> [basename]
# Verify srcfile's sha256 against the key, then atomically promote it into
# the cache (adopt: mv when possible so a 700 MB image is not copied).
# Used by qd_fetch after download and by migrations adopting a legacy
# cache file. Prints the canonical cache path.
qd_store_file() {
    local class="$1" key="$2" src="$3" url="$4" base dir actual
    base="${5:-$(basename "$src")}"  # separate line: $-expansion in a local
    dir="$(qd_path "$class" "$key" "$base")" || return 1
    dir="$(dirname "$dir")"
    [ -s "$src" ] || { _qdc_die "qd_store_file: empty/missing source: $src"; return 1; }
    actual="$(sha256sum "$src" | awk '{print $1}')" || return 1
    [ "$actual" = "$key" ] || {
        _qdc_die "qd_store_file: digest mismatch for $src (got $actual, want $key); refusing to cache"
        return 1
    }
    install -d "$dir" || return 1
    mv -f "$src" "$dir/$base" 2>/dev/null || { cp -f "$src" "$dir/$base" && rm -f "$src"; } || return 1
    _qdc_write_manifest "$dir" "$class" "$url" "$key" "$base" || return 1
    printf '%s\n' "$dir/$base"
}

# qd_adopt <class> <sha256> <legacy_file> <url>
# One-time migration: if <legacy_file> exists, its entry is missing, and its
# digest matches the key, move it (plus any <file>.sha256/.sha256.asc
# sidecars) into the canonical layout. Never downloads. Returns 0 when there
# is nothing to adopt; a corrupt legacy file is left in place with a
# warning (the caller's normal verified-fetch path then replaces it).
qd_adopt() {
    local class="$1" key="$2" legacy="$3" url="$4" dir base side suffix
    dir="$(qd_path "$class" "$key")" || return 1
    qd_has "$class" "$key" && return 0
    [ -s "$legacy" ] || return 0
    # canonical basename is the UPSTREAM artifact name, not the legacy one —
    # e.g. openSUSE signature verification binds the signed checksum to it
    base="$(basename "${url%%\?*}")"
    if stored="$(qd_store_file "$class" "$key" "$legacy" "$url" "$base")"; then
        for suffix in .sha256 .sha256.asc .asc .sig; do
            [ -s "$legacy$suffix" ] && mv -f "$legacy$suffix" "$dir/$base$suffix"
        done
        echo "[deps-cache] adopted legacy $legacy -> $stored" >&2
        return 0
    fi
    echo "[deps-cache] WARNING: legacy cache $legacy does not match key $key; left in place, will refetch" >&2
    return 0
}

# qd_verify <class> <sha256> — re-hash the stored artifact against its key.
qd_verify() {
    local class="$1" key="$2" dir base actual
    dir="$(qd_path "$class" "$key")" || return 1
    base="$(qd_entry_name "$class" "$key")" || return 1
    actual="$(sha256sum "$dir/$base" | awk '{print $1}')" || return 1
    [ "$actual" = "$key" ] || { _qdc_die "qd_verify: cached $class/$key corrupt (got $actual)"; return 1; }
}

# ---- fetch --------------------------------------------------------------

# qd_fetch <class> <url> <sha256> [basename]
# Ensure the artifact is cached under <class>/<sha256>/ and print its path.
# Cold: download to a private .partial dir, verify the digest, promote.
# Warm: return the existing path (verified at store time; qd_verify for an
# explicit re-check). Offline: never touches the network; a missing entry
# fails naming the exact key to warm.
#
# The caller's expected digest IS the integrity check — use it for classes
# where a signed manifest supplies the sha. For classes with a detached
# signature (cloud qcow2) keep the signature-verified helpers; this is the
# generic digest-only path.
qd_fetch() {
    local class="$1" url="$2" key="$3" base dir lock_fd tmp dest rc
    base="${4:-$(basename "${url%%\?*}")}"  # separate line: $-expansion in a
    # local decl evaluates against the OUTER scope, not the new locals
    dir="$(qd_path "$class" "$key")" || return 1
    dest="$(qd_path "$class" "$key" "$base")" || return 1

    if [ -s "$dest" ]; then
        printf '%s\n' "$dest"; return 0
    fi

    qd_deps_offline && {
        _qdc_die "offline: missing dependency $class/$key ($base) — warm the cache first (QDISTRO_DEPS_CACHE_DIR=$(qd_deps_root))"
        return 1
    }

    qd_deps_init || return 1
    exec {lock_fd}>"$(qd_deps_root)/.locks/${class}-${key}.lock" || return 1
    flock -x "$lock_fd" || { exec {lock_fd}>&-; return 1; }

    rc=0
    if [ ! -s "$dest" ]; then  # recheck under the lock
        tmp="$(mktemp -d "$(qd_deps_root)/.partial/${class}-${key:0:12}.XXXXXX")" \
            || { rc=1; }
        if [ "$rc" -eq 0 ]; then
            echo "[deps-cache] fetching $url" >&2
            if wget -q -O "$tmp/$base" "$url"; then
                if stored="$(qd_store_file "$class" "$key" "$tmp/$base" "$url" "$base")"; then
                    dest="$stored"
                else
                    rc=1
                fi
            else
                _qdc_die "download failed: $url"; rc=1
            fi
            rm -rf "$tmp"
        fi
    fi

    flock -u "$lock_fd"; exec {lock_fd}>&-
    [ "$rc" -eq 0 ] && printf '%s\n' "$dest"
    return "$rc"
}

# qd_fetch_sidecar <class> <sha256> <url> <suffix>
# Fetch a sidecar (<url><suffix>) into the entry dir, e.g.
#   qd_fetch_sidecar cloud "$SHA" "$CLOUD_URL" .sha256
#   qd_fetch_sidecar cloud "$SHA" "$CLOUD_URL" .sha256.asc
# Digest is not checked (the artifact's own verify path covers content);
# presence is. Prints the sidecar path. Skips if already present.
qd_fetch_sidecar() {
    local class="$1" key="$2" url="$3" suffix="$4" dir base side dest
    base="$(basename "${url%%\?*}")"
    dir="$(qd_path "$class" "$key")" || return 1
    side="$base$suffix"; dest="$dir/$side"
    [ -s "$dest" ] && { printf '%s\n' "$dest"; return 0; }
    qd_deps_offline && { _qdc_die "offline: missing sidecar $class/$key/$side"; return 1; }
    [ -d "$dir" ] || { _qdc_die "qd_fetch_sidecar: artifact entry absent, fetch it first ($dir)"; return 1; }
    wget -q -O "$dest" "$url$suffix" || { rm -f "$dest"; _qdc_die "download failed: $url$suffix"; return 1; }
    printf '%s\n' "$dest"
}

# ---- audit --------------------------------------------------------------

# qd_offline_check — read artifact specs on stdin, one per line:
#   <class> <sha256> <basename>     (basename may be "-" for "any artifact")
# Report every missing entry as "MISSING <class>/<key> <base>" and return
# non-zero if any are absent. For callers wiring an offline preflight.
qd_offline_check() {
    local missing=0 class key base line
    while read -r class key base; do
        case "$class" in ''|'#'*) continue ;; esac
        line="$class $key $base"
        if [ "$base" = - ]; then
            qd_has "$class" "$key" || { echo "MISSING $line" >&2; missing=1; }
        else
            qd_has "$class" "$key" "$base" || { echo "MISSING $line" >&2; missing=1; }
        fi
    done
    return "$missing"
}
