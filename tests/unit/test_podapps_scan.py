from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCANNER = ROOT / "tier2" / "podapps-scan.sh"


def _fake_podman(bindir: Path) -> None:
    podman = bindir / "podman"
    podman.write_text(
        "#!/bin/bash\n"
        "case \"$1\" in\n"
        "  ps) echo tier2-c-ui ;;\n"
        "  inspect) echo qdistro/tier2-weston-terminal:latest ;;\n"
        "  exec)\n"
        "    if [ \"${3:-}\" = bash ]; then\n"
        "      echo /usr/share/applications/weston-terminal.desktop\n"
        "    elif [ \"${3:-}\" = cat ]; then\n"
        "      sleep \"0.${FAKE_SCAN_DELAY:-1}\"\n"
        "      printf '[Desktop Entry]\\nType=Application\\nName=Weston Terminal\\nIcon=terminal\\nExec=weston-terminal\\n'\n"
        "    fi\n"
        "    ;;\n"
        "esac\n"
    )
    podman.chmod(0o755)


def test_scans_use_process_unique_cache_temporary_files() -> None:
    source = SCANNER.read_text()
    assert 'mktemp "$CACHE_DIR/.apps.json.' in source
    assert '"$CACHE_DIR/apps.json.tmp"' not in source


def test_concurrent_scans_publish_complete_json_atomically(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_podman(bindir)
    cache = tmp_path / "cache"

    def scan(delay: int) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.update(
            {
                "FAKE_SCAN_DELAY": str(delay),
                "PATH": f"{bindir}:{env['PATH']}",
                "QDISTRO_PODAPPS_CACHE": str(cache),
            }
        )
        return subprocess.run(
            [str(SCANNER), "tier2-c-ui"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    # Different delays force the writers to overlap and finish out of order.
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(scan, range(1, 9)))

    assert all(result.returncode == 0 for result in results), [
        (result.returncode, result.stdout, result.stderr) for result in results
    ]
    apps = json.loads((cache / "tier2-c-ui" / "apps.json").read_text())
    assert apps == [
        {
            "appId": "tier2-c-ui/weston-terminal",
            "container": "tier2-c-ui",
            "workload": "weston-terminal",
            "name": "Weston Terminal",
            "iconName": "terminal",
            "comment": "",
            "execArgv": ["weston-terminal"],
            "silo": "tier2/tier2-c-ui",
        }
    ]
    assert list((cache / "tier2-c-ui").glob(".apps.json.*")) == []
