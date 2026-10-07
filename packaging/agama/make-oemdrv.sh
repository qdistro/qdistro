#!/usr/bin/env bash
# make-oemdrv.sh — build an OEMDRV vfat image carrying a rendered Agama
# autoinstall profile. Boot the installer ISO with this attached and Agama
# installs unattended (inst.auto label probe).
#
# Usage: make-oemdrv.sh <profile.json> <out.img>
#   profile.json — rendered output of render-profile.sh (contains credentials;
#                  the image gets mode-600 contents by vfat nature — keep the
#                  file itself protected).
set -euo pipefail
[ $# -eq 2 ] || { echo "usage: $0 <profile.json> <out.img>" >&2; exit 2; }
profile=$1 out=$2
[ -f "$profile" ] || { echo "profile not found: $profile (render-profile.sh first)" >&2; exit 1; }
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$profile" \
    || { echo "profile is not valid JSON" >&2; exit 1; }
command -v mformat mcopy >/dev/null || { echo "needs mtools" >&2; exit 1; }

umask 077
truncate -s 16M "$out"
mformat -i "$out" -v OEMDRV -F ::
mcopy -i "$out" "$profile" ::autoinst.json
mdir -i "$out" ::
echo "wrote $out"
