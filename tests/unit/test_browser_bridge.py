"""Tests for browser_bridge — task(113)/spec/14 Phase-8 MVP.

The browser-bridge module is pure-python so every branch is
drivable in-process. Tests inject:
- BytesIO for stdin/stdout to drive the length-prefix codec.
- Stubs for the four parent-chain probes.
- A custom dispatch table for op-routing edge cases.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import struct
import sys
from pathlib import Path

_MOD = (Path(__file__).resolve().parent.parent.parent
        / "browser_bridge" / "qdistro_browser_bridge.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_bridge", _MOD)
bb = importlib.util.module_from_spec(spec)
sys.modules["qdistro_browser_bridge"] = bb
spec.loader.exec_module(bb)


# ---- length-prefix framing ----

class TestFraming:
    def test_roundtrip(self):
        buf = io.BytesIO()
        bb.write_message(buf, {"op": "qdistro.ping", "n": 42})
        buf.seek(0)
        msg = bb.read_message(buf)
        assert msg == {"op": "qdistro.ping", "n": 42}

    def test_eof_returns_none(self):
        assert bb.read_message(io.BytesIO()) is None

    def test_short_length_prefix_raises(self):
        try:
            bb.read_message(io.BytesIO(b"\x01\x02"))
        except ValueError as e:
            assert "short length prefix" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_too_large_length_raises(self):
        # 5 MiB request — over the 4 MiB cap.
        big = struct.pack("<I", 5 * 1024 * 1024)
        try:
            bb.read_message(io.BytesIO(big))
        except ValueError as e:
            assert "too large" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_short_body_raises(self):
        # Length says 100, only 5 bytes of body present.
        prefix = struct.pack("<I", 100)
        try:
            bb.read_message(io.BytesIO(prefix + b"hello"))
        except ValueError as e:
            assert "short body" in str(e)
        else:
            raise AssertionError("expected ValueError")

    @staticmethod
    def _framed(raw: bytes) -> io.BytesIO:
        """Length-prefix an arbitrary (possibly non-JSON-object) body."""
        return io.BytesIO(struct.pack("<I", len(raw)) + raw)

    def test_nonobject_scalar_raises(self):
        # A bare JSON number is valid JSON but not a dict — the wire
        # contract is object-shaped, so it must fail closed.
        for body in (b"42", b"3.14", b"true", b"false", b'"hi"'):
            try:
                bb.read_message(self._framed(body))
            except ValueError as e:
                assert "not an object" in str(e), body
            else:
                raise AssertionError(f"expected ValueError for {body!r}")

    def test_nonobject_array_raises(self):
        try:
            bb.read_message(self._framed(b"[1, 2, 3]"))
        except ValueError as e:
            assert "not an object" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_nonobject_null_raises(self):
        # JSON null parses to Python None — must not be mistaken for EOF
        # (which is signalled by a missing length prefix, not a frame).
        try:
            bb.read_message(self._framed(b"null"))
        except ValueError as e:
            assert "not an object" in str(e)
        else:
            raise AssertionError("expected ValueError")

    def test_malformed_json_raises_valueerror(self):
        # json.JSONDecodeError is a ValueError subclass, so a corrupt
        # body surfaces on the same fail-closed path as the type guard.
        try:
            bb.read_message(self._framed(b"{not json"))
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_object_still_roundtrips(self):
        # The guard must not reject legitimate object frames.
        msg = bb.read_message(self._framed(b'{"op": "qdistro.ping"}'))
        assert msg == {"op": "qdistro.ping"}


# ---- parent-chain identity ----

class TestVerifyParent:
    def test_allowed_firefox(self):
        ident = bb.verify_parent(
            ppid_fn=lambda: 4242,
            exe_reader=lambda _p: "/usr/lib64/firefox/firefox",
            selinux_reader=lambda _p: "user_u:user_r:user_t:s0",
            argv=["/usr/lib/qdistro/browser-bridge",
                  "/path/to/manifest.json",
                  "qdistro@qdistro.local"],
        )
        assert ident["allowed"] is True
        assert ident["ppid"] == 4242
        assert ident["parent_exe"] == "/usr/lib64/firefox/firefox"
        assert ident["parent_selinux"].startswith("user_u")
        assert ident["extension_id"] == "qdistro@qdistro.local"

    def test_allowed_chromium(self):
        # Valid Chrome extension id = 32 lowercase a-p chars (P04
        # fix-pass S4: parse_extension_id_from_argv tightened).
        valid_id = "a" * 32
        ident = bb.verify_parent(
            ppid_fn=lambda: 1,
            exe_reader=lambda _p: "/usr/bin/chromium",
            selinux_reader=lambda _p: "",
            argv=["/usr/lib/qdistro/browser-bridge",
                  f"chrome-extension://{valid_id}/"],
        )
        assert ident["allowed"] is True
        assert ident["extension_id"] == valid_id

    def test_denied_unknown_parent(self):
        ident = bb.verify_parent(
            ppid_fn=lambda: 999,
            exe_reader=lambda _p: "/usr/local/bin/curl",
            selinux_reader=lambda _p: "",
            argv=[],
        )
        assert ident["allowed"] is False

    def test_denied_snap_firefox(self):
        # The Snap Firefox case from spec/14 §"Supported-browser
        # matrix" — parent ends up being xdg-desktop-portal, not
        # firefox itself.
        ident = bb.verify_parent(
            ppid_fn=lambda: 1234,
            exe_reader=lambda _p: "/usr/libexec/xdg-desktop-portal",
            selinux_reader=lambda _p: "",
            argv=[],
        )
        assert ident["allowed"] is False

    def test_denied_empty_exe(self):
        ident = bb.verify_parent(
            ppid_fn=lambda: 1,
            exe_reader=lambda _p: "",
            selinux_reader=lambda _p: "",
            argv=[],
        )
        assert ident["allowed"] is False

    def test_explicit_allowlist_override(self):
        ident = bb.verify_parent(
            ppid_fn=lambda: 1,
            exe_reader=lambda _p: "/opt/qdistro/test-firefox",
            selinux_reader=lambda _p: "",
            allowlist=("/opt/qdistro/test-firefox",),
            argv=[],
        )
        assert ident["allowed"] is True


# ---- env-var allowlist bypass closed (P0-2) ----

class TestAllowlistEnvBypass:
    def test_legacy_env_var_rejected(self, monkeypatch):
        # Pre-P0-2 the bridge honored this and let any same-uid
        # process replace the parent-exe allowlist. Now it must hard
        # error so regressions are loud.
        monkeypatch.setenv(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST", "/bin/sh")
        try:
            bb._resolve_allowlist()
        except RuntimeError as e:
            assert "rejected" in str(e).lower()
        else:
            raise AssertionError(
                "legacy env var must raise RuntimeError")

    def test_test_env_var_requires_test_mode(self, monkeypatch):
        monkeypatch.delenv(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST", raising=False)
        monkeypatch.setenv(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST", "/bin/sh")
        monkeypatch.delenv("QDISTRO_TEST_MODE", raising=False)
        try:
            bb._resolve_allowlist()
        except RuntimeError as e:
            assert "QDISTRO_TEST_MODE" in str(e)
        else:
            raise AssertionError(
                "test env var without QDISTRO_TEST_MODE must raise")

    def test_test_env_var_honored_under_test_mode(self, monkeypatch):
        monkeypatch.delenv(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST", raising=False)
        monkeypatch.setenv("QDISTRO_TEST_MODE", "1")
        monkeypatch.setenv(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST",
            "/opt/test-bin:/opt/other-bin")
        out = bb._resolve_allowlist()
        assert out == ("/opt/test-bin", "/opt/other-bin")

    def test_default_allowlist_when_unset(self, monkeypatch, tmp_path):
        # P0-4: with no opt-in config the effective allowlist is the
        # Firefox+Chromium baseline only — the optional browsers
        # (chrome/brave/vivaldi/edge) are NOT trusted parents by default.
        monkeypatch.delenv(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST", raising=False)
        monkeypatch.delenv(
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST", raising=False)
        out = bb._resolve_allowlist(
            config_path=str(tmp_path / "absent.conf"))
        assert out == bb.DEFAULT_ALLOWED_PARENT_EXES


# ---- P0-4: optional-browser allowlist is admin opt-in -----------------

class TestOptionalBrowserOptIn:
    """The Brave/Vivaldi/Chrome/Edge parents are default-OFF; an admin
    opts each one in via a root-owned config file. Mirrors the F4
    firefox-containers opt-in: a trust-widening capability stays off
    until an admin authors a root-owned policy artifact.
    """

    OPTIONAL_EXES = (
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/brave", "/usr/bin/brave-browser",
        "/usr/bin/vivaldi", "/usr/bin/vivaldi-stable",
        "/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable",
    )

    def _write(self, tmp_path, text):
        cfg = tmp_path / "browser-bridge-allowlist.conf"
        cfg.write_text(text, encoding="utf-8")
        cfg.chmod(0o644)
        return cfg

    def test_optional_browsers_denied_by_default(self):
        # Baseline contains Firefox + Chromium, never the optionals.
        for exe in self.OPTIONAL_EXES:
            assert exe not in bb.DEFAULT_ALLOWED_PARENT_EXES
        assert "/usr/lib64/firefox/firefox" in bb.DEFAULT_ALLOWED_PARENT_EXES
        assert "/usr/bin/chromium" in bb.DEFAULT_ALLOWED_PARENT_EXES

    def test_optin_adds_only_named_browser(self, tmp_path):
        cfg = self._write(tmp_path, "brave\n")
        # Honor the file as if root-owned by binding trusted_uid to us.
        out = bb._resolve_allowlist(
            config_path=str(cfg), trusted_uid=os.geteuid())
        assert "/usr/bin/brave" in out
        assert "/usr/bin/brave-browser" in out
        # Chrome/Vivaldi/Edge stay denied — opt-in is per-browser.
        assert "/usr/bin/google-chrome" not in out
        assert "/usr/bin/vivaldi" not in out
        assert "/usr/bin/microsoft-edge" not in out
        # Baseline still present.
        assert "/usr/lib64/firefox/firefox" in out

    def test_optin_multiple_with_comments_and_blanks(self, tmp_path):
        cfg = self._write(
            tmp_path,
            "# optional browsers this admin trusts\n"
            "chrome\n"
            "\n"
            "  EDGE   # case-insensitive, trailing comment\n")
        out = bb._resolve_allowlist(
            config_path=str(cfg), trusted_uid=os.geteuid())
        assert "/usr/bin/google-chrome" in out
        assert "/usr/bin/microsoft-edge-stable" in out
        assert "/usr/bin/brave" not in out

    def test_unknown_key_ignored(self, tmp_path):
        cfg = self._write(tmp_path, "brave\nnetscape\n")
        out = bb._resolve_allowlist(
            config_path=str(cfg), trusted_uid=os.geteuid())
        assert "/usr/bin/brave" in out
        # No crash, no spurious entries from the unknown key.
        assert out == bb.DEFAULT_ALLOWED_PARENT_EXES + (
            "/usr/bin/brave", "/usr/bin/brave-browser")

    def test_wrong_owner_rejected_failclosed(self, tmp_path):
        # The crux: a config NOT owned by the trusted uid is ignored, so
        # the bridge's own (unprivileged) uid cannot widen its trust
        # boundary by writing this file. trusted_uid=0 while the test
        # file is owned by us models "user-written, must be ignored".
        cfg = self._write(tmp_path, "brave\nchrome\nvivaldi\nedge\n")
        assert os.stat(cfg).st_uid != 0  # test sanity
        out = bb._resolve_allowlist(config_path=str(cfg), trusted_uid=0)
        assert out == bb.DEFAULT_ALLOWED_PARENT_EXES

    def test_group_or_other_writable_rejected(self, tmp_path):
        cfg = self._write(tmp_path, "brave\n")
        os.chmod(cfg, 0o664)  # group-writable
        out = bb._resolve_allowlist(
            config_path=str(cfg), trusted_uid=os.geteuid())
        assert out == bb.DEFAULT_ALLOWED_PARENT_EXES

    def test_symlink_rejected(self, tmp_path):
        real = self._write(tmp_path, "brave\n")
        link = tmp_path / "link.conf"
        link.symlink_to(real)
        out = bb._resolve_allowlist(
            config_path=str(link), trusted_uid=os.geteuid())
        assert out == bb.DEFAULT_ALLOWED_PARENT_EXES

    def test_fifo_rejected_and_does_not_block(self, tmp_path):
        # A non-regular file (here a FIFO) with otherwise-acceptable
        # owner+mode must be rejected by the regular-file check — and the
        # O_NONBLOCK open must NOT hang the bridge on a reader-less FIFO.
        fifo = tmp_path / "browser-bridge-allowlist.conf"
        os.mkfifo(fifo, 0o644)
        out = bb._resolve_allowlist(
            config_path=str(fifo), trusted_uid=os.geteuid())
        assert out == bb.DEFAULT_ALLOWED_PARENT_EXES

    def test_malformed_utf8_failclosed(self, tmp_path):
        # A decode error must fail closed to the baseline, never raise.
        cfg = tmp_path / "browser-bridge-allowlist.conf"
        cfg.write_bytes(b"brave\xff\nchrome\n")
        cfg.chmod(0o644)
        out = bb._resolve_allowlist(
            config_path=str(cfg), trusted_uid=os.geteuid())
        assert out == bb.DEFAULT_ALLOWED_PARENT_EXES

    def test_absent_config_is_baseline(self, tmp_path):
        out = bb._resolve_allowlist(
            config_path=str(tmp_path / "nope.conf"),
            trusted_uid=os.geteuid())
        assert out == bb.DEFAULT_ALLOWED_PARENT_EXES

    def test_opted_in_browser_passes_verify_parent(self, tmp_path):
        # End-to-end: an opted-in Brave parent is `allowed`; a
        # not-opted-in Vivaldi parent is not.
        cfg = self._write(tmp_path, "brave\n")
        allow = bb._resolve_allowlist(
            config_path=str(cfg), trusted_uid=os.geteuid())
        ident_ok = bb.verify_parent(
            ppid_fn=lambda: 4242,
            exe_reader=lambda _p: "/usr/bin/brave",
            selinux_reader=lambda _p: "",
            allowlist=allow, argv=[])
        assert ident_ok["allowed"] is True
        ident_deny = bb.verify_parent(
            ppid_fn=lambda: 4242,
            exe_reader=lambda _p: "/usr/bin/vivaldi",
            selinux_reader=lambda _p: "",
            allowlist=allow, argv=[])
        assert ident_deny["allowed"] is False


# ---- argv-derived extension identity (P0-1) ----

class TestParseExtensionIdFromArgv:
    """Per P04 fix-pass S4, the parser enforces the format gate:

    - Chrome / Chromium-family: exactly 32 lowercase a-p chars.
    - Firefox: ``{UUID-IN-BRACES}`` or ``name@host`` syntax.

    Anything else returns "" rather than echoing back arbitrary
    content. Future code that interpolates the id into a path / URL
    inherits the strict grammar.
    """

    _VALID_CHROME = "a" * 32

    def test_chrome_origin(self):
        eid = bb.parse_extension_id_from_argv(
            ["/usr/lib/qdistro/browser-bridge",
             f"chrome-extension://{self._VALID_CHROME}/"],
            parent_exe="/usr/bin/chromium")
        assert eid == self._VALID_CHROME

    def test_chrome_origin_chromium_browser(self):
        eid = bb.parse_extension_id_from_argv(
            ["bridge", f"chrome-extension://{self._VALID_CHROME}/"],
            parent_exe="/usr/bin/chromium-browser")
        assert eid == self._VALID_CHROME

    def test_chrome_origin_google_chrome(self):
        # Real Chrome IDs are 32 a-p chars; non-conforming ids are
        # rejected (S4 hardening).
        eid = bb.parse_extension_id_from_argv(
            ["bridge", "chrome-extension://google-id/"],
            parent_exe="/usr/bin/google-chrome")
        assert eid == ""

    def test_chrome_origin_path_escape_rejected(self):
        # Hardening: pre-S4, this returned "aaa/../bbb"; now empty.
        eid = bb.parse_extension_id_from_argv(
            ["bridge",
             "chrome-extension://aaa/../bbb/"],
            parent_exe="/usr/bin/chromium")
        assert eid == ""

    def test_firefox_argv2_name_at_host(self):
        eid = bb.parse_extension_id_from_argv(
            ["bridge", "/path/to/manifest.json",
             "qdistro@qdistro.local"],
            parent_exe="/usr/lib64/firefox/firefox")
        assert eid == "qdistro@qdistro.local"

    def test_firefox_argv2_uuid_in_braces(self):
        eid = bb.parse_extension_id_from_argv(
            ["bridge", "/path/to/manifest.json",
             "{12345678-1234-1234-1234-1234567890ab}"],
            parent_exe="/usr/lib64/firefox/firefox")
        assert eid == "{12345678-1234-1234-1234-1234567890ab}"

    def test_firefox_argv2_garbage_rejected(self):
        eid = bb.parse_extension_id_from_argv(
            ["bridge", "/path/to/manifest.json",
             "not-an-id-at-all"],
            parent_exe="/usr/lib64/firefox/firefox")
        assert eid == ""

    def test_firefox_missing_argv2_empty(self):
        # Firefox bridge with no extension ID in argv (shouldn't happen
        # post-Firefox 55, but the bridge mustn't crash).
        eid = bb.parse_extension_id_from_argv(
            ["bridge", "/path/to/manifest.json"],
            parent_exe="/usr/lib64/firefox/firefox")
        assert eid == ""

    def test_unrecognised_argv_empty(self):
        # No origin scheme = not a real browser launch. The bridge
        # treats this as "unknown extension," not "trusted."
        eid = bb.parse_extension_id_from_argv(
            ["bridge", "junk"],
            parent_exe="/usr/bin/chromium")
        assert eid == ""

    def test_empty_argv(self):
        assert bb.parse_extension_id_from_argv([], "") == ""
        assert bb.parse_extension_id_from_argv(None, "") == ""

    def test_stdio_extension_id_is_ignored(self, monkeypatch):
        # P0-1 regression: even if the extension supplies an
        # extension_id over stdio, dispatch returns the argv-derived
        # value (or empty).
        identity = {
            "ppid": 1, "parent_exe": "/usr/lib64/firefox/firefox",
            "parent_selinux": "", "allowed": True,
            "extension_id": "real-id-from-argv",
        }
        resp = bb.dispatch(
            {"op": "qdistro.ping", "extension_id": "spoofed-by-ext"},
            identity)
        assert resp["extension_id"] == "real-id-from-argv"


# ---- dispatch ----

class TestDispatch:
    _ALLOWED = {
        "ppid": 100, "parent_exe": "/usr/lib64/firefox/firefox",
        "parent_selinux": "user_u:user_r:user_t:s0", "allowed": True}
    _DENIED = {**_ALLOWED, "allowed": False}

    def test_ping_handler(self):
        identity = {**self._ALLOWED,
                    "extension_id": "qdistro@qdistro.local"}
        resp = bb.dispatch(
            {"op": "qdistro.ping", "echo": "hello",
             # Stdio extension_id is ignored — bridge trusts only argv.
             "extension_id": "ignored-spoof"},
            identity)
        assert resp["ok"] is True
        assert resp["op"] == "qdistro.ping"
        assert resp["pong"] is True
        assert resp["echo"] == "hello"
        assert resp["parent_exe"] == "/usr/lib64/firefox/firefox"
        assert resp["extension_id"] == "qdistro@qdistro.local"

    def test_denied_parent_short_circuits(self):
        resp = bb.dispatch(
            {"op": "qdistro.ping"}, self._DENIED)
        assert resp["ok"] is False
        assert resp["error"] == "parent_not_allowed"

    def test_unknown_op(self):
        resp = bb.dispatch({"op": "qdistro.fake"}, self._ALLOWED)
        assert resp["ok"] is False
        assert resp["error"] == "unknown_op"
        assert resp["op"] == "qdistro.fake"

    def test_missing_op(self):
        resp = bb.dispatch({}, self._ALLOWED)
        assert resp["ok"] is False
        assert resp["error"] == "missing_op"

    def test_handler_raises_caught(self):
        def boom(_msg, _id):
            raise RuntimeError("nope")
        resp = bb.dispatch(
            {"op": "x"}, self._ALLOWED,
            handlers={"x": boom})
        assert resp["ok"] is False
        assert resp["error"] == "handler_raised"
        assert resp["op"] == "x"
        assert "nope" in resp["detail"]

    def test_custom_handler(self):
        def echo(msg, _id):
            return {"got": msg.get("payload")}
        resp = bb.dispatch(
            {"op": "myop", "payload": [1, 2, 3]},
            self._ALLOWED,
            handlers={"myop": echo})
        assert resp["ok"] is True
        assert resp["got"] == [1, 2, 3]
        assert resp["op"] == "myop"


# ---- main loop ----

class TestMainLoop:
    def _stdio_with(self, msgs):
        """Build a BytesIO stdin with N length-prefixed messages.
        Returns (stdin, stdout)."""
        stdin = io.BytesIO()
        for m in msgs:
            body = json.dumps(m).encode()
            stdin.write(struct.pack("<I", len(body)))
            stdin.write(body)
        stdin.seek(0)
        return stdin, io.BytesIO()

    def _read_responses(self, stdout):
        stdout.seek(0)
        out = []
        while True:
            msg = bb.read_message(stdout)
            if msg is None:
                break
            out.append(msg)
        return out

    def test_roundtrip_one_message(self, monkeypatch):
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1,
                     "parent_exe": "/usr/lib64/firefox/firefox",
                     "parent_selinux": "", "allowed": True})
        stdin, stdout = self._stdio_with(
            [{"op": "qdistro.ping", "echo": "ok"}])
        rc = bb.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        responses = self._read_responses(stdout)
        assert len(responses) == 1
        assert responses[0]["pong"] is True
        assert responses[0]["echo"] == "ok"

    def test_two_messages(self, monkeypatch):
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1,
                     "parent_exe": "/usr/bin/chromium",
                     "parent_selinux": "", "allowed": True})
        stdin, stdout = self._stdio_with([
            {"op": "qdistro.ping", "echo": "first"},
            {"op": "qdistro.ping", "echo": "second"},
        ])
        rc = bb.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        responses = self._read_responses(stdout)
        assert len(responses) == 2
        assert [r["echo"] for r in responses] == ["first", "second"]

    def test_denied_parent_main(self, monkeypatch):
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1, "parent_exe": "/bin/sh",
                     "parent_selinux": "", "allowed": False})
        stdin, stdout = self._stdio_with(
            [{"op": "qdistro.ping"}])
        rc = bb.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        responses = self._read_responses(stdout)
        assert responses[0]["error"] == "parent_not_allowed"

    def test_frame_error_returns_2(self, monkeypatch):
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1,
                     "parent_exe": "/usr/lib64/firefox/firefox",
                     "parent_selinux": "", "allowed": True})
        # Truncated frame: claims 100-byte body, supplies 3.
        stdin = io.BytesIO(struct.pack("<I", 100) + b"abc")
        stdout = io.BytesIO()
        rc = bb.main(stdin=stdin, stdout=stdout)
        assert rc == 2

    def test_nonobject_frame_does_not_crash_loop(self, monkeypatch):
        # A well-framed but non-object JSON value (the only thing the
        # extension itself can inject) must elicit a frame_error reply
        # and a clean exit(2) — not an uncaught AttributeError from
        # deliver_reply/dispatch calling .get on a list/int.
        monkeypatch.setattr(
            bb, "verify_parent",
            lambda: {"ppid": 1,
                     "parent_exe": "/usr/lib64/firefox/firefox",
                     "parent_selinux": "", "allowed": True})
        for body in (b"[1, 2, 3]", b"42", b"null", b'"oops"'):
            stdin = io.BytesIO(struct.pack("<I", len(body)) + body)
            stdout = io.BytesIO()
            rc = bb.main(stdin=stdin, stdout=stdout)
            assert rc == bb.EXIT_FRAME_ERROR, body
            stdout.seek(0)
            reply = bb.read_message(stdout)
            assert reply["ok"] is False, body
            assert reply["error"] == "frame_error", body
            assert "not an object" in reply["detail"], body


# ---- subprocess end-to-end (proves argv + env-var fixes work in a
#      real process, not just through fakes). ----

class TestSubprocessEndToEnd:
    """Spawn the bridge as a real subprocess and round-trip a ping.

    This exercises everything fakes can't: the real ``execve`` argv
    handoff, ``readlink(/proc/<ppid>/exe)`` against the running
    python3, and the env-var gate on the test-mode allowlist.
    """

    def _resolved_python_exe(self):
        # The bridge sees parent_exe via readlink, which chases the
        # python3 → python3.13 symlink. Match the same form.
        return os.readlink("/proc/self/exe")

    def _spawn_bridge(self, extra_argv, env_extra):
        import subprocess
        env = dict(os.environ)
        env.pop("QDISTRO_BROWSER_BRIDGE_ALLOWLIST", None)
        env.update(env_extra)
        return subprocess.Popen(
            [sys.executable, str(_MOD), *extra_argv],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)

    def _ping(self, proc, payload):
        body = json.dumps(payload).encode("utf-8")
        proc.stdin.write(struct.pack("<I", len(body)) + body)
        proc.stdin.flush()
        raw = proc.stdout.read(4)
        assert len(raw) == 4, "no length prefix in reply"
        (n,) = struct.unpack("<I", raw)
        resp = proc.stdout.read(n)
        proc.stdin.close()
        proc.wait(timeout=5)
        return json.loads(resp.decode())

    def test_chrome_argv_extension_id_round_trip(self):
        env = {
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST":
                self._resolved_python_exe(),
            "QDISTRO_TEST_MODE": "1",
        }
        # Valid Chrome id = 32 lowercase a-p chars (S4 hardening).
        chrome_id = "abcdefghijklmnopabcdefghijklmnop"
        proc = self._spawn_bridge(
            [f"chrome-extension://{chrome_id}/"], env)
        body = self._ping(proc,
                          {"op": "qdistro.ping", "echo": "e2e",
                           # Ignored — bridge trusts argv only.
                           "extension_id": "spoof-attempt"})
        assert body["pong"] is True
        assert body["echo"] == "e2e"
        assert body["extension_id"] == chrome_id

    def test_firefox_argv_extension_id_round_trip(self):
        env = {
            "QDISTRO_BROWSER_BRIDGE_ALLOWLIST_TEST":
                self._resolved_python_exe(),
            "QDISTRO_TEST_MODE": "1",
        }
        # We can't pretend to be Firefox by parent_exe (python3 is the
        # real parent), so argv parsing falls into the Chrome branch.
        # Use a valid 32-char a-p id (S4).
        chrome_id = "ponmlkjihgfedcbaponmlkjihgfedcba"
        proc = self._spawn_bridge(
            [f"chrome-extension://{chrome_id}/"], env)
        body = self._ping(proc, {"op": "qdistro.ping"})
        assert body["extension_id"] == chrome_id

    def test_legacy_env_var_aborts_subprocess(self):
        # If anything in production accidentally sets the old name,
        # the bridge must fail loudly — not silently accept the
        # override. We expect the subprocess to die before producing
        # any framed reply.
        import subprocess
        env = dict(os.environ)
        env["QDISTRO_BROWSER_BRIDGE_ALLOWLIST"] = "/bin/sh"
        proc = subprocess.Popen(
            [sys.executable, str(_MOD)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env)
        proc.stdin.close()
        proc.wait(timeout=5)
        # Non-zero exit and a clear stderr message.
        assert proc.returncode != 0
        assert b"rejected" in proc.stderr.read().lower()
