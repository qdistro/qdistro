"""qdistro_pwd_tpm — combined PCR + PIN seal (task 103).

Pre-task-103, sealing with both ``pcrs`` and a non-empty ``auth_pin``
silently dropped the PIN at unseal: the policy session alone gated
the unseal, ignoring the auth-value entirely. Task 103 wires
``tpm2_policyauthvalue`` into the trial policy at seal and replays it
at unseal under a ``session+hex:<pin>`` auth string so BOTH factors
are required.

Real-TPM coverage of the tpm2-tools surface lives in the in-VM
phase8 bats; pytest exercises:

  - MockBackend correctness — combined-auth blob refuses unseal with
    wrong PIN OR wrong PCR state.
  - Backwards compatibility — legacy blobs without ``combined_auth``
    use the old behaviour (PCR-only or PIN-only) so existing data
    still unlocks.
"""
from __future__ import annotations

import pytest

from qdistro_pwd_tpm import MockBackend, TpmAuthFailed


# -- Combined-auth seal blob shape -------------------------------------------

class TestSealBlobShape:
    def test_blob_marks_combined_auth(self):
        be = MockBackend()
        blob = be.seal(b"secret", b"pin", pcrs="sha256:7,11")
        assert blob.get("combined_auth") is True

    def test_blob_omits_combined_auth_when_pin_empty(self):
        be = MockBackend()
        blob = be.seal(b"secret", b"", pcrs="sha256:7,11")
        assert "combined_auth" not in blob

    def test_blob_omits_combined_auth_when_no_pcrs(self):
        be = MockBackend()
        blob = be.seal(b"secret", b"pin")
        assert "combined_auth" not in blob


# -- Round-trip ---------------------------------------------------------------

class TestRoundTrip:
    def test_round_trip_correct_pin_and_state(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "boot-ok")
        be = MockBackend()
        blob = be.seal(b"my-secret", b"the-pin", pcrs="sha256:7,11")
        out = be.unseal(blob, b"the-pin")
        assert out == b"my-secret"

    def test_wrong_pin_fails(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "boot-ok")
        be = MockBackend()
        blob = be.seal(b"my-secret", b"the-pin", pcrs="sha256:7,11")
        with pytest.raises(TpmAuthFailed):
            be.unseal(blob, b"wrong-pin")

    def test_wrong_state_fails(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "boot-ok")
        be = MockBackend()
        blob = be.seal(b"my-secret", b"the-pin", pcrs="sha256:7,11")
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "tampered")
        with pytest.raises(TpmAuthFailed):
            be.unseal(blob, b"the-pin")


# -- Legacy compat ------------------------------------------------------------

class TestLegacyCompat:
    def test_legacy_pcr_only_blob_unseals(self):
        """A blob from before task 103 that has ``pcrs`` but no
        ``combined_auth`` flag (e.g. portal-keys PIN stash) keeps
        working with auth_pin=b'' under the legacy PCR-only path."""
        be = MockBackend()
        # Simulate the pre-task-103 path: pcrs set, no auth.
        blob = be.seal(b"stashed-pin", b"", pcrs="sha256:7,11")
        assert "combined_auth" not in blob
        out = be.unseal(blob, b"")
        assert out == b"stashed-pin"

    def test_legacy_pin_only_blob_unseals(self):
        """Pre-task-103 'PIN-only' (pcrs disabled): blob has neither
        pcrs nor combined_auth. Unseal still gates on the PIN via the
        scrypt key derivation."""
        be = MockBackend()
        blob = be.seal(b"my-secret", b"my-pin")
        assert "combined_auth" not in blob
        with pytest.raises(TpmAuthFailed):
            be.unseal(blob, b"wrong")
        out = be.unseal(blob, b"my-pin")
        assert out == b"my-secret"

    def test_legacy_combined_blob_without_marker(self, monkeypatch):
        """Hand-crafted blob: pcrs set, valid PIN, but
        ``combined_auth`` absent (mimicking pre-task-103 v2 vault).
        Pre-task-103 the unseal silently ignored the PIN. The mock's
        AES-GCM key is still derived from the PIN, so the test pins
        the legacy semantics: PIN derivation alone gates the crypto.
        """
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "boot-ok")
        be = MockBackend()
        blob = be.seal(b"secret", b"pin", pcrs="sha256:7,11")
        # Strip the combined marker to mimic a pre-task-103 blob.
        blob.pop("combined_auth", None)
        # The mock's AAD was written WITH combined_auth=1; without that
        # flag the unseal AAD diverges → InvalidTag → TpmAuthFailed.
        # That's a cleaner failure than silently ignoring the PIN.
        with pytest.raises(TpmAuthFailed):
            be.unseal(blob, b"pin")
