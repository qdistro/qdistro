"""Multi-rename plugin for QFileMan.

A batch renaming tool patterned after the Total Commander / Krusader /
Double Commander multi-rename utilities. The user picks a starting
file in the file list and the plugin renames every sibling in the same
directory according to a template.

Template tokens:

* ``[N]``      — original base name (without extension)
* ``[E]``      — original extension (including the dot, or empty)
* ``[C]``      — sequential counter, padded to the configured width
* ``[C:n]``    — sequential counter padded to ``n`` digits

The search/replace pair is applied to the base name *before* the
template is rendered. Setting "Search" to empty disables that step.

The transform — :func:`apply_template` — is a pure function so it can
be tested without touching the filesystem or Qt.
"""

from __future__ import annotations

import logging
import os
import re

from qfileman.plugin import MenuProvider

log = logging.getLogger(__name__)


_COUNTER_TOKEN_RE = re.compile(r"\[C(?::(\d+))?\]")


def apply_template(template: str, original: str, index: int,
                   *, search: str = "", replace: str = "",
                   default_pad: int = 3, regex: bool = False) -> str:
    """Render ``template`` for one file.

    ``original`` is the basename including extension. ``index`` is the
    zero-based position within the batch. ``default_pad`` is the digit
    width used when ``[C]`` appears without an explicit ``:n``.
    """
    base, ext = os.path.splitext(original)
    if search:
        if regex:
            try:
                base = re.sub(search, replace, base)
            except re.error as e:
                log.warning("multi_rename: bad regex %r: %s", search, e)
                # Fall through with unchanged base — the user sees their
                # template applied to the raw name, which is the least
                # surprising failure mode.
        else:
            base = base.replace(search, replace)

    out = template.replace("[N]", base).replace("[E]", ext)

    def _counter(m: re.Match) -> str:
        width = int(m.group(1)) if m.group(1) else default_pad
        return f"{index + 1:0{width}d}"

    return _COUNTER_TOKEN_RE.sub(_counter, out)


def plan_renames(directory: str, names: list[str], template: str, *,
                 search: str = "", replace: str = "",
                 regex: bool = False, default_pad: int = 3) -> list[tuple[str, str]]:
    """Return a list of ``(old_path, new_path)`` pairs for the rename.

    Skips entries that would map to themselves. Does not detect
    collisions among the new names — the rename step does, and surfaces
    the OSError to the caller.
    """
    out: list[tuple[str, str]] = []
    for i, name in enumerate(names):
        new_name = apply_template(
            template, name, i,
            search=search, replace=replace,
            default_pad=default_pad, regex=regex,
        )
        if new_name == name or not new_name:
            continue
        out.append((os.path.join(directory, name), os.path.join(directory, new_name)))
    return out


def _apply_rename_plan(plan: list[tuple[str, str]]) -> list[str]:
    """Execute a rename plan safely; return a list of human-readable failures.

    Renames are staged through a private temp directory first, then moved to
    their final names. This makes in-set collisions (``a->b, b->c`` chains and
    ``a<->b`` swaps) non-destructive: a naive in-order ``os.rename`` would
    clobber the original ``b`` before its own rename ran. Out-of-set
    overwrites are the caller's decision (it has already prompted); here we
    just carry them out. Hardened against three data-loss traps:

    * **duplicate finals** — two sources mapping to the same new name would
      have the second silently destroy the first; such entries are rejected
      up front and reported, never executed.
    * **temp-name collisions** — staging uses ``tempfile.mkdtemp`` inside the
      target directory, so reserved temp names can never clobber a real file
      that happens to look like our scratch name.
    * **partial staging** — if any phase-1 move fails, every already-staged
      file is rolled back to its original and the whole batch aborts, so
      phase 2 can never ``os.replace`` over an unstaged source.
    """
    failures: list[str] = []
    effective = [(old, new) for old, new in plan if old != new]
    if not effective:
        return failures

    # Reject duplicate destination names within the plan (a->x, b->x): we
    # cannot land both, and silently keeping one is the data loss we're here
    # to prevent. Drop every member of a colliding group with a clear error.
    # Canonicalize the collision key (case-fold + Unicode NFC) so that
    # spellings which denote the *same* entry on a case-insensitive or
    # name-normalizing filesystem (a->X, b->x) are also treated as colliding —
    # erring toward rejecting rather than risking a silent clobber.
    import unicodedata
    from collections import Counter

    def _key(p):
        return os.path.normcase(unicodedata.normalize("NFC", os.path.abspath(p)))

    counts = Counter(_key(new) for _o, new in effective)
    runnable = []
    for old, new in effective:
        if counts[_key(new)] > 1:
            failures.append(
                f"{os.path.basename(old)}: target "
                f"{os.path.basename(new)!r} claimed by multiple files")
        else:
            runnable.append((old, new))
    if not runnable:
        return failures

    # A runnable entry whose *final* lands on the original path of a file that
    # is NOT being staged away (e.g. the source of a rejected duplicate, like
    # `c -> a` when `a` was rejected) would have phase 2 silently overwrite
    # that surviving file. Reject such entries too. Removing one makes its own
    # source survive, which can expose another — so iterate to a fixpoint.
    survivors = {_key(old) for old, _new in effective} - {
        _key(old) for old, _new in runnable
    }
    changed = True
    while changed:
        changed = False
        kept = []
        for old, new in runnable:
            if _key(new) in survivors:
                failures.append(
                    f"{os.path.basename(old)}: target "
                    f"{os.path.basename(new)!r} would overwrite a file kept by "
                    f"this batch")
                survivors.add(_key(old))  # this source now survives too
                changed = True
            else:
                kept.append((old, new))
        runnable = kept
    if not runnable:
        return failures

    # All sources live in the same directory (plan_renames guarantees it), so
    # one scratch dir on the same filesystem keeps every move atomic.
    import tempfile
    directory = os.path.dirname(runnable[0][1])
    try:
        scratch = tempfile.mkdtemp(prefix=".qfm-rename-", dir=directory)
    except OSError as e:
        return failures + [f"batch rename: cannot stage: {e}"]

    staged: list[tuple[str, str]] = []  # (temp_path, final_path)
    aborted = False
    try:
        # Phase 1: move every source into the scratch dir. On the first
        # failure, roll back and abort so no source is left unstaged in a way
        # phase 2 could clobber.
        for i, (old, new) in enumerate(runnable):
            tmp = os.path.join(scratch, str(i))
            try:
                os.rename(old, tmp)
            except OSError as e:
                failures.append(f"{os.path.basename(old)}: {e}")
                # Roll back everything staged so far to its original name.
                for done_tmp, _final, orig in staged:
                    try:
                        os.rename(done_tmp, orig)
                    except OSError as re:
                        failures.append(
                            f"{os.path.basename(orig)}: rollback failed: {re}")
                staged.clear()
                aborted = True
                break
            staged.append((tmp, new, old))

        # Phase 2: move each staged temp to its final name. os.replace is
        # intentional — any same-name bystander was confirmed for overwrite by
        # the caller, and in-set finals are free because their originals were
        # staged away in phase 1.
        if not aborted:
            for tmp, new, _old in staged:
                try:
                    os.replace(tmp, new)
                except OSError as e:
                    failures.append(f"{os.path.basename(new)}: {e}")
                    # Leave the temp in scratch so nothing is lost; it will be
                    # surfaced by the non-empty-scratch cleanup below.
    finally:
        # Remove the scratch dir if empty; otherwise leave it (data still
        # inside) and report so the user can recover.
        try:
            os.rmdir(scratch)
        except OSError:
            if os.path.isdir(scratch) and os.listdir(scratch):
                failures.append(
                    f"batch rename: recoverable files left in {scratch}")
    return failures


class MultiRenamePlugin(MenuProvider):
    name = "multi_rename"
    description = "Batch rename files in the current directory"
    version = "1.0"
    category = "File"

    def get_menu_items(self, path):
        if not path:
            return []
        return [("Batch Rename...", self._open_dialog)]

    def _open_dialog(self, path: str) -> None:
        from PyQt6.QtWidgets import (
            QCheckBox,
            QDialog,
            QDialogButtonBox,
            QFormLayout,
            QLabel,
            QLineEdit,
            QListWidget,
            QMessageBox,
            QVBoxLayout,
        )

        directory = path if os.path.isdir(path) else os.path.dirname(path) or "."
        try:
            entries = sorted(
                e for e in os.listdir(directory)
                if os.path.isfile(os.path.join(directory, e))
            )
        except OSError as e:
            QMessageBox.warning(None, "Batch Rename", f"List failed: {e}")
            return

        if not entries:
            QMessageBox.information(None, "Batch Rename", "No files to rename.")
            return

        dlg = QDialog()
        dlg.setWindowTitle("Batch Rename")
        dlg.resize(600, 500)
        form = QFormLayout()
        template_edit = QLineEdit("[N][E]", dlg)
        search_edit = QLineEdit("", dlg)
        replace_edit = QLineEdit("", dlg)
        regex_check = QCheckBox("Regex", dlg)
        form.addRow("Template:", template_edit)
        form.addRow("Search:", search_edit)
        form.addRow("Replace:", replace_edit)
        form.addRow("", regex_check)

        preview = QListWidget(dlg)
        for old, new in plan_renames(
            directory, entries, template_edit.text(),
            search=search_edit.text(), replace=replace_edit.text(),
            regex=regex_check.isChecked(),
        ):
            preview.addItem(f"{os.path.basename(old)} → {os.path.basename(new)}")

        def refresh() -> None:
            preview.clear()
            for old, new in plan_renames(
                directory, entries, template_edit.text(),
                search=search_edit.text(), replace=replace_edit.text(),
                regex=regex_check.isChecked(),
            ):
                preview.addItem(f"{os.path.basename(old)} → {os.path.basename(new)}")

        template_edit.textChanged.connect(refresh)
        search_edit.textChanged.connect(refresh)
        replace_edit.textChanged.connect(refresh)
        regex_check.stateChanged.connect(refresh)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=dlg,
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)

        layout = QVBoxLayout(dlg)
        layout.addLayout(form)
        layout.addWidget(QLabel("Preview:", dlg))
        layout.addWidget(preview, 1)
        layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        plan = plan_renames(
            directory, entries, template_edit.text(),
            search=search_edit.text(), replace=replace_edit.text(),
            regex=regex_check.isChecked(),
        )

        # Guard against silent data loss. Two failure modes:
        #  * a new name collides with a file NOT being renamed away (an
        #    out-of-set bystander) — refuse unless the user confirms;
        #  * a new name collides with another file that *is* in the plan
        #    (e.g. a->b, b->c, or a swap) — a naive in-order os.rename would
        #    destroy the original on POSIX, so stage through temp names.
        sources = {old for old, _ in plan}
        bystanders = sorted(
            os.path.basename(new) for old, new in plan
            if os.path.lexists(new) and new not in sources
        )
        if bystanders:
            reply = QMessageBox.question(
                None, "Batch Rename",
                "These existing files would be overwritten:\n"
                + "\n".join(bystanders)
                + "\n\nOverwrite them?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        failures = _apply_rename_plan(plan)
        if failures:
            QMessageBox.warning(
                None, "Batch Rename",
                "Some renames failed:\n" + "\n".join(failures),
            )
