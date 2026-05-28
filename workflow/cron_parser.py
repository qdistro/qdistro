"""Minimal 5-field cron expression parser.

Supports the standard ``minute hour day-of-month month day-of-week``
syntax with ``*``, lists (``1,2,3``), ranges (``1-5``), and steps
(``*/5``, ``1-30/2``). Day-of-week accepts 0 or 7 for Sunday.

No external dependency (no croniter): a small matcher plus a
forward-search ``next_after`` so the cron trigger can compute its next
fire time deterministically. Intentionally stdlib-only to match the
rest of the broker-side code.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# (min, max) inclusive bounds for each of the five fields.
_FIELD_BOUNDS = (
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 6),    # day of week (0 = Sunday)
)

# Forward search cap: 4 years of minutes. Guards against an expression
# that can never match (e.g. Feb 30) spinning forever.
_MAX_SEARCH_MINUTES = 4 * 366 * 24 * 60


class CronParseError(ValueError):
    """Raised when a cron expression cannot be parsed."""


def _parse_field(spec: str, lo: int, hi: int, *, is_dow: bool = False) -> frozenset[int]:
    """Parse one cron field into the explicit set of matching values."""
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronParseError(f"empty term in field {spec!r}")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                raise CronParseError(f"bad step {step_s!r} in {part!r}")
            if step <= 0:
                raise CronParseError(f"step must be positive in {part!r}")
        else:
            base = part

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a_s, _, b_s = base.partition("-")
            try:
                start, end = int(a_s), int(b_s)
            except ValueError:
                raise CronParseError(f"bad range {base!r}")
        else:
            try:
                start = end = int(base)
            except ValueError:
                raise CronParseError(f"bad value {base!r}")

        if is_dow:
            # Normalise 7 -> 0 (both mean Sunday).
            start = 0 if start == 7 else start
            end = 0 if end == 7 else end
        if start > end:
            raise CronParseError(f"range start > end in {base!r}")
        if start < lo or end > hi:
            raise CronParseError(
                f"value out of bounds [{lo},{hi}] in {part!r}"
            )
        values.update(range(start, end + 1, step))

    return frozenset(values)


class CronExpr:
    """A parsed 5-field cron expression."""

    __slots__ = ("minutes", "hours", "doms", "months", "dows",
                 "_dom_restricted", "_dow_restricted")

    def __init__(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise CronParseError(
                f"cron expression must have 5 fields, got {len(fields)}: "
                f"{expr!r}"
            )
        (self.minutes, self.hours, self.doms,
         self.months, self.dows) = (
            _parse_field(fields[i], *_FIELD_BOUNDS[i],
                         is_dow=(i == 4))
            for i in range(5)
        )
        # Standard cron: if BOTH dom and dow are restricted (not "*"),
        # a match on EITHER is sufficient. Track which were wildcards.
        self._dom_restricted = fields[2].strip() != "*"
        self._dow_restricted = fields[4].strip() != "*"

    def matches(self, dt: datetime) -> bool:
        if dt.minute not in self.minutes:
            return False
        if dt.hour not in self.hours:
            return False
        if dt.month not in self.months:
            return False
        # cron day-of-week: Sunday = 0; datetime.weekday(): Monday = 0.
        cron_dow = (dt.weekday() + 1) % 7
        dom_ok = dt.day in self.doms
        dow_ok = cron_dow in self.dows
        if self._dom_restricted and self._dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def next_after(self, after: datetime) -> datetime:
        """Return the next datetime strictly after ``after`` that matches.

        Searches minute-by-minute. Raises CronParseError if no match is
        found within the search cap (an unsatisfiable expression).
        """
        # Advance to the start of the next minute.
        candidate = (after + timedelta(minutes=1)).replace(
            second=0, microsecond=0)
        for _ in range(_MAX_SEARCH_MINUTES):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise CronParseError(
            "cron expression has no match within search window")
