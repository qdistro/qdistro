#!/usr/bin/env bats
# qci:host-only — runs on the host in the bats gate, no VM (ci/lib/gates/bats.sh).
# Host-only lock-in of the first-party tier-2 presentation consumer image:
# Containerfile.qfileman installs qfileman + qdistro-presentation without
# WebEngine, make-tier2-image.sh stages those sources into the qfileman
# context only, and a hardened seccomp profile exists. No VM, no podman
# build, no live spawn.

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

NEED = (
    "COPY qdfileman /usr/src/qdfileman",
    "COPY presentation /usr/src/presentation",
    "python313-PyQt6",
    "python313-tomli-w",
    "qt6-wayland",
    "google-noto-sans-fonts",
    "pip install --no-deps",
    'CMD ["qfileman"]',
    "import qdistro_presentation, qfileman",
)
for needle in NEED:
    if needle not in text:
        raise SystemExit(f"Containerfile.qfileman missing {needle!r}")

FORBID = ("WebEngine", "qdbrowser", "qterminator", "qnotebook", "python313-PyQt6-WebEngine")
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

check(text, make, seccomp_path)
expect_fail(text, "COPY qdfileman /usr/src/qdfileman\n", "COPY qdfileman")
expect_fail(text, "COPY presentation /usr/src/presentation\n", "COPY presentation")
expect_fail(text, "python313-PyQt6 \\\n", "python313-PyQt6")
expect_fail(text, 'CMD ["qfileman"]\n', "CMD qfileman")
expect_fail(text, "import qdistro_presentation, qfileman", "import smoke")

webengine = text.replace("python313-PyQt6 \\\n", "python313-PyQt6 \\\n        python313-PyQt6-WebEngine \\\n")
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

print("ok")
PY
}
