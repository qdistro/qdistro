from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SPAWN = REPO_ROOT / "tier4-vm" / "spawn-tier4.sh"
PUBLISHER = REPO_ROOT / "tier4-vm-guest" / "qdistro-tier4-publisher.sh"
BUILD_GUEST_IMAGE = REPO_ROOT / "tier4-vm-guest" / "build-guest-image.sh"
PUBLISHER_EXAMPLE = REPO_ROOT / "tier4-vm-guest" / "tier4-publisher.conf.example"


def test_spawn_sources_config_without_overriding_env(tmp_path: Path):
    cfg = tmp_path / "tier4.conf"
    cfg.write_text(
        "TIER4_STREAMING_METHOD=rdp\n"
        "TIER4_RDP_SUBSCRIBE=last\n"
    )
    script = f"""
set -eu
export TIER4_CONFIG={shlex.quote(str(cfg))}
export TIER4_DISPLAY=waypipe
source {shlex.quote(str(SPAWN))} --source-only
load_tier4_config
if [ "${{TIER4_DISPLAY}}" != waypipe ]; then
    echo "env override lost: $TIER4_DISPLAY" >&2
    exit 1
fi
"""
    cp = subprocess.run(["bash", "-c", script], text=True,
                        capture_output=True, timeout=10)
    assert cp.returncode == 0, cp.stderr


def test_spawn_accepts_rdp_template_resolution():
    script = f"""
set -eu
source {shlex.quote(str(SPAWN))} --source-only
resolve_template rdp {shlex.quote(str(SPAWN.parent))}
"""
    cp = subprocess.run(["bash", "-c", script], text=True,
                        capture_output=True, timeout=10)
    assert cp.returncode == 0, cp.stderr
    assert "tier4-vm-guest/domain-template.xml" in cp.stdout


def test_spawn_contains_rdp_transport_pipeline():
    text = SPAWN.read_text()
    assert "TIER4_RDP_FIXED_PORT=7880" in text
    assert "QDISTRO_TIER4_DISPLAY=rdp" in text
    assert "VSOCK-CONNECT:$CID:$PORT" in text
    assert "/v:127.0.0.1:$RDP_LOCAL_PORT" in text
    assert "/from-stdin" in text
    assert "/p:$RDP_PASSWORD" not in text
    assert "systemctl stop qdistro-tier4-publisher.service" in text
    assert "runuser -u admin -- env XDG_RUNTIME_DIR=/run/user/1000" in text
    assert "tcp_loopback_accepts \"$RDP_LOCAL_PORT\"" in text
    assert "find_rdp_client" in text


def test_guest_image_bake_contains_rdp_runtime_and_forwarder():
    text = BUILD_GUEST_IMAGE.read_text()
    assert "socat" in text
    assert "pipewire" in text
    assert "freerdp" in text
    assert "meson compile -C \"$QDISTRO_DAEMONS_BUILD\" qdistro-forward" in text
    assert "--copy-in \"$QDISTRO_FORWARD:/usr/bin/\"" in text
    assert "test -x /usr/bin/qdistro-forward" in text
    assert "test -x /usr/bin/socat" in text
    assert "backend=headless-backend.so,pipewire-backend.so" in text
    assert "[pipewire]" in text
    assert "num-outputs=8" in text
    assert "qdistro-pipewire-admin.service" in text
    assert "systemctl enable qdistro-pipewire-admin.service" in text


def test_guest_publisher_example_documents_rdp_config():
    text = PUBLISHER_EXAMPLE.read_text()
    assert "QDISTRO_TIER4_STREAMING_METHOD=rdp" in text
    assert "QDISTRO_TIER4_RDP_SUBSCRIBE=last" in text
    assert "QDISTRO_TIER4_RDP_CREDS=/run/qdistro-tier4-rdp.env" in text


def test_guest_publisher_rdp_dry_run_writes_subscription_and_bridge(tmp_path: Path):
    dry = tmp_path / "dry.log"
    sock_dir = tmp_path / "run"
    sock_dir.mkdir()
    (sock_dir / "wayland-0").touch()
    env = os.environ.copy()
    env.update(
        {
            "QDISTRO_TIER4_DRY_RUN": "1",
            "QDISTRO_TIER4_DRY_OUT": str(dry),
            "QDISTRO_TIER4_DISPLAY": "rdp",
            "QDISTRO_TIER4_RDP_SUBSCRIBE": "last",
            "XDG_RUNTIME_DIR": str(sock_dir),
        }
    )
    cp = subprocess.run(
        ["bash", str(PUBLISHER), "7880"],
        env=env, text=True, capture_output=True, timeout=10,
    )
    assert cp.returncode == 0, cp.stderr
    out = dry.read_text()
    assert "display=rdp" in out
    assert "--subscribe last" in out
    assert "VSOCK-LISTEN:7880" in out
    assert "TCP:127.0.0.1:$RDP_PORT" in out


def test_guest_publisher_waits_for_rdp_tcp_before_publishing_creds():
    text = PUBLISHER.read_text()
    assert "tcp_loopback_accepts \"$RDP_PORT\"" in text
    assert "qdistro-forward did not accept TCP" in text
    assert "install -m 0600 \"$CREDS_TMP\" \"$RDP_CREDS_PATH\"" in text


def test_guest_publisher_streaming_method_config_alias(tmp_path: Path):
    cfg = tmp_path / "publisher.conf"
    dry = tmp_path / "dry.log"
    sock_dir = tmp_path / "run"
    sock_dir.mkdir()
    (sock_dir / "wayland-0").touch()
    cfg.write_text(
        "QDISTRO_TIER4_STREAMING_METHOD=rdp\n"
        "QDISTRO_TIER4_RDP_SUBSCRIBE=last\n"
    )
    env = os.environ.copy()
    env.update(
        {
            "QDISTRO_TIER4_DRY_RUN": "1",
            "QDISTRO_TIER4_DRY_OUT": str(dry),
            "QDISTRO_TIER4_CONFIG": str(cfg),
            "XDG_RUNTIME_DIR": str(sock_dir),
        }
    )
    cp = subprocess.run(
        ["bash", str(PUBLISHER), "7880"],
        env=env, text=True, capture_output=True, timeout=10,
    )
    assert cp.returncode == 0, cp.stderr
    assert "display=rdp" in dry.read_text()
