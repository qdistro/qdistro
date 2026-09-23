"""Per-profile CA bundle resolution for qdbrowser.

EXPERIMENTAL / UNVERIFIED — read this before trusting the feature
====================================================================
This module exports ``SSL_CERT_FILE`` to point QtWebEngine at a
per-profile CA bundle. **On the QtWebEngine build shipped in this
environment that almost certainly does NOTHING for HTTPS page TLS
validation**, and the same is true for most distro QtWebEngine builds.
Do not assume a page's server-certificate trust is actually changed by
enabling this feature without a live HTTPS test against a custom CA.

Why it may be a no-op
---------------------
QtWebEngine validates server certificates with its bundled Chromium
network stack, NOT Qt's ``QSslSocket`` / ``QSslConfiguration``. How
Chromium finds *system* trust anchors on Linux depends on how that
Chromium was built:

  * ``use_nss_certs=true`` (the historical default, and what Qt's
    QtWebEngine is built with): trust comes from **NSS** — the per-user
    NSS database at ``~/.pki/nssdb`` (``trust_store_nss.cc``). Chromium
    **ignores** ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` in this mode.
  * ``use_nss_certs=false`` with the unix system-trust verifier
    (``net/cert/internal/trust_store_unix.cc``, added upstream ≈M114 /
    2023): this reader honours ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``.

Inspecting the bundled ``libQt6WebEngineCore`` in this environment
(Qt 6.11.0, Chromium 140) shows it references ``trust_store_nss.cc`` and
``~/.pki`` / ``sql:`` NSS-DB strings but contains **no**
``trust_store_unix.cc`` and **no** ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``
literal at all. That is strong evidence this build is the NSS variant,
so the ``SSL_CERT_FILE`` export here is expected to be a no-op for page
TLS. We keep the (harmless, safe) export as forward-looking plumbing for
a future ``use_nss_certs=false`` build, but it is **unverified** by a
live TLS test.

The real mechanism for THIS build (NSS user DB)
-----------------------------------------------
To actually make a profile trust an enterprise/private CA on an
NSS-backed QtWebEngine, import the CA into the user's NSS DB::

    mkdir -p ~/.pki/nssdb
    certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "my-ca" \\
        -i /path/to/<profile>-ca.pem

That is process-global too (one NSS DB per user), so it does not give
per-profile isolation inside a single running instance — see the
per-launch note below. qdbrowser does not perform this import
automatically; it is documented here so a reader knows where trust
actually lives.

Per-launch limitation (applies to either mechanism)
---------------------------------------------------
QtWebEngine does **not** expose a per-``QWebEngineProfile`` SSL/CA
configuration, and qdbrowser runs every profile (``default``, ``work``,
``private``, ...) inside a **single OS process** — see
``webview.get_profile`` which mints named ``QWebEngineProfile`` objects
in one process. The CA trust source (whichever it is) is consumed once,
process-wide, so even if the mechanism worked it would only affect the
profile selected at **launch** (the ``--profile`` argument). Profiles
opened *afterward* in the same running window share the same network
process and therefore the same CA trust — not hot-swappable between
profiles in one instance. To isolate trust, launch ``personal`` as a
separate process. This matches qdistro's per-silo launch story.

Resolution order for a profile ``<p>`` (later wins is *not* the model —
the **user** bundle is preferred over the **admin** bundle when both
exist, mirroring the user-overlay-over-system precedence in
``cert_policy.load_pin_store``):

  1. user:  ``~/.config/qdbrowser/certs/<p>-ca.pem``
  2. admin: ``/etc/qdistro/qdbrowser/certs/<p>-ca.pem``

A resolved bundle must pass safety checks (``_is_trusted_bundle``):

  - the configured directory prefix must contain the resolved real path
    (no ``..`` traversal escaping the certs dir),
  - it must be a *regular* file (not a symlink target outside the dir,
    FIFO, device, ...),
  - it must be within a sane size cap,
  - the **admin** bundle, its certs directory, and every ancestor up to
    the certs dir must be owned by a trusted uid (root or this user) and
    must not be group/world-writable — an untrusted writer (a non-root
    owner, or a writable parent that permits rename/replace) must not be
    able to inject a CA the whole machine then trusts.

Default-off: when the feature is disabled in config (the default), or no
bundle is found, the environment is left untouched and qdbrowser behaves
exactly as before (system CA store only).
"""

from __future__ import annotations

import logging
import os
import stat

log = logging.getLogger("qdbrowser.cert")


# Default search roots. Both are overridable (mainly for tests) via the
# resolver arguments; production reads them from config.
USER_CERTS_DIR = os.path.expanduser("~/.config/qdbrowser/certs")
ADMIN_CERTS_DIR = "/etc/qdistro/qdbrowser/certs"

# A PEM CA bundle with hundreds of roots is ~250 KiB; cap generously but
# refuse anything that is obviously not a bundle (a multi-MB blob is a
# sign of a mistake or an attack feeding us a huge file to parse).
MAX_BUNDLE_BYTES = 4 * 1024 * 1024


def _safe_profile_name(profile: str) -> str | None:
    """Return ``profile`` if it is a safe single path component, else None.

    Profile names come from the ``--profile`` CLI arg / config and are
    interpolated into a filename. Reject anything with a path separator,
    ``..``, NUL, or that is empty — so a name can never escape the certs
    directory or smuggle in a traversal.
    """
    if not profile or not isinstance(profile, str):
        return None
    if profile in (".", ".."):
        return None
    if "/" in profile or "\\" in profile or "\x00" in profile:
        return None
    # ``os.path.basename`` collapses any sneaky residue; if it differs
    # from the input the name had path-ish structure we don't allow.
    if os.path.basename(profile) != profile:
        return None
    return profile


def _world_or_group_writable(st: os.stat_result) -> bool:
    return bool(st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))


def _unsafely_writable_dir(st: os.stat_result) -> bool:
    """True if a *directory* is group/world-writable in a way that lets a
    third party rename/replace entries. A sticky-bit directory (e.g.
    ``/tmp``, ``drwxrwxrwt``) is exempt: the sticky bit means only an
    entry's owner can rename or delete it, so a trusted-owned bundle in a
    sticky dir can't be swapped by others.
    """
    writable = bool(st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    if not writable:
        return False
    return not bool(st.st_mode & stat.S_ISVTX)


def _trusted_uids() -> set:
    """UIDs allowed to own an admin-trusted CA bundle: root and the
    current user. Anything else means a third party controls the file
    and could swap in their own CA, so we refuse to trust it machine-wide.
    """
    uids = {0}
    try:
        uids.add(os.getuid())
    except AttributeError:  # pragma: no cover - non-POSIX
        pass
    return uids


def _ancestor_chain_trusted(real_path: str) -> bool:
    """Every path component from ``real_path`` up to the filesystem root
    must be owned by a trusted uid and not group/world-writable. A
    writable *ancestor* directory lets an attacker rename or replace the
    bundle (or the certs dir) even when the bundle file itself is locked
    down, so the whole chain to ``/`` has to be checked — the standard
    "secure path" property for files the machine trusts.
    """
    trusted = _trusted_uids()
    cur = real_path
    is_leaf = True
    while True:
        try:
            st = os.lstat(cur)
        except OSError:
            return False
        if st.st_uid not in trusted:
            log.warning("ca-bundle: %s owned by untrusted uid %d; rejected",
                        cur, st.st_uid)
            return False
        # The leaf file must not be group/world-writable at all. For
        # directories, a sticky world-writable dir (e.g. /tmp) is safe
        # because only an entry's owner can replace it.
        unsafe = (_world_or_group_writable(st) if is_leaf
                  else _unsafely_writable_dir(st))
        if unsafe:
            log.warning("ca-bundle: %s is unsafely group/world-writable; "
                        "rejected", cur)
            return False
        parent = os.path.dirname(cur)
        if parent == cur:  # reached filesystem root
            return True
        cur = parent
        is_leaf = False


def _is_trusted_bundle(path: str, base_dir: str,
                       require_owner_trust: bool) -> bool:
    """Validate that ``path`` is a safe CA bundle under ``base_dir``.

    ``require_owner_trust`` is True for the admin path: the file and its
    containing directory must not be group/world-writable. For the user
    path we don't enforce that (it lives under the user's own ``$HOME``,
    which the user already controls), but we still require it to be a
    regular file inside the certs dir with a sane size.
    """
    try:
        real = os.path.realpath(path)
        real_base = os.path.realpath(base_dir)
    except OSError as exc:
        log.warning("ca-bundle realpath failed for %s: %s", path, exc)
        return False

    # Containment: the resolved path must sit inside the certs dir. Compare
    # on a separator-terminated prefix so ``/a/certs-evil`` doesn't pass for
    # base ``/a/certs``.
    prefix = real_base.rstrip(os.sep) + os.sep
    if real != real_base and not real.startswith(prefix):
        log.warning("ca-bundle path %s escapes %s; rejected", path, base_dir)
        return False

    try:
        st = os.stat(real)  # follow to the real file; we already realpath'd
    except OSError:
        return False

    if not stat.S_ISREG(st.st_mode):
        log.warning("ca-bundle %s is not a regular file; rejected", real)
        return False

    if st.st_size <= 0 or st.st_size > MAX_BUNDLE_BYTES:
        log.warning("ca-bundle %s size %d out of bounds; rejected",
                    real, st.st_size)
        return False

    if require_owner_trust:
        # The bundle, the certs dir, and every ancestor up to the certs
        # dir must be owned by a trusted uid (root or this user) and not
        # group/world-writable. Otherwise an untrusted principal could
        # inject or swap the CA the whole machine then trusts.
        if not _ancestor_chain_trusted(real):
            return False

    return True


def resolve_ca_bundle(profile: str,
                      user_dir: str | None = None,
                      admin_dir: str | None = None) -> str | None:
    """Return the path of a safe CA bundle for ``profile``, or None.

    User bundle (``<user_dir>/<profile>-ca.pem``) is preferred over the
    admin bundle (``<admin_dir>/<profile>-ca.pem``), mirroring the
    user-overlay-over-system precedence used by the pin store. Returns the
    real, validated path; returns None when nothing safe is found.
    """
    safe = _safe_profile_name(profile)
    if safe is None:
        log.warning("ca-bundle: unsafe profile name %r; skipping", profile)
        return None

    user_dir = USER_CERTS_DIR if user_dir is None else user_dir
    admin_dir = ADMIN_CERTS_DIR if admin_dir is None else admin_dir

    filename = f"{safe}-ca.pem"
    candidates = [
        (os.path.join(user_dir, filename), user_dir, False),
        (os.path.join(admin_dir, filename), admin_dir, True),
    ]
    for path, base_dir, require_owner_trust in candidates:
        if not os.path.exists(path):
            continue
        if _is_trusted_bundle(path, base_dir, require_owner_trust):
            return os.path.realpath(path)
    return None


def apply_ca_bundle_env(profile: str,
                        config=None,
                        user_dir: str | None = None,
                        admin_dir: str | None = None,
                        environ: dict | None = None) -> str | None:
    """Set ``SSL_CERT_FILE`` for ``profile`` if a safe bundle exists.

    .. warning::
       EXPERIMENTAL / UNVERIFIED. On an NSS-backed QtWebEngine build
       (including the one in this environment) Chromium ignores
       ``SSL_CERT_FILE`` and this export does **not** change page TLS
       trust — see the module docstring. The real mechanism for such
       builds is importing the CA into ``~/.pki/nssdb`` with
       ``certutil``. This function still performs the safe env export as
       forward-looking plumbing for a ``use_nss_certs=false`` build, but
       callers must not treat a non-None return as proof that the CA is
       actually trusted by QtWebEngine.

    Must be called **before** ``QApplication`` / QtWebEngine starts; the
    env var is read once by Chromium's network process. Returns the
    bundle path that was applied, or None if nothing changed.

    The feature is **off by default**. It only acts when a ``config``
    object is supplied AND ``[security] per_profile_ca_bundles`` is true
    in it. When ``config`` is None we default to OFF and do nothing —
    callers that want the env applied must pass a config with the flag
    set. When off, or when no safe bundle resolves, the environment is
    left untouched (no behaviour change).

    We never clobber an ``SSL_CERT_FILE`` the user already exported — an
    explicit env wins over the per-profile auto-resolution, the same way
    ``__main__._compose_chromium_flags`` appends rather than replaces.
    """
    environ = os.environ if environ is None else environ

    # Safe default OFF: with no config object we have no opt-in signal,
    # so we do nothing. (Previously this defaulted enabled=True, which
    # contradicted the documented default-off intent.)
    enabled = False
    if config is not None:
        try:
            enabled = bool(config.get(
                "security", "per_profile_ca_bundles", default=False))
        except Exception:
            enabled = False
        # Allow config to override the search dirs.
        if user_dir is None:
            user_dir = config.get(
                "security", "ca_bundle_user_dir", default=None)
        if admin_dir is None:
            admin_dir = config.get(
                "security", "ca_bundle_admin_dir", default=None)
    if not enabled:
        return None

    if environ.get("SSL_CERT_FILE"):
        log.info("ca-bundle: SSL_CERT_FILE already set; leaving as-is")
        return None

    bundle = resolve_ca_bundle(profile, user_dir=user_dir, admin_dir=admin_dir)
    if not bundle:
        return None

    environ["SSL_CERT_FILE"] = bundle
    log.info("qdbrowser.cert per_profile_ca profile=%s bundle=%s",
             profile, bundle)
    return bundle
