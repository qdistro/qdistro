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

@test "tier-2 presentation bind is shared by both homes and keep-id owner" {
    python3 - "$SPAWN" <<'PY'
from pathlib import Path
import re
import sys

WANTED = (
    "-v /var/lib/qdistro/presentation:"
    "/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec"
)
HOME_MARKERS = (
    "TIER2_STATE_PATH_RESOLVED",
    "TIER2_DISPOSABLE_RESOLVED",
)


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


def parse_condition(keyword: str, code: str) -> str:
    rest = code[len(keyword) :].strip()
    if rest.endswith("; then"):
        rest = rest[: -len("; then")].strip()
    elif rest.endswith("then"):
        rest = rest[: -len("then")].strip()
    return rest


def check_wrapper(wrapper: str) -> None:
    if_stack: list[str] = []
    hits = []
    persistent_if_line = None
    disposable_elif_line = None
    args_lines: list[str] = []
    in_args = False
    args_depth = 0

    for lineno, raw in enumerate(wrapper.splitlines(), 1):
        code = code_of(raw)
        if not code:
            continue
        if in_args:
            args_lines.append(code)
            args_depth += code.count("(") - code.count(")")
            if args_depth <= 0:
                in_args = False
            continue
        if code.startswith("PODMAN_ARGS=("):
            in_args = True
            args_depth = code.count("(") - code.count(")")
            args_lines.append(code)
            if args_depth <= 0:
                in_args = False
            continue
        if re.match(r"^if\b", code):
            if_stack.append(parse_condition("if", code))
            if "TIER2_STATE_PATH_RESOLVED" in if_stack[-1]:
                persistent_if_line = lineno
            continue
        if re.match(r"^elif\b", code):
            if not if_stack:
                raise SystemExit(f"elif without if at wrapper:{lineno}")
            if_stack[-1] = parse_condition("elif", code)
            if "TIER2_DISPOSABLE_RESOLVED" in if_stack[-1]:
                disposable_elif_line = lineno
            continue
        if code == "else" or code.startswith("else;"):
            if not if_stack:
                raise SystemExit(f"else without if at wrapper:{lineno}")
            if_stack[-1] = "else:" + if_stack[-1]
            continue
        if code == "fi" or code.startswith("fi;"):
            if not if_stack:
                raise SystemExit(f"fi without if at wrapper:{lineno}")
            if_stack.pop()
            continue
        if "qdistro/presentation" in code:
            hits.append((lineno, list(if_stack), code))

    if any("current.json" in h[2] for h in hits):
        raise SystemExit(f"presentation bind mounts the file, not the directory: {hits!r}")
    volume = [h for h in hits if WANTED in h[2]]
    if len(volume) != 1:
        raise SystemExit(f"expected one presentation volume line, got {hits!r}")
    lineno, stack, code = volume[0]
    if any(":Z" in h[2] or ":z" in h[2] for h in hits):
        raise SystemExit(f"presentation bind uses SELinux relabel: {hits!r}")
    home_on_stack = [c for c in stack if any(m in c for m in HOME_MARKERS)]
    if home_on_stack:
        raise SystemExit(
            f"presentation bind is inside a home-mode branch {home_on_stack!r} at wrapper:{lineno}"
        )
    if persistent_if_line is None or disposable_elif_line is None:
        raise SystemExit(
            "wrapper is missing persistent or disposable home-mode branches"
        )
    if not (lineno < persistent_if_line and lineno < disposable_elif_line):
        raise SystemExit(
            f"presentation bind at wrapper:{lineno} is not before both home-mode "
            f"branches (persistent={persistent_if_line}, disposable={disposable_elif_line})"
        )
    args_text = "\n".join(args_lines)
    if "--userns=keep-id" not in args_text:
        raise SystemExit("PODMAN_ARGS is missing --userns=keep-id")
    if '"${TIER2_ADMIN_UID_RESOLVED}:${TIER2_ADMIN_UID_RESOLVED}"' not in args_text:
        raise SystemExit("PODMAN_ARGS is missing --user TIER2_ADMIN_UID_RESOLVED")
    if '"${PODMAN_HARDENING[@]}"' not in args_text:
        raise SystemExit("PODMAN_ARGS does not splice PODMAN_HARDENING")


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
        -v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec
    )
fi
if [ -n "${TIER2_STATE_PATH_RESOLVED:-}" ]; then
    PODMAN_HARDENING+=( -v "$TIER2_STATE_PATH_RESOLVED:/home/admin:rw" )
elif [ "${TIER2_DISPOSABLE_RESOLVED:-0}" = 1 ]; then
    PODMAN_HARDENING+=( --mount type=tmpfs,destination=/home/admin,tmpfs-size=256m,tmpfs-mode=0700,U )
fi
PODMAN_ARGS=(
    run
    --userns=keep-id
    --user "${TIER2_ADMIN_UID_RESOLVED}:${TIER2_ADMIN_UID_RESOLVED}"
    "${PODMAN_HARDENING[@]}"
)
"""
check_wrapper(GOOD)

inside_persistent = GOOD.replace(
    'if [ -n "${TIER2_STATE_PATH_RESOLVED:-}" ]; then\n'
    '    PODMAN_HARDENING+=( -v "$TIER2_STATE_PATH_RESOLVED:/home/admin:rw" )',
    'if [ -n "${TIER2_STATE_PATH_RESOLVED:-}" ]; then\n'
    '    PODMAN_HARDENING+=(\n'
    '        -v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec\n'
    '    )\n'
    '    PODMAN_HARDENING+=( -v "$TIER2_STATE_PATH_RESOLVED:/home/admin:rw" )',
).replace(
    'if [ -d /var/lib/qdistro/presentation ]; then\n'
    '    PODMAN_HARDENING+=(\n'
    '        -v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec\n'
    '    )\n'
    'fi\n',
    '',
)
expect_fail(inside_persistent, "inside a home-mode branch")

inside_disposable = GOOD.replace(
    'elif [ "${TIER2_DISPOSABLE_RESOLVED:-0}" = 1 ]; then\n'
    '    PODMAN_HARDENING+=( --mount type=tmpfs,destination=/home/admin,tmpfs-size=256m,tmpfs-mode=0700,U )',
    'elif [ "${TIER2_DISPOSABLE_RESOLVED:-0}" = 1 ]; then\n'
    '    PODMAN_HARDENING+=(\n'
    '        -v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec\n'
    '    )\n'
    '    PODMAN_HARDENING+=( --mount type=tmpfs,destination=/home/admin,tmpfs-size=256m,tmpfs-mode=0700,U )',
).replace(
    'if [ -d /var/lib/qdistro/presentation ]; then\n'
    '    PODMAN_HARDENING+=(\n'
    '        -v /var/lib/qdistro/presentation:/var/lib/qdistro/presentation:ro,nodev,nosuid,noexec\n'
    '    )\n'
    'fi\n',
    '',
)
expect_fail(inside_disposable, "inside a home-mode branch")

expect_fail(GOOD.replace("--userns=keep-id\n", ""), "missing --userns=keep-id")
expect_fail(
    GOOD.replace(
        '"${TIER2_ADMIN_UID_RESOLVED}:${TIER2_ADMIN_UID_RESOLVED}"',
        '"0:0"',
    ),
    "missing --user TIER2_ADMIN_UID_RESOLVED",
)
expect_fail(
    GOOD.replace('"${PODMAN_HARDENING[@]}"', ""),
    "does not splice PODMAN_HARDENING",
)
expect_fail(
    GOOD.replace(
        "/var/lib/qdistro/presentation:/var/lib/qdistro/presentation:",
        "/var/lib/qdistro/presentation/current.json:/var/lib/qdistro/presentation/current.json:",
    ),
    "file, not the directory",
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
