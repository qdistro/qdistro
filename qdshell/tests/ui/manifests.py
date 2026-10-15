"""Surface manifests — the canonical list of what gets tested.

Each Surface is a (id, open_cmd, close_cmd, expectation_file) tuple. open_cmd and
close_cmd are lists passed to `qs ipc call`. expectation_file is the path
(relative to tests/ui/expectations/) of the human-authored golden description.

Coverage status:
  - Settings tabs: all 30 IPC-reachable tabs, via `settings openTab <name>`.
    "autostart" and "vault" appear in SettingsContent.qml's tabsModel but have no
    _settingsTabMap entry, so they are not openable via IPC and not covered here.
  - Panels: 9 of 13 have first-class IPC toggle hooks. Audio, Brightness, Tray,
    Plugins lack panel-open IPC in current qdshell — listed with NO_IPC for now
    so the manifest stays complete; the harness skips them with a clear message.
  - Bar: bar is always visible when shell is running; one full-shell screenshot
    is taken with no panel open (sanity baseline for bar/dock layout).
"""

from dataclasses import dataclass
from typing import Optional

from . import fixtures

NO_IPC = object()  # sentinel: surface known but no IPC handle yet


@dataclass(frozen=True)
class ZoomCrop:
    """A magnified, contrast-stretched crop of one captured view.

    Some load-bearing details are too small or too low-contrast for the
    vision describer to read from the full 1280x800 frame (a disabled
    switch's knob inside a near-black outline). The harness crops `box`
    out of an already-captured view, stretches its contrast, magnifies it
    and describes it as an EXTRA view with its own `question`. It adds
    evidence to the judged description; it never replaces a full view.

    `knob_must_be` ("left"/"right"), when set, makes the crop a switch
    check on top of the description: the harness measures the knob's side
    from the UNSTRETCHED pixels (runner.measure_switch_knob) and fails the
    capture unless it is at that end. The measurement line is appended to
    the crop's described view, so the judge sees it next to the crop text.
    """
    view: int        # 0 = the first capture; n = the view after post_open_clicks[n-1]
    box: tuple       # (x0, y0, x1, y1) screen fractions of the region to crop
    label: str       # short human name used in the stitched description
    question: str    # what the describer must report from the crop (neutral)
    scale: int = 8   # integer magnification
    knob_must_be: Optional[str] = None   # "left" / "right": measured, enforced


@dataclass(frozen=True)
class Surface:
    id: str
    kind: str                  # "settings" | "panel" | "bar"
    open_cmd: object           # list[str] of `qs ipc call` args, or NO_IPC
    close_cmd: object          # list[str] of `qs ipc call` args, or NO_IPC
    expectation: str           # filename under tests/ui/expectations/
    # Optional fixtures: bash snippets run inside the guest as admin, on
    # qdshell's own session bus (see runner.guest_sh_vm), before the surface
    # opens (setup_guest) and after it closes (teardown_guest — a failed
    # restore is a test error, not a warning; see runner.guest_cleanup_vm).
    setup_guest: tuple = ()
    teardown_guest: tuple = ()
    # Optional extra described visual states: each (x_frac, y_frac) is a real
    # QMP mouse click at that screen fraction after the panel opens; each
    # resulting view is captured and described. This covers non-default
    # subtabs and controls whose options are hidden until opened.
    post_open_clicks: tuple = ()
    # Optional pointer hovers (mouse_move, no click) applied before the
    # FIRST capture — used where the golden covers hover/focus affordances
    # (e.g. the session menu's highlighted card) without risking a real
    # activation click.
    post_open_moves: tuple = ()
    # Optional key taps (QMP qcodes) applied before the FIRST capture —
    # e.g. the session menu ignores mouse hover until the pointer moves
    # twice, but an arrow key sets a real selectedIndex deterministically.
    post_open_keys: tuple = ()
    # Optional ZoomCrop entries: magnified, contrast-stretched crops of a
    # captured view, each described as an extra view (see ZoomCrop).
    zoom_crops: tuple = ()


# 30 covered settings tabs — `qs ipc call settings openTab <name>`.
# Names are a subset of IPCService.qml::_settingsTabMap keys; the map also
# contains "about", which we deliberately omit (see notes below — "about"
# falls back to General).
SETTINGS_TABS = [
    "general", "userinterface", "colorscheme", "wallpaper", "bar", "dock",
    "desktopwidgets", "desktopicons", "controlcenter", "launcher", "notifications",
    "audio", "display", "location", "mouse", "keyboard", "accessibility", "osd",
    "connections", "hooks", "lockscreen", "session", "sessionmenu", "systemmonitor",
    "plugins", "power", "windowmanager", "advanced", "appearance", "defaultapps",
]
# Notes on tabs we deliberately don't cover:
#  - "about" — the IPC map keeps the name and SettingsPanel.Tab.About exists,
#    but qdshell strips the About entry from SettingsContent's tabsModel, so
#    `openTab about` resolves to a tab with no model row and falls back to the
#    General view. Not testable.
#  - "appearance" — `openTab appearance` resolves to SettingsPanel.Tab.Appearance.
#    Now covers GTK-theme/font-rendering/icon-policy/sound-theme controls that no
#    longer overlap colorscheme/userinterface, so it has its own expectation.
#  - "Region" tab in the audit corresponds to "location" in the IPC map.
#  - "Vault" (from the original audit) isn't in _settingsTabMap and isn't
#    IPC-reachable.

# Settings tabs whose golden covers an additional state: each fraction pair
# clicks a real control and captures/describes the resulting view. Most are
# non-default subtabs (strip at y≈124 of 800); Notifications opens the Toast
# theme chooser lower in the Appearance subtab to reveal its options.
_SETTINGS_EXTRA_CLICKS = {
    "audio": ((0.527, 0.155),),                   # Devices
    "connections": ((0.695, 0.155),),             # Bluetooth
    "display": ((0.586, 0.155),),                 # Brightness
    "hooks": ((0.695, 0.155),),                   # Hooks (list/editor)
    "plugins": ((0.586, 0.155), (0.734, 0.155)),  # Available, Sources
    "systemmonitor": ((0.727, 0.155),),           # Thresholds
    "wallpaper": ((0.578, 0.155),),               # Look (fill mode / transitions)
    "notifications": ((0.730, 0.450),),           # Toast theme choices
}

# Settings tabs whose golden needs a detail the full frame cannot show the
# describer reliably. Connections: with no Bluetooth adapter the master switch
# is disabled at 0.6 opacity and its off-state track outline is #161645 on a
# #070722 card; luna repeatedly read the knob as "RIGHT / on" from the full
# frame although it sits at the left end (knob x~982-996 in a track
# x~977-1020 at y~176-201 of 1280x800, measured on the 2026-10-13 frame).
# 2026-10-14 full run: the crop was described "knob at the LEFT end" while the
# full-frame describer still placed the thumb RIGHT, and the judge sided with
# the full frame. The crop is therefore also measured from pixels and the
# product's off state enforced: NToggle draws an unchecked thumb at x=3 (the
# LEFT end); BluetoothSubTab binds the master toggle's `checked` to
# BluetoothService.enabled (false without an adapter) and disables it
# (`enabled: ... && BluetoothService.bluetoothAvailable`).
_SETTINGS_ZOOM_CROPS = {
    "connections": (
        ZoomCrop(
            view=1,                                   # after clicking Bluetooth
            box=(960 / 1280, 170 / 800, 1026 / 1280, 206 / 800),
            label="Bluetooth master switch",
            question=(
                "The crop shows one toggle switch. Describe the outline of "
                "its track (its pill/rounded-rectangle shape) and say where "
                "the round knob sits inside that track: at the LEFT end, the "
                "RIGHT end, or the middle. Say whether the track interior "
                "is filled with a solid color or is an empty outline. If no "
                "switch is recognisable, say so."
            ),
            knob_must_be="left",
        ),
    ),
}

# Settings tabs needing a guest-side state fixture (see fixtures.py for what
# each seeds and why a restart-seed is required).
_SETTINGS_GUEST_FIXTURES = {
    # Exercise a real per-app PipeWire stream in the Volumes tab; the
    # empty-stream state is still truthful when this fixture is absent.
    "audio": (fixtures.AUDIO_APP_SEED, fixtures.AUDIO_APP_CLEAN),
    # "changed from default" marker dots + enabled reset buttons only render
    # when a live value differs from its default; seed one.
    "advanced": (fixtures.ADVANCED_SEED, fixtures.ADVANCED_CLEAN),
    # The installed-plugin row's toggle/uninstall affordances only render for
    # an actually-installed plugin; seed one so the populated state is judged.
    "plugins": (fixtures.PLUGIN_SEED, fixtures.PLUGIN_CLEAN),
}

SETTINGS_SURFACES = [
    Surface(
        id=f"settings_{tab}",
        kind="settings",
        open_cmd=["settings", "openTab", tab],
        close_cmd=["settings", "toggle"],   # toggle closes when open
        expectation=f"settings_{tab}.md",
        setup_guest=(() if tab not in _SETTINGS_GUEST_FIXTURES
                     else (_SETTINGS_GUEST_FIXTURES[tab][0],)),
        teardown_guest=(() if tab not in _SETTINGS_GUEST_FIXTURES
                        else (_SETTINGS_GUEST_FIXTURES[tab][1],)),
        post_open_clicks=_SETTINGS_EXTRA_CLICKS.get(tab, ()),
        zoom_crops=_SETTINGS_ZOOM_CROPS.get(tab, ()),
    )
    for tab in SETTINGS_TABS
]


PANEL_SURFACES = [
    Surface("panel_battery",        "panel", ["battery", "togglePanel"],         ["battery", "togglePanel"],         "panel_battery.md"),
    Surface("panel_bluetooth",      "panel", ["bluetooth", "togglePanel"],       ["bluetooth", "togglePanel"],       "panel_bluetooth.md"),
    # panel_calendar: seed the service's own location cache so the weather
    # card renders a live open-meteo forecast — the api.qdshell.dev geocode
    # hop it would otherwise need is dead upstream (filed follow-up).
    Surface("panel_calendar",       "panel", ["calendar", "toggle"],             ["calendar", "toggle"],             "panel_calendar.md",
            setup_guest=(fixtures.LOCATION_SEED,), teardown_guest=(fixtures.LOCATION_CLEAN,)),
    Surface("panel_media",          "panel", ["media", "toggle"],                ["media", "toggle"],                "panel_media.md",
            setup_guest=(fixtures.MPRIS_START,), teardown_guest=(fixtures.MPRIS_STOP,)),
    Surface("panel_network",        "panel", ["network", "togglePanel"],         ["network", "togglePanel"],         "panel_network.md"),
    Surface("panel_notifications",  "panel", ["notifications", "toggleHistory"], ["notifications", "toggleHistory"], "panel_notifications.md",
            setup_guest=(fixtures.NOTIFY_SEED,), teardown_guest=(fixtures.NOTIFY_CLEAN,)),
    Surface("panel_controlcenter",  "panel", ["controlCenter", "toggle"],        ["controlCenter", "toggle"],        "panel_controlcenter.md"),
    Surface("panel_wallpaper",      "panel", ["wallpaper", "toggle"],            ["wallpaper", "toggle"],            "panel_wallpaper.md"),
    # panel_sessionmenu: restore the shipped powerOptions defaults (keybind
    # hints + a single row that fits) — the VM's persisted list predates the
    # keybind field and legitimately hides hints.
    Surface("panel_sessionmenu",    "panel", ["sessionMenu", "toggle"],          ["sessionMenu", "toggle"],          "panel_sessionmenu.md",
            setup_guest=(fixtures.SESSIONMENU_SEED,), teardown_guest=(fixtures.SESSIONMENU_CLEAN,),
            post_open_keys=("right",)),   # select an action card for real
    Surface("panel_systemmonitor",  "panel", ["systemMonitor", "toggle"],        ["systemMonitor", "toggle"],        "panel_systemmonitor.md"),
    Surface("panel_launcher",       "panel", ["launcher", "toggle"],             ["launcher", "toggle"],             "panel_launcher.md"),
    # panel_audio's golden covers the Devices tab too (device selectors);
    # the extra click opens it so the judged description covers both tabs.
    Surface("panel_audio",          "panel", ["audio", "togglePanel"],      ["audio", "togglePanel"],      "panel_audio.md",
            post_open_clicks=((0.906, 0.156),)),
    Surface("panel_brightness",     "panel", ["brightness", "togglePanel"], ["brightness", "togglePanel"], "panel_brightness.md"),
    Surface("panel_tray",           "panel", ["tray", "togglePanel"],       ["tray", "togglePanel"],       "panel_tray.md"),
]


BAR_SURFACES = [
    # Bar is the resting state of the shell; no panel open. We just screenshot.
    Surface("bar_idle",             "bar", None, None, "bar_idle.md"),
]


ALL_SURFACES = SETTINGS_SURFACES + PANEL_SURFACES + BAR_SURFACES
