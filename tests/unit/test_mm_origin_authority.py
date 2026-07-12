"""Fail-closed tests for paired machine/trust-domain authorization (R2)."""
from __future__ import annotations

import pytest

from multimachine.origin_authority import (
    ATTACH_UI,
    RECEIVE_INPUT,
    OriginAuthorizationError,
    OriginGrant,
    StaticOriginAuthority,
)
from multimachine.rdp_client_wrapper import StreamSpec


def _spec(**over) -> StreamSpec:
    raw = dict(
        origin="vm-a", stream_id="sid-a", generation=51,
        app_id="qdistro.mm.vm-a.shared", instance_id="vm-a-shared-1",
        rdp_host="10.0.2.2", rdp_port=5555, width=640, height=400,
        allow_input=1,
    )
    raw.update(over)
    return StreamSpec(**raw)


def _authority(*, capabilities=(ATTACH_UI, RECEIVE_INPUT), generation=51):
    return StaticOriginAuthority([
        OriginGrant("vm-a", "owner-machines", generation,
                    frozenset(capabilities)),
    ])


class TestOriginGrantConfig:
    def test_config_snapshot_parses_exact_grant(self):
        authority = StaticOriginAuthority.from_config([{
            "machine_id": "vm-a",
            "trust_domain_id": "owner-machines",
            "generation": 51,
            "capabilities": [ATTACH_UI, RECEIVE_INPUT],
        }])
        grant = authority.authorize(_spec())
        assert grant.machine_id == "vm-a"
        assert grant.trust_domain_id == "owner-machines"
        assert grant.capabilities == frozenset({ATTACH_UI, RECEIVE_INPUT})

    @pytest.mark.parametrize("raw", [
        None,
        [],
        ["vm-a"],
        [{"machine_id": "vm-a", "trust_domain_id": "owner-machines",
          "generation": True, "capabilities": [ATTACH_UI]}],
        [{"machine_id": "vm-a", "trust_domain_id": "owner machines",
          "generation": 51, "capabilities": [ATTACH_UI]}],
        [{"machine_id": "vm-a", "trust_domain_id": "owner-machines",
          "generation": 51, "capabilities": [ATTACH_UI, "admin"]}],
    ])
    def test_malformed_or_unknown_config_fails_closed(self, raw):
        with pytest.raises(OriginAuthorizationError):
            StaticOriginAuthority.from_config(raw)

    def test_duplicate_machine_grant_rejected(self):
        grant = OriginGrant("vm-a", "owner-machines", 51,
                            frozenset({ATTACH_UI}))
        with pytest.raises(OriginAuthorizationError, match="duplicate"):
            StaticOriginAuthority([grant, grant])


class TestOriginAuthorization:
    def test_unpaired_origin_rejected(self):
        with pytest.raises(OriginAuthorizationError, match="not paired"):
            _authority().authorize(_spec(
                origin="vm-z", app_id="qdistro.mm.vm-z.shared"))

    def test_stale_dock_generation_rejected(self):
        with pytest.raises(OriginAuthorizationError, match="generation"):
            _authority(generation=52).authorize(_spec())

    def test_ui_attachment_is_mandatory(self):
        authority = StaticOriginAuthority([
            OriginGrant("vm-a", "owner-machines", 51,
                        frozenset({RECEIVE_INPUT})),
        ])
        with pytest.raises(OriginAuthorizationError, match=ATTACH_UI):
            authority.authorize(_spec())

    def test_input_grant_is_separate_from_read_only_attachment(self):
        authority = _authority(capabilities=(ATTACH_UI,))
        read_only = authority.authorize(_spec(allow_input=0))
        assert read_only.trust_domain_id == "owner-machines"
        with pytest.raises(OriginAuthorizationError, match=RECEIVE_INPUT):
            authority.authorize(_spec(allow_input=1))
