# Anthropic OSS Scanner environment

This directory prepares qdistro for Anthropic's opt-in scanner. It does not
enroll the project. The Dockerfile follows the `/src` build-context contract
in https://github.com/anthropics/oss-scanner. Dependencies are fetched online
only while constructing the image. The audit and rebuild/test hooks work
without a network or host cache mounts.

## Local check

From the repository root:

```sh
podman build --layers --ignorefile .oss-scanner/Dockerfile.dockerignore -f .oss-scanner/Dockerfile -t localhost/qdistro/oss-scanner:local .
bash .oss-scanner/check-offline.sh localhost/qdistro/oss-scanner:local
```

Docker also works; set `CONTAINER_ENGINE=docker` for the offline check. The
Docker builder automatically uses the Dockerfile-specific ignore file;
Podman receives it explicitly. This excludes local run logs and VM disks
from the source context.
The check tests network isolation, reinstalls both extension dependency sets from
the image's npm cache offline, touches native sources to force recompilation,
rebuilds the SIP binding, configures native Meson builds from scratch, runs
every headless group and exercises ASan/UBSan logic/frame-parser tests. It does not mount host dependencies.
Logs go to `ci/runs/oss-scanner-local/` by default. Any failure makes the check exit nonzero; independent test groups continue
to collect evidence. Each check resolves its image tag to an immutable ID
and records that ID, inspection metadata and the final exit code.
As in qci's host runner, local headless checks disable container SELinux
labeling because fixtures use container processes as simulated trusted peers.
This does not exercise deployed SELinux enforcement; use the VM lane for that.
The runtime requests 2 CPUs and 8 GB RAM. If rootless container resource
controllers are unavailable, `OSS_SCANNER_RESOURCE_LIMITS=0` permits testing
network isolation and dependencies without claiming that memory envelope.
On such a host, CPU affinity must be set **inside** the container: Podman can
reset affinity inherited from the host. For example, use
`podman run --rm --network=none --entrypoint=taskset IMAGE -c 0,1 bash`.
Choose CPUs from the container's allowed set; verify with `os.sched_getaffinity`.

Tests use ordinary UID 1001, outside qdistro's reserved admin UID 1000.
`setup-runtime.sh` provides nested UID/GID ranges and an empty VFS Podman store
for template GC queries. This needs user namespaces but no image downloads.
The libvirt client is present for absent-domain helper tests; no VM daemon,
guest disk or host libvirt socket is provided.

`test.sh smoke` runs during image construction. `test.sh all` runs the scanner headless lane. It excludes the desktop
shell's integration lane and the terminal's printer-dependent test; it does
not run the VM/GUI/release gates. See `threat_model.md` for
component priorities, severity guidance and runtime limitations.
Independent steps have qci's 600-second timeout, configurable through
`OSS_SCANNER_TEST_TIMEOUT`; the large root suite has qci's separate
1800-second budget (`OSS_SCANNER_ROOT_TEST_TIMEOUT`). The large terminal suite
also gets 1800 seconds on the two-CPU audit machine
(`OSS_SCANNER_QDTERM_TEST_TIMEOUT`); it progressed beyond 1400 checks before
the initial 600-second limit expired. A timeout or crash fails the check; remaining
groups and sanitizer tests still run so their results can be inspected.

The original scanner image exposed two notebook failures although project CI
passed the same product sources: a native crash in the full suite during
`test_pdf_export_matches_baseline_in_any_live_mode`, and a PDF comparison
failure when UTC timestamps cross a second boundary. The native crash stops
in `QTreeViewPrivate::layout`; its cause remains under investigation. The PDF
test originally recognized offset dates but not UTC `Z` dates. See current
local validation evidence for the status after correcting that normalizer.
The corrected terminal run passed 1,610 tests with two intentional skips.

Image construction uses available CPUs on the larger build machine. Offline
rebuilds default to two jobs. Quickshell PCH is disabled in both environments.
Selected sanitizer builds cover one compositor logic test and two daemon
tests; bundled libraries are not wholly instrumented.

## Dependency snapshot maintenance

`snapshot.conf` is the sole dependency snapshot pin. The Dockerfile uses an
immutable bootstrap image, reads the snapshot from the repository, runs
`zypper dup` against those dated repositories, and verifies the resulting
`VERSION_ID`. Changing `snapshot.conf` invalidates the dependency layer.
The bootstrap digest is not a second package snapshot setting.

openSUSE history URLs expire after roughly a month. Keep the shared pin
current and rerun a clean online build plus the offline check whenever it
changes. Cached layers do not establish availability from upstream mirrors.
A retained package mirror is needed for reproduction after upstream expiry.

## Enrollment preparation

`.oss-scanner/enrollment/project.yaml` is a local draft for
`projects/qdistro/project.yaml` in Anthropic's repository. The report address
must be confirmed as monitored before submission. The Dockerfile and guidance
must first be available at the configured public repository/ref; unpublished
worktree files cannot be tested by the stock `tools/check` anonymous clone.

Validate the draft with the official `tools/validate.py`. Once these files are
published, run `tools/check qdistro` (or `--qemu`) for an end-to-end check,
including Anthropic's additional tools layer. Local checks use the same
Dockerfile and offline contract but do not substitute for that final clone.
No enrollment PR or publish action is performed by these scripts.
