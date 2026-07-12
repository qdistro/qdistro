"""Paired-origin authority for the multi-machine shared-GUI broker.

The cryptographic pairing/control-plane service is deliberately outside the
viewer broker.  It hands the session launcher an inherited configuration fd;
this module validates the vouched facts from that trusted channel and turns
them into the narrow authorization decision needed to attach a remote window.

Nothing here trusts a secctx string, window title, source ``Announce`` field,
or network address as proof of pairing.  Those values remain correlation and
anti-mixup inputs.  The authority only accepts a configured machine grant for
the exact dock generation, with UI attachment and input as separate powers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from .rdp_client_wrapper import StreamSpec


ATTACH_UI = "attach_ui"
RECEIVE_INPUT = "receive_input"
_KNOWN_CAPABILITIES = frozenset({ATTACH_UI, RECEIVE_INPUT})
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class OriginAuthorizationError(ValueError):
    """A stream is not authorized by the paired-origin control plane."""


@dataclass(frozen=True)
class OriginGrant:
    machine_id: str
    trust_domain_id: str
    generation: int
    capabilities: frozenset[str]

    def validate(self) -> None:
        if _IDENTITY.fullmatch(self.machine_id) is None:
            raise OriginAuthorizationError(
                f"invalid paired machine_id {self.machine_id!r}")
        if _IDENTITY.fullmatch(self.trust_domain_id) is None:
            raise OriginAuthorizationError(
                f"invalid trust_domain_id {self.trust_domain_id!r}")
        if self.generation <= 0:
            raise OriginAuthorizationError(
                f"non-positive paired generation {self.generation}")
        unknown = self.capabilities - _KNOWN_CAPABILITIES
        if unknown:
            raise OriginAuthorizationError(
                f"unknown origin capabilities {sorted(unknown)!r}")


class StaticOriginAuthority:
    """Fail-closed snapshot supplied by the paired control-plane launcher."""

    def __init__(self, grants: Iterable[OriginGrant]) -> None:
        self._grants: dict[str, OriginGrant] = {}
        for grant in grants:
            grant.validate()
            if grant.machine_id in self._grants:
                raise OriginAuthorizationError(
                    f"duplicate paired machine_id {grant.machine_id!r}")
            self._grants[grant.machine_id] = grant
        if not self._grants:
            raise OriginAuthorizationError("paired origins must not be empty")

    @classmethod
    def from_config(cls, raw: object) -> "StaticOriginAuthority":
        if not isinstance(raw, list):
            raise OriginAuthorizationError("origins must be an array")
        grants: list[OriginGrant] = []
        for index, item in enumerate(raw):
            if not isinstance(item, Mapping):
                raise OriginAuthorizationError(
                    f"origins[{index}] must be an object")
            expected = {
                "machine_id", "trust_domain_id", "generation", "capabilities"}
            if set(item) != expected:
                raise OriginAuthorizationError(
                    f"origins[{index}] fields do not match grant schema")
            machine_id = item.get("machine_id")
            trust_domain_id = item.get("trust_domain_id")
            generation = item.get("generation")
            capabilities = item.get("capabilities")
            if not isinstance(machine_id, str):
                raise OriginAuthorizationError(
                    f"origins[{index}].machine_id must be a string")
            if not isinstance(trust_domain_id, str):
                raise OriginAuthorizationError(
                    f"origins[{index}].trust_domain_id must be a string")
            if not isinstance(generation, int) or isinstance(generation, bool):
                raise OriginAuthorizationError(
                    f"origins[{index}].generation must be an integer")
            if (not isinstance(capabilities, list)
                    or not all(isinstance(cap, str) for cap in capabilities)):
                raise OriginAuthorizationError(
                    f"origins[{index}].capabilities must be a string array")
            grants.append(OriginGrant(
                machine_id=machine_id,
                trust_domain_id=trust_domain_id,
                generation=generation,
                capabilities=frozenset(capabilities),
            ))
        return cls(grants)

    def authorize(self, spec: StreamSpec) -> OriginGrant:
        grant = self._grants.get(spec.origin)
        if grant is None:
            raise OriginAuthorizationError(
                f"origin {spec.origin!r} is not paired")
        if grant.generation != spec.generation:
            raise OriginAuthorizationError(
                f"origin {spec.origin!r} generation {spec.generation} does "
                f"not match paired generation {grant.generation}")
        if ATTACH_UI not in grant.capabilities:
            raise OriginAuthorizationError(
                f"origin {spec.origin!r} lacks {ATTACH_UI!r} grant")
        if spec.allow_input == 1 and RECEIVE_INPUT not in grant.capabilities:
            raise OriginAuthorizationError(
                f"origin {spec.origin!r} lacks {RECEIVE_INPUT!r} grant")
        return grant
