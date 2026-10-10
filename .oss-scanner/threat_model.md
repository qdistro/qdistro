# qdistro scanner guidance

qdistro is an early-stage, single-owner Linux distribution with an authorization service and several
application isolation mechanisms. It is not yet widely deployed.
Read `doc/overview.md`, `doc/threat-model.md`, `doc/isolation-tiers.md`,
`doc/permissions.md`, `doc/architecture.md` and `doc/glossary.md` first.
Those documents define the guarantees; this file provides audit navigation.

## Security guarantees and scope

This is a desktop for one human owner; the additional Unix accounts separate
application contexts, not different human users. Read `doc/threat-model.md`
before judging a behavior as a violation. The project targets accidental data
leaks and containment of compromised applications, but explicitly does not
promise containment of actively hostile sessions outside virtual machines,
or protection against malicious application authors through cooperative SDK
flags. Treat these limits as part of the guarantee, not as bugs to report.

In scope: authorization and user-approval decisions, caller authentication,
resource ownership across separate Unix accounts and configured containers,
compositor client separation, privilege helpers, and browser/remote-message
inputs. For each finding name the actual configured boundary and demonstrate
the documented check that should have denied the action. A parser defect or
approval bypass in a trusted service can be in scope without asserting a
complete hostile-code sandbox guarantee.

Out of scope: file access among applications sharing an account or running
without isolation; bypass of cooperative application flags by malicious
application authors; host-kernel exploits, hardware attacks and side channels;
and adversarial escape qualification for experimental virtual-machine or
paravirtualized environments. Do not treat the absence of an undocumented
sandbox guarantee as a vulnerability. Revisit experimental isolation scope
only when maintainers explicitly extend this guidance.

Include bundled dependency defects when project patches or integration make
them reachable from the interfaces above. Unrelated upstream issues without
project reachability are outside this audit. Trusted administrator actions
are not privilege escalations merely because they access application data.

## Priority surfaces and attacker capabilities

* `broker/`, `session_manager/`, `workflow/`, `templates/`: decisions about
  cross-application authority, approvals, resource handles, revocation and provenance.
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
project's userspace guarantee. Unix-account, SELinux and container isolation share the host kernel.
Virtual-machine isolation has separate guarantees and is experimental. Cooperative first-party SDK flags
are not promised as kernel-enforced protection against malicious app authors.
Do not infer stronger guarantees from the word "isolation" alone.

## Build and investigation commands

The checkout is `/src`. Dependencies and npm downloads are installed during
image construction. There is no network during the audit. Do not invoke the
bootstrap installer, `qci full`, VM provisioning or distro-image builders here.
The image includes an empty VFS Podman store for GC membership-query tests;
that does not provide prebuilt application container images or nested VM support.

* `bash .oss-scanner/build.sh`: incremental native rebuild as root. Production
  vendored libweston, vendored Quickshell, qdwin, daemons and qdshell are built;
  qsu is at `qsu/build-oss/qsu`; the QTermWidget binding and six policy modules
  are built too. Debug info and frame pointers are retained. Native component
  build directories are `<component>/build-oss`. libweston/Quickshell retain
  their own build directories under their vendored source trees.
* `bash .oss-scanner/test.sh`: default smoke checks (focused Python, native
  components and both extensions). Optional groups are `all`, `root`, `native`,
  `apps`, `extensions`; `all` runs the broader headless diagnostic lane. Tests run as UID 1001 with a
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

The optional root suite is batched using the same generator as qci.
Experimental isolation probe tests require a non-permissive host
`kernel.yama.ptrace_scope`; the helper Debian VM defaults to 0 and fails four
checks. A full-suite failure is not evidence of a vulnerability by itself.
Use focused tests with their documented prerequisites and retain failure logs. In the optional `all` lane, the desktop shell's integration tests are
excluded; native, Python, QML and JavaScript checks still run. Browser tests use
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
admin/host, or broad cross-application secret access without required approval.
High: demonstrated unauthorized access to another application's data, approval bypass,
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
