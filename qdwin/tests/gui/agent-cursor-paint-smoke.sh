#!/usr/bin/env bash
set -euo pipefail

# Agent-assisted VM smoke for the two regressions that make a qdwin
# desktop unusable, both of which previously shipped silently:
#
#   (A) qdshell crash-loop. A version-skewed qdshell QML <-> qdwin QML
#       plugin pair (the plugin .so predating the `outputs` Q_PROPERTY
#       that Qdwin.qml's onOutputsChanged handler needs) makes Quickshell
#       throw "Cannot assign to non-existent property onOutputsChanged",
#       exit 255, and restart forever — so nothing paints and the scanout
#       is black. See todo/issues/qdwin/vm-double-cursor.md and the
#       Services/Qdwin/Qdwin.qml outputs/outputsChanged wiring.
#
#   (B) Missing / doubled VM cursor. The virtio-gpu cursor plane is hidden
#       from atomic clients that don't set DRM_CLIENT_CAP_CURSOR_PLANE_HOTSPOT
#       (fixed in the vendored libweston backend-drm), so the cursor used
#       to be software-composited into the scanout and doubled with the
#       SPICE host cursor. The fix puts a single cursor on the hardware DRM
#       plane (off the scanout). A regression shows up as either no cursor
#       plane / a blank cursor BO, or a cursor composited back into the
#       framebuffer.
#
# Assertions (in order):
#   1. qdshell is alive and NOT crash-looping (no exit 255, no QML
#      "non-existent property" error, bounded restart count).
#   2. The desktop actually PAINTS: qdshell's bar layer-shell surface is
#      mapped AND the scanout is non-black (pixel sample).
#   3. The cursor is on the hardware DRM plane: plane-1 has crtc=crtc-0,
#      a real fb, and is "allocated by weston" (authoritative — a hw-plane
#      cursor never appears in a virsh screenshot).
#   4. A cursor sprite is actually installed (not a blank BO).
#   5. AGENT-ASSISTED visual: move the pointer via QMP, screenshot, and
#      have a vision LLM confirm the desktop painted with a top bar and
#      that the scanout contains the EXPECTED number of cursors
#      (0 for the hw-plane/gl profile — a cursor drawn into the scanout
#      would be the double-cursor regression; set QDWIN_CURSOR_IN_SCANOUT=1
#      for the pixman/software-cursor profile).
#
# Usage:
#   VMNAME=qd-hwcursor-clean-260601-1657 tests/gui/agent-cursor-paint-smoke.sh
#
# Env:
#   VMNAME                   target libvirt domain (qemu:///session)
#   QDWIN_CURSOR_IN_SCANOUT  expected sw-cursors in screenshot (default 0)
#   QDWIN_SKIP_AGENT=1       skip the vision step (deterministic only)
#   QDWIN_AGENT_CMD          vision agent command (default: claude -p ...)
#   QDWIN_MAX_RESTARTS       max tolerated noctalia-shell NRestarts (default 2)
#
# Exit: 0 all-pass; 1 a discriminator failed; 2 environment/setup.

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
QDWIN_REPO=${QDWIN_REPO:-$ROOT}
VMNAME=${VMNAME:-$(virsh -c qemu:///session list --name --state-running | head -n1)}

export QDWIN_REPO
export QDWIN_VIRSH=${QDWIN_VIRSH:-virsh -c qemu:///session}

QDWIN_CURSOR_IN_SCANOUT=${QDWIN_CURSOR_IN_SCANOUT:-0}
QDWIN_MAX_RESTARTS=${QDWIN_MAX_RESTARTS:-2}
QDWIN_SKIP_AGENT=${QDWIN_SKIP_AGENT:-0}
ART=${QDWIN_ARTIFACT_DIR:-/tmp}

# shellcheck source=qdwin-helpers.sh
source "$QDWIN_REPO/tests/gui/qdwin-helpers.sh"
qdwin_set_vm "$VMNAME"

fail()       { echo "FAIL: $*" >&2; exit 1; }
setup_fail() { echo "SETUP: $*" >&2; exit 2; }
pass()       { echo "PASS: $*"; }

vm_exec() { "$QDWIN_VM_EXEC" "$VMNAME" "$@"; }

[ -n "$VMNAME" ] || setup_fail "no running VM; set VMNAME"
$QDWIN_VIRSH domstate "$VMNAME" >/dev/null 2>&1 || setup_fail "VM '$VMNAME' not found"
vm_exec "test -S /run/user/1000/wayland-1" >/dev/null \
    || setup_fail "qdwin Wayland socket is not up (compositor not running)"
pass "qdwin Wayland socket is up on $VMNAME"

# ---- 1. qdshell alive and not crash-looping ----------------------------
# A crash loop manifests three ways at once; assert against all three so a
# single flaky signal can't hide it.
SC='runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000 systemctl --user'

state=$(vm_exec "$SC show noctalia-shell.service -p ActiveState --value" 2>/dev/null | tr -d '[:space:]')
[ "$state" = active ] || fail "noctalia-shell.service is '$state', not active (qdshell down)"

nrestarts=$(vm_exec "$SC show noctalia-shell.service -p NRestarts --value" 2>/dev/null | tr -d '[:space:]')
[ -n "$nrestarts" ] || nrestarts=0
[ "$nrestarts" -le "$QDWIN_MAX_RESTARTS" ] \
    || fail "noctalia-shell.service NRestarts=$nrestarts > $QDWIN_MAX_RESTARTS (crash-looping)"
pass "noctalia-shell.service active, NRestarts=$nrestarts (<= $QDWIN_MAX_RESTARTS)"

# A single live qs process that has been up for a few seconds is the
# positive signal that the loop has stopped (vs. churning PIDs). Capture
# its start timestamp (VM clock) so the QML-error scan below ignores
# errors from earlier crash-looping instances in the SAME boot — we want
# "is qdshell healthy NOW", not "did it ever stumble at startup".
read -r qspid etimes since < <(vm_exec "p=\$(pgrep -x qs | head -1); [ -n \"\$p\" ] && echo \"\$p \$(ps -o etimes= -p \$p | tr -d ' ') \$(date -d @\$(stat -c %Y /proc/\$p) '+%Y-%m-%d_%H:%M:%S')\"" 2>/dev/null)
[ -n "${qspid:-}" ] || fail "no live qs (qdshell) process"
[ "${etimes:-0}" -ge 5 ] || fail "qs process (pid $qspid) only ${etimes:-0}s old — likely still restarting"
pass "qdshell (qs pid $qspid) process stable for ${etimes}s"

# The crash signatures, scoped to the LIVE process lifetime.
qmlerr=$(vm_exec "journalctl _UID=1000 --since '${since/_/ }' --no-pager 2>/dev/null | grep -c -e 'non-existent property' -e 'Quickshell:.*exit' || true" 2>/dev/null | tr -d '[:space:]')
[ "${qmlerr:-0}" = 0 ] \
    || fail "qdshell logged $qmlerr QML fatal/non-existent-property errors since the live process started (the onOutputsChanged crash)"
pass "no QML 'non-existent property' / Quickshell-exit errors since live process start"

bound=$(vm_exec "journalctl _UID=1000 -b --no-pager 2>/dev/null | sed -n 's/.*qdwin_shell_v1 bound v\\([0-9]*\\).*/\\1/p' | tail -1" 2>/dev/null | tr -d '[:space:]')
[ -n "$bound" ] && [ "$bound" -ge 14 ] \
    || fail "qdshell did not bind qdwin_shell_v1 at >=14 (got '${bound:-none}')"
pass "qdshell bound qdwin_shell_v1 v$bound"

# ---- 2. The desktop actually paints ------------------------------------
# qdshell-specific paint signal: its bar layer-shell surface is mapped.
vm_exec "journalctl _UID=1000 -b --no-pager 2>/dev/null | grep -q 'layer-shell mapped ns=qdshell-bar-content'" \
    || fail "qdshell bar-content layer surface never mapped — qdshell did not paint"
pass "qdshell bar-content layer surface mapped (desktop painted)"

shot="$ART/qdwin-cursor-paint-$$.png"
qdwin_screenshot "$shot" >/dev/null
# Non-black sample: a black scanout (crashed qdshell) reads ~0; a painted
# desktop is well above. Threshold 1% is comfortably between.
nonblack=$(python3 -W ignore - "$shot" <<'PY'
import sys
from PIL import Image
im = Image.open(sys.argv[1]).convert("RGB").resize((192, 108))
b = im.tobytes(); n = len(b) // 3
nb = sum(1 for i in range(0, len(b), 3) if max(b[i], b[i + 1], b[i + 2]) > 24) / n
print(f"{nb:.4f}")
PY
)
awk "BEGIN { exit !($nonblack > 0.01) }" \
    || fail "scanout is essentially black (non-black fraction $nonblack <= 0.01) — nothing painted"
pass "scanout non-black (fraction=$nonblack) — $shot"

# ---- 3. Cursor on the hardware DRM plane -------------------------------
# Authoritative cursor signal: plane-1 bound to crtc-0 with a real fb,
# allocated by weston. virsh screenshot can NOT show a hw-plane cursor, so
# this DRM state — not the pixels — is ground truth for "the cursor exists,
# and exactly one, on the hardware plane."
# Extract the plane-1 block: from its "...: plane-1" header to the next
# "...: plane-" header. (DRM debugfs prints e.g. "plane[34]: plane-1".)
cur=$(vm_exec "for d in /sys/kernel/debug/dri/*/state; do sed -n '/: plane-1\$/,/: plane-/p' \"\$d\" 2>/dev/null; done | head -16" 2>/dev/null || true)
grep -q 'crtc=crtc-0' <<<"$cur" \
    || fail "cursor plane-1 not bound to crtc-0 (no hardware cursor active). Plane state:\n$cur"
grep -qE 'fb=[0-9]+' <<<"$cur" \
    || fail "cursor plane-1 has no framebuffer (blank/absent cursor BO). Plane state:\n$cur"
grep -q 'allocated by = weston' <<<"$cur" \
    || fail "cursor plane-1 fb not allocated by weston. Plane state:\n$cur"
fbline=$(grep -m1 -oE 'fb=[0-9]+' <<<"$cur")
pass "cursor on hardware plane-1: crtc=crtc-0 $fbline allocated by weston"

# ---- 4. A cursor sprite is actually installed (not a blank BO) ---------
vm_exec "journalctl _UID=1000 -b --no-pager 2>/dev/null | grep -qE 'cursor-shape install shape=.*mapped on cursor_layer|cursor-sprite registered shape='" \
    || fail "no cursor sprite installed (mapped on cursor_layer) — cursor BO is likely blank/invisible"
pass "cursor sprite installed (mapped on cursor_layer)"

# ---- 4b. The installed sprite is NOT blank/transparent -----------------
# "mapped on cursor_layer" + an allocated DRM fb (step 3) are both satisfied
# by an all-transparent sprite — an invisible cursor. The compositor now logs
# the introspected payload of the live wl_shm sprite path
# ("payload=WxH nonzero_alpha=N"); require non-transparent pixels so a blank
# BO fails here instead of silently shipping an invisible cursor. Solid-color
# sprites (QDWIN_CURSOR_SPRITE_SOLID) are opaque by construction and logged
# separately as "solid rgba=...,A" with A>0. Non-introspectable buffers log
# "payload=non-shm" and fall back to the DRM-plane signal (a NOTE, not a fail).
# NOTE: parse SIGNED nonzero_alpha (-?[0-9]+). The compositor logs -1 when the
# SHM payload is non-introspectable (unknown format, or the hostile-stride
# guard tripped). In this live session the sprite is always ARGB8888 with a
# real count, so a 0 (blank) OR a -1 (introspection failed / abnormal) both
# mean "we have no positive evidence of a visible cursor" → fail closed.
# payload=none / payload=non-shm carry no nonzero_alpha field and fall through
# to the DRM-plane NOTE.
payload=$(vm_exec "journalctl _UID=1000 -b --no-pager 2>/dev/null | grep -oE 'mapped on cursor_layer \(hotspot=[-0-9,]+\) payload=[^ ]*( nonzero_alpha=-?[0-9]+)?' | tail -1" 2>/dev/null)
solid=$(vm_exec "journalctl _UID=1000 -b --no-pager 2>/dev/null | grep -oE 'solid rgba=[0-9.,]+' | tail -1" 2>/dev/null)
if grep -q 'nonzero_alpha=' <<<"$payload"; then
    na=$(grep -oE 'nonzero_alpha=-?[0-9]+' <<<"$payload" | grep -oE '\-?[0-9]+')
    if [ "${na:-0}" -eq 0 ]; then
        fail "installed cursor sprite is BLANK/transparent (nonzero_alpha=0) — invisible cursor. Payload: $payload"
    elif [ "${na:-0}" -lt 0 ]; then
        fail "cursor sprite payload not introspectable (nonzero_alpha=$na) — unexpected for the live wl_shm ARGB8888 path (format change or malformed sprite). Payload: $payload"
    fi
    pass "cursor sprite payload non-transparent (nonzero_alpha=$na) — $payload"
elif [ -n "$solid" ]; then
    a=$(grep -oE '[0-9.]+$' <<<"$solid")
    awk "BEGIN { exit !(${a:-0} > 0) }" \
        || fail "solid cursor sprite is fully transparent ($solid) — invisible cursor"
    pass "cursor sprite is opaque solid ($solid)"
elif [ -n "$payload" ]; then
    # An explicit payload=none / payload=non-shm line: the sprite mapped but
    # its pixels genuinely can't be introspected here. Fall back to the
    # DRM-plane checks (step 3) for non-blank evidence — a NOTE, not a fail.
    echo "NOTE: cursor sprite payload not introspectable ($payload); relying on DRM-plane checks (step 3) for non-blank evidence"
else
    # No "mapped on cursor_layer ... payload=" line and no solid sprite at all.
    # Step 4 can be satisfied by a bare "cursor-sprite registered shape=" log
    # (the sprite was registered but never actually mapped onto a pointer), so
    # reaching here means we have NO positive evidence a visible sprite was
    # installed. Fail closed rather than NOTE-passing an unmapped cursor.
    fail "no inspectable cursor sprite was mapped (neither a 'mapped on cursor_layer ... payload=' line nor a solid sprite found) — cursor likely registered-but-not-mapped, i.e. invisible"
fi

# ---- 5. Agent-assisted visual confirmation -----------------------------
# Move the pointer to a known spot, screenshot, and let a vision LLM judge
# the framebuffer. The hw-plane cursor is off-scanout, so the EXPECTED
# software-cursor count is 0 (gl profile) — a cursor drawn into the
# screenshot is the double-cursor regression. The agent also confirms the
# desktop painted with a top bar, catching paint failures a pixel-count
# threshold can miss.
read -r SW SH < <(python3 - "$shot" <<'PY'
import sys
from PIL import Image
w, h = Image.open(sys.argv[1]).size
print(w, h)
PY
)
export QDWIN_SCREEN_W=$SW QDWIN_SCREEN_H=$SH
midx=$(( SW / 2 )); midy=$(( SH / 2 ))
qdwin_mouse_move "$midx" "$midy"
sleep 1.0
ashot="$ART/qdwin-cursor-paint-agent-$$.png"
qdwin_screenshot "$ashot" >/dev/null

if [ "$QDWIN_SKIP_AGENT" = 1 ]; then
    echo "SKIP: agent vision step (QDWIN_SKIP_AGENT=1). Artifact: $ashot"
    echo
    echo "ALL DETERMINISTIC DISCRIMINATORS PASSED on $VMNAME"
    exit 0
fi

prompt="You are verifying a Wayland desktop screenshot from an automated GUI test.
Read the PNG image at: $ashot
It is a $SW x $SH framebuffer capture of the qdwin/qdshell desktop, taken
just after the mouse pointer was moved to the screen center ($midx,$midy).
The hardware mouse cursor is rendered on a separate DRM overlay plane and is
NORMALLY NOT present in this framebuffer capture.
Report ONLY a single compact JSON object on the last line, no prose:
{\"painted\": <true if the desktop shows real content, false if black/blank>,
 \"top_bar\": <true if a horizontal status/menu bar is visible along the top edge>,
 \"cursor_count\": <integer: number of distinct mouse-pointer/arrow cursor
   glyphs visibly drawn INTO this image>}"

AGENT_CMD=${QDWIN_AGENT_CMD:-claude -p --allowedTools Read --permission-mode acceptEdits}
echo "agent-verify: invoking vision LLM on $ashot ..."
verdict_raw=$(timeout "${QDWIN_AGENT_TIMEOUT:-240}" $AGENT_CMD "$prompt" 2>/dev/null || true)
verdict=$(python3 - <<PY
import sys, json, re
raw = '''$verdict_raw'''
objs = re.findall(r'\{[^{}]*\}', raw)
for o in reversed(objs):
    try:
        d = json.loads(o)
        if 'painted' in d and 'cursor_count' in d:
            print(json.dumps(d)); break
    except Exception:
        continue
PY
)
[ -n "$verdict" ] || setup_fail "vision agent returned no parseable JSON verdict. Raw:\n$verdict_raw\nArtifact: $ashot (set QDWIN_SKIP_AGENT=1 to bypass)"
echo "agent verdict: $verdict"

painted=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('painted'))" "$verdict")
topbar=$(python3 -c "import json,sys; print(json.loads(sys.argv[1]).get('top_bar'))" "$verdict")
ccount=$(python3 -c "import json,sys; print(int(json.loads(sys.argv[1]).get('cursor_count',-1)))" "$verdict")

[ "$painted" = True ] || fail "vision agent: desktop NOT painted (painted=$painted). Artifact: $ashot"
pass "vision agent: desktop is painted"
[ "$topbar" = True ] || fail "vision agent: top bar NOT visible (top_bar=$topbar) — qdshell bar not drawn. Artifact: $ashot"
pass "vision agent: top bar visible"
[ "$ccount" = "$QDWIN_CURSOR_IN_SCANOUT" ] \
    || fail "vision agent: $ccount cursor(s) drawn into scanout, expected $QDWIN_CURSOR_IN_SCANOUT (double-cursor regression if >expected). Artifact: $ashot"
pass "vision agent: exactly $ccount software cursor(s) in scanout (expected $QDWIN_CURSOR_IN_SCANOUT)"

echo
echo "ALL DISCRIMINATORS PASSED on $VMNAME"
