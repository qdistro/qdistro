#!/usr/bin/env python3
"""qdistro-vault-recovery — owner-side vault recovery bundle CLI wrapper.

Installed to /usr/local/bin/qdistro-vault-recovery. The implementation lives in
qdistro_vault_recovery_export.py next to the other qdistro_pwd_* modules in
/usr/libexec/qdistro; this thin wrapper puts that dir on sys.path and dispatches.

This is the documented `qdistro vault-recovery export` entrypoint (06-backup-dr
§3.4): it encrypts the live vault MASTER KEY to an owner recovery passphrase and
writes the ciphertext-only bundle that the daily backup collector folds into the
recovery/ set. Run it as root, out-of-band, with the vault secret + the recovery
passphrase present.

Usage:
    qdistro-vault-recovery export --vault main \\
        --out /etc/qdistro/recovery/vault-recovery.json
"""
from __future__ import annotations

import os
import sys

# The flat module layout installs the implementation + its qdistro_pwd_* deps
# into the same libexec dir as this wrapper's resolved target. Put that dir on
# the path so the imports resolve regardless of CWD.
_LIBEXEC = os.environ.get("QDISTRO_LIBEXEC", "/usr/libexec/qdistro")
for _cand in (os.path.dirname(os.path.realpath(__file__)), _LIBEXEC):
    if _cand and _cand not in sys.path:
        sys.path.insert(0, _cand)

from qdistro_vault_recovery_export import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
