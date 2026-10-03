#!/usr/bin/env bats
# qci:host-only — runs on the host in the bats gate, no VM (ci/lib/gates/bats.sh).
# Host-only lock-in of presentation delivery: installer layout, tier-2 bind
# construction, silo launchers dropping QDISTRO_PRESENTATION_FILE,
# isolated-domain SELinux rights, and affected-gate mapping.
# No VM, no root, no live /var/lib/qdistro writes.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    INSTALLER="$REPO/scripts/install/install-presentation-for-vm.sh"
    SRC="$REPO/sdk/presentation/qdistro_presentation"
    SPAWN="$REPO/tier2/spawn-tier2.sh"
    SPAWN_TIER1="$REPO/selinux/tier1/spawn-tier1.sh"
    SPAWN_TIER3="$REPO/tier3/spawn-tier3.sh"
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

@test "silo spawners drop QDISTRO_PRESENTATION_FILE" {
    python3 - "$SPAWN" "$SPAWN_TIER1" "$SPAWN_TIER3" <<'PY'
from pathlib import Path
import re
import sys

OVERRIDE = "QDISTRO_PRESENTATION_FILE"
STRIP = f"-u {OVERRIDE}"


def code_of(raw: str) -> str:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        return ""
    return stripped.split("#", 1)[0].rstrip()


def active_lines(src: str) -> list[tuple[int, str]]:
    out = []
    for lineno, raw in enumerate(src.splitlines(), 1):
        code = code_of(raw)
        if code:
            out.append((lineno, code))
    return out


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


def join_continuations(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Join active lines that end with a backslash into one command."""
    joined: list[tuple[int, str]] = []
    buf = ""
    start = 0
    for lineno, code in lines:
        if not buf:
            start = lineno
        if code.endswith("\\"):
            buf += code[:-1] + " "
            continue
        buf += code
        joined.append((start, " ".join(buf.split())))
        buf = ""
    if buf:
        raise SystemExit("unterminated backslash continuation")
    return joined


def env_cmds(src: str) -> list[tuple[int, str]]:
    cmds = []
    for lineno, code in join_continuations(active_lines(src)):
        if re.search(r"(^|exec\s+|;\s*)env\b", code) or code.startswith("env "):
            cmds.append((lineno, code))
        elif " env " in code or code.startswith("env"):
            cmds.append((lineno, code))
    return cmds


def require_strip_on_env(src: str, predicate, label: str) -> None:
    hits = [(ln, code) for ln, code in env_cmds(src) if predicate(code)]
    if not hits:
        raise SystemExit(f"{label}: no matching env launch")
    for lineno, code in hits:
        tokens = code.split()
        stripped = any(
            tokens[i] == "-u" and i + 1 < len(tokens) and tokens[i + 1] == OVERRIDE
            for i in range(len(tokens))
        )
        if not stripped:
            raise SystemExit(
                f"{label} env at line {lineno} is missing {STRIP}: {code}"
            )


def check_tier1(src: str) -> None:
    require_strip_on_env(
        src,
        lambda c: "QDISTRO_TIER1_TITLE_PREFIX" in c,
        "spawn-tier1",
    )


def check_tier3(src: str) -> None:
    require_strip_on_env(
        src,
        lambda c: "runuser" in c and "waypipe" in c and "server" in c,
        "spawn-tier3 silo",
    )


def podman_args_tokens(wrapper: str) -> list[str]:
    args_lines: list[str] = []
    in_args = False
    args_depth = 0
    for _lineno, raw in enumerate(wrapper.splitlines(), 1):
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
    return "\n".join(args_lines).split()


def check_tier2_wrapper(wrapper: str) -> None:
    require_strip_on_env(
        wrapper,
        lambda c: "podman" in c and ("exec env" in c or c.startswith("exec env")),
        "spawn-tier2 wrapper",
    )
    tokens = podman_args_tokens(wrapper)
    if not tokens:
        raise SystemExit("PODMAN_ARGS missing")
    if "--env-host" in tokens:
        raise SystemExit("PODMAN_ARGS forwards host env via --env-host")
    for i, tok in enumerate(tokens):
        if tok in ("-e", "--env") and i + 1 < len(tokens) and OVERRIDE in tokens[i + 1]:
            raise SystemExit(f"PODMAN_ARGS forwards {OVERRIDE}: {tokens[i]} {tokens[i + 1]}")
        if tok.startswith("-e") and OVERRIDE in tok:
            raise SystemExit(f"PODMAN_ARGS forwards {OVERRIDE}: {tok}")
        if tok.startswith("--env=") and OVERRIDE in tok:
            raise SystemExit(f"PODMAN_ARGS forwards {OVERRIDE}: {tok}")


def expect_fail(fn, src: str, needle: str) -> None:
    try:
        fn(src)
    except SystemExit as exc:
        msg = str(exc)
        if needle not in msg:
            raise SystemExit(f"expected {needle!r} in {msg!r}") from None
        return
    raise SystemExit(f"checker accepted a broken source; wanted {needle!r}")


TIER1_GOOD = r'''
if [ -n "$LAUNCHREC_PATH" ]; then
    env -u QDISTRO_PRESENTATION_FILE \
        QDISTRO_TIER1_TITLE_PREFIX="$TITLE_PREFIX" \
        "${CMD[@]}" &
fi
exec env -u QDISTRO_PRESENTATION_FILE \
    QDISTRO_TIER1_TITLE_PREFIX="$TITLE_PREFIX" "${CMD[@]}"
'''
check_tier1(TIER1_GOOD)
expect_fail(
    check_tier1,
    TIER1_GOOD.replace("-u QDISTRO_PRESENTATION_FILE", "-u PYTHONPATH"),
    "missing -u QDISTRO_PRESENTATION_FILE",
)
expect_fail(
    check_tier1,
    TIER1_GOOD.replace(
        "exec env -u QDISTRO_PRESENTATION_FILE \\\n"
        '    QDISTRO_TIER1_TITLE_PREFIX="$TITLE_PREFIX" "${CMD[@]}"',
        'exec env QDISTRO_TIER1_TITLE_PREFIX="$TITLE_PREFIX" "${CMD[@]}"',
    ),
    "missing -u QDISTRO_PRESENTATION_FILE",
)
expect_fail(
    check_tier1,
    TIER1_GOOD.replace(
        "    env -u QDISTRO_PRESENTATION_FILE \\\n"
        '        QDISTRO_TIER1_TITLE_PREFIX="$TITLE_PREFIX" \\\n'
        '        "${CMD[@]}" &',
        "    env QDISTRO_TIER1_TITLE_PREFIX=\"$TITLE_PREFIX\" \\\n"
        '        "${CMD[@]}" &',
    ),
    "missing -u QDISTRO_PRESENTATION_FILE",
)
commented = TIER1_GOOD.replace(
    "-u QDISTRO_PRESENTATION_FILE",
    "# -u QDISTRO_PRESENTATION_FILE",
)
expect_fail(check_tier1, commented, "no matching env launch")

TIER3_GOOD = r'''
"${NETNS_PREFIX[@]}" runuser -u "$SILO" -- env -u QDISTRO_PRESENTATION_FILE \
    XDG_RUNTIME_DIR="$SILO_RUNTIME" \
    HOME="$SILO_HOME" \
    waypipe "${SERVER_OPTS[@]}" server -- "$@" >"$SERVER_LOG" 2>&1 &
'''
check_tier3(TIER3_GOOD)
expect_fail(
    check_tier3,
    TIER3_GOOD.replace("-u QDISTRO_PRESENTATION_FILE", "-u PYTHONPATH"),
    "missing -u QDISTRO_PRESENTATION_FILE",
)
expect_fail(
    check_tier3,
    TIER3_GOOD.replace("env -u QDISTRO_PRESENTATION_FILE \\", "env \\"),
    "missing -u QDISTRO_PRESENTATION_FILE",
)

TIER2_GOOD = r'''
PODMAN_ARGS=(
    run
    --userns=keep-id
    -e WAYLAND_DISPLAY
)
exec env -u QDISTRO_PRESENTATION_FILE podman "${PODMAN_ARGS[@]}" "$@"
'''
check_tier2_wrapper(TIER2_GOOD)
expect_fail(
    check_tier2_wrapper,
    TIER2_GOOD.replace("exec env -u QDISTRO_PRESENTATION_FILE podman", "exec podman"),
    "no matching env launch",
)
expect_fail(
    check_tier2_wrapper,
    TIER2_GOOD.replace("-u QDISTRO_PRESENTATION_FILE", "-u PYTHONPATH"),
    "missing -u QDISTRO_PRESENTATION_FILE",
)
expect_fail(
    check_tier2_wrapper,
    TIER2_GOOD.replace(
        "    -e WAYLAND_DISPLAY\n",
        "    -e WAYLAND_DISPLAY\n    --env-host\n",
    ),
    "--env-host",
)
expect_fail(
    check_tier2_wrapper,
    TIER2_GOOD.replace(
        "    -e WAYLAND_DISPLAY\n",
        "    -e WAYLAND_DISPLAY\n    -e QDISTRO_PRESENTATION_FILE\n",
    ),
    "forwards QDISTRO_PRESENTATION_FILE",
)
expect_fail(
    check_tier2_wrapper,
    TIER2_GOOD.replace(
        "    -e WAYLAND_DISPLAY\n",
        '    -e WAYLAND_DISPLAY\n    --env=QDISTRO_PRESENTATION_FILE=/tmp/x.json\n',
    ),
    "forwards QDISTRO_PRESENTATION_FILE",
)

tier1 = Path(sys.argv[2]).read_text(encoding="utf-8")
tier3 = Path(sys.argv[3]).read_text(encoding="utf-8")
tier2 = Path(sys.argv[1]).read_text(encoding="utf-8")
check_tier1(tier1)
check_tier3(tier3)
check_tier2_wrapper(extract_wrapper_body(tier2))
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
