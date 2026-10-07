#!/usr/bin/env bats
# qci:host-only — runs on the host in the bats gate, no VM (ci/lib/gates/bats.sh).
# Host-only lock-in of the first-party tier-2 presentation consumer image:
# Containerfile.qfileman installs qfileman + qdistro-presentation without
# WebEngine, bakes root-owned deployment.json before USER, make-tier2-image.sh
# stages those sources into the qfileman context only, and a hardened
# seccomp profile exists. No VM, no podman build, no live spawn.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    CF="$REPO/tier2/Containerfile.qfileman"
    MAKE="$REPO/tier2/make-tier2-image.sh"
    SECCOMP="$REPO/tier2/seccomp/qfileman.json"
}

@test "tier-2 qfileman image installs the presentation consumer without WebEngine" {
    python3 - "$CF" "$MAKE" "$SECCOMP" <<'PY'
from pathlib import Path
import sys

cf_path, make_path, seccomp_path = (Path(p) for p in sys.argv[1:])
text = cf_path.read_text(encoding="utf-8")
make = make_path.read_text(encoding="utf-8")

PIP_BLOCK = (
    "RUN python3 -m pip install --no-deps --no-cache-dir --prefix=/usr \\\n"
    "        /usr/src/presentation \\\n"
    "        /usr/src/qdfileman \\\n"
)
NEED = (
    "COPY qdfileman /usr/src/qdfileman",
    "COPY presentation /usr/src/presentation",
    "python314-PyQt6",
    "python314-tomli-w",
    "qt6-wayland",
    "google-noto-sans-fonts",
    PIP_BLOCK,
    'CMD ["qfileman"]',
    "import qdistro_presentation, qfileman",
)
for needle in NEED:
    if needle not in text:
        raise SystemExit(f"Containerfile.qfileman missing {needle!r}")

FORBID = ("WebEngine", "qdbrowser", "qterminator", "qnotebook", "python314-PyQt6-WebEngine")
for needle in FORBID:
    if needle in text:
        raise SystemExit(f"Containerfile.qfileman contains forbidden {needle!r}")

STAGE_CALL = 'stage_workload_context "$wcontext" "$workload"'
if STAGE_CALL not in make:
    raise SystemExit("make-tier2-image.sh is missing stage_workload_context call")
if "resolve_consumer_src qdfileman qdfileman" not in make:
    raise SystemExit("make-tier2-image.sh does not resolve qdfileman sources")
if "resolve_consumer_src sdk/presentation presentation" not in make:
    raise SystemExit("make-tier2-image.sh does not resolve presentation sources")
if 'cp -a "$app" "$dest/qdfileman"' not in make:
    raise SystemExit("make-tier2-image.sh does not copy qdfileman into the context")
if 'cp -a "$pres" "$dest/presentation"' not in make:
    raise SystemExit("make-tier2-image.sh does not copy presentation into the context")
if 'rm -rf "$context/consumer"' not in make:
    raise SystemExit("make-tier2-image.sh does not strip consumer/ from the shared context")
if "qfileman)" not in make:
    raise SystemExit("make-tier2-image.sh qfileman staging is not workload-gated")

if not seccomp_path.is_file():
    raise SystemExit("tier2/seccomp/qfileman.json is missing")

# Mutations the named test must reject.
def expect_fail(src: str, dropped: str, label: str) -> None:
    if dropped not in src:
        raise SystemExit(f"cannot mutate missing {dropped!r}")
    mutated = src.replace(dropped, "", 1)
    if dropped in mutated:
        # first occurrence only is enough when the token is unique
        pass
    try:
        check(mutated, make, seccomp_path)
    except SystemExit:
        return
    raise SystemExit(f"checker accepted Containerfile after dropping {label}")

def check(src: str, make_src: str, seccomp: Path) -> None:
    for needle in NEED:
        if needle not in src:
            raise SystemExit(f"missing {needle!r}")
    for needle in FORBID:
        if needle in src:
            raise SystemExit(f"forbidden {needle!r}")
    if not seccomp.is_file():
        raise SystemExit("seccomp missing")
    if STAGE_CALL not in make_src:
        raise SystemExit("staging missing")
    if 'rm -rf "$context/consumer"' not in make_src:
        raise SystemExit("consumer strip missing")

check(text, make, seccomp_path)
expect_fail(text, "COPY qdfileman /usr/src/qdfileman\n", "COPY qdfileman")
expect_fail(text, "COPY presentation /usr/src/presentation\n", "COPY presentation")
expect_fail(text, "        /usr/src/presentation \\\n", "pip operand presentation")
expect_fail(text, "        /usr/src/qdfileman \\\n", "pip operand qdfileman")
expect_fail(text, "python314-PyQt6 \\\n", "python314-PyQt6")
expect_fail(text, 'CMD ["qfileman"]\n', "CMD qfileman")
expect_fail(text, "import qdistro_presentation, qfileman", "import smoke")

webengine = text.replace("python314-PyQt6 \\\n", "python314-PyQt6 \\\n        python314-PyQt6-WebEngine \\\n")
try:
    check(webengine, make, seccomp_path)
except SystemExit as exc:
    if "WebEngine" not in str(exc) and "forbidden" not in str(exc):
        raise SystemExit(f"WebEngine mutation failed for the wrong reason: {exc}") from None
else:
    raise SystemExit("checker accepted a WebEngine install")

dropped_stage = make.replace(STAGE_CALL, 'true "$wcontext" "$workload"', 1)
try:
    check(text, dropped_stage, seccomp_path)
except SystemExit as exc:
    if "staging missing" not in str(exc):
        raise SystemExit(f"staging mutation failed for the wrong reason: {exc}") from None
else:
    raise SystemExit("checker accepted make-tier2-image.sh without staging")

dropped_strip = make.replace('rm -rf "$context/consumer"\n', "", 1)
try:
    check(text, dropped_strip, seccomp_path)
except SystemExit as exc:
    if "consumer strip missing" not in str(exc):
        raise SystemExit(f"consumer-strip mutation failed for the wrong reason: {exc}") from None
else:
    raise SystemExit("checker accepted make-tier2-image.sh without stripping consumer/")

print("ok")
PY
}

@test "tier-2 qfileman image installs root-owned deployment metadata before USER" {
    python3 - "$CF" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")

INSTALL_D = (
    "install -d -o 0 -g 0 -m 0755 /usr/share/qdistro /usr/share/qdistro/presentation"
)
PRINTF = (
    "printf '%s\\n' '{\"version\":1,\"admin_uid\":1000}' "
    "> /usr/share/qdistro/presentation/deployment.json"
)
CHOWN = "chown 0:0 /usr/share/qdistro/presentation/deployment.json"
CHMOD = "chmod 0644 /usr/share/qdistro/presentation/deployment.json"
USER_LINE = "USER 1000:1000"
META_PATH = "/usr/share/qdistro/presentation/deployment.json"

META_RUN = (
    "RUN install -d -o 0 -g 0 -m 0755 /usr/share/qdistro /usr/share/qdistro/presentation \\\n"
    " && printf '%s\\n' '{\"version\":1,\"admin_uid\":1000}' "
    "> /usr/share/qdistro/presentation/deployment.json \\\n"
    " && chown 0:0 /usr/share/qdistro/presentation/deployment.json \\\n"
    " && chmod 0644 /usr/share/qdistro/presentation/deployment.json"
)
META_COMMENT = (
    "# Fixed deployment contract: the VM installer rejects admin UIDs other than 1000."
)
USER_COMMENT = "# uid 1000 = admin on the host; --userns=keep-id maps it 1:1."


def active_instructions(src: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    for raw in src.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if not stripped:
            if buf:
                continue
            continue
        if stripped.endswith("\\"):
            buf.append(stripped[:-1].rstrip())
            continue
        buf.append(stripped)
        out.append(" ".join(buf))
        buf = []
    if buf:
        out.append(" ".join(buf))
    return out


def is_nonroot_user(instr: str) -> bool:
    if not instr.startswith("USER "):
        return False
    spec = instr[5:].strip()
    return spec not in {"0", "0:0", "root", "root:root"}


def check(src: str) -> None:
    instrs = active_instructions(src)
    runs = [
        i
        for i in instrs
        if i.startswith("RUN ") and META_PATH in i
    ]
    if not runs:
        raise SystemExit("no metadata RUN")
    if len(runs) != 1:
        raise SystemExit(f"multiple metadata RUN: {runs!r}")
    run = runs[0]
    if INSTALL_D not in run:
        raise SystemExit("missing install -d")
    if PRINTF not in run:
        raise SystemExit("missing printf")
    if CHOWN not in run:
        raise SystemExit("missing chown")
    if CHMOD not in run:
        raise SystemExit("missing chmod")
    if USER_LINE not in instrs:
        raise SystemExit("USER 1000:1000 missing")
    user_idxs = [i for i, instr in enumerate(instrs) if is_nonroot_user(instr)]
    if not user_idxs:
        raise SystemExit("USER 1000:1000 missing")
    run_idx = instrs.index(run)
    if run_idx >= user_idxs[0]:
        raise SystemExit("metadata RUN after USER")


def expect_fail(src: str, needle: str) -> None:
    try:
        check(src)
    except SystemExit as exc:
        msg = str(exc)
        if needle not in msg:
            raise SystemExit(f"expected {needle!r} in {msg!r}") from None
        return
    raise SystemExit(f"checker accepted a broken Containerfile; wanted {needle!r}")


if META_RUN not in text:
    raise SystemExit(f"Containerfile.qfileman missing metadata RUN block")
check(text)

expect_fail(text.replace(META_RUN, "", 1), "no metadata RUN")

commented = "\n".join(
    f"# {line}" if line.strip() else line for line in META_RUN.splitlines()
)
expect_fail(text.replace(META_RUN, commented, 1), "no metadata RUN")

printf_line = (
    " && printf '%s\\n' '{\"version\":1,\"admin_uid\":1000}' "
    "> /usr/share/qdistro/presentation/deployment.json \\"
)
expect_fail(text.replace(printf_line + "\n", "", 1), "missing printf")

expect_fail(
    text.replace(
        "> /usr/share/qdistro/presentation/deployment.json",
        "> /tmp/deployment.json",
        1,
    ),
    "missing printf",
)
expect_fail(
    text.replace('"admin_uid":1000', '"admin_uid":1001', 1),
    "missing printf",
)
expect_fail(text.replace(INSTALL_D + " \\\n", "", 1), "missing install -d")
expect_fail(
    text.replace("install -d -o 0 -g 0 -m 0755", "install -d -o 0 -g 0 -m 0777", 1),
    "missing install -d",
)
expect_fail(text.replace(" && " + CHOWN + " \\\n", "\n", 1), "missing chown")
expect_fail(
    text.replace("chown 0:0 /usr/share/qdistro/presentation/deployment.json", "chown 1000:1000 /usr/share/qdistro/presentation/deployment.json", 1),
    "missing chown",
)
expect_fail(
    text.replace("chmod 0644 /usr/share/qdistro/presentation/deployment.json", "chmod 0666 /usr/share/qdistro/presentation/deployment.json", 1),
    "missing chmod",
)

ordered = f"{META_COMMENT}\n{META_RUN}\n\n{USER_COMMENT}\n{USER_LINE}"
moved = f"{USER_COMMENT}\n{USER_LINE}\n\n{META_COMMENT}\n{META_RUN}"
if ordered not in text:
    raise SystemExit("cannot locate metadata-before-USER order for mutation")
expect_fail(text.replace(ordered, moved, 1), "metadata RUN after USER")

comment_rescue = text.replace(printf_line + "\n", f"# {printf_line}\n", 1)
expect_fail(comment_rescue, "missing printf")

print("ok")
PY
}
