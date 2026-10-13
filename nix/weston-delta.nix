# Files weston-qdistro copies wholesale from qdwin/libweston-vendored/src onto
# upstream weston 16.0.0 (audit: nix/delta-audit.md). The
# `weston-delta-complete` check (nix/checks.nix) fails if a vendored file
# differs from upstream without being listed here, or a listed file no
# longer differs.
[
  "include/libweston/backend-rdp.h"
  "include/libweston/desktop.h"
  "libweston/backend-drm/drm-internal.h"
  "libweston/backend-drm/drm-kms-enums.h"
  "libweston/backend-drm/kms.c"
  "libweston/backend-headless/headless.c"
  "libweston/backend-rdp/rdp.c"
  "libweston/backend-rdp/rdp.h"
  "libweston/desktop/qdwin-xdg-constrain.h"
  "libweston/desktop/xdg-shell.c"
  "libweston/input.c"
  "libweston/output-capture.c"
  "libweston/output-capture.h"
  "libweston/pixman-renderer.c"
  "libweston/renderer-gl/gl-renderer.c"
]
