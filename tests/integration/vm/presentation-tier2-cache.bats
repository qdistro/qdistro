#!/usr/bin/env bats
# qci:host-only — runs on the host in the bats gate, no VM (ci/lib/gates/bats.sh).
# Host-only lock-in: the golden cache builder and VM bootstrap verify loop
# ship qdistro/tier2-qfileman:latest with first-party sources in that
# workload context only. No VM, no live podman build, no live spawn.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
    CACHE="$REPO/scripts/vm/build-tier2-podman-cache.sh"
    BOOTSTRAP="$REPO/scripts/vm/fresh-vm-bootstrap.sh"
}

@test "golden cache and bootstrap include the qfileman presentation consumer" {
    python3 - "$CACHE" "$BOOTSTRAP" <<'PY'
from pathlib import Path
import sys

cache_path, bootstrap_path = (Path(p) for p in sys.argv[1:])
cache = cache_path.read_text(encoding="utf-8")
bootstrap = bootstrap_path.read_text(encoding="utf-8")

WORKLOADS = "workloads=(weston-terminal text-viewer url-preview qfileman)\n"
LS_FILES = (
    'git -C "$repo" ls-files -z --cached --others --exclude-standard -- \\\n'
    "    tier2 qdfileman sdk/presentation |\n"
)
STAGE_QD = 'cp -a "$work/context/qdfileman" "$wcontext/qdfileman"\n'
STAGE_PRES = 'cp -a "$work/context/sdk/presentation" "$wcontext/presentation"\n'
STRIP = 'rm -rf "$wcontext/consumer"\n'
PER_WORKLOAD = 'cp -a "$work/context/tier2/." "$wcontext/"\n'
BOOT_LOOP = "for _w in weston-terminal text-viewer url-preview qfileman; do\n"
BOOT_LOG = (
    "tier-2 images pre-built: weston-terminal, text-viewer, url-preview, qfileman"
)

def check(cache_src: str, boot_src: str) -> None:
    need_cache = (
        WORKLOADS,
        LS_FILES,
        STAGE_QD,
        STAGE_PRES,
        STRIP,
        PER_WORKLOAD,
        'wcontext=$(mktemp -d "$work/wcontext.$workload.XXXXXX")\n',
        "--file \"$wcontext/Containerfile.$workload\"",
    )
    for needle in need_cache:
        if needle not in cache_src:
            raise SystemExit(f"cache script missing {needle!r}")
    if BOOT_LOOP not in boot_src:
        raise SystemExit(f"bootstrap missing {BOOT_LOOP!r}")
    if BOOT_LOG not in boot_src:
        raise SystemExit(f"bootstrap missing {BOOT_LOG!r}")
    if 'workloads=(weston-terminal text-viewer url-preview)\n' in cache_src:
        raise SystemExit("cache script still builds only the three bats-minimum images")
    if "for _w in weston-terminal text-viewer url-preview; do" in boot_src:
        raise SystemExit("bootstrap verify loop omits qfileman")

def expect_fail(cache_src: str, boot_src: str, dropped: str, label: str) -> None:
    if dropped not in cache_src and dropped not in boot_src:
        raise SystemExit(f"cannot mutate missing {dropped!r}")
    if dropped in cache_src:
        mutated_cache = cache_src.replace(dropped, "", 1)
        mutated_boot = boot_src
    else:
        mutated_cache = cache_src
        mutated_boot = boot_src.replace(dropped, "", 1)
    try:
        check(mutated_cache, mutated_boot)
    except SystemExit:
        return
    raise SystemExit(f"checker accepted scripts after dropping {label}")

def expect_replace(cache_src: str, boot_src: str, old: str, new: str, label: str) -> None:
    if old not in cache_src and old not in boot_src:
        raise SystemExit(f"cannot replace missing {old!r}")
    if old in cache_src:
        mutated_cache = cache_src.replace(old, new, 1)
        mutated_boot = boot_src
    else:
        mutated_cache = cache_src
        mutated_boot = boot_src.replace(old, new, 1)
    try:
        check(mutated_cache, mutated_boot)
    except SystemExit:
        return
    raise SystemExit(f"checker accepted scripts after replacing {label}")

check(cache, bootstrap)
expect_fail(cache, bootstrap, WORKLOADS, "workloads array with qfileman")
expect_fail(cache, bootstrap, "    tier2 qdfileman sdk/presentation |\n", "ls-files consumer pathspecs")
expect_fail(cache, bootstrap, " qdfileman", "qdfileman ls-files pathspec")
expect_fail(cache, bootstrap, " sdk/presentation", "presentation ls-files pathspec")
expect_fail(cache, bootstrap, STAGE_QD, "qdfileman staging copy")
expect_fail(cache, bootstrap, STAGE_PRES, "presentation staging copy")
expect_fail(cache, bootstrap, STRIP, "consumer strip")
expect_fail(cache, bootstrap, PER_WORKLOAD, "per-workload context copy")
expect_fail(cache, bootstrap, BOOT_LOOP, "bootstrap qfileman verify loop")
expect_fail(cache, bootstrap, BOOT_LOG, "bootstrap qfileman log")
expect_replace(
    cache,
    bootstrap,
    WORKLOADS,
    "workloads=(weston-terminal text-viewer url-preview)\n",
    "qfileman dropped from workloads",
)
expect_replace(
    cache,
    bootstrap,
    BOOT_LOOP,
    "for _w in weston-terminal text-viewer url-preview; do\n",
    "qfileman dropped from bootstrap loop",
)
print("ok")
PY
}
