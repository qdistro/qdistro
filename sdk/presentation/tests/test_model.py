"""Strict snapshot parser and producer normalizer."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from qdistro_presentation.model import (
    DEFAULT_DARK_COLORS,
    DESKTOP_SETTINGS_UNAVAILABLE,
    LocalOverrides,
    SnapshotError,
    contrast_ratio,
    desktop_status_text,
    example_snapshot,
    format_point_size,
    generation_for_content,
    loads_strict,
    normalize_producer,
    parse_local_overrides,
    parse_snapshot,
    parse_snapshot_text,
    resolve_presentation,
    with_generation,
)


def test_example_snapshot_roundtrip():
    snap = example_snapshot()
    parsed = parse_snapshot_text(snap.to_json())
    assert parsed == snap
    assert parsed.generation == generation_for_content(parsed.content_dict())


def test_default_palette_meets_contrast():
    colors = DEFAULT_DARK_COLORS
    assert contrast_ratio(colors["mSurface"], colors["mOnSurface"]) >= 4.5
    assert contrast_ratio(colors["mPrimary"], colors["mOnPrimary"]) >= 4.5
    assert contrast_ratio(colors["mSurfaceVariant"], colors["mOnSurfaceVariant"]) >= 3.0


def test_rejects_duplicate_keys():
    raw = '{"version":1,"version":2}'
    with pytest.raises(SnapshotError, match="duplicate"):
        loads_strict(raw)


def test_deeply_nested_json_is_snapshot_error():
    nested = "[" * 2000 + "]" * 2000
    with pytest.raises(SnapshotError):
        loads_strict(nested)


def test_oversized_json_integer_is_snapshot_error():
    with pytest.raises(SnapshotError):
        loads_strict('{"version":' + ("9" * 5000) + "}")


def test_unrepresentable_font_size_is_snapshot_error():
    snap = example_snapshot().to_dict()
    snap["fonts"]["basePointSize"] = int("1" + "0" * 400)
    with pytest.raises(SnapshotError, match="finite number"):
        parse_snapshot(snap)


def test_producer_unrepresentable_scale_uses_default():
    snap = normalize_producer(
        mode="dark",
        colors=DEFAULT_DARK_COLORS,
        settings={"ui": {"fontDefaultScale": int("1" + "0" * 400)}},
    )
    assert snap.fonts.ui_scale == 1.0


def test_rejects_nan_and_infinity():
    with pytest.raises(SnapshotError, match="non-finite"):
        loads_strict('{"v": NaN}')
    with pytest.raises(SnapshotError, match="non-finite"):
        loads_strict('{"v": Infinity}')


def test_rejects_unknown_version():
    snap = example_snapshot().to_dict()
    snap["version"] = 2
    with pytest.raises(SnapshotError, match="unsupported version"):
        parse_snapshot(snap)


def test_ignores_unknown_v1_fields():
    snap = example_snapshot().to_dict()
    snap["extra"] = {"ignored": True}
    parsed = parse_snapshot(snap)
    assert parsed.mode == "dark"


def test_rejects_incomplete_palette():
    snap = example_snapshot().to_dict()
    del snap["colors"]["mError"]
    snap["generation"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(SnapshotError, match="mError"):
        parse_snapshot(snap)


def test_rejects_unreadable_palette():
    snap = example_snapshot().to_dict()
    snap["colors"]["mOnSurface"] = snap["colors"]["mSurface"]
    with pytest.raises(SnapshotError, match="contrast"):
        parse_snapshot(snap)


def test_rejects_wrong_generation():
    snap = example_snapshot().to_dict()
    snap["generation"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    with pytest.raises(SnapshotError, match="generation"):
        parse_snapshot(snap)


def test_generation_stable_for_same_content():
    a = example_snapshot()
    b = with_generation(a)
    assert a.generation == b.generation
    disabled = with_generation(
        type(a)(
            version=a.version,
            enabled=False,
            generation="00000000-0000-0000-0000-000000000000",
            mode=a.mode,
            colors=a.colors,
            fonts=a.fonts,
            metrics=a.metrics,
            motion=a.motion,
            tooltips_enabled=a.tooltips_enabled,
            icon_theme=a.icon_theme,
        )
    )
    assert disabled.generation != a.generation


def test_producer_clamps_and_defaults():
    snap = normalize_producer(
        mode="dark",
        colors=DEFAULT_DARK_COLORS,
        settings={
            "ui": {
                "fontDefault": "",
                "fontDefaultScale": 9,
                "fontFixedScale": 0.1,
                "tooltipsEnabled": "yes",
            },
            "general": {"scaleRatio": 1.5, "animationSpeed": 99, "animationDisabled": 1},
        },
    )
    assert snap.fonts.ui_family == "Sans Serif"
    assert snap.fonts.ui_scale == 1.25
    assert snap.fonts.fixed_scale == 0.75
    assert snap.metrics.ui_scale == 1.2
    assert snap.motion.speed == 2.0
    assert snap.motion.disabled is False
    assert snap.tooltips_enabled is True


def test_producer_reads_ui_tooltips_not_general():
    snap = normalize_producer(
        mode="dark",
        colors=DEFAULT_DARK_COLORS,
        settings={"ui": {"tooltipsEnabled": False}, "general": {"tooltipsEnabled": True}},
    )
    assert snap.tooltips_enabled is False


def test_local_font_family_still_inherits_size():
    snap = example_snapshot()
    resolved = resolve_presentation(
        theme_mode="system",
        snapshot=snap,
        local=LocalOverrides(ui_font_family="Custom Sans"),
        native_ui_family="DejaVu Sans",
        native_fixed_family="DejaVu Sans Mono",
        native_icon_theme="breeze",
    )
    assert resolved.ui_family == "Custom Sans"
    assert resolved.ui_point_size == 11.0
    assert resolved.using_shared_palette is True


def test_local_palette_choice_still_inherits_font():
    snap = example_snapshot()
    resolved = resolve_presentation(
        theme_mode="dark",
        snapshot=snap,
        local=LocalOverrides(),
        native_ui_family="DejaVu Sans",
        native_fixed_family="DejaVu Sans Mono",
        native_icon_theme="breeze",
    )
    assert resolved.using_shared_palette is False
    assert resolved.ui_family == snap.fonts.ui_family


def test_enabled_false_drops_shared_layer():
    snap = example_snapshot()
    disabled = with_generation(
        type(snap)(
            version=snap.version,
            enabled=False,
            generation="00000000-0000-0000-0000-000000000000",
            mode=snap.mode,
            colors=snap.colors,
            fonts=snap.fonts,
            metrics=snap.metrics,
            motion=snap.motion,
            tooltips_enabled=snap.tooltips_enabled,
            icon_theme=snap.icon_theme,
        )
    )
    resolved = resolve_presentation(
        theme_mode="system",
        snapshot=disabled,
        local=LocalOverrides(ui_font_family="Keep Me"),
        native_ui_family="DejaVu Sans",
        native_fixed_family="DejaVu Sans Mono",
        native_icon_theme="breeze",
    )
    assert resolved.using_shared_palette is False
    assert resolved.desktop_available is False
    assert resolved.ui_family == "Keep Me"


def test_empty_icon_override_means_native():
    snap = with_generation(replace(example_snapshot(), icon_theme="Papirus"))
    resolved = resolve_presentation(
        theme_mode="system",
        snapshot=snap,
        local=LocalOverrides(icon_theme=""),
        native_ui_family="DejaVu Sans",
        native_fixed_family="DejaVu Sans Mono",
        native_icon_theme="breeze",
    )
    assert resolved.icon_theme == "breeze"


def test_content_sizes_ignore_ui_scale():
    snap = normalize_producer(
        mode="dark",
        colors=DEFAULT_DARK_COLORS,
        settings={"ui": {"fontFixedScale": 1.2}, "general": {"scaleRatio": 1.1}},
    )
    resolved = resolve_presentation(
        theme_mode="system",
        snapshot=snap,
        native_ui_family="Sans",
        native_fixed_family="Mono",
        native_icon_theme="",
    )
    assert resolved.content_fixed_point_size == pytest.approx(11 * 1.2)
    assert resolved.fixed_ui_point_size == pytest.approx(11 * 1.2 * 1.1)


def test_parse_local_overrides_absence():
    assert parse_local_overrides({}) == LocalOverrides()
    parsed = parse_local_overrides({"version": 1, "ui_font_family": "Inter"})
    assert parsed.ui_font_family == "Inter"
    assert parsed.persistable() == {"version": 1, "ui_font_family": "Inter"}


def test_oversize_rejected():
    snap = example_snapshot().to_dict()
    snap["pad"] = "x" * (70 * 1024)
    text = json.dumps(snap)
    with pytest.raises(SnapshotError, match="64 KiB"):
        parse_snapshot_text(text)


def test_shell_payload_shape_normalizes():
    """AppPresentationService.buildPayload() is this object, minus Qt.application.font."""
    snap = normalize_producer(
        mode="dark",
        colors=DEFAULT_DARK_COLORS,
        settings={
            "ui": {
                "fontDefault": "Inter",
                "fontFixed": "JetBrains Mono",
                "fontDefaultScale": 1.0,
                "fontFixedScale": 1.05,
                "tooltipsEnabled": False,
            },
            "general": {
                "scaleRatio": 1.0,
                "radiusRatio": 1.0,
                "iRadiusRatio": 1.0,
                "animationDisabled": False,
                "animationSpeed": 1.0,
            },
            "appearance": {"iconTheme": "breeze"},
        },
        default_ui_family="Sans Serif",
    )
    assert snap.mode == "dark"
    assert snap.fonts.ui_family == "Inter"
    assert snap.fonts.fixed_family == "JetBrains Mono"
    assert snap.fonts.fixed_scale == pytest.approx(1.05)
    assert snap.tooltips_enabled is False
    assert snap.icon_theme == "breeze"
    parsed = parse_snapshot_text(snap.to_json())
    assert parsed == snap


def _scaled_resolved(**kwargs):
    snap = example_snapshot()
    snap = with_generation(
        replace(snap, fonts=replace(snap.fonts, ui_scale=1.25, fixed_scale=1.25))
    )
    return resolve_presentation(
        theme_mode="system",
        snapshot=snap,
        native_ui_family="DejaVu Sans",
        native_fixed_family="DejaVu Sans Mono",
        native_icon_theme="breeze",
        **kwargs,
    )


def test_format_point_size_keeps_fraction():
    assert format_point_size(11.0) == "11"
    assert format_point_size(12.1) == "12.1"
    assert format_point_size(13.75) == "13.75"


def test_desktop_status_unavailable_without_snapshot():
    assert (
        desktop_status_text(None, follow_desktop=True, use_desktop_fonts=True)
        == DESKTOP_SETTINGS_UNAVAILABLE
    )
    assert (
        desktop_status_text(None, follow_desktop=True, use_desktop_fonts=False)
        == DESKTOP_SETTINGS_UNAVAILABLE
    )
    assert (
        desktop_status_text(None, follow_desktop=False, use_desktop_fonts=True)
        == DESKTOP_SETTINGS_UNAVAILABLE
    )
    assert (
        desktop_status_text(None, follow_desktop=False, use_desktop_fonts=False)
        == ""
    )


def test_desktop_status_inherited_fractional_ui_size():
    resolved = _scaled_resolved()
    assert resolved.ui_point_size == pytest.approx(13.75)
    text = desktop_status_text(
        resolved, follow_desktop=True, use_desktop_fonts=True
    )
    assert DESKTOP_SETTINGS_UNAVAILABLE not in text
    assert "13.75" in text
    assert resolved.ui_family in text
    local_fonts = desktop_status_text(
        resolved, follow_desktop=True, use_desktop_fonts=False
    )
    assert local_fonts == ""


def test_desktop_status_inherited_fixed_content_size():
    resolved = _scaled_resolved()
    text = desktop_status_text(
        resolved,
        follow_desktop=False,
        use_desktop_fonts=True,
        font_kind="fixed",
    )
    assert "13.75" in text
    assert resolved.fixed_family in text


def test_readable_on_color_always_meets_text_contrast():
    import random

    from qdistro_presentation.model import readable_on_color

    rng = random.Random(4242)
    for _ in range(5000):
        bg = "#%06x" % rng.randrange(0x1000000)
        assert contrast_ratio(bg, readable_on_color(bg)) >= 4.5, bg


def test_producer_corrects_only_failing_on_colors():
    from qdistro_presentation.model import COLOR_KEYS, normalize_producer

    weak = example_snapshot().colors.as_dict()
    weak["mPrimary"] = "#f5d76e"      # light yellow
    weak["mOnPrimary"] = "#ffffff"    # ~1.4:1 on it
    weak["mSecondary"] = "#8a8a8a"
    weak["mOnSecondary"] = "#9a9a9a"  # ~1.2:1
    with pytest.raises(SnapshotError, match="contrast"):
        parse_snapshot({**example_snapshot().to_dict(), "colors": weak})
    snap = normalize_producer(mode="light", colors=weak)
    out = snap.colors.as_dict()
    assert out["mOnPrimary"] == "#000000"
    assert out["mOnSecondary"] in ("#000000", "#ffffff")
    for key in COLOR_KEYS:
        if key not in ("mOnPrimary", "mOnSecondary"):
            assert out[key] == weak[key], key
    # The published document is readable by the strict reader.
    assert parse_snapshot(snap.to_dict()).colors == snap.colors


def test_every_bundled_qdshell_scheme_publishes():
    import json
    from pathlib import Path

    from qdistro_presentation.model import COLOR_KEYS, normalize_producer

    root = Path(__file__).resolve().parents[3] / "qdshell" / "Assets" / "ColorScheme"
    schemes = sorted(root.glob("*/*.json"))
    if not schemes:
        pytest.skip("qdshell color schemes not in this tree")
    for path in schemes:
        data = json.loads(path.read_text(encoding="utf-8"))
        for mode in ("dark", "light"):
            colors = data.get(mode)
            if not isinstance(colors, dict) or not all(k in colors for k in COLOR_KEYS):
                continue
            palette = {k: colors[k].lower() for k in COLOR_KEYS}
            snap = normalize_producer(mode=mode, colors=palette)
            out = snap.colors.as_dict()
            for key in COLOR_KEYS:
                if not key.startswith("mOn"):
                    assert out[key] == palette[key], (path.name, mode, key)
