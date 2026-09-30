"""Stdlib-only presentation snapshot model, parser, and producer normalizer.

Importing this module must not load Qt, dbus, or read HOME.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1
MAX_BYTES = 64 * 1024
MAX_DEPTH = 6
MAX_OBJECT_KEYS = 64
MAX_FAMILY_LEN = 128
MAX_ICON_THEME_LEN = 128
BASE_POINT_SIZE_DEFAULT = 11.0
BASE_POINT_SIZE_MIN = 6.0
BASE_POINT_SIZE_MAX = 48.0
FONT_SCALE_MIN = 0.75
FONT_SCALE_MAX = 1.25
UI_SCALE_MIN = 0.8
UI_SCALE_MAX = 1.2
RADIUS_RATIO_MIN = 0.0
RADIUS_RATIO_MAX = 2.0
MOTION_SPEED_MIN = 0.05
MOTION_SPEED_MAX = 2.0
LOCAL_SIZE_MIN = 6.0
LOCAL_SIZE_MAX = 48.0
CONTRAST_TEXT = 4.5
CONTRAST_SECONDARY = 3.0

COLOR_KEYS: tuple[str, ...] = (
    "mPrimary",
    "mOnPrimary",
    "mSecondary",
    "mOnSecondary",
    "mTertiary",
    "mOnTertiary",
    "mError",
    "mOnError",
    "mSurface",
    "mOnSurface",
    "mSurfaceVariant",
    "mOnSurfaceVariant",
    "mOutline",
    "mShadow",
    "mHover",
    "mOnHover",
)

_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")
_GENERATION_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# qdshell built-in dark target palette (Color.qml defaultColors).
DEFAULT_DARK_COLORS: dict[str, str] = {
    "mPrimary": "#fff59b",
    "mOnPrimary": "#0e0e43",
    "mSecondary": "#a9aefe",
    "mOnSecondary": "#0e0e43",
    "mTertiary": "#9bfece",
    "mOnTertiary": "#0e0e43",
    "mError": "#fd4663",
    "mOnError": "#0e0e43",
    "mSurface": "#070722",
    "mOnSurface": "#f3edf7",
    "mSurfaceVariant": "#11112d",
    "mOnSurfaceVariant": "#7c80b4",
    "mOutline": "#21215f",
    "mShadow": "#070722",
    "mHover": "#9bfece",
    "mOnHover": "#0e0e43",
}

CONTRAST_PAIRS_TEXT: tuple[tuple[str, str], ...] = (
    ("mSurface", "mOnSurface"),
    ("mPrimary", "mOnPrimary"),
    ("mSecondary", "mOnSecondary"),
    ("mTertiary", "mOnTertiary"),
    ("mError", "mOnError"),
    ("mHover", "mOnHover"),
)
CONTRAST_PAIRS_SECONDARY: tuple[tuple[str, str], ...] = (("mSurfaceVariant", "mOnSurfaceVariant"),)


class SnapshotError(ValueError):
    """Snapshot is missing, unreadable, or fails strict validation."""


class SnapshotPathError(SnapshotError):
    """Trusted-path walk failed (symlink, ownership, mode, type)."""


def _reject_constant(name: str) -> None:
    raise SnapshotError(f"non-finite JSON number: {name}")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    out: dict[str, Any] = {}
    if len(pairs) > MAX_OBJECT_KEYS:
        raise SnapshotError(f"too many object keys ({len(pairs)} > {MAX_OBJECT_KEYS})")
    for key, value in pairs:
        if key in seen:
            raise SnapshotError(f"duplicate JSON key: {key!r}")
        seen.add(key)
        out[key] = value
    return out


def loads_strict(text: str) -> Any:
    """Parse JSON, rejecting duplicates, NaN/Infinity, and oversize documents."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise SnapshotError("document exceeds 64 KiB")
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_pairs,
        )
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"invalid JSON: {exc}") from exc
    except RecursionError as exc:
        raise SnapshotError("JSON nesting exceeds decoder limits") from exc
    _check_depth(value, 0)
    return value


def _check_depth(value: Any, depth: int) -> None:
    if depth > MAX_DEPTH:
        raise SnapshotError(f"JSON nesting exceeds {MAX_DEPTH}")
    if isinstance(value, dict):
        if len(value) > MAX_OBJECT_KEYS:
            raise SnapshotError(f"too many object keys ({len(value)} > {MAX_OBJECT_KEYS})")
        for nested in value.values():
            _check_depth(nested, depth + 1)
    elif isinstance(value, list):
        if len(value) > MAX_OBJECT_KEYS:
            raise SnapshotError(f"too many array items ({len(value)} > {MAX_OBJECT_KEYS})")
        for nested in value:
            _check_depth(nested, depth + 1)


def parse_hex_color(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise SnapshotError(f"{field} must be a #rrggbb string")
    lowered = value.strip().lower()
    if not _COLOR_RE.fullmatch(lowered):
        raise SnapshotError(f"{field} must be opaque #rrggbb, got {value!r}")
    return lowered


def _linear_srgb(channel: int) -> float:
    c = channel / 255.0
    if c <= 0.04045:
        return c / 12.92
    return ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    hex_body = color[1:]
    r = int(hex_body[0:2], 16)
    g = int(hex_body[2:4], 16)
    b = int(hex_body[4:6], 16)
    return 0.2126 * _linear_srgb(r) + 0.7152 * _linear_srgb(g) + 0.0722 * _linear_srgb(b)


def contrast_ratio(a: str, b: str) -> float:
    la = relative_luminance(a)
    lb = relative_luminance(b)
    lighter = max(la, lb)
    darker = min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def blend_hex(foreground: str, background: str, fg_weight: float = 0.6) -> str:
    """Opaque blend used for disabled foreground (60% fg + 40% bg)."""
    bg_weight = 1.0 - fg_weight

    def mix(i: int) -> int:
        fv = int(foreground[1 + i : 3 + i], 16)
        bv = int(background[1 + i : 3 + i], 16)
        return int(round(fv * fg_weight + bv * bg_weight))

    return f"#{mix(0):02x}{mix(2):02x}{mix(4):02x}"


def _validate_palette(colors: Mapping[str, str]) -> None:
    missing = [key for key in COLOR_KEYS if key not in colors]
    if missing:
        raise SnapshotError(f"incomplete palette, missing {missing}")
    for a, b in CONTRAST_PAIRS_TEXT:
        ratio = contrast_ratio(colors[a], colors[b])
        if ratio < CONTRAST_TEXT:
            raise SnapshotError(
                f"palette contrast {a}/{b} is {ratio:.2f}:1, need {CONTRAST_TEXT}:1"
            )
    for a, b in CONTRAST_PAIRS_SECONDARY:
        ratio = contrast_ratio(colors[a], colors[b])
        if ratio < CONTRAST_SECONDARY:
            raise SnapshotError(
                f"palette contrast {a}/{b} is {ratio:.2f}:1, need {CONTRAST_SECONDARY}:1"
            )


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise SnapshotError(f"{field} must be a finite number")
    return number


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def _require_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise SnapshotError(f"{field} must be a boolean")
    return value


def _clean_family(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise SnapshotError(f"{field} must be a string")
    trimmed = value.strip()
    if not trimmed:
        raise SnapshotError(f"{field} must be nonempty")
    if len(trimmed) > MAX_FAMILY_LEN:
        raise SnapshotError(f"{field} exceeds {MAX_FAMILY_LEN} characters")
    if _CONTROL_RE.search(trimmed) or any(unicodedata.category(ch) == "Cc" for ch in trimmed):
        raise SnapshotError(f"{field} contains control characters")
    if "\x00" in value:
        raise SnapshotError(f"{field} contains NUL")
    return trimmed


def _clean_icon_theme(value: Any, *, field: str = "iconTheme") -> str:
    if not isinstance(value, str):
        raise SnapshotError(f"{field} must be a string")
    if value == "":
        return ""
    trimmed = value.strip()
    if not trimmed:
        raise SnapshotError(f"{field} must be a name or empty")
    if len(trimmed) > MAX_ICON_THEME_LEN:
        raise SnapshotError(f"{field} exceeds {MAX_ICON_THEME_LEN} characters")
    if "/" in trimmed or "\\" in trimmed:
        raise SnapshotError(f"{field} must be a name, not a path")
    if _CONTROL_RE.search(trimmed) or "\x00" in value:
        raise SnapshotError(f"{field} contains control characters")
    return trimmed


@dataclass(frozen=True)
class Colors:
    mPrimary: str
    mOnPrimary: str
    mSecondary: str
    mOnSecondary: str
    mTertiary: str
    mOnTertiary: str
    mError: str
    mOnError: str
    mSurface: str
    mOnSurface: str
    mSurfaceVariant: str
    mOnSurfaceVariant: str
    mOutline: str
    mShadow: str
    mHover: str
    mOnHover: str

    def as_dict(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in COLOR_KEYS}


@dataclass(frozen=True)
class Fonts:
    ui_family: str
    fixed_family: str
    base_point_size: float
    ui_scale: float
    fixed_scale: float


@dataclass(frozen=True)
class Metrics:
    ui_scale: float
    radius_ratio: float
    input_radius_ratio: float


@dataclass(frozen=True)
class Motion:
    disabled: bool
    speed: float


@dataclass(frozen=True)
class PresentationSnapshot:
    version: int
    enabled: bool
    generation: str
    mode: str
    colors: Colors
    fonts: Fonts
    metrics: Metrics
    motion: Motion
    tooltips_enabled: bool
    icon_theme: str

    def content_dict(self) -> dict[str, Any]:
        """Canonical content excluding generation, for hashing and comparison."""
        return {
            "version": self.version,
            "enabled": self.enabled,
            "mode": self.mode,
            "colors": self.colors.as_dict(),
            "fonts": {
                "uiFamily": self.fonts.ui_family,
                "fixedFamily": self.fonts.fixed_family,
                "basePointSize": self.fonts.base_point_size,
                "uiScale": self.fonts.ui_scale,
                "fixedScale": self.fonts.fixed_scale,
            },
            "metrics": {
                "uiScale": self.metrics.ui_scale,
                "radiusRatio": self.metrics.radius_ratio,
                "inputRadiusRatio": self.metrics.input_radius_ratio,
            },
            "motion": {
                "disabled": self.motion.disabled,
                "speed": self.motion.speed,
            },
            "tooltipsEnabled": self.tooltips_enabled,
            "iconTheme": self.icon_theme,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self.content_dict()
        payload["generation"] = self.generation
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def canonical_content_bytes(content: Mapping[str, Any]) -> bytes:
    return json.dumps(content, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def generation_for_content(content: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(canonical_content_bytes(content)).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def with_generation(snapshot: PresentationSnapshot) -> PresentationSnapshot:
    generation = generation_for_content(snapshot.content_dict())
    if snapshot.generation == generation:
        return snapshot
    return PresentationSnapshot(
        version=snapshot.version,
        enabled=snapshot.enabled,
        generation=generation,
        mode=snapshot.mode,
        colors=snapshot.colors,
        fonts=snapshot.fonts,
        metrics=snapshot.metrics,
        motion=snapshot.motion,
        tooltips_enabled=snapshot.tooltips_enabled,
        icon_theme=snapshot.icon_theme,
    )


def _parse_colors(raw: Any) -> Colors:
    if not isinstance(raw, dict):
        raise SnapshotError("colors must be an object")
    parsed: dict[str, str] = {}
    for key in COLOR_KEYS:
        if key not in raw:
            raise SnapshotError(f"colors.{key} is required")
        parsed[key] = parse_hex_color(raw[key], field=f"colors.{key}")
    _validate_palette(parsed)
    return Colors(**parsed)


def _parse_fonts(raw: Any) -> Fonts:
    if not isinstance(raw, dict):
        raise SnapshotError("fonts must be an object")
    for required in ("uiFamily", "fixedFamily", "basePointSize", "uiScale", "fixedScale"):
        if required not in raw:
            raise SnapshotError(f"fonts.{required} is required")
    base = _finite_number(raw["basePointSize"], field="fonts.basePointSize")
    if base < BASE_POINT_SIZE_MIN or base > BASE_POINT_SIZE_MAX:
        raise SnapshotError("fonts.basePointSize out of range")
    ui_scale = _finite_number(raw["uiScale"], field="fonts.uiScale")
    if ui_scale < FONT_SCALE_MIN or ui_scale > FONT_SCALE_MAX:
        raise SnapshotError("fonts.uiScale out of range")
    fixed_scale = _finite_number(raw["fixedScale"], field="fonts.fixedScale")
    if fixed_scale < FONT_SCALE_MIN or fixed_scale > FONT_SCALE_MAX:
        raise SnapshotError("fonts.fixedScale out of range")
    return Fonts(
        ui_family=_clean_family(raw["uiFamily"], field="fonts.uiFamily"),
        fixed_family=_clean_family(raw["fixedFamily"], field="fonts.fixedFamily"),
        base_point_size=base,
        ui_scale=ui_scale,
        fixed_scale=fixed_scale,
    )


def _parse_metrics(raw: Any) -> Metrics:
    if not isinstance(raw, dict):
        raise SnapshotError("metrics must be an object")
    for required in ("uiScale", "radiusRatio", "inputRadiusRatio"):
        if required not in raw:
            raise SnapshotError(f"metrics.{required} is required")
    ui_scale = _finite_number(raw["uiScale"], field="metrics.uiScale")
    if ui_scale < UI_SCALE_MIN or ui_scale > UI_SCALE_MAX:
        raise SnapshotError("metrics.uiScale out of range")
    radius = _finite_number(raw["radiusRatio"], field="metrics.radiusRatio")
    if radius < RADIUS_RATIO_MIN or radius > RADIUS_RATIO_MAX:
        raise SnapshotError("metrics.radiusRatio out of range")
    input_radius = _finite_number(raw["inputRadiusRatio"], field="metrics.inputRadiusRatio")
    if input_radius < RADIUS_RATIO_MIN or input_radius > RADIUS_RATIO_MAX:
        raise SnapshotError("metrics.inputRadiusRatio out of range")
    return Metrics(ui_scale=ui_scale, radius_ratio=radius, input_radius_ratio=input_radius)


def _parse_motion(raw: Any) -> Motion:
    if not isinstance(raw, dict):
        raise SnapshotError("motion must be an object")
    if "disabled" not in raw or "speed" not in raw:
        raise SnapshotError("motion.disabled and motion.speed are required")
    speed = _finite_number(raw["speed"], field="motion.speed")
    if speed < MOTION_SPEED_MIN or speed > MOTION_SPEED_MAX:
        raise SnapshotError("motion.speed out of range")
    return Motion(disabled=_require_bool(raw["disabled"], field="motion.disabled"), speed=speed)


def parse_snapshot(obj: Any) -> PresentationSnapshot:
    """Strict consumer parse. Unknown v1 fields are ignored; unknown version rejects."""
    if not isinstance(obj, dict):
        raise SnapshotError("snapshot must be a JSON object")
    if "version" not in obj:
        raise SnapshotError("version is required")
    if not isinstance(obj["version"], int) or isinstance(obj["version"], bool):
        raise SnapshotError("version must be an integer")
    if obj["version"] != SCHEMA_VERSION:
        raise SnapshotError(f"unsupported version {obj['version']}")
    required = (
        "enabled",
        "generation",
        "mode",
        "colors",
        "fonts",
        "metrics",
        "motion",
        "tooltipsEnabled",
        "iconTheme",
    )
    missing = [key for key in required if key not in obj]
    if missing:
        raise SnapshotError(f"missing required fields: {missing}")
    mode = obj["mode"]
    if mode not in ("dark", "light"):
        raise SnapshotError("mode must be 'dark' or 'light'")
    generation = obj["generation"]
    if not isinstance(generation, str) or not _GENERATION_RE.fullmatch(generation):
        raise SnapshotError("generation must be an opaque UUID")
    snapshot = PresentationSnapshot(
        version=SCHEMA_VERSION,
        enabled=_require_bool(obj["enabled"], field="enabled"),
        generation=generation,
        mode=mode,
        colors=_parse_colors(obj["colors"]),
        fonts=_parse_fonts(obj["fonts"]),
        metrics=_parse_metrics(obj["metrics"]),
        motion=_parse_motion(obj["motion"]),
        tooltips_enabled=_require_bool(obj["tooltipsEnabled"], field="tooltipsEnabled"),
        icon_theme=_clean_icon_theme(obj["iconTheme"]),
    )
    expected = generation_for_content(snapshot.content_dict())
    if snapshot.generation != expected:
        raise SnapshotError("generation does not match canonical content")
    return snapshot


def parse_snapshot_text(text: str) -> PresentationSnapshot:
    return parse_snapshot(loads_strict(text))


def parse_snapshot_bytes(data: bytes) -> PresentationSnapshot:
    if len(data) > MAX_BYTES:
        raise SnapshotError("document exceeds 64 KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotError("snapshot is not UTF-8") from exc
    return parse_snapshot_text(text)


def _producer_family(value: Any, *, default: str, field: str) -> str:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        return default
    try:
        return _clean_family(value, field=field)
    except SnapshotError:
        return default


def _producer_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    return default


def _producer_clamp(value: Any, *, default: float, lo: float, hi: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    return _clamp(number, lo, hi)


def _producer_icon(value: Any) -> str:
    if value is None:
        return ""
    try:
        return _clean_icon_theme(value)
    except SnapshotError:
        return ""


def _producer_colors(raw: Any) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise SnapshotError("producer palette must be a complete object")
    parsed: dict[str, str] = {}
    for key in COLOR_KEYS:
        if key not in raw:
            raise SnapshotError(f"producer palette missing {key}")
        parsed[key] = parse_hex_color(raw[key], field=f"colors.{key}")
    _validate_palette(parsed)
    return parsed


def normalize_producer(
    *,
    mode: Any,
    colors: Any,
    settings: Mapping[str, Any] | None = None,
    enabled: bool = True,
    default_ui_family: str = "Sans Serif",
    default_fixed_family: str = "monospace",
) -> PresentationSnapshot:
    """Publisher path: clamp finite scalars, default missing settings, require a full palette."""
    if mode not in ("dark", "light"):
        raise SnapshotError("mode must be 'dark' or 'light'")
    if not isinstance(enabled, bool):
        raise SnapshotError("enabled must be a boolean")
    settings = settings or {}
    ui = settings.get("ui") if isinstance(settings.get("ui"), Mapping) else {}
    general = settings.get("general") if isinstance(settings.get("general"), Mapping) else {}
    appearance = (
        settings.get("appearance") if isinstance(settings.get("appearance"), Mapping) else {}
    )
    fonts = Fonts(
        ui_family=_producer_family(
            ui.get("fontDefault"), default=default_ui_family, field="ui.fontDefault"
        ),
        fixed_family=_producer_family(
            ui.get("fontFixed"), default=default_fixed_family, field="ui.fontFixed"
        ),
        base_point_size=BASE_POINT_SIZE_DEFAULT,
        ui_scale=_producer_clamp(
            ui.get("fontDefaultScale"),
            default=1.0,
            lo=FONT_SCALE_MIN,
            hi=FONT_SCALE_MAX,
            field="ui.fontDefaultScale",
        ),
        fixed_scale=_producer_clamp(
            ui.get("fontFixedScale"),
            default=1.0,
            lo=FONT_SCALE_MIN,
            hi=FONT_SCALE_MAX,
            field="ui.fontFixedScale",
        ),
    )
    metrics = Metrics(
        ui_scale=_producer_clamp(
            general.get("scaleRatio"),
            default=1.0,
            lo=UI_SCALE_MIN,
            hi=UI_SCALE_MAX,
            field="general.scaleRatio",
        ),
        radius_ratio=_producer_clamp(
            general.get("radiusRatio"),
            default=1.0,
            lo=RADIUS_RATIO_MIN,
            hi=RADIUS_RATIO_MAX,
            field="general.radiusRatio",
        ),
        input_radius_ratio=_producer_clamp(
            general.get("iRadiusRatio"),
            default=1.0,
            lo=RADIUS_RATIO_MIN,
            hi=RADIUS_RATIO_MAX,
            field="general.iRadiusRatio",
        ),
    )
    motion = Motion(
        disabled=_producer_bool(general.get("animationDisabled"), default=False),
        speed=_producer_clamp(
            general.get("animationSpeed"),
            default=1.0,
            lo=MOTION_SPEED_MIN,
            hi=MOTION_SPEED_MAX,
            field="general.animationSpeed",
        ),
    )
    snapshot = PresentationSnapshot(
        version=SCHEMA_VERSION,
        enabled=enabled,
        generation="00000000-0000-0000-0000-000000000000",
        mode=mode,
        colors=Colors(**_producer_colors(colors)),
        fonts=fonts,
        metrics=metrics,
        motion=motion,
        tooltips_enabled=_producer_bool(ui.get("tooltipsEnabled"), default=True),
        icon_theme=_producer_icon(appearance.get("iconTheme")),
    )
    return with_generation(snapshot)


@dataclass(frozen=True)
class LocalOverrides:
    """Per-app appearance overrides. None means inherit; persist only set fields."""

    version: int = 1
    ui_font_family: str | None = None
    ui_font_size_pt: float | None = None
    fixed_ui_font_family: str | None = None
    fixed_ui_font_size_pt: float | None = None
    ui_scale: float | None = None
    radius_ratio: float | None = None
    input_radius_ratio: float | None = None
    motion_disabled: bool | None = None
    motion_speed: float | None = None
    tooltips_enabled: bool | None = None
    icon_theme: str | None = None

    def persistable(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"version": self.version}
        mapping = {
            "ui_font_family": self.ui_font_family,
            "ui_font_size_pt": self.ui_font_size_pt,
            "fixed_ui_font_family": self.fixed_ui_font_family,
            "fixed_ui_font_size_pt": self.fixed_ui_font_size_pt,
            "ui_scale": self.ui_scale,
            "radius_ratio": self.radius_ratio,
            "input_radius_ratio": self.input_radius_ratio,
            "motion_disabled": self.motion_disabled,
            "motion_speed": self.motion_speed,
            "tooltips_enabled": self.tooltips_enabled,
            "icon_theme": self.icon_theme,
        }
        for key, value in mapping.items():
            if value is not None:
                payload[key] = value
        return payload


def parse_local_overrides(raw: Mapping[str, Any] | None) -> LocalOverrides:
    if not raw:
        return LocalOverrides()
    version = raw.get("version", 1)
    if version != 1:
        raise SnapshotError(f"unsupported appearance version {version}")

    def opt_family(key: str) -> str | None:
        if key not in raw:
            return None
        return _clean_family(raw[key], field=key)

    def opt_size(key: str) -> float | None:
        if key not in raw:
            return None
        size = _finite_number(raw[key], field=key)
        if size < LOCAL_SIZE_MIN or size > LOCAL_SIZE_MAX:
            raise SnapshotError(f"{key} out of range")
        return size

    def opt_scale(key: str, lo: float, hi: float) -> float | None:
        if key not in raw:
            return None
        value = _finite_number(raw[key], field=key)
        if value < lo or value > hi:
            raise SnapshotError(f"{key} out of range")
        return value

    def opt_bool(key: str) -> bool | None:
        if key not in raw:
            return None
        return _require_bool(raw[key], field=key)

    icon: str | None
    if "icon_theme" not in raw:
        icon = None
    else:
        icon = _clean_icon_theme(raw["icon_theme"], field="icon_theme")
    return LocalOverrides(
        version=1,
        ui_font_family=opt_family("ui_font_family"),
        ui_font_size_pt=opt_size("ui_font_size_pt"),
        fixed_ui_font_family=opt_family("fixed_ui_font_family"),
        fixed_ui_font_size_pt=opt_size("fixed_ui_font_size_pt"),
        ui_scale=opt_scale("ui_scale", UI_SCALE_MIN, UI_SCALE_MAX),
        radius_ratio=opt_scale("radius_ratio", RADIUS_RATIO_MIN, RADIUS_RATIO_MAX),
        input_radius_ratio=opt_scale("input_radius_ratio", RADIUS_RATIO_MIN, RADIUS_RATIO_MAX),
        motion_disabled=opt_bool("motion_disabled"),
        motion_speed=opt_scale("motion_speed", MOTION_SPEED_MIN, MOTION_SPEED_MAX),
        tooltips_enabled=opt_bool("tooltips_enabled"),
        icon_theme=icon,
    )


THEME_MODES = ("system", "dark", "light", "native")


@dataclass(frozen=True)
class ResolvedPresentation:
    theme_mode: str
    using_shared_palette: bool
    snapshot: PresentationSnapshot | None
    ui_family: str
    ui_point_size: float
    fixed_family: str
    fixed_ui_point_size: float
    content_ui_point_size: float
    content_fixed_point_size: float
    ui_scale: float
    radius_ratio: float
    input_radius_ratio: float
    input_radius_px: int
    motion_disabled: bool
    motion_speed: float
    tooltips_enabled: bool
    icon_theme: str
    colors: Colors | None
    generation: str | None
    desktop_available: bool

    def field_map(self) -> dict[str, Any]:
        return {
            "theme_mode": self.theme_mode,
            "using_shared_palette": self.using_shared_palette,
            "ui_family": self.ui_family,
            "ui_point_size": self.ui_point_size,
            "fixed_family": self.fixed_family,
            "fixed_ui_point_size": self.fixed_ui_point_size,
            "content_ui_point_size": self.content_ui_point_size,
            "content_fixed_point_size": self.content_fixed_point_size,
            "ui_scale": self.ui_scale,
            "radius_ratio": self.radius_ratio,
            "input_radius_ratio": self.input_radius_ratio,
            "input_radius_px": self.input_radius_px,
            "motion_disabled": self.motion_disabled,
            "motion_speed": self.motion_speed,
            "tooltips_enabled": self.tooltips_enabled,
            "icon_theme": self.icon_theme,
            "colors": None if self.colors is None else self.colors.as_dict(),
            "generation": self.generation,
            "desktop_available": self.desktop_available,
        }


def changed_fields(old: ResolvedPresentation | None, new: ResolvedPresentation) -> tuple[str, ...]:
    if old is None:
        return tuple(new.field_map())
    old_map = old.field_map()
    new_map = new.field_map()
    return tuple(key for key in new_map if old_map.get(key) != new_map.get(key))


def animation_ms(base_ms: float, motion: Motion | ResolvedPresentation) -> int:
    disabled = motion.disabled if isinstance(motion, Motion) else motion.motion_disabled
    speed = motion.speed if isinstance(motion, Motion) else motion.motion_speed
    if disabled:
        return 0
    if speed <= 0:
        return 0
    return int(round(base_ms / speed))


def input_radius_px(ratio: float) -> int:
    return int(round(3 * ratio))


def card_radius_px(tier: int, radius_ratio: float) -> int:
    """Style radius tiers 3/4/8/12/16/20 times radiusRatio."""
    bases = {3: 3, 4: 4, 8: 8, 12: 12, 16: 16, 20: 20}
    base = bases.get(tier, tier)
    return int(round(base * radius_ratio))


def resolve_presentation(
    *,
    theme_mode: str,
    snapshot: PresentationSnapshot | None,
    local: LocalOverrides | None = None,
    native_ui_family: str,
    native_fixed_family: str,
    native_icon_theme: str,
    native_ui_point_size: float = BASE_POINT_SIZE_DEFAULT,
) -> ResolvedPresentation:
    if theme_mode not in THEME_MODES:
        raise SnapshotError(f"unknown theme_mode {theme_mode!r}")
    local = local or LocalOverrides()
    shared = snapshot if snapshot is not None and snapshot.enabled else None
    desktop_available = shared is not None
    base_size = shared.fonts.base_point_size if shared else native_ui_point_size
    font_ui_scale = shared.fonts.ui_scale if shared else 1.0
    font_fixed_scale = shared.fonts.fixed_scale if shared else 1.0
    metrics_ui_scale = shared.metrics.ui_scale if shared else 1.0
    radius_ratio = shared.metrics.radius_ratio if shared else 1.0
    input_radius_ratio = shared.metrics.input_radius_ratio if shared else 1.0
    motion_disabled = shared.motion.disabled if shared else False
    motion_speed = shared.motion.speed if shared else 1.0
    tooltips_enabled = shared.tooltips_enabled if shared else True
    ui_family = shared.fonts.ui_family if shared else native_ui_family
    fixed_family = shared.fonts.fixed_family if shared else native_fixed_family
    icon_theme = shared.icon_theme if shared else native_icon_theme
    if icon_theme == "":
        icon_theme = native_icon_theme

    if local.ui_font_family is not None:
        ui_family = local.ui_font_family
    if local.fixed_ui_font_family is not None:
        fixed_family = local.fixed_ui_font_family
    if local.ui_scale is not None:
        metrics_ui_scale = local.ui_scale
    if local.radius_ratio is not None:
        radius_ratio = local.radius_ratio
    if local.input_radius_ratio is not None:
        input_radius_ratio = local.input_radius_ratio
    if local.motion_disabled is not None:
        motion_disabled = local.motion_disabled
    if local.motion_speed is not None:
        motion_speed = local.motion_speed
    if local.tooltips_enabled is not None:
        tooltips_enabled = local.tooltips_enabled
    if local.icon_theme is not None:
        icon_theme = native_icon_theme if local.icon_theme == "" else local.icon_theme

    ui_point_size = base_size * font_ui_scale * metrics_ui_scale
    fixed_ui_point_size = base_size * font_fixed_scale * metrics_ui_scale
    content_ui_point_size = base_size * font_ui_scale
    content_fixed_point_size = base_size * font_fixed_scale
    if local.ui_font_size_pt is not None:
        ui_point_size = local.ui_font_size_pt * metrics_ui_scale
        content_ui_point_size = local.ui_font_size_pt
    if local.fixed_ui_font_size_pt is not None:
        fixed_ui_point_size = local.fixed_ui_font_size_pt * metrics_ui_scale
        content_fixed_point_size = local.fixed_ui_font_size_pt

    using_shared_palette = theme_mode == "system" and shared is not None
    colors = shared.colors if using_shared_palette else None
    generation = shared.generation if shared is not None else None
    return ResolvedPresentation(
        theme_mode=theme_mode,
        using_shared_palette=using_shared_palette,
        snapshot=snapshot if desktop_available else None,
        ui_family=ui_family,
        ui_point_size=ui_point_size,
        fixed_family=fixed_family,
        fixed_ui_point_size=fixed_ui_point_size,
        content_ui_point_size=content_ui_point_size,
        content_fixed_point_size=content_fixed_point_size,
        ui_scale=metrics_ui_scale,
        radius_ratio=radius_ratio,
        input_radius_ratio=input_radius_ratio,
        input_radius_px=input_radius_px(input_radius_ratio),
        motion_disabled=motion_disabled,
        motion_speed=motion_speed,
        tooltips_enabled=tooltips_enabled,
        icon_theme=icon_theme,
        colors=colors,
        generation=generation,
        desktop_available=desktop_available,
    )


def example_snapshot() -> PresentationSnapshot:
    return with_generation(
        PresentationSnapshot(
            version=SCHEMA_VERSION,
            enabled=True,
            generation="00000000-0000-0000-0000-000000000000",
            mode="dark",
            colors=Colors(**DEFAULT_DARK_COLORS),
            fonts=Fonts(
                ui_family="Sans Serif",
                fixed_family="monospace",
                base_point_size=BASE_POINT_SIZE_DEFAULT,
                ui_scale=1.0,
                fixed_scale=1.0,
            ),
            metrics=Metrics(ui_scale=1.0, radius_ratio=1.0, input_radius_ratio=1.0),
            motion=Motion(disabled=False, speed=1.0),
            tooltips_enabled=True,
            icon_theme="",
        )
    )
