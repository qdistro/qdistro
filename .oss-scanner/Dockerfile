# Build context: the qdistro repository root (Anthropic's /src contract).
# The immutable bootstrap image is updated to snapshot.conf by zypper dup;
# snapshot.conf is the sole dependency version pin. No qci cache is required.
FROM registry.opensuse.org/opensuse/tumbleweed@sha256:e07d5a7a1fe1cf85e5fe9c0b33250ab8b4a45fa2a4a217ff536f9f00e6ee7928
ENV QT_QPA_PLATFORM=offscreen \
    PYTEST_QT_API=pyqt6 QT_API=pyqt6 QDISTRO_REQUIRE_PYQT6=1 \
    PATH=/opt/scanner-tools:/opt/scanner-python/bin:/usr/local/bin:/usr/bin:/bin \
    VIRTUAL_ENV=/opt/scanner-python
COPY scripts/vm/container-native-deps.sh /recipe/container-native-deps.sh
COPY snapshot.conf /recipe/snapshot.conf
RUN . /recipe/snapshot.conf \
 && QDISTRO_SUBSTRATE_SNAPSHOT="$snapshot" bash /recipe/container-native-deps.sh \
 && . /etc/os-release && test "$VERSION_ID" = "$snapshot"
COPY ci/containers/host-packages.txt /recipe/host-packages.txt
COPY .oss-scanner/install-deps.sh /recipe/install-deps.sh
RUN bash /recipe/install-deps.sh
# Exercise the file-manager's real local-tool integrations offline too.
RUN zypper -n install --no-recommends zip unzip 7zip xdg-utils util-linux-systemd udisks2 rclone
RUN zypper -n install --no-recommends wiggle mergiraf
RUN zypper -n install --no-recommends tmux mosh
COPY . /src
WORKDIR /src
# Policy tooling and user management install executables in /usr/sbin.
ENV PATH=/opt/scanner-tools:/opt/scanner-python/bin:/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin
# openSUSE's normal /run/tmux path is initialized by systemd-tmpfiles.
# The scanner is systemd-less; tmux creates private UID-owned sockets in /tmp.
ENV TMUX_TMPDIR=/tmp
# Never ship a linked-worktree pointer to host Git metadata. Anthropic's
# normal clone has a real .git directory, which is retained.
RUN if test -f .git; then rm .git; fi
RUN OSS_SCANNER_JOBS="$(nproc)" QDSHELL_QS_BUILD_JOBS="$(nproc)" bash .oss-scanner/build.sh
# Image construction has a larger machine; offline investigation has 2 CPUs.
ENV OSS_SCANNER_JOBS=2 QDSHELL_QS_BUILD_JOBS=2
RUN bash ci/containers/prepare-host.sh
RUN useradd -m -u 1001 -s /bin/bash scanner \
 && chown -R scanner:scanner /src \
 && git config --system --add safe.directory /src \
 && rpm -qa | sort > /opt/scanner-rpms.txt
# Keep build directories, development headers, generated protocols and npm
# caches: the offline agent must be able to rebuild and write reproducers.
RUN bash .oss-scanner/setup-runtime.sh
RUN bash .oss-scanner/test.sh smoke
CMD ["bash"]
