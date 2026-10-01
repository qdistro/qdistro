#!/usr/bin/env bats
# Host-only lock-in of presentation delivery: installer layout, tier-2 bind
# construction, isolated-domain SELinux rights, and affected-gate mapping.
# No VM, no root, no live /var/lib/qdistro writes.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    INSTALLER="$REPO/scripts/install/install-presentation-for-vm.sh"
    SRC="$REPO/sdk/presentation/qdistro_presentation"
    SPAWN="$REPO/tier2/spawn-tier2.sh"
    POLICY="$REPO/selinux/presentation/qdistro_presentation.te"
    AFFECTED="$REPO/ci/lib/affected.sh"
    ROOT="$BATS_TEST_TMPDIR/root"
}

@test "presentation installer stages dir, metadata, package, and publisher without current.json" {
    run env DESTDIR="$ROOT" bash "$INSTALLER" "$SRC"
    [ "$status" -eq 0 ]
    [[ "$output" == *"[install-presentation] OK"* ]]

    local dir="$ROOT/var/lib/qdistro/presentation"
    local meta="$ROOT/usr/share/qdistro/presentation/deployment.json"
    local pub="$ROOT/usr/bin/qdistro-presentation-publish"
    [ -d "$dir" ]
    [ "$(stat -c %a "$dir")" = 755 ]
    [ ! -e "$dir/current.json" ]
    [ -f "$meta" ]
    [ "$(stat -c %a "$meta")" = 644 ]
    grep -qx '{"version":1,"admin_uid":1000}' "$meta"
    [ -x "$pub" ]
    grep -q 'from qdistro_presentation.cli import main' "$pub"
    [ -f "$ROOT$(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")/qdistro_presentation/__init__.py" ]
    [ -f "$ROOT$(/usr/bin/python3 -c "import sysconfig; print(sysconfig.get_paths()['purelib'])")/qdistro_presentation/qt.py" ]
}

@test "presentation installer refuses a missing package before staging files" {
    mkdir -p "$BATS_TEST_TMPDIR/empty"
    run env DESTDIR="$ROOT" bash "$INSTALLER" "$BATS_TEST_TMPDIR/empty"
    [ "$status" -eq 2 ]
    [[ "$output" == *"package not found"* ]]
    [ ! -e "$ROOT/var/lib/qdistro/presentation" ]
    [ ! -e "$ROOT/usr/bin/qdistro-presentation-publish" ]
}

@test "presentation installer rejects DESTDIR=/ and a relative DESTDIR" {
    run env DESTDIR=/ bash "$INSTALLER" "$SRC"
    [ "$status" -eq 2 ]
    [[ "$output" == *"DESTDIR=/"* ]]

    run env DESTDIR=relative bash "$INSTALLER" "$SRC"
    [ "$status" -eq 2 ]
    [[ "$output" == *"absolute path"* ]]
}

@test "tier-2 spawn binds the presentation directory read-only without :Z" {
    grep -F 'if [ -d /var/lib/qdistro/presentation ]; then' "$SPAWN"
    grep -F -- '-v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec' "$SPAWN"
    grep -F 'Mount the directory (not current.json)' "$SPAWN"
    # Any presentation volume that carries a SELinux relabel is a contract break.
    if awk '
        /qdistro\/presentation/ {
            if ($0 ~ /:Z|:z/) { found=1 }
        }
        END { exit found ? 0 : 1 }
    ' "$SPAWN"; then
        echo "spawn-tier2.sh presentation bind uses :Z/:z" >&2
        awk '/qdistro\/presentation/' "$SPAWN" >&2
        return 1
    fi
}

@test "isolated SELinux domains may watch the snapshot and must not write it" {
    python3 - "$POLICY" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text(encoding="utf-8")
# Isolated consumers: read + watch, never mutate.
for domain in ("qdistro_tier1_t", "container_t", "qdistro_tier2_t"):
    needle = f"allow {domain} qdistro_presentation_t:dir"
    if needle not in text:
        raise SystemExit(f"missing dir allow for {domain}")
    line = next(line for line in text.splitlines() if needle in line)
    for perm in ("getattr", "search", "open", "read", "watch"):
        if perm not in line:
            raise SystemExit(f"{domain} dir allow missing {perm}: {line}")
    for perm in ("write", "add_name", "remove_name", "create", "unlink", "rename"):
        if perm in line:
            raise SystemExit(f"{domain} dir allow includes {perm}: {line}")
    file_needle = f"allow {domain} qdistro_presentation_t:file"
    file_line = next(line for line in text.splitlines() if file_needle in line)
    for perm in ("getattr", "open", "read"):
        if perm not in file_line:
            raise SystemExit(f"{domain} file allow missing {perm}: {file_line}")
    for perm in ("write", "create", "unlink", "rename", "setattr"):
        if perm in file_line:
            raise SystemExit(f"{domain} file allow includes {perm}: {file_line}")
print("ok")
PY
}

@test "affected map selects host and bats for the library and installer" {
    python3 - "$AFFECTED" <<'PY'
from pathlib import Path
import sys
text = Path(sys.argv[1]).read_text(encoding="utf-8")
for needle in ("sdk/presentation/*)", "scripts/install/install-presentation-for-vm.sh)"):
    idx = text.find(needle)
    if idx < 0:
        raise SystemExit(f"missing arm {needle}")
    chunk = text[idx : idx + 240]
    if "host\\nbats" not in chunk:
        raise SystemExit(f"{needle} does not select host+bats:\n{chunk}")
print("ok")
PY
}
