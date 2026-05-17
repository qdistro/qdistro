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
DEFAULT_FIREFOX_EXTENSION_ID = "qdistro@qdistro.local"
DEFAULT_CHROMIUM_EXTENSION_ID = "qdistroqdistroqdistroqdistroaaaaaaaa"

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
    p.add_argument("--firefox-extension-id",
                   default=DEFAULT_FIREFOX_EXTENSION_ID)
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
    if args.print:
        for b in browsers:
            body = render_manifest(
                browser=b, bridge_path=args.bridge_path,
                firefox_extension_id=args.firefox_extension_id,
                chromium_extension_id=args.chromium_extension_id)
            print(f"# {b}: {manifest_path(args.home, b)}")
            print(json.dumps(body, indent=2))
        return 0
    written = install_all(
        home=args.home,
        browsers=browsers,
        bridge_path=args.bridge_path,
        firefox_extension_id=args.firefox_extension_id,
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
