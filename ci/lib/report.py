#!/usr/bin/env python3
"""Generate qdistro local-CI Markdown and HTML reports."""

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path

KEY_RE = re.compile(
    r"(FAIL|FAIL_LOUD|ERROR|FAILED|Traceback|AssertionError|not found|missing|"
    r"denied|refused|timed out|timeout|No module named|protocol error|inactive|failed)",
    re.IGNORECASE,
)

# Per-assertion evidence markers emitted by the bats helpers in
# tests/integration/vm/helpers.bash (ensures / check_pass / check_fail) and by
# any other layer that follows the same greppable shape. Parsing these is
# strictly OPTIONAL: a captured log without them yields no evidence rows and
# renders exactly as before.
#
#   --- ensures: <capability> ---
#   --- CHECK pass: <message> | evidence: <...> ---
#   --- CHECK pass: <message> ---
#   --- CHECK fail: <message> | expected: <...> | actual: <...> ---
#   --- CHECK fail: expected: <...> | actual: <...> ---
ENSURES_RE = re.compile(r"^---\s*ensures:\s*(?P<cap>.*?)\s*---\s*$")
CHECK_PASS_RE = re.compile(
    r"^---\s*CHECK pass:\s*(?P<msg>.*?)(?:\s*\|\s*evidence:\s*(?P<evidence>.*?))?\s*---\s*$"
)
CHECK_FAIL_RE = re.compile(
    r"^---\s*CHECK fail:\s*(?:(?P<msg>.*?)\s*\|\s*)?"
    r"expected:\s*(?P<expected>.*?)\s*\|\s*actual:\s*(?P<actual>.*?)\s*---\s*$"
)


def parse_evidence(path: Path, max_checks: int = 40) -> list[dict[str, str]]:
    """Extract per-assertion evidence from a captured log.

    Returns a list of check dicts, each with a ``kind`` of ``pass`` or
    ``fail`` plus the parsed fields and the nearest preceding ``ensures``
    capability (when present). Returns an empty list when the log is missing
    or carries no evidence markers, so callers stay backward compatible.
    """
    if not path.exists() or not path.is_file():
        return []
    checks: list[dict[str, str]] = []
    pending_ensures = ""
    for line in path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        m = ENSURES_RE.match(stripped)
        if m:
            pending_ensures = m.group("cap")
            continue
        m = CHECK_PASS_RE.match(stripped)
        if m:
            checks.append(
                {
                    "kind": "pass",
                    "message": m.group("msg") or "",
                    "evidence": m.group("evidence") or "",
                    "ensures": pending_ensures,
                }
            )
            pending_ensures = ""
            if len(checks) >= max_checks:
                break
            continue
        m = CHECK_FAIL_RE.match(stripped)
        if m:
            checks.append(
                {
                    "kind": "fail",
                    "message": m.group("msg") or "",
                    "expected": m.group("expected") or "",
                    "actual": m.group("actual") or "",
                    "ensures": pending_ensures,
                }
            )
            pending_ensures = ""
            if len(checks) >= max_checks:
                break
            continue
    return checks


def evidence_md_lines(checks: list[dict[str, str]]) -> list[str]:
    """Render parsed evidence checks as Markdown list lines.

    Returns an empty list when there is nothing to render, so existing rows
    without evidence add no output at all.
    """
    if not checks:
        return []
    lines = ["- evidence (per-assertion):"]
    for chk in checks:
        ensures = chk.get("ensures", "")
        prefix = f"_ensures: {ensures}_ — " if ensures else ""
        if chk["kind"] == "pass":
            ev = chk.get("evidence", "")
            ev_suffix = f": `{ev}`" if ev else ""
            lines.append(f"  - PASS {prefix}{chk.get('message', '')}{ev_suffix}")
        else:
            msg = chk.get("message", "")
            msg_part = f"{msg} — " if msg else ""
            lines.append(
                f"  - FAIL {prefix}{msg_part}"
                f"expected `{chk.get('expected', '')}`, "
                f"actual `{chk.get('actual', '')}`"
            )
    return lines


def read_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def read_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    lines = path.read_text(errors="replace").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        cols = line.split("\t")
        cols.extend([""] * (len(header) - len(cols)))
        rows.append(dict(zip(header, cols, strict=False)))
    return rows


def rel_link(run_dir: Path, value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute():
        try:
            return path.relative_to(run_dir).as_posix()
        except ValueError:
            return path.as_posix()
    return value


def md_link(run_dir: Path, value: str, label: str | None = None) -> str:
    href = rel_link(run_dir, value)
    if not href:
        return ""
    label = label or href
    safe_href = href.replace(" ", "%20")
    return f"[{label}]({safe_href})"


def html_link(run_dir: Path, value: str, label: str | None = None) -> str:
    href = rel_link(run_dir, value)
    if not href:
        return ""
    label = label or href
    return f'<a href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'


def excerpt(path: Path, max_lines: int = 20) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    lines = path.read_text(errors="replace").splitlines()
    hits = [line for line in lines if KEY_RE.search(line)]
    if hits:
        return hits[:max_lines]
    return lines[-max_lines:]


def recommendation(row: dict[str, str], run_dir: Path) -> str:
    notes = row.get("notes", "")
    text = notes
    log = row.get("log", "")
    if log:
        log_path = run_dir / log
        if log_path.exists():
            text += "\n" + "\n".join(excerpt(log_path, 12))
    lower = text.lower()
    if "qci_agent_cmd" in lower or "agent runner" in lower:
        return (
            "Run this gate with a real visual-agent command or have an agent "
            "complete the generated prompt files under agent-notes/."
        )
    if "qdistro_vm_password" in lower:
        return "The qdistro test VM password is fixed at Pa_ssw0rd45; rebuild or patch the baseweed images if authentication fails."
    if "baseweed-baked" in lower:
        return "Build or repair the prebaked VM image with qdistro/scripts/vm/build-baked-baseweed.sh."
    if "no module named" in lower:
        return "Install the missing Python dependency or add it to the project test extras."
    if "npm" in lower and ("not found" in lower or "missing" in lower):
        return "Install node dependencies for that extension repo, then rerun the host gate."
    if "protocol error" in lower or "qdwin_shell" in lower or "wayland" in lower:
        return "Inspect qdwin/qdshell protocol logs first; avoid qdshell workarounds for compositor protocol bugs."
    if "inactive" in lower or "systemctl" in lower:
        return "Start with the collected user/system journals and the systemctl status artifact for the failed VM."
    if row.get("gate", "").startswith("gui"):
        return "Open the scenario log, adjacent screenshots, and journal delta; rerun only this scenario on the preserved VM."
    if row.get("gate", "").startswith("bats") or row.get("kind") == "bats":
        return "Rerun the single bats file against the preserved VM before broadening the search."
    return "Inspect the linked log excerpt and rerun the smallest failing command."


def source_links(row: dict[str, str], run_dir: Path, manifest: dict[str, str]) -> list[str]:
    """Return Markdown links to source files mentioned by a result log."""
    log = row.get("log", "")
    if not log:
        return []
    log_path = run_dir / log
    if not log_path.exists():
        return []
    workspace = Path(manifest.get("workspace", "/"))
    text = log_path.read_text(errors="replace")
    links: list[str] = []
    seen: set[Path] = set()

    def path_exists(path: Path) -> bool:
        try:
            return path.exists()
        except OSError:
            return False

    # Generic fallback for absolute source paths.
    for raw in re.findall(r"(/[A-Za-z0-9_./+@-]+\.[A-Za-z0-9_+-]+)", text):
        path = Path(raw.rstrip(":,.)"))
        if not path_exists(path) or path in seen:
            continue
        try:
            path.relative_to(workspace)
        except ValueError:
            continue
        seen.add(path)
        links.append(f"[{display_path(path, workspace, None)}]({path.as_posix()})")
    return links


def display_path(path: Path, workspace: Path, line_no: int | None) -> str:
    try:
        label = path.relative_to(workspace).as_posix()
    except ValueError:
        label = path.as_posix()
    if line_no:
        label = f"{label}:{line_no}"
    return label


def status_counts(rows: list[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = row.get("status", "unknown") or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


# Notes substrings that mark a skip as a *dependency-missing* skip rather than
# an expected-environment skip. These are called out explicitly so a
# dependency gap can't hide behind a green-looking "mostly skipped" run.
#
# Anchored to the runners' ACTUAL dependency-missing vocabulary, taken from the
# live skip-emitting sites (qci gate, helpers.bash, the vm probes): "<tool> not
# installed", "missing shellcheck/bats", "optional tool missing", "no module
# named", "<file> not found", "dbus-send absent", "dbus-python not importable",
# "python3 needed for scan", "not configured".
#
# The earlier broad terms (`could not` / `cannot ` / `unavailable` / `not set`)
# were dropped, AND bare `not available` is deliberately NOT a marker: live
# EXPECTED-ENVIRONMENT skips use exactly those phrasings — "could not connect to
# display", "cannot acquire wl_seat", "wl_seat unavailable", "DISPLAY not set",
# "legacy qdshell ctrl-socket not available", "nested KVM not available in VM" —
# so matching them mislabelled benign env skips as dependency gaps. `missing` is
# matched only adjacent to a dependency noun (either order) so a content skip
# like "missing headings" is not caught. Test: tests/unit/test_ci_report.py.
_DEP_NOUN = (r"dep|dependenc|tool|binar|program|command|executable|module|"
             r"package|interpreter|file|header|library|runtime")
# Known external tools the gates name directly in "missing <tool>" /
# "<tool> not installed" skips. Bare tool names are needed only for the
# "missing <tool>" form (e.g. "missing shellcheck/bats"); the name-agnostic
# markers below ("not installed", "absent", "not importable") cover the rest.
_DEP_TOOL = (r"shellcheck|bats|node|npm|ruff|mypy|python3?|pip|"
             r"dbus-send|dbus-python|systemctl|systemd-run|busctl|jq|cmake|"
             r"meson|ninja|virsh|podman|qemu-img|nft|"
             # SELinux probe tooling (used with "<tool> absent")
             r"semodule|sesearch|audit2allow|ausearch")
_DEP_MISSING_RE = re.compile(
    r"("
    r"not installed"
    r"|not importable"
    r"|not configured"
    r"|no module named"
    r"|not found"
    r"|needed (?:for|by)\b"
    rf"|missing (?:{_DEP_NOUN}|{_DEP_TOOL})"
    # "<tool|noun> [is/are] missing|absent". Both `missing` and `absent` are
    # scoped to a tool/dep noun (NOT bare): live env/state skips also say
    # "absent" ("$IMAGE absent", "renderD128 absent", "tier-5 base disk
    # absent"), so a bare match would re-flag benign rows. The leading \b stops
    # a noun stem matching mid-word ("profile absent" must not match via
    # "file").
    rf"|\b(?:{_DEP_NOUN}|{_DEP_TOOL})s? (?:is |are )?(?:missing|absent)\b"
    r")",
    re.IGNORECASE,
)


def category_breakdown(rows: list[dict[str, str]]) -> dict[str, dict[str, int]]:
    """Per-category status tallies keyed by the taxonomy `category` column.

    Rows from an older runner that predate the column (``category`` absent or
    empty) are bucketed under ``"(uncategorized)"`` so the section degrades
    gracefully and never crashes on a legacy results.tsv.
    """
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        cat = (row.get("category") or "").strip() or "(uncategorized)"
        status = row.get("status", "unknown") or "unknown"
        bucket = out.setdefault(cat, {})
        bucket[status] = bucket.get(status, 0) + 1
        bucket["total"] = bucket.get("total", 0) + 1
    return out


def dependency_missing_skips(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Skips whose notes look like a missing dependency (not an env skip)."""
    return [
        r for r in rows
        if r.get("status") == "skip" and _DEP_MISSING_RE.search(r.get("notes", ""))
    ]


def generate_md(run_dir: Path) -> str:
    manifest = read_kv(run_dir / "manifest.txt")
    rows = read_tsv(run_dir / "results.tsv")
    repos = read_tsv(run_dir / "repo-state.tsv")
    counts = status_counts(rows)
    failures = [r for r in rows if r.get("status") in {"fail", "blocked"}]
    skips = [r for r in rows if r.get("status") == "skip"]

    title = manifest.get("run_id", run_dir.name)
    lines: list[str] = [f"# qdistro CI report: {title}", ""]
    lines.append("## Summary")
    for key in ("gate", "started_utc", "finished_utc", "exit_code", "exit_class", "workspace", "command"):
        if manifest.get(key):
            lines.append(f"- **{key}**: `{manifest[key]}`")
    if counts:
        lines.append("- **results**: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append("")

    # Confidence taxonomy breakdown (additive; see ci/TAXONOMY.md). Groups
    # results by the per-row `category` column so a reader sees how the
    # pass/fail/skip tally distributes across confidence bands (a vm row and a
    # source_invariant row are NOT equally strong evidence). Emitted only when
    # at least one row carries a category, so legacy runs render as before.
    cats = category_breakdown(rows)
    has_category = any(c != "(uncategorized)" for c in cats)
    if cats and has_category:
        lines.append("## Test categories")
        lines.append("Per-category result tally. Categories are the shared "
                     "confidence vocabulary documented in `ci/TAXONOMY.md`; "
                     "this is reporting only — it gates nothing.")
        lines.append("")
        lines.append("| category | total | pass | fail | blocked | skip |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for cat in sorted(cats):
            b = cats[cat]
            lines.append(
                f"| {cat} | {b.get('total', 0)} | {b.get('pass', 0)} | "
                f"{b.get('fail', 0)} | {b.get('blocked', 0)} | "
                f"{b.get('skip', 0)} |"
            )
        lines.append("")
        dep_skips = dependency_missing_skips(rows)
        if dep_skips:
            lines.append("**Dependency-missing skips** (a missing dep is a "
                         "bake/host regression, not an expected skip — install "
                         "the dep, do not let it hide a gap):")
            for row in dep_skips:
                lines.append(
                    f"- `{row.get('category', '')}` "
                    f"{row.get('gate', '?')} / {row.get('subject', '?')}: "
                    f"{row.get('notes', '')}"
                )
            lines.append("")

    if failures:
        lines.append("## Failures and blocked work")
        for row in failures:
            subject = row.get("subject", "?")
            gate = row.get("gate", "?")
            status = row.get("status", "?").upper()
            log = md_link(run_dir, row.get("log", ""), "log")
            lines.append(f"### {status}: {gate} / {subject}")
            lines.append(f"- exit: `{row.get('exit_code', '')}` class: `{row.get('exit_class', '')}` kind: `{row.get('kind', '')}`")
            if log:
                lines.append(f"- evidence: {log}")
            sources = source_links(row, run_dir, manifest)
            if sources:
                lines.append("- source: " + ", ".join(sources))
            if row.get("notes"):
                lines.append(f"- notes: {row['notes']}")
            lines.append(f"- recommendation: {recommendation(row, run_dir)}")
            log_path = run_dir / row.get("log", "")
            lines.extend(evidence_md_lines(parse_evidence(log_path)))
            ex = excerpt(log_path)
            if ex:
                lines.append("")
                lines.append("```text")
                lines.extend(ex)
                lines.append("```")
            lines.append("")
    else:
        lines.append("## Failures and blocked work")
        lines.append("No failing or blocked result rows were recorded.")
        lines.append("")

    if skips:
        lines.append("## Skips")
        for row in skips:
            log = md_link(run_dir, row.get("log", ""), "log")
            suffix = f" ({log})" if log else ""
            lines.append(f"- {row.get('gate', '?')} / {row.get('subject', '?')}: {row.get('notes', '')}{suffix}")
        lines.append("")

    # Optional per-assertion evidence section. Only emitted when at least one
    # result's captured log carries the greppable evidence markers; runs with
    # no evidence-bearing logs render exactly as before (no section, no
    # heading). Failure rows already inline their evidence above, so this
    # section surfaces evidence from the remaining rows (e.g. passing ones
    # that cite what they proved).
    failure_ids = {id(r) for r in failures}
    evidence_blocks: list[list[str]] = []
    for row in rows:
        if id(row) in failure_ids:
            continue
        checks = parse_evidence(run_dir / row.get("log", ""))
        if not checks:
            continue
        block = [
            f"### {row.get('status', '?').upper()}: "
            f"{row.get('gate', '?')} / {row.get('subject', '?')}"
        ]
        block.extend(evidence_md_lines(checks))
        block.append("")
        evidence_blocks.append(block)
    if evidence_blocks:
        lines.append("## Per-assertion evidence")
        for block in evidence_blocks:
            lines.extend(block)

    # Agent attempts & host load (Phase 2 observability; additive). Surfaces the
    # flake-relevant signal: agent attempts that did not cleanly PASS (rc!=0,
    # UNKNOWN/timeout, slow walls) and the host contention they ran under. Both
    # source TSVs are optional — read_tsv() returns [] when absent, so legacy runs
    # render exactly as before (no heading). Reporting only; gates nothing.
    attempts = read_tsv(run_dir / "scenario-attempts.tsv")
    flaky_attempts = [
        a for a in attempts
        if (a.get("status") or "").upper() != "PASS" or (a.get("agent_rc") or "0") != "0"
    ]
    if flaky_attempts:
        lines.append("## Agent attempts (non-clean)")
        lines.append("Agent scenario attempts that did not cleanly PASS with rc=0 — "
                     "the flake-relevant rows (UNKNOWN/timeout/slow). Reporting only.")
        lines.append("")
        lines.append("| scenario | attempt | status | agent_rc | classifier | wall_s |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for a in flaky_attempts:
            lines.append(
                f"| {a.get('subject', '?')} | {a.get('attempt', '')} | "
                f"{a.get('status', '')} | {a.get('agent_rc', '')} | "
                f"{a.get('classifier', '') or '—'} | {a.get('wall_s', '')} |"
            )
        lines.append("")

    host_load = read_tsv(run_dir / "host-load.tsv")
    if host_load:
        def _nums(key: str) -> list[float]:
            out = []
            for r in host_load:
                try:
                    out.append(float(r.get(key, "")))
                except (TypeError, ValueError):
                    pass
            return out
        loads = _nums("loadavg1")
        avails = _nums("mem_avail_mb")
        vms = _nums("qemu_vms")
        if loads or avails or vms:
            lines.append("## Host load during GUI scenarios")
            lines.append("Contention proxy sampled at each scenario's start/end. "
                         "High peak load / low available memory correlate with the "
                         "GUI flake variance. Reporting only.")
            if loads:
                lines.append(f"- **peak loadavg (1m)**: {max(loads):.2f} "
                             f"(min {min(loads):.2f})")
            if avails:
                lines.append(f"- **min MemAvailable**: {min(avails):.0f} MiB "
                             f"(max {max(avails):.0f} MiB)")
            if vms:
                lines.append(f"- **peak concurrent qemu VMs**: {max(vms):.0f}")
            lines.append("")

    lines.append("## All Results")
    lines.append("| status | gate | subject | category | kind | exit | log | notes |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        log = md_link(run_dir, row.get("log", ""), "log")
        cols = [
            row.get("status", ""),
            row.get("gate", ""),
            row.get("subject", ""),
            row.get("category", ""),
            row.get("kind", ""),
            row.get("exit_code", ""),
            log,
            row.get("notes", ""),
        ]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cols) + " |")
    lines.append("")

    if repos:
        lines.append("## Repo State")
        lines.append("| repo | branch | head | dirty | status |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in repos:
            status = md_link(run_dir, row.get("status_log", ""), "status")
            lines.append(
                "| "
                + " | ".join(
                    [
                        row.get("repo", ""),
                        row.get("branch", ""),
                        row.get("head", ""),
                        row.get("dirty_files", ""),
                        status,
                    ]
                )
                + " |"
            )
        lines.append("")

    lines.append("## Artifact Index")
    for child in sorted(run_dir.iterdir()):
        if child.name in {"report.md", "report.html"}:
            continue
        if child.is_dir():
            lines.append(f"- {md_link(run_dir, child.as_posix(), child.name + '/')}")
        else:
            lines.append(f"- {md_link(run_dir, child.as_posix(), child.name)}")
    lines.append("")
    return "\n".join(lines)


def generate_summary(run_dir: Path) -> dict[str, object]:
    manifest = read_kv(run_dir / "manifest.txt")
    rows = read_tsv(run_dir / "results.tsv")
    failures = [r for r in rows if r.get("status") in {"fail", "blocked"}]
    return {
        "run_id": manifest.get("run_id", run_dir.name),
        "gate": manifest.get("gate", ""),
        "started_utc": manifest.get("started_utc", ""),
        "finished_utc": manifest.get("finished_utc", ""),
        "exit_code": int(manifest.get("exit_code", "0") or 0),
        "exit_class": manifest.get("exit_class", ""),
        "workspace": manifest.get("workspace", ""),
        "counts": status_counts(rows),
        # Per-category tally for machine consumers (additive; empty for legacy
        # runs whose results.tsv predates the `category` column).
        "categories": category_breakdown(rows),
        "dependency_missing_skips": [
            {"gate": r.get("gate", ""), "subject": r.get("subject", ""),
             "category": r.get("category", ""), "notes": r.get("notes", "")}
            for r in dependency_missing_skips(rows)
        ],
        "first_failure": failures[0] if failures else None,
        "report_md": "report.md",
        "report_html": "report.html",
    }


def generate_html(run_dir: Path, markdown: str) -> str:
    # Keep this dependency-free. It is intentionally simple but readable.
    body_lines: list[str] = []
    in_code = False
    in_table = False
    for raw in markdown.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                body_lines.append("</code></pre>")
                in_code = False
            else:
                body_lines.append("<pre><code>")
                in_code = True
            continue
        if in_code:
            body_lines.append(html.escape(line))
            continue
        if line.startswith("# "):
            body_lines.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            if in_table:
                body_lines.append("</tbody></table>")
                in_table = False
            body_lines.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("### "):
            if in_table:
                body_lines.append("</tbody></table>")
                in_table = False
            body_lines.append(f"<h3>{html.escape(line[4:])}</h3>")
        elif line.startswith("| ") and line.endswith(" |"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if set(cells) == {"---"}:
                continue
            if not in_table:
                body_lines.append("<table><tbody>")
                in_table = True
            tag = "th" if all(c in {"status", "gate", "subject", "category", "kind", "exit", "log", "notes", "repo", "branch", "head", "dirty", "total", "pass", "fail", "blocked", "skip"} for c in cells) else "td"
            body_lines.append("<tr>" + "".join(f"<{tag}>{linkify(c)}</{tag}>" for c in cells) + "</tr>")
        else:
            if in_table:
                body_lines.append("</tbody></table>")
                in_table = False
            if not line:
                body_lines.append("")
            elif line.startswith("- "):
                body_lines.append(f"<p>{linkify(line)}</p>")
            else:
                body_lines.append(f"<p>{linkify(line)}</p>")
    if in_table:
        body_lines.append("</tbody></table>")
    style = """
body { font-family: system-ui, sans-serif; max-width: 1120px; margin: 32px auto; line-height: 1.45; padding: 0 24px; }
h1, h2, h3 { line-height: 1.15; }
table { border-collapse: collapse; width: 100%; margin: 12px 0 24px; font-size: 14px; }
th, td { border: 1px solid #ccc; padding: 6px 8px; vertical-align: top; }
th { background: #f2f2f2; text-align: left; }
pre { background: #111; color: #eee; padding: 12px; overflow-x: auto; border-radius: 6px; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
a { color: #0757a8; }
"""
    return "<!doctype html><html><head><meta charset='utf-8'><title>qdistro CI report</title><style>" + style + "</style></head><body>" + "\n".join(body_lines) + "</body></html>"


def linkify(text: str) -> str:
    # Convert Markdown links to HTML links while escaping non-link text once.
    out: list[str] = []
    pos = 0
    pattern = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for match in pattern.finditer(text):
        out.append(html.escape(text[pos : match.start()]))
        out.append(
            f'<a href="{html.escape(match.group(2), quote=True)}">'
            f'{html.escape(match.group(1))}</a>'
        )
        pos = match.end()
    out.append(html.escape(text[pos:]))
    return "".join(out)

def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: report.py <run-dir>", file=sys.stderr)
        return 2
    run_dir = Path(argv[1]).resolve()
    if not run_dir.is_dir():
        print(f"not a directory: {run_dir}", file=sys.stderr)
        return 2
    markdown = generate_md(run_dir)
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    (run_dir / "report.html").write_text(generate_html(run_dir, markdown), encoding="utf-8")
    (run_dir / "summary.json").write_text(
        json.dumps(generate_summary(run_dir), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(run_dir / "report.md")
    print(run_dir / "report.html")
    print(run_dir / "summary.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
