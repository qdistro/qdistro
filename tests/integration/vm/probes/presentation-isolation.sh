#!/bin/bash
# In-VM driver for presentation-isolation.bats.
# Exercises the installed public snapshot: import, publish, DAC denial,
# symlink rejection, last-known-good, live watch, polkit/locker override ignore.
# Does not require a compositor or a tier-2 image.
set -u

PASSCOUNT=0
FAILCOUNT=0

pass() { echo "PASS: $*"; PASSCOUNT=$((PASSCOUNT + 1)); }
fail() { echo "FAIL: $*"; FAILCOUNT=$((FAILCOUNT + 1)); }
die() { fail "$*"; echo "[presentation-isolation] $PASSCOUNT passes, $FAILCOUNT failures"; exit 1; }

DIR=/var/lib/qdistro/presentation
FILE=$DIR/current.json
META=/usr/share/qdistro/presentation/deployment.json
ORIG_BACKUP=$DIR/current.json.p7orig
HAD_ORIGINAL=0

cleanup() {
    rm -f "$DIR/qdistro-write-probe" "$DIR/real.json" 2>/dev/null || true
    if [ "$HAD_ORIGINAL" = 1 ] && [ -f "$ORIG_BACKUP" ]; then
        mv -f "$ORIG_BACKUP" "$FILE"
    else
        rm -f "$FILE" "$ORIG_BACKUP"
    fi
}
trap cleanup EXIT

[ -d "$DIR" ] || die "managed presentation directory missing"
[ -f "$META" ] || die "deployment.json missing"
command -v python3 >/dev/null 2>&1 || die "python3 missing"
command -v qdistro-presentation-publish >/dev/null 2>&1 || die "qdistro-presentation-publish missing"

OWNER=$(stat -c '%U:%G' "$DIR")
MODE=$(stat -c '%a' "$DIR")
[ "$OWNER" = "admin:admin" ] && pass "presentation dir owner admin:admin" \
    || fail "presentation dir owner $OWNER, expected admin:admin"
[ "$MODE" = "755" ] && pass "presentation dir mode 0755" \
    || fail "presentation dir mode $MODE, expected 0755"

META_MODE=$(stat -c '%a' "$META")
META_OWNER=$(stat -c '%U' "$META")
[ "$META_OWNER" = "root" ] && pass "deployment.json owned by root" \
    || fail "deployment.json owner $META_OWNER, expected root"
[ "$META_MODE" = "644" ] && pass "deployment.json mode 0644" \
    || fail "deployment.json mode $META_MODE, expected 0644"
if python3 - "$META" <<'PY'
import json, sys
from pathlib import Path
obj = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert obj == {"version": 1, "admin_uid": 1000}, obj
PY
then
    pass "deployment.json is version 1 admin_uid 1000"
else
    fail "deployment.json payload is not version 1 admin_uid 1000"
fi

# Import from a non-source cwd.
IMPORT_OUT=$(cd / && env -u PYTHONPATH -u QDISTRO_PRESENTATION_FILE PYTHONSAFEPATH=1 \
    python3 -c 'import qdistro_presentation; print("ok", qdistro_presentation.SCHEMA_VERSION)' \
    2>/tmp/p7-import.err) || true
if [ "$IMPORT_OUT" = "ok 1" ]; then
    pass "installed import qdistro_presentation from / without PYTHONPATH"
else
    fail "installed import failed: out='$IMPORT_OUT' err=$(tr '\n' ' ' </tmp/p7-import.err)"
fi

if [ -f "$FILE" ]; then
    cp -a "$FILE" "$ORIG_BACKUP"
    HAD_ORIGINAL=1
fi

# Distinct palettes A then B so generation must change.
publish_palette() {
    local hex=$1
    runuser -u admin -- env -u QDISTRO_PRESENTATION_FILE PYTHONSAFEPATH=1 python3 - "$hex" <<'PY'
import json, sys
from qdistro_presentation.model import DEFAULT_DARK_COLORS, example_snapshot
from qdistro_presentation.publish import write_snapshot
from dataclasses import replace

hex_color = sys.argv[1]
snap = example_snapshot()
colors = replace(snap.colors, mSurface=hex_color)
snap = replace(snap, colors=colors, enabled=True)
result = write_snapshot(
    "/var/lib/qdistro/presentation",
    snap,
    owner_uid=1000,
    skip_unchanged=False,
)
print(result.generation)
PY
}

GEN_A=$(publish_palette "#112233") || die "admin publish A failed"
[ -n "$GEN_A" ] || die "publisher printed empty generation A"
[ -f "$FILE" ] || die "publish A did not create current.json"
FILE_OWNER=$(stat -c '%U:%G' "$FILE")
FILE_MODE=$(stat -c '%a' "$FILE")
[ "$FILE_OWNER" = "admin:admin" ] && pass "current.json owner admin:admin after publish" \
    || fail "current.json owner $FILE_OWNER, expected admin:admin"
[ "$FILE_MODE" = "644" ] && pass "current.json mode 0644 after publish" \
    || fail "current.json mode $FILE_MODE, expected 0644"

if command -v getenforce >/dev/null 2>&1 || [ -d /sys/fs/selinux ]; then
    if ! semodule -l 2>/dev/null | grep -q '^qdistro_presentation\b'; then
        fail "qdistro_presentation SELinux module is not loaded"
    else
        pass "qdistro_presentation SELinux module is loaded"
    fi
    LABEL=$(ls -Zd "$FILE" 2>/dev/null | awk '{print $1}')
    case "$LABEL" in
        *:qdistro_presentation_t:*)
            pass "current.json type is qdistro_presentation_t ($LABEL)"
            ;;
        *)
            fail "current.json SELinux label '$LABEL' is not qdistro_presentation_t"
            ;;
    esac
    GEN_B=$(publish_palette "#445566") || die "admin publish B failed"
    LABEL2=$(ls -Zd "$FILE" 2>/dev/null | awk '{print $1}')
    case "$LABEL2" in
        *:qdistro_presentation_t:*)
            pass "replaced current.json keeps qdistro_presentation_t"
            ;;
        *)
            fail "replaced current.json label '$LABEL2' lost qdistro_presentation_t"
            ;;
    esac
    [ "$GEN_A" != "$GEN_B" ] && pass "second publish minted a new generation" \
        || fail "second publish reused generation $GEN_A"
else
    GEN_B=$(publish_palette "#445566") || die "admin publish B failed"
    [ "$GEN_A" != "$GEN_B" ] && pass "second publish minted a new generation" \
        || fail "second publish reused generation $GEN_A"
fi

if getent passwd work >/dev/null; then
    if runuser -u work -- touch "$DIR/qdistro-write-probe" 2>/dev/null; then
        fail "work user could create a file in the presentation directory"
        rm -f "$DIR/qdistro-write-probe"
    else
        pass "work user cannot create files in the presentation directory"
    fi
    if runuser -u work -- rm -f "$FILE" 2>/dev/null; then
        fail "work user could unlink current.json"
    else
        pass "work user cannot unlink current.json"
    fi
    if runuser -u work -- mv "$FILE" "$DIR/renamed.json" 2>/dev/null; then
        fail "work user could rename current.json"
        mv -f "$DIR/renamed.json" "$FILE" 2>/dev/null || true
    else
        pass "work user cannot rename current.json"
    fi
else
    die "work account missing; cannot prove DAC denial"
fi

# Symlink leaf is rejected even when the target is admin-owned (so a
# missing O_NOFOLLOW cannot hide behind an ownership mismatch).
cp -a "$FILE" "$DIR/real.json"
chown admin:admin "$DIR/real.json" 2>/dev/null || true
rm -f "$FILE"
ln -s "$DIR/real.json" "$FILE"
chown -h admin:admin "$FILE" 2>/dev/null || true
SYM_OUT=$(cd / && env -u PYTHONPATH -u QDISTRO_PRESENTATION_FILE PYTHONSAFEPATH=1 python3 - <<'PY'
from qdistro_presentation.paths import resolve_snapshot_path, load_snapshot
from qdistro_presentation.model import SnapshotPathError
resolved = resolve_snapshot_path(role="ordinary")
assert resolved is not None
try:
    load_snapshot(resolved)
except SnapshotPathError as exc:
    print("rejected", type(exc).__name__)
else:
    print("accepted")
PY
) || SYM_OUT="python-failed"
echo "$SYM_OUT" | grep -q "^rejected" \
    && pass "reader rejects a symlink current.json" \
    || fail "reader accepted a symlink current.json ($SYM_OUT)"
rm -f "$FILE" "$DIR/real.json"
GEN_B=$(publish_palette "#445566") || die "restore after symlink failed"

# polkit/locker ignore QDISTRO_PRESENTATION_FILE.
OVERRIDE_OUT=$(cd / && env QDISTRO_PRESENTATION_FILE=/tmp/p7-does-not-exist.json PYTHONSAFEPATH=1 python3 - <<'PY'
from qdistro_presentation.paths import resolve_snapshot_path, MANAGED_FILE
ordinary = resolve_snapshot_path(role="ordinary")
polkit = resolve_snapshot_path(role="polkit")
locker = resolve_snapshot_path(role="locker")
print("ordinary", ordinary.kind if ordinary else None, ordinary.path if ordinary else None)
print("polkit", polkit.kind if polkit else None, polkit.path if polkit else None)
print("locker", locker.kind if locker else None, locker.path if locker else None)
assert ordinary is not None and ordinary.kind == "override"
assert polkit is not None and polkit.kind == "managed" and polkit.path == MANAGED_FILE
assert locker is not None and locker.kind == "managed" and locker.path == MANAGED_FILE
print("ok")
PY
) || OVERRIDE_OUT="python-failed"
echo "$OVERRIDE_OUT" | grep -qx "ok" \
    && pass "polkit and locker ignore QDISTRO_PRESENTATION_FILE" \
    || fail "override isolation failed: $OVERRIDE_OUT"

# Last-known-good + enabled:false + live watch via the installed Qt adapter.
QT_OUT=$(cd / && env -u PYTHONPATH -u QDISTRO_PRESENTATION_FILE \
    QT_QPA_PLATFORM=offscreen PYTHONSAFEPATH=1 python3 - <<'PY'
import os
import time
from dataclasses import replace

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication
from qdistro_presentation.model import example_snapshot
from qdistro_presentation.publish import write_snapshot, write_disabled_envelope
from qdistro_presentation.qt import PresentationController, reset_controller_for_tests

DIR = "/var/lib/qdistro/presentation"
FILE = DIR + "/current.json"

def publish(surface: str):
    base = example_snapshot()
    snap = replace(base, colors=replace(base.colors, mSurface=surface), enabled=True)
    return write_snapshot(DIR, snap, owner_uid=1000, skip_unchanged=False)

def window_hex(app):
    return app.palette().color(QPalette.ColorRole.Window).name().lower()

reset_controller_for_tests()
app = QApplication.instance() or QApplication(["presentation-isolation"])
native = QPalette(app.palette())
native.setColor(QPalette.ColorRole.Window, QColor("#fedcba"))
app.setPalette(native)
app.setStyleSheet("QWidget { background: #fedcba; }")
native_window = window_hex(app)
native_ss = app.styleSheet()
assert native_window == "#fedcba"

publish("#112233")
ctrl = PresentationController(app, theme_mode="system", watch=True, role="ordinary")
assert ctrl.state.using_shared_palette is True
assert ctrl.state.colors.mSurface == "#112233"
assert window_hex(app) == "#112233", window_hex(app)
assert app.styleSheet() != native_ss
gen_a = ctrl.state.generation

os.unlink(FILE)
ctrl._reload()
assert ctrl.state.using_shared_palette is True, "deletion dropped last-known-good"
assert ctrl.state.generation == gen_a
assert window_hex(app) == "#112233", "deletion did not keep painted shared palette"
print("last-good-on-delete")

write_disabled_envelope(DIR, example_snapshot(), owner_uid=1000)
ctrl._reload()
assert ctrl.state.using_shared_palette is False, "enabled:false kept shared palette"
assert window_hex(app) == native_window, (
    f"enabled:false left painted palette {window_hex(app)} instead of native {native_window}"
)
assert app.styleSheet() == native_ss, "enabled:false left shared QSS"
print("enabled-false-clears")

publish("#112233")
ctrl._reload()
assert ctrl.state.colors.mSurface == "#112233"
result = publish("#445566")
deadline = time.time() + 5
while time.time() < deadline:
    app.processEvents()
    if ctrl.state.colors.mSurface == "#445566" and ctrl.state.generation == result.generation:
        print("watch-followed")
        break
    time.sleep(0.05)
else:
    raise SystemExit(
        f"watch did not follow replace: have {ctrl.state.colors.mSurface} {ctrl.state.generation} want #445566 {result.generation}"
    )
ctrl.stop()
reset_controller_for_tests()
print("ok")
PY
) || QT_OUT="qt-failed:$QT_OUT"

echo "$QT_OUT" | grep -qx "last-good-on-delete" \
    && pass "deletion keeps last-known-good appearance" \
    || fail "deletion last-known-good failed: $QT_OUT"
echo "$QT_OUT" | grep -qx "enabled-false-clears" \
    && pass "enabled:false restores fallback appearance" \
    || fail "enabled:false failed: $QT_OUT"
echo "$QT_OUT" | grep -qx "watch-followed" \
    && pass "running controller follows an atomic snapshot replace" \
    || fail "live watch follow failed: $QT_OUT"

if [ "$FAILCOUNT" -eq 0 ]; then
    pass "presentation isolation invariants held"
    echo "[presentation-isolation] $PASSCOUNT passes, 0 failures"
    exit 0
fi
echo "[presentation-isolation] $PASSCOUNT passes, $FAILCOUNT failures"
exit 1
