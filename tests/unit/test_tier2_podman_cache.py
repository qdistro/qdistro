"""Golden-cache builder stages qfileman sources into that workload only."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_SCRIPT = ROOT / "scripts" / "vm" / "build-tier2-podman-cache.sh"
BOOTSTRAP = ROOT / "scripts" / "vm" / "fresh-vm-bootstrap.sh"

WORKLOADS_LINE = "workloads=(weston-terminal text-viewer url-preview qfileman)"
LS_FILES_BLOCK = (
    'git -C "$repo" ls-files -z --cached --others --exclude-standard -- \\\n'
    "    tier2 qdfileman sdk/presentation |"
)
STAGE_QDFILEMAN = 'cp -a "$work/context/qdfileman" "$wcontext/qdfileman"'
STAGE_PRESENTATION = (
    'cp -a "$work/context/sdk/presentation" "$wcontext/presentation"'
)
STRIP_CONSUMER = 'rm -rf "$wcontext/consumer"'
BOOTSTRAP_LOOP = "for _w in weston-terminal text-viewer url-preview qfileman; do"
BOOTSTRAP_LOG = (
    "tier-2 images pre-built: weston-terminal, text-viewer, url-preview, qfileman"
)
BOOTSTRAP_FETCH = (
    'wget -nv -O /var/tmp/qdistro-tier2-images.tar "$HOST/tier2-images.tar" \\'
)
BOOTSTRAP_LOAD = (
    "runuser -u admin -- podman load -i /var/tmp/qdistro-tier2-images.tar \\"
)
BOOTSTRAP_TMP_DEST = " /tmp/qdistro-tier2-images.tar"


def _write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _fake_podman(tmp_path: Path) -> tuple[Path, Path, Path]:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    calls = tmp_path / "podman-calls"
    listing = tmp_path / "context-listing"
    _write_exec(
        fakebin / "podman",
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {calls}\n"
        "case \"$1\" in\n"
        "  info) echo true ;;\n"
        "  image)\n"
        "    case \"$2\" in\n"
        "      exists) exit 0 ;;\n"
        "      inspect) echo sha256:fake-base ;;\n"
        "      *) exit 3 ;;\n"
        "    esac ;;\n"
        "  build)\n"
        "    last=\n"
        "    for a; do last=$a; done\n"
        f"    {{ echo CONTEXT=\"$last\"; ls -1 \"$last\"; }} >> {listing}\n"
        "    exit 0 ;;\n"
        "  save)\n"
        "    out=\n"
        "    prev=\n"
        "    for a; do\n"
        "      if [ \"$prev\" = --output ]; then out=$a; fi\n"
        "      prev=$a\n"
        "    done\n"
        "    tmpd=$(mktemp -d)\n"
        "    echo '{}' > \"$tmpd/manifest.json\"\n"
        "    tar -C \"$tmpd\" -cf \"$out\" manifest.json\n"
        "    rm -rf \"$tmpd\"\n"
        "    exit 0 ;;\n"
        "  *) exit 3 ;;\n"
        "esac\n",
    )
    return fakebin, calls, listing


def _runtime_dir(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    sock = runtime / "bus"
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(sock))
    s.close()
    return runtime


def _run_cache(tmp_path: Path, extra_bin: Path | None = None) -> subprocess.CompletedProcess[str]:
    fakebin, _calls, _listing = _fake_podman(tmp_path)
    runtime = _runtime_dir(tmp_path)
    path = str(fakebin)
    if extra_bin is not None:
        path = f"{extra_bin}:{path}"
    env = {
        **os.environ,
        "PATH": f"{path}:{os.environ['PATH']}",
        "QDWIN_CACHE_DIR": str(tmp_path / "cache"),
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/dev/null",
        "QCI_OFFLINE": "0",
    }
    return subprocess.run(
        ["bash", str(CACHE_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )


def test_cache_script_source_pins_qfileman_and_consumer_trees():
    text = CACHE_SCRIPT.read_text()
    bootstrap = BOOTSTRAP.read_text()
    assert WORKLOADS_LINE in text
    assert LS_FILES_BLOCK in text
    assert STAGE_QDFILEMAN in text
    assert STAGE_PRESENTATION in text
    assert STRIP_CONSUMER in text
    assert BOOTSTRAP_LOOP in bootstrap
    assert BOOTSTRAP_LOG in bootstrap
    assert BOOTSTRAP_FETCH in bootstrap
    assert BOOTSTRAP_LOAD in bootstrap
    assert BOOTSTRAP_TMP_DEST not in bootstrap
    assert "wget -q -O /var/tmp/qdistro-tier2-images.tar" not in bootstrap


def test_cache_script_builds_qfileman_context_with_consumer_sources(tmp_path):
    proc = _run_cache(tmp_path)
    assert proc.returncode == 0, proc.stderr
    listing = (tmp_path / "context-listing").read_text()
    calls = (tmp_path / "podman-calls").read_text()
    blocks = [b for b in listing.split("CONTEXT=") if b.strip()]
    assert len(blocks) == 4, listing

    def names_for(workload: str) -> set[str]:
        marker = f"wcontext.{workload}."
        matched = [b for b in blocks if b.split("\n", 1)[0].find(marker) >= 0]
        assert len(matched) == 1, listing
        return set(matched[0].split())

    for other in ("weston-terminal", "text-viewer", "url-preview"):
        names = names_for(other)
        assert "qdfileman" not in names, other
        assert "presentation" not in names, other
        assert "consumer" not in names, other
    qfileman = names_for("qfileman")
    assert "qdfileman" in qfileman
    assert "presentation" in qfileman
    assert "Containerfile.qfileman" in qfileman
    assert "--tag qdistro/tier2-qfileman:latest" in calls
    assert "--tag qdistro/tier2-weston-terminal:latest" in calls
    stdout_paths = [line for line in proc.stdout.splitlines() if line.endswith(".tar")]
    assert stdout_paths, proc.stdout
    assert Path(stdout_paths[-1]).is_file()


def test_cache_script_fails_without_qfileman_sources(tmp_path):
    extra = tmp_path / "gitbin"
    extra.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    _write_exec(
        extra / "git",
        "#!/bin/sh\n"
        "repo=\n"
        'if [ "$1" = "-C" ]; then repo=$2; shift 2; fi\n'
        'if [ "$1" = "ls-files" ]; then\n'
        f'  exec {real_git} -C "$repo" ls-files -z '
        "--cached --others --exclude-standard -- tier2\n"
        "fi\n"
        f'exec {real_git} -C "${{repo:-.}}" "$@"\n',
    )
    proc = _run_cache(tmp_path, extra_bin=extra)
    assert proc.returncode == 2, proc.stderr
    assert "qdfileman sources missing from cache context" in proc.stderr
    listing = (tmp_path / "context-listing").read_text()
    blocks = [b for b in listing.split("CONTEXT=") if b.strip()]
    assert len(blocks) == 3, listing
    for block in blocks:
        names = set(block.split())
        assert "qdfileman" not in names
        assert "presentation" not in names
