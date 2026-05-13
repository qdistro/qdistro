"""qdistro_pwd_pinstash — TPM-sealed PIN stash for portal-keys
auto-unlock at login.

Drives the helper directly against the MockBackend so the tests
don't need a TPM. Real-TPM coverage lives in the in-VM s61
phase8 bats.
"""
from __future__ import annotations

import json
import os

import pytest

from qdistro_pwd_pinstash import (
    PIN_STASH_FORMAT_VERSION, PinStashError,
    stash_meta, stash_pin, stash_present, unseal_pin,
)
from qdistro_pwd_tpm import (
    MockBackend, NoneBackend, TpmAuthFailed, TpmUnavailable,
)


def _backend_lookup_factory(backend_by_name):
    def lookup(name):
        if name not in backend_by_name:
            raise ValueError(f"unknown backend {name!r}")
        return backend_by_name[name]
    return lookup


@pytest.fixture
def stash_path(tmp_path):
    return str(tmp_path / "portal-keys-pin.tpm")


# -- stash_pin ----------------------------------------------------------

class TestStashPin:
    def test_writes_file_with_0600_mode(self, stash_path):
        meta = stash_pin(b"hunter2", MockBackend(), path=stash_path)
        assert os.path.exists(stash_path)
        st = os.stat(stash_path)
        # Owner read/write only.
        assert (st.st_mode & 0o777) == 0o600
        assert meta["backend"] == "mock"
        assert meta["format_version"] == PIN_STASH_FORMAT_VERSION

    def test_atomic_write_no_partial_file(self, stash_path):
        # Pre-fill with garbage; stash_pin must atomically replace.
        with open(stash_path, "wb") as f:
            f.write(b"OLD-CORRUPT-CONTENT\n")
        stash_pin(b"newpin", MockBackend(), path=stash_path)
        # No leftover .new tmpfile.
        assert not os.path.exists(stash_path + ".new")
        # New content parses fine.
        with open(stash_path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
        assert doc["format_version"] == PIN_STASH_FORMAT_VERSION

    def test_empty_pin_rejected(self, stash_path):
        with pytest.raises(ValueError):
            stash_pin(b"", MockBackend(), path=stash_path)

    def test_overlong_pin_rejected(self, stash_path):
        with pytest.raises(ValueError):
            stash_pin(b"x" * 200, MockBackend(), path=stash_path)

    def test_non_bytes_rejected(self, stash_path):
        with pytest.raises(ValueError):
            stash_pin("plain str", MockBackend(), path=stash_path)  # type: ignore[arg-type]

    def test_unavailable_backend_raises(self, stash_path):
        with pytest.raises(TpmUnavailable):
            stash_pin(b"pin", NoneBackend(), path=stash_path)


# -- unseal_pin ---------------------------------------------------------

class TestUnsealPin:
    def test_roundtrip(self, stash_path):
        backend = MockBackend()
        stash_pin(b"my-pin-7", backend, path=stash_path)
        lookup = _backend_lookup_factory({"mock": backend})
        out = unseal_pin(lookup, path=stash_path)
        assert out == b"my-pin-7"

    def test_missing_file(self, stash_path):
        lookup = _backend_lookup_factory({"mock": MockBackend()})
        with pytest.raises(PinStashError):
            unseal_pin(lookup, path=stash_path)

    def test_corrupt_file(self, stash_path):
        with open(stash_path, "w") as f:
            f.write("{not json")
        lookup = _backend_lookup_factory({"mock": MockBackend()})
        with pytest.raises(PinStashError):
            unseal_pin(lookup, path=stash_path)

    def test_wrong_format_version(self, stash_path):
        bad = {
            "format_version": 999,
            "tpm_seal": {"backend": "mock"},
            "created_at_unix": 0,
        }
        with open(stash_path, "w") as f:
            json.dump(bad, f)
        lookup = _backend_lookup_factory({"mock": MockBackend()})
        with pytest.raises(PinStashError):
            unseal_pin(lookup, path=stash_path)

    def test_tampered_blob_raises_auth_failed(self, stash_path):
        backend = MockBackend()
        stash_pin(b"pin1", backend, path=stash_path)
        with open(stash_path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
        # Flip a byte in the seal ciphertext.
        ct = doc["tpm_seal"]["ciphertext"]
        # The ciphertext is base64 — re-decode, flip bit, re-encode.
        import base64
        raw = bytearray(base64.b64decode(ct))
        raw[-1] ^= 0xFF
        doc["tpm_seal"]["ciphertext"] = base64.b64encode(bytes(raw)).decode("ascii")
        with open(stash_path, "w") as f:
            json.dump(doc, f)
        lookup = _backend_lookup_factory({"mock": backend})
        with pytest.raises(TpmAuthFailed):
            unseal_pin(lookup, path=stash_path)

    def test_unknown_backend_in_blob(self, stash_path):
        # Force a bogus backend discriminator in the on-disk seal.
        backend = MockBackend()
        stash_pin(b"x", backend, path=stash_path)
        with open(stash_path, "rb") as f:
            doc = json.loads(f.read().decode("utf-8"))
        doc["tpm_seal"]["backend"] = "no-such-backend"
        with open(stash_path, "w") as f:
            json.dump(doc, f)
        lookup = _backend_lookup_factory({"mock": backend})
        with pytest.raises(ValueError):
            unseal_pin(lookup, path=stash_path)


# -- stash_meta + stash_present -----------------------------------------

class TestMeta:
    def test_absent(self, stash_path):
        assert stash_present(path=stash_path) is False
        m = stash_meta(path=stash_path)
        assert m == {"present": False, "backend": "", "created_at_unix": 0}

    def test_present(self, stash_path):
        stash_pin(b"y", MockBackend(), path=stash_path)
        assert stash_present(path=stash_path) is True
        m = stash_meta(path=stash_path)
        assert m["present"] is True
        assert m["backend"] == "mock"
        assert m["created_at_unix"] > 0

    def test_corrupt_returns_absent(self, stash_path):
        with open(stash_path, "w") as f:
            f.write("garbage")
        # Corrupt parsing → returns the absent shape.
        m = stash_meta(path=stash_path)
        assert m["backend"] == ""
        assert m["created_at_unix"] == 0
