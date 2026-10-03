"""Keep the workflow VM driver's inline Python valid without starting a VM."""

import json
import re
import subprocess
import sys
from pathlib import Path


DRIVER = Path(__file__).resolve().parents[1] / "integration" / "vm" / "s130-workflow-headless.sh"


def test_inline_python_compiles_and_seed_probe_matches_name() -> None:
    source = DRIVER.read_text()
    snippets = re.findall(r"python3 -c '([^']*)'", source, re.DOTALL)
    assert snippets
    assert len(snippets) == source.count("python3 -c '")
    for snippet in snippets:
        compile(snippet, str(DRIVER), "exec")

    seed_probe = next(code for code in snippets if 'print("ready" if' in code)
    for workflows, expected in [
        ([{"name": "wfhl-approval"}], "ready"),
        ([{"name": "wfhl-tick"}], "wfhl-tick"),
    ]:
        result = subprocess.run(
            [sys.executable, "-c", seed_probe, "wfhl-approval"],
            input=json.dumps(workflows), text=True, capture_output=True,
            check=True,
        )
        assert result.stdout.strip() == expected
