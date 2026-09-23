#!/usr/bin/env python3
"""qdshell plugin registry/install helper."""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


# Load the sibling signing-verify primitive. Module name has a hyphen on disk
# (plugin-helper.py is loaded the same way by tests), so import by path.
_SV_PATH = Path(__file__).resolve().parent / "signing_verify.py"
_sv_spec = importlib.util.spec_from_file_location("qdshell_signing_verify", _SV_PATH)
assert _sv_spec and _sv_spec.loader
signing_verify = importlib.util.module_from_spec(_sv_spec)
_sv_spec.loader.exec_module(signing_verify)


PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
COMPOSITE_KEY_RE = re.compile(r"^(?:[A-Fa-f0-9]{6}:)?[A-Za-z0-9_][A-Za-z0-9_.-]*$")


def _validate_repo_url(url: str) -> None:
    # F9: a plugin source is a full code-trust decision (the cloned tree is loaded
    # as live QML). Restrict to remote, host-bearing transports only — https and
    # ssh. Drop file:/git:/http: and scp-style "user@host:path" shorthand:
    #   - file:  lets `git clone` a local path (cross-silo info disclosure / a way
    #            to stage attacker-controlled content from another silo's dir);
    #   - git:/http: are unauthenticated/cleartext;
    #   - scp-style shorthand is harder to validate and widens the surface
    #     (use the explicit ssh:// form instead).
    # urlparse is not a security boundary; reject control chars outright.
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        raise ValueError("repository URL contains control characters")
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "ssh"):
        raise ValueError("unsupported repository URL scheme (only https/ssh)")
    if not parsed.netloc:
        raise ValueError("repository URL is missing a host")


def _validate_plugin_id(plugin_id: str) -> None:
    # F6 (QML/Python parity): the charset regex alone admits "safe..x"; reject any
    # ".." traversal segment and all-dot ids, matching PluginRegistry.isSafePluginId.
    if (
        not PLUGIN_ID_RE.fullmatch(plugin_id)
        or ".." in plugin_id
        or set(plugin_id) == {"."}
    ):
        raise ValueError("invalid plugin id")


def _validate_composite_key(composite_key: str) -> None:
    if not COMPOSITE_KEY_RE.fullmatch(composite_key):
        raise ValueError("invalid plugin install key")
    suffix = composite_key.rsplit(":", 1)[-1]
    if ".." in suffix or set(suffix) == {"."}:
        raise ValueError("invalid plugin install key")


def _run(argv: list[str], cwd: Path | None = None) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _clone_sparse(repo_url: str, checkout: str, temp_dir: Path) -> None:
    _run([
        "git", "clone", "--filter=blob:none", "--sparse", "--depth=1",
        "--quiet", "--", repo_url, str(temp_dir),
    ])
    _run(["git", "sparse-checkout", "set", "--no-cone", "--", checkout], cwd=temp_dir)


def fetch_registry(repo_url: str) -> int:
    _validate_repo_url(repo_url)
    # Trust mode is read from on-disk config by this trusted helper, never passed
    # by the caller — so a compromised UI/argv cannot turn verification off, and a
    # download that simply omits the signature cannot downgrade an opted-in user.
    trust = signing_verify.load_trust_config()
    with tempfile.TemporaryDirectory(prefix="qdshell-plugin-") as tmp:
        temp_dir = Path(tmp)
        # Enforced mode needs the signed SHA256SUMS(.sig) alongside registry.json.
        if trust is None:
            _clone_sparse(repo_url, "/registry.json", temp_dir)
        else:
            _clone_sparse(
                repo_url,
                "/registry.json\n/%s\n/%s"
                % (signing_verify.MANIFEST_NAME, signing_verify.SIGNATURE_NAME),
                temp_dir,
            )
            # Verify the exact tree we are about to emit from. registry.json is the
            # only in-scope content file; SHA256SUMS(.sig) are excluded by name.
            signing_verify.verify_dir(temp_dir, trust)
        sys.stdout.buffer.write((temp_dir / "registry.json").read_bytes())
    return 0


def install_plugin(repo_url: str, plugin_id: str, plugin_dir: str) -> int:
    _validate_repo_url(repo_url)
    _validate_plugin_id(plugin_id)
    dest = Path(plugin_dir).expanduser()
    _validate_composite_key(dest.name)
    trust = signing_verify.load_trust_config()
    with tempfile.TemporaryDirectory(prefix="qdshell-plugin-") as tmp:
        temp_dir = Path(tmp)
        if trust is None:
            checkout = plugin_id
        else:
            # Pull the plugin dir plus the per-plugin SHA256SUMS(.sig) that sign it.
            checkout = "%s\n%s/%s\n%s/%s" % (
                plugin_id,
                plugin_id, signing_verify.MANIFEST_NAME,
                plugin_id, signing_verify.SIGNATURE_NAME,
            )
        _clone_sparse(repo_url, checkout, temp_dir)
        src = temp_dir / plugin_id
        if not src.is_dir():
            raise FileNotFoundError(plugin_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if trust is None:
            _publish_unsigned(src, dest)
        else:
            _publish_verified(src, dest, trust)
    return 0


def _publish_unsigned(src: Path, dest: Path) -> None:
    """Unsigned mode: exact prior behavior — merge-copy into dest, preserving
    settings.json. No verification."""
    if dest.exists():
        shutil.copytree(src, dest, symlinks=False, dirs_exist_ok=True)
    else:
        shutil.copytree(src, dest, symlinks=False)


def _publish_verified(src: Path, dest: Path, trust: dict) -> None:
    """Enforced mode: stage the new tree in a sibling dir, verify the STAGED tree
    (the exact bytes that will be published), then atomically swap into place.

    Avoids any dest-path TOCTOU: between the symlink check and use we never operate
    on `dest` by path. The only dest mutations are (a) reading the old settings.json
    via O_NOFOLLOW and (b) the final atomic os.replace of the verified staging dir.
    A symlinked / non-directory dest is refused before any swap, and a temp-collision
    name is removed so the rename publishes exactly the verified tree.
    """
    EXCLUDE = frozenset({"settings.json"})
    # Reject a symlinked or non-directory dest up front (pure hardening; a legit
    # plugin dir is never a symlink). Atomic replace targets the real path only.
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        raise signing_verify.VerifyError(f"plugin dest is not a real directory: {dest}")

    # Unpredictable, uniquely-created staging dir on the SAME filesystem as dest so
    # the final publish is an atomic rename. mkdtemp avoids a predictable-name race.
    staging = Path(tempfile.mkdtemp(prefix=".staging-" + dest.name + "-", dir=dest.parent))
    # copytree needs the target absent; remove the empty dir mkdtemp just made.
    staging.rmdir()
    # symlinks=False copies symlink *targets* as regular files; verify_dir then
    # re-hashes every final regular file and refuses any non-regular file, so the
    # published bytes are exactly what the signed manifest covers.
    shutil.copytree(src, staging, symlinks=False)
    try:
        # Carry forward the user-local settings.json (not signed by upstream) so an
        # upgrade preserves it. Pin the old dest by an O_NOFOLLOW|O_DIRECTORY fd and
        # read settings.json relative to THAT fd (dir_fd) with O_NOFOLLOW, so neither
        # a swapped-in dest-dir symlink nor a settings.json symlink can redirect the
        # read out of the real directory.
        try:
            dfd = os.open(dest, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        except (FileNotFoundError, NotADirectoryError, OSError):
            dfd = None
        if dfd is not None:
            try:
                sfd = os.open("settings.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
            except (FileNotFoundError, OSError):
                sfd = None
            try:
                if sfd is not None:
                    st = os.fstat(sfd)
                    if stat.S_ISREG(st.st_mode):
                        data = os.read(sfd, 1 << 20)
                        (staging / "settings.json").write_bytes(data)
            finally:
                if sfd is not None:
                    os.close(sfd)
                os.close(dfd)
        # Verify the staged tree (the exact bytes we are about to publish).
        signing_verify.verify_dir(staging, trust, extra_exclude=EXCLUDE)
        # Atomic publish: replace old dest with the verified staging dir. os.replace
        # of a dir requires the target be an empty dir or absent, so drop the old one
        # first (it is a real dir per the check above; never a symlink we'd follow).
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(staging, dest)
    except BaseException:
        # Fail closed: never leave unverified bytes where they would load.
        shutil.rmtree(staging, ignore_errors=True)
        raise


def verify_scheme(scheme_dir: str) -> int:
    """F1 color-scheme verify hook. Consults on-disk trust config:
      - unsigned mode (default / no signing.json): no-op, returns 0 immediately.
      - enforced mode: verify scheme_dir against its signed SHA256SUMS(.sig); any
        failure raises -> caller treats nonzero exit as a download failure and
        cleans up the (unverified) partial dir.
    """
    d = Path(scheme_dir).expanduser()
    signing_verify.maybe_verify_dir(d)  # raises in enforced mode on failure
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    fetch = sub.add_parser("fetch-registry")
    fetch.add_argument("repo_url")
    install = sub.add_parser("install-plugin")
    install.add_argument("repo_url")
    install.add_argument("plugin_id")
    install.add_argument("plugin_dir")
    verify = sub.add_parser("verify-scheme")
    verify.add_argument("scheme_dir")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "fetch-registry":
            return fetch_registry(args.repo_url)
        if args.cmd == "install-plugin":
            return install_plugin(args.repo_url, args.plugin_id, args.plugin_dir)
        if args.cmd == "verify-scheme":
            return verify_scheme(args.scheme_dir)
    except (OSError, subprocess.CalledProcessError, ValueError,
            signing_verify.TrustConfigError, signing_verify.VerifyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
