"""Stable per-uid color chips for surfaces that render silo identity.

Used by the TUI (queue table + detail pane) and intended for the Qt
admin app, audit viewer, and cache-revoke UI as they grow color-chip
affordances too. Keeping the mapping in one place means a given uid
renders the same color across every surface.

Resolution order for a uid:
1. /etc/qdistro/silo-colors.toml override (`{ "2000": "green" }`).
2. A deterministic hash → palette index fallback.

The palette is a Rich-compatible set of terminal-safe color names —
16-color ANSI only, so the chip survives low-color ssh / tmux and
monochrome screenshots (the foreground text is still readable against
every palette entry).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# 10 terminal-safe colors. Excludes black / bright_black (invisible on
# dark themes), white / bright_white (invisible on light themes), and
# plain red (reserved by the TUI for error states — see broker-offline
# banner, Deny chrome).
_PALETTE: tuple[str, ...] = (
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "bright_magenta",
    "bright_cyan",
)

DEFAULT_OVERRIDE_PATH = Path("/etc/qdistro/silo-colors.toml")

_cached_overrides: dict[int, str] | None = None
_cached_from_path: Path | None = None


def _load_overrides(path: Path) -> dict[int, str]:
    """Parse TOML into a {uid: color_name} map. Invalid entries are skipped
    loudly (print) rather than raising — a bad override should never take
    the TUI down."""
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (FileNotFoundError, PermissionError):
        return {}
    except Exception as e:  # noqa: BLE001
        print(f"[silo_colors] ignoring {path}: {e}", flush=True)
        return {}
    out: dict[int, str] = {}
    for k, v in data.items():
        try:
            uid = int(k)
        except (TypeError, ValueError):
            print(f"[silo_colors] {path}: non-integer uid key {k!r}", flush=True)
            continue
        if not isinstance(v, str):
            print(f"[silo_colors] {path}: uid {uid} value is not a string: {v!r}",
                  flush=True)
            continue
        out[uid] = v
    return out


def chip_for_uid(uid: int, *, override_path: Path | None = None) -> str:
    """Return a stable Rich color name for this uid.

    override_path defaults to /etc/qdistro/silo-colors.toml. Missing /
    unreadable is fine — we fall back to the hash.
    """
    global _cached_overrides, _cached_from_path
    path = override_path if override_path is not None else DEFAULT_OVERRIDE_PATH
    if _cached_overrides is None or _cached_from_path != path:
        _cached_overrides = _load_overrides(path)
        _cached_from_path = path
    override = _cached_overrides.get(int(uid))
    if override is not None:
        return override
    # SHA-1 is overkill for this, but it makes the mapping trivially
    # reproducible by anyone reading the source; modular-hash on a
    # `hash(uid)` would depend on Python's salted string hash and differ
    # across processes. First byte is plenty of entropy for a
    # 10-color palette.
    digest = hashlib.sha1(str(int(uid)).encode()).digest()
    return _PALETTE[digest[0] % len(_PALETTE)]


def reset_cache() -> None:
    """Drop the memoized override map — useful in tests that tweak files."""
    global _cached_overrides, _cached_from_path
    _cached_overrides = None
    _cached_from_path = None
