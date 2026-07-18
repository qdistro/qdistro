# shellcheck shell=bash
# opensuse-cloud-image.sh — verified download/cache of the openSUSE
# Tumbleweed cloud base image (J25 supply-chain hardening).
#
# The Tumbleweed "Minimal-VM Cloud" qcow2 becomes the root-disk base of every
# VM built by the harnesses (baseweed-admin, tier-5 base, bare-metal test
# targets). Fetched with only a `[ -s ]` non-empty check it is an
# unauthenticated root artifact: a MITM or poisoned mirror can substitute a
# trojaned base that then runs as root in every downstream VM.
#
# openSUSE publishes `<image>.sha256` and a detached `<image>.sha256.asc`
# signed by the openSUSE Project Signing Key. This library:
#   1. verifies the .sha256.asc signature against a CHECKED-IN copy of that
#      key (scripts/vm/keys/opensuse-tumbleweed-signing-key.asc), pinned to
#      its fingerprint so an accidental/hostile key swap is caught;
#   2. binds to gpgv's actual VALIDSIG fingerprint (not any in-band claim);
#   3. checks the image digest against the signed .sha256;
#   4. only then promotes the download into the shared cache.
# The signed .sha256/.sha256.asc sidecars are cached next to the image so an
# existing cache can be re-verified after the rolling upstream checksum moves.
#
# Sourced, not executed. Callers:
#   . "<scripts/vm>/lib/opensuse-cloud-image.sh"
#   download_verified_cloud_image "$CLOUD_URL" "$CLOUD_CACHE"
#
# Authenticity, not freshness: a signed rolling checksum proves openSUSE
# signed this image, not that it is the newest. Add a pinned lockfile if exact
# snapshot reproducibility is ever required (see J25 notes).

# Resolve this library's own directory so the key path is independent of the
# caller's CWD or SCRIPT_DIR.
_OSCI_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
OPENSUSE_TW_KEY="${OPENSUSE_TW_KEY:-$_OSCI_LIB_DIR/../keys/opensuse-tumbleweed-signing-key.asc}"
# openSUSE Project Signing Key (the key currently signing Tumbleweed
# appliance checksums). Verified end-to-end against the live
# Cloud.qcow2.sha256.asc when this was landed.
OPENSUSE_TW_FPR="${OPENSUSE_TW_FPR:-AD485664E901B867051AB15F35A2F86E29B700A4}"

_osci_die() { echo "ERROR: $*" >&2; return 1; }

_osci_require_tools() {
    local t
    for t in wget gpg gpgv sha256sum awk mktemp basename dirname mv install chmod rm; do
        command -v "$t" >/dev/null 2>&1 || { _osci_die "$t not found; cannot verify openSUSE cloud image"; return 1; }
    done
}

# Verify a detached .sha256.asc over its .sha256 with the pinned key, binding
# to gpgv's real VALIDSIG fingerprint. rc 0 iff the signature is good AND made
# by exactly the pinned key.
verify_opensuse_sha256_signature() {
    local sha_file="$1" sig_file="$2" tmp gnupghome keyring key_fpr status sig_fpr rc

    [ -s "$OPENSUSE_TW_KEY" ] || { _osci_die "missing openSUSE Tumbleweed key: $OPENSUSE_TW_KEY"; return 1; }
    [ -s "$sha_file" ]        || { _osci_die "missing checksum file: $sha_file"; return 1; }
    [ -s "$sig_file" ]        || { _osci_die "missing checksum signature: $sig_file"; return 1; }

    tmp="$(mktemp -d "${TMPDIR:-/tmp}/qdistro-os-key.XXXXXX")" || { _osci_die "mktemp failed"; return 1; }
    chmod 0700 "$tmp"
    gnupghome="$tmp/gnupg"; install -d -m 0700 "$gnupghome"

    # (1) the checked-in key must carry the pinned fingerprint.
    key_fpr="$(GNUPGHOME="$gnupghome" gpg --batch --quiet --show-keys --with-colons "$OPENSUSE_TW_KEY" 2>/dev/null \
                | awk -F: '$1=="fpr"{print toupper($10); exit}')"
    if [ "$key_fpr" != "$OPENSUSE_TW_FPR" ]; then
        rm -rf "$tmp"
        _osci_die "openSUSE key fingerprint mismatch: got ${key_fpr:-<none>}, want $OPENSUSE_TW_FPR"; return 1
    fi

    # gpgv needs a dearmored keyring (armored .asc is rejected by --keyring).
    keyring="$tmp/opensuse-tumbleweed.gpg"
    if ! GNUPGHOME="$gnupghome" gpg --batch --quiet --dearmor -o "$keyring" "$OPENSUSE_TW_KEY"; then
        rm -rf "$tmp"; _osci_die "failed to dearmor openSUSE key"; return 1
    fi

    # (2) signature must be cryptographically good.
    rc=0
    status="$(gpgv --status-fd 1 --keyring "$keyring" "$sig_file" "$sha_file" 2>/dev/null)" || rc=$?
    if [ "$rc" -ne 0 ]; then
        rm -rf "$tmp"; _osci_die "bad openSUSE checksum signature (gpgv rc=$rc)"; return 1
    fi

    # (3) …and made by exactly the pinned key (VALIDSIG is gpgv's own verdict).
    sig_fpr="$(printf '%s\n' "$status" | awk '$1=="[GNUPG:]" && $2=="VALIDSIG"{print toupper($3); exit}')"
    rm -rf "$tmp"
    if [ "$sig_fpr" != "$OPENSUSE_TW_FPR" ]; then
        _osci_die "checksum signed by unexpected key: got ${sig_fpr:-<none>}, want $OPENSUSE_TW_FPR"; return 1
    fi
    return 0
}

# Extract the sha256 digest for a SPECIFIC filename from a .sha256 file. The
# signature authenticates the checksum file, not which artifact it is for, so
# we bind to the expected basename: a mirror replaying a valid signed checksum
# for a *different* openSUSE artifact (with matching bytes) is rejected. The
# coreutils format is "<hex>  <name>" (name may carry a leading '*' for binary).
_osci_signed_sha256_for() {
    local sha_file="$1" want="$2" expected
    expected="$(awk -v want="$want" '
        tolower($1) ~ /^[0-9a-f]{64}$/ {
            fn=$2; sub(/^\*/, "", fn);
            if (fn == want) { print tolower($1); exit }
        }' "$sha_file")"
    [ -n "$expected" ] || { _osci_die "no sha256 digest for '$want' in $sha_file"; return 1; }
    printf '%s' "$expected"
}

# verify_cached_cloud_image <image> <sha_file> <sig_file> <expected_name>
# Verify an image against its signed .sha256/.sha256.asc sidecars, binding the
# signed digest to <expected_name> (the upstream artifact basename).
verify_cached_cloud_image() {
    local image="$1" sha_file="$2" sig_file="$3" want="$4" expected actual
    [ -n "$want" ] || { _osci_die "verify_cached_cloud_image: missing expected artifact name"; return 1; }
    verify_opensuse_sha256_signature "$sha_file" "$sig_file" || return 1
    expected="$(_osci_signed_sha256_for "$sha_file" "$want")" || return 1
    actual="$(sha256sum "$image" | awk '{print tolower($1)}')"
    [ "$actual" = "$expected" ] || { _osci_die "cloud image digest mismatch for $image (got $actual, want $expected)"; return 1; }
    return 0
}

# download_verified_cloud_image <url> <cache_path>
# Ensures <cache_path> holds an openSUSE-signed image. Re-verifies an existing
# cache (with sidecars); otherwise downloads image+.sha256+.sha256.asc to a
# private temp dir, verifies, and only then promotes into the cache. Fails
# closed: on any verification failure nothing is promoted and rc is non-zero.
download_verified_cloud_image() {
    local url="$1" cache="$2" tmp base image sha sig cache_dir

    _osci_require_tools || return 1
    cache_dir="$(dirname "$cache")"; install -d "$cache_dir"
    base="$(basename "$url")"   # the upstream artifact name the checksum is for

    if [ -s "$cache" ] && [ -s "$cache.sha256" ] && [ -s "$cache.sha256.asc" ]; then
        echo "[cloud] verifying cached image: $cache"
        verify_cached_cloud_image "$cache" "$cache.sha256" "$cache.sha256.asc" "$base" || return 1
        echo "[cloud] cached image verified (openSUSE-signed)"
        return 0
    fi
    [ -s "$cache" ] && echo "[cloud] cached image lacks signed sidecars; re-downloading a verified copy" >&2

    tmp="$(mktemp -d "$cache_dir/cloud-download.XXXXXX")" || { _osci_die "mktemp under $cache_dir failed"; return 1; }
    chmod 0700 "$tmp"
    image="$tmp/$base"; sha="$tmp/$base.sha256"; sig="$tmp/$base.sha256.asc"

    echo "[cloud] downloading $url"
    wget -q --show-progress -O "$image" "$url"        || { rm -rf "$tmp"; _osci_die "download failed: $url"; return 1; }
    wget -q -O "$sha" "$url.sha256"                    || { rm -rf "$tmp"; _osci_die "download failed: $url.sha256"; return 1; }
    wget -q -O "$sig" "$url.sha256.asc"               || { rm -rf "$tmp"; _osci_die "download failed: $url.sha256.asc"; return 1; }

    if ! verify_cached_cloud_image "$image" "$sha" "$sig" "$base"; then
        rm -rf "$tmp"; return 1
    fi

    mv "$image" "$cache"; mv "$sha" "$cache.sha256"; mv "$sig" "$cache.sha256.asc"
    rm -rf "$tmp"
    echo "[cloud] verified cache ready: $cache"
    return 0
}
