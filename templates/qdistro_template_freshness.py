"""qdistro-template-freshness — opportunistic rebuild-from-scratch of
derived templates (todo/fableplan task 08).

Freshness proves the recipe still builds from scratch; it never promotes.
It is laptop-aware: a missed window is not a failure — staleness is
surfaced instead. Conditions are checked at trigger (AC power is gated by
the unit's ConditionACPower=; the rest are checked in-service): idle,
trusted non-metered network, normal thermals, free space, an advisory
night window.

For each derived template whose last successful freshness build is older
than ``desired_max_age`` (default 7d), this runs qdistro-template-build
--no-cache then qdistro-template-validate, and records the outcome in a
per-template status file. It does NOT touch bindings.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

import qdistro_templates as qt
import qdistro_template_build as build
import qdistro_template_validate as validate

DESIRED_MAX_AGE_DEFAULT = 7 * 86400
WARN_AGE = 7 * 86400
NEEDS_ATTENTION_AGE = 30 * 86400
MIN_FREE_BYTES = 20 * 1024 ** 3
IDLE_MIN_SECONDS = 20 * 60
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 6


def log(msg: str) -> None:
    print(f"[template-freshness] {msg}", file=sys.stderr, flush=True)


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# --------------------------------------------------------------------------
# condition checks — each returns (ok: bool, detail: str)
# --------------------------------------------------------------------------

def check_ac_power() -> tuple[bool, str]:
    # A desktop with no battery/AC supply counts as on-AC. The unit also
    # gates with ConditionACPower=; this is the in-service backstop.
    onlines = glob.glob("/sys/class/power_supply/A*/online")
    if not onlines:
        return True, "no AC supply node (desktop) — treated as on AC"
    for node in onlines:
        try:
            with open(node, encoding="utf-8") as fh:
                if fh.read().strip() == "1":
                    return True, "on AC"
        except OSError:
            continue
    return False, "on battery"


def check_idle() -> tuple[bool, str]:
    # Block while a graphical session is actively used (logind IdleHint=no);
    # permit when every graphical session is idle, or when no idle source is
    # available (the AC + night-window conditions already make this
    # opportunistic). logind's IdleHint already reflects the session idle
    # timeout, which is the practical proxy for the spec's "idle >= 20 min".
    try:
        listing = subprocess.run(["loginctl", "list-sessions", "--no-legend"],
                                 capture_output=True, text=True, timeout=5)
        if listing.returncode != 0:
            return True, "logind unavailable — permitted"
        for line in listing.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            sid = parts[0]
            props = subprocess.run(
                ["loginctl", "show-session", sid, "-p", "Type", "-p", "IdleHint"],
                capture_output=True, text=True, timeout=5)
            kv = dict(l.split("=", 1) for l in props.stdout.splitlines() if "=" in l)
            if kv.get("Type") in ("x11", "wayland", "mir") and kv.get("IdleHint") == "no":
                return False, f"graphical session {sid} active (not idle)"
        return True, "no active graphical session"
    except Exception:  # noqa: BLE001
        return True, "idle source unavailable — permitted"


def check_network() -> tuple[bool, str]:
    # Require full connectivity AND non-metered; permit only when
    # NetworkManager itself is unavailable.
    try:
        conn = subprocess.run(["nmcli", "-t", "-f", "CONNECTIVITY", "general"],
                              capture_output=True, text=True, timeout=5)
        if conn.returncode != 0:
            return True, "NetworkManager unavailable — permitted"
        connectivity = conn.stdout.strip()
        if connectivity != "full":
            return False, f"connectivity is {connectivity!r} (need full)"
        # The metered field on the `general` object is METERED (not
        # GENERAL.METERED, which some nmcli builds reject). Values look like
        # "no (4)" / "yes (1)" / "unknown (0)".
        metered = subprocess.run(["nmcli", "-t", "-f", "METERED", "general"],
                                 capture_output=True, text=True, timeout=5)
        if metered.returncode != 0:
            return True, "connectivity full, metered state unknown — permitted"
        val = metered.stdout.strip().lower()
        if val.startswith("yes"):
            return False, "metered network"
        return True, f"connectivity full, metered={val or 'unknown'}"
    except Exception:  # noqa: BLE001
        return True, "NetworkManager unavailable — permitted"


def check_thermal() -> tuple[bool, str]:
    # Block only if a thermal zone is in a critical/hot trip state we can
    # read; otherwise permit.
    for zone in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(zone, encoding="utf-8") as fh:
                milli = int(fh.read().strip())
            if milli >= 95000:  # 95C — clearly too hot to start a build
                return False, f"thermal zone at {milli // 1000}C"
        except (OSError, ValueError):
            continue
    return True, "thermals normal"


def check_free_space(layout: qt.Layout) -> tuple[bool, str]:
    path = layout.templates_var if os.path.isdir(layout.templates_var) else layout.var
    try:
        free = shutil.disk_usage(path).free
    except OSError as exc:
        return False, f"cannot stat {path}: {exc}"
    if free < MIN_FREE_BYTES:
        return False, f"only {free // 1024 ** 3} GiB free (< 20 GiB)"
    return True, f"{free // 1024 ** 3} GiB free"


def in_night_window(now: float) -> bool:
    hour = time.localtime(now).tm_hour
    if NIGHT_START_HOUR <= NIGHT_END_HOUR:
        return NIGHT_START_HOUR <= hour < NIGHT_END_HOUR
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR


def evaluate_conditions(layout: qt.Layout, now: float,
                        force: bool = False) -> tuple[bool, list[dict]]:
    """Run all condition checks. Returns (all_ok, results)."""
    checks = [
        ("ac_power", check_ac_power()),
        ("idle", check_idle()),
        ("network", check_network()),
        ("thermal", check_thermal()),
        ("free_space", check_free_space(layout)),
        ("night_window", (in_night_window(now),
                          "advisory night window" if in_night_window(now)
                          else "outside advisory night window")),
    ]
    results = [{"name": n, "ok": ok, "detail": d} for n, (ok, d) in checks]
    all_ok = all(r["ok"] for r in results)
    if force:
        return True, results
    return all_ok, results


# --------------------------------------------------------------------------
# staleness
# --------------------------------------------------------------------------

def status_path(layout: qt.Layout, template: str) -> str:
    return os.path.join(layout.template_dir(template), "freshness.toml")


def read_status(layout: qt.Layout, template: str) -> dict:
    path = status_path(layout, template)
    if os.path.isfile(path):
        return qt.read_toml(path)
    return {}


def last_success_age(status: dict, now: float) -> float | None:
    """Seconds since the last successful freshness build, or None if never."""
    last = status.get("last_success_epoch")
    if last is None:
        return None
    return now - float(last)


def staleness_label(age: float | None) -> str:
    if age is None:
        return "never"
    if age >= NEEDS_ATTENTION_AGE:
        return "needs-attention"
    if age >= WARN_AGE:
        return "warn"
    return "ok"


def is_stale(status: dict, now: float, desired_max_age: int) -> bool:
    age = last_success_age(status, now)
    return age is None or age >= desired_max_age


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def _derived_templates(layout: qt.Layout) -> list[str]:
    out = []
    if not os.path.isdir(layout.templates_etc):
        return out
    for name in sorted(os.listdir(layout.templates_etc)):
        if not name.endswith(".toml"):
            continue
        template = name[:-5]
        try:
            policy = qt.validate_template_policy(
                qt.read_toml(layout.template_policy(template)))
        except qt.TemplateError:
            continue
        if policy["template"]["class"] == "derived":
            out.append(template)
    return out


def _write_status(layout: qt.Layout, template: str, status: dict) -> None:
    qt.write_toml_atomic(status_path(layout, template), status, 0o644)


def run_freshness(layout: qt.Layout | None = None, *,
                  desired_max_age: int = DESIRED_MAX_AGE_DEFAULT,
                  now: float | None = None, force: bool = False,
                  builder=None, validator=None) -> dict:
    """Opportunistic freshness pass. Returns a summary dict. Never promotes,
    never touches bindings."""
    layout = layout or qt.Layout()
    now = time.time() if now is None else now
    # The builder returns (rc, run_id) so we validate exactly the candidate
    # this build produced — never whatever dir has the newest mtime.
    builder = builder or (lambda t: build.build_candidate(t, layout=layout,
                                                          no_cache=True))
    validator = validator or (lambda run_id: validate.validate(run_id, layout=layout))

    ok, results = evaluate_conditions(layout, now, force=force)
    summary = {"conditions_ok": ok, "conditions": results, "templates": []}
    if not ok:
        failed = [r["name"] for r in results if not r["ok"]]
        log(f"conditions not met ({', '.join(failed)}); skipping (NOT a failure)")
        summary["skipped"] = True
        return summary

    for template in _derived_templates(layout):
        status = read_status(layout, template)
        if not is_stale(status, now, desired_max_age):
            age = last_success_age(status, now)
            log(f"{template}: fresh ({int((age or 0))//86400}d old) — skip")
            summary["templates"].append({"template": template, "action": "skipped-fresh"})
            continue
        log(f"{template}: stale — rebuilding from scratch")
        rc, run_id = builder(template)
        if rc != 0 or run_id is None:
            # Early-warning signal — surface as degraded, keep prior status
            # (and the prior last_success_epoch, so staleness still reflects
            # the last GOOD build).
            status.update({"last_attempt_epoch": now, "last_attempt": _iso(now),
                           "last_result": "degraded",
                           "degraded_reason": "freshness build failed"})
            status["staleness"] = staleness_label(last_success_age(status, now))
            _write_status(layout, template, status)
            log(f"{template}: freshness build FAILED — degraded")
            summary["templates"].append({"template": template, "action": "build-failed"})
            continue
        vrc = validator(run_id)
        if vrc == 0:
            status.update({"last_attempt_epoch": now, "last_attempt": _iso(now),
                           "last_success_epoch": now, "last_success": _iso(now),
                           "last_result": "validated", "last_run_id": run_id})
            log(f"{template}: freshness candidate {run_id} validated (not promoted)")
            summary["templates"].append({"template": template, "action": "validated",
                                         "run_id": run_id})
        else:
            status.update({"last_attempt_epoch": now, "last_attempt": _iso(now),
                           "last_result": "degraded",
                           "degraded_reason": "freshness validation failed",
                           "last_run_id": run_id})
            log(f"{template}: freshness candidate {run_id} failed validation — degraded")
            summary["templates"].append({"template": template, "action": "validate-failed",
                                         "run_id": run_id})
        status["staleness"] = staleness_label(last_success_age(status, now))
        _write_status(layout, template, status)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qdistro-template-freshness")
    parser.add_argument("--force", action="store_true",
                        help="admin diagnostic: bypass the opportunistic "
                             "conditions and run now")
    parser.add_argument("--desired-max-age-days", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        summary = run_freshness(desired_max_age=args.desired_max_age_days * 86400,
                                force=args.force)
    except qt.TemplateError as exc:
        log(f"FATAL: {exc}")
        return 2
    # A missed window is not a failure: always exit 0 unless something
    # genuinely errored.
    return 0


if __name__ == "__main__":
    sys.exit(main())
