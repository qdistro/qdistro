#!/usr/bin/env bash
# Image-local configuration: no host mounts and no network downloads.
set -euo pipefail
# The outer rootless container maps 0..65535. Keep nested mappings inside
# that range and outside scanner's own UID/GID (1001).
printf 'scanner:2000:32000\n' > /etc/subuid
printf 'scanner:2000:32000\n' > /etc/subgid
mkdir -p /etc/containers
# GC unit tests query a real, empty image store. VFS avoids requiring a
# nested overlay/FUSE mount; no workload images are pulled into this store.
cat > /etc/containers/storage.conf <<'CONF'
[storage]
driver = "vfs"
CONF
cat > /etc/containers/containers.conf <<'CONF'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
CONF
