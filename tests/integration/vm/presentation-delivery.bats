#!/usr/bin/env bats
# qci:host-only — runs on the host in the bats gate, no VM (ci/lib/gates/bats.sh).
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

wanted = "-v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec,rprivate"
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

@test "tier-2 presentation bind is private and excludes sibling trees" {
    python3 - "$SPAWN" <<'PY'
from pathlib import Path
import re
import sys

PRES = "/var/lib/qdistro/presentation"
PREFIX = "/var/lib/qdistro"
NEED_OPTS = {"ro", "nodev", "nosuid", "noexec", "rprivate"}
FORBID_OPTS = {"Z", "z"}
VOLUME_RE = re.compile(r"(?:^|\s)-v\s+(\S+)")
MOUNT_RE = re.compile(r"(?:^|\s)--mount\s+(\S+)")


def extract_wrapper_body(src: str) -> str:
    marker = "WRAPPER_BODY='"
    start = src.find(marker)
    if start < 0:
        raise SystemExit("WRAPPER_BODY assignment missing")
    i = start + len(marker)
    out = []
    while i < len(src):
        if src.startswith("'\"'\"'", i):
            out.append("'")
            i += 5
            continue
        if src[i] == "'":
            return "".join(out)
        out.append(src[i])
        i += 1
    raise SystemExit("unterminated WRAPPER_BODY")


def code_of(raw: str) -> str:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    return stripped.split("#", 1)[0].rstrip()


def active_text(wrapper: str) -> str:
    lines = []
    for raw in wrapper.splitlines():
        code = code_of(raw)
        if code:
            lines.append(code)
    return " ".join(lines)


def unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
        return token[1:-1]
    return token


def dest_kind(dst: str) -> str:
    dst = dst.rstrip("/")
    if dst == PRES:
        return "presentation"
    if dst == PREFIX or dst.startswith(PREFIX + "/"):
        return "sibling"
    return "other"


def split_bind(spec: str) -> tuple[str, str, str]:
    spec = unquote(spec)
    parts = spec.split(":")
    if len(parts) < 2:
        raise SystemExit(f"bind spec missing destination: {spec}")
    src, dst = parts[0], parts[1]
    opts = parts[2] if len(parts) > 2 else ""
    return src, dst, opts


def mount_destination(spec: str) -> str:
    spec = unquote(spec)
    for item in spec.split(","):
        if item.startswith("destination="):
            return item.split("=", 1)[1]
    return ""


def check_wrapper(wrapper: str) -> None:
    text = active_text(wrapper)
    if "current.json" in text and ("-v " in text or "--mount" in text):
        for spec in VOLUME_RE.findall(text):
            if "current.json" in spec:
                raise SystemExit(
                    f"presentation bind mounts the file, not the directory: {spec}"
                )
        for spec in MOUNT_RE.findall(text):
            if "current.json" in spec:
                raise SystemExit(
                    f"presentation bind mounts the file, not the directory: {spec}"
                )
    presentation = []
    for spec in VOLUME_RE.findall(text):
        src, dst, opts = split_bind(spec)
        kind = dest_kind(dst)
        if kind == "sibling":
            raise SystemExit(f"sibling /var/lib/qdistro volume destination: {spec}")
        if kind == "presentation":
            presentation.append((src, dst, opts, spec))
    for spec in MOUNT_RE.findall(text):
        dst = mount_destination(spec)
        if not dst:
            continue
        kind = dest_kind(dst)
        if kind == "sibling":
            raise SystemExit(f"sibling /var/lib/qdistro volume destination: {spec}")
        if kind == "presentation":
            raise SystemExit(
                f"presentation bind must use -v, not --mount: {spec}"
            )
    if len(presentation) != 1:
        raise SystemExit(
            f"expected one presentation volume line, got {presentation!r}"
        )
    src, _dst, opts, spec = presentation[0]
    if src != PRES:
        raise SystemExit(f"presentation bind source is not {PRES}: {spec}")
    tokens = {t for t in opts.split(",") if t}
    missing = NEED_OPTS - tokens
    if missing:
        raise SystemExit(
            f"presentation bind missing {sorted(missing)}: {spec}"
        )
    extra = tokens & FORBID_OPTS
    if extra:
        raise SystemExit(f"presentation bind uses SELinux relabel: {spec}")


def expect_fail(wrapper: str, needle: str) -> None:
    try:
        check_wrapper(wrapper)
    except SystemExit as exc:
        msg = str(exc)
        if needle not in msg:
            raise SystemExit(f"expected {needle!r} in {msg!r}") from None
        return
    raise SystemExit(f"checker accepted a broken wrapper; wanted {needle!r}")


GOOD = r"""
if [ -d /var/lib/qdistro/presentation ]; then
    PODMAN_HARDENING+=(
        -v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec,rprivate
    )
fi
if [ -n "${TIER2_STATE_PATH_RESOLVED:-}" ]; then
    PODMAN_HARDENING+=( -v "$TIER2_STATE_PATH_RESOLVED:/home/admin:rw" )
fi
PODMAN_ARGS=(
    run
    "${PODMAN_HARDENING[@]}"
    -v "$TIER2_PERCONT_DIR:/run/user/${TIER2_ADMIN_UID_RESOLVED}:rw"
)
"""
check_wrapper(GOOD)

expect_fail(
    GOOD.replace(",rprivate", ""),
    "missing ['rprivate']",
)
expect_fail(
    GOOD.replace(
        "-v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec,rprivate",
        "-v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec,rprivate\n"
        "        -v /var/lib/qdistro/bindings:/var/lib/qdistro/bindings:ro",
    ),
    "sibling /var/lib/qdistro volume destination",
)
expect_fail(
    GOOD.replace(
        "-v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec,rprivate",
        "-v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec,rprivate\n"
        "        -v /var/lib/qdistro:/var/lib/qdistro:ro",
    ),
    "sibling /var/lib/qdistro volume destination",
)
expect_fail(
    GOOD.replace(
        "-v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec,rprivate",
        "-v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec,rprivate\n"
        "        --mount type=bind,source=/var/lib/qdistro/lineage,destination=/var/lib/qdistro/lineage,ro",
    ),
    "sibling /var/lib/qdistro volume destination",
)
expect_fail(
    GOOD.replace(",rprivate", ",rprivate,Z"),
    "SELinux relabel",
)
expect_fail(
    GOOD.replace(
        "/var/lib/qdistro/presentation:/var/lib/qdistro/presentation:",
        "/var/lib/qdistro/presentation/current.json:/var/lib/qdistro/presentation/current.json:",
    ),
    "file, not the directory",
)
expect_fail(
    GOOD.replace(",rprivate", "#,rprivate"),
    "missing ['rprivate']",
)

src = Path(sys.argv[1]).read_text(encoding="utf-8")
check_wrapper(extract_wrapper_body(src))
print("ok")
PY
}

@test "isolated SELinux domains may watch the snapshot and must not write it" {
    python3 - "$POLICY" <<'PY'
from collections import defaultdict
from pathlib import Path
import re
import sys

ALLOW_RE = re.compile(
    r"allow\s+(\S+)\s+(qdistro_presentation_t)\s*:\s*(\S+)\s+"
    r"(?:\{([^}]*)\}|(\S+))\s*;"
)
LEFTOVER_RE = re.compile(r"allow\s+\S+\s+qdistro_presentation_t\s*:")


def parse_allows(src: str):
    """Union permissions from complete allow statements (braced or singleton)."""
    lines = []
    for raw in src.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        code = stripped.split("#", 1)[0].rstrip()
        if code:
            lines.append(code)
    text = " ".join(lines)
    grants = defaultdict(set)
    for match in ALLOW_RE.finditer(text):
        domain, _typ, cls, braced, single = match.groups()
        tokens = braced.split() if braced is not None else [single]
        grants[(domain, cls)].update(tokens)
    remainder = ALLOW_RE.sub(" ", text)
    leftover = LEFTOVER_RE.search(remainder)
    if leftover is not None:
        snippet = remainder[leftover.start() : leftover.start() + 120].strip()
        raise SystemExit(
            f"unparsed allow involving qdistro_presentation_t: {snippet}"
        )
    return grants


grants = parse_allows(Path(sys.argv[1]).read_text(encoding="utf-8"))

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

braced_extra = parse_allows(
    """
# allow container_t qdistro_presentation_t:dir { getattr search open read watch };
allow container_t qdistro_presentation_t:dir { getattr search open read watch };
allow container_t qdistro_presentation_t:dir { write unlink };
"""
)
if "write" not in braced_extra[("container_t", "dir")]:
    raise SystemExit("parser missed extra braced write grant")
if "watch" not in braced_extra[("container_t", "dir")]:
    raise SystemExit("parser missed active watch grant")

commented_only = parse_allows(
    """
# allow container_t qdistro_presentation_t:dir { getattr search open read watch };
"""
)
if commented_only[("container_t", "dir")]:
    raise SystemExit("parser treated a commented allow as active")

singleton_extra = parse_allows(
    """
allow container_t qdistro_presentation_t:dir { getattr search open read watch };
allow container_t qdistro_presentation_t:dir write;
allow container_t qdistro_presentation_t:file { getattr open read };
allow container_t qdistro_presentation_t:file unlink;
"""
)
if "write" not in singleton_extra[("container_t", "dir")]:
    raise SystemExit("parser missed singleton dir write grant")
if "unlink" not in singleton_extra[("container_t", "file")]:
    raise SystemExit("parser missed singleton file unlink grant")
if "watch" not in singleton_extra[("container_t", "dir")]:
    raise SystemExit("parser missed active watch next to singleton write")

multiline_extra = parse_allows(
    """
allow container_t qdistro_presentation_t:dir { getattr search open read watch };
allow container_t qdistro_presentation_t:dir {
    write
};
"""
)
if "write" not in multiline_extra[("container_t", "dir")]:
    raise SystemExit("parser missed multiline extra write grant")
print("ok")
PY
}

@test "isolation probe restores original snapshot state on host-safe fixtures" {
    run bash "$REPO/tests/integration/vm/probes/presentation-isolation.sh" --host-cleanup-self-test
    [ "$status" -eq 0 ]
    [ "$output" = "ok" ]
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
