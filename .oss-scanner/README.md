# Anthropic OSS Scanner environment

This directory prepares qdistro for Anthropic's opt-in scanner. It does not
enroll the project. The Dockerfile follows the `/src` build-context contract
in https://github.com/anthropics/oss-scanner. Dependencies are fetched online
only while constructing the image. The audit and rebuild/test hooks work
without a network or host cache mounts.

## Local check

From the repository root:

```sh
podman build --layers -f .oss-scanner/Dockerfile -t localhost/qdistro/oss-scanner:local .
bash .oss-scanner/check-offline.sh localhost/qdistro/oss-scanner:local
```

Docker also works; set `CONTAINER_ENGINE=docker` for the offline check. The
check tests network isolation, reinstalls both extension dependency sets from
the image's npm cache offline, touches native sources to force recompilation,
rebuilds the SIP binding, configures native Meson builds from scratch, runs
every headless group and exercises ASan/UBSan logic/frame-parser tests. It does not mount host dependencies.
Logs go to `ci/runs/oss-scanner-local/` by default. Every failing step is fatal.
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

`test.sh smoke` runs during image construction. `test.sh all` is the full
headless audit lane, not the VM/GUI/release gate. See `threat_model.md` for
component priorities, severity guidance and runtime limitations.

The native build follows the sequence used by the small GitHub VM-image
workflow, but retains development tools and build outputs instead of producing
a bootable QCOW2. It caps Ninja and Quickshell parallelism at two and disables
Quickshell PCH to fit small audit environments.

## Dependency snapshot maintenance

The Dockerfile currently pins the dated Tumbleweed dependency snapshot
20261007. This is independent of the cloud QCOW2 pin in `snapshot.conf`: the
scanner does not consume a cloud image. openSUSE history URLs expire; refresh
this ARG and rerun the offline check before they do. Do not assume cached
local layers prove a fresh online build still works. A retained package mirror
is needed if future builds must reproduce this snapshot after upstream expiry.

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
