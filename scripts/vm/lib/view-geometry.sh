# shellcheck shell=bash
#
# view-geometry.sh -- VIEW-UNIQUE GEOMETRY for every image the harness hands a
# vision driver (spec: todo/test-blankscreenshots/SPEC.md, F1/F2).
#
# WHY. The sanctioned vision driver sees BLACK wherever a later image repeats,
# at the same position in an image of the same size, content it was already
# shown in that session. Byte-identical frames, a one-pixel change, and a
# second look at the same file are all read as black. A different size (even
# 1 px) is read correctly. So every image the harness writes during one
# ATTEMPT gets a (W,H) that has not been issued before in that attempt: the
# raw frame is padded with a black right/bottom margin of k px, where k >= 1 is
# the smallest value whose (raw_w+k, raw_h+k) pair is still unused.
#
# SOURCE-SAFE: functions only, no `set -e/-u`, no side effects at load. It is
# sourced by capture-attest.sh (and so by vm-gui and the qdwin/qdlocker
# helpers) and by ci/lib/gates/gui.sh. There is ONE copy of every rule below.
#
# STATE. The gate exports QCI_GUI_VIEW_STATE, an absolute path to a dotfile
# inside the per-attempt artifact directory. Every allocation takes flock on
# that file and appends one TSV row:
#     W  H  padded_sha256  raw_pix_sha  kind  source  path
# Failed publications may consume a reservation; nothing is ever re-issued.
# Without QCI_GUI_VIEW_STATE (manual use outside qci) publication is a plain
# no-op pass-through: no padding, no sidecar, and no uniqueness guarantee.
#
# SIDECAR. A padded frame F carries `F.raw`, one line:
#     raw_w raw_h k raw_pix_sha kind source
# (`source` is the rest of the line and may contain spaces.) A frame without a
# sidecar is raw (legacy). Invariant I6: F cropped to raw_w x raw_h +0+0 has
# canonical pixels equal to raw_pix_sha.
#
# CANONICAL PIXELS. raw_pix_sha is sha256 over ONE decoded stream, defined only
# in qci_view_pix_sha: the header `qci-rgb8 <w> <h>\n` followed by 8-bit RGB
# (alpha dropped). It is used for candidates, legacy frames, crops and
# sidecars alike. Encoded-file digests (ledger, guest transfer, evidence
# manifest) are NOT this and are never compared with it.
#
# KIND is one token: a primary kind (screenshot, screenshot-fresh, click-raw,
# click-post, qdwin, apps, virsh-diag: captures; click-annotated,
# click-zoom, view, crop:WxH+X+Y: derivatives), optionally followed by
# comma-separated status tags inherited along a lineage: rejected, diag, stale.


# Canonical pixel digest of an image file. Echoes the sha256.
# Returns 5 when ImageMagick is absent, 2 when the file cannot be decoded.
qci_view_pix_sha() {
    local f=${1:-} dims out
    command -v magick >/dev/null 2>&1 || return 5
    [ -f "$f" ] || return 2
    dims=$(magick identify -quiet -format '%w %h\n' "${f}[0]" 2>/dev/null | head -1) || return 2
    [[ "$dims" =~ ^[1-9][0-9]*\ [1-9][0-9]*$ ]] || return 2
    out=$(set -o pipefail
          { printf 'qci-rgb8 %s\n' "$dims"
            magick "${f}[0]" -alpha off -depth 8 rgb:- 2>/dev/null; } | sha256sum) || return 2
    out=${out%% *}
    [[ "$out" =~ ^[0-9a-f]{64}$ ]] || return 2
    printf '%s\n' "$out"
}

# Decoded dimensions of an image file: echoes "W H". Returns 5/2 as above.
qci_view_dims() {
    local f=${1:-} dims
    command -v magick >/dev/null 2>&1 || return 5
    [ -f "$f" ] || return 2
    dims=$(magick identify -quiet -format '%w %h\n' "${f}[0]" 2>/dev/null | head -1) || return 2
    [[ "$dims" =~ ^[1-9][0-9]*\ [1-9][0-9]*$ ]] || return 2
    printf '%s\n' "$dims"
}

# Echo the frame's sidecar line when it has a well-formed one; return 1 when
# it has none (the frame is raw) and 3 when a sidecar exists but is malformed.
qci_view_sidecar() {
    local f=${1:-} line rw rh k sha kind src
    [ -f "$f.raw" ] && [ ! -L "$f.raw" ] || return 1
    IFS= read -r line < "$f.raw" || [ -n "$line" ] || return 3
    read -r rw rh k sha kind src <<<"$line"
    [[ "$rw" =~ ^[1-9][0-9]*$ && "$rh" =~ ^[1-9][0-9]*$ && "$k" =~ ^[1-9][0-9]*$ ]] || return 3
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || return 3
    [ -n "$kind" ] || return 3
    printf '%s\n' "$line"
}

# Raw dimensions of a frame: from its sidecar, else decoded (legacy frame).
qci_view_raw_dims() {
    local f=${1:-} line rw rh rest
    if line=$(qci_view_sidecar "$f"); then
        read -r rw rh rest <<<"$line"
        printf '%s %s\n' "$rw" "$rh"
        return 0
    fi
    qci_view_dims "$f"
}

# Raw canonical pixel digest of a frame: from its sidecar, else computed.
qci_view_raw_pix_sha() {
    local f=${1:-} line rw rh k sha rest
    if line=$(qci_view_sidecar "$f"); then
        read -r rw rh k sha rest <<<"$line"
        printf '%s\n' "$sha"
        return 0
    fi
    qci_view_pix_sha "$f"
}

# Write the frame's RAW content (cropped to its sidecar dims at +0+0) to OUT as
# a PNG. A frame without a sidecar is copied unchanged. For consumers that
# measure pixels: OCR, brightness, equal-size diffs, dimension checks.
qci_view_raw_extract() {
    local f=${1:-} out=${2:-} rw rh
    [ -n "$out" ] && [ -f "$f" ] || return 2
    if qci_view_sidecar "$f" >/dev/null; then
        command -v magick >/dev/null 2>&1 || return 5
        read -r rw rh < <(qci_view_raw_dims "$f") || return 2
        magick "${f}[0]" -crop "${rw}x${rh}+0+0" +repage PNG24:"$out" 2>/dev/null || return 2
        return 0
    fi
    cp -T -- "$f" "$out"
}

# The state path for this attempt, validated. Echoes it; returns 1 when unset
# (manual use: publication is a pass-through) and 2 when set but unusable.
qci_view_state() {
    local s=${QCI_GUI_VIEW_STATE:-}
    [ -n "$s" ] || return 1
    case "$s" in
        /*) ;;
        *) echo "view-geometry: ERROR: QCI_GUI_VIEW_STATE must be absolute (got $s)" >&2; return 2 ;;
    esac
    if [ -L "$s" ] || { [ -e "$s" ] && [ ! -f "$s" ]; }; then
        echo "view-geometry: ERROR: QCI_GUI_VIEW_STATE $s is not a regular file" >&2
        return 2
    fi
    [ -d "$(dirname -- "$s")" ] || {
        echo "view-geometry: ERROR: the directory of QCI_GUI_VIEW_STATE $s does not exist" >&2
        return 2
    }
    printf '%s\n' "$s"
}

# The allocation rule, pure: given raw W H on the command line and the state
# file on stdin, echo the smallest k >= 1 whose (W+k, H+k) is unused.
qci_view_pick_k() {
    local rw=$1 rh=$2
    awk -F'\t' -v rw="$rw" -v rh="$rh" '
        /^#/ { next }
        NF >= 2 { used[$1 " " $2] = 1 }
        END { k = 1; while (((rw + k) " " (rh + k)) in used) k++; print k }'
}

# Plain staged publisher for images that carry no ledger row (derivatives).
qci_view_mv_publish() {
    mv -fT -- "$1" "$2"
}

# Strip TSV-breaking characters from a caller-influenced field.
_qci_view_field() {
    local v=${1:-}
    v=${v//$'\t'/ }; v=${v//$'\n'/ }; v=${v//$'\r'/ }
    printf '%s' "$v"
}

# PUBLISH A RAW FRAME AS A VIEW-UNIQUE PADDED FRAME.
#   qci_view_publish RAW DST KIND SOURCE PUBLISHER [ARGS...]
# PUBLISHER is called as `PUBLISHER [ARGS...] STAGED DST` and must move/copy
# the staged file to DST (and write any ledger row); its exit status is
# returned. RAW itself is never modified or published under an image name.
#
# Order, all under the state lock (same-destination publishers serialize):
# measure raw -> reserve (W,H) and append the state row -> pad to a stage in
# DST's directory -> stage the sidecar -> set any old sidecar aside -> publish
# the frame -> rename the sidecar into place. A failure removes both stages,
# restores the old sidecar only if the old frame is still there untouched, and
# never leaves a sidecar that does not describe the frame beside it.
qci_view_publish() {
    local raw=${1:-} dst=${2:-} kind=${3:-} source=${4:-}
    shift 4 || return 2
    [ "$#" -ge 1 ] || { echo "view-geometry: qci_view_publish needs a publisher" >&2; return 2; }
    local state rc=0 fd dims rw rh rsha k W H stage="" sstage="" psha old="" dir
    state=$(qci_view_state) || rc=$?
    if [ "$rc" -eq 1 ]; then
        # Manual use: pass-through. A sidecar left by an earlier padded
        # publication would now describe the wrong bytes, so it goes.
        rc=0
        "$@" "$raw" "$dst" || rc=$?
        [ "$rc" -ne 0 ] || rm -f -- "$dst.raw"
        return "$rc"
    fi
    [ "$rc" -eq 0 ] || return 1
    if ! command -v magick >/dev/null 2>&1; then
        # Loud, not silent: the frame is published raw and this attempt loses
        # the unique-geometry guarantee for it.
        echo "view-geometry: WARNING: ImageMagick is absent; $dst is published UNPADDED and a repeat view of it may be misread as black" >&2
        rc=0
        "$@" "$raw" "$dst" || rc=$?
        [ "$rc" -ne 0 ] || rm -f -- "$dst.raw"
        return "$rc"
    fi
    kind=$(_qci_view_field "${kind:-frame}"); kind=${kind// /_}
    source=$(_qci_view_field "${source:--}")
    dims=$(qci_view_dims "$raw") || { echo "view-geometry: ERROR: cannot decode $raw; nothing was published to $dst" >&2; return 2; }
    read -r rw rh <<<"$dims"
    rsha=$(qci_view_pix_sha "$raw") || { echo "view-geometry: ERROR: cannot read the pixels of $raw; nothing was published to $dst" >&2; return 2; }
    dir=$(dirname -- "$dst")
    exec {fd}>>"$state" || { echo "view-geometry: ERROR: cannot open the view state $state" >&2; return 1; }
    if ! flock -x "$fd"; then
        exec {fd}>&-
        echo "view-geometry: ERROR: cannot lock the view state $state" >&2
        return 1
    fi
    rc=0
    k=$(qci_view_pick_k "$rw" "$rh" < "$state") || rc=1
    [[ "${k:-}" =~ ^[1-9][0-9]*$ ]] || rc=1
    if [ "$rc" -eq 0 ]; then
        W=$((rw + k)); H=$((rh + k))
        stage=$(mktemp -- "$dir/.qci-view.XXXXXX") || rc=2
    fi
    if [ "$rc" -eq 0 ]; then
        magick "${raw}[0]" -alpha off -background black -gravity northwest \
            -extent "${W}x${H}" PNG24:"$stage" 2>/dev/null || rc=2
    fi
    if [ "$rc" -eq 0 ]; then
        psha=$(sha256sum -- "$stage" | awk '{print $1}') || rc=2
        chmod "$(printf '%o' "$(( 0666 & ~$(umask) ))")" -- "$stage" 2>/dev/null || rc=2
    fi
    if [ "$rc" -eq 0 ]; then
        # The reservation is durable before anything is published.
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$W" "$H" "$psha" "$rsha" "$kind" \
            "$source" "$(_qci_view_field "$dst")" >> "$state" || rc=1
    fi
    if [ "$rc" -eq 0 ]; then
        sstage=$(mktemp -- "$dir/.qci-view-raw.XXXXXX") || rc=2
        [ "$rc" -ne 0 ] || printf '%s %s %s %s %s %s\n' "$rw" "$rh" "$k" "$rsha" "$kind" "$source" \
            > "$sstage" || rc=2
        [ "$rc" -ne 0 ] || chmod "$(printf '%o' "$(( 0666 & ~$(umask) ))")" -- "$sstage" || rc=2
    fi
    if [ "$rc" -eq 0 ] && { [ -e "$dst.raw" ] || [ -L "$dst.raw" ]; }; then
        old=$(mktemp -- "$dir/.qci-view-old.XXXXXX") && mv -fT -- "$dst.raw" "$old" || rc=2
    fi
    if [ "$rc" -eq 0 ]; then
        "$@" "$stage" "$dst" || rc=$?
        if [ "$rc" -eq 0 ]; then
            if ! mv -fT -- "$sstage" "$dst.raw"; then
                echo "view-geometry: ERROR: published $dst but could not write its sidecar; the frame was removed rather than left without its raw identity" >&2
                rm -f -- "$dst"
                rc=1
            fi
            sstage=""
            [ -z "$old" ] || rm -f -- "$old"
            old=""
        fi
    fi
    [ -z "$stage" ] || rm -f -- "$stage"
    [ -z "$sstage" ] || rm -f -- "$sstage"
    if [ -n "$old" ]; then
        # The publication failed. Restore the previous sidecar only beside the
        # frame it described; a sidecar without its frame is removed.
        if [ -f "$dst" ]; then mv -fT -- "$old" "$dst.raw" 2>/dev/null || rm -f -- "$old"
        else rm -f -- "$old"; fi
    fi
    exec {fd}>&-
    return "$rc"
}

# F2: `view-copy`. Write a freshly padded copy of ANY image, for a second look
# or for an image the harness did not just hand over (a scenario crop).
#   qci_view_copy SRC [--source CAPTURE --crop WxH+X+Y] [--out PATH]
# Raw content is used when SRC has a sidecar (a padded frame is never
# re-padded). The copy's sidecar records its lineage: kind `view` and source
# SRC, or kind `crop:GEOM` and source CAPTURE for a declared scenario crop,
# which is VERIFIED against the capture's raw pixels. Status inherits along the
# lineage (rejected, diag, stale; a stale `.meta` is copied beside the view).
# A view never gets a ledger row. Echoes the new path.
qci_view_copy() {
    local src="" cap="" geom="" out="" arg rc=0 tmp stem n kind tags="" sline skind cline ckind src_for_row
    while [ "$#" -gt 0 ]; do
        arg=$1; shift
        case "$arg" in
            --source) cap=${1:-}; shift || true ;;
            --crop) geom=${1:-}; shift || true ;;
            --out) out=${1:-}; shift || true ;;
            -*) echo "view-copy: unknown option $arg" >&2; return 2 ;;
            *) [ -z "$src" ] || { echo "view-copy: one image at a time" >&2; return 2; }; src=$arg ;;
        esac
    done
    [ -n "$src" ] && [ -f "$src" ] || { echo "view-copy: no such image: ${src:-<none>}" >&2; return 2; }
    if [ -n "$cap$geom" ] && { [ -z "$cap" ] || [ -z "$geom" ]; }; then
        echo "view-copy: --source and --crop go together" >&2; return 2
    fi
    qci_view_state >/dev/null || rc=$?
    if [ "$rc" -ne 0 ]; then
        [ "$rc" -eq 2 ] || echo "view-copy: QCI_GUI_VIEW_STATE is not set. Inside qci it always is; by hand, pass --attempt-dir DIR (vm-gui) so views of one attempt get unique sizes" >&2
        return 2
    fi
    command -v magick >/dev/null 2>&1 || { echo "view-copy: ImageMagick 'magick' is required" >&2; return 2; }
    src=$(readlink -f -- "$src") || return 2
    if [ -z "$out" ]; then
        stem=${src%.png}; stem=${stem%.PNG}
        n=1; while [ -e "$stem.view-$n.png" ] || [ -e "$stem.view-$n.png.raw" ]; do n=$((n + 1)); done
        out="$stem.view-$n.png"
    fi
    case "$out" in /*) ;; *) out="$PWD/$out" ;; esac
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/qci-view-copy.XXXXXX") || return 2
    qci_view_raw_extract "$src" "$tmp/raw.png" || { rm -rf -- "$tmp"; echo "view-copy: cannot read $src" >&2; return 2; }
    # Status inherited from SRC.
    case "$src" in *.rejected) tags="$tags,rejected" ;; esac
    if sline=$(qci_view_sidecar "$src"); then
        read -r _ _ _ _ skind _ <<<"$sline"
        case ",${skind#*,}," in *,rejected,*) tags="$tags,rejected" ;; esac
        case ",${skind#*,}," in *,stale,*) tags="$tags,stale" ;; esac
        case "$skind" in virsh-diag*|*,diag*) tags="$tags,diag" ;; esac
    fi
    [ -f "$src.meta" ] && tags="$tags,stale"
    if [ -n "$geom" ]; then
        [[ "$geom" =~ ^([1-9][0-9]*)x([1-9][0-9]*)\+([0-9]+)\+([0-9]+)$ ]] || {
            rm -rf -- "$tmp"; echo "view-copy: --crop must be WxH+X+Y (got $geom)" >&2; return 2; }
        [ -f "$cap" ] || { rm -rf -- "$tmp"; echo "view-copy: --source capture not found: $cap" >&2; return 2; }
        cap=$(readlink -f -- "$cap") || return 2
        qci_view_raw_extract "$cap" "$tmp/cap.png" || { rm -rf -- "$tmp"; echo "view-copy: cannot read $cap" >&2; return 2; }
        magick "$tmp/cap.png" -crop "$geom" +repage PNG24:"$tmp/check.png" 2>/dev/null || true
        if [ ! -f "$tmp/check.png" ] || [ "$(qci_view_pix_sha "$tmp/check.png")" != "$(qci_view_pix_sha "$tmp/raw.png")" ]; then
            rm -rf -- "$tmp"
            echo "view-copy: $src is not the $geom crop of $cap's raw pixels; lineage NOT recorded, nothing written" >&2
            return 2
        fi
        case "$cap" in *.rejected) tags="$tags,rejected" ;; esac
        if cline=$(qci_view_sidecar "$cap"); then
            read -r _ _ _ _ ckind _ <<<"$cline"
            case ",${ckind#*,}," in *,rejected,*) tags="$tags,rejected" ;; esac
            case "$ckind" in virsh-diag*|*,diag*) tags="$tags,diag" ;; esac
        fi
        [ -f "$cap.meta" ] && tags="$tags,stale"
        kind="crop:$geom"
        src_for_row=$cap
    else
        kind=view
        src_for_row=$src
    fi
    [ -z "$tags" ] || kind="$kind$(printf '%s' "$tags" | tr ',' '\n' | awk 'NF && !s[$0]++ {printf ",%s", $0}')"
    rc=0
    qci_view_publish "$tmp/raw.png" "$out" "$kind" "$src_for_row" qci_view_mv_publish || rc=$?
    if [ "$rc" -eq 0 ]; then
        if [ -f "$src.meta" ]; then cp -T -- "$src.meta" "$out.meta" 2>/dev/null || true
        elif [ -n "$cap" ] && [ -f "$cap.meta" ]; then cp -T -- "$cap.meta" "$out.meta" 2>/dev/null || true
        fi
    fi
    rm -rf -- "$tmp"
    [ "$rc" -eq 0 ] || return "$rc"
    printf '%s\n' "$out"
}

# Copy a frame WITH its identity: the frame plus its `.raw` sidecar (and a
# stale `.meta`). Use this, or `cp` both files, whenever a scenario copies a
# capture; a PNG-only copy loses the raw identity and reads as a raw frame.
qci_view_pair_copy() {
    local src=${1:-} dst=${2:-}
    [ -f "$src" ] && [ -n "$dst" ] || { echo "qci_view_pair_copy: SRC DST" >&2; return 2; }
    [ -d "$dst" ] && dst="$dst/$(basename -- "$src")"
    cp -T -- "$src" "$dst" || return 1
    if [ -f "$src.raw" ]; then cp -T -- "$src.raw" "$dst.raw" || return 1
    else rm -f -- "$dst.raw"; fi
    if [ -f "$src.meta" ]; then cp -T -- "$src.meta" "$dst.meta" || return 1; fi
    printf '%s\n' "$dst"
}
