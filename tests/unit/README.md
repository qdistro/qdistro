# phase1 tests

Pure-Python unit tests for the broker's cache + audit modules and the
`qdistro-approvals` CLI. No D-Bus, no Qt, no display required — runs
in <1s on the host.

## Run

```bash
# from qdistro/
pytest

# or from repo root
pytest qdistro/

# with verbose
pytest qdistro/ -v
```

If pytest isn't installed system-wide, use a venv:

```bash
python3 -m venv ~/.local/share/qdistro-test-venv
~/.local/share/qdistro-test-venv/bin/pip install pytest
~/.local/share/qdistro-test-venv/bin/pytest qdistro/
```

## What's tested

| File | Subject |
|---|---|
| `test_cache.py` | `ApprovalCache` lookup/store/gc, scope precedence, expiry, uid/action isolation |
| `test_audit.py` | `AuditLog` row writes, schema, indexes |
| `test_scope_roundtrip.py` | scope-string ⇄ db-row decode is consistent for all persisted scopes |
| `test_cli.py` | `qdistro-approvals` subcommands (list/revoke/audit/gc), root check, exit codes, output |

`test_permission.py` is the **in-VM acceptance script** (deployed to
`/usr/local/bin/qdistro-test-permission`), not a unit test. It's
excluded from collection in `conftest.py`.

## What's NOT tested here (deliberately)

- **Broker D-Bus surface** — needs a real bus or extensive fakery.
 Validated via the in-VM walk in and
 .
- **SDK `qdistro_app.request()`** — same; trivial wrapper, exercised
 end-to-end by the in-VM acceptance script.
- **Admin app PyQt6 UI** — would need `pytest-qt` + `QT_QPA_PLATFORM=offscreen`.
 Worth adding when the UI grows beyond the current ~150 LOC.

The two-layer split (in-process unit + VM integration) follows
`doc/dev.md`.
