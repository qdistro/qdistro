# Build context: the qdistro repository root (Anthropic's /src contract).
# This dated dependency snapshot is deliberately explicit; refresh it before
# openSUSE retires its history URLs. No local qci cache is required.
ARG SNAPSHOT=20261007
FROM registry.opensuse.org/opensuse/tumbleweed:${SNAPSHOT}
ARG SNAPSHOT
ENV QDISTRO_SUBSTRATE_SNAPSHOT=${SNAPSHOT} \
    OSS_SCANNER_JOBS=2 \
    QDSHELL_QS_BUILD_JOBS=2 \
    QT_QPA_PLATFORM=offscreen \
    PYTEST_QT_API=pyqt6 QT_API=pyqt6 QDISTRO_REQUIRE_PYQT6=1 \
    PATH=/opt/scanner-tools:/opt/scanner-python/bin:/usr/local/bin:/usr/bin:/bin \
    VIRTUAL_ENV=/opt/scanner-python
COPY scripts/vm/container-native-deps.sh /recipe/container-native-deps.sh
RUN bash /recipe/container-native-deps.sh
COPY ci/containers/host-packages.txt /recipe/host-packages.txt
COPY .oss-scanner/install-deps.sh /recipe/install-deps.sh
RUN bash /recipe/install-deps.sh
COPY . /src
WORKDIR /src
# Never ship a linked-worktree pointer to host Git metadata. Anthropic's
# normal clone has a real .git directory, which is retained.
RUN if test -f .git; then rm .git; fi
RUN bash .oss-scanner/build.sh
RUN bash ci/containers/prepare-host.sh
RUN useradd -m -u 1000 -s /bin/bash scanner \
 && chown -R scanner:scanner /src \
 && git config --system --add safe.directory /src \
 && rpm -qa | sort > /opt/scanner-rpms.txt
# Keep build directories, development headers, generated protocols and npm
# caches: the offline agent must be able to rebuild and write reproducers.
RUN bash .oss-scanner/test.sh smoke
CMD ["bash"]
