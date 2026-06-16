"""Evidence-bundle format for the two-VM display harness.

Per ``09-test-strategy.md`` "build now" item 1: a bundle ties together
captures + logs + topology + generation/output/frame ids + netem profile +
scenario step + oracle result, so every claim is traceable to exactly which
capture point and which network profile it proves (the documentation
guardrail).

A bundle is a directory with a ``manifest.json`` describing it. Captures are
*named by what they prove* (``CaptureClass``) — the harness's central
honesty rule (09): comparing VM-A local against the VM-A RDP-output framebuffer
hides every encode/transport defect, so the class is recorded and the oracle /
reviewer can refuse the wrong comparison.

Pure stdlib (json/dataclasses/pathlib); no imaging or numpy here.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

SCHEMA_VERSION = 1


class CaptureClass(str, Enum):
    """What a framebuffer capture actually proves (09 "name every framebuffer").

    The names encode the proof boundary; do not invent comparisons across
    incompatible classes (e.g. VM-A local vs VM_A_RDP_SOURCE hides transport).
    """

    VM_A_GUEST = "vm_a_guest"          # compositor-owned logical pixels, pre-encode
    VM_A_HOST = "vm_a_host"            # QEMU virtual-head pixels (VM-A local displays)
    VM_A_RDP_SOURCE = "vm_a_rdp_source"  # "what VM-A intended to send" — NOT what B shows
    VM_B_GUEST = "vm_b_guest"          # VM-B compositor view incl. decoded client window
    VM_B_HOST = "vm_b_host"            # best "what VM-B's monitor shows" (post-decode)
    FREERDP_DECODED = "freerdp_decoded"  # decoded framebuffer (pre-VM-B-composition)


# Which capture class is the authoritative "what the remote monitor shows".
DECODED_REMOTE_CLASSES = {CaptureClass.VM_B_HOST, CaptureClass.VM_B_GUEST,
                          CaptureClass.FREERDP_DECODED}
SOURCE_INTENT_CLASSES = {CaptureClass.VM_A_GUEST, CaptureClass.VM_A_HOST,
                         CaptureClass.VM_A_RDP_SOURCE}


@dataclass
class Capture:
    """One framebuffer capture in a bundle."""

    path: str                     # bundle-relative path to the image file
    capture_class: str            # CaptureClass value
    output_id: int | None = None  # which output / screen this is
    role: str = ""                # human label, e.g. "VM-A display-1"
    fmt: str = ""                 # BGRA/RGBA/RGB/PPM/PNG — record for normalization
    scale: float | None = None
    note: str = ""


@dataclass
class OracleRecord:
    """A serialized oracle verdict attached to a capture (or comparison)."""

    capture: str                  # which capture path it ran on
    ok: bool
    output_id: int | None = None
    generation: int | None = None
    frame: int | None = None
    measured_scale: float | None = None
    hidden_scaling: bool = False
    stale_generation: bool = False
    bad_bands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Topology:
    """The VM topology + network shaping the bundle was produced under."""

    vms: list[str] = field(default_factory=list)      # e.g. ["vm-a", "vm-b"]
    netem_profile: str = "lan-clean"
    description: str = ""


@dataclass
class Manifest:
    schema_version: int = SCHEMA_VERSION
    scenario: str = ""
    step: str = ""
    generation: int | None = None
    topology: Topology = field(default_factory=Topology)
    captures: list[Capture] = field(default_factory=list)
    oracle: list[OracleRecord] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)     # bundle-relative log paths
    passed: bool | None = None
    notes: list[str] = field(default_factory=list)


class EvidenceBundle:
    """A directory holding captures, logs, and a manifest.json."""

    def __init__(self, root: Path | str, manifest: Manifest | None = None):
        self.root = Path(root)
        self.manifest = manifest or Manifest()

    # ---- construction ----------------------------------------------------
    @classmethod
    def create(cls, root: Path | str, scenario: str, step: str = "",
               generation: int | None = None,
               topology: Topology | None = None) -> "EvidenceBundle":
        root = Path(root)
        (root / "captures").mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(parents=True, exist_ok=True)
        m = Manifest(scenario=scenario, step=step, generation=generation,
                     topology=topology or Topology())
        return cls(root, m)

    def add_capture(self, src: Path | str, capture_class: CaptureClass,
                    *, output_id: int | None = None, role: str = "",
                    fmt: str = "", scale: float | None = None,
                    note: str = "") -> Capture:
        """Copy ``src`` into the bundle's captures/ and register it."""
        import shutil

        src = Path(src)
        dst = self.root / "captures" / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        rel = str(dst.relative_to(self.root))
        cap = Capture(path=rel, capture_class=capture_class.value,
                      output_id=output_id, role=role,
                      fmt=fmt or src.suffix.lstrip(".").upper(), scale=scale,
                      note=note)
        self.manifest.captures.append(cap)
        return cap

    def add_log(self, src: Path | str) -> str:
        import shutil

        src = Path(src)
        dst = self.root / "logs" / src.name
        if src.resolve() != dst.resolve():
            shutil.copy2(src, dst)
        rel = str(dst.relative_to(self.root))
        self.manifest.logs.append(rel)
        return rel

    def add_oracle(self, rec: OracleRecord) -> None:
        self.manifest.oracle.append(rec)

    # ---- the honesty rule (09) ------------------------------------------
    def assert_remote_proof(self) -> None:
        """Refuse a bundle that claims to prove the remote monitor's content
        unless a **passing oracle result is tied to a decoded-remote capture**
        (codex impl-3 finding 4). It is not enough for a decoded-remote file to
        merely exist in the bundle — the ``ok`` verdict must have been computed
        on it, else a source-intent capture could carry the proof while an
        unrelated VM-B file sits unused."""
        decoded = {c.value for c in DECODED_REMOTE_CLASSES}
        # map capture path -> class
        cls_by_path = {c.path: c.capture_class for c in self.manifest.captures}
        if not (set(cls_by_path.values()) & decoded):
            raise ValueError(
                "bundle has no decoded-remote capture (VM-B host/guest or "
                "FreeRDP-decoded); a VM-A RDP-source framebuffer only proves "
                "source intent, not what the peer monitor shows (09).")
        ok_decoded = [
            o for o in self.manifest.oracle
            if o.ok and cls_by_path.get(o.capture) in decoded]
        if not ok_decoded:
            raise ValueError(
                "no passing oracle record is tied to a decoded-remote capture; "
                "the remote-monitor claim must be proven on the decoded-remote "
                "framebuffer, not a source-intent one (09).")

    # ---- persistence -----------------------------------------------------
    def write(self) -> Path:
        path = self.root / "manifest.json"
        path.write_text(json.dumps(_to_jsonable(self.manifest), indent=2,
                                   sort_keys=False))
        return path

    @classmethod
    def load(cls, root: Path | str) -> "EvidenceBundle":
        root = Path(root)
        data = json.loads((root / "manifest.json").read_text())
        return cls(root, _manifest_from_jsonable(data))


# --------------------------------------------------------------------------
def _to_jsonable(m: Manifest) -> dict:
    d = asdict(m)
    return d


def _manifest_from_jsonable(d: dict) -> Manifest:
    if d.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported evidence schema {d.get('schema_version')}")
    topo = Topology(**d.get("topology", {}))
    caps = [Capture(**c) for c in d.get("captures", [])]
    orc = [OracleRecord(**o) for o in d.get("oracle", [])]
    return Manifest(
        schema_version=d["schema_version"], scenario=d.get("scenario", ""),
        step=d.get("step", ""), generation=d.get("generation"),
        topology=topo, captures=caps, oracle=orc, logs=d.get("logs", []),
        passed=d.get("passed"), notes=d.get("notes", []),
    )
