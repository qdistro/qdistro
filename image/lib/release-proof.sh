#!/bin/bash
# release-proof.sh -- prove a copied-out release artifact on the host
# (todo/iso/14 Phase C DONE bar). Sourced by image/build-in-vm.sh and by the
# hermetic tests; pure function, prints its evidence, returns non-zero on
# the first failed check. Run it in a subshell when its output is captured:
# it uses `return`, never `exit`, so the caller keeps control either way.
#
# qdistro_prove_release <raw> <bundle-dir> <xz-name> <size-mib>
#   raw         the raw kiwi built, copied to the host
#   bundle-dir  the copied-out bundle/ holding <xz-name> and <xz-name>.sha256
#   xz-name     qdistro-<version>-<snapshot>.raw.xz (what the name must be)
#   size-mib    config.xml <size unit="M">: the raw and the decompressed xz
#               must both be exactly this many MiB
qdistro_prove_release() {
    local raw="$1" bundle="$2" name="$3" size_mib="$4"
    local xz="$bundle/$name" want raw_bytes unc
    want=$(( size_mib * 1024 * 1024 ))
    echo "artifact: $xz"
    [ -f "$raw" ] || { echo "FAIL: raw missing: $raw"; return 1; }
    raw_bytes="$(stat -c %s "$raw")"
    echo "raw: $raw $raw_bytes bytes (want $want)"
    [ "$raw_bytes" = "$want" ] || { echo "FAIL: raw size != <size unit=M>$size_mib"; return 1; }
    [ -f "$xz" ] || { echo "FAIL: artifact missing: $xz"; return 1; }
    [ -s "$xz.sha256" ] || { echo "FAIL: checksum file missing: $xz.sha256"; return 1; }
    # The checksum file names the artifact; it must name THIS one, literally
    # (a regex would let the dots match a lookalike, round-2 review), and it
    # must be the file's only record.
    local sum_lines sum_name
    sum_lines="$(grep -c . "$xz.sha256")"
    [ "$sum_lines" = 1 ] || { echo "FAIL: $name.sha256 has $sum_lines records, want 1"; return 1; }
    sum_name="$(awk 'NR == 1 { sub(/^[0-9a-f]+ [ *]/, ""); print }' "$xz.sha256")"
    [ "$sum_name" = "$name" ] || { echo "FAIL: $name.sha256 names '$sum_name', not $name"; return 1; }
    (cd "$bundle" && sha256sum -c "$name.sha256") || { echo "FAIL: sha256 mismatch"; return 1; }
    # xz -l --robot: the `totals` row is streams, blocks, compressed,
    # uncompressed, ratio, check, ...; column 5 is the uncompressed size.
    unc="$(xz -l --robot "$xz" | awk '$1 == "totals" { print $5 }')" || { echo "FAIL: xz -l"; return 1; }
    echo "xz uncompressed: ${unc:-?} bytes (want $want)"
    [ "$unc" = "$want" ] || { echo "FAIL: decompressed size != <size unit=M>$size_mib"; return 1; }
    xz -t -T0 "$xz" || { echo "FAIL: xz -t"; return 1; }
    echo "xz -t: OK"
    echo "RESULT: PASS $name"
}
