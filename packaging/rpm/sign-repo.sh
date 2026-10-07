#!/usr/bin/env bash
# sign-repo.sh — sign every RPM in repo/ and sign the repodata so the
# qdistro repository verifies under Agama's gpgFingerprints policy.
# Uses the keyring in keys/gnupg/ (gitignored — see keys/README.md).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$here/repo
gnupg=$here/keys/gnupg

[ -d "$gnupg" ] || { echo "no keyring at $gnupg — see keys/README.md" >&2; exit 1; }
keyid=$(GNUPGHOME=$gnupg gpg --batch --list-secret-keys --with-colons \
        | awk -F: '/^sec/ {print $5; exit}')
[ -n "$keyid" ] || { echo "no secret key in $gnupg" >&2; exit 1; }

command -v rpmsign >/dev/null || { echo "needs rpmsign (rpm-sign)" >&2; exit 1; }

for rpm in "$repo"/*.rpm; do
    [ -e "$rpm" ] || { echo "no RPMs in $repo — run build-all.sh first" >&2; exit 1; }
    if ! rpm -K --nosignature "$rpm" >/dev/null 2>&1 || \
       ! rpmsign --checksign "$rpm" 2>/dev/null | grep -qi 'signatures OK'; then
        GNUPGHOME=$gnupg rpmsign --addsign --key-id="$keyid" "$rpm" >/dev/null
        echo "signed $(basename "$rpm")"
    else
        echo "already signed: $(basename "$rpm")"
    fi
done

# repomd signature (detached) + public key next to the metadata
GNUPGHOME=$gnupg gpg --batch -a --export "$keyid" > "$repo/repodata/repomd.xml.key"
GNUPGHOME=$gnupg gpg --batch -a --detach-sign --default-key "$keyid" \
    "$repo/repodata/repomd.xml"
echo "repo signed: $repo"
