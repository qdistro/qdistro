#!/bin/bash
# Just the build step, capture full output.
set +e
cd /root/qdwin-src
ninja -C build qdistro-forward 2>&1 | tail -30
