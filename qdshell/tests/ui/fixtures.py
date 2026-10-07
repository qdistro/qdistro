"""Guest-side fixtures for UI surfaces that need live backing services.

Each constant is a bash snippet executed inside the VM via
runner.guest_sh_vm — i.e. as the admin user, on qdshell's own
`dbus-run-session` bus (so notify-send and MPRIS name registration reach
the same D-Bus services qdshell listens on). These set up REAL observable
state (notifications in the service's history, an org.mpris.* name on the
bus) — the panel renders genuine content; nothing about the framebuffer
or judge is mocked.
"""


# --- Notification history -------------------------------------------------
# History panels only render range tabs / entries once history is non-empty
# (NotificationHistoryPanel.qml: headerBox `visible:` is gated on
# historyList.count > 0). Seed three real notifications so the populated
# state is what gets judged.
# Three real notifications via notify-send on qdshell's own session bus.
# History is populated at RECEIPT, so `notifications dismissAll` drops the
# live popups while keeping the entries — the panel's time-range tabs
# (All/Today/Yesterday/Earlier) only render once history is non-empty
# (NotificationHistoryPanel.qml gates headerBox on historyList.count > 0),
# and the popups are gone before they can obscure the captured frame.
NOTIFY_SEED = (
    'notify-send -a "qdshell-test" "Mail" "You have 3 unread messages"; '
    'notify-send -a "qdshell-test" "Updates" "qdistro 1.2.3 is available"; '
    'notify-send -a "qdshell-test" "Builds" "qci host finished"; '
    'sleep 1; '
    'qs ipc -p /usr/share/quickshell/qdshell call notifications dismissAll '
    '|| true; '
    'sleep 1; '
)

NOTIFY_CLEAN = (
    'qs ipc -p /usr/share/quickshell/qdshell call notifications clear || true'
)


# --- Shared: restart qdshell and wait for the qdwin bind ----------------------
# guest_sh_vm snippets run AS admin with XDG_RUNTIME_DIR exported, so plain
# `systemctl --user` reaches the right manager. Settings FileViews only load
# at shell start (the watcher can't arm a path that didn't exist, and live
# reload doesn't re-resolve option lists), so fixtures that change persistent
# state must restart the unit and wait for `qdwin_shell_v1 bound` — otherwise
# the test races the reconnect gap.
_RESTART_AND_WAIT = (
    'TS=$(date "+%Y-%m-%d %H:%M:%S")\n'
    "systemctl --user restart qdshell\n"
    "for i in $(seq 1 45); do\n"
    '  journalctl --user _COMM=quickshell --since "$TS" 2>/dev/null '
    "| grep -q 'bound v' && break\n"
    "  sleep 1\n"
    "done\n"
    "sleep 2\n"
)


# --- Weather location seed --------------------------------------------------
# LocationService geocodes Settings.data.location.name through
# api.qdshell.dev/geocode — an endpoint that does not currently resolve, so
# weather can never reach the forecast render path. The service's own cache
# file (~/.cache/qdshell/location.json) is the supported bypass: when it holds
# coordinates AND the cached name matches the configured location, the dead
# geocode hop is skipped and weather is fetched live from api.open-meteo.com
# (reachable from the VM). Seeding exercises the REAL forecast render path;
# it does not fabricate weather data.
#
# Caveat: FileView's watcher cannot arm a path that doesn't exist when it
# loads, so the file is written BEFORE a guest-side qdshell restart, and the
# fixture then waits for the `qdwin_shell_v1 bound` journal line so the test
# proceeds against a live shell.
LOCATION_SEED = (
    "mkdir -p /home/admin/.cache/qdshell\n"
    "python3 - <<'QDEOF'\n"
    "import json, time\n"
    "loc = {\"latitude\": \"35.6762\", \"longitude\": \"139.6503\", "
    "\"name\": \"Tokyo\", \"weatherLastFetch\": int(time.time()), "
    "\"weather\": None}\n"
    "open(\"/home/admin/.cache/qdshell/location.json\", \"w\").write("
    "json.dumps(loc))\n"
    "QDEOF\n"
    # guest_sh_vm already runs the snippet AS admin with XDG_RUNTIME_DIR set —
    # plain `systemctl --user` reaches the right manager.
    + _RESTART_AND_WAIT +
    "sleep 5\n"   # qdwin bind + first open-meteo fetch round-trip
)

LOCATION_CLEAN = (
    "rm -f /home/admin/.cache/qdshell/location.json\n"
)


# --- MPRIS media player -----------------------------------------------------
# A minimal org.mpris.MediaPlayer2.qdtest service on the session bus: real
# D-Bus, real MPRIS properties, Position advancing once a second — the media
# panel reads exactly what a real player would publish. dbus-python + GLib
# are both in the baseweed image (verified).
_MPRIS_PY = r'''
import sys
import dbus, dbus.service, dbus.mainloop.glib
from gi.repository import GLib

# argv: <bus-name-suffix> <identity> <title> <artist>
SUFFIX, IDENTITY, TITLE, ARTIST = sys.argv[1:5]

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
bus = dbus.SessionBus()
name = dbus.service.BusName("org.mpris.MediaPlayer2." + SUFFIX, bus)

class Player(dbus.service.Object):
    ROOT = "org.mpris.MediaPlayer2"
    PLAYER = "org.mpris.MediaPlayer2.Player"

    def __init__(self):
        super().__init__(bus, "/org/mpris/MediaPlayer2")
        self.pos_us = 42_000_000
        self.props = {
            self.ROOT: {
                "CanQuit": dbus.Boolean(False),
                "CanRaise": dbus.Boolean(False),
                "HasTrackList": dbus.Boolean(False),
                "Identity": dbus.String(IDENTITY),
                "DesktopEntry": dbus.String(SUFFIX),
                "SupportedUriSchemes": dbus.Array([], signature="s"),
                "SupportedMimeTypes": dbus.Array([], signature="s"),
            },
            self.PLAYER: {
                "PlaybackStatus": dbus.String("Playing"),
                "LoopStatus": dbus.String("None"),
                "Rate": dbus.Double(1.0),
                "Shuffle": dbus.Boolean(False),
                "Metadata": dbus.Dictionary({
                    "mpris:trackid": dbus.ObjectPath("/org/mpris/MediaPlayer2/track/1"),
                    "xesam:title": dbus.String(TITLE),
                    "xesam:artist": dbus.Array([dbus.String(ARTIST)], signature="s"),
                    "xesam:album": dbus.String("Fixture Album"),
                    "mpris:length": dbus.Int64(240_000_000),
                }, signature="sv"),
                "Volume": dbus.Double(0.8),
                "MinimumRate": dbus.Double(1.0),
                "MaximumRate": dbus.Double(1.0),
                "CanGoNext": dbus.Boolean(True),
                "CanGoPrevious": dbus.Boolean(True),
                "CanPlay": dbus.Boolean(True),
                "CanPause": dbus.Boolean(True),
                "CanSeek": dbus.Boolean(True),
                "CanControl": dbus.Boolean(True),
            },
        }
        GLib.timeout_add(1000, self._tick)

    def _tick(self):
        # MPRIS Position is poll-only (never in GetAll / PropertiesChanged);
        # expose it via Get so the panel's poller sees it advance.
        if self.props[self.PLAYER]["PlaybackStatus"] == "Playing":
            self.pos_us += 1_000_000
        return True

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        if iface == self.PLAYER and prop == "Position":
            return dbus.Int64(self.pos_us)
        return self.props[iface][prop]

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.props.get(iface, {})

    @dbus.service.method("org.freedesktop.DBus.Properties",
                         in_signature="ssv")
    def Set(self, iface, prop, val):
        self.props[iface][prop] = val
        self.PropertiesChanged(iface, {prop: val}, [])

    @dbus.service.signal("org.freedesktop.DBus.Properties",
                         signature="sa{sv}as")
    def PropertiesChanged(self, iface, changed, invalidated):
        pass

    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def Play(self): self._set_status("Playing")
    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def Pause(self): self._set_status("Paused")
    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def PlayPause(self):
        self._set_status("Paused" if self.props[self.PLAYER]["PlaybackStatus"]
                         == "Playing" else "Playing")
    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def Next(self): pass
    @dbus.service.method(PLAYER, in_signature="", out_signature="")
    def Previous(self): pass
    @dbus.service.method(PLAYER, in_signature="x", out_signature="")
    def Seek(self, offset): pass

    def _set_status(self, st):
        self.props[self.PLAYER]["PlaybackStatus"] = dbus.String(st)
        self.PropertiesChanged(self.PLAYER, {"PlaybackStatus": dbus.String(st)}, [])

Player()
GLib.MainLoop().run()
'''

# Two players are registered: the header player-selector chip only renders
# when MediaService sees >1 player, and the golden asserts it.
MPRIS_START = (
    "install -d /run/user/1000/qdui-fixtures\n"
    "cat > /run/user/1000/qdui-fixtures/mpris_player.py <<'QDEOF'\n"
    + _MPRIS_PY +
    "QDEOF\n"
    "pkill -u admin -f qdui-fixtures/mpris_player.py 2>/dev/null || true\n"
    "nohup python3 /run/user/1000/qdui-fixtures/mpris_player.py "
    "qdtest 'qdtest player' 'Fixture Track' 'Fixture Artist' "
    ">/run/user/1000/qdui-fixtures/mpris.log 2>&1 &\n"
    "nohup python3 /run/user/1000/qdui-fixtures/mpris_player.py "
    "qdtest2 'qdtest second player' 'Second Track' 'Second Artist' "
    ">/run/user/1000/qdui-fixtures/mpris2.log 2>&1 &\n"
    # Wait until BOTH names are owned on qdshell's private session bus
    # before the panel opens (dbus-send --session honors
    # DBUS_SESSION_BUS_ADDRESS; busctl --user would not).
    "for i in $(seq 1 30); do\n"
    "  N=$(dbus-send --session --dest=org.freedesktop.DBus --print-reply "
    "/org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>/dev/null "
    "| grep -c 'org.mpris.MediaPlayer2.qdtest')\n"
    '  [ "$N" -ge 2 ] && break\n'
    "  sleep 0.2\n"
    "done\n"
    "N=$(dbus-send --session --dest=org.freedesktop.DBus --print-reply "
    "/org/freedesktop/DBus org.freedesktop.DBus.ListNames 2>/dev/null "
    "| grep -c 'org.mpris.MediaPlayer2.qdtest')\n"
    '[ "$N" -ge 2 ] '
    "|| { echo 'mpris fixtures did not acquire bus names' >&2; exit 65; }\n"
)

MPRIS_STOP = (
    "pkill -u admin -f qdui-fixtures/mpris_player.py || true\n"
    "rm -f /run/user/1000/qdui-fixtures/mpris_player.py "
    "/run/user/1000/qdui-fixtures/mpris.log\n"
)


# --- Degraded audio -----------------------------------------------------------
# The degraded[audio] golden asserts the coherent no-devices state a panel
# must show when its backing service is absent. This VM HAS a sound device
# (ich9-hda + pipewire), so the absence is induced for the duration of the
# case and restored immediately after — panel_audio later in the suite still
# sees real devices.
AUDIO_DEGRADE = (
    "systemctl --user stop pipewire.service pipewire.socket wireplumber.service "
    "2>/dev/null || true\n"
    "sleep 1\n"
)

AUDIO_RESTORE = (
    "systemctl --user start pipewire.socket pipewire.service wireplumber.service "
    "2>/dev/null || true\n"
)

# --- Session menu defaults ----------------------------------------------------
# The VM's persisted settings.json carries a 7-entry powerOptions list with NO
# "keybind" fields (older-shape serialization) — so the keybind hint pills are
# legitimately hidden and the single-row layout overflows 1280px. Seeding the
# shipped defaults (6 entries, keybinds "1"-"6", showKeybinds on) restores the
# configuration the golden describes; FileView watches settings.json so the
# panel re-reads it live. Teardown restores the file byte-for-byte.
SESSIONMENU_SEED = (
    "python3 - <<'QDEOF'\n"
    "import json, shutil\n"
    'path = "/home/admin/.config/qdshell/settings.json"\n'
    'shutil.copy2(path, path + ".qdtest-bak")\n'
    "d = json.load(open(path))\n"
    'sm = d.setdefault("sessionMenu", {})\n'
    'sm["showKeybinds"] = True\n'
    'sm["powerOptions"] = [\n'
    '    {"action": "lock", "enabled": True, "keybind": "1"},\n'
    '    {"action": "suspend", "enabled": True, "keybind": "2"},\n'
    '    {"action": "hibernate", "enabled": True, "keybind": "3"},\n'
    '    {"action": "reboot", "enabled": True, "keybind": "4"},\n'
    '    {"action": "logout", "enabled": True, "keybind": "5"},\n'
    '    {"action": "shutdown", "enabled": True, "keybind": "6"}]\n'
    'json.dump(d, open(path, "w"), indent=2)\n'
    "QDEOF\n"
    # Settings only load at shell start — restart so the defaults apply.
    + _RESTART_AND_WAIT
)

SESSIONMENU_CLEAN = (
    'test -f /home/admin/.config/qdshell/settings.json.qdtest-bak && '
    'mv -f /home/admin/.config/qdshell/settings.json.qdtest-bak '
    '/home/admin/.config/qdshell/settings.json || true\n'
    # Settings only load at shell start — restart again so later surfaces see
    # the VM's real (restored) configuration.
    + _RESTART_AND_WAIT
)


# --- Advanced tab: one deliberately non-default value -------------------------
# The golden asserts the "changed from default" affordances — marker dots and
# a reset button enabled ONLY on changed rows (AdvancedTab.qml: marker dot
# `visible: rowItem.changed`, reset `enabled: rowItem.changed`). With a stock
# config every row is at its default, so neither affordance is ever rendered
# and the golden cannot be judged. Seeding one real non-default value
# (bar.showOutline=true; default false — the row sits in the first viewport)
# makes the affordances deterministically visible. Teardown restores the file
# byte-for-byte and restarts so later surfaces see the real config.
ADVANCED_SEED = (
    "python3 - <<'QDEOF'\n"
    "import json, shutil\n"
    'path = "/home/admin/.config/qdshell/settings.json"\n'
    'shutil.copy2(path, path + ".qdtest-adv-bak")\n'
    "d = json.load(open(path))\n"
    'd.setdefault("bar", {})["showOutline"] = True\n'
    'json.dump(d, open(path, "w"), indent=2)\n'
    "QDEOF\n"
    # Settings only load at shell start — restart so the value is live.
    + _RESTART_AND_WAIT
)

ADVANCED_CLEAN = (
    'test -f /home/admin/.config/qdshell/settings.json.qdtest-adv-bak && '
    'mv -f /home/admin/.config/qdshell/settings.json.qdtest-adv-bak '
    '/home/admin/.config/qdshell/settings.json || true\n'
    + _RESTART_AND_WAIT
)
