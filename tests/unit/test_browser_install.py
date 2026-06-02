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

import pytest

_MOD = (Path(__file__).resolve().parent.parent.parent
        / "browser_bridge" / "qdistro_browser_install.py")
spec = importlib.util.spec_from_file_location(
    "qdistro_browser_install", _MOD)
bi = importlib.util.module_from_spec(spec)
sys.modules["qdistro_browser_install"] = bi
spec.loader.exec_module(bi)


# Gecko id of the BUNDLED Firefox extension this installer authorizes.
# The installer ships next to browser_bridge/extension/ and the README
# directs users to run `qdistro-browser-install --browsers firefox` to
# authorize that bundled extension (built from manifest.firefox.json), so
# the installer default MUST equal the bundled manifest's gecko id.
BUNDLED_GECKO_ID = "qdistro@qdistro.local"

# Gecko id of the STANDALONE qdfirefox extension (a separate artifact
# shipped from the qdfirefox-extension repo). The native-host/standalone
# install mode must authorize THIS id.
STANDALONE_GECKO_ID = "qdistro-firefox@qdistro.local"

# Path to the bundled Firefox manifest (single source of truth).
_BUNDLED_FIREFOX_MANIFEST = (
    Path(__file__).resolve().parent.parent.parent
    / "browser_bridge" / "extension" / "manifest.firefox.json")

# Canonical standalone manifest in the sibling qdfirefox-extension repo.
# Checked by the cross-repo contract test when present.
_STANDALONE_MANIFEST_CANDIDATES = [
    Path("/home/playai/doc/qdistro2/qdfirefox-extension/manifest.json"),
    Path(__file__).resolve().parents[3]
    / "qdfirefox-extension" / "manifest.json",
    Path(__file__).resolve().parents[2]
    / "qdfirefox-extension" / "manifest.json",
]


def _gecko_id(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    return (data.get("browser_specific_settings", {})
            .get("gecko", {}).get("id"))


def _bundled_manifest_gecko_id() -> str:
    return _gecko_id(_BUNDLED_FIREFOX_MANIFEST)


# ---- extension-id single source of truth (finding #13) -----------

class TestFirefoxExtensionIdContract:
    def test_installer_default_equals_bundled_manifest_id(self):
        """The installer default and the BUNDLED extension's gecko id must
        be EQUAL. This is the real contract: the installer authorizes the
        bundled extension, so allowed_extensions must list its id.

        Regression for finding #13's broken remediation, which set the
        default to the *standalone* qdfirefox id (qdistro-firefox@...)
        while the bundled manifest still declares qdistro@qdistro.local —
        the native-messaging host would then reject the bundled extension.
        """
        assert _BUNDLED_FIREFOX_MANIFEST.exists()
        bundled_id = _bundled_manifest_gecko_id()
        # Read BOTH sources and assert they are EQUAL (not merely != old).
        assert bi.DEFAULT_FIREFOX_EXTENSION_ID == bundled_id
        assert bundled_id == BUNDLED_GECKO_ID

    def test_default_rendered_manifest_uses_bundled_id(self):
        """render_manifest('firefox') with no explicit id must emit the
        bundled extension's id in allowed_extensions."""
        body = bi.render_manifest("firefox", bridge_path="/x/bridge")
        assert body["allowed_extensions"] == [_bundled_manifest_gecko_id()]


# ---- bundled vs standalone install modes (finding #13) -----------

class TestFirefoxInstallModes:
    """Finding #13 (corrected): the generic installer authorized only the
    BUNDLED id, leaving the separately-shipped standalone qdfirefox
    extension unauthorized. The installer must expose an explicit
    standalone mode whose default id is the standalone gecko id, while the
    bundled mode keeps the bundled gecko id."""

    def test_mode_ids_are_distinct(self):
        bundled = bi.firefox_extension_id_for_mode("bundled")
        standalone = bi.firefox_extension_id_for_mode("standalone")
        assert bundled == BUNDLED_GECKO_ID
        assert standalone == STANDALONE_GECKO_ID
        # The whole point of #13: the two artifacts have DIFFERENT ids.
        assert bundled != standalone

    def test_bundled_mode_default_equals_bundled_manifest_id(self):
        """Bundled-mode default MUST equal the bundled
        manifest.firefox.json gecko id."""
        assert (bi.firefox_extension_id_for_mode("bundled")
                == _bundled_manifest_gecko_id())

    def test_standalone_mode_default_equals_standalone_manifest_id(self):
        """Cross-repo CONTRACT TEST: the standalone-mode default MUST equal
        the canonical standalone qdfirefox manifest's gecko id. Skipped
        only when that sibling repo isn't checked out.

        This is the regression for #13's broken remediation: the bundled
        installer never authorized the standalone id, so qdfirefox failed.
        """
        manifest = next(
            (p for p in _STANDALONE_MANIFEST_CANDIDATES if p.exists()),
            None)
        if manifest is None:
            pytest.skip("qdfirefox-extension/manifest.json not in tree")
        standalone_gecko = _gecko_id(manifest)
        assert (bi.firefox_extension_id_for_mode("standalone")
                == standalone_gecko)
        assert standalone_gecko == STANDALONE_GECKO_ID

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError):
            bi.firefox_extension_id_for_mode("bogus")

    def test_cli_standalone_mode_writes_standalone_id(self, tmp_path):
        rc = bi.main(["--home", str(tmp_path), "--browsers", "firefox",
                      "--firefox-mode", "standalone"])
        assert rc == 0
        path = (tmp_path / ".mozilla/native-messaging-hosts/qdistro.json")
        body = json.loads(path.read_text())
        assert body["allowed_extensions"] == [STANDALONE_GECKO_ID]

    def test_cli_default_mode_writes_bundled_id(self, tmp_path):
        rc = bi.main(["--home", str(tmp_path), "--browsers", "firefox"])
        assert rc == 0
        path = (tmp_path / ".mozilla/native-messaging-hosts/qdistro.json")
        body = json.loads(path.read_text())
        assert body["allowed_extensions"] == [BUNDLED_GECKO_ID]

    def test_cli_explicit_id_overrides_mode(self, tmp_path):
        rc = bi.main(["--home", str(tmp_path), "--browsers", "firefox",
                      "--firefox-mode", "standalone",
                      "--firefox-extension-id", "custom@x"])
        assert rc == 0
        path = (tmp_path / ".mozilla/native-messaging-hosts/qdistro.json")
        body = json.loads(path.read_text())
        assert body["allowed_extensions"] == ["custom@x"]


# ---- manifest rendering ------------------------------------------

class TestManifestRendering:
    def test_firefox_shape(self):
        body = bi.render_firefox_manifest(
            "/usr/lib/qdistro/browser-bridge",
            "qdistro@qdistro.local")
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
