"""Declarative pre-approval rules for the qdistro admin broker.

Admin drops YAML files in `/etc/qdistro/rules.d/*.yaml`; the broker
consults the loaded rule set BEFORE the approval cache or a prompt.
A matched `allow` rule grants silently (and optionally writes a
cache row so audit histories survive); a matched `deny` rule refuses
silently too.

Phase 2 v1 scope — deliberately minimal:

- **Selectors**: `uid` (int), `action` (str), `exe` (str), `app_id`
  (str — secctx in-band identity, see qdwin §6.10 / qdwin_shell_v1@v13),
  `sandbox_engine` (str — same source), `mime_type` (str — clipboard
  receive only, see qdwin_shell_v1@v15). All string selectors
  (`action`, `exe`, `app_id`, `sandbox_engine`, `mime_type`) accept
  fnmatch-style globs auto-detected by a literal `*` in the rule
  value: `text/*`, `image/*`, `*/json`, `qdistro.clipboard.*:user1:*`,
  `/usr/bin/python3*`, `org.example.*`, etc. The dispatch is
  unambiguous because none of these selectors carry `*` in their
  well-formed payload (mime types per IANA, action/app_id are dotted
  identifiers, exe paths are admin-authored). `uid` stays
  integer-eq. All selectors optional; a rule with no selectors
  matches everything (useful for a blanket default at the end of
  the ordered list).
  When a rule names `app_id` (or `sandbox_engine`, `mime_type`) but
  the broker caller didn't pass one, the rule does NOT match — i.e.
  selector presence implies "the request must carry this attribute
  and it must equal X." This keeps unsandboxed callers from
  accidentally matching app_id rules authored for tier-3 silos, and
  keeps mime-typed rules from bleeding into transfer / handoff calls
  that don't carry a single mime.
- **Precedence**: first-match wins. Files are loaded in sorted order
  across the directory; within a file, list order. This is
  predictable and debuggable — most-specific-wins requires a
  cost function that depends on the spec clarifying.
- **Operators**: `eq` for `uid` (integer); `eq` OR `fnmatch` for
  every string selector (`action`, `exe`, `app_id`,
  `sandbox_engine`, `mime_type`, `argv_basename`); `eq` for the
  argv list selectors (`argv_exact`, `argv_prefix`). Selection is
  automatic for the string selectors: a literal `*` in the rule
  value triggers fnmatch, otherwise exact-eq.
- **argv selectors (qsu / spec/21)**: `argv_exact` (full argv
  tuple, exact list-equality), `argv_basename` (basename of
  argv[0], glob-aware), and `argv_prefix` (list-equality on the
  first N argv elements). At most one of these may be set per
  rule; a rule with multiple is rejected at load time. The broker
  populates `argv` from the request's details dict — qsu sends
  `argv[NN]` per-element keys (lossless; `details.argv` is the
  shlex-joined human-readable form, lossy and not used for
  matching). When a rule names any argv selector but the caller
  doesn't carry argv (e.g. CheckClipboardTransfer), the rule does
  NOT match — same selector-presence semantics as `app_id`.
  The broader explicit-operator expansion (`prefix:`, `regex:`,
  `not-eq:`) for the non-argv selectors stays parked behind
  .
- **Hot reload**: shipped in task 059. The broker installs a
  Gio.FileMonitor (inotify) on the rules directory and reloads
  on file create/change/move/delete (debounced 200 ms to coalesce
  vim's tmp+rename atomic-save). `kill -HUP <broker-pid>` is also
  honoured. Programmatic D-Bus `ReloadRules` remains available.
  Each path emits `RulesReloaded(count)` so qdshell drops cached
  decisions.

Spec refs: `doc/permissions.md`, `doc/sudo.md`
(rule examples). Related:  (open
design questions captured before this landed).
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml  # PyYAML
except ImportError:  # pragma: no cover - broker install enforces this
    yaml = None  # type: ignore[assignment]


RULES_DIR_DEFAULT = "/etc/qdistro/rules.d"

# Cap per-file size and per-directory total bytes so a buggy admin
# script dropping a multi-gigabyte file can't wedge broker startup.
# The numbers are generous vs typical rule files (kilobytes at most)
# but hard ceilings rather than soft limits.
MAX_FILE_BYTES = 1 * 1024 * 1024        # 1 MiB per file
MAX_TOTAL_BYTES = 8 * 1024 * 1024       # 8 MiB across all files

# Scope vocabulary the broker understands. Kept in sync with
# qdistro_admin_cache.scope_to_row and the broker's DecideRequest
# — a rule that writes a cache row can only use these. task(072)
# extended the set with argv-aware scopes (cache backend support
# landed in task(069)).
_VALID_SCOPES = frozenset((
    "once", "1h", "24h",
    "forever", "forever_exe",
    "forever_argv", "forever_basename", "forever_prefix",
))
_VALID_DECISIONS = frozenset(("allow", "deny"))


@dataclass(frozen=True)
class Rule:
    name: str
    decision: str          # 'allow' or 'deny'
    source_path: str       # absolute path to the YAML file
    # Selectors — None means "don't care". Match is exact equality.
    uid: int | None = None
    action: str | None = None
    exe: str | None = None
    # qdwin §6.10 / v13 secctx selectors. None → don't care; "" means
    # "must equal empty" (i.e. the caller passed an explicit empty
    # string, typical for unsandboxed callers). Distinct from None on
    # purpose: an admin authoring `app_id: ""` is rare but valid.
    app_id: str | None = None
    sandbox_engine: str | None = None
    # qdwin_shell_v1@v15 per-MIME selector for CheckClipboardReceive.
    # None → don't care; "" → only matches receive calls with empty
    # mime (rare). Same selector-presence semantics as app_id: if a
    # rule names `mime_type` but the caller doesn't carry one (e.g.
    # CheckClipboardTransfer or CheckHandoffActivation, which omit
    # mime_type), the rule does NOT match — keeping mime-typed rules
    # scoped to receive.
    mime_type: str | None = None
    # qsu / spec/21 argv selectors. At most ONE may be set per rule
    # (validated at load time). All three default to None → "no argv
    # constraint." When set, a request without argv (e.g. clipboard
    # CheckClipboardTransfer that omits argv from details) does NOT
    # match — same selector-presence semantics as app_id.
    #   argv_exact:    tuple(["/usr/bin/apt-get", "update"]) — equal-only.
    #   argv_basename: "python3" or "python*" — fnmatch on basename(argv[0]).
    #   argv_prefix:   tuple(["/usr/bin/systemctl", "restart"]) —
    #                  equal-only on argv[:len(prefix)].
    argv_exact: tuple[str, ...] | None = None
    argv_basename: str | None = None
    argv_prefix: tuple[str, ...] | None = None
    # Optional cache scope for matched allows; None → don't cache
    # (Phase-1 'once' semantics). Deny rules ignore this field.
    scope: str | None = None
    rationale: str = ""

    @staticmethod
    def _str_selector_match(rule_value: str, request_value: str) -> bool:
        """Apply the auto-glob convention: a `*` in the rule value
        switches that selector from exact-eq to fnmatch. Centralised so
        all string selectors share one rule. Used by `action`, `exe`,
        `app_id`, `sandbox_engine`, `mime_type`; `uid` stays integer-eq.

        Globs are case-sensitive (fnmatchcase). The dispatch is
        unambiguous for the action/app_id/exe/sandbox_engine selectors
        because `*` is not a valid character in any of their well-formed
        values:
          - `action` is a dotted lowercase identifier
            (`qdistro.clipboard.transfer:user1:admin`).
          - `exe` is an absolute filesystem path
            (paths can technically contain `*` on Linux but admins do
            not author them; treating `*` as a glob is the correct
            ergonomic call here, and a literal-`*` exe is a bug-flavor
            edge case we won't optimize for).
          - `app_id` is a wp_security_context_v1 tag, reverse-DNS
            convention.
          - `sandbox_engine` is a short identifier ("podman",
            "waypipe", "qdistro-secctx").
          - `mime_type` per IANA never contains `*`.
        """
        if "*" in rule_value:
            return fnmatch.fnmatchcase(request_value, rule_value)
        return rule_value == request_value

    def matches(self, uid: int, action: str, exe: str, *,
                app_id: str = "", sandbox_engine: str = "",
                mime_type: str = "",
                argv: list[str] | tuple[str, ...] | None = None) -> bool:
        if self.uid is not None and self.uid != int(uid):
            return False
        if self.action is not None and \
           not self._str_selector_match(self.action, str(action)):
            return False
        if self.exe is not None and \
           not self._str_selector_match(self.exe, str(exe)):
            return False
        if self.app_id is not None and \
           not self._str_selector_match(self.app_id, str(app_id)):
            return False
        if self.sandbox_engine is not None and \
           not self._str_selector_match(self.sandbox_engine,
                                        str(sandbox_engine)):
            return False
        if self.mime_type is not None and \
           not self._str_selector_match(self.mime_type, str(mime_type)):
            return False
        if self.argv_exact is not None:
            if argv is None or tuple(argv) != self.argv_exact:
                return False
        if self.argv_basename is not None:
            if argv is None or len(argv) == 0:
                return False
            actual = os.path.basename(str(argv[0]))
            if not self._str_selector_match(self.argv_basename, actual):
                return False
        if self.argv_prefix is not None:
            if argv is None or len(argv) < len(self.argv_prefix):
                return False
            if tuple(argv[: len(self.argv_prefix)]) != self.argv_prefix:
                return False
        return True


class RulesEngine:
    """Ordered rule list. Immutable per load; `reload()` rebuilds."""

    def __init__(self, rules_dir: str | None = None):
        self._dir = rules_dir if rules_dir is not None else RULES_DIR_DEFAULT
        self._rules: list[Rule] = []
        self._errors: list[str] = []
        self.reload()

    # -- API used by the broker --
    def match(self, *, uid: int, action: str, exe: str,
              app_id: str = "",
              sandbox_engine: str = "",
              mime_type: str = "",
              argv: list[str] | tuple[str, ...] | None = None,
              ) -> Rule | None:
        for r in self._rules:
            if r.matches(uid, action, exe,
                         app_id=app_id, sandbox_engine=sandbox_engine,
                         mime_type=mime_type, argv=argv):
                return r
        return None

    def reload(self) -> None:
        """Re-walk the rules directory. Silent on missing directory
        (cold boot, admin hasn't authored anything yet); loud per-file
        on parse errors — bad rules are skipped rather than taking the
        broker down, and the errors surface via `load_errors()`."""
        self._rules = []
        self._errors = []
        if yaml is None:
            self._errors.append("PyYAML not installed; no rules loaded")
            return
        if not os.path.isdir(self._dir):
            return
        total_bytes = 0
        for name in sorted(os.listdir(self._dir)):
            if not (name.endswith(".yaml") or name.endswith(".yml")):
                continue
            path = os.path.join(self._dir, name)
            try:
                size = os.path.getsize(path)
            except OSError as e:
                self._errors.append(f"{path}: stat failed: {e}")
                continue
            if size > MAX_FILE_BYTES:
                self._errors.append(
                    f"{path}: {size} bytes exceeds per-file cap "
                    f"{MAX_FILE_BYTES}; skipping")
                continue
            if total_bytes + size > MAX_TOTAL_BYTES:
                self._errors.append(
                    f"{path}: rules directory total exceeds "
                    f"{MAX_TOTAL_BYTES} bytes; skipping remaining files")
                break
            total_bytes += size
            try:
                with open(path, "r") as f:
                    data = yaml.safe_load(f)
            except Exception as e:  # noqa: BLE001
                self._errors.append(f"{path}: parse failed: {e}")
                continue
            if data is None:
                continue
            if not isinstance(data, list):
                self._errors.append(f"{path}: top-level must be a list, got {type(data).__name__}")
                continue
            for i, entry in enumerate(data):
                try:
                    self._rules.append(_rule_from_dict(entry, path))
                except ValueError as e:
                    self._errors.append(f"{path} [{i}]: {e}")

    def load_errors(self) -> list[str]:
        return list(self._errors)

    def rules(self) -> list[Rule]:
        """Exposed read-only so the admin app can render a "loaded
        rules" view for debugging."""
        return list(self._rules)

    def directory(self) -> str:
        """The directory this engine reads from. Used by the broker
        to install a hot-reload watch on the same path the engine
        will scan on the next reload()."""
        return self._dir


def _rule_from_dict(entry: Any, source_path: str) -> Rule:
    """Validate one YAML entry and construct a Rule. Raises ValueError
    on any structural problem — caller records the message and moves
    on to the next entry."""
    if not isinstance(entry, dict):
        raise ValueError(f"rule must be a mapping, got {type(entry).__name__}")
    decision = entry.get("decision")
    if decision not in _VALID_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(_VALID_DECISIONS)}, got {decision!r}")
    match = entry.get("match") or {}
    if not isinstance(match, dict):
        raise ValueError(f"match must be a mapping, got {type(match).__name__}")
    uid = match.get("uid")
    if uid is not None and not isinstance(uid, int):
        raise ValueError(f"match.uid must be int, got {type(uid).__name__}")
    action = match.get("action")
    if action is not None and not isinstance(action, str):
        raise ValueError(f"match.action must be str, got {type(action).__name__}")
    exe = match.get("exe")
    if exe is not None and not isinstance(exe, str):
        raise ValueError(f"match.exe must be str, got {type(exe).__name__}")
    app_id = match.get("app_id")
    if app_id is not None and not isinstance(app_id, str):
        raise ValueError(f"match.app_id must be str, got {type(app_id).__name__}")
    sandbox_engine = match.get("sandbox_engine")
    if sandbox_engine is not None and not isinstance(sandbox_engine, str):
        raise ValueError(f"match.sandbox_engine must be str, got "
                         f"{type(sandbox_engine).__name__}")
    mime_type = match.get("mime_type")
    if mime_type is not None and not isinstance(mime_type, str):
        raise ValueError(f"match.mime_type must be str, got "
                         f"{type(mime_type).__name__}")

    # qsu / spec/21 argv selectors. argv_exact + argv_prefix are
    # YAML lists of strings; argv_basename is a single string (with
    # optional fnmatch glob). At most ONE of the three may be set.
    argv_exact_raw = match.get("argv_exact")
    if argv_exact_raw is not None:
        if not isinstance(argv_exact_raw, list) or \
           not all(isinstance(x, str) for x in argv_exact_raw):
            raise ValueError("match.argv_exact must be a list of strings")
        if len(argv_exact_raw) == 0:
            raise ValueError("match.argv_exact must be non-empty")
    argv_exact = tuple(argv_exact_raw) if argv_exact_raw is not None else None

    argv_basename = match.get("argv_basename")
    if argv_basename is not None and not isinstance(argv_basename, str):
        raise ValueError(f"match.argv_basename must be str, got "
                         f"{type(argv_basename).__name__}")
    if argv_basename is not None and "/" in argv_basename:
        # Authoring trap: `/usr/bin/python3` looks plausible but
        # argv_basename is checked against basename(argv[0]) — the
        # leading path components are guaranteed-stripped before
        # comparison. Catch this early so admin sees a clear error.
        raise ValueError(
            "match.argv_basename must be a basename (no '/'); use "
            "match.exe or match.argv_exact for a path-aware check")

    argv_prefix_raw = match.get("argv_prefix")
    if argv_prefix_raw is not None:
        if not isinstance(argv_prefix_raw, list) or \
           not all(isinstance(x, str) for x in argv_prefix_raw):
            raise ValueError("match.argv_prefix must be a list of strings")
        if len(argv_prefix_raw) == 0:
            raise ValueError("match.argv_prefix must be non-empty")
    argv_prefix = tuple(argv_prefix_raw) if argv_prefix_raw is not None else None

    set_argv = sum(x is not None for x in (argv_exact, argv_basename, argv_prefix))
    if set_argv > 1:
        raise ValueError(
            "at most one of match.argv_exact, match.argv_basename, "
            "match.argv_prefix may be set per rule")

    scope = entry.get("scope")
    if scope is not None and scope not in _VALID_SCOPES:
        raise ValueError(f"scope must be one of {sorted(_VALID_SCOPES)}, got {scope!r}")
    unknown_top = set(entry.keys()) - {"name", "decision", "match", "scope", "rationale"}
    if unknown_top:
        raise ValueError(f"unknown top-level keys: {sorted(unknown_top)}")
    unknown_match = set(match.keys()) - {"uid", "action", "exe",
                                         "app_id", "sandbox_engine",
                                         "mime_type",
                                         "argv_exact", "argv_basename",
                                         "argv_prefix"}
    if unknown_match:
        raise ValueError(f"unknown match keys: {sorted(unknown_match)}")
    return Rule(
        name=str(entry.get("name") or "(unnamed)"),
        decision=decision,
        source_path=source_path,
        uid=uid,
        action=action,
        exe=exe,
        app_id=app_id,
        sandbox_engine=sandbox_engine,
        mime_type=mime_type,
        argv_exact=argv_exact,
        argv_basename=argv_basename,
        argv_prefix=argv_prefix,
        scope=scope,
        rationale=str(entry.get("rationale") or ""),
    )
