"""Safe save: atomic write + 3-way merge ladder.

Every user-content write in qnotebook routes through SafeWriter so concurrent
edits from Syncthing/rsync/other editors are detected and merged rather
than silently overwritten. The merge ladder escalates through progressively
more capable tools; the chosen rung is logged to ``.qnotebook/merge.log``.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HAS_GIT_MERGE_FILE = shutil.which("git") is not None
HAS_WIGGLE = shutil.which("wiggle") is not None


def _find_mergiraf() -> str | None:
    from_path = shutil.which("mergiraf")
    if from_path:
        return from_path
    for candidate in (
        Path.home() / ".cargo" / "bin" / "mergiraf",
        Path.home() / ".local" / "bin" / "mergiraf",
        Path("/usr/local/bin/mergiraf"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


MERGIRAF_PATH = _find_mergiraf()
HAS_MERGIRAF = MERGIRAF_PATH is not None


# ------------------------------------------------------------------
# Atomic write
# ------------------------------------------------------------------


def atomic_write(path: Path, data: bytes, *, retries: int = 1) -> None:
    """Write ``data`` to ``path`` atomically via same-dir tempfile + os.replace.

    Retries once if the source file vanishes mid-write (rsync-style replace).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    last_err: Exception | None = None
    for _attempt in range(retries + 1):
        try:
            fd, tmp_name = tempfile.mkstemp(
                prefix=".qnotebook-tmp-", dir=str(path.parent)
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, path)
                return
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
        except FileNotFoundError as e:
            last_err = e
            time.sleep(0.01)
            continue
    if last_err:
        raise last_err


def detect_sync_conflict_siblings(path: Path) -> list[Path]:
    """Find Syncthing conflict files next to ``path`` (``*.sync-conflict-*``)."""
    parent = path.parent
    if not parent.is_dir():
        return []
    stem = path.stem
    suffix = path.suffix
    out: list[Path] = []
    for entry in parent.iterdir():
        name = entry.name
        if name.startswith(stem + ".sync-conflict-") and name.endswith(suffix):
            out.append(entry)
    return out


# ------------------------------------------------------------------
# Hash / load
# ------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class LoadResult:
    original: bytes       # original — raw disk bytes at load time
    baseline: bytes       # B — serialize(parse(original)), the editor's baseline
    hash_original: str


@dataclass
class DeferredSave:
    path: Path
    editor_bytes: bytes
    load: LoadResult
    serialize_fn: Any = None
    root: Path | None = None
    write: bool = True
    strict_preserve: bool = True


# ------------------------------------------------------------------
# Save result
# ------------------------------------------------------------------


@dataclass
class SaveResult:
    status: str           # "ok" | "conflict" | "refused" | "needs_merge"
    bytes: bytes = b""
    rung: str = ""
    reason: str = ""
    base: bytes = b""
    ours: bytes = b""
    theirs: bytes = b""
    conflict_markers: bytes = b""
    deferred: DeferredSave | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def conflict(self) -> bool:
        return self.status == "conflict"

    @property
    def refused(self) -> bool:
        return self.status == "refused"

    @property
    def needs_merge(self) -> bool:
        return self.status == "needs_merge"


# ------------------------------------------------------------------
# Diff helpers
# ------------------------------------------------------------------


def _split_lines(data: bytes) -> list[bytes]:
    """Split bytes keeping line endings, like splitlines(keepends=True)."""
    return data.splitlines(keepends=True)


def _hunks(a: list[bytes], b: list[bytes]) -> list[tuple[int, int, int, int]]:
    """Return list of (a_start, a_end, b_start, b_end) for non-equal ops."""
    matcher = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            out.append((i1, i2, j1, j2))
    return out


def _ranges_disjoint(hunks_a: list[tuple[int, int, int, int]],
                     hunks_b: list[tuple[int, int, int, int]]) -> bool:
    """Do the A-side line ranges of the two hunk sets overlap?"""
    ra = [(h[0], h[1]) for h in hunks_a]
    rb = [(h[0], h[1]) for h in hunks_b]
    for sa, ea in ra:
        for sb, eb in rb:
            # Treat zero-length hunks as occupying position sa..sa+1 for safety
            la = max(ea - sa, 1)
            lb = max(eb - sb, 1)
            if not (sa + la <= sb or sb + lb <= sa):
                return False
    return True


def _apply_three_way_disjoint(original: bytes, E: bytes, D: bytes) -> bytes | None:
    """If original->E and original->D hunks are line-disjoint, produce union by picking
    the non-original side at each line. Returns None if not disjoint."""
    o_lines = _split_lines(original)
    e_lines = _split_lines(E)
    d_lines = _split_lines(D)

    oe_hunks = _hunks(o_lines, e_lines)
    od_hunks = _hunks(o_lines, d_lines)

    if not _ranges_disjoint(oe_hunks, od_hunks):
        return None

    # Walk original line-by-line, taking E's substitution if this line is in an OE
    # hunk, D's if in an OD hunk, else original's line. Merge replacements by range.
    result: list[bytes] = []
    i = 0
    n = len(o_lines)

    oe_by_start = {h[0]: h for h in oe_hunks}
    od_by_start = {h[0]: h for h in od_hunks}

    # Insertion hunks (i2 == i1) — capture so we can splice them in at i1.
    # Accumulate pending inserts keyed by position.
    oe_inserts: dict[int, list[bytes]] = {}
    od_inserts: dict[int, list[bytes]] = {}
    for (i1, i2, j1, j2) in oe_hunks:
        if i1 == i2:
            oe_inserts.setdefault(i1, []).extend(e_lines[j1:j2])
    for (i1, i2, j1, j2) in od_hunks:
        if i1 == i2:
            od_inserts.setdefault(i1, []).extend(d_lines[j1:j2])

    while i <= n:
        if i in oe_inserts:
            result.extend(oe_inserts[i])
        if i in od_inserts:
            result.extend(od_inserts[i])
        if i == n:
            break
        # Is i the start of a substitution/deletion hunk?
        if i in oe_by_start and oe_by_start[i][0] != oe_by_start[i][1]:
            i1, i2, j1, j2 = oe_by_start[i]
            result.extend(e_lines[j1:j2])
            i = i2
            continue
        if i in od_by_start and od_by_start[i][0] != od_by_start[i][1]:
            i1, i2, j1, j2 = od_by_start[i]
            result.extend(d_lines[j1:j2])
            i = i2
            continue
        result.append(o_lines[i])
        i += 1

    return b"".join(result)


# ------------------------------------------------------------------
# External merge tools
# ------------------------------------------------------------------


def _git_merge_file(original: bytes, E: bytes, D: bytes) -> tuple[bool, bytes]:
    """Run ``git merge-file -p --diff3 E original D``.

    Returns (clean, output). ``clean`` is True iff merge returned 0 (no
    conflicts). Output is the merged bytes (with conflict markers if not
    clean)."""
    if not HAS_GIT_MERGE_FILE:
        return False, b""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "ours").write_bytes(E)
        (tdp / "base").write_bytes(original)
        (tdp / "theirs").write_bytes(D)
        res = subprocess.run(
            ["git", "merge-file", "-p", "--diff3",
             str(tdp / "ours"), str(tdp / "base"), str(tdp / "theirs")],
            capture_output=True,
        )
    # Exit code: 0 = clean, >0 = number of conflicts, <0 = error
    clean = res.returncode == 0
    return clean, res.stdout


def _wiggle(original: bytes, E: bytes, D: bytes) -> tuple[bool, bytes]:
    """Try to apply the original->E patch onto D with wiggle -r.

    Returns (clean, output). clean means wiggle rewrote with no unresolved."""
    if not HAS_WIGGLE:
        return False, b""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        target = tdp / "target"
        target.write_bytes(D)
        base = tdp / "base"
        base.write_bytes(original)
        new = tdp / "new"
        new.write_bytes(E)
        # Generate diff from original -> E, apply onto D with wiggle
        diff = subprocess.run(
            ["diff", "-u", str(base), str(new)], capture_output=True
        )
        if not diff.stdout:
            return True, D
        res = subprocess.run(
            ["wiggle", "--merge", "--replace", str(target)],
            input=diff.stdout, capture_output=True,
        )
        merged = target.read_bytes() if target.exists() else b""
    # wiggle exit: 0 = clean, 1 = conflicts remain, 2 = error
    if res.returncode == 0 and merged and b"<<<<<<<" not in merged:
        return True, merged
    return False, merged


def _mergiraf(original: bytes, E: bytes, D: bytes, ext: str = ".md") -> tuple[bool, bytes]:
    """Run mergiraf on (E, original, D). Returns (clean, output)."""
    if not HAS_MERGIRAF:
        return False, b""
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ours = tdp / ("ours" + ext)
        base = tdp / ("base" + ext)
        theirs = tdp / ("theirs" + ext)
        ours.write_bytes(E)
        base.write_bytes(original)
        theirs.write_bytes(D)
        res = subprocess.run(
            [MERGIRAF_PATH, "merge",
             str(base), str(ours), str(theirs)],
            capture_output=True,
        )
    clean = res.returncode == 0 and b"<<<<<<<" not in res.stdout
    return clean, res.stdout


# ------------------------------------------------------------------
# Whitespace normalization
# ------------------------------------------------------------------


def _strip_conflict_markers(data: bytes) -> bytes:
    lines = data.splitlines(keepends=True)
    out = []
    skip_until = None
    for line in lines:
        ls = line.lstrip()
        if ls.startswith(b"<<<<<<<"):
            skip_until = b">>>>>>>"
            continue
        if skip_until and ls.startswith(skip_until):
            skip_until = None
            continue
        if skip_until:
            continue
        out.append(line)
    return b"".join(out)


def _whitespace_only_diff(a: bytes, b: bytes) -> bool:
    """True if a and b differ only in whitespace / line endings."""
    def norm(x: bytes) -> bytes:
        # Normalize CRLF/CR to LF, then strip trailing whitespace per line,
        # then drop blank lines.
        x = x.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        return b"\n".join(line.rstrip() for line in x.split(b"\n") if line.strip())
    return norm(a) == norm(b)


# ------------------------------------------------------------------
# Roundtrip guard
# ------------------------------------------------------------------


def _roundtrip_parses(data: bytes) -> bool:
    """Best-effort: will the parser accept this without exploding?"""
    try:
        import mistune
    except Exception:
        return True  # no parser available — don't block
    try:
        md = mistune.create_markdown(renderer=None)
        md(data.decode("utf-8", errors="replace"))
        return True
    except Exception:
        return False


# ------------------------------------------------------------------
# Merge log
# ------------------------------------------------------------------


def _log_rung(root: Path | None, path: Path, rung: str) -> None:
    if root is None:
        return
    log_dir = root / ".qnotebook"
    try:
        log_dir.mkdir(exist_ok=True)
        log = log_dir / "merge.log"
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = path
        with log.open("a", encoding="utf-8") as f:
            f.write(f"{ts}\t{rung}\t{rel}\n")
    except OSError:
        pass


# ------------------------------------------------------------------
# SafeWriter
# ------------------------------------------------------------------


class SafeWriter:
    """File writer with merge-ladder conflict resolution.

    Typical use:

        lr = SafeWriter.load(path, serialize_fn)
        # ... user edits ...
        result = SafeWriter.save(path, editor_bytes, lr, serialize_fn,
                                 root=notebook_root)
        if result.ok:
            # editor model can pin to result.bytes
            pass
        elif result.conflict:
            # show 3-pane dialog with result.base/ours/theirs
            pass
    """

    @staticmethod
    def load(
        path: Path,
        serialize_fn=None,
    ) -> LoadResult:
        path = Path(path)
        if path.is_file():
            original = path.read_bytes()
        else:
            original = b""
        if serialize_fn is not None:
            try:
                baseline = serialize_fn(original)
            except Exception:
                baseline = original
        else:
            baseline = original
        return LoadResult(
            original=original,
            baseline=baseline,
            hash_original=sha256_bytes(original),
        )

    @staticmethod
    def save(
        path: Path,
        E: bytes,
        load: LoadResult,
        serialize_fn=None,
        *,
        root: Path | None = None,
        write: bool = True,
        strict_preserve: bool = True,
        allow_subprocess: bool = True,
    ) -> SaveResult:
        path = Path(path)
        if isinstance(E, str):
            E = E.encode("utf-8")
        original = load.original
        deferred = DeferredSave(
            path=path,
            editor_bytes=E,
            load=load,
            serialize_fn=serialize_fn,
            root=root,
            write=write,
            strict_preserve=strict_preserve,
        )

        # ---- preserve-phase: restore plugin-metadata-bearing lines the user
        # didn't touch. When the serializer canonicalized unknown constructs
        # at load time (load.baseline != load.original), merging
        # base=baseline, ours=E, theirs=original line-by-line resurrects the
        # original bytes for every region the editor didn't modify.
        if strict_preserve and load.baseline and load.baseline != load.original:
            restored = _apply_three_way_disjoint(load.baseline, E, load.original)
            if restored is not None:
                E = restored
            elif HAS_GIT_MERGE_FILE:
                if not allow_subprocess:
                    return SaveResult(
                        status="needs_merge",
                        reason="strict-preserve",
                        base=load.baseline,
                        ours=E,
                        theirs=load.original,
                        deferred=deferred,
                    )
                clean, out = _git_merge_file(load.baseline, E, load.original)
                if clean and _roundtrip_parses(out):
                    E = out

        # ---- rung (a): trivial — disk unchanged since load
        disk = path.read_bytes() if path.is_file() else b""
        if sha256_bytes(disk) == load.hash_original:
            if write:
                atomic_write(path, E)
            _log_rung(root, path, "trivial")
            return SaveResult(status="ok", bytes=E, rung="trivial")

        D = disk  # external / disk bytes (theirs)

        # If disk matches our editor exactly, nothing to merge.
        if D == E:
            _log_rung(root, path, "noop-match")
            return SaveResult(status="ok", bytes=E, rung="noop-match")

        # ---- rung (b): disjoint-hunks fast path
        merged = _apply_three_way_disjoint(original, E, D)
        if merged is not None and _roundtrip_parses(merged):
            if write:
                atomic_write(path, merged)
            _log_rung(root, path, "disjoint-hunks")
            return SaveResult(status="ok", bytes=merged, rung="disjoint-hunks")

        if not allow_subprocess and (
            HAS_GIT_MERGE_FILE or HAS_WIGGLE or HAS_MERGIRAF
        ):
            return SaveResult(
                status="needs_merge",
                reason="external-merge",
                base=original,
                ours=E,
                theirs=D,
                deferred=deferred,
            )

        # ---- rung (c): git merge-file
        if HAS_GIT_MERGE_FILE:
            clean, out = _git_merge_file(original, E, D)
            if clean and _roundtrip_parses(out):
                if write:
                    atomic_write(path, out)
                _log_rung(root, path, "git-merge-file")
                return SaveResult(status="ok", bytes=out, rung="git-merge-file")
            gmf_out = out
        else:
            gmf_out = b""

        # ---- rung (d): wiggle fuzzy patch
        if HAS_WIGGLE:
            clean, out = _wiggle(original, E, D)
            if clean and out and _roundtrip_parses(out):
                if write:
                    atomic_write(path, out)
                _log_rung(root, path, "wiggle")
                return SaveResult(status="ok", bytes=out, rung="wiggle")

        # ---- rung (e): mergiraf structural merge
        if HAS_MERGIRAF:
            clean, out = _mergiraf(original, E, D, ext=path.suffix or ".md")
            if clean and out and _roundtrip_parses(out):
                if write:
                    atomic_write(path, out)
                _log_rung(root, path, "mergiraf")
                return SaveResult(status="ok", bytes=out, rung="mergiraf")

        # ---- rung (f): whitespace-only — prefer ours silently
        if gmf_out and _whitespace_only_diff(
            _strip_conflict_markers(gmf_out), E
        ):
            if write:
                atomic_write(path, E)
            _log_rung(root, path, "whitespace-ours")
            return SaveResult(status="ok", bytes=E, rung="whitespace-ours")

        # ---- rung (g): roundtrip guard is implicit above — any would-be
        # clean merge that doesn't parse was rejected.

        # ---- rung (h): fallthrough — surface conflict to UI
        _log_rung(root, path, "conflict")
        return SaveResult(
            status="conflict",
            base=original,
            ours=E,
            theirs=D,
            rung="conflict",
            conflict_markers=gmf_out,
        )


def run_deferred_save(deferred: DeferredSave) -> SaveResult:
    """Finish a save that the GUI fast path stopped before subprocess rungs."""
    return SafeWriter.save(
        deferred.path,
        deferred.editor_bytes,
        deferred.load,
        deferred.serialize_fn,
        root=deferred.root,
        write=deferred.write,
        strict_preserve=deferred.strict_preserve,
        allow_subprocess=True,
    )
