"""qdistro-pwd TPM backend + v2 vault format tests.

Pure unit; no D-Bus, no daemon. Uses MockBackend so no real TPM is
required on the host. The Tpm2ToolsBackend selection logic is also
exercised (env-var driven), without actually invoking tpm2-tools.
"""
from __future__ import annotations

import json
import os
import pytest

from qdistro_pwd_tpm import (  # type: ignore[import-not-found]
    MockBackend, NoneBackend, Tpm2ToolsBackend,
    TpmAuthFailed, TpmUnavailable,
    lookup_backend, select_backend,
)
from qdistro_pwd_vault import (  # type: ignore[import-not-found]
    VAULT_FORMAT_VERSION_TPM, VaultBadPassword, VaultDuplicate,
    VaultIntegrityError, VaultNotFound,
    add_item, create_vault, create_vault_tpm, get_item_payload,
    get_tpm_seal_meta, list_vaults, unlock_vault, unlock_vault_tpm,
    vault_path, vault_version,
)


# -- backend selection --------------------------------------------------------

def test_select_backend_explicit_mock(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_TPM_BACKEND", "mock")
    be = select_backend()
    assert isinstance(be, MockBackend)
    assert be.is_available()


def test_select_backend_explicit_none(monkeypatch):
    monkeypatch.setenv("QDISTRO_PWD_TPM_BACKEND", "none")
    be = select_backend()
    assert isinstance(be, NoneBackend)
    assert not be.is_available()


def test_select_backend_unknown_raises():
    with pytest.raises(ValueError):
        select_backend("does-not-exist")


def test_lookup_backend_returns_class(monkeypatch):
    monkeypatch.delenv("QDISTRO_PWD_TPM_BACKEND", raising=False)
    assert isinstance(lookup_backend("mock"), MockBackend)
    assert isinstance(lookup_backend("none"), NoneBackend)
    assert isinstance(lookup_backend("tpm2tools"), Tpm2ToolsBackend)


# -- mock backend -------------------------------------------------------------

def test_mock_backend_seal_unseal_roundtrip():
    be = MockBackend()
    secret = b"\x00" * 32  # representative master-key length
    pin = b"123456"
    blob = be.seal(secret, pin)
    assert isinstance(blob, dict)
    assert blob.keys() == {"salt", "nonce", "ciphertext"}
    out = be.unseal(blob, pin)
    assert out == secret


def test_mock_backend_wrong_pin_raises_auth_failed():
    be = MockBackend()
    blob = be.seal(b"\xff" * 32, b"correct-pin")
    with pytest.raises(TpmAuthFailed):
        be.unseal(blob, b"wrong-pin")


def test_mock_backend_empty_pin_allowed():
    be = MockBackend()
    blob = be.seal(b"\xaa" * 32, b"")
    assert be.unseal(blob, b"") == b"\xaa" * 32


def test_none_backend_raises_unavailable():
    be = NoneBackend()
    with pytest.raises(TpmUnavailable):
        be.seal(b"x" * 32, b"")
    with pytest.raises(TpmUnavailable):
        be.unseal({}, b"")


# -- v2 vault crypto ----------------------------------------------------------

@pytest.fixture
def vd(tmp_path) -> str:
    return str(tmp_path / "vaults")


@pytest.fixture
def mock_be() -> MockBackend:
    return MockBackend()


def test_create_vault_tpm_writes_v2_format(vd, mock_be):
    create_vault_tpm(vd, "tpmv1", b"123456", mock_be)
    body = json.loads(open(vault_path(vd, "tpmv1")).read())
    assert body["version"] == VAULT_FORMAT_VERSION_TPM
    assert body["tpm_seal"]["backend"] == "mock"
    assert "blob" in body["tpm_seal"]
    assert "kdf" not in body
    assert "kek" not in body


def test_unlock_vault_tpm_returns_master_key(vd, mock_be):
    create_vault_tpm(vd, "v", b"4321", mock_be)
    key = unlock_vault_tpm(vd, "v", b"4321", lookup_backend)
    assert isinstance(key, bytes)
    assert len(key) == 32


def test_unlock_vault_tpm_wrong_pin_raises_bad_password(vd, mock_be):
    create_vault_tpm(vd, "v", b"correct", mock_be)
    with pytest.raises(VaultBadPassword):
        unlock_vault_tpm(vd, "v", b"wrong", lookup_backend)


def test_unlock_vault_tpm_missing_raises_not_found(vd):
    with pytest.raises(VaultNotFound):
        unlock_vault_tpm(vd, "absent", b"", lookup_backend)


def test_unlock_vault_tpm_wrong_method_for_v1_raises(vd):
    create_vault(vd, "v1", b"pw")
    with pytest.raises(VaultIntegrityError):
        unlock_vault_tpm(vd, "v1", b"pw", lookup_backend)


def test_unlock_vault_v1_method_for_v2_raises(vd, mock_be):
    create_vault_tpm(vd, "v2", b"pin", mock_be)
    with pytest.raises(VaultIntegrityError):
        unlock_vault(vd, "v2", b"pin")


def test_create_vault_tpm_duplicate_rejected(vd, mock_be):
    create_vault_tpm(vd, "v", b"1", mock_be)
    with pytest.raises(VaultDuplicate):
        create_vault_tpm(vd, "v", b"1", mock_be)


def test_v2_items_roundtrip_using_master_key(vd, mock_be):
    """Adding/getting items goes through add_item / get_item_payload
    unchanged — they don't care which path produced the master key."""
    create_vault_tpm(vd, "v", b"pin", mock_be)
    key = unlock_vault_tpm(vd, "v", b"pin", lookup_backend)
    add_item(vd, "v", key, "gmail", b"secret-payload",
             pin_app_exe="/usr/bin/firefox")
    assert get_item_payload(vd, "v", key, "gmail") == b"secret-payload"


def test_v1_and_v2_vaults_coexist(vd, mock_be):
    create_vault(vd, "v1", b"pwd")
    create_vault_tpm(vd, "v2", b"pin", mock_be)
    # list_vaults sees both
    assert list_vaults(vd) == ["v1", "v2"]
    assert vault_version(vd, "v1") == 1
    assert vault_version(vd, "v2") == VAULT_FORMAT_VERSION_TPM


def test_get_tpm_seal_meta_for_v1_returns_empty(vd):
    create_vault(vd, "v1", b"pwd")
    assert get_tpm_seal_meta(vd, "v1") == {}


def test_get_tpm_seal_meta_for_v2_returns_backend(vd, mock_be):
    create_vault_tpm(vd, "v2", b"pin", mock_be)
    meta = get_tpm_seal_meta(vd, "v2")
    # Phase-8.5: meta also exposes the PCR selection (empty string
    # when the seal didn't bind to PCRs, as in this test).
    assert meta["backend"] == "mock"
    assert "pcrs" in meta


def test_v2_unlock_with_unavailable_backend_raises_integrity(vd, mock_be,
                                                              monkeypatch):
    """When the vault was sealed with a backend that isn't reachable on
    this host, unseal returns VaultIntegrityError so the caller sees it
    as 'this vault file is not usable here' rather than 'wrong PIN'."""
    create_vault_tpm(vd, "v", b"1234", mock_be)
    body = json.loads(open(vault_path(vd, "v")).read())
    body["tpm_seal"]["backend"] = "none"
    open(vault_path(vd, "v"), "w").write(json.dumps(body))
    with pytest.raises(VaultIntegrityError):
        unlock_vault_tpm(vd, "v", b"1234", lookup_backend)


def test_v2_malformed_seal_raises_integrity(vd, mock_be):
    create_vault_tpm(vd, "v", b"x", mock_be)
    body = json.loads(open(vault_path(vd, "v")).read())
    del body["tpm_seal"]["blob"]
    open(vault_path(vd, "v"), "w").write(json.dumps(body))
    with pytest.raises(VaultIntegrityError):
        unlock_vault_tpm(vd, "v", b"x", lookup_backend)


def test_v2_swap_tag_in_aad_fails_integrity(vd, mock_be):
    """AAD binding still applies in v2 — items are encrypted with the
    master key + AAD(vault, tag), independent of how the master key
    was sealed."""
    create_vault_tpm(vd, "v", b"x", mock_be)
    key = unlock_vault_tpm(vd, "v", b"x", lookup_backend)
    add_item(vd, "v", key, "real", b"v")
    body = json.loads(open(vault_path(vd, "v")).read())
    body["items"][0]["tag"] = "fake"
    open(vault_path(vd, "v"), "w").write(json.dumps(body))
    with pytest.raises(VaultIntegrityError):
        get_item_payload(vd, "v", key, "fake")


def test_v2_vault_file_mode_is_600(vd, mock_be):
    create_vault_tpm(vd, "v", b"x", mock_be)
    assert (os.stat(vault_path(vd, "v")).st_mode & 0o777) == 0o600


def test_unsupported_version_rejected(vd, mock_be):
    create_vault_tpm(vd, "v", b"x", mock_be)
    body = json.loads(open(vault_path(vd, "v")).read())
    body["version"] = 999
    open(vault_path(vd, "v"), "w").write(json.dumps(body))
    with pytest.raises(VaultIntegrityError):
        vault_version(vd, "v")


# -- PIN is never placed on tpm2-tools argv (leak via /proc/<pid>/cmdline) -----
#
# The Tpm2ToolsBackend must pass the PIN to tpm2_create/tpm2_unseal via stdin
# (``-p file:-``), NOT ``-p hex:<pin>`` on argv. These tests stub ``_run`` so
# no real TPM is needed: they capture every invocation and assert the PIN
# bytes appear only in stdin, never in argv.

class _CapturingTpm(Tpm2ToolsBackend):
    """Tpm2ToolsBackend whose ``_run`` records calls and fakes the TPM.

    ``tpm2_readpublic`` succeeds (primary already present), and
    ``tpm2_create``/``tpm2_load`` write the output files the caller reads
    back, so seal()/unseal() complete without a TPM.
    """

    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    def _run(self, *args, input_bytes=None, cwd=None):
        self.calls.append({"argv": list(args), "input": input_bytes})
        # Write any requested output files so the caller's reads succeed.
        for i, a in enumerate(args):
            if a in ("-r", "-u", "-c") and i + 1 < len(args):
                try:
                    with open(args[i + 1], "wb") as f:
                        f.write(b"\x00")
                except OSError:
                    pass
        # tpm2_unseal returns the sealed secret on stdout.
        if args and args[0] == "tpm2_unseal":
            return 0, b"secret", b""
        return 0, b"", b""


def _assert_pin_off_argv(calls, pin: bytes):
    hexpin = pin.hex()
    expected_stdin = b"hex:" + hexpin.encode("ascii")
    saw_file_stdin = False
    for c in calls:
        argv_str = " ".join(c["argv"])
        assert "hex:" + hexpin not in argv_str, f"PIN on argv: {c['argv']}"
        assert pin.decode("latin-1") not in argv_str, f"PIN on argv: {c['argv']}"
        if "file:-" in argv_str or "+file:-" in argv_str:
            saw_file_stdin = True
            # Delivered on stdin as ``hex:<pin.hex()>`` so tpm2-tools decodes it
            # back to exactly ``pin`` (byte-identical to the old argv path).
            assert c["input"] == expected_stdin, "PIN must be hex-encoded on stdin"
    assert saw_file_stdin, "expected a tpm2 call to read the PIN from stdin"


def test_seal_passes_pin_on_stdin_not_argv():
    be = _CapturingTpm()
    pin = b"987654"
    be.seal(b"master-key-bytes", pin)
    create_calls = [c for c in be.calls if c["argv"][:1] == ["tpm2_create"]]
    assert create_calls, "tpm2_create was not invoked"
    _assert_pin_off_argv(be.calls, pin)


def test_unseal_passes_pin_on_stdin_not_argv():
    be = _CapturingTpm()
    pin = b"987654"
    blob = {"priv": _b64("x"), "pub": _b64("y"), "auth_set": True}
    be.unseal(blob, pin)
    unseal_calls = [c for c in be.calls if c["argv"][:1] == ["tpm2_unseal"]]
    assert unseal_calls, "tpm2_unseal was not invoked"
    _assert_pin_off_argv(be.calls, pin)


def _b64(s: str) -> str:
    import base64
    return base64.b64encode(s.encode()).decode("ascii")


# The stdin payload must decode (as tpm2-tools decodes ``hex:``) back to the
# EXACT PIN bytes — including PINs that would break a raw ``file:-`` feed
# (trailing CR/LF stripped, or an auth-prefix reinterpreted by tpm2-tools).
@pytest.mark.parametrize("pin", [
    b"987654",
    b"123456\n",       # raw file:- would strip the trailing \n
    b"abc\r\n",        # raw file:- would strip trailing \r\n
    b"hex:313233",     # raw file:- would reinterpret the hex: prefix -> b"123"
    b"str:abc",        # raw file:- would reinterpret the str: prefix -> b"abc"
    b"file:/tmp/x",    # raw file:- would attempt nested file read
    b"\x00\x01\xff",   # arbitrary bytes incl NUL
])
def test_pin_stdin_is_byte_exact(pin):
    from qdistro_pwd_tpm import _pin_stdin  # type: ignore[import-not-found]
    payload = _pin_stdin(pin)
    assert payload.startswith(b"hex:")
    # tpm2-tools decodes ``hex:<h>`` by hex-decoding <h> -> must equal pin.
    decoded = bytes.fromhex(payload[len(b"hex:"):].decode("ascii"))
    assert decoded == pin
    # And the raw PIN never appears verbatim in the payload for the risky cases.
    assert payload != pin
