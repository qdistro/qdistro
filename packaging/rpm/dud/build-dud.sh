#!/usr/bin/env bash
# build-dud.sh — build the Driver Update Disk RPM that injects the qdistro
# product into a STOCK Agama ISO's live products.d at initrd time
# (inst.dud=label://OEMDRV/qdistro-product-dud.rpm). Only needed for the
# unmodified stock ISO — the custom ISO embeds the product already.
#
# Usage: build-dud.sh <out.rpm>
set -euo pipefail
OUT=${1:?usage: $0 <out.rpm>}
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=../../env.sh
. "$here/../../env.sh"

yaml=$here/../../agama/out/qdistro.yaml
[ -f "$yaml" ] || { echo "$yaml missing — run agama/render-profile.sh first" >&2; exit 1; }

work=$(mktemp -d "${QDISTRO_BUILD_TMP:-/tmp}/dud.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir -p "$work"/{SPECS,SOURCES,BUILD,BUILDROOT,RPMS,SRPMS}
cp "$here/qdistro-product-dud.spec" "$work/SPECS/"
cp "$yaml" "$work/SOURCES/qdistro.yaml"

podman run --rm -v "$work":/rpmbuild:rw,Z "$QDISTRO_RPM_BUILDER" \
    rpmbuild --define "_topdir /rpmbuild" -ba /rpmbuild/SPECS/qdistro-product-dud.spec

find "$work/RPMS" -name '*.rpm' -exec cp {} "$OUT" \;
echo "wrote $OUT"
