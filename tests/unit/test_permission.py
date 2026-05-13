#!/usr/bin/env python3
"""Phase 1 acceptance: from a non-admin user, request a permission and
print whether it was allowed. Exits 0 on allow, 1 on deny/timeout.
"""
import sys

import qdistro_app


def main() -> int:
    result = qdistro_app.request("test.action", details={"purpose": "smoke test"})
    print("ALLOWED" if result else "DENIED", flush=True)
    return 0 if result else 1


if __name__ == "__main__":
    sys.exit(main())
