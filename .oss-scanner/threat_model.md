# qdistro scanner guidance

qdistro is an early-stage, single-owner Linux distribution with brokered
application silos and graduated isolation. It is not yet widely deployed.
Read `doc/overview.md`, `doc/threat-model.md`, `doc/isolation-tiers.md`,
`doc/permissions.md`, `doc/architecture.md` and `doc/glossary.md` first.
Those documents define the guarantees; this file provides audit navigation.

## Priority surfaces and attacker capabilities

* `broker/`, `session_manager/`, `workflow/`, `templates/`: decisions about
  cross-silo authority, approvals, resource handles, revocation and provenance.
  Exercise requests from an unprivileged caller, stale processes/handles,
  mismatched UID/session identities, reordered events and concurrent requests.
* `qdwin/qdwin/`, `daemons/`: Wayland client identity, protocol lifetimes,
  clipboard/input/capture boundaries, nested surfaces and frame/RDP inputs.
  Distinguish privileged compositor extensions from ordinary client requests.
* `browser_bridge/`, `browser_daemons/`, both browser extensions,
  `multimachine/`, `media/`: native-message/frame parsing, sender identity,
  authorization, connection binding and untrusted remote/content inputs.
  State whether access requires pairing/authentication or a compromised app.
* `qsu/`, `polkit/`, `pwd/`, `print/`, `user_relay/`, `sdk/`: caller identity,
  approval scope, paths/file descriptors, vault selection and cross-UID flows.
* `selinux/` and launch/install scripts: enforcement gaps, fail-open behavior,
  incorrect ownership/labels and gaps between installed and intended policy.

The owner/admin and its trusted compositor are policy authorities. A report
must show an attacker gaining authority it did not already possess. Root/admin
can already read user data; requiring that authority is not an escalation.
Host-kernel compromise, hardware attacks and side channels are outside the
project's userspace guarantee. Tier 0-3 share the host kernel. VM tiers have
separate guarantees and are experimental. Cooperative first-party SDK flags
are not promised as kernel-enforced protection against malicious app authors.
Do not infer stronger guarantees from the word "isolation" alone.

## Build and investigation commands

The checkout is `/src`. Dependencies and npm downloads are installed during
image construction. There is no network during the audit. Do not invoke the
bootstrap installer, `qci full`, VM provisioning or distro-image builders here.
The image includes an empty VFS Podman store for GC membership-query tests;
that does not provide prebuilt silo/workload images or nested VM support.

* `bash .oss-scanner/build.sh`: incremental native rebuild as root. Production
  vendored libweston, vendored Quickshell, qdwin, daemons and qdshell are built;
  qsu is at `qsu/build-oss/qsu`; the QTermWidget binding and six policy modules
  are built too. Debug info and frame pointers are retained. Native component
  build directories are `<component>/build-oss`. libweston/Quickshell retain
  their own build directories under their vendored source trees.
* `bash .oss-scanner/test.sh`: complete headless lane. Optional groups are
  `smoke`, `root`, `native`, `apps`, `extensions`. Tests run as UID 1001 with a
  private D-Bus session and offscreen Qt. Failures propagate to the exit code.
* `bash .oss-scanner/build-sanitized.sh qdwin`: optional separate ASan/UBSan
  native build; also accepts `daemons`. Vendored dependencies
  remain ordinary builds; this does not instrument the entire desktop.
* `bash .oss-scanner/shell.sh`: interactive unprivileged investigation shell;
  append a command to execute it in that same environment.
* Python imports use each component's pytest configuration; run a targeted
  reproducer from the corresponding directory. Native tests are listed with
  `meson test -C qdwin/build-oss --list` and equivalent daemon/shell commands.
* Package versions: `/opt/scanner-rpms.txt`. Build parallelism defaults to two.

The root suite is batched using the same generator as qci. Browser tests use
one process per file. qdterm's printer-coupled test is excluded exactly as in
qci; runtime printing requires the dedicated VM. Source-invariant and mocked
backend tests do not demonstrate runtime enforcement. Report skips explicitly.

## Runtime limits

This image is a development environment, not a booted qdistro installation.
Private D-Bus fixtures are not the installed system broker. Compiling policy
and running the broker negative control checks policy correctness, not actual
SELinux enforcement. Hardware, real login/locking sessions, installed service
ownership and container/VM escape tests need qdistro's disposable VM lane.
See `ci/README.md` and `scripts/vm/test-vm-suites.sh`. Do not claim those tests
passed merely because their headless counterparts passed.

## Severity and reports

Critical: demonstrated unprivileged or remote compromise of the trusted
admin/host, or broad cross-silo secret access without required approval.
High: demonstrated unauthorized access to another silo's data, approval bypass,
peer/session identity confusion granting authority, or reachable memory
corruption in a trusted service. Reachability and exploitability determine
severity; memory corruption is not automatically critical.
Medium: attacker-triggerable denial of service or bounded confidentiality/
integrity failures. Lower-impact hardening opportunities should be separated.
Explain assumptions and uncertainty instead of asserting a severity from a
source pattern alone. This rubric guides triage; maintainers make the decision.

Each finding should include the exact commit, affected interface, attacker
identity/capabilities, prerequisites, violated documented guarantee, minimal
executable reproducer, observed versus expected result, and a candidate patch
with a regression check. Record which checks could run in this image and which
need a VM. Deduplicate by root cause. Avoid formatting-only patches to vendored
code; include qdistro integration and local vendor modifications in the audit.
