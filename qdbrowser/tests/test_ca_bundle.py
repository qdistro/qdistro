"""Per-profile CA bundle resolution and env wiring.

Pure-Python — no QWebEngineProfile is spun up. We exercise the
resolution/safety logic and the ``SSL_CERT_FILE`` env application that
``__main__`` performs before QApplication.
"""

import os
from datetime import UTC

import pytest


# A real (self-signed) PEM so the size check sees plausible bundle bytes.
def _make_ca_pem():
    pytest.importorskip("cryptography")
    from datetime import datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None),
                       critical=True)
        .sign(private_key=key, algorithm=hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


@pytest.fixture(scope="module")
def ca_pem():
    return _make_ca_pem()


def _write_bundle(directory, profile, pem):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{profile}-ca.pem")
    with open(path, "wb") as f:
        f.write(pem)
    return path


# ---------------------------------------------------------------------------
# resolve_ca_bundle
# ---------------------------------------------------------------------------


def test_resolve_user_bundle_found(tmp_path, ca_pem):
    from qdbrowser.ca_bundle import resolve_ca_bundle
    user = tmp_path / "user-certs"
    written = _write_bundle(str(user), "work", ca_pem)
    got = resolve_ca_bundle("work", user_dir=str(user),
                            admin_dir=str(tmp_path / "admin"))
    assert got == os.path.realpath(written)


def test_resolve_user_wins_over_admin(tmp_path, ca_pem):
    from qdbrowser.ca_bundle import resolve_ca_bundle
    user = tmp_path / "user-certs"
    admin = tmp_path / "admin-certs"
    user_path = _write_bundle(str(user), "work", ca_pem)
    _write_bundle(str(admin), "work", ca_pem)
    os.chmod(admin, 0o755)
    got = resolve_ca_bundle("work", user_dir=str(user), admin_dir=str(admin))
    assert got == os.path.realpath(user_path)


def test_resolve_admin_used_when_no_user(tmp_path, ca_pem):
    from qdbrowser.ca_bundle import resolve_ca_bundle
    admin = tmp_path / "admin-certs"
    admin.mkdir()
    os.chmod(admin, 0o755)
    written = _write_bundle(str(admin), "work", ca_pem)
    os.chmod(written, 0o644)
    got = resolve_ca_bundle("work", user_dir=str(tmp_path / "none"),
                            admin_dir=str(admin))
    assert got == os.path.realpath(written)


def test_resolve_absent_bundle_returns_none(tmp_path):
    from qdbrowser.ca_bundle import resolve_ca_bundle
    got = resolve_ca_bundle("personal", user_dir=str(tmp_path / "u"),
                            admin_dir=str(tmp_path / "a"))
    assert got is None


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", ".", "", "a\x00b",
                                 "a\\b"])
def test_resolve_rejects_unsafe_profile_names(tmp_path, bad):
    from qdbrowser.ca_bundle import resolve_ca_bundle
    assert resolve_ca_bundle(bad, user_dir=str(tmp_path),
                             admin_dir=str(tmp_path)) is None


def test_resolve_rejects_symlink_escaping_dir(tmp_path, ca_pem):
    """A symlink in the certs dir pointing outside it must be rejected."""
    from qdbrowser.ca_bundle import resolve_ca_bundle
    outside = tmp_path / "outside-ca.pem"
    outside.write_bytes(ca_pem)
    user = tmp_path / "user-certs"
    user.mkdir()
    link = user / "work-ca.pem"
    os.symlink(str(outside), str(link))
    got = resolve_ca_bundle("work", user_dir=str(user),
                            admin_dir=str(tmp_path / "a"))
    assert got is None


def test_resolve_rejects_non_regular_file(tmp_path):
    from qdbrowser.ca_bundle import resolve_ca_bundle
    user = tmp_path / "user-certs"
    user.mkdir()
    fifo = user / "work-ca.pem"
    try:
        os.mkfifo(str(fifo))
    except (AttributeError, OSError):
        pytest.skip("mkfifo unavailable")
    got = resolve_ca_bundle("work", user_dir=str(user),
                            admin_dir=str(tmp_path / "a"))
    assert got is None


def test_resolve_rejects_oversized_bundle(tmp_path):
    from qdbrowser import ca_bundle
    user = tmp_path / "user-certs"
    user.mkdir()
    big = user / "work-ca.pem"
    big.write_bytes(b"x" * (ca_bundle.MAX_BUNDLE_BYTES + 1))
    got = ca_bundle.resolve_ca_bundle("work", user_dir=str(user),
                                      admin_dir=str(tmp_path / "a"))
    assert got is None


def test_resolve_rejects_empty_bundle(tmp_path):
    from qdbrowser.ca_bundle import resolve_ca_bundle
    user = tmp_path / "user-certs"
    user.mkdir()
    (user / "work-ca.pem").write_bytes(b"")
    got = resolve_ca_bundle("work", user_dir=str(user),
                            admin_dir=str(tmp_path / "a"))
    assert got is None


def test_admin_bundle_world_writable_rejected(tmp_path, ca_pem):
    from qdbrowser.ca_bundle import resolve_ca_bundle
    admin = tmp_path / "admin-certs"
    admin.mkdir()
    os.chmod(admin, 0o755)
    path = _write_bundle(str(admin), "work", ca_pem)
    os.chmod(path, 0o666)  # world-writable file
    got = resolve_ca_bundle("work", user_dir=str(tmp_path / "u"),
                            admin_dir=str(admin))
    assert got is None


def test_admin_dir_group_writable_rejected(tmp_path, ca_pem):
    from qdbrowser.ca_bundle import resolve_ca_bundle
    admin = tmp_path / "admin-certs"
    admin.mkdir()
    path = _write_bundle(str(admin), "work", ca_pem)
    os.chmod(path, 0o644)
    os.chmod(admin, 0o775)  # group-writable dir
    got = resolve_ca_bundle("work", user_dir=str(tmp_path / "u"),
                            admin_dir=str(admin))
    assert got is None


def test_admin_bundle_untrusted_owner_rejected(tmp_path, ca_pem,
                                               monkeypatch):
    """An admin bundle owned by neither root nor the current user is
    rejected (that third party could swap in their own CA)."""
    from qdbrowser import ca_bundle
    admin = tmp_path / "admin-certs"
    admin.mkdir()
    os.chmod(admin, 0o755)
    path = _write_bundle(str(admin), "work", ca_pem)
    os.chmod(path, 0o644)
    # Pretend only root (uid 0) is trusted, while the files are owned by
    # the test user — simulating a bundle owned by some other account.
    monkeypatch.setattr(ca_bundle, "_trusted_uids", lambda: {0})
    got = ca_bundle.resolve_ca_bundle("work", user_dir=str(tmp_path / "u"),
                                      admin_dir=str(admin))
    assert got is None


def test_admin_writable_parent_rejected(tmp_path, ca_pem):
    """A group/world-writable parent dir above the certs dir is rejected
    even when the certs dir and file themselves are locked down."""
    from qdbrowser.ca_bundle import resolve_ca_bundle
    parent = tmp_path / "parent"
    parent.mkdir()
    admin = parent / "certs"
    admin.mkdir()
    path = _write_bundle(str(admin), "work", ca_pem)
    os.chmod(path, 0o644)
    os.chmod(admin, 0o755)
    os.chmod(parent, 0o777)  # world-writable ancestor
    got = resolve_ca_bundle("work", user_dir=str(tmp_path / "u"),
                            admin_dir=str(admin))
    assert got is None


def test_user_bundle_writable_is_allowed(tmp_path, ca_pem):
    """The user path is under the user's own $HOME; writability there is
    not a trust problem, so a 0644 user bundle is accepted."""
    from qdbrowser.ca_bundle import resolve_ca_bundle
    user = tmp_path / "user-certs"
    path = _write_bundle(str(user), "work", ca_pem)
    os.chmod(path, 0o644)
    got = resolve_ca_bundle("work", user_dir=str(user),
                            admin_dir=str(tmp_path / "a"))
    assert got == os.path.realpath(path)


# ---------------------------------------------------------------------------
# apply_ca_bundle_env
# ---------------------------------------------------------------------------


class _Cfg:
    def __init__(self, **vals):
        self._vals = vals

    def get(self, *keys, default=None):
        return self._vals.get(keys[-1], default)


def test_apply_sets_env_when_enabled(tmp_path, ca_pem):
    from qdbrowser.ca_bundle import apply_ca_bundle_env
    user = tmp_path / "user-certs"
    written = _write_bundle(str(user), "work", ca_pem)
    env = {}
    cfg = _Cfg(per_profile_ca_bundles=True,
               ca_bundle_user_dir=str(user),
               ca_bundle_admin_dir=str(tmp_path / "a"))
    applied = apply_ca_bundle_env("work", config=cfg, environ=env)
    assert applied == os.path.realpath(written)
    assert env["SSL_CERT_FILE"] == os.path.realpath(written)


def test_apply_disabled_by_default(tmp_path, ca_pem):
    from qdbrowser.ca_bundle import apply_ca_bundle_env
    user = tmp_path / "user-certs"
    _write_bundle(str(user), "work", ca_pem)
    env = {}
    cfg = _Cfg(per_profile_ca_bundles=False,
               ca_bundle_user_dir=str(user))
    applied = apply_ca_bundle_env("work", config=cfg, environ=env)
    assert applied is None
    assert "SSL_CERT_FILE" not in env


def test_apply_default_off_when_no_config(tmp_path, ca_pem, monkeypatch):
    """With no config object the helper must be OFF and never touch the
    env, even when a perfectly valid bundle exists in the default search
    dirs. (Regression: it previously defaulted enabled=True.)"""
    from qdbrowser import ca_bundle
    user = tmp_path / "user-certs"
    _write_bundle(str(user), "work", ca_pem)
    # Point the module-level default dirs at our valid bundle so that,
    # if the helper were wrongly enabled, it WOULD resolve and export.
    monkeypatch.setattr(ca_bundle, "USER_CERTS_DIR", str(user))
    monkeypatch.setattr(ca_bundle, "ADMIN_CERTS_DIR", str(tmp_path / "a"))
    env = {}
    applied = ca_bundle.apply_ca_bundle_env("work", config=None, environ=env)
    assert applied is None
    assert "SSL_CERT_FILE" not in env


def test_apply_absent_bundle_no_env_change(tmp_path):
    from qdbrowser.ca_bundle import apply_ca_bundle_env
    env = {}
    cfg = _Cfg(per_profile_ca_bundles=True,
               ca_bundle_user_dir=str(tmp_path / "u"),
               ca_bundle_admin_dir=str(tmp_path / "a"))
    applied = apply_ca_bundle_env("personal", config=cfg, environ=env)
    assert applied is None
    assert "SSL_CERT_FILE" not in env


def test_apply_does_not_clobber_existing_env(tmp_path, ca_pem):
    from qdbrowser.ca_bundle import apply_ca_bundle_env
    user = tmp_path / "user-certs"
    _write_bundle(str(user), "work", ca_pem)
    env = {"SSL_CERT_FILE": "/preset/bundle.pem"}
    cfg = _Cfg(per_profile_ca_bundles=True,
               ca_bundle_user_dir=str(user),
               ca_bundle_admin_dir=str(tmp_path / "a"))
    applied = apply_ca_bundle_env("work", config=cfg, environ=env)
    assert applied is None
    assert env["SSL_CERT_FILE"] == "/preset/bundle.pem"


def test_apply_unsafe_profile_no_env_change(tmp_path):
    from qdbrowser.ca_bundle import apply_ca_bundle_env
    env = {}
    cfg = _Cfg(per_profile_ca_bundles=True,
               ca_bundle_user_dir=str(tmp_path),
               ca_bundle_admin_dir=str(tmp_path))
    applied = apply_ca_bundle_env("../../etc", config=cfg, environ=env)
    assert applied is None
    assert "SSL_CERT_FILE" not in env
