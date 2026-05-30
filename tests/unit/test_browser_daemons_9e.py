"""Unit tests for the Phase-9e browser desktop-integration daemons.

Spec: ``todo/browser/01-bridge-phase9.md`` §9e and
``todo/browser/02-qdbrowser-unification.md`` step-4.

Covers the four SESSION-bus daemons that own the bus names the bridge's
9e handlers call:

  * ``org.qdistro.Mpris``         — qdistro_mpris_daemon
  * ``org.qdistro.Downloads``     — qdistro_downloads_daemon
  * ``org.qdistro.Notifications`` — qdistro_notifications_daemon
  * ``org.qdistro.Compositor``    — qdistro_compositor_daemon

Each daemon's dispatch core is pure: it takes the decoded request body
plus the kernel-attested caller (uid, pid) and an injectable sink, so
these tests never touch a real session bus. Every daemon shares the
``browser_bridge_allowed`` identity gate (qdistro_browser_daemon_identity)
which is driven through injectable /proc readers.

Each suite asserts BOTH the success response shape AND identity-deny
behaviour (``parent_not_allowed`` for a non-bridge caller; cross-user
isolation where it applies).
"""
from __future__ import annotations

import pytest

import qdistro_browser_daemon_identity as ident
import qdistro_mpris_daemon as mpris
import qdistro_downloads_daemon as downloads
import qdistro_notifications_daemon as notifications
import qdistro_compositor_daemon as compositor


# --------------------------------------------------------------------- #
# Shared identity-gate doubles
# --------------------------------------------------------------------- #

def allow_gate(pid):
    """A bridge-identity gate that always allows (caller IS the bridge)."""
    return True, "browser-bridge"


def deny_gate(pid):
    """A gate that always denies (caller is not the bridge)."""
    return False, "not-browser-bridge"


# =====================================================================
# Shared identity helper — browser_bridge_allowed
# =====================================================================

class TestBrowserBridgeAllowed:
    BRIDGE = "/usr/libexec/qdistro/qdistro_browser_bridge.py"
    FF = "/usr/lib64/firefox/firefox"

    def _gate(self, *, cmdline, ppid, parent_exe):
        return ident.browser_bridge_allowed(
            1234,
            bridge_script=self.BRIDGE,
            parent_exes=(self.FF,),
            cmdline_reader=lambda _pid: cmdline,
            ppid_reader=lambda _pid: ppid,
            exe_reader=lambda _pid: parent_exe,
        )

    def test_happy_path(self):
        ok, reason = self._gate(
            cmdline=["python3", self.BRIDGE, "chrome-extension://x/"],
            ppid=100, parent_exe=self.FF)
        assert ok is True
        assert reason == "browser-bridge"

    def test_valueless_flags_tolerated(self):
        ok, _ = self._gate(
            cmdline=["python3", "-I", "-S", self.BRIDGE, "arg"],
            ppid=100, parent_exe=self.FF)
        assert ok is True

    def test_operand_flag_fails_closed(self):
        # -W consumes the next token; the bridge path could be smuggled
        # as the flag operand while python runs a different file.
        ok, reason = self._gate(
            cmdline=["python3", "-W", self.BRIDGE, "evil.py"],
            ppid=100, parent_exe=self.FF)
        assert ok is False
        assert reason == "not-browser-bridge"

    def test_wrong_script_denied(self):
        ok, reason = self._gate(
            cmdline=["python3", "/tmp/evil.py", self.BRIDGE],
            ppid=100, parent_exe=self.FF)
        assert ok is False
        assert reason == "not-browser-bridge"

    def test_non_browser_parent_denied(self):
        ok, reason = self._gate(
            cmdline=["python3", self.BRIDGE],
            ppid=100, parent_exe="/usr/bin/bash")
        assert ok is False
        assert reason == "parent-not-browser"

    def test_missing_parent_denied(self):
        ok, reason = self._gate(
            cmdline=["python3", self.BRIDGE], ppid=None, parent_exe=self.FF)
        assert ok is False
        assert reason == "parent-unreadable"

    def test_username_for_uid_fallback(self):
        # uid 0 always resolves (root); a wild uid falls back to uid:<n>.
        assert ident.username_for_uid(0) == "root"
        assert ident.username_for_uid(2_000_111) == "uid:2000111"

    def test_browser_label_mapping(self):
        assert ident.browser_label("/usr/lib64/firefox/firefox") == "firefox"
        assert ident.browser_label(
            "/usr/bin/google-chrome-stable") == "chrome"
        assert ident.browser_label("/usr/bin/brave-browser") == "brave"
        assert ident.browser_label("") == "unknown"

    def test_caller_advisory_bounded(self):
        ext, exe = ident.caller_advisory(
            {"extension_id": "x" * 500, "parent_exe": "y" * 500})
        assert len(ext) <= 128
        assert len(exe) <= 256

    def test_caller_advisory_strips_control_chars(self):
        ext, _ = ident.caller_advisory({"extension_id": "a\nb\x00c"})
        assert ext == "abc"


# =====================================================================
# MPRIS daemon
# =====================================================================

class _RecordingPublisher(mpris._BasePublisher):
    def __init__(self, fail=False):
        self.calls = []
        self._fail = fail

    def publish(self, player_name, playback_status, metadata, *,
                bridge_name="", uid=-1, browser=""):
        if self._fail:
            raise RuntimeError("sink boom")
        self.calls.append((player_name, playback_status, metadata,
                           bridge_name, uid, browser))


class TestMprisDaemon:
    def test_publish_happy_path(self):
        pub = _RecordingPublisher()
        reply = mpris.handle_publish(
            {"title": "Song", "artist": "Band",
             "playback_status": "playing",
             "parent_exe": "/usr/lib64/firefox/firefox", "tab_id": 5},
            caller_uid=0, caller_pid=1234, publisher=pub,
            bridge_gate=allow_gate,
            bridge_name_fn=lambda pid: f"org.qdistro.BrowserBridge.{pid}")
        assert reply["ok"] is True
        assert reply["playback_status"] == "Playing"
        assert reply["player"] == "org.mpris.MediaPlayer2.qdistro.root.firefox"
        assert len(pub.calls) == 1
        name, status, meta, bridge_name, uid, browser = pub.calls[0]
        assert status == "Playing"
        assert meta["xesam:title"] == "Song"
        assert meta["xesam:artist"] == ["Band"]
        assert meta["mpris:trackid"].endswith("/5")
        # The player is bound to the originating bridge connection so a
        # later Play/Pause routes back to it, and to the attested uid +
        # resolved browser label.
        assert bridge_name == "org.qdistro.BrowserBridge.1234"
        assert uid == 0
        assert browser == "firefox"

    def test_publish_user_in_player_name(self):
        # uid 0 -> root; the <user> segment is the kernel-attested uid,
        # not anything from the body.
        pub = _RecordingPublisher()
        reply = mpris.handle_publish(
            {"title": "x", "user": "attacker",
             "parent_exe": "/usr/bin/chromium"},
            caller_uid=0, caller_pid=1, publisher=pub, bridge_gate=allow_gate,
            bridge_name_fn=lambda pid: "org.qdistro.BrowserBridge.0")
        assert reply["player"] == (
            "org.mpris.MediaPlayer2.qdistro.root.chromium")

    def test_publish_denied_non_bridge(self):
        pub = _RecordingPublisher()
        reply = mpris.handle_publish(
            {"title": "x"}, caller_uid=0, caller_pid=1, publisher=pub,
            bridge_gate=deny_gate)
        assert reply["ok"] is False
        assert reply["error"] == "parent_not_allowed"
        assert pub.calls == []

    def test_publish_sink_failure(self):
        pub = _RecordingPublisher(fail=True)
        reply = mpris.handle_publish(
            {"title": "x"}, caller_uid=0, caller_pid=1, publisher=pub,
            bridge_gate=allow_gate,
            bridge_name_fn=lambda pid: "org.qdistro.BrowserBridge.0")
        assert reply["ok"] is False
        assert reply["error"] == "publish_failed"

    def test_normalize_playback_status(self):
        assert mpris.normalize_playback_status("Playing") == "Playing"
        assert mpris.normalize_playback_status("paused") == "Paused"
        assert mpris.normalize_playback_status("garbage") == "Stopped"
        assert mpris.normalize_playback_status(None) == "Stopped"

    def test_player_name_sanitises_segments(self):
        name = mpris.player_name_for(0, "/usr/bin/some.weird/browser!")
        # No dots/slashes/bang leak into the <browser> segment.
        suffix = name.rsplit(".", 1)[-1]
        assert suffix == "unknown"  # unrecognised exe -> unknown

    def test_control_routes_to_bridge(self):
        class _BC(mpris._BridgeClient):
            def __init__(self):
                self.calls = []

            def control(self, uid, browser, action, tab_id):
                self.calls.append((uid, browser, action, tab_id))
                return {"ok": True, "delivered": True}

        bc = _BC()
        reply = mpris.handle_control(
            "PlayPause", uid=0, browser="firefox", tab_id=3,
            bridge_client=bc)
        assert reply["ok"] is True
        assert bc.calls == [(0, "firefox", "playpause", 3)]

    def test_control_rejects_bad_action(self):
        class _BC(mpris._BridgeClient):
            def __init__(self):
                pass

            def control(self, *a):
                raise AssertionError("should not be called")

        reply = mpris.handle_control(
            "format_disk", uid=0, browser="firefox", tab_id=1,
            bridge_client=_BC())
        assert reply["ok"] is False
        assert reply["error"] == "bad_action"


# =====================================================================
# Downloads daemon
# =====================================================================

class _RecordingNotifier(downloads._BaseNotifier):
    def __init__(self, fail=False):
        self.calls = []
        self._fail = fail

    def notify(self, *, summary, body, download_id, path, uid):
        if self._fail:
            raise RuntimeError("notify boom")
        self.calls.append({"summary": summary, "body": body,
                           "download_id": download_id, "path": path,
                           "uid": uid})
        return 77


class TestDownloadsDaemon:
    def test_complete_raises_notification(self):
        notif = _RecordingNotifier()
        reply = downloads.handle_notify(
            {"download_id": 9, "filename": "/home/u/Downloads/a.zip",
             "state": "complete"},
            caller_uid=0, caller_pid=1, notifier=notif, bridge_gate=allow_gate)
        assert reply["ok"] is True
        assert reply["notified"] is True
        assert reply["notification_id"] == 77
        assert notif.calls[0]["download_id"] == 9

    def test_in_progress_no_ui(self):
        notif = _RecordingNotifier()
        reply = downloads.handle_notify(
            {"download_id": 9, "filename": "x", "state": "in_progress"},
            caller_uid=0, caller_pid=1, notifier=notif, bridge_gate=allow_gate)
        assert reply["ok"] is True
        assert reply["notified"] is False
        assert notif.calls == []

    def test_denied_non_bridge(self):
        notif = _RecordingNotifier()
        reply = downloads.handle_notify(
            {"download_id": 1, "filename": "x", "state": "complete"},
            caller_uid=0, caller_pid=1, notifier=notif, bridge_gate=deny_gate)
        assert reply["ok"] is False
        assert reply["error"] == "parent_not_allowed"
        assert notif.calls == []

    def test_bad_download_id(self):
        notif = _RecordingNotifier()
        reply = downloads.handle_notify(
            {"download_id": "not-int", "state": "complete"},
            caller_uid=0, caller_pid=1, notifier=notif, bridge_gate=allow_gate)
        assert reply["ok"] is False
        assert reply["error"] == "bad_download_id"

    def test_notify_sink_failure(self):
        notif = _RecordingNotifier(fail=True)
        reply = downloads.handle_notify(
            {"download_id": 1, "filename": "/home/u/Downloads/a",
             "state": "complete"},
            caller_uid=0, caller_pid=1, notifier=notif, bridge_gate=allow_gate)
        assert reply["ok"] is False
        assert reply["error"] == "notify_failed"

    def test_path_allowed_under_home(self):
        home = lambda _uid: "/home/bob"  # noqa: E731
        assert downloads.path_allowed_for_uid(
            "/home/bob/Downloads/x.zip", 1001, home_fn=home) is True

    def test_path_traversal_denied(self):
        home = lambda _uid: "/home/bob"  # noqa: E731
        assert downloads.path_allowed_for_uid(
            "/home/bob/../../etc/shadow", 1001, home_fn=home) is False

    def test_path_sibling_prefix_denied(self):
        # /home/bob-evil must not match the /home/bob anchor.
        home = lambda _uid: "/home/bob"  # noqa: E731
        assert downloads.path_allowed_for_uid(
            "/home/bob-evil/x", 1001, home_fn=home) is False

    def test_path_unknown_home_denied(self):
        home = lambda _uid: ""  # noqa: E731
        assert downloads.path_allowed_for_uid(
            "/home/bob/x", 1001, home_fn=home) is False

    def test_open_location_gated_by_path(self):
        calls = []
        reply = downloads.open_location(
            "/etc/shadow", 1001, 1001,
            runner=lambda argv, uid, gid: calls.append(argv) or 0,
            fm_resolver=lambda: "/usr/bin/pcmanfm",
            home_fn=lambda _uid: "/home/bob")
        assert reply["ok"] is False
        assert reply["error"] == "path_not_allowed"
        assert calls == []

    def test_open_location_runs_file_manager(self, tmp_path):
        # Build a real file under a fake home so realpath checks pass.
        home = tmp_path / "home" / "bob"
        dl = home / "Downloads"
        dl.mkdir(parents=True)
        f = dl / "a.zip"
        f.write_text("x")
        calls = []
        reply = downloads.open_location(
            str(f), 1001, 1001,
            runner=lambda argv, uid, gid: calls.append((argv, uid)) or 0,
            fm_resolver=lambda: "/usr/bin/pcmanfm",
            home_fn=lambda _uid: str(home))
        assert reply["ok"] is True
        assert reply["folder"] == str(dl)
        assert calls[0][0] == ["/usr/bin/pcmanfm", str(dl)]
        assert calls[0][1] == 1001

    def test_open_location_no_file_manager(self, tmp_path):
        home = tmp_path / "bob"
        home.mkdir()
        f = home / "a"
        f.write_text("x")
        reply = downloads.open_location(
            str(f), 1001, 1001, runner=lambda *a: 0,
            fm_resolver=lambda: "", home_fn=lambda _uid: str(home))
        assert reply["ok"] is False
        assert reply["error"] == "no_file_manager"

    def test_resolve_file_manager_prefers_first(self):
        fm = downloads.resolve_file_manager(
            ("/x/a", "/x/b"), exists=lambda p: p == "/x/b")
        assert fm == "/x/b"


# =====================================================================
# Notifications daemon
# =====================================================================

class _NotifSink(notifications._BaseNotifier):
    def __init__(self, fail=False):
        self.calls = []
        self._fail = fail

    def notify(self, *, summary, body, icon, origin, uid):
        if self._fail:
            raise RuntimeError("boom")
        self.calls.append({"summary": summary, "body": body,
                           "origin": origin, "uid": uid})
        return 55


class TestNotificationsDaemon:
    def test_default_allow(self):
        sink = _NotifSink()
        reply = notifications.handle_show(
            {"title": "Hi", "body": "there", "origin": "example.com"},
            caller_uid=0, caller_pid=1, notifier=sink,
            policy=notifications.NotificationPolicy([]), bridge_gate=allow_gate)
        assert reply["ok"] is True
        assert reply["decision"] == "default_allow"
        assert reply["notification_id"] == 55
        assert len(sink.calls) == 1

    def test_denied_non_bridge(self):
        sink = _NotifSink()
        reply = notifications.handle_show(
            {"title": "Hi", "origin": "example.com"},
            caller_uid=0, caller_pid=1, notifier=sink,
            policy=notifications.NotificationPolicy([]), bridge_gate=deny_gate)
        assert reply["ok"] is False
        assert reply["error"] == "parent_not_allowed"
        assert sink.calls == []

    def test_origin_rule_deny(self):
        policy = notifications.NotificationPolicy(
            [{"origin": "*.doubleclick.net", "decision": "deny"}])
        sink = _NotifSink()
        reply = notifications.handle_show(
            {"title": "Buy", "origin": "ads.doubleclick.net"},
            caller_uid=0, caller_pid=1, notifier=sink, policy=policy,
            bridge_gate=allow_gate)
        assert reply["ok"] is False
        assert reply["error"] == "policy_denied"
        assert reply["decision"] == "origin_rule"
        assert sink.calls == []

    def test_user_rule_outranks_default(self):
        # uid 0 -> "root"; a per-user deny matches.
        policy = notifications.NotificationPolicy(
            [{"user": "root", "origin": "noisy.example", "decision": "deny"}])
        sink = _NotifSink()
        reply = notifications.handle_show(
            {"title": "x", "origin": "noisy.example"},
            caller_uid=0, caller_pid=1, notifier=sink, policy=policy,
            bridge_gate=allow_gate)
        assert reply["ok"] is False
        assert reply["decision"] == "user_rule"

    def test_user_rule_does_not_affect_other_user(self):
        # Rule names user "kiosk"; caller is root -> falls through to allow.
        policy = notifications.NotificationPolicy(
            [{"user": "kiosk", "origin": "noisy.example", "decision": "deny"}])
        sink = _NotifSink()
        reply = notifications.handle_show(
            {"title": "x", "origin": "noisy.example"},
            caller_uid=0, caller_pid=1, notifier=sink, policy=policy,
            bridge_gate=allow_gate)
        assert reply["ok"] is True

    def test_empty_notification_rejected(self):
        sink = _NotifSink()
        reply = notifications.handle_show(
            {"title": "", "body": "", "origin": "x"},
            caller_uid=0, caller_pid=1, notifier=sink,
            policy=notifications.NotificationPolicy([]), bridge_gate=allow_gate)
        assert reply["ok"] is False
        assert reply["error"] == "empty_notification"

    def test_title_body_bounded(self):
        sink = _NotifSink()
        notifications.handle_show(
            {"title": "T" * 5000, "body": "B" * 5000, "origin": "x"},
            caller_uid=0, caller_pid=1, notifier=sink,
            policy=notifications.NotificationPolicy([]), bridge_gate=allow_gate)
        assert len(sink.calls[0]["summary"]) <= notifications._MAX_TITLE
        assert len(sink.calls[0]["body"]) <= notifications._MAX_BODY

    def test_policy_load_missing_file_is_allow(self, tmp_path):
        policy = notifications.NotificationPolicy.load(
            str(tmp_path / "nope.json"))
        allowed, reason = policy.decide("anyuser", "any.origin")
        assert allowed is True
        assert reason == "default_allow"

    def test_policy_load_from_file(self, tmp_path):
        import json as _json
        p = tmp_path / "policy.json"
        p.write_text(_json.dumps(
            {"rules": [{"origin": "bad.example", "decision": "deny"}]}))
        policy = notifications.NotificationPolicy.load(str(p))
        assert policy.decide("u", "bad.example")[0] is False

    def test_policy_malformed_file_fails_open(self, tmp_path):
        p = tmp_path / "policy.json"
        p.write_text("{not json")
        policy = notifications.NotificationPolicy.load(str(p))
        assert policy.decide("u", "x")[0] is True

    def test_origin_wildcard_matches_apex(self):
        assert notifications._origin_matches(
            "*.doubleclick.net", "doubleclick.net") is True
        assert notifications._origin_matches(
            "*.doubleclick.net", "a.doubleclick.net") is True
        assert notifications._origin_matches(
            "*.doubleclick.net", "evildoubleclick.net") is False


# =====================================================================
# Compositor daemon
# =====================================================================

class _RecordingInhibitor(compositor._BaseInhibitor):
    def __init__(self, fail_acquire=False, fail_release=False):
        self.acquired = []
        self.released = []
        self._next = 0
        self._fa = fail_acquire
        self._fr = fail_release

    def acquire(self, *, uid, reason, who):
        if self._fa:
            raise RuntimeError("no logind")
        self._next += 1
        self.acquired.append((uid, reason, who, self._next))
        return self._next

    def release(self, handle):
        if self._fr:
            raise RuntimeError("release boom")
        self.released.append(handle)


class TestCompositorDaemon:
    def test_inhibit_happy_path(self):
        inh = _RecordingInhibitor()
        st = compositor.InhibitState()
        reply = compositor.handle_inhibit(
            {"reason": "fullscreen_video", "tab_id": 3},
            caller_uid=0, caller_pid=1, inhibitor=inh, state=st,
            bridge_gate=allow_gate)
        assert reply["ok"] is True
        assert reply["inhibited"] is True
        assert reply["refreshed"] is False
        assert len(inh.acquired) == 1

    def test_inhibit_denied_non_bridge(self):
        inh = _RecordingInhibitor()
        st = compositor.InhibitState()
        reply = compositor.handle_inhibit(
            {"reason": "fullscreen_video", "tab_id": 3},
            caller_uid=0, caller_pid=1, inhibitor=inh, state=st,
            bridge_gate=deny_gate)
        assert reply["ok"] is False
        assert reply["error"] == "parent_not_allowed"
        assert inh.acquired == []

    def test_inhibit_policy_denies_bad_reason(self):
        inh = _RecordingInhibitor()
        st = compositor.InhibitState()
        reply = compositor.handle_inhibit(
            {"reason": "mine_bitcoin", "tab_id": 3},
            caller_uid=0, caller_pid=1, inhibitor=inh, state=st,
            bridge_gate=allow_gate)
        assert reply["ok"] is False
        assert reply["error"] == "policy_denied"
        assert inh.acquired == []

    def test_inhibit_idempotent_refresh(self):
        inh = _RecordingInhibitor()
        st = compositor.InhibitState()
        args = dict(caller_uid=0, caller_pid=1, inhibitor=inh, state=st,
                    bridge_gate=allow_gate)
        compositor.handle_inhibit(
            {"reason": "presentation", "tab_id": 3}, **args)
        reply2 = compositor.handle_inhibit(
            {"reason": "presentation", "tab_id": 3}, **args)
        assert reply2["refreshed"] is True
        # Only one real inhibitor was ever acquired.
        assert len(inh.acquired) == 1

    def test_inhibit_cap(self, monkeypatch):
        monkeypatch.setattr(compositor, "MAX_INHIBITS_PER_UID", 2)
        inh = _RecordingInhibitor()
        st = compositor.InhibitState()
        args = dict(caller_uid=0, caller_pid=1, inhibitor=inh, state=st,
                    bridge_gate=allow_gate)
        for tab in (1, 2):
            assert compositor.handle_inhibit(
                {"reason": "presentation", "tab_id": tab}, **args)["ok"]
        capped = compositor.handle_inhibit(
            {"reason": "presentation", "tab_id": 3}, **args)
        assert capped["ok"] is False
        assert capped["error"] == "inhibit_cap_exceeded"

    def test_inhibit_acquire_failure(self):
        inh = _RecordingInhibitor(fail_acquire=True)
        st = compositor.InhibitState()
        reply = compositor.handle_inhibit(
            {"reason": "presentation", "tab_id": 1},
            caller_uid=0, caller_pid=1, inhibitor=inh, state=st,
            bridge_gate=allow_gate)
        assert reply["ok"] is False
        assert reply["error"] == "inhibit_failed"

    def test_release_drops_inhibitor(self):
        inh = _RecordingInhibitor()
        st = compositor.InhibitState()
        compositor.handle_inhibit(
            {"reason": "presentation", "tab_id": 7},
            caller_uid=0, caller_pid=1, inhibitor=inh, state=st,
            bridge_gate=allow_gate)
        reply = compositor.handle_release(
            {"tab_id": 7}, caller_uid=0, caller_pid=1, inhibitor=inh,
            state=st, bridge_gate=allow_gate)
        assert reply["ok"] is True
        assert reply["released"] is True
        assert inh.released == [1]

    def test_release_no_op_when_none_held(self):
        inh = _RecordingInhibitor()
        st = compositor.InhibitState()
        reply = compositor.handle_release(
            {"tab_id": 99}, caller_uid=0, caller_pid=1, inhibitor=inh,
            state=st, bridge_gate=allow_gate)
        assert reply["ok"] is True
        assert reply["released"] is False

    def test_release_denied_non_bridge(self):
        inh = _RecordingInhibitor()
        st = compositor.InhibitState()
        reply = compositor.handle_release(
            {"tab_id": 1}, caller_uid=0, caller_pid=1, inhibitor=inh,
            state=st, bridge_gate=deny_gate)
        assert reply["ok"] is False
        assert reply["error"] == "parent_not_allowed"

    def test_cross_user_cannot_release(self):
        # User A (uid 0) holds an inhibitor on tab 5; user B (uid 1234)
        # releasing tab 5 must NOT drop A's inhibitor — the (uid, tab)
        # key is scoped by the kernel-attested uid.
        inh = _RecordingInhibitor()
        st = compositor.InhibitState()
        compositor.handle_inhibit(
            {"reason": "presentation", "tab_id": 5},
            caller_uid=0, caller_pid=1, inhibitor=inh, state=st,
            bridge_gate=allow_gate)
        reply = compositor.handle_release(
            {"tab_id": 5}, caller_uid=1234, caller_pid=2, inhibitor=inh,
            state=st, bridge_gate=allow_gate)
        assert reply["released"] is False
        assert inh.released == []  # A's inhibitor untouched
        # A can still release their own.
        own = compositor.handle_release(
            {"tab_id": 5}, caller_uid=0, caller_pid=1, inhibitor=inh,
            state=st, bridge_gate=allow_gate)
        assert own["released"] is True

    def test_state_sweep_reclaims_stale(self):
        st = compositor.InhibitState()
        st.put("0:1", handle=10, uid=0, now=1000.0)
        st.put("0:2", handle=20, uid=0, now=2000.0)
        reclaimed = st.sweep(now=2050.0, ttl_s=100.0)
        # Only the 1000.0 entry is older than ttl.
        assert len(reclaimed) == 1
        assert reclaimed[0]["handle"] == 10
        assert st.get("0:2") is not None
