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
    python3 - "$SPAWN" <<'PY'
from pathlib import Path
import sys

wanted = "-v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec"
active = []
for raw in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    code = stripped.split("#", 1)[0].rstrip()
    if "qdistro/presentation" in code:
        active.append(code)
if not any(wanted in line for line in active):
    raise SystemExit(f"no active presentation volume line, active={active!r}")
for line in active:
    if ":Z" in line or ":z" in line:
        raise SystemExit(f"presentation bind uses SELinux relabel: {line}")
print("ok")
PY
}

@test "isolated SELinux domains may watch the snapshot and must not write it" {
    python3 - "$POLICY" <<'PY'
from collections import defaultdict
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
allow_re = re.compile(
    r"^allow\s+(\S+)\s+(qdistro_presentation_t):(\S+)\s*\{([^}]+)\}"
)
grants = defaultdict(set)
for raw in text.splitlines():
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    match = allow_re.match(stripped)
    if match is None:
        continue
    domain, _typ, cls, body = match.groups()
    grants[(domain, cls)].update(body.split())

dir_need = {"getattr", "search", "open", "read", "watch"}
file_need = {"getattr", "open", "read"}
dir_forbid = {"write", "add_name", "remove_name", "create", "unlink", "rename"}
file_forbid = {"write", "create", "unlink", "rename", "setattr"}
for domain in ("qdistro_tier1_t", "container_t", "qdistro_tier2_t"):
    have_dir = grants[(domain, "dir")]
    have_file = grants[(domain, "file")]
    missing = dir_need - have_dir
    if missing:
        raise SystemExit(f"{domain} dir missing {sorted(missing)}: {sorted(have_dir)}")
    extra = have_dir & dir_forbid
    if extra:
        raise SystemExit(f"{domain} dir has mutation perms {sorted(extra)}")
    missing_f = file_need - have_file
    if missing_f:
        raise SystemExit(f"{domain} file missing {sorted(missing_f)}: {sorted(have_file)}")
    extra_f = have_file & file_forbid
    if extra_f:
        raise SystemExit(f"{domain} file has mutation perms {sorted(extra_f)}")

# Parser fixtures: a commented grant is inactive; a second write grant counts.
sample = """
# allow container_t qdistro_presentation_t:dir { getattr search open read watch };
allow container_t qdistro_presentation_t:dir { getattr search open read watch };
allow container_t qdistro_presentation_t:dir { write unlink };
"""
sample_grants = defaultdict(set)
for raw in sample.splitlines():
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    match = allow_re.match(stripped)
    if match is None:
        continue
    domain, _typ, cls, body = match.groups()
    sample_grants[(domain, cls)].update(body.split())
if "write" not in sample_grants[("container_t", "dir")]:
    raise SystemExit("parser missed extra write grant")
if "watch" not in sample_grants[("container_t", "dir")]:
    raise SystemExit("parser missed active watch grant")
commented_only = """
# allow container_t qdistro_presentation_t:dir { getattr search open read watch };
"""
commented_grants = defaultdict(set)
for raw in commented_only.splitlines():
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    match = allow_re.match(stripped)
    if match is None:
        continue
    domain, _typ, cls, body = match.groups()
    commented_grants[(domain, cls)].update(body.split())
if commented_grants[("container_t", "dir")]:
    raise SystemExit("parser treated a commented allow as active")
print("ok")
PY
}

@test "affected map selects host and bats for the library and installer" {
    python3 - "$AFFECTED" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")

def arm_body(pattern: str) -> str:
    idx = text.find(pattern)
    if idx < 0:
        raise SystemExit(f"missing arm {pattern}")
    rest = text[idx + len(pattern) :]
    end = rest.find(";;")
    if end < 0:
        raise SystemExit(f"unclosed arm {pattern}")
    return rest[:end]

sdk = arm_body("sdk/presentation/*)")
inst = arm_body("scripts/install/install-presentation-for-vm.sh)")
if "install-presentation-for-vm.sh" in sdk:
    raise SystemExit("sdk arm window leaked into the installer arm")
if "tier2/*" in inst or "tier3/*" in inst:
    raise SystemExit("installer arm window leaked into tier2/tier3")
for name, body in (("sdk", sdk), ("installer", inst)):
    if "printf 'host\\nbats\\n'" not in body:
        raise SystemExit(f"{name} arm does not printf host+bats:\n{body}")
    if "printf 'host\\n'" in body:
        raise SystemExit(f"{name} arm still prints host-only:\n{body}")
print("ok")
PY
}
