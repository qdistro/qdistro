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
    local v
    for v in "${QDISTRO_DEPS_OFFLINE:-}" "${QCI_OFFLINE:-}"; do
        case "$v" in 1|true|yes|on) return 0 ;; esac
    done
    return 1
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

# ---- repo mirror ---------------------------------------------------------

# The mirrored TW history repos live at <root>/repos/<snapshot>/tumbleweed/
# repo/{oss,non-oss}/ — upstream's own layout, so a consumer needs only a
# different BASE for the same <base>/<snapshot>/tumbleweed/repo/<name>/ URL.
# qd_repo_base prints the base a caller should use: an explicit
# QDISTRO_REPO_BASE override, else the local mirror (file://) when complete
# for both repos, else upstream. Offline with no mirror fails closed.

# qd_repo_dir <snapshot> — local repo root for a snapshot.
qd_repo_dir() {
    case "$1" in ''|*[!0-9]*) _qdc_die "qd_repo_dir: bad snapshot '$1'"; return 1 ;; esac
    printf '%s/repos/%s\n' "$(qd_deps_root)" "$1"
}

# qd_repo_present <snapshot> <repo> — the mirror COMPLETED for that repo
# (mirror.json is written only after repodata + every named package
# verified; a repodata-only tree does not count).
qd_repo_present() {
    [ -s "$(qd_repo_dir "$1" 2>/dev/null)/tumbleweed/repo/$2/mirror.json" ]
}

# qd_repo_base <snapshot> — print the history-base URL to hand to consumers.
# The mirror root is <root>/repos (NOT .../repos/<snap>): consumers append
# /<snap>/tumbleweed/repo/<name>/ just like they do to the upstream base.
qd_repo_base() {
    local snap="$1" dir
    [ -n "${QDISTRO_REPO_BASE:-}" ] && { printf '%s\n' "$QDISTRO_REPO_BASE"; return 0; }
    dir="$(qd_repo_dir "$snap")" || return 1
    if qd_repo_present "$snap" oss && qd_repo_present "$snap" non-oss; then
        printf 'file://%s/repos\n' "$(qd_deps_root)"; return 0
    fi
    qd_deps_offline && {
        _qdc_die "offline: no complete repo mirror for snapshot $snap under $dir — warm with qd_repo_mirror"
        return 1
    }
    printf '%s\n' "${QDISTRO_REPO_UPSTREAM:-https://download.opensuse.org/history}"
}

# qd_repo_base_or_upstream <snapshot> — the default base for consumers that
# CANNOT resolve file:// (guests, containers, guestfish appliances — the
# host filesystem is not theirs). Explicit QDISTRO_REPO_BASE wins (caller
# picked the transport, e.g. qd_serve_guest_base); offline without one
# fails closed naming the fix; online falls back to upstream. Host-side
# consumers resolve through qd_repo_base instead (file:// is fine there).
qd_repo_base_or_upstream() {
    local snap="${1:-}"
    if [ -n "${QDISTRO_REPO_BASE:-}" ]; then
        printf '%s\n' "$QDISTRO_REPO_BASE"; return 0
    fi
    if qd_deps_offline; then
        _qdc_die "offline: no repo base — export QDISTRO_REPO_BASE to a guest-reachable mirror (qd_serve_start + qd_serve_guest_base${snap:+ $snap})"
        return 1
    fi
    printf '%s\n' "${QDISTRO_REPO_UPSTREAM:-https://download.opensuse.org/history}"
}

# qd_repo_mirror <snapshot> [oss|non-oss ...] [-- worker flags]
# Warm the repo mirror: repomd-verified metadata + checksum-verified RPMs
# via scripts/vm/qd-repo-mirror.py. Refuses under the offline flags.
# Bare repo names select repos (default both); anything else (--limit N,
# --verify, --force, --metadata-only, --base U) passes through to the worker.
qd_repo_mirror() {
    local snap="$1" script; shift
    script="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/qd-repo-mirror.py"
    [ -f "$script" ] || { _qdc_die "repo mirror worker missing: $script"; return 1; }
    local -a repos=() passthru=()
    local r
    for r in "$@"; do
        case "$r" in
            oss|non-oss) repos+=(--repo "$r") ;;
            *) passthru+=("$r") ;;
        esac
    done
    [ "${#repos[@]}" -eq 0 ] && repos=(--repo oss --repo non-oss)
    python3 "$script" --root "$(qd_deps_root)" --snapshot "$snap" "${repos[@]}" "${passthru[@]}"
}

# ---- vendored-libweston libdisplay-info seed ------------------------------

# weston-16 wants libdisplay-info >=0.2.0 <0.4.0; Tumbleweed ships 0.4.x, so
# meson's wrap-git fallback (subprojects/display-info.wrap) clones
# gitlab.freedesktop.org on EVERY cold build — a network fetch invisible to
# QCI_OFFLINE. qd_display_info_tarball returns the verified, cached 0.2.0
# source archive; build-libweston.sh seeds subprojects/display-info/ from it
# so the wrap is never reached.
QDISTRO_DISPLAY_INFO_URL="${QDISTRO_DISPLAY_INFO_URL:-https://gitlab.freedesktop.org/emersion/libdisplay-info/-/archive/0.2.0/libdisplay-info-0.2.0.tar.gz}"
QDISTRO_DISPLAY_INFO_SHA256="${QDISTRO_DISPLAY_INFO_SHA256:-f7331fcaf5527251b84c8fb84238d06cd2f458422ce950c80e86c72927aa8c2b}"

qd_display_info_tarball() {
    qd_fetch src "$QDISTRO_DISPLAY_INFO_URL" "$QDISTRO_DISPLAY_INFO_SHA256"
}

# ---- OCI images -----------------------------------------------------------

# Registry pulls are the one remote op behind every Containerfile build
# (podman build runs --pull=never everywhere). The cacheable artifact is
# the digest-keyed saved archive:
#
#   oci/<digest-hex>/image.tar        podman save --format oci-archive
#   oci/<digest-hex>/manifest.txt     ref=<ref>, digest=sha256:...
#   oci/refs/<ref-hash>.digest        ref -> digest map (offline tag
#                                   resolution: a tag cannot be resolved
#                                   without a registry query)
#
# qd_oci_ensure <ref> — the image ends up in the local podman store,
# content-verified, and the archive + ref map are cached so a cold host
# (or a post-`podman system prune` one) never pulls again. Prints the
# resolved sha256 digest. Offline: local image or cached archive suffices;
# else fails closed naming the ref.

# _qd_oci_ref_key <ref> — filesystem-safe ref key for oci/refs/.
_qd_oci_ref_key() {
    printf '%s' "$1" | sha256sum | awk '{print $1}'
}

# _qd_oci_digest <ref> — image digest in the local store (.Digest, else .Id).
_qd_oci_digest() {
    local d
    d="$(podman image inspect "$1" --format '{{.Digest}}' 2>/dev/null)"
    if [ -z "$d" ]; then
        d="$(podman image inspect "$1" --format '{{.Id}}' 2>/dev/null)"
        case "$d" in sha256:*) ;; *) d="sha256:$d" ;; esac
    fi
    [ -n "$d" ] && printf '%s\n' "$d"
}

# _qd_oci_complete <dir> — a cached archive is usable only when ALL of it
# landed: the tar, its sha256 sidecar, and the manifest written last.
# An interrupted save can leave image.tar alone — that state is INCOMPLETE,
# never a warm hit. Empty files don't count either (-s).
_qd_oci_complete() {
    [ -s "$1/image.tar" ] && [ -s "$1/image.tar.sha256" ] \
        && [ -s "$1/manifest.txt" ]
}

# _qd_oci_save <ref> <digest> — save the archive under a per-digest lock.
_qd_oci_save() {
    local ref="$1" digest="$2" hex dir lock tmp
    hex="${digest#sha256:}"
    dir="$(qd_deps_root)/oci/$hex"
    _qd_oci_complete "$dir" && return 0
    install -d "$dir" "$(qd_deps_root)/.locks" || return 1
    lock="$(qd_deps_root)/.locks/oci-$hex.lock"
    (
        flock -x 9
        _qd_oci_complete "$dir" && exit 0
        tmp="$(mktemp "$dir/.image.XXXXXXXX")" || exit 1
        if podman save --format oci-archive -o "$tmp" "$ref" >&2; then
            chmod 0644 "$tmp"
            mv -f "$tmp" "$dir/image.tar" || exit 1
        else
            rm -f "$tmp"; exit 1
        fi
        ( cd "$dir" && sha256sum image.tar ) > "$dir/.image.tar.sha256.tmp" \
            && mv -f "$dir/.image.tar.sha256.tmp" "$dir/image.tar.sha256" \
            || exit 1
        # The manifest lands last — its presence is the completion marker.
        qd_write_manifest oci "$hex" "$ref" image.tar || exit 1
    ) 9>"$lock"
}

# qd_oci_cached <ref> [--verify] — is a COMPLETE archive cached for this
# ref? Pure filesystem audit: ref map resolves, and the digest entry has
# non-empty tar + sidecar + manifest. Does not touch the podman store.
# With --verify, also checks archive bytes against the sha256 sidecar —
# the same proof qd_oci_ensure runs before podman load.
qd_oci_cached() {
    local ref="$1" root refmap digest dir
    root="$(qd_deps_root)"
    refmap="$root/oci/refs/$(_qd_oci_ref_key "$ref").digest"
    [ -s "$refmap" ] || return 1
    digest="$(cat "$refmap")"
    dir="$root/oci/${digest#sha256:}"
    _qd_oci_complete "$dir" || return 1
    if [ "${2:-}" = --verify ]; then
        ( cd "$dir" && sha256sum -c image.tar.sha256 ) >/dev/null 2>&1 || return 1
    fi
}

qd_oci_ensure() {
    local ref="$1" root refdir refmap digest archive
    [ -n "$ref" ] || { _qdc_die "qd_oci_ensure: empty ref"; return 1; }
    command -v podman >/dev/null || { _qdc_die "qd_oci_ensure: podman required"; return 2; }
    qd_deps_init || return 1
    root="$(qd_deps_root)"
    refdir="$root/oci/refs"
    refmap="$refdir/$(_qd_oci_ref_key "$ref").digest"

    # 1. Local store hit — still cache the archive when online so the
    #    warm story survives `podman system prune` and cold hosts.
    if podman image exists "$ref"; then
        digest="$(_qd_oci_digest "$ref")" || {
            _qdc_die "qd_oci_ensure: cannot inspect local image $ref"
            return 3
        }
        install -d "$refdir" || return 1
        printf '%s\n' "$digest" >"$refmap.tmp" && mv "$refmap.tmp" "$refmap"
        qd_deps_offline || _qd_oci_save "$ref" "$digest" || return 1
        printf '%s\n' "$digest"
        return 0
    fi

    # 2. Complete cached archive for this ref — verify + load, never pull.
    #    An interrupted save leaves image.tar without its sidecar/manifest:
    #    that is NOT a hit — online it repairs via pull+resave, offline it
    #    fails closed below.
    if [ -s "$refmap" ]; then
        digest="$(cat "$refmap")"
        archive="$root/oci/${digest#sha256:}/image.tar"
        if _qd_oci_complete "$root/oci/${digest#sha256:}"; then
            ( cd "$(dirname "$archive")" \
                && sha256sum -c image.tar.sha256 ) >/dev/null 2>&1 || {
                _qdc_die "qd_oci_ensure: cached archive fails sha256 — $archive"
                return 3
            }
            podman load -i "$archive" >/dev/null 2>&1 || {
                _qdc_die "qd_oci_ensure: podman load failed for $archive"
                return 3
            }
            podman image exists "$ref" || {
                _qdc_die "qd_oci_ensure: loaded $archive but $ref absent from store"
                return 3
            }
            printf '%s\n' "$digest"
            return 0
        fi
    fi

    # 3. Cold — offline fails closed naming the ref.
    if qd_deps_offline; then
        _qdc_die "offline: no complete cached OCI archive for $ref (need oci/<digest>/{image.tar,image.tar.sha256,manifest.txt} + refs map)"
        return 1
    fi
    podman pull --quiet "$ref" >&2 || {
        _qdc_die "qd_oci_ensure: podman pull $ref failed"
        return 3
    }
    digest="$(_qd_oci_digest "$ref")" || {
        _qdc_die "qd_oci_ensure: cannot inspect pulled image $ref"
        return 3
    }
    install -d "$refdir" || return 1
    printf '%s\n' "$digest" >"$refmap.tmp" && mv "$refmap.tmp" "$refmap"
    _qd_oci_save "$ref" "$digest" || return 1
    printf '%s\n' "$digest"
}

# ---- guest transport (qd-serve) -----------------------------------------

# Guests cannot see the host filesystem: slirp reaches the host at
# 10.0.2.2, libguestfs/passt at 169.254.2.2. qd_serve_start exports the
# whole deps root over HTTP on 0.0.0.0 so a guest can use
# QDISTRO_REPO_BASE=http://<host-addr>:<port>/repos — the same layout as
# the file:// host base. State lives in the root: .serve.pid/.serve.port
# are stale-tolerant (a dead pid is re-checked, not trusted).

# qd_serve_start — start (or reuse) the server; print the bound port.
qd_serve_start() {
    local root pid_file port_file pid
    qd_deps_init || return 1
    root="$(qd_deps_root)"
    pid_file="$root/.serve.pid" port_file="$root/.serve.port"
    if [ -s "$pid_file" ] && [ -s "$port_file" ]; then
        pid="$(cat "$pid_file")"
        if kill -0 "$pid" 2>/dev/null; then
            cat "$port_file"; return 0
        fi
        rm -f "$pid_file" "$port_file"
    fi
    rm -f "$port_file.next"
    # The server binds port 0 and writes the kernel-assigned port itself —
    # no pick-then-bind race.
    python3 - "$root" "$port_file.next" >"$root/.serve.log" 2>&1 <<'PY' &
import functools, http.server, socketserver, sys
root, portfile = sys.argv[1], sys.argv[2]
h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=root)
with socketserver.TCPServer(("0.0.0.0", 0), h) as s:
    with open(portfile, "w") as f:
        f.write(str(s.server_address[1]))
    s.serve_forever()
PY
    pid=$!
    for _ in $(seq 1 50); do
        kill -0 "$pid" 2>/dev/null || break
        [ -s "$port_file.next" ] && break
        sleep 0.1
    done
    if [ ! -s "$port_file.next" ] || ! kill -0 "$pid" 2>/dev/null; then
        _qdc_die "qd_serve_start: http server failed to start (see $root/.serve.log)"
        return 1
    fi
    mv "$port_file.next" "$port_file"
    echo "$pid" >"$pid_file"
    cat "$port_file"
}

qd_serve_port() {
    local root pid_file port_file pid
    root="$(qd_deps_root)"
    pid_file="$root/.serve.pid" port_file="$root/.serve.port"
    [ -s "$pid_file" ] && [ -s "$port_file" ] || return 1
    pid="$(cat "$pid_file")"
    kill -0 "$pid" 2>/dev/null || return 1
    cat "$port_file"
}

qd_serve_stop() {
    local root pid_file port_file pid
    root="$(qd_deps_root)"
    pid_file="$root/.serve.pid" port_file="$root/.serve.port"
    [ -s "$pid_file" ] || { rm -f "$port_file"; return 0; }
    pid="$(cat "$pid_file")"
    kill "$pid" 2>/dev/null || true
    rm -f "$pid_file" "$port_file"
}

# qd_serve_guest_base <snapshot> [host-addr] — the QDISTRO_REPO_BASE value
# for a guest lane: requires the server running and a complete mirror.
# host-addr defaults to slirp's 10.0.2.2; passt lanes pass 169.254.2.2.
qd_serve_guest_base() {
    local snap="$1" host="${2:-10.0.2.2}" port
    port="$(qd_serve_port)" || {
        _qdc_die "qd_serve_guest_base: server not running — qd_serve_start first"
        return 1
    }
    if ! qd_repo_present "$snap" oss || ! qd_repo_present "$snap" non-oss; then
        _qdc_die "qd_serve_guest_base: no complete mirror for $snap — qd_repo_mirror first"
        return 1
    fi
    printf 'http://%s:%s/repos\n' "$host" "$port"
}

# qd_repo_wire_container <snapshot> <mountpoint>
# Container builds cannot see the host cache — when the caller hasn't set
# QDISTRO_REPO_BASE and a complete mirror exists, export
# QDISTRO_REPO_BASE=file://<mountpoint> and return 0; the caller must then
# add --volume <root>/repos:<mountpoint>:ro,z to its podman argv (file://
# inside the build resolves only with the mount present). Returns 1 when
# unwired: explicit base wins, or no mirror → online falls back upstream,
# offline fails closed downstream. MUST be called in the current shell —
# a subshell (command substitution, mapfile process substitution) drops
# the export.
qd_repo_wire_container() {
    local snap="$1" mnt="$2"
    [ -n "${QDISTRO_REPO_BASE:-}" ] && return 1
    qd_repo_present "$snap" oss || return 1
    qd_repo_present "$snap" non-oss || return 1
    QDISTRO_REPO_BASE="file://$mnt"
    export QDISTRO_REPO_BASE
    echo "[deps-cache] wired QDISTRO_REPO_BASE=$QDISTRO_REPO_BASE" >&2
}

# qd_repo_base_safe <url> — repo bases interpolate into .repo files and
# appliance --run-command strings; anything beyond a single plain URL token
# (whitespace, quotes, metacharacters) is injection, not a URL.
qd_repo_base_safe() {
    # regex in a variable: bare &/() in an unquoted =~ pattern trips the
    # [[ ]] parser before the match ever runs.
    local re='^(https?|file)://[A-Za-z0-9._~:/?&=%+@-]+$'
    [[ "$1" =~ $re ]]
}

# qd_guestfs_host_addr — the host address a libguestfs appliance reaches:
# 1.60+ prefers passt (169.254.2.2) when runnable; older releases and
# hosts without passt fall back to QEMU slirp (10.0.2.2). Call sites that
# probe the backend themselves (build-baked-baseweed.sh) should pass the
# discovered address to qd_repo_wire_guest instead.
qd_guestfs_host_addr() {
    local ver major='' minor='' k v
    ver="$(guestfish version 2>/dev/null || true)"
    # Current output is multi-line "major: N\nminor: N"; older guests printed
    # "guestfish X.Y.Z" on one line — accept both. Parse with builtins only:
    # callers may run this under a stripped PATH.
    while IFS=': ' read -r k v; do
        case "$k" in major) major="$v" ;; minor) minor="$v" ;; esac
    done <<< "$ver"
    if [ -z "$major" ] && [[ "$ver" =~ ([0-9]+)\.([0-9]+) ]]; then
        major="${BASH_REMATCH[1]}" minor="${BASH_REMATCH[2]}"
    fi
    if { [ "${major:-0}" -gt 1 ] 2>/dev/null \
        || { [ "${major:-0}" -eq 1 ] && [ "${minor:-0}" -ge 60 ]; } 2>/dev/null; } \
        && command -v passt >/dev/null 2>&1; then
        echo 169.254.2.2
    else
        echo 10.0.2.2
    fi
}

# qd_repo_wire_guest <snapshot> [host-addr]
# Wire guest lanes to the mirror automatically: when the caller has not set
# QDISTRO_REPO_BASE and a complete mirror exists for <snapshot>, start
# qd_serve and export QDISTRO_REPO_BASE for the guest-reachable host
# address. With no complete mirror this is a no-op — online callers fall
# back to upstream and offline consumers still fail closed in
# qd_repo_base/qdistro_substrate_repo_command.
qd_repo_wire_guest() {
    local snap="$1" host="${2:-10.0.2.2}"
    [ -n "${QDISTRO_REPO_BASE:-}" ] && return 0
    qd_repo_present "$snap" oss || return 0
    qd_repo_present "$snap" non-oss || return 0
    qd_serve_start >/dev/null || return 1
    QDISTRO_REPO_BASE="$(qd_serve_guest_base "$snap" "$host")" || return 1
    export QDISTRO_REPO_BASE
    echo "[deps-cache] wired QDISTRO_REPO_BASE=$QDISTRO_REPO_BASE" >&2
}

# ---- path resolution ----------------------------------------------------

# qd_path <class> <sha256> [basename]
# Print the canonical cache path for an artifact. With no basename, resolves
# to the entry directory (which also holds manifest.txt and sidecars).
# _qdc_valid_basename <name> — a basename may never be a metadata name
# (manifest.txt would have _qdc_write_manifest overwrite the artifact
# itself), a dotfile, a lock, a staging tmp, or anything with a slash.
_qdc_valid_basename() {
    case "$1" in
        ''|.|..|manifest.txt|.*|*.lock|*.partial.*|*/*) return 1 ;;
    esac
    return 0
}

qd_path() {
    local class="$1" key="$2" base="${3:-}"
    case "$class" in
        .|..|*/*|*[!a-z0-9._-]*|'') _qdc_die "qd_path: bad class '$class'"; return 1 ;;
    esac
    [[ "$key" =~ ^[0-9a-f]{64}$ ]] || { _qdc_die "qd_path: bad sha256 key '$key'"; return 1; }
    case "$base" in
        '') printf '%s/%s/%s\n' "$(qd_deps_root)" "$class" "$key" ;;
        *) _qdc_valid_basename "$base" || { _qdc_die "qd_path: bad basename '$base'"; return 1; }
           printf '%s/%s/%s/%s\n' "$(qd_deps_root)" "$class" "$key" "$base" ;;
    esac
}

# _qdc_entry_candidates <dir> — print artifact-looking regular files:
# not manifest.txt, not dotfiles, not sidecar/lock suffixes. Fallback for
# entries written by callers that keep no manifest (osci-managed classes).
_qdc_entry_candidates() {
    local f base
    for f in "$1"/*; do
        [ -f "$f" ] || continue
        base="$(basename "$f")"
        case "$base" in
            manifest.txt|.*|*.sha256|*.asc|*.sig|*.lock) continue ;;
        esac
        printf '%s\n' "$f"
    done
}

# Manifests are the canonical record of the artifact name when present.
# A manifest basename that fails the artifact-name rules is ignored (a
# corrupt/hostile manifest cannot escape the entry dir via ../).
_qdc_manifest_basename() {
    local base
    [ -f "$1/manifest.txt" ] || return 0
    base="$(sed -n 's/^basename=//p' "$1/manifest.txt" | head -1)"
    _qdc_valid_basename "$base" && printf '%s\n' "$base" || return 0
}

# qd_has <class> <sha256> [basename] — 0 iff the artifact file is present
# and non-empty. With no basename, resolves the canonical artifact name
# (manifest first, then a filtered scan that excludes sidecars/locks).
qd_has() {
    local class="$1" key="$2" base="${3:-}" dir f
    dir="$(qd_path "$class" "$key")" || return 1
    if [ -n "$base" ]; then
        _qdc_valid_basename "$base" || { _qdc_die "qd_has: bad basename '$base'"; return 1; }
        [ -s "$dir/$base" ]; return
    fi
    [ -d "$dir" ] || return 1
    base="$(_qdc_manifest_basename "$dir")"
    if [ -n "$base" ]; then
        [ -s "$dir/$base" ]; return
    fi
    while IFS= read -r f; do
        [ -s "$f" ] && return 0
    done < <(_qdc_entry_candidates "$dir")
    return 1
}

# qd_entry_name <class> <sha256> — print the stored artifact's basename
# (manifest first, then the filtered scan).
qd_entry_name() {
    local class="$1" key="$2" dir base f
    dir="$(qd_path "$class" "$key")" || return 1
    base="$(_qdc_manifest_basename "$dir")"
    if [ -n "$base" ] && [ -f "$dir/$base" ]; then
        printf '%s\n' "$base"; return 0
    fi
    while IFS= read -r f; do
        basename "$f"; return 0
    done < <(_qdc_entry_candidates "$dir")
    _qdc_die "qd_entry_name: no artifact under $dir"; return 1
}

# ---- store / verify -----------------------------------------------------

_qdc_write_manifest() {
    local dir="$1" class="$2" url="$3" key="$4" base="$5"
    printf 'class=%s\nurl=%s\nsha256=%s\nbasename=%s\nfetched_at=%s\n' \
        "$class" "$url" "$key" "$base" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > "$dir/manifest.txt" || return 1
}

# qd_write_manifest <class> <sha256> <url> <basename>
# Public manifest writer for callers that store artifacts through their own
# verified path (e.g. the openSUSE signature-verified cloud download), so
# every entry — qd_fetch-created or externally managed — has the canonical
# record. Idempotent; overwrites.
qd_write_manifest() {
    local class="$1" key="$2" url="$3" base="$4" dir
    dir="$(qd_path "$class" "$key")" || return 1
    [ -d "$dir" ] || { _qdc_die "qd_write_manifest: no entry dir $dir"; return 1; }
    _qdc_write_manifest "$dir" "$class" "$url" "$key" "$base"
}

# _qdc_promote <src> <dest> — move src into place at dest atomically:
# always lands first at a hidden tmp name inside dest's own directory
# (mktemp), then renames. A same-fs mv is a rename; a cross-fs mv/cp
# copies into the hidden name — either way nothing partial is ever
# visible at the final path. src is consumed on success.
_qdc_promote() {
    local src="$1" dest="$2" staged
    staged="$(mktemp "$(dirname "$dest")/.promote.XXXXXXXX")" || return 1
    if ! mv -f "$src" "$staged" 2>/dev/null && ! cp -f "$src" "$staged"; then
        rm -f "$staged"; return 1
    fi
    if ! mv -f "$staged" "$dest"; then rm -f "$staged"; return 1; fi
    [ ! -e "$src" ] || rm -f "$src"
    return 0
}

# qd_store_file <class> <sha256> <srcfile> <url> [basename]
# Verify srcfile's sha256 against the key, then promote it into the cache
# (verify-then-promote at the destination, so even a cross-fs adopt is
# atomic and the promoted bytes are re-verified before becoming visible).
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
    _qdc_promote "$src" "$dir/$base" || return 1
    # Re-verify the promoted bytes: a truncated cross-fs copy can never
    # masquerade as verified content under this key.
    actual="$(sha256sum "$dir/$base" | awk '{print $1}')" || { rm -f "$dir/$base"; return 1; }
    [ "$actual" = "$key" ] || {
        rm -f "$dir/$base"
        _qdc_die "qd_store_file: promoted copy of $src failed digest re-check; refusing to cache"
        return 1
    }
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
    local class="$1" key="$2" legacy="$3" url="$4" dir base suffix stored lock_fd art_fd rc
    dir="$(qd_path "$class" "$key")" || return 1
    qd_has "$class" "$key" && return 0
    [ -s "$legacy" ] || return 0
    # canonical basename is the UPSTREAM artifact name, not the legacy one —
    # e.g. openSUSE signature verification binds the signed checksum to it
    base="$(basename "${url%%\?*}")"
    _qdc_valid_basename "$base" || { _qdc_die "qd_adopt: bad basename '$base'"; return 1; }

    # Two locks: the per-key deps-cache flock (vs qd_fetch/adopt) AND the
    # per-artifact $cache.lock the signature-verified cloud helper holds
    # while publishing into this same entry dir.
    qd_deps_init || return 1
    exec {lock_fd}>"$(qd_deps_root)/.locks/${class}-${key}.lock" || return 1
    flock -x "$lock_fd" || { exec {lock_fd}>&-; return 1; }
    install -d "$dir" || { flock -u "$lock_fd"; exec {lock_fd}>&-; return 1; }
    exec {art_fd}>"$dir/$base.lock" || { flock -u "$lock_fd"; exec {lock_fd}>&-; return 1; }
    flock -x "$art_fd" || { exec {art_fd}>&-; flock -u "$lock_fd"; exec {lock_fd}>&-; return 1; }

    rc=0
    if ! qd_has "$class" "$key" && [ -s "$legacy" ]; then  # recheck under the locks
        if stored="$(qd_store_file "$class" "$key" "$legacy" "$url" "$base")"; then
            for suffix in .sha256 .sha256.asc .asc .sig; do
                [ -s "$legacy$suffix" ] && { _qdc_promote "$legacy$suffix" "$dir/$base$suffix" || true; }
            done
            echo "[deps-cache] adopted legacy $legacy -> $stored" >&2
        else
            echo "[deps-cache] WARNING: legacy cache $legacy does not match key $key; left in place, will refetch" >&2
        fi
    fi

    flock -u "$art_fd"; exec {art_fd}>&-
    flock -u "$lock_fd"; exec {lock_fd}>&-
    return "$rc"
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
    local class="$1" url="$2" key="$3" base dir lock_fd tmp dest rc stored entry
    base="${4:-$(basename "${url%%\?*}")}"  # separate line: $-expansion in a
    # local decl evaluates against the OUTER scope, not the new locals
    dir="$(qd_path "$class" "$key")" || return 1
    dest="$(qd_path "$class" "$key" "$base")" || return 1

    if [ -s "$dest" ]; then
        printf '%s\n' "$dest"; return 0
    fi
    # Content-keyed warm hit: same digest under a different basename (e.g.
    # floating vs dated name for identical bytes) resolves, no re-fetch.
    if qd_has "$class" "$key"; then
        entry="$(qd_entry_name "$class" "$key")" || return 1
        printf '%s\n' "$dir/$entry"; return 0
    fi

    qd_deps_offline && {
        _qdc_die "offline: missing dependency $class/$key ($base) — warm the cache first (QDISTRO_DEPS_CACHE_DIR=$(qd_deps_root))"
        return 1
    }

    qd_deps_init || return 1
    exec {lock_fd}>"$(qd_deps_root)/.locks/${class}-${key}.lock" || return 1
    flock -x "$lock_fd" || { exec {lock_fd}>&-; return 1; }

    rc=0
    # Full canonical recheck under the lock: an adopt or a differently
    # named fetch may have populated this digest while we waited.
    if [ ! -s "$dest" ] && qd_has "$class" "$key"; then
        entry="$(qd_entry_name "$class" "$key")" || { rc=1; entry=; }
        [ -n "$entry" ] && dest="$dir/$entry"
    fi
    if [ "$rc" -eq 0 ] && [ ! -s "$dest" ]; then
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
    local class="$1" key="$2" url="$3" suffix="$4" dir base side dest tmp
    base="$(basename "${url%%\?*}")"
    dir="$(qd_path "$class" "$key")" || return 1
    side="$base$suffix"; dest="$dir/$side"
    [ -s "$dest" ] && { printf '%s\n' "$dest"; return 0; }
    qd_deps_offline && { _qdc_die "offline: missing sidecar $class/$key/$side"; return 1; }
    [ -d "$dir" ] || { _qdc_die "qd_fetch_sidecar: artifact entry absent, fetch it first ($dir)"; return 1; }
    # Hidden mktemp + mv: a partial sidecar is never visible at the final
    # path, $$-based names can't collide across sibling subshells, and a
    # failed fetch removes only our own temp — never a live one.
    tmp="$(mktemp "$dir/.sidecar.XXXXXXXX")" || return 1
    wget -q -O "$tmp" "$url$suffix" || { rm -f "$tmp"; _qdc_die "download failed: $url$suffix"; return 1; }
    mv -f "$tmp" "$dest" || { rm -f "$tmp"; return 1; }
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
