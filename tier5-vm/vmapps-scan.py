#!/usr/bin/env python3
"""qdistro-tier5-vmapps-scan — derive the tier-5 VM-app catalogue from the
guest base image and publish it as a JSON manifest.

What it does
------------
Reads ``/usr/local/share/applications`` and ``/usr/share/applications``
inside the tier-5 guest base qcow2 (read-only, via libguestfs), applies
desktop-entry precedence/filtering, maps desktop IDs onto the STABLE
``tier5/<appId>`` keys that Settings/policy/lifecycle configuration is
already keyed on, and publishes the result to
``/usr/share/qdistro/vmapps/base.json`` — the file
``qdshell/Services/Qdistro/VMApps.qml`` loads with ``watchChanges``.

Before this, the catalogue was a hardcoded literal in VMApps.qml and
advertised apps that might not exist in the guest base.

Design notes (vmapps-dynamic plan, todo/vmapps-dynamic/01-plan.md):

- **The menu is discovered from the guest base, not baked into QML.** The
  only prebaked entry anywhere is foot: the QML loader's deterministic
  fallback, and the scanner REFUSES to publish a catalogue-bearing
  manifest for a base that lacks a guest ``foot`` binary (foot is the
  operator's escape hatch when discovery misfires).
- **Stable appIds.** Known upstream desktop IDs alias to the historical
  keys (``org.kde.konsole`` → ``tier5/konsole``) so existing Settings
  overrides, ``--policy-key``, VM naming and broker selectors keep
  working. Unmapped apps get ``tier5/<desktopId>``.
- **Atomic publish**: manifest is written to a temp file in the
  destination directory and renamed; 0644 root:root.
- **Idempotence lives in the caller.** The scanner always emits; the
  ``qdistro-tier5-vmapps-scan`` service (vmapps-reconcile.sh) owns the
  fast path, identity sampling, bounded retries and stale-manifest
  publication.

Usage:

  vmapps-scan.py [--base QCOW2] [--out MANIFEST] [--denylist FILE]
  vmapps-scan.py --root DIR [--out MANIFEST]      (host-dir source; tests)
  vmapps-scan.py --check MANIFEST [--base QCOW2]  (validate + freshness)
  vmapps-scan.py --emit-stale OUT --reason TEXT   (atomic stale marker)

Exit codes: 0 ok · 1 scan/validation failure · 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import stat as statmod
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

SCHEMA = 1
SCANNER_ID = "vmapps-scan.py 1"

DEFAULT_BASE = "/var/lib/libvirt/images/qdistro-tier5-base.qcow2"
DEFAULT_OUT = "/usr/share/qdistro/vmapps/base.json"
DEFAULT_DENYLIST = "/etc/qdistro/vmapps-denylist"

# XDG desktop-file search dirs inside the guest, highest precedence first.
APPS_DIRS = ("/usr/local/share/applications", "/usr/share/applications")

# Bare TryExec/program names resolve against these guest dirs, in order.
BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")

# foot is the operator escape hatch and the loader's deterministic
# fallback. A catalogue-bearing base with no guest foot binary makes the
# scan fail rather than publish a menu that cannot offer it.
FOOT_CANDIDATES = ("/usr/local/bin/foot", "/usr/bin/foot")

# Historical appId assignments. Known desktop IDs map onto these so the
# policy key (appId) is stable; everything else is tier5/<desktopId>.
LEGACY_APP_IDS = {
    "firefox": "tier5/firefox",
    "org.kde.konsole": "tier5/konsole",
    "org.kde.kate": "tier5/kate",
    "org.kde.dolphin": "tier5/dolphin",
    "org.kde.kcalc": "tier5/kcalc",
    "org.gnome.baobab": "tier5/baobab",
    "org.gnome.TextEditor": "tier5/gnome-text-editor",
    "org.gnome.Nautilus": "tier5/nautilus",
    "org.gnome.Calculator": "tier5/gnome-calculator",
}

# Never offer these regardless of what the guest ships: a fresh per-app
# VM has no foot server (footclient exits immediately) and a foot server
# opens no window. The drop-in denylist file can extend this.
BUILTIN_DENYLIST = {"footclient", "foot-server"}

# The spawn wrapper's --policy-key / domain-binding charset — keep in
# sync with the [[ =~ ]] rule in spawn-tier5.sh. Rows whose derived
# appId violates it are dropped before publication. Checked with
# fullmatch: re.match(...$) would accept a trailing '\n' that the
# wrapper's `[[ =~ ]]` rejects.
_APP_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}")

# Exec field codes the launcher side provides at activation time — we
# have no file/URL to substitute, so the token is dropped entirely.
_DROP_TOKENS = {"%f", "%F", "%u", "%U"}

# Backslash escapes inside Exec values (desktop-entry spec "The Exec key").
_ESCAPES = {
    "s": " ", "n": "\n", "t": "\t", "r": "\r",
    '"': '"', "'": "'", "\\": "\\",
}


class ScanError(Exception):
    """Fatal scan failure — the reconcile loop retries/stale-publishes."""


def log(msg: str) -> None:
    print(f"[vmapps-scan] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Exec value handling (desktop-entry spec)
# ---------------------------------------------------------------------------

def tokenize_exec(value: str) -> list[str]:
    """Split an Exec= value into argv tokens.

    Double quotes group; backslash escapes are honoured for the spec's
    reserved set and left literal otherwise.
    """
    args: list[str] = []
    cur: list[str] = []
    quoted = False
    started = False
    i, n = 0, len(value)
    while i < n:
        c = value[i]
        if c == "\\" and i + 1 < n:
            nxt = value[i + 1]
            cur.append(_ESCAPES.get(nxt, "\\" + nxt))
            started = True
            i += 2
            continue
        if c == '"':
            quoted = not quoted
            started = True
            i += 1
            continue
        if c in " \t" and not quoted:
            if started:
                args.append("".join(cur))
                cur, started = [], False
            i += 1
            continue
        cur.append(c)
        started = True
        i += 1
    if started:
        args.append("".join(cur))
    return args


def expand_field_codes(
    args: list[str], *, icon: str, name: str, path: str
) -> list[str] | None:
    """Resolve XDG field codes.

    Whole-token: %f/%F/%u/%U drop; %i -> --icon <icon>; %c -> Name;
    %k -> .desktop path. Embedded: %% -> %, %c/%k substitute in place.
    Any other %-sequence (deprecated codes, embedded %i/%f/%u, stray %)
    returns None so the caller drops the row and logs.
    """
    out: list[str] = []
    for arg in args:
        if arg in _DROP_TOKENS:
            continue
        if arg == "%i":
            if icon:
                out += ["--icon", icon]
            continue
        if arg == "%c":
            out.append(name)
            continue
        if arg == "%k":
            out.append(path)
            continue
        if "%" not in arg:
            out.append(arg)
            continue
        expanded = ""
        i = 0
        while i < len(arg):
            c = arg[i]
            if c == "%":
                if i + 1 >= len(arg):
                    return None
                code = arg[i + 1]
                if code == "%":
                    expanded += "%"
                elif code == "c":
                    expanded += name
                elif code == "k":
                    expanded += path
                else:
                    return None
                i += 2
            else:
                expanded += c
                i += 1
        out.append(expanded)
    return out


# ---------------------------------------------------------------------------
# Guest-content sources
# ---------------------------------------------------------------------------

class RootDirSource:
    """Read guest content from a host directory (tests / offline review).

    Binary stat()s and .desktop enumeration resolve <root>/<guest path>.
    """

    def __init__(self, root: str) -> None:
        self.root = root
        if not os.path.isdir(root):
            raise ScanError(f"--root {root}: not a directory")

    def _p(self, guest_path: str) -> str:
        return os.path.join(self.root, guest_path.lstrip("/"))

    def desktops(self) -> list[tuple[str, dict[str, bytes]]]:
        out: list[tuple[str, dict[str, bytes]]] = []
        for d in APPS_DIRS:
            base = self._p(d)
            entries: dict[str, bytes] = {}
            if os.path.isdir(base):
                for dirpath, _dirs, files in os.walk(base):
                    for fname in files:
                        if not fname.endswith(".desktop"):
                            continue
                        full = os.path.join(dirpath, fname)
                        rel = os.path.relpath(full, base)
                        try:
                            with open(full, "rb") as fh:
                                entries[rel] = fh.read()
                        except OSError as exc:
                            log(f"unreadable desktop file {full}: {exc}")
            out.append((d, entries))
        return out

    def stat(self, guest_path: str) -> tuple[bool, int]:
        try:
            st = os.stat(self._p(guest_path))
        except OSError:
            return False, 0
        return True, st.st_mode


class GuestfsSource:
    """Read guest content out of the base qcow2 via libguestfs.

    Two operations only: ``tar-out`` per applications dir (enumeration +
    contents in one shot) and one batched ``stat`` per candidate program
    path. Inspection (-i) mounts the guest root; the image is opened
    read-only.
    """

    def __init__(self, base: str) -> None:
        self.base = base
        if not os.path.isfile(base):
            raise ScanError(f"base {base}: not a file")
        if not os.access(base, os.R_OK):
            raise ScanError(f"base {base}: not readable")
        for tool in ("guestfish",):
            if not _which(tool):
                raise ScanError(f"missing tool: {tool}")

    def _run(self, script: str) -> subprocess.CompletedProcess[bytes]:
        # Never propagate a per-command error as a dead run: guestfish in
        # stdin-script mode continues past failed commands and may still
        # exit nonzero. Callers parse output and decide.
        return subprocess.run(
            ["guestfish", "--ro", "-a", self.base, "-i"],
            input=script.encode(), capture_output=True, check=False,
        )

    def desktops(self) -> list[tuple[str, dict[str, bytes]]]:
        out: list[tuple[str, dict[str, bytes]]] = []
        for d in APPS_DIRS:
            entries: dict[str, bytes] = {}
            with tempfile.TemporaryFile() as tf:
                proc = subprocess.run(
                    ["guestfish", "--ro", "-a", self.base, "-i",
                     "tar-out", d, "-"],
                    stdout=tf, stderr=subprocess.PIPE, check=False,
                )
                if proc.returncode != 0:
                    err = proc.stderr.decode(errors="replace")
                    if "No such file" in err or "no such file" in err:
                        out.append((d, entries))
                        continue
                    raise ScanError(f"tar-out {d} failed: {err.strip()}")
                tf.seek(0)
                try:
                    with tarfile.open(fileobj=tf) as tar:
                        for member in tar.getmembers():
                            if not member.isfile():
                                continue
                            rel = member.name.lstrip("./")
                            if not rel.endswith(".desktop"):
                                continue
                            fh = tar.extractfile(member)
                            if fh is not None:
                                entries[rel] = fh.read()
                except tarfile.TarError as exc:
                    raise ScanError(f"tar-out {d}: unreadable tar: {exc}")
            out.append((d, entries))
        return out

    def stat(self, guest_path: str) -> tuple[bool, int]:
        return self._stats([guest_path]).get(guest_path, (False, 0))

    def stats(self, paths: list[str]) -> dict[str, tuple[bool, int]]:
        return self._stats(paths)

    def _stats(self, paths: list[str]) -> dict[str, tuple[bool, int]]:
        if not paths:
            return {}
        script = "".join(
            f'echo "::stat::{p}"\nstat "{p}"\n' for p in paths
        )
        proc = self._run(script)
        text = proc.stdout.decode(errors="replace")
        result: dict[str, tuple[bool, int]] = {}
        current: str | None = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("::stat::"):
                current = line[len("::stat::"):].strip()
                result[current] = (False, 0)
                continue
            if current is None:
                continue
            if line.startswith("mode:"):
                try:
                    mode = int(line.split(":", 1)[1].strip())
                except ValueError:
                    continue
                result[current] = (True, mode)
        return result


def _which(tool: str) -> str | None:
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = os.path.join(d, tool)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# ---------------------------------------------------------------------------
# Desktop-entry selection
# ---------------------------------------------------------------------------

def _parse_desktop(data: bytes, ident: str) -> configparser.RawConfigParser | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        log(f"{ident}: not utf-8 — skipped")
        return None
    parser = configparser.RawConfigParser(
        delimiters=("=",), interpolation=None, strict=False,
    )
    parser.optionxform = str  # type: ignore[assignment]
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        log(f"{ident}: unparseable desktop file ({exc}) — skipped")
        return None
    if "Desktop Entry" not in parser:
        log(f"{ident}: no [Desktop Entry] section — skipped")
        return None
    return parser


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


def _load_denylist(path: str | None) -> set[str]:
    denied = set(BUILTIN_DENYLIST)
    cand = path or DEFAULT_DENYLIST
    if cand and os.path.isfile(cand):
        try:
            with open(cand, encoding="utf-8") as fh:
                for line in fh:
                    line = line.split("#", 1)[0].strip()
                    if line:
                        denied.add(line)
        except OSError as exc:
            log(f"denylist {cand} unreadable: {exc} — ignored")
    return denied


def _denied(denylist: set[str], desktop_id: str, argv0: str) -> bool:
    stem = desktop_id.rsplit("/", 1)[-1]
    if stem.endswith(".desktop"):
        stem = stem[: -len(".desktop")]
    return (
        desktop_id in denylist
        or stem in denylist
        or os.path.basename(argv0) in denylist
    )


def _exec_ok(src, guest_path: str) -> bool:
    exists, mode = src.stat(guest_path)
    return exists and statmod.S_ISREG(mode) and bool(mode & 0o111)


def _resolve_prog(src, name: str) -> str | None:
    """Return the guest path that satisfies a bare/absolute program name."""
    if "/" in name:
        return name if _exec_ok(src, name) else None
    for d in BIN_DIRS:
        cand = os.path.join(d, name)
        if _exec_ok(src, cand):
            return cand
    return None


def select_apps(
    src,
    denylist_path: str | None = None,
) -> list[dict[str, str]]:
    """Enumerate, filter and map desktop entries to manifest rows."""
    denylist = _load_denylist(denylist_path)

    # Merge precedence: first (highest-precedence) occurrence of a desktop
    # ID wins; a Hidden winner masks the id entirely.
    winners: dict[str, tuple[str, bytes]] = {}
    hidden: set[str] = set()
    for d, files in src.desktops():
        for rel, data in files.items():
            # Desktop file ID: path relative to the applications dir,
            # '/' → '-', without the .desktop suffix.
            desktop_id = rel.replace("/", "-")[: -len(".desktop")]
            if desktop_id in winners or desktop_id in hidden:
                continue
            parsed = _parse_desktop(data, f"{d}/{rel}")
            if parsed is None:
                # Malformed higher-precedence file does NOT mask — it
                # simply isn't a valid winner. Treat like a missing file
                # at this level but keep scanning (a lower file with the
                # same id can still win, matching XDG merge semantics).
                continue
            ent = parsed["Desktop Entry"]
            if _truthy(ent.get("Hidden")):
                hidden.add(desktop_id)
                log(f"{desktop_id}: Hidden — masked")
                continue
            winners[desktop_id] = (f"{d}/{rel}", data)

    rows: list[dict[str, str]] = []
    for desktop_id, (path, data) in sorted(winners.items()):
        parsed = _parse_desktop(data, path)
        if parsed is None:  # already validated above; defensive
            continue
        ent = parsed["Desktop Entry"]

        if (ent.get("Type") or "").strip() != "Application":
            log(f"{desktop_id}: Type != Application — skipped")
            continue
        name = (ent.get("Name") or "").strip()
        if not name:
            log(f"{desktop_id}: no Name — skipped")
            continue
        if _truthy(ent.get("NoDisplay")):
            log(f"{desktop_id}: NoDisplay — skipped")
            continue
        if _truthy(ent.get("Terminal")):
            log(f"{desktop_id}: Terminal=true (console app) — skipped")
            continue
        if (ent.get("OnlyShowIn") or "").strip():
            log(f"{desktop_id}: OnlyShowIn set — skipped")
            continue
        # NotShowIn is ignored deliberately: a base image is shared and
        # there is no DE here to exclude.

        exec_value = (ent.get("Exec") or "").strip()
        if not exec_value:
            # DBusActivatable entries are kept only when Exec is usable;
            # with no Exec there is nothing to launch over waypipe.
            log(f"{desktop_id}: no Exec — skipped")
            continue

        icon = (ent.get("Icon") or "").strip()
        comment = (ent.get("Comment") or ent.get("GenericName") or "").strip()

        argv = expand_field_codes(
            tokenize_exec(exec_value), icon=icon, name=name, path=path,
        )
        if argv is None:
            log(f"{desktop_id}: unsupported field code in Exec — skipped")
            continue
        if not argv or not argv[0]:
            log(f"{desktop_id}: Exec expands to empty argv — skipped")
            continue

        tryexec = (ent.get("TryExec") or "").strip()
        if tryexec:
            if _resolve_prog(src, tryexec) is None:
                log(f"{desktop_id}: TryExec {tryexec!r} not executable — skipped")
                continue

        if _denied(denylist, desktop_id, argv[0]):
            log(f"{desktop_id}: denylisted — skipped")
            continue

        app_id = LEGACY_APP_IDS.get(desktop_id, f"tier5/{desktop_id}")
        # The appId becomes the spawn wrapper's --policy-key AND the
        # domain's bound identity, so it must satisfy the wrapper's key
        # charset — an unshapeable desktop filename (spaces, +, UTF-8)
        # would otherwise reach launch() and die at the wrapper, or
        # worse, alias another app's binding (sol r4). Drop and log
        # rather than publish an unusable row.
        if not _APP_ID_RE.fullmatch(app_id):
            log(f"{desktop_id}: appId {app_id!r} outside the policy-key "
                f"charset — dropped")
            continue

        rows.append(
            {
                "appId": app_id,
                "desktopId": desktop_id,
                "name": name,
                "iconName": icon,
                "comment": comment,
                # Stored stringified — the loader and today's catalogue
                # rows share the JSON-encoded execArgv shape.
                "execArgv": json.dumps(argv),
            }
        )

    # appId collision policy: a row carrying a KNOWN legacy mapping wins;
    # the duplicate is dropped and logged. Between two unmapped rows the
    # lexically-first desktopId wins for determinism. The test is on the
    # row's desktopId — the resulting appId is the same colliding key, so
    # it cannot be the discriminator.
    final: dict[str, dict[str, str]] = {}
    for row in sorted(rows, key=lambda r: (r["desktopId"] not in LEGACY_APP_IDS,
                                           r["desktopId"])):
        aid = row["appId"]
        if aid in final:
            log(f"{row['desktopId']}: appId {aid} already claimed by "
                f"{final[aid]['desktopId']} — dropped")
            continue
        final[aid] = row

    return sorted(final.values(), key=lambda r: r["appId"])


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _base_identity(base_path: str) -> dict[str, int | str]:
    st = os.stat(base_path)
    return {
        "base": base_path,
        "baseDev": st.st_dev,
        "baseIno": st.st_ino,
        "baseMtime": int(st.st_mtime),
        "baseSize": st.st_size,
    }


def _publish(out_path: str, doc: dict) -> None:
    """Atomic publish: temp file in the destination dir + rename, 0644."""
    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=out_dir, prefix=".base.json.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_manifest(src, base_path: str, denylist: str | None) -> dict:
    apps = select_apps(src, denylist)
    if apps:
        foot = next(
            (p for p in FOOT_CANDIDATES if _exec_ok(src, p)), None,
        )
        if foot is None:
            raise ScanError(
                "base carries a non-empty app catalogue but no guest foot "
                "binary — refusing to publish a manifest without the "
                "operator escape hatch (install foot in the guest base)"
            )
    doc = {
        "schema": SCHEMA,
        "scanner": SCANNER_ID,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "apps": apps,
    }
    doc.update(_base_identity(base_path))
    return doc


def _row_ok(row) -> bool:
    if not isinstance(row, dict):
        return False
    for key in ("appId", "desktopId", "name", "iconName", "comment"):
        if not isinstance(row.get(key), str):
            return False
    argv_field = row.get("execArgv")
    if not isinstance(argv_field, str):
        return False
    try:
        argv = json.loads(argv_field)
    except (ValueError, TypeError):
        return False
    return (
        isinstance(argv, list)
        and len(argv) > 0
        and all(isinstance(a, str) for a in argv)
        and bool(argv[0])
    )


def manifest_problems(doc) -> list[str]:
    """Structural checks shared by --check and the reconcile fast path."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["manifest is not a JSON object"]
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema {doc.get('schema')!r} != {SCHEMA}")
    if doc.get("stale"):
        problems.append(f"stale marker present: {doc['stale']}")
    apps = doc.get("apps")
    if not isinstance(apps, list):
        problems.append("apps is not a list")
    elif not apps:
        problems.append("apps is empty")
    else:
        bad = [i for i, r in enumerate(apps) if not _row_ok(r)]
        if bad:
            problems.append(f"malformed app rows at {bad}")
    for key in ("base", "baseDev", "baseIno", "baseMtime", "baseSize"):
        if key not in doc:
            problems.append(f"missing identity field {key}")
    return problems


def cmd_check(manifest_path: str, base_path: str) -> int:
    """Validate a manifest AND confirm its identity tuple still matches
    the current base. 0 fresh+usable, 1 otherwise."""
    try:
        doc = json.loads(Path(manifest_path).read_text())
    except (OSError, ValueError) as exc:
        print(f"check: {manifest_path}: {exc}", file=sys.stderr)
        return 1
    problems = manifest_problems(doc)
    if problems:
        print(f"check: {manifest_path}: {'; '.join(problems)}",
              file=sys.stderr)
        return 1
    try:
        ident = _base_identity(base_path)
    except OSError as exc:
        print(f"check: base {base_path}: {exc}", file=sys.stderr)
        return 1
    stale_keys = [
        k for k in ("baseDev", "baseIno", "baseMtime", "baseSize")
        if doc[k] != ident[k]
    ]
    if doc["base"] != base_path:
        stale_keys.append("base")
    if stale_keys:
        print(f"check: identity mismatch on {stale_keys}", file=sys.stderr)
        return 1
    return 0


def cmd_emit_stale(out_path: str, reason: str) -> int:
    _publish(out_path, {"schema": SCHEMA, "stale": reason, "apps": []})
    return 0


def cmd_scan(args) -> int:
    if args.root:
        src: object = RootDirSource(args.root)
        # In --root mode the scanned root stands in for the base unless the
        # caller pinned a different identity file with --base.
        base_for_identity = args.base or args.root
    else:
        src = GuestfsSource(args.base or DEFAULT_BASE)
        base_for_identity = args.base or DEFAULT_BASE
    doc = build_manifest(src, base_for_identity, args.denylist)
    _publish(args.out, doc)
    log(f"published {len(doc['apps'])} app(s) -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="qdistro-tier5-vmapps-scan",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--base", default=None,
                    help=f"guest base qcow2 (default {DEFAULT_BASE}; in "
                         "--root mode, identity file — default: the root)")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"manifest destination (default {DEFAULT_OUT})")
    ap.add_argument("--root", default=None,
                    help="read guest content from a host directory instead "
                         "of a qcow2 (testing/offline review)")
    ap.add_argument("--denylist", default=None,
                    help=f"extra denylist file (default {DEFAULT_DENYLIST})")
    ap.add_argument("--check", metavar="MANIFEST", default=None,
                    help="validate MANIFEST and confirm its identity tuple "
                         "still matches --base; exit 0 when fresh+usable")
    ap.add_argument("--emit-stale", metavar="OUT", default=None,
                    help="publish an atomic empty manifest carrying "
                         "--reason as the stale marker")
    ap.add_argument("--reason", default="reconciler",
                    help="reason text for --emit-stale")
    args = ap.parse_args(argv)

    try:
        if args.emit_stale:
            return cmd_emit_stale(args.emit_stale, args.reason)
        if args.check:
            return cmd_check(args.check, args.base or DEFAULT_BASE)
        return cmd_scan(args)
    except ScanError as exc:
        log(f"FAIL: {exc}")
        return 1
    except OSError as exc:
        log(f"FAIL: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
