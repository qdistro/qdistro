"""qdistro_pwd_tpm — PCR-bound seal (Phase-8.5).

The MockBackend simulates PCR binding via AES-GCM AAD that includes
the current ``QDISTRO_PWD_TPM_MOCK_PCR_STATE`` env value at seal +
unseal time. Mismatched env → unseal fails with ``TpmAuthFailed``.

Real-TPM coverage (Tpm2ToolsBackend with tpm2_policypcr) lives in
the in-VM s61 phase8 bats — pytest doesn't have a TPM.
"""
from __future__ import annotations

import os

import pytest

from qdistro_pwd_tpm import (
    DEFAULT_PCRS, MockBackend, NoneBackend, TpmAuthFailed, TpmUnavailable,
    configured_pcrs,
)


# -- configured_pcrs ----------------------------------------------------

class TestConfiguredPcrs:
    def test_default_when_unset(self):
        assert configured_pcrs(env={}) == DEFAULT_PCRS
        assert DEFAULT_PCRS == "sha256:7,11"

    def test_env_override(self):
        assert configured_pcrs(env={"QDISTRO_PWD_TPM_PCRS": "sha256:0,2,4"}) \
            == "sha256:0,2,4"

    def test_empty_disables_binding(self):
        assert configured_pcrs(env={"QDISTRO_PWD_TPM_PCRS": ""}) is None

    def test_whitespace_stripped(self):
        assert configured_pcrs(env={"QDISTRO_PWD_TPM_PCRS": "  sha256:7  "}) \
            == "sha256:7"


# -- MockBackend PCR binding -------------------------------------------

class TestMockPcrBinding:
    def test_seal_records_pcrs_in_blob(self):
        be = MockBackend()
        blob = be.seal(b"secret", b"pin", pcrs="sha256:7,11")
        assert blob["pcrs"] == "sha256:7,11"

    def test_seal_without_pcrs_omits_field(self):
        be = MockBackend()
        blob = be.seal(b"secret", b"pin")
        assert "pcrs" not in blob

    def test_round_trip_same_state(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "boot-ok")
        be = MockBackend()
        blob = be.seal(b"hello", b"pin", pcrs="sha256:7,11")
        out = be.unseal(blob, b"pin")
        assert out == b"hello"

    def test_unseal_with_changed_state_fails(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "boot-ok")
        be = MockBackend()
        blob = be.seal(b"hello", b"pin", pcrs="sha256:7,11")
        # Simulate boot-tamper: PCR state changed between seal and unseal.
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "tampered")
        with pytest.raises(TpmAuthFailed):
            be.unseal(blob, b"pin")

    def test_unseal_with_different_pcrs_fails(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "boot-ok")
        be = MockBackend()
        blob = be.seal(b"hello", b"pin", pcrs="sha256:7,11")
        # Mutate the recorded PCR selection — unseal AAD diverges.
        blob["pcrs"] = "sha256:0,1"
        with pytest.raises(TpmAuthFailed):
            be.unseal(blob, b"pin")

    def test_seal_without_pcr_unseal_without_pcr(self):
        """Backwards compat: legacy seals (no pcrs field) keep working."""
        be = MockBackend()
        blob = be.seal(b"legacy", b"pin")
        assert "pcrs" not in blob
        out = be.unseal(blob, b"pin")
        assert out == b"legacy"

    def test_pin_still_validated(self, monkeypatch):
        monkeypatch.setenv("QDISTRO_PWD_TPM_MOCK_PCR_STATE", "x")
        be = MockBackend()
        blob = be.seal(b"hello", b"correct-pin", pcrs="sha256:7,11")
        with pytest.raises(TpmAuthFailed):
            be.unseal(blob, b"wrong-pin")


# -- NoneBackend grew the pcrs kwarg ------------------------------------

class TestNoneBackendKwarg:
    def test_seal_accepts_pcrs_kwarg(self):
        be = NoneBackend()
        with pytest.raises(TpmUnavailable):
            be.seal(b"x", b"y", pcrs="sha256:7")
