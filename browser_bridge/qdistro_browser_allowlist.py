"""Single source of truth for the browser parent-exe allowlist + the
root-owned optional-browser opt-in (P0-4).

The browser bridge's *entry gate* (``qdistro_browser_bridge.py``) and the
per-user 9e daemons' *process-identity gate*
(``qdistro_browser_daemon_identity.py`` + ``qdistro_pwd_daemon.py``) must
agree on exactly which parent-browser exes are trusted. Before this module
the three carried independent copies of the matrix and the daemons defaulted
to the full Brave/Vivaldi/Chrome/Edge set while the bridge had moved to an
admin opt-in — a latent drift between the entry gate and the
defense-in-depth gates. Centralising the baseline, the optional set, and the
opt-in resolver here makes them agree *by construction*.

Trust model (P0-2 / P0-4 lesson): the optional browsers are accepted ONLY
when an admin opts each one in through a **root-owned** config file, read
through an fd-based gate so an unprivileged process (the bridge runs as the
browser-child uid; the daemons run as the session uid) can never widen its
own boundary. Every error path fails closed to the Firefox+Chromium
baseline.

This module is intentionally dependency-free (stdlib only) so the TCB
daemons can import it without pulling in the bridge's heavier imports. It is
installed alongside the bridge under ``/usr/libexec/qdistro/``; consumers
that may be installed without it import it defensively and fall back to
:data:`DEFAULT_ALLOWED_PARENT_EXES` (the narrowest, fail-closed set).
"""
from __future__ import annotations

import os
import stat
import sys

# Default-on baseline: Firefox + Chromium are the two browser families
# qdistro supports out of the box, always trusted as bridge/daemon parents.
DEFAULT_ALLOWED_PARENT_EXES: tuple[str, ...] = (
    "/usr/lib64/firefox/firefox",
    "/usr/lib/firefox/firefox",   # 32-bit / non-/lib64 distros
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

# Optional parent browsers (P0-4). Chrome, Brave, Vivaldi, and Edge are
# accepted ONLY when an admin opts each one in — they are default-OFF. This
# mirrors the F4 firefox-containers opt-in: a capability that widens the
# trust boundary is off until an admin authors a root-owned policy artifact
# to enable it.
OPTIONAL_PARENT_EXES: dict[str, tuple[str, ...]] = {
    "chrome":  ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"),
    "brave":   ("/usr/bin/brave", "/usr/bin/brave-browser"),
    "vivaldi": ("/usr/bin/vivaldi", "/usr/bin/vivaldi-stable"),
    "edge":    ("/usr/bin/microsoft-edge", "/usr/bin/microsoft-edge-stable"),
}

# Root-owned opt-in config for the optional browsers above. One browser key
# per non-comment line (``chrome`` / ``brave`` / ``vivaldi`` / ``edge``);
# ``#`` starts a comment. Absent or empty => baseline only. Honored ONLY
# when it is a root-owned regular file that is not group/other-writable — an
# unprivileged process must never be able to widen its own trust boundary
# (the same lesson P0-2 applied to the env-var override).
ALLOWLIST_CONFIG_PATH = "/etc/qdistro/browser-bridge-allowlist.conf"


def audit_allowlist_config(path: str, decision: str, reason: str) -> None:
    """Audit an allowlist-config trust decision to stderr (journal).

    Writes to stderr, never stdout — the bridge's stdout is the
    native-messaging wire, and the daemons keep stdout clean too.
    """
    sys.stderr.write(
        f"qdistro-browser-allowlist: config {path!r} "
        f"decision={decision} reason={reason}\n")
    sys.stderr.flush()


def read_optin_browser_keys(
        config_path: str = ALLOWLIST_CONFIG_PATH,
        trusted_uid: int = 0,
) -> tuple[str, ...]:
    """Return the optional-browser keys an admin has opted into.

    Reads ``config_path`` (default the root-owned allowlist config). Each
    non-blank, non-``#`` line names one optional browser
    (``chrome`` / ``brave`` / ``vivaldi`` / ``edge``); ``#`` begins a
    comment. Unknown keys are ignored (audited). Returns ``()`` when the
    file is absent.

    Fail-closed trust gate: the config is honored ONLY if it is a
    **regular file** (not a symlink/FIFO/device) owned by ``trusted_uid``
    (root in production) and **not group/other-writable**. A wrong owner,
    a group/other-writable mode, a non-regular file, or any read/decode
    error yields ``()`` — the caller falls back to the Firefox+Chromium
    baseline rather than let a non-admin widen the trust boundary. The
    ``trusted_uid`` parameter exists so tests can exercise both the
    honored and the rejected path without running as root.

    The gate is **fd-based**: the path is opened ``O_NOFOLLOW`` (no
    final-component symlink), ``O_NONBLOCK`` (a planted FIFO can't block
    the open), and all owner/mode/regular-file checks run on the opened
    object via ``fstat`` — so there is no lstat->open TOCTOU and the bytes
    read are provably the bytes that passed the checks.
    """
    try:
        fd = os.open(
            config_path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return ()            # absent => baseline (the common case)
    except OSError as e:
        # ELOOP (final-component symlink, O_NOFOLLOW) and friends land here.
        audit_allowlist_config(config_path, "ignored", f"open failed: {e}")
        return ()
    f = None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            audit_allowlist_config(
                config_path, "ignored",
                "not a regular file (symlink/fifo/device?)")
            return ()
        if st.st_uid != int(trusted_uid):
            audit_allowlist_config(
                config_path, "ignored",
                f"owner uid {st.st_uid} != trusted {int(trusted_uid)}")
            return ()
        if st.st_mode & 0o022:
            audit_allowlist_config(
                config_path, "ignored",
                f"group/other-writable mode {stat.S_IMODE(st.st_mode):#o}")
            return ()
        f = os.fdopen(fd, "r", encoding="utf-8")
        fd = -1              # the file object owns the descriptor now
        raw_lines = f.read().splitlines()
    except (OSError, UnicodeError, ValueError) as e:
        audit_allowlist_config(config_path, "ignored", f"read error: {e}")
        return ()
    finally:
        try:
            if f is not None:
                f.close()
            elif fd >= 0:
                os.close(fd)
        except OSError:
            pass
    keys: list[str] = []
    for raw in raw_lines:
        line = raw.split("#", 1)[0].strip().lower()
        if not line:
            continue
        if line in OPTIONAL_PARENT_EXES:
            if line not in keys:
                keys.append(line)
        else:
            audit_allowlist_config(
                config_path, "skip-key", f"unknown browser key {line!r}")
    return tuple(keys)


def resolve_parent_exes(
        config_path: str = ALLOWLIST_CONFIG_PATH,
        trusted_uid: int = 0,
) -> tuple[str, ...]:
    """Return the effective trusted parent-exe set.

    The Firefox+Chromium baseline plus whichever optional browsers
    (``chrome`` / ``brave`` / ``vivaldi`` / ``edge``) an admin has opted
    into via the root-owned ``config_path`` (P0-4). With no opt-in config
    the optional browsers are excluded.
    """
    allow = list(DEFAULT_ALLOWED_PARENT_EXES)
    for key in read_optin_browser_keys(config_path, trusted_uid):
        allow.extend(OPTIONAL_PARENT_EXES[key])
    return tuple(allow)
