"""Tests for browser_bridge/qdistro_browser_install.py.

Pure-python install-tool helpers. Tests use tmp_path for the
target home dir so no real filesystem state is touched. Each
manifest's JSON shape is asserted byte-exactly per spec/14
§"Per-user, per-browser installation".
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MOD = (Path(__file__).resolve().parent.parent.parent
        / "browser_bridge" / "qdistro_browser_install.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_install", _MOD)
bi = importlib.util.module_from_spec(spec)
sys.modules["qdistro_browser_install"] = bi
spec.loader.exec_module(bi)


# ---- manifest rendering ------------------------------------------

class TestManifestRendering:
    def test_firefox_shape(self):
        body = bi.render_firefox_manifest(
            "/usr/lib/qdistro/browser-bridge", "qdistro@qdistro.local")
        assert body["name"] == "qdistro"
        assert body["type"] == "stdio"
        assert body["path"] == "/usr/lib/qdistro/browser-bridge"
        assert body["allowed_extensions"] == ["qdistro@qdistro.local"]
        assert "allowed_origins" not in body

    def test_chromium_shape(self):
        body = bi.render_chromium_manifest(
            "/usr/lib/qdistro/browser-bridge",
            "qdistroqdistroqdistroqdistroaaaaaaaa")
        assert body["name"] == "qdistro"
        assert body["allowed_origins"] == [
            "chrome-extension://qdistroqdistroqdistroqdistroaaaaaaaa/"
        ]
        assert "allowed_extensions" not in body

    def test_render_dispatch_firefox(self):
        body = bi.render_manifest(
            "firefox",
            bridge_path="/x/bridge",
            firefox_extension_id="ff@x")
        assert body["allowed_extensions"] == ["ff@x"]

    def test_render_dispatch_chromium_family(self):
        for b in ("chromium", "chrome", "brave", "vivaldi", "edge"):
            body = bi.render_manifest(
                b, bridge_path="/x/bridge",
                chromium_extension_id="aaa")
            assert body["allowed_origins"] == [
                "chrome-extension://aaa/"], b

    def test_render_unknown_browser_raises(self):
        try:
            bi.render_manifest("opera", bridge_path="/x")
        except ValueError as e:
            assert "opera" in str(e)
        else:
            raise AssertionError("expected ValueError")


# ---- manifest path resolution ------------------------------------

class TestManifestPath:
    def test_firefox_path(self, tmp_path):
        p = bi.manifest_path(str(tmp_path), "firefox")
        assert p == tmp_path / ".mozilla/native-messaging-hosts/qdistro.json"

    def test_chromium_paths_known(self, tmp_path):
        cases = {
            "chromium": ".config/chromium/NativeMessagingHosts/qdistro.json",
            "chrome":   ".config/google-chrome/NativeMessagingHosts/qdistro.json",
            "brave":    ".config/BraveSoftware/Brave-Browser/NativeMessagingHosts/qdistro.json",
            "vivaldi":  ".config/vivaldi/NativeMessagingHosts/qdistro.json",
            "edge":     ".config/microsoft-edge/NativeMessagingHosts/qdistro.json",
        }
        for b, suffix in cases.items():
            p = bi.manifest_path(str(tmp_path), b)
            assert p == tmp_path / suffix, b

    def test_unknown_browser_raises(self, tmp_path):
        try:
            bi.manifest_path(str(tmp_path), "opera")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


# ---- install_one + install_all -----------------------------------

class TestInstall:
    def test_install_one_writes_atomic(self, tmp_path):
        path = bi.install_one(
            home=str(tmp_path), browser="firefox",
            bridge_path="/usr/lib/qdistro/browser-bridge")
        assert path.exists()
        body = json.loads(path.read_text())
        assert body["name"] == "qdistro"
        assert body["allowed_extensions"] == ["qdistro@qdistro.local"]
        # 0644 — manifest is read by the user's browser process.
        assert (path.stat().st_mode & 0o777) == 0o644

    def test_install_one_idempotent(self, tmp_path):
        p1 = bi.install_one(home=str(tmp_path), browser="firefox")
        body1 = p1.read_bytes()
        p2 = bi.install_one(home=str(tmp_path), browser="firefox")
        body2 = p2.read_bytes()
        assert p1 == p2
        assert body1 == body2

    def test_install_all_default_writes_six(self, tmp_path):
        out = bi.install_all(home=str(tmp_path))
        assert set(out.keys()) == set(bi.ALL_BROWSERS)
        for browser, path in out.items():
            assert path.exists(), browser

    def test_install_all_subset(self, tmp_path):
        out = bi.install_all(home=str(tmp_path),
                             browsers=("firefox", "chromium"))
        assert set(out.keys()) == {"firefox", "chromium"}
        assert (tmp_path / ".mozilla").exists()
        assert (tmp_path / ".config" / "chromium").exists()
        # Other browsers' dirs must NOT have been created.
        assert not (tmp_path / ".config" / "google-chrome").exists()


# ---- chromium policy ---------------------------------------------

class TestChromiumPolicy:
    def test_render_policy_shape(self):
        body = bi.render_chromium_policy(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1",
            "https://qdistro.example/update.xml")
        assert "ExtensionInstallForcelist" in body
        assert body["ExtensionInstallForcelist"] == [
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa1;"
            "https://qdistro.example/update.xml",
        ]

    def test_policy_path_chromium_family(self):
        for b in ("chromium", "chrome", "brave", "vivaldi", "edge"):
            p = bi.policy_path(b)
            assert str(p).startswith("/etc/"), b
            assert p.name == "qdistro.json", b

    def test_policy_path_firefox_raises(self):
        try:
            bi.policy_path("firefox")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_install_chromium_policy_root_override(self, tmp_path):
        p = bi.install_chromium_policy(
            "chromium", "aaa",
            "https://qdistro.example/update.xml",
            root=str(tmp_path))
        assert p.exists()
        body = json.loads(p.read_text())
        assert body["ExtensionInstallForcelist"] == [
            "aaa;https://qdistro.example/update.xml"
        ]


# ---- CLI ---------------------------------------------------------

class TestCli:
    def test_parse_browser_list_default(self):
        out = bi.parse_browser_list(None)
        assert out == bi.ALL_BROWSERS

    def test_parse_browser_list_subset(self):
        assert bi.parse_browser_list("firefox,chromium") == (
            "firefox", "chromium")

    def test_parse_browser_list_unknown_exits(self):
        try:
            bi.parse_browser_list("opera")
        except SystemExit as e:
            assert "opera" in str(e)
        else:
            raise AssertionError("expected SystemExit")

    def test_main_print_does_not_write(self, tmp_path, capsys):
        rc = bi.main(["--home", str(tmp_path),
                      "--browsers", "firefox", "--print"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "firefox" in captured.out
        assert "allowed_extensions" in captured.out
        # nothing on disk
        assert not (tmp_path / ".mozilla").exists()

    def test_main_default_writes(self, tmp_path):
        rc = bi.main(["--home", str(tmp_path),
                      "--browsers", "firefox,chromium"])
        assert rc == 0
        assert (tmp_path / ".mozilla/native-messaging-hosts/"
                "qdistro.json").exists()
        assert (tmp_path / ".config/chromium/NativeMessagingHosts/"
                "qdistro.json").exists()

    def test_main_install_policy_with_root_override(self, tmp_path):
        rc = bi.main([
            "--home", str(tmp_path),
            "--browsers", "chromium",
            "--install-policy",
            "--policy-update-url", "https://qdistro.example/u.xml",
            "--policy-root", str(tmp_path),
        ])
        assert rc == 0
        policy = (tmp_path / "etc" / "chromium"
                  / "policies" / "managed" / "qdistro.json")
        assert policy.exists()
        body = json.loads(policy.read_text())
        assert "ExtensionInstallForcelist" in body
