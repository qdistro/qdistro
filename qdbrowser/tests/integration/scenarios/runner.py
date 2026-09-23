"""Drive a running qdbrowser through a sequence of agent-side
scenarios. Each scenario saves a PNG screenshot to ``out/`` and prints
journal-style lines to stdout (so a wrapping bats / journalctl harness
can assert on text instead of pixels).

Usage:
    QDBROWSER_AGENT_CONTROL=1 python3 -m qdbrowser &   # in another shell
    python3 tests/integration/scenarios/runner.py
"""

from __future__ import annotations

import argparse
import base64
import importlib
import os
import pkgutil
import sys
import time

from _client import Client


def emit(tag: str, **kv):
    """One journal-style log line. Loud, single-line, key=value."""
    parts = [tag] + [f"{k}={v}" for k, v in kv.items()]
    print(" ".join(parts), flush=True)


def save_png(png_b64: str, out_dir: str, name: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name + ".png")
    with open(path, "wb") as f:
        f.write(base64.b64decode(png_b64))
    return path


def run_scenario(name: str, fn, client: Client, out_dir: str) -> bool:
    emit("qdbrowser.scenario.start", name=name)
    t0 = time.time()
    try:
        fn(client, out_dir)
    except AssertionError as exc:
        emit("qdbrowser.scenario.fail", name=name,
             elapsed_ms=int((time.time() - t0) * 1000), reason=repr(exc))
        return False
    except Exception as exc:  # noqa: BLE001
        emit("qdbrowser.scenario.error", name=name,
             elapsed_ms=int((time.time() - t0) * 1000), reason=repr(exc))
        return False
    emit("qdbrowser.scenario.pass", name=name,
         elapsed_ms=int((time.time() - t0) * 1000))
    return True


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--socket")
    p.add_argument("--out", default="/tmp/qdbrowser-scenarios")
    p.add_argument("scenarios", nargs="*",
                   help="Scenario names to run (default: all)")
    args = p.parse_args(argv)

    client = Client(args.socket)
    client.connect()

    # Discover scenario_*.py modules in this directory.
    here = os.path.dirname(__file__)
    sys.path.insert(0, here)
    modules = []
    for _, mod_name, _ in pkgutil.iter_modules([here]):
        if mod_name.startswith("scenario_"):
            modules.append((mod_name[len("scenario_"):],
                            importlib.import_module(mod_name)))
    if args.scenarios:
        modules = [(n, m) for n, m in modules if n in args.scenarios]

    if not modules:
        emit("qdbrowser.scenario.none-found")
        sys.exit(2)

    failed = 0
    for name, mod in modules:
        if not hasattr(mod, "run"):
            emit("qdbrowser.scenario.skip", name=name, reason="no_run_fn")
            continue
        ok = run_scenario(name, mod.run, client, args.out)
        if not ok:
            failed += 1

    emit("qdbrowser.scenario.summary",
         total=len(modules), failed=failed)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
