"""qdistro-browser-install — native-messaging manifest writer.

Per spec/14 §"Per-user, per-browser installation" + §"Phase-8 MVP scope".
Writes the per-browser native-messaging host manifest (and, for
Chromium-family, the ``ExtensionInstallForcelist`` policy file)
under a chosen user's home directory.

The module is pure-python so all path/template logic is testable
without touching live filesystems. The CLI entrypoint at the bottom
is a thin shell over the helpers.

Six browsers covered (RPM-only matrix per spec/14):
- Firefox          ``~/.mozilla/native-messaging-hosts/``
- Chromium         ``~/.config/chromium/NativeMessagingHosts/``
- Chrome           ``~/.config/google-chrome/NativeMessagingHosts/``
- Brave            ``~/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts/``
- Vivaldi          ``~/.config/vivaldi/NativeMessagingHosts/``
- Edge             ``~/.config/microsoft-edge/NativeMessagingHosts/``

Manifest fields are identical across browsers except the allowed-
extension keying: Firefox uses ``allowed_extensions`` (raw IDs);
Chromium-family uses ``allowed_origins`` (``chrome-extension://<id>/``).
We code-generate both shapes from one source-of-truth template.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

NATIVE_HOST_NAME = "qdistro"
DEFAULT_BRIDGE_PATH = "/usr/lib/qdistro/browser-bridge"


def _bundled_firefox_extension_id() -> str:
    """Read the gecko id of the *bundled* Firefox extension that this
    installer authorizes.

    This installer ships next to ``browser_bridge/extension/`` and the
    README directs users to run ``qdistro-browser-install --browsers
    firefox`` to authorize the bundled extension built by
    ``build-extension.sh`` from ``manifest.firefox.json``. The native-
    messaging manifest's ``allowed_extensions`` MUST therefore match that
    bundled manifest's ``browser_specific_settings.gecko.id`` — otherwise
    the native-messaging host rejects the canonical bundled extension.

    The bundled manifest is the single source of truth: read it at import
    time so the default can never silently drift from what is shipped.
    Falls back to the known-shipped literal if the file is absent (e.g.
    the module is vendored without the extension tree).
    """
    fallback = "qdistro@qdistro.local"
    try:
        manifest = (Path(__file__).resolve().parent
                    / "extension" / "manifest.firefox.json")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        gecko_id = (data.get("browser_specific_settings", {})
                    .get("gecko", {}).get("id"))
        return str(gecko_id) if gecko_id else fallback
    except (OSError, ValueError):
        return fallback


# Single source of truth for the bundled Firefox extension id — derived
# from the bundled ``extension/manifest.firefox.json`` this installer
# authorizes (``qdistro@qdistro.local``). A mismatch renders the native-
# messaging manifest's ``allowed_extensions`` inert, so the bridge would
# refuse the canonical bundled extension.
DEFAULT_FIREFOX_EXTENSION_ID = _bundled_firefox_extension_id()

# The *standalone* qdfirefox extension is a SEPARATE artifact, MANUALLY built
# by the user from the ``qdfirefox-extension`` repo (v1 has no signed
# extension channel and no installer ships it — see
# ``doc/browser-extension-install.md``); it declares a DIFFERENT gecko id
# (``qdistro-firefox@qdistro.local``) in its own ``manifest.json``. A user
# who installed that standalone extension (rather than the bundled MV2
# build) needs the native-messaging host to authorize THAT id, or the
# bridge refuses it — this was finding #13: the generic installer only
# knew the bundled id and silently left qdfirefox unauthorized.
#
# This id is a known-shipped literal here (the standalone manifest lives
# in a sibling repo that is NOT part of this package's source tree, so it
# cannot be read at import time on an installed system). The cross-repo
# contract — that this literal equals the standalone manifest's gecko id —
# is asserted by the unit suite's contract test against the canonical
# ``qdfirefox-extension/manifest.json`` when that repo is checked out.
STANDALONE_FIREFOX_EXTENSION_ID = "qdistro-firefox@qdistro.local"

# Firefox install modes. ``bundled`` authorizes the LEGACY MV2 extension
# built next to this installer (manifest.firefox.json) — the flat tree with
# no ``src/``/``gate.js`` and no origin allowlist that J11 found the installer
# copying to /usr/share/qdistro/browser-extension/; ``standalone`` authorizes
# the maintained qdfirefox extension the v1 install doc tells users to build.
# NOTE the default below is still ``bundled`` for compatibility: v1 users must
# pass ``--firefox-mode standalone`` explicitly. Each maps to its own default extension id so the
# right ``allowed_extensions`` is written for what the user actually has.
FIREFOX_MODE_IDS: dict[str, str] = {
    "bundled": DEFAULT_FIREFOX_EXTENSION_ID,
    "standalone": STANDALONE_FIREFOX_EXTENSION_ID,
}
DEFAULT_FIREFOX_MODE = "bundled"


def firefox_extension_id_for_mode(mode: str) -> str:
    """Default Firefox extension id for an install ``mode`` (bundled |
    standalone). Unknown modes are a hard error — fail closed rather than
    silently authorize the wrong (or no) extension."""
    try:
        return FIREFOX_MODE_IDS[mode]
    except KeyError:
        raise ValueError(
            f"unknown firefox install mode: {mode!r} "
            f"(expected one of {sorted(FIREFOX_MODE_IDS)})") from None


# Chromium-family extension id of the canonical qdchrome extension.
#
# A Chromium extension id is the first 16 bytes of the SHA-256 of the
# extension's DER public key, hex-encoded and then mapped 0-9a-f -> a-p:
# ALWAYS 32 characters, ALWAYS in [a-p]. ``qdchrome-extension`` pins its
# PUBLIC key in ``manifest.chromium.json``'s ``"key"`` field, so a
# developer-mode "Load unpacked" of ``dist/chromium/`` yields this id on
# every rebuild — which is what makes the v1 manual-load flow work with a
# pre-written native-messaging manifest (see
# ``doc/browser-extension-install.md``). A future packed CRX carries the
# same id ONLY if it is signed with the matching PRIVATE key; and because
# the pinned key is public, the id identifies a key namespace, never the
# authorship of the loaded code.
#
# R4 finding: this default used to be the placeholder
# ``"qdistroqdistroqdistroqdistroaaaaaaaa"`` — 36 characters, and
# containing letters outside [a-p], so it is not even a syntactically
# valid id. Every Chromium-family native-messaging manifest written by
# the default install path therefore authorized an extension that cannot
# exist, and the real extension was rejected by the browser with
# "Access to the specified native messaging host is forbidden". The
# Firefox side of the same bug was fixed as finding #13; the Chromium
# side was not. Like ``STANDALONE_FIREFOX_EXTENSION_ID`` this is a
# known-shipped literal (the sibling repo is not part of this package's
# source tree on an installed system); the cross-repo contract — that it
# equals the id derived from ``qdchrome-extension/manifest.chromium.json``
# ``"key"`` — is asserted by the unit suite when that repo is checked out.
DEFAULT_CHROMIUM_EXTENSION_ID = "ammgnkddbnjdhikklpljgiclldedgncf"


def chromium_extension_id_is_wellformed(ext_id: str) -> bool:
    """True when ``ext_id`` has the Chromium extension-id shape: exactly
    32 characters drawn from ``a``-``p``.

    Used to fail closed rather than write a native-messaging manifest
    whose ``allowed_origins`` can never match a real extension (the
    placeholder-default bug above).
    """
    return (len(ext_id) == 32
            and all("a" <= c <= "p" for c in ext_id))

# Per-browser native-messaging host-manifest directories. Keyed by
# the short browser name passed on the CLI; values are paths
# relative to the target user's home (so the same map drives both
# `--home /tmp/x` testing and live `/home/<user>` installs).
NATIVE_HOST_DIRS: dict[str, str] = {
    "firefox":  ".mozilla/native-messaging-hosts",
    "chromium": ".config/chromium/NativeMessagingHosts",
    "chrome":   ".config/google-chrome/NativeMessagingHosts",
    "brave":    ".config/BraveSoftware/Brave-Browser/NativeMessagingHosts",
    "vivaldi":  ".config/vivaldi/NativeMessagingHosts",
    "edge":     ".config/microsoft-edge/NativeMessagingHosts",
}

CHROMIUM_FAMILY = ("chromium", "chrome", "brave", "vivaldi", "edge")
FIREFOX_FAMILY = ("firefox",)
ALL_BROWSERS: tuple[str, ...] = tuple(NATIVE_HOST_DIRS.keys())

# System-wide ExtensionInstallForcelist policy directories. Used
# only when the admin opts in via `--install-policy`. Per spec/14:
# Chromium family supports the same policy schema; the qdistro
# CRX is force-installed via update.xml.
POLICY_DIRS: dict[str, str] = {
    "chromium": "/etc/chromium/policies/managed",
    "chrome":   "/etc/opt/chrome/policies/managed",
    "brave":    "/etc/brave/policies/managed",
    "vivaldi":  "/etc/vivaldi/policies/managed",
    "edge":     "/etc/opt/edge/policies/managed",
}


def render_firefox_manifest(
        bridge_path: str,
        extension_id: str,
        description: str = "qdistro browser bridge",
) -> dict:
    """Firefox-shape manifest. ``allowed_extensions`` is a list of
    raw extension IDs (e.g. ``qdistro@qdistro.local``).
    """
    return {
        "name": NATIVE_HOST_NAME,
        "description": description,
        "path": bridge_path,
        "type": "stdio",
        "allowed_extensions": [extension_id],
    }


def render_chromium_manifest(
        bridge_path: str,
        extension_id: str,
        description: str = "qdistro browser bridge",
) -> dict:
    """Chromium-shape manifest. ``allowed_origins`` is a list of
    ``chrome-extension://<id>/`` URLs.
    """
    return {
        "name": NATIVE_HOST_NAME,
        "description": description,
        "path": bridge_path,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }


def render_manifest(
        browser: str,
        bridge_path: str,
        firefox_extension_id: str = DEFAULT_FIREFOX_EXTENSION_ID,
        chromium_extension_id: str = DEFAULT_CHROMIUM_EXTENSION_ID,
        description: str = "qdistro browser bridge",
) -> dict:
    """Code-generate the right manifest shape for ``browser``."""
    if browser in FIREFOX_FAMILY:
        return render_firefox_manifest(
            bridge_path, firefox_extension_id, description)
    if browser in CHROMIUM_FAMILY:
        return render_chromium_manifest(
            bridge_path, chromium_extension_id, description)
    raise ValueError(f"unknown browser: {browser}")


def manifest_path(home: str, browser: str) -> Path:
    """Resolve the on-disk manifest path for ``browser`` under
    ``home``. Filename is always ``<host-name>.json``.
    """
    if browser not in NATIVE_HOST_DIRS:
        raise ValueError(f"unknown browser: {browser}")
    return Path(home) / NATIVE_HOST_DIRS[browser] / f"{NATIVE_HOST_NAME}.json"


def write_manifest_atomic(path: Path, body: dict) -> None:
    """Atomic write: tmp + rename. Parent dir is created at 0o755;
    final file is mode 0o644 (manifests are world-readable since
    the browser opens them as the user).
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    tmp = path.with_suffix(path.suffix + ".tmp")
    raw = json.dumps(body, indent=2).encode("utf-8")
    with open(tmp, "wb") as f:
        f.write(raw)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def install_one(
        home: str,
        browser: str,
        bridge_path: str = DEFAULT_BRIDGE_PATH,
        firefox_extension_id: str = DEFAULT_FIREFOX_EXTENSION_ID,
        chromium_extension_id: str = DEFAULT_CHROMIUM_EXTENSION_ID,
) -> Path:
    """Render + write the per-browser manifest. Returns the path
    written. Idempotent — overwriting yields the same body."""
    body = render_manifest(
        browser=browser,
        bridge_path=bridge_path,
        firefox_extension_id=firefox_extension_id,
        chromium_extension_id=chromium_extension_id,
    )
    p = manifest_path(home, browser)
    write_manifest_atomic(p, body)
    return p


def install_all(
        home: str,
        browsers: tuple[str, ...] | list[str] | None = None,
        bridge_path: str = DEFAULT_BRIDGE_PATH,
        firefox_extension_id: str = DEFAULT_FIREFOX_EXTENSION_ID,
        chromium_extension_id: str = DEFAULT_CHROMIUM_EXTENSION_ID,
) -> dict[str, Path]:
    """Install manifests for every browser in ``browsers`` (default
    all six). Returns ``{browser: path}`` for the writes that
    happened. Browsers whose home subtree doesn't exist (e.g. user
    never launched Firefox) still get a manifest written — the
    parent dir is created on demand because the browser will read
    the manifest the next time it starts.
    """
    if browsers is None:
        browsers = ALL_BROWSERS
    out: dict[str, Path] = {}
    for b in browsers:
        out[b] = install_one(
            home=home,
            browser=b,
            bridge_path=bridge_path,
            firefox_extension_id=firefox_extension_id,
            chromium_extension_id=chromium_extension_id,
        )
    return out


def render_chromium_policy(
        extension_id: str,
        update_url: str,
) -> dict:
    """ExtensionInstallForcelist policy body for Chromium family.

    The string format ``"<extension-id>;<update-url>"`` is verified
    against Chromium's policy templates (chrome.policy_templates) —
    do not change. See spec/14 §"Distribution constraints".
    """
    return {
        "ExtensionInstallForcelist": [
            f"{extension_id};{update_url}",
        ],
    }


def policy_path(browser: str) -> Path:
    """System-wide ExtensionInstallForcelist drop dir, by browser.

    Caller is responsible for invoking this only when the browser
    is in CHROMIUM_FAMILY (Firefox uses a different mechanism).
    """
    if browser not in POLICY_DIRS:
        raise ValueError(f"no policy dir for browser: {browser}")
    return Path(POLICY_DIRS[browser]) / f"{NATIVE_HOST_NAME}.json"


def install_chromium_policy(
        browser: str,
        extension_id: str,
        update_url: str,
        root: str | None = None,
) -> Path:
    """Drop the ExtensionInstallForcelist policy file. ``root``
    overrides ``/`` for tests.
    """
    raw = policy_path(browser)
    p = Path(root) / raw.relative_to("/") if root else raw
    body = render_chromium_policy(extension_id, update_url)
    write_manifest_atomic(p, body)
    return p


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qdistro-browser-install",
        description="Write per-browser native-messaging host manifests "
                    "for the qdistro browser-bridge.",
    )
    p.add_argument("--home", default=os.path.expanduser("~"),
                   help="target user's home dir (default $HOME)")
    p.add_argument("--browsers",
                   help="comma-separated subset of: "
                        + ",".join(ALL_BROWSERS)
                        + " (default: all)")
    p.add_argument("--bridge-path", default=DEFAULT_BRIDGE_PATH,
                   help="path to the bridge binary "
                        f"(default {DEFAULT_BRIDGE_PATH})")
    p.add_argument("--firefox-mode",
                   choices=sorted(FIREFOX_MODE_IDS),
                   default=DEFAULT_FIREFOX_MODE,
                   help="which Firefox extension this host authorizes: "
                        "'bundled' (the MV2 build shipped with this "
                        "package) or 'standalone' (the separately-shipped "
                        "qdfirefox extension). Selects the default "
                        "allowed-extensions id; overridden by "
                        "--firefox-extension-id if given.")
    p.add_argument("--firefox-extension-id", default=None,
                   help="explicit Firefox extension id; overrides the "
                        "--firefox-mode default.")
    p.add_argument("--chromium-extension-id",
                   default=DEFAULT_CHROMIUM_EXTENSION_ID)
    p.add_argument("--install-policy", action="store_true",
                   help="ALSO drop ExtensionInstallForcelist policy "
                        "files under /etc (Chromium-family only). "
                        "Requires root.")
    p.add_argument("--policy-update-url",
                   default="https://example.invalid/qdistro-update.xml",
                   help="update.xml URL for Chromium-family policy.")
    p.add_argument("--policy-root", default=None,
                   help="override /, for tests")
    p.add_argument("--print", action="store_true",
                   help="print resolved paths/JSON without writing")
    return p


def parse_browser_list(arg: str | None) -> tuple[str, ...]:
    if not arg:
        return ALL_BROWSERS
    out = []
    for b in arg.split(","):
        b = b.strip()
        if not b:
            continue
        if b not in NATIVE_HOST_DIRS:
            raise SystemExit(f"unknown browser: {b}")
        out.append(b)
    return tuple(out)


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    browsers = parse_browser_list(args.browsers)
    # An explicit --firefox-extension-id wins; otherwise the id is the
    # default for the chosen --firefox-mode (bundled vs standalone).
    firefox_extension_id = (
        args.firefox_extension_id
        if args.firefox_extension_id is not None
        else firefox_extension_id_for_mode(args.firefox_mode))
    # Fail closed on a malformed Chromium id: a manifest whose
    # ``allowed_origins`` cannot match any real extension is worse than no
    # manifest — the browser reports only "Access to the specified native
    # messaging host is forbidden", with nothing pointing at the id.
    if (any(b in CHROMIUM_FAMILY for b in browsers)
            and not chromium_extension_id_is_wellformed(
                args.chromium_extension_id)):
        raise SystemExit(
            f"invalid chromium extension id: {args.chromium_extension_id!r} "
            "(must be exactly 32 characters in [a-p] — the mapped SHA-256 "
            "prefix of the extension's public key)")
    if args.print:
        for b in browsers:
            body = render_manifest(
                browser=b, bridge_path=args.bridge_path,
                firefox_extension_id=firefox_extension_id,
                chromium_extension_id=args.chromium_extension_id)
            print(f"# {b}: {manifest_path(args.home, b)}")
            print(json.dumps(body, indent=2))
        return 0
    written = install_all(
        home=args.home,
        browsers=browsers,
        bridge_path=args.bridge_path,
        firefox_extension_id=firefox_extension_id,
        chromium_extension_id=args.chromium_extension_id,
    )
    for b, p in written.items():
        print(f"[install] {b}: {p}")
    if args.install_policy:
        for b in browsers:
            if b not in CHROMIUM_FAMILY:
                continue
            p = install_chromium_policy(
                browser=b,
                extension_id=args.chromium_extension_id,
                update_url=args.policy_update_url,
                root=args.policy_root,
            )
            print(f"[policy ] {b}: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
