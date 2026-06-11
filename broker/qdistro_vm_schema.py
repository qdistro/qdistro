"""Schema validation for VM-backed silo guest definitions and VM image
manifests (see ``doc/vm-definitions.md``, ``doc/isolation-tiers.md`` tiers 4-5,
``doc/resources.md``, and ``doc/workflows.md`` §VM Build Workflow).

This extends the structural validation begun in ``qdistro_metadata_schema`` to
the parts of a silo manifest that are specific to VM-backed isolation tiers:

* ``spec.isolation`` — tier (4 or 5), backend, and display transport;
* ``spec.guest`` — the declarative guest definition reference: ``language``,
  ``system``, ``flakeRef``, ``lockRef``, ``module``, ``output``, and the
  ``exposedServices`` the guest is allowed to publish;
* ``spec.publisher`` — publisher mode, command, and whether arbitrary commands
  are permitted.

It also defines the **qdistro image manifest** (``validate_image_manifest``):
the build-lineage record a VM image build workflow emits, capturing NixOS
module/flake inputs, builder identity, build command, output digest, and
health checks. A helper (:func:`tumbleweed_image_manifest`) emits one of these
manifests for an existing Tumbleweed image builder, so legacy builders carry
the same lineage shape until the NixOS definition path replaces them
(doc/vm-definitions.md §Legacy Builders).

Finally, :func:`check_guest_services_match_manifest` is the **fail-closed**
activation guard from doc/vm-definitions.md §Runtime Policy: a guest definition
or image that exposes a vsock service not declared in the manifest's
``spec.guest.exposedServices`` must block activation. The check fails closed —
missing/ill-typed inputs produce an error, never a silent allow.

Style follows ``qdistro_metadata_schema``: plain functions, frozen
vocabularies, no Pydantic/jsonschema dependency. Validators accumulate errors
into a shared :class:`ValidationResult` (reused from that module) and a thin
``*_or_raise`` wrapper raises :class:`MetadataSchemaError` for fail-fast call
sites.
"""
from __future__ import annotations

import re
from typing import Any

from qdistro_metadata_schema import (
    MetadataSchemaError,
    ValidationResult,
)

# --------------------------------------------------------------------------
# Static vocabulary (doc/isolation-tiers.md, doc/vm-definitions.md)
# --------------------------------------------------------------------------

#: Isolation tiers that are VM-backed (doc/isolation-tiers.md tiers 4-5). Only
#: these tiers carry a ``spec.guest`` definition; lower tiers are rejected by
#: :func:`validate_vm_silo_spec` if they carry one.
VM_TIERS = frozenset({4, 5})

#: Isolation backends qdistro recognises for VM-backed silos. Tier 4/5 use
#: libvirt+QEMU today (doc/isolation-tiers.md §Tier 4/5).
RESERVED_VM_BACKENDS = frozenset({"libvirt-qemu"})

#: Display transports for VM-backed silos (doc/isolation-tiers.md): tier 4 uses
#: waypipe over AF_VSOCK for a whole-VM toplevel; tier 5 uses per-app waypipe
#: over AF_VSOCK.
RESERVED_VM_DISPLAYS = frozenset({"waypipe-vsock"})

#: Guest definition languages (doc/vm-definitions.md §Resource Reference Shape).
#: ``nixos-module`` / ``nixos-flake`` are the preferred path for new images;
#: ``tumbleweed-script`` is the legacy builder path retained until replaced
#: (doc/vm-definitions.md §Legacy Builders).
RESERVED_GUEST_LANGUAGES = frozenset(
    {"nixos-module", "nixos-flake", "tumbleweed-script"}
)

#: Guest languages that build from a Nix definition and therefore require a
#: flake/module reference, lock reference, and named output.
_NIX_GUEST_LANGUAGES = frozenset({"nixos-module", "nixos-flake"})

#: Publisher modes (doc/vm-definitions.md §NixOS Module Contract,
#: ``services.qdistro.publisher.mode``).
RESERVED_PUBLISHER_MODES = frozenset({"per-app", "whole-window"})

# A bounded vsock port: AF_VSOCK ports are u32; qdistro guest services bind a
# concrete, small port (doc/isolation-tiers.md §Tier 5).
_MIN_VSOCK_PORT = 1
_MAX_VSOCK_PORT = 0xFFFFFFFF

# A guest service name: a short DNS-1123 label (e.g. "waypipe", "qdni").
_SERVICE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

# An "algorithm:hex" content digest, e.g. "sha256:ab12…". The algorithm is a
# short token; the body is hex. For known algorithms the hex length is fixed
# (a truncated sha256 is structurally invalid and weakens lineage), otherwise a
# generous minimum is required.
_DIGEST_RE = re.compile(r"^([a-z0-9][a-z0-9+._-]*):([0-9a-fA-F]+)$")

#: Exact hex-digit counts for digest algorithms qdistro recognises.
_DIGEST_HEX_LEN = {
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
    "blake2b": 128,
    "blake2s": 64,
}
#: Minimum hex length for an unrecognised digest algorithm.
_DIGEST_MIN_HEX = 32


def _valid_digest(digest: Any) -> bool:
    """An ``algorithm:hex`` content digest with the correct hex length for the
    named algorithm (or a minimum length for an unknown algorithm)."""
    if not isinstance(digest, str):
        return False
    m = _DIGEST_RE.match(digest)
    if not m:
        return False
    algo, body = m.group(1), m.group(2)
    expected = _DIGEST_HEX_LEN.get(algo)
    if expected is not None:
        return len(body) == expected
    return len(body) >= _DIGEST_MIN_HEX


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


# --------------------------------------------------------------------------
# spec.isolation
# --------------------------------------------------------------------------

def validate_isolation(isolation: Any) -> ValidationResult:
    """Validate ``spec.isolation`` of a VM-backed silo manifest.

    Returns the recognised tier in errors-free results implicitly via the
    caller; this function only reports structural problems. ``tier`` must be a
    VM tier (4 or 5); ``backend`` and ``display`` must name reserved values.
    """
    res = ValidationResult()
    if not isinstance(isolation, dict):
        res.errors.append(
            f"spec.isolation must be a mapping, got {type(isolation).__name__}"
        )
        return res

    tier = isolation.get("tier")
    # bool is an int subclass — reject it explicitly so ``tier: true`` is not
    # silently read as 1.
    if not isinstance(tier, int) or isinstance(tier, bool):
        res.errors.append(
            f"spec.isolation.tier must be an integer, got {tier!r}"
        )
    elif tier not in VM_TIERS:
        res.errors.append(
            f"spec.isolation.tier {tier!r} is not a VM-backed tier "
            f"(VM tiers: {', '.join(str(t) for t in sorted(VM_TIERS))})"
        )

    backend = isolation.get("backend")
    if backend is not None and backend not in RESERVED_VM_BACKENDS:
        res.errors.append(
            f"spec.isolation.backend {backend!r} is not a reserved VM backend "
            f"(reserved: {', '.join(sorted(RESERVED_VM_BACKENDS))})"
        )

    display = isolation.get("display")
    if display is not None and display not in RESERVED_VM_DISPLAYS:
        res.errors.append(
            f"spec.isolation.display {display!r} is not a reserved VM display "
            f"transport (reserved: {', '.join(sorted(RESERVED_VM_DISPLAYS))})"
        )
    return res


# --------------------------------------------------------------------------
# spec.guest.exposedServices
# --------------------------------------------------------------------------

def _validate_exposed_service(entry: Any, idx: int) -> ValidationResult:
    """Validate one ``spec.guest.exposedServices`` entry: a mapping with a
    ``name`` and a ``vsockPort``."""
    res = ValidationResult()
    where = f"spec.guest.exposedServices[{idx}]"
    if not isinstance(entry, dict):
        res.errors.append(f"{where} must be a mapping, got {type(entry).__name__}")
        return res

    name = entry.get("name")
    if not _is_nonempty_str(name) or not _SERVICE_NAME_RE.match(name):
        res.errors.append(
            f"{where}.name must be a DNS-1123 label, got {name!r}"
        )

    port = entry.get("vsockPort")
    if not isinstance(port, int) or isinstance(port, bool):
        res.errors.append(
            f"{where}.vsockPort must be an integer, got {port!r}"
        )
    elif not (_MIN_VSOCK_PORT <= port <= _MAX_VSOCK_PORT):
        res.errors.append(
            f"{where}.vsockPort {port!r} out of range "
            f"[{_MIN_VSOCK_PORT}, {_MAX_VSOCK_PORT}]"
        )
    return res


def validate_exposed_services(services: Any) -> ValidationResult:
    """Validate the whole ``spec.guest.exposedServices`` list.

    Each entry names a guest vsock service the manifest authorises. Duplicate
    service names and duplicate ports are flagged: the runtime guard
    (:func:`check_guest_services_match_manifest`) matches by name, so a
    duplicate name is ambiguous, and two services on one port cannot both bind.
    """
    res = ValidationResult()
    if services is None:
        return res
    if not isinstance(services, list):
        res.errors.append(
            f"spec.guest.exposedServices must be a list, got "
            f"{type(services).__name__}"
        )
        return res
    seen_names: set[str] = set()
    seen_ports: set[int] = set()
    for idx, entry in enumerate(services):
        res.merge(_validate_exposed_service(entry, idx))
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                if name in seen_names:
                    res.errors.append(
                        f"spec.guest.exposedServices: duplicate service "
                        f"name {name!r}"
                    )
                seen_names.add(name)
            port = entry.get("vsockPort")
            if isinstance(port, int) and not isinstance(port, bool):
                if port in seen_ports:
                    res.errors.append(
                        f"spec.guest.exposedServices: duplicate vsockPort "
                        f"{port!r}"
                    )
                seen_ports.add(port)
    return res


# --------------------------------------------------------------------------
# spec.guest
# --------------------------------------------------------------------------

def validate_guest(guest: Any) -> ValidationResult:
    """Validate ``spec.guest`` — the declarative guest definition reference.

    Required: ``language`` (a reserved guest language). For Nix-based languages
    (``nixos-module``/``nixos-flake``) ``flakeRef``, ``lockRef``, and ``output``
    are required and ``module`` is recommended. ``system`` (a Nix system tuple)
    and ``exposedServices`` are optional. The legacy ``tumbleweed-script``
    language does not require the Nix reference fields.
    """
    res = ValidationResult()
    if not isinstance(guest, dict):
        res.errors.append(
            f"spec.guest must be a mapping, got {type(guest).__name__}"
        )
        return res

    language = guest.get("language")
    if not _is_nonempty_str(language):
        res.errors.append(
            f"spec.guest.language is required and must be a non-empty string, "
            f"got {language!r}"
        )
    elif language not in RESERVED_GUEST_LANGUAGES:
        res.errors.append(
            f"spec.guest.language {language!r} is not a reserved guest "
            f"language (reserved: {', '.join(sorted(RESERVED_GUEST_LANGUAGES))})"
        )

    # String-typed reference fields. flakeRef/lockRef/module/output/system are
    # all opaque strings to qdistro (the native Nix tooling resolves them); we
    # only check type/presence here.
    for fld in ("flakeRef", "lockRef", "module", "output", "system"):
        val = guest.get(fld)
        if val is not None and not isinstance(val, str):
            res.errors.append(
                f"spec.guest.{fld} must be a string, got {type(val).__name__}"
            )

    # Nix-based guests must carry a resolvable definition: a flake ref, a
    # locked revision (lineage anchor), and a named output. Without these the
    # build is not reproducible (doc/vm-definitions.md §Flake Contract,
    # §Build Lineage).
    # Guard with isinstance: a non-string language value has already produced
    # an error above; skipping the `in` check avoids TypeError on unhashable
    # types (e.g. list) while remaining fail-closed (error already recorded).
    if isinstance(language, str) and language in _NIX_GUEST_LANGUAGES:
        for fld in ("flakeRef", "lockRef", "output"):
            if not _is_nonempty_str(guest.get(fld)):
                res.errors.append(
                    f"spec.guest.{fld} is required for language {language!r} "
                    f"and must be a non-empty string"
                )

    res.merge(validate_exposed_services(guest.get("exposedServices")))
    return res


# --------------------------------------------------------------------------
# spec.publisher
# --------------------------------------------------------------------------

def validate_publisher(publisher: Any) -> ValidationResult:
    """Validate ``spec.publisher`` (doc/vm-definitions.md §Resource Reference
    Shape). ``mode`` must be a reserved publisher mode; ``command`` must be a
    non-empty string; ``arbitraryCommand``, when present, must be a bool."""
    res = ValidationResult()
    if publisher is None:
        return res
    if not isinstance(publisher, dict):
        res.errors.append(
            f"spec.publisher must be a mapping, got {type(publisher).__name__}"
        )
        return res

    mode = publisher.get("mode")
    if not _is_nonempty_str(mode):
        res.errors.append(
            f"spec.publisher.mode is required and must be a non-empty string, "
            f"got {mode!r}"
        )
    elif mode not in RESERVED_PUBLISHER_MODES:
        res.errors.append(
            f"spec.publisher.mode {mode!r} is not a reserved publisher mode "
            f"(reserved: {', '.join(sorted(RESERVED_PUBLISHER_MODES))})"
        )

    command = publisher.get("command")
    if not _is_nonempty_str(command):
        res.errors.append(
            f"spec.publisher.command is required and must be a non-empty "
            f"string, got {command!r}"
        )

    arbitrary = publisher.get("arbitraryCommand")
    if arbitrary is not None and not isinstance(arbitrary, bool):
        res.errors.append(
            f"spec.publisher.arbitraryCommand must be a boolean, got "
            f"{arbitrary!r}"
        )
    return res


# --------------------------------------------------------------------------
# Aggregate: spec of a VM-backed silo
# --------------------------------------------------------------------------

def validate_vm_silo_spec(obj: Any) -> ValidationResult:
    """Validate the VM-specific ``spec`` blocks of a silo manifest.

    Owns ``spec.isolation``, ``spec.guest``, and ``spec.publisher``. It does
    **not** re-validate ``metadata.labels``/``security.guards`` — call
    ``qdistro_metadata_schema.validate_metadata_security`` for those. A manifest
    that declares a VM tier (4/5) must carry a ``spec.guest``; a non-VM tier
    must not.
    """
    res = ValidationResult()
    if not isinstance(obj, dict):
        res.errors.append(f"manifest must be a mapping, got {type(obj).__name__}")
        return res

    spec = obj.get("spec")
    if spec is None:
        res.errors.append("spec is required for a VM-backed silo manifest")
        return res
    if not isinstance(spec, dict):
        res.errors.append(f"spec must be a mapping, got {type(spec).__name__}")
        return res

    isolation = spec.get("isolation")
    if isolation is None:
        res.errors.append("spec.isolation is required")
        tier = None
    else:
        res.merge(validate_isolation(isolation))
        tier = isolation.get("tier") if isinstance(isolation, dict) else None

    guest = spec.get("guest")
    is_vm_tier = isinstance(tier, int) and not isinstance(tier, bool) and tier in VM_TIERS
    if guest is None:
        if is_vm_tier:
            res.errors.append(
                "spec.guest is required for a VM-backed silo "
                f"(tier {tier})"
            )
    else:
        if not is_vm_tier:
            res.errors.append(
                "spec.guest is only valid for a VM-backed tier "
                f"({', '.join(str(t) for t in sorted(VM_TIERS))}); "
                f"tier is {tier!r}"
            )
        res.merge(validate_guest(guest))

    res.merge(validate_publisher(spec.get("publisher")))
    return res


def validate_vm_silo_spec_or_raise(obj: Any) -> None:
    """Fail-fast wrapper: raise :class:`MetadataSchemaError` on any hard error."""
    res = validate_vm_silo_spec(obj)
    if not res.ok:
        raise MetadataSchemaError("; ".join(res.errors))


# --------------------------------------------------------------------------
# qdistro image manifest (build lineage; doc/workflows.md §VM Build Workflow,
# doc/vm-definitions.md §Build Lineage)
# --------------------------------------------------------------------------

def validate_image_manifest(manifest: Any) -> ValidationResult:
    """Validate a qdistro VM image manifest — the build-lineage record a VM
    image build workflow emits.

    A built image is an artifact with lineage. The manifest records, per
    doc/vm-definitions.md §Build Lineage and doc/workflows.md §VM Build
    Workflow:

    * ``kind: Image`` and ``apiVersion`` (structural anchor);
    * ``definition`` — the guest definition inputs (language, flakeRef,
      lockRef, module, output) the image was built from;
    * ``build`` — builder identity, build command, and (for Nix) nixpkgs
      revision / sandbox+network state;
    * ``output`` — the output path and content digest;
    * ``healthChecks`` — a non-empty list of named guest health checks.

    This is structural validation only; it does not run the build or verify the
    digest against an on-disk image (that needs a live build — see the module
    docstring and the todo).
    """
    res = ValidationResult()
    if not isinstance(manifest, dict):
        res.errors.append(
            f"image manifest must be a mapping, got {type(manifest).__name__}"
        )
        return res

    kind = manifest.get("kind")
    if kind != "Image":
        res.errors.append(
            f"image manifest kind must be \"Image\", got {kind!r}"
        )
    if not _is_nonempty_str(manifest.get("apiVersion")):
        res.errors.append(
            "image manifest apiVersion is required and must be a non-empty "
            "string"
        )

    res.merge(_validate_image_definition(manifest.get("definition")))
    res.merge(_validate_image_build(manifest.get("build")))
    res.merge(_validate_image_output(manifest.get("output")))
    res.merge(_validate_image_health_checks(manifest.get("healthChecks")))
    return res


def _validate_image_definition(definition: Any) -> ValidationResult:
    res = ValidationResult()
    if not isinstance(definition, dict):
        res.errors.append(
            f"image manifest definition must be a mapping, got "
            f"{type(definition).__name__}"
        )
        return res
    language = definition.get("language")
    if not _is_nonempty_str(language):
        res.errors.append(
            "image manifest definition.language is required and must be a "
            "non-empty string"
        )
    elif language not in RESERVED_GUEST_LANGUAGES:
        res.errors.append(
            f"image manifest definition.language {language!r} is not a "
            f"reserved guest language "
            f"(reserved: {', '.join(sorted(RESERVED_GUEST_LANGUAGES))})"
        )
    for fld in ("flakeRef", "lockRef", "module", "output"):
        val = definition.get(fld)
        if val is not None and not isinstance(val, str):
            res.errors.append(
                f"image manifest definition.{fld} must be a string, got "
                f"{type(val).__name__}"
            )
    # Nix-built images must record the locked inputs that make them
    # reproducible. Guard with isinstance for the same reason as validate_guest.
    if isinstance(language, str) and language in _NIX_GUEST_LANGUAGES:
        for fld in ("flakeRef", "lockRef", "output"):
            if not _is_nonempty_str(definition.get(fld)):
                res.errors.append(
                    f"image manifest definition.{fld} is required for "
                    f"language {language!r}"
                )

    # Legacy Tumbleweed builders record a package list and source refs
    # (doc/vm-definitions.md §Legacy Builders). When present these must be
    # lists of non-empty strings — a bare string would otherwise be silently
    # accepted (and iterated char-by-char downstream), weakening lineage.
    for fld in ("packages", "sourceRefs"):
        val = definition.get(fld)
        if val is None:
            continue
        if not isinstance(val, list) or not all(_is_nonempty_str(v) for v in val):
            res.errors.append(
                f"image manifest definition.{fld} must be a list of non-empty "
                f"strings, got {val!r}"
            )
    # A tumbleweed-script image's lineage value is its package/source lists, so
    # require them to be present.
    if language == "tumbleweed-script":
        for fld in ("packages", "sourceRefs"):
            if not isinstance(definition.get(fld), list):
                res.errors.append(
                    f"image manifest definition.{fld} is required for "
                    f"language 'tumbleweed-script'"
                )
    return res


def _validate_image_build(build: Any) -> ValidationResult:
    res = ValidationResult()
    if not isinstance(build, dict):
        res.errors.append(
            f"image manifest build must be a mapping, got "
            f"{type(build).__name__}"
        )
        return res
    if not _is_nonempty_str(build.get("builder")):
        res.errors.append(
            "image manifest build.builder (builder identity) is required and "
            "must be a non-empty string"
        )
    command = build.get("command")
    # The build command may be a string or an argv list; both are recorded
    # verbatim for lineage.
    if isinstance(command, list):
        if not command or not all(isinstance(c, str) for c in command):
            res.errors.append(
                "image manifest build.command argv must be a non-empty list "
                "of strings"
            )
    elif not _is_nonempty_str(command):
        res.errors.append(
            "image manifest build.command is required and must be a non-empty "
            "string or argv list"
        )
    # sandbox/network state, when present, is boolean.
    for fld in ("sandboxed", "network"):
        val = build.get(fld)
        if val is not None and not isinstance(val, bool):
            res.errors.append(
                f"image manifest build.{fld} must be a boolean, got {val!r}"
            )
    return res


def _validate_image_output(output: Any) -> ValidationResult:
    res = ValidationResult()
    if not isinstance(output, dict):
        res.errors.append(
            f"image manifest output must be a mapping, got "
            f"{type(output).__name__}"
        )
        return res
    if not _is_nonempty_str(output.get("path")):
        res.errors.append(
            "image manifest output.path is required and must be a non-empty "
            "string"
        )
    digest = output.get("digest")
    if not _is_nonempty_str(digest):
        res.errors.append(
            "image manifest output.digest is required and must be a non-empty "
            "string"
        )
    elif not _valid_digest(digest):
        res.errors.append(
            f"image manifest output.digest {digest!r} is not a valid "
            f"\"algorithm:hex\" content digest (wrong/short hex length)"
        )
    return res


def _validate_image_health_checks(checks: Any) -> ValidationResult:
    res = ValidationResult()
    if not isinstance(checks, list) or not checks:
        res.errors.append(
            "image manifest healthChecks must be a non-empty list"
        )
        return res
    seen: set[str] = set()
    for idx, check in enumerate(checks):
        where = f"image manifest healthChecks[{idx}]"
        if not isinstance(check, dict):
            res.errors.append(f"{where} must be a mapping, got {type(check).__name__}")
            continue
        name = check.get("name")
        if not _is_nonempty_str(name):
            res.errors.append(f"{where}.name is required and must be a non-empty string")
            continue
        if name in seen:
            res.errors.append(f"image manifest healthChecks: duplicate name {name!r}")
        seen.add(name)
    return res


def validate_image_manifest_or_raise(manifest: Any) -> None:
    """Fail-fast wrapper: raise :class:`MetadataSchemaError` on any hard error."""
    res = validate_image_manifest(manifest)
    if not res.ok:
        raise MetadataSchemaError("; ".join(res.errors))


# --------------------------------------------------------------------------
# Emit a qdistro image manifest for a legacy Tumbleweed builder
# (doc/vm-definitions.md §Legacy Builders)
# --------------------------------------------------------------------------

def tumbleweed_image_manifest(
    *,
    name: str,
    packages: list[str],
    source_refs: list[str],
    build_command: Any,
    builder: str,
    output_path: str,
    output_digest: str,
    health_checks: list[Any],
    api_version: str = "resources.qdistro.io/v1alpha1",
) -> dict[str, Any]:
    """Emit a qdistro image manifest for an existing Tumbleweed image builder.

    Legacy Tumbleweed builders are retained for current tier-4/tier-5 images
    until the NixOS definition path replaces them, but they must still emit a
    qdistro image manifest with package list, source refs, build command, and
    output digest (doc/vm-definitions.md §Legacy Builders). This produces a
    manifest in the same shape :func:`validate_image_manifest` checks, with
    ``definition.language = "tumbleweed-script"`` and the package/source lists
    recorded under ``definition``.

    The returned dict is validated before return so a malformed call fails
    fast rather than emitting an invalid manifest.
    """
    # Guard against a bare string being char-split by ``list()`` (a common
    # caller slip that would otherwise silently produce a garbage package list).
    for label, seq in (("packages", packages), ("source_refs", source_refs),
                       ("health_checks", health_checks)):
        if isinstance(seq, str) or not isinstance(seq, (list, tuple)):
            raise MetadataSchemaError(
                f"tumbleweed_image_manifest: {label} must be a list, got "
                f"{type(seq).__name__}"
            )
    manifest: dict[str, Any] = {
        "apiVersion": api_version,
        "kind": "Image",
        "metadata": {"name": name},
        "definition": {
            "language": "tumbleweed-script",
            "packages": list(packages),
            "sourceRefs": list(source_refs),
        },
        "build": {
            "builder": builder,
            "command": build_command,
        },
        "output": {
            "path": output_path,
            "digest": output_digest,
        },
        "healthChecks": list(health_checks),
    }
    validate_image_manifest_or_raise(manifest)
    return manifest


# --------------------------------------------------------------------------
# Activation guard: guest-exposed services must match the manifest
# (doc/vm-definitions.md §Runtime Policy — fail closed)
# --------------------------------------------------------------------------

def _valid_vsock_port(port: Any) -> bool:
    return (
        isinstance(port, int)
        and not isinstance(port, bool)
        and _MIN_VSOCK_PORT <= port <= _MAX_VSOCK_PORT
    )


def check_guest_services_match_manifest(
    spec_guest: Any,
    guest_exposed: Any,
) -> ValidationResult:
    """Fail-closed check that the services a guest definition/image exposes are
    all declared in the manifest's ``spec.guest.exposedServices``.

    From doc/vm-definitions.md §Runtime Policy: *"a NixOS module exposing an
    extra vsock service that is not present in ``spec.guest.exposedServices``
    should block image activation or mark the silo failed. If the guest
    definition and qdistro manifest disagree, the broker fails closed."*

    ``spec_guest`` is the manifest's ``spec.guest`` mapping (its
    ``exposedServices`` is the authoritative allow-set). ``guest_exposed`` is
    the set of services the actual guest definition or built image exposes —
    each a mapping with a ``name`` (and optionally a ``vsockPort``), or a bare
    service-name string.

    Fail-closed semantics — *any* of the following blocks activation:

    * ``spec_guest`` is not a mapping;
    * the manifest allow-set is structurally invalid (bad/duplicate names, bad
      ports) — it is re-validated here via :func:`validate_exposed_services`
      rather than trusted, since this guard is on the security boundary and a
      malformed allow-set must not silently widen access;
    * ``guest_exposed`` is **absent** (``None``) — missing service-discovery
      data is treated as a failure, not "zero services". Pass an explicit
      empty list ``[]`` to mean "the guest exposes no services";
    * ``guest_exposed`` is not a list, or any entry is neither a string nor a
      mapping, or has no valid name, or carries a malformed ``vsockPort``;
    * a guest service name is not in the allow-set, its port disagrees with the
      declared port, or — when the manifest **pins** a concrete port — the
      guest report omits a valid matching port (incomplete evidence cannot
      confirm the declared port, so it fails closed).

    An empty/absent manifest allow-set means *no* guest service is permitted:
    every service the guest exposes is then a violation, never *all*. The one
    non-violation is a guest that exposes *zero* services (an explicit empty
    ``guest_exposed`` list) against an empty allow-set — there is nothing to
    disagree about, so that matches and is allowed. (Missing discovery data —
    ``None`` — is still a failure; see above.)
    """
    res = ValidationResult()

    if not isinstance(spec_guest, dict):
        res.errors.append(
            "activation blocked: spec.guest is missing or not a mapping "
            "(cannot verify guest-exposed services against the manifest)"
        )
        return res

    # The manifest's guest block must itself be structurally valid before it can
    # serve as an authoritative allow-set — do not trust it. This rejects a
    # degenerate guest (no language, malformed/duplicate exposedServices) rather
    # than letting it stand in as an empty, permissive allow-set.
    guest_res = validate_guest(spec_guest)
    if not guest_res.ok:
        res.errors.append(
            "activation blocked: spec.guest is structurally invalid ("
            + "; ".join(guest_res.errors) + ")"
        )
        return res

    # Build the authoritative allow-map: name -> declared vsockPort (or None).
    # validate_guest -> validate_exposed_services has already guaranteed unique,
    # well-formed names/ports, so no entry is silently overwritten.
    declared = spec_guest.get("exposedServices")
    allow: dict[str, Any] = {}
    for entry in declared or []:
        allow[entry["name"]] = entry.get("vsockPort")

    # Missing discovery data fails closed; an explicit empty list is "no
    # services exposed" and is allowed only when the allow-set is also empty.
    if guest_exposed is None:
        res.errors.append(
            "activation blocked: guest-exposed services are unknown "
            "(no discovery data supplied)"
        )
        return res
    if not isinstance(guest_exposed, list):
        res.errors.append(
            "activation blocked: guest-exposed services is not a list "
            f"({type(guest_exposed).__name__})"
        )
        return res

    for entry in guest_exposed:
        if isinstance(entry, str):
            gname: Any = entry
            gport: Any = None
            has_port = False
        elif isinstance(entry, dict):
            gname = entry.get("name")
            gport = entry.get("vsockPort")
            has_port = "vsockPort" in entry
        else:
            res.errors.append(
                "activation blocked: guest-exposed service entry is neither a "
                f"string nor a mapping ({entry!r})"
            )
            continue

        if not _is_nonempty_str(gname):
            res.errors.append(
                "activation blocked: guest-exposed service has no valid name "
                f"({entry!r})"
            )
            continue
        # A guest entry that carries a port at all must carry a *valid* one —
        # a malformed port (bool/string/out-of-range) blocks activation rather
        # than being ignored.
        if has_port and not _valid_vsock_port(gport):
            res.errors.append(
                f"activation blocked: guest service {gname!r} has a malformed "
                f"vsockPort ({gport!r})"
            )
            continue
        if gname not in allow:
            res.errors.append(
                f"activation blocked: guest exposes service {gname!r} which is "
                f"not declared in spec.guest.exposedServices"
            )
            continue
        declared_port = allow[gname]
        declared_has_port = (
            isinstance(declared_port, int) and not isinstance(declared_port, bool)
        )
        # When the manifest pins a concrete port for this service, the guest
        # report must *confirm* that port: a port-less guest entry (bare string
        # or mapping with no vsockPort) is incomplete evidence — the broker
        # cannot prove the guest is on the declared port — so it fails closed.
        if declared_has_port and not _valid_vsock_port(gport):
            res.errors.append(
                f"activation blocked: manifest pins vsock port {declared_port!r} "
                f"for service {gname!r} but the guest report omits a valid port "
                f"(cannot confirm the declared port)"
            )
            continue
        # Both sides name a concrete port: they must agree.
        if declared_has_port and gport != declared_port:
            res.errors.append(
                f"activation blocked: guest service {gname!r} exposes vsock "
                f"port {gport!r} but the manifest declares {declared_port!r}"
            )
    return res


def check_guest_services_match_manifest_or_raise(
    spec_guest: Any, guest_exposed: Any
) -> None:
    """Fail-fast wrapper for the activation guard."""
    res = check_guest_services_match_manifest(spec_guest, guest_exposed)
    if not res.ok:
        raise MetadataSchemaError("; ".join(res.errors))
