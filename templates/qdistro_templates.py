"""On-disk model for qdistro templates (podman-image backend).

This is the trust-boundary layer for the template/promotion slice
(doc/templates.md). It owns the directory layout, the TOML read/write
helpers, and — the load-bearing part — the validation that refuses a
mutable image tag anywhere a generation is referenced. A binding that
points at a tag instead of a `sha256:` digest is a hard error, because
that binding is the only path a real silo has to real state: if it
could resolve a moving tag, "a candidate fails validation and the
binding still points at the old generation" would stop being true.

There is no daemon and no registry here. Plain TOML in known
directories, read synchronously by the broker and the one-shot
template services, written atomically (temp + rename) so a crash never
leaves a half-written binding.

Reads use the stdlib ``tomllib``. Writes use a small restricted emitter
(``dumps_toml``) rather than a third-party writer dependency, because
qdistro modules ship as flat ``.py`` files on the target with no pip
closure — and the schema we emit is narrow (strings, ints, string
lists, nested tables, arrays of tables).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from typing import Any

# A generation is referenced by immutable content digest, never a tag.
# podman image digests, config ids, and our input hashes are all
# sha256:<64 hex>. Anything else (``:latest``, a bare name, a short id)
# is rejected at the boundary.
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

PIN_REASONS = (
    "active",
    "rollback-window",
    "pre-migration-snapshot",
    "in-flight-workflow",
    "manual-hold",
)

CANDIDATE_STATES = ("built", "validated", "failed")


class TemplateError(Exception):
    """Raised when a template file fails validation at a trust boundary."""


# Template/silo/run-id names become path components under /etc and
# /var/lib, and arrive as CLI args or systemd ``%i`` instance strings, so
# they are untrusted input. Constrain them to a narrow alphabet with no
# path separators or dot-dot so a crafted name cannot escape its tree.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def require_safe_name(name: object, kind: str = "name") -> str:
    if not isinstance(name, str) or not _SAFE_NAME_RE.match(name) or ".." in name:
        raise TemplateError(
            f"unsafe {kind} {name!r}: must match [A-Za-z0-9][A-Za-z0-9_.-]* "
            f"and contain no '..'"
        )
    return name


def require_state_path(value: object) -> str:
    """A silo's state_path must be an absolute, single-line path with no
    control characters.

    Single-line is load-bearing, not cosmetic: qdistro-resolve-binding
    --launch-env emits ``STATE_PATH=<value>`` and spawn-tier2 parses stdout
    line-by-line, so a newline (or other control char) in the path would let
    a crafted/typo'd state_path inject extra KEY=VALUE records (e.g. a second
    ``GENERATION=``) and subvert the resolved launch image."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise TemplateError("state_path must be an absolute path")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise TemplateError(
            "state_path must be a single line with no control characters "
            "(it crosses the KEY=VALUE launch-env boundary)")
    # ':' would mis-split spawn-tier2's `-v <state_path>:/home/admin:rw`
    # volume spec — refuse it at the trust boundary, not at launch.
    if ":" in value:
        raise TemplateError(
            "state_path must not contain ':' (it would corrupt the bind-mount "
            "volume spec)")
    return value


# --------------------------------------------------------------------------
# digest helpers
# --------------------------------------------------------------------------

def is_digest(value: object) -> bool:
    return isinstance(value, str) and bool(DIGEST_RE.match(value))


def require_digest(value: object, field: str) -> str:
    if not is_digest(value):
        raise TemplateError(
            f"{field} must be an immutable sha256: digest, got {value!r} "
            f"(mutable tags/names are refused — see doc/templates.md)"
        )
    return value  # type: ignore[return-value]


# --------------------------------------------------------------------------
# restricted TOML emitter
# --------------------------------------------------------------------------

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _emit_key(key: str) -> str:
    if _BARE_KEY_RE.match(key):
        return key
    return _emit_str(key)


_SHORT_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}


def _emit_str(value: str) -> str:
    out = []
    for ch in value:
        esc = _SHORT_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ch < " " or ord(ch) == 0x7F:
            # TOML basic strings forbid raw control chars; \uXXXX-escape
            # anything below 0x20 (and DEL) that has no short form.
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _emit_value(value: Any) -> str:
    # bool before int: bool is an int subclass in Python.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _emit_str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_emit_value(e) for e in value) + "]"
    raise TemplateError(f"cannot serialise value of type {type(value).__name__}")


def _emit_table(table: dict, path: list[str], lines: list[str]) -> None:
    sub_tables: list[tuple[str, dict]] = []
    arr_tables: list[tuple[str, list]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            sub_tables.append((key, value))
        elif (
            isinstance(value, (list, tuple))
            and value
            and all(isinstance(e, dict) for e in value)
        ):
            arr_tables.append((key, list(value)))
        else:
            lines.append(f"{_emit_key(key)} = {_emit_value(value)}")
    for key, value in sub_tables:
        new_path = path + [key]
        header = ".".join(_emit_key(p) for p in new_path)
        lines.append("")
        lines.append(f"[{header}]")
        _emit_table(value, new_path, lines)
    for key, elems in arr_tables:
        new_path = path + [key]
        header = ".".join(_emit_key(p) for p in new_path)
        for elem in elems:
            lines.append("")
            lines.append(f"[[{header}]]")
            _emit_table(elem, new_path, lines)


def dumps_toml(obj: dict) -> str:
    """Serialise a dict to TOML covering the schema we author.

    Supports scalars (str/int/float/bool), string/scalar arrays, nested
    tables, and arrays of tables. Round-trips through ``tomllib``."""
    if not isinstance(obj, dict):
        raise TemplateError("top-level TOML value must be a table")
    lines: list[str] = []
    _emit_table(obj, [], lines)
    text = "\n".join(lines).lstrip("\n")
    return text + "\n" if text else ""


def loads_toml(text: str) -> dict:
    return tomllib.loads(text)


# --------------------------------------------------------------------------
# atomic file I/O
# --------------------------------------------------------------------------

def read_toml(path: str) -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` via temp-file + rename in the same dir.

    The rename is atomic on POSIX, so a reader (the launch path, GC)
    sees either the old file or the new one, never a truncated write.
    The parent directory is fsync'd so the rename survives a crash."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".toml")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def atomic_write(path: str, data: str, mode: int = 0o644) -> None:
    """Text wrapper around :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, data.encode("utf-8"), mode)


def write_toml_atomic(path: str, obj: dict, mode: int = 0o644) -> None:
    atomic_write(path, dumps_toml(obj), mode)


# --------------------------------------------------------------------------
# directory layout
# --------------------------------------------------------------------------

class Layout:
    """Resolved on-disk locations for one host.

    Roots are overridable (``QDISTRO_ETC_DIR`` / ``QDISTRO_VAR_DIR``, or
    constructor args) so unit tests run against a tmpdir without root."""

    def __init__(self, etc: str | None = None, var: str | None = None):
        self.etc = etc or os.environ.get("QDISTRO_ETC_DIR", "/etc/qdistro")
        self.var = var or os.environ.get("QDISTRO_VAR_DIR", "/var/lib/qdistro")

    # authored policy
    @property
    def templates_etc(self) -> str:
        return os.path.join(self.etc, "templates")

    @property
    def retention_file(self) -> str:
        return os.path.join(self.etc, "template-retention.toml")

    def template_policy(self, template: str) -> str:
        require_safe_name(template, "template")
        return os.path.join(self.templates_etc, f"{template}.toml")

    # runtime state
    @property
    def templates_var(self) -> str:
        return os.path.join(self.var, "templates")

    @property
    def bindings_dir(self) -> str:
        return os.path.join(self.var, "bindings")

    @property
    def pins_dir(self) -> str:
        return os.path.join(self.var, "pins")

    @property
    def identity_dir(self) -> str:
        return os.path.join(self.var, "identity")

    @property
    def silos_dir(self) -> str:
        return os.path.join(self.var, "silos")

    def silo_dir(self, silo: str) -> str:
        require_safe_name(silo, "silo")
        return os.path.join(self.silos_dir, silo)

    def default_state_path(self, silo: str) -> str:
        return os.path.join(self.silo_dir(silo), "state")

    def binding_file(self, silo: str) -> str:
        require_safe_name(silo, "silo")
        return os.path.join(self.bindings_dir, f"{silo}.toml")

    def template_dir(self, template: str) -> str:
        require_safe_name(template, "template")
        return os.path.join(self.templates_var, template)

    def generations_dir(self, template: str) -> str:
        return os.path.join(self.template_dir(template), "generations")

    def generation_dir(self, template: str, digest: str) -> str:
        require_digest(digest, "generation digest")
        return os.path.join(self.generations_dir(template), digest)

    def candidates_dir(self, template: str) -> str:
        return os.path.join(self.template_dir(template), "candidates")

    def candidate_dir(self, template: str, run_id: str) -> str:
        require_safe_name(run_id, "run-id")
        return os.path.join(self.candidates_dir(template), run_id)

    def pins_for(self, template: str, digest: str) -> str:
        require_digest(digest, "pin generation digest")
        return os.path.join(self.pins_dir, require_safe_name(template, "template"), digest)

    def identity_for(self, silo: str) -> str:
        require_safe_name(silo, "silo")
        return os.path.join(self.identity_dir, silo)


# Directories the bootstrap creates (idempotent). Modes are permissive
# on the parents; the bindings/pins/identity trees hold the security
# state, so they are owner-only.
SKELETON = (
    ("templates_etc", 0o755),
    ("templates_var", 0o755),
    ("bindings_dir", 0o700),
    ("pins_dir", 0o700),
    ("identity_dir", 0o700),
    ("silos_dir", 0o700),
)


def ensure_skeleton(layout: Layout | None = None) -> None:
    layout = layout or Layout()
    for attr, mode in SKELETON:
        path = getattr(layout, attr)
        os.makedirs(path, exist_ok=True)
        os.chmod(path, mode)


# --------------------------------------------------------------------------
# schema validation (trust boundaries)
# --------------------------------------------------------------------------

def _require(table: dict, key: str, where: str) -> Any:
    if key not in table:
        raise TemplateError(f"{where}: missing required key {key!r}")
    return table[key]


def _is_int(value: object) -> bool:
    # bool is an int subclass; a TOML ``true`` must not pass as a count.
    return isinstance(value, int) and not isinstance(value, bool)


def validate_template_policy(policy: dict) -> dict:
    """Validate an authored ``/etc/qdistro/templates/<t>.toml``."""
    tmpl = _require(policy, "template", "template policy")
    cls = _require(tmpl, "class", "[template]")
    if cls not in ("derived", "artifact"):
        raise TemplateError(f"[template].class must be derived|artifact, got {cls!r}")
    boundary = _require(tmpl, "state_boundary", "[template]")
    enforced = _require(boundary, "enforced", "[template.state_boundary]")
    if enforced not in ("true", "partial", "false"):
        raise TemplateError(
            f"[template.state_boundary].enforced must be true|partial|false, "
            f"got {enforced!r}"
        )
    # fableplan2 task 05: pre-activation snapshot policy. Optional (defaults to
    # availability — a snapshot failure never wedges a silo that did not ask
    # for strict protection), but a typo must fail loudly rather than silently
    # downgrade a strict silo to availability.
    snap_policy = tmpl.get("activation_snapshot")
    if snap_policy is not None and snap_policy not in ("strict", "availability"):
        raise TemplateError(
            f"[template].activation_snapshot must be strict|availability, "
            f"got {snap_policy!r}")
    return policy


# fableplan2 task 05: the default when a template policy omits
# activation_snapshot. Availability — never refuse a launch for a silo that
# did not explicitly opt into strict pre-activation snapshots.
DEFAULT_ACTIVATION_SNAPSHOT = "availability"


def activation_snapshot_policy(policy: dict | None) -> str:
    """The pre-activation snapshot policy for a validated template policy
    dict, or the default when the policy (or the key) is absent."""
    if not policy:
        return DEFAULT_ACTIVATION_SNAPSHOT
    return policy.get("template", {}).get(
        "activation_snapshot", DEFAULT_ACTIVATION_SNAPSHOT)


def validate_binding(binding: dict) -> dict:
    """Validate a ``/var/lib/qdistro/bindings/<silo>.toml``.

    This is the enforcement point: ``active_generation`` and every entry
    of ``previous_generations`` must be an immutable digest. A tag here
    is rejected, so a silo can never resolve a moving target."""
    for key in ("silo", "template", "backend", "active_generation",
                "state_path", "activation_policy", "identity_revision"):
        _require(binding, key, "binding")
    require_digest(binding["active_generation"], "binding.active_generation")
    prev = binding.get("previous_generations", [])
    if not isinstance(prev, list):
        raise TemplateError("binding.previous_generations must be a list")
    for i, gen in enumerate(prev):
        require_digest(gen, f"binding.previous_generations[{i}]")
    if binding["backend"] != "podman-image":
        # This slice ships only the podman-image backend; other backends
        # share the contract but are not implemented yet.
        raise TemplateError(
            f"binding.backend {binding['backend']!r} unsupported in this slice "
            f"(only podman-image)"
        )
    # state_path is the silo's only path to real state; an empty, relative,
    # or control-char-bearing value would let a launch mount the wrong tree
    # or inject into the KEY=VALUE launch-env contract.
    require_state_path(binding["state_path"])
    if binding["activation_policy"] not in ("manual", "auto"):
        raise TemplateError(
            "binding.activation_policy must be manual|auto "
            f"(this slice promotes manually), got {binding['activation_policy']!r}"
        )
    if not _is_int(binding["identity_revision"]):
        raise TemplateError("binding.identity_revision must be an integer")
    return binding


def generation_ref(manifest: dict) -> str:
    """The single canonical digest a silo launches and a binding pins.

    podman resolves an image's config id (``image_id``) locally with
    ``podman run sha256:<id>``, so that is the launch reference. Promotion
    and the launch path use *only* this — never a tag, never the candidate
    name."""
    return manifest["generation_ref"]


def validate_manifest(manifest: dict) -> dict:
    """Validate a generation/candidate manifest.

    The build tool writes every required key including ``artifact_manifest``
    (the fetched-artifact record — the build-time evidence of what entered
    the candidate) and ``generation_ref`` (the canonical launch digest).
    ``validation`` is added later by the validate tool, so it is optional
    here and only shape-checked when present."""
    for key in ("template", "run_id", "image_digest", "image_id",
                "containerfile_digest", "build_command", "network_mode",
                "artifact_manifest", "generation_ref"):
        _require(manifest, key, "manifest")
    require_digest(manifest["image_digest"], "manifest.image_digest")
    require_digest(manifest["image_id"], "manifest.image_id")
    require_digest(manifest["containerfile_digest"], "manifest.containerfile_digest")
    require_digest(manifest["generation_ref"], "manifest.generation_ref")
    if not isinstance(manifest["artifact_manifest"], list):
        raise TemplateError("manifest.artifact_manifest must be a list")
    validation = manifest.get("validation")
    if validation is not None:
        if not isinstance(validation, dict):
            raise TemplateError("manifest.validation must be a table")
        _require(validation, "command", "manifest.validation")
    return manifest


def validate_pin(pin: dict) -> dict:
    for key in ("owner_type", "owner_id", "reason", "generation", "template"):
        _require(pin, key, "pin receipt")
    if pin["reason"] not in PIN_REASONS:
        raise TemplateError(
            f"pin.reason must be one of {PIN_REASONS}, got {pin['reason']!r}"
        )
    require_digest(pin["generation"], "pin.generation")
    return pin


RETENTION_COUNT_KEYS = (
    "keep_promoted_generations", "keep_promoted_generations_vm",
    "failed_candidate_days", "build_log_days", "audit_evidence_years",
)


def validate_retention(retention: dict) -> dict:
    for key in RETENTION_COUNT_KEYS:
        value = _require(retention, key, "retention")
        if not _is_int(value) or value < 0:
            raise TemplateError(f"retention.{key} must be a non-negative integer")
    # Validate the whole [overrides.<template>] tree up front so GC fails
    # closed before deleting anything — a malformed override for a later
    # template must not surface mid-run after earlier rmi calls.
    overrides = retention.get("overrides", {})
    if not isinstance(overrides, dict):
        raise TemplateError("retention.overrides must be a table")
    for template, override in overrides.items():
        if not isinstance(override, dict):
            raise TemplateError(f"retention.overrides.{template} must be a table")
        for key, value in override.items():
            if key not in RETENTION_COUNT_KEYS:
                raise TemplateError(
                    f"retention.overrides.{template}.{key} is not a valid "
                    f"retention key")
            if not _is_int(value) or value < 0:
                raise TemplateError(
                    f"retention.overrides.{template}.{key} must be a "
                    f"non-negative integer")
    return retention


# --------------------------------------------------------------------------
# binding / pin / manifest accessors
# --------------------------------------------------------------------------

def read_binding(path: str) -> dict:
    return validate_binding(read_toml(path))


def write_binding(path: str, binding: dict) -> None:
    write_toml_atomic(path, validate_binding(binding), mode=0o600)


def read_manifest(path: str) -> dict:
    return validate_manifest(read_toml(path))


def write_pin(path: str, pin: dict) -> None:
    write_toml_atomic(path, validate_pin(pin), mode=0o600)


# --------------------------------------------------------------------------
# silo state tree (fableplan2 task 01)
# --------------------------------------------------------------------------

# A silo's persistent state lives at binding.state_path. The on-disk
# mechanism (btrfs subvolume vs plain directory) is recorded in a metadata
# file adjacent to the state dir so task 05's snapshot layer knows whether a
# snapper/btrfs snapshot is possible without re-probing the filesystem. The
# metadata is NOT in the binding: the binding is the launch-target schema and
# stays minimal; the state mechanism is a property of the storage, not the
# launch.
STATE_MECHANISMS = ("subvolume", "directory")


def state_meta_path(state_path: str) -> str:
    """Path of the state-mechanism metadata file, a sibling of the state dir
    (``/…/state`` → ``/…/state.meta.toml``)."""
    return state_path.rstrip("/") + ".meta.toml"


def read_state_meta(state_path: str) -> dict | None:
    path = state_meta_path(state_path)
    if not os.path.isfile(path):
        return None
    return read_toml(path)


def _stat_fstype(path: str) -> str:
    """Filesystem type of ``path`` (e.g. ``btrfs``) via stat(1), or ''.

    Used only to decide whether to create a state dir as a btrfs subvolume;
    a wrong answer downgrades to a plain directory (recorded honestly), never
    a security decision."""
    try:
        out = subprocess.run(["stat", "-f", "-c", "%T", path],
                             capture_output=True, text=True)
    except OSError:
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def create_state_tree(state_path: str, mode: int = 0o700) -> str:
    """Create ``state_path`` as a btrfs subvolume when the filesystem
    supports it (btrfs CLI present and the parent is btrfs), else a plain
    directory, and write the mechanism metadata. Idempotent: an existing
    state_path is verified (must be a directory) and its recorded mechanism
    returned. Returns the mechanism (one of :data:`STATE_MECHANISMS`)."""
    require_state_path(state_path)
    parent = os.path.dirname(state_path.rstrip("/")) or "/"
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.exists(state_path):
        if not os.path.isdir(state_path):
            raise TemplateError(
                f"state_path {state_path} exists but is not a directory")
        existing = read_state_meta(state_path)
        if existing and existing.get("mechanism") in STATE_MECHANISMS:
            mechanism = existing["mechanism"]
        else:
            # Pre-existing dir with no/invalid metadata: record it as a
            # directory so the snapshot layer has an honest mechanism to read.
            mechanism = "directory"
            _write_state_meta(state_path, mechanism)
        _ensure_cache_mountpoint(state_path)
        return mechanism

    mechanism = "directory"
    btrfs = shutil.which("btrfs")
    if btrfs and _stat_fstype(parent) == "btrfs":
        rc = subprocess.run([btrfs, "subvolume", "create", state_path],
                            capture_output=True, text=True)
        if rc.returncode == 0:
            mechanism = "subvolume"
        else:
            # Creation failed (e.g. no permission); fall back to a plain dir
            # rather than refusing — the snapshot layer fails loudly later if
            # a subvolume was required.
            os.makedirs(state_path, mode=mode, exist_ok=True)
    else:
        os.makedirs(state_path, mode=mode, exist_ok=True)
    os.chmod(state_path, mode)
    _ensure_cache_mountpoint(state_path)
    _write_state_meta(state_path, mechanism)
    return mechanism


def _ensure_cache_mountpoint(state_path: str) -> None:
    """Pre-create ``<state_path>/.cache`` admin-owned 0700.

    The launch path overlays a tmpfs here with podman's ``,U`` flag; if the
    mountpoint did not already exist, podman would create it inside the
    persistent state and ``,U``-chown it to a container subuid, leaving an
    unremovable, wrongly owned dir in the silo's home (codex r1/r2). Doing it
    on EVERY create_state_tree return — including the existing-tree branches
    (an admin-precreated dir, or an upgraded tree with no .cache) — keeps the
    tmpfs a pure runtime overlay over an admin-owned mountpoint."""
    cache = os.path.join(state_path, ".cache")
    os.makedirs(cache, mode=0o700, exist_ok=True)
    os.chmod(cache, 0o700)


def _write_state_meta(state_path: str, mechanism: str) -> None:
    if mechanism not in STATE_MECHANISMS:
        raise TemplateError(f"invalid state mechanism {mechanism!r}")
    write_toml_atomic(state_meta_path(state_path), {
        "state_path": state_path,
        "mechanism": mechanism,
    }, 0o600)


def candidate_state(candidate_dir: str) -> str | None:
    """Return the recorded state of a candidate, or None if unmarked."""
    marker = os.path.join(candidate_dir, "state")
    if not os.path.exists(marker):
        return None
    with open(marker, encoding="utf-8") as fh:
        state = fh.read().strip()
    return state or None


def set_candidate_state(candidate_dir: str, state: str) -> None:
    if state not in CANDIDATE_STATES:
        raise TemplateError(f"invalid candidate state {state!r}")
    atomic_write(os.path.join(candidate_dir, "state"), state + "\n", mode=0o644)
