"""Source-invariant checks for the dedicated qdlocker PAM service.

These tests read the checked-in repo files only — no VM, no live PAM
stack, no Qt. They lock in the harden-qdlocker findings 01 (dedicated
PAM service) + 03 (explicit pam_faillock brute-force lockout) so a
later edit cannot silently re-borrow the `login` stack or drop a
faillock line.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# tests/unit/test_pam_service.py -> repo root is two parents up.
REPO_ROOT = Path(__file__).parents[2]
UNIT_FILE = REPO_ROOT / "systemd" / "qdlocker.service"
PAM_FILE = REPO_ROOT / "pam" / "qdlocker"


@pytest.mark.cheat_aware(
    protects="screen-unlock is gated by a dedicated PAM service with an "
    "explicit pam_faillock lockout (preauth+authfail+authsucc), not the "
    "borrowed `login` stack",
    severity="critical",
    cheats=[
        "drop the authsucc line so a correct password never clears the tally",
        "point QDLOCKER_PAM_SERVICE back at login",
        "assert only that the file exists, not that faillock lines are present",
    ],
    consequence="screen-unlock password guessing is unbounded, or legit "
    "users accumulate permanent lockouts",
)
def test_unit_points_at_dedicated_pam_service():
    text = UNIT_FILE.read_text()
    assert "Environment=QDLOCKER_PAM_SERVICE=qdlocker" in text, (
        "qdlocker.service must point QDLOCKER_PAM_SERVICE at the dedicated "
        "`qdlocker` PAM service"
    )
    assert "Environment=QDLOCKER_PAM_SERVICE=login" not in text, (
        "qdlocker.service must NOT borrow the `login` PAM stack"
    )


@pytest.mark.cheat_aware(
    protects="screen-unlock is gated by a dedicated PAM service with an "
    "explicit pam_faillock lockout (preauth+authfail+authsucc), not the "
    "borrowed `login` stack",
    severity="critical",
    cheats=[
        "drop the authsucc line so a correct password never clears the tally",
        "point QDLOCKER_PAM_SERVICE back at login",
        "assert only that the file exists, not that faillock lines are present",
    ],
    consequence="screen-unlock password guessing is unbounded, or legit "
    "users accumulate permanent lockouts",
)
def test_pam_file_enforces_faillock_lockout():
    assert PAM_FILE.exists(), f"checked-in PAM file missing at {PAM_FILE}"
    text = PAM_FILE.read_text()

    # preauth + authfail + authsucc are all required: preauth+authfail
    # enforce the lockout, and authsucc clears the tally on a correct
    # password so legit users don't accumulate a permanent lockout.
    assert re.search(r"pam_faillock\.so\s+preauth", text), (
        "missing `pam_faillock.so preauth` line"
    )
    assert re.search(r"pam_faillock\.so\s+authfail", text), (
        "missing `pam_faillock.so authfail` line"
    )
    assert re.search(r"pam_faillock\.so\s+authsucc", text), (
        "missing `pam_faillock.so authsucc` line — a correct password would "
        "never clear the tally"
    )

    # The lockout policy itself.
    assert "deny=5" in text, "missing deny=5 lockout threshold"
    assert "unlock_time=10" in text, "missing unlock_time=10 recovery window"

    # Well-formed account management via the included common-account.
    assert re.search(r"^account\b.*\bcommon-account\b", text, re.MULTILINE), (
        "account stage must include common-account"
    )

    # Regression guard for the openSUSE faillock footgun (caught in the VM
    # lane): the AUTH phase must invoke pam_unix DIRECTLY, not via
    # `include`/`substack common-auth`. openSUSE's common-auth is
    # `auth required pam_unix.so` (not sufficient), so an include/substack
    # falls through to `authfail [default=die]` on a *correct* password and
    # bricks the locker (the right password is rejected). The behavioural
    # guarantee is the qdlocker-faillock VM test; this is the cheap static
    # tripwire so nobody "tidies" the stack back into the bricking form.
    auth_lines = [
        ln for ln in text.splitlines()
        if ln.strip().startswith("auth") and not ln.lstrip().startswith("#")
    ]
    auth_text = "\n".join(auth_lines)
    assert re.search(r"\bpam_unix\.so\b", auth_text), (
        "auth phase must invoke pam_unix directly"
    )
    assert "common-auth" not in auth_text, (
        "auth phase must NOT include/substack common-auth — on openSUSE that "
        "falls through to authfail on a correct password and bricks the locker"
    )


@pytest.mark.cheat_aware(
    protects="qdlocker.service disables core dumps (LimitCORE=0) so a crash "
    "cannot spill the unlock password (held in a non-zeroable Python str) to "
    "disk via systemd-coredump",
    severity="low",
    cheats=[
        "drop LimitCORE=0 so coredumps re-enable",
        "set LimitCORE to a non-zero value",
    ],
    consequence="a locker crash can write the plaintext password / prompt "
    "buffer into a core dump readable post-incident",
)
def test_unit_disables_core_dumps():
    """Finding 07: no core dumps for the locker unit."""
    text = UNIT_FILE.read_text()
    assert re.search(r"^\s*LimitCORE\s*=\s*0\s*$", text, re.MULTILINE), (
        "qdlocker.service must set LimitCORE=0 to keep the unlock password "
        "out of core dumps"
    )
