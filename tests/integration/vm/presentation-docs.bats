#!/usr/bin/env bats
# qci:host-only — runs on the host in the bats gate, no VM (ci/lib/gates/bats.sh).
# Host-only lock-in of presentation documentation: overview consumer status,
# presentation.md trust/reset/type/hash, and architecture/ui links.
# No VM, no root, no live /var/lib/qdistro writes.

setup() {
    REPO="$(cd "$BATS_TEST_DIRNAME/../../.." && pwd)"
}

@test "presentation docs record shipped consumers, trust, reset, type, and hash" {
    python3 - "$REPO/doc/overview.md" "$REPO/doc/presentation.md" \
        "$REPO/doc/architecture.md" "$REPO/doc/ui.md" <<'PY'
from pathlib import Path
import re
import sys

overview_path, presentation_path, architecture_path, ui_path = (
    Path(p) for p in sys.argv[1:]
)
overview = overview_path.read_text(encoding="utf-8")
presentation = presentation_path.read_text(encoding="utf-8")
architecture = architecture_path.read_text(encoding="utf-8")
ui = ui_path.read_text(encoding="utf-8")

OVERVIEW_NEED = (
    "The locker freezes a trusted copy of",
    "qdgreeter/qdgreeter/qml/shim/Color.qml",
    "does not follow a logged-in user's live settings",
    "There is no theme-propagation",
    "Live isolated-app, enforcing-tier",
    "[presentation.md](presentation.md)",
)
OVERVIEW_FORBID = (
    "qdgreeter and qdlocker do not read a shared store",
    "No dynamic theme loading",
)
STALE_GREETER = re.compile(r"(?<!qdgreeter/)qdgreeter/qml/shim/Color.qml")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
PRESENTATION_NEED = (
    "/usr/share/qdistro/presentation/deployment.json",
    "O_NOFOLLOW",
    "untrusted metadata yields no managed source",
    "A bind of `current.json` is not by",
    "`--reset` writes an `enabled=false` envelope",
    "qdistro_presentation_t",
    "generation` does not match the SHA-256",
    "Symlinks are refused",
)
ARCHITECTURE_NEED = (
    "**qdistro-presentation**",
    "[presentation.md](presentation.md)",
)
UI_NEED = ("[presentation.md](presentation.md)",)


def active(text: str) -> str:
    return HTML_COMMENT.sub("", text)


def check(
    overview_src: str,
    presentation_src: str,
    architecture_src: str,
    ui_src: str,
) -> None:
    overview_src = active(overview_src)
    presentation_src = active(presentation_src)
    architecture_src = active(architecture_src)
    ui_src = active(ui_src)
    for needle in OVERVIEW_FORBID:
        if needle in overview_src:
            raise SystemExit(f"overview.md contains stale {needle!r}")
    if STALE_GREETER.search(overview_src):
        raise SystemExit("overview.md contains stale 'qdgreeter/qml/shim/Color.qml'")
    for needle in OVERVIEW_NEED:
        if needle not in overview_src:
            raise SystemExit(f"overview.md missing {needle!r}")
    for needle in PRESENTATION_NEED:
        if needle not in presentation_src:
            raise SystemExit(f"presentation.md missing {needle!r}")
    for needle in ARCHITECTURE_NEED:
        if needle not in architecture_src:
            raise SystemExit(f"architecture.md missing {needle!r}")
    for needle in UI_NEED:
        if needle not in ui_src:
            raise SystemExit(f"ui.md missing {needle!r}")


def expect_fail(
    overview_src: str,
    presentation_src: str,
    architecture_src: str,
    ui_src: str,
    *,
    label: str,
    expect: str,
) -> None:
    try:
        check(overview_src, presentation_src, architecture_src, ui_src)
    except SystemExit as exc:
        if expect not in str(exc):
            raise SystemExit(
                f"{label} mutation failed for the wrong reason: {exc}"
            ) from None
        return
    raise SystemExit(f"checker accepted docs after {label}")


def mutate(src: str, old: str, new: str, label: str) -> str:
    if old not in src:
        raise SystemExit(f"cannot mutate missing {old!r} ({label})")
    return src.replace(old, new, 1)


check(overview, presentation, architecture, ui)

stale = mutate(
    overview,
    "The locker freezes a trusted copy of",
    "qdgreeter and qdlocker do not read a shared store. The locker freezes a trusted copy of",
    "stale shared-store sentence",
)
expect_fail(
    stale,
    presentation,
    architecture,
    ui,
    label="stale shared-store sentence",
    expect="stale 'qdgreeter and qdlocker do not read a shared store'",
)

wrong_greeter = mutate(
    overview,
    "qdgreeter/qdgreeter/qml/shim/Color.qml",
    "qdgreeter/qml/shim/Color.qml",
    "wrong greeter shim path",
)
expect_fail(
    wrong_greeter,
    presentation,
    architecture,
    ui,
    label="wrong greeter shim path",
    expect="stale 'qdgreeter/qml/shim/Color.qml'",
)

with_static = mutate(
    overview,
    "The greeter keeps independent",
    "The greeter keeps independent. No dynamic theme loading",
    "static-palette claim",
)
expect_fail(
    with_static,
    presentation,
    architecture,
    ui,
    label="static-palette claim",
    expect="stale 'No dynamic theme loading'",
)

dropped_freeze = mutate(
    overview, "The locker freezes a trusted copy of", "", "dropped locker freeze"
)
expect_fail(
    dropped_freeze,
    presentation,
    architecture,
    ui,
    label="dropped locker freeze",
    expect="overview.md missing 'The locker freezes a trusted copy of'",
)

commented_freeze = mutate(
    overview,
    "The locker freezes a trusted copy of",
    "<!-- The locker freezes a trusted copy of --> omitted:",
    "commented locker freeze",
)
expect_fail(
    commented_freeze,
    presentation,
    architecture,
    ui,
    label="commented locker freeze",
    expect="overview.md missing 'The locker freezes a trusted copy of'",
)

dropped_meta = mutate(
    presentation,
    "/usr/share/qdistro/presentation/deployment.json",
    "",
    "dropped deployment.json",
)
expect_fail(
    overview,
    dropped_meta,
    architecture,
    ui,
    label="dropped deployment.json",
    expect="presentation.md missing '/usr/share/qdistro/presentation/deployment.json'",
)

dropped_reset = mutate(
    presentation,
    "`--reset` writes an `enabled=false` envelope",
    "",
    "dropped --reset",
)
expect_fail(
    overview,
    dropped_reset,
    architecture,
    ui,
    label="dropped --reset",
    expect="presentation.md missing '`--reset` writes an `enabled=false` envelope'",
)

dropped_type = mutate(
    presentation, "qdistro_presentation_t", "", "dropped SELinux type"
)
expect_fail(
    overview,
    dropped_type,
    architecture,
    ui,
    label="dropped SELinux type",
    expect="presentation.md missing 'qdistro_presentation_t'",
)

dropped_hash = mutate(
    presentation,
    "generation` does not match the SHA-256",
    "",
    "dropped generation hash",
)
expect_fail(
    overview,
    dropped_hash,
    architecture,
    ui,
    label="dropped generation hash",
    expect="presentation.md missing 'generation` does not match the SHA-256'",
)

dropped_bind = mutate(
    presentation,
    "A bind of `current.json` is not by",
    "",
    "dropped bind-is-not-source",
)
expect_fail(
    overview,
    dropped_bind,
    architecture,
    ui,
    label="dropped bind-is-not-source",
    expect="presentation.md missing 'A bind of `current.json` is not by'",
)

dropped_arch = mutate(
    architecture,
    "[presentation.md](presentation.md)",
    "",
    "dropped architecture link",
)
expect_fail(
    overview,
    presentation,
    dropped_arch,
    ui,
    label="dropped architecture link",
    expect="architecture.md missing '[presentation.md](presentation.md)'",
)

dropped_ui = mutate(
    ui, "[presentation.md](presentation.md)", "", "dropped ui link"
)
expect_fail(
    overview,
    presentation,
    architecture,
    dropped_ui,
    label="dropped ui link",
    expect="ui.md missing '[presentation.md](presentation.md)'",
)

print("ok")
PY
}
