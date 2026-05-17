/*
 * §6.8 cursor-sprite full theme — helper binary that registers
 * per-shape wl_surfaces with the compositor via
 * qdwin_shell_v1.set_cursor_sprite (v10).
 *
 * For each cursor-shape-v1 shape, we try libXcursor first
 * (XcursorLibraryLoadImages with the well-known CSS name). If a real
 * theme image is found, we upload its actual ARGB pixels to a
 * wl_shm-backed wl_surface and register with the image's xhot/yhot.
 * If theme lookup fails (no theme installed, name unknown), we fall
 * back to a synthetic 24×24 disc with a per-shape colour so the
 * cursor at least isn't invisible.
 *
 * The helper stays alive (paused on signalfd) so the registered
 * surfaces survive — destroying the wl_client tears down the surfaces
 * which fires the compositor's destroy listener and clears the cache.
 *
 * Args: none.
 * Env:
 *     WAYLAND_DISPLAY      — outer display socket
 *     XDG_RUNTIME_DIR      — standard
 *     XCURSOR_THEME        — Xcursor theme name (libXcursor honours
 *                            it; defaults to "default" if unset).
 *     XCURSOR_SIZE         — preferred nominal size in px (default 24).
 *     QDWIN_CURSOR_SPRITES_SHAPES — comma-separated list of shape ids
 *                                   (default 1..36 = all shapes).
 *     QDWIN_CURSOR_SPRITES_NO_XCURSOR — if set, skip libXcursor lookup
 *                                       and use the synthetic painter
 *                                       directly (for tests on VMs
 *                                       without a theme installed).
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/signalfd.h>
#include <unistd.h>

#include <wayland-client.h>
#include <X11/Xcursor/Xcursor.h>

#include "qdwin-shell-v1-client-protocol.h"

#define LOGI(fmt, ...) \
	fprintf(stderr, "[cursor-sprites %d] " fmt "\n", \
		(int)getpid(), ##__VA_ARGS__)
#define LOGE(fmt, ...) \
	fprintf(stderr, "[cursor-sprites %d ERR] " fmt "\n", \
		(int)getpid(), ##__VA_ARGS__)

/* Highest shape value in cursor-shape-v1 v1 today. Keep sync with
 * libweston's cursor-shape-server-protocol.h. */
#define MAX_SHAPE 36

/* CSS-style cursor names per the cursor-shape-v1 protocol enum, indexed
 * by shape value. libXcursor's freedesktop fallback table maps these to
 * traditional X11 names ("left_ptr", "xterm", etc.) automatically when
 * the theme provides them. Index 0 is the off-by-one slot (shapes start
 * at 1); leave NULL so try_load_xcursor() can short-circuit. */
static const char *const SHAPE_NAMES[MAX_SHAPE + 1] = {
	NULL,            /* 0 — unused */
	"default",       /* 1 */
	"context-menu",  /* 2 */
	"help",          /* 3 */
	"pointer",       /* 4 */
	"progress",      /* 5 */
	"wait",          /* 6 */
	"cell",          /* 7 */
	"crosshair",     /* 8 */
	"text",          /* 9 */
	"vertical-text", /* 10 */
	"alias",         /* 11 */
	"copy",          /* 12 */
	"move",          /* 13 */
	"no-drop",       /* 14 */
	"not-allowed",   /* 15 */
	"grab",          /* 16 */
	"grabbing",      /* 17 */
	"e-resize",      /* 18 */
	"n-resize",      /* 19 */
	"ne-resize",     /* 20 */
	"nw-resize",     /* 21 */
	"s-resize",      /* 22 */
	"se-resize",     /* 23 */
	"sw-resize",     /* 24 */
	"w-resize",      /* 25 */
	"ew-resize",     /* 26 */
	"ns-resize",     /* 27 */
	"nesw-resize",   /* 28 */
	"nwse-resize",   /* 29 */
	"col-resize",    /* 30 */
	"row-resize",    /* 31 */
	"all-scroll",    /* 32 */
	"zoom-in",       /* 33 */
	"zoom-out",      /* 34 */
	"dnd-ask",       /* 35 */
	"all-resize",    /* 36 */
};

struct cs_state {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_compositor *compositor;
	struct wl_shm *shm;
	struct qdwin_shell_v1 *shell;
	uint32_t shell_version;
};

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct cs_state *s = data;
	if (strcmp(interface, wl_compositor_interface.name) == 0) {
		s->compositor = wl_registry_bind(reg, name,
			&wl_compositor_interface, version >= 4 ? 4 : version);
	} else if (strcmp(interface, wl_shm_interface.name) == 0) {
		s->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	} else if (strcmp(interface, qdwin_shell_v1_interface.name) == 0) {
		uint32_t v = version > 10 ? 10 : version;
		s->shell_version = v;
		s->shell = wl_registry_bind(reg, name,
			&qdwin_shell_v1_interface, v);
	}
}

static void
on_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{ (void)data; (void)reg; (void)name; }

static const struct wl_registry_listener registry_listener = {
	on_global, on_global_remove,
};

static int
make_anon_fd(size_t size)
{
	int fd = memfd_create("qdistro-cursor-sprites-shm",
			      MFD_CLOEXEC | MFD_ALLOW_SEALING);
	if (fd < 0) return -1;
	if (ftruncate(fd, size) < 0) { close(fd); return -1; }
	return fd;
}

/* Synthetic ARGB sprite — a soft-edged disc whose hue depends on
 * shape, plus a small accent dot offset towards the hotspot direction
 * so different shapes are visually distinct. WL_SHM_FORMAT_ARGB8888
 * little-endian = bytes B,G,R,A in memory.
 *
 * Called once per shape when libXcursor data isn't (yet) wired.
 */
static void
fill_sprite_buffer(uint8_t *dst, int w, int h, uint32_t shape)
{
	static const uint32_t palette[] = {
		0xFFFAFAFA, /* 0  default */
		0xFFFAFAFA, /* 1  default */
		0xFF60E060, /* 2  pointer */
		0xFFE0C040, /* 3  context_menu */
		0xFFE0C040, /* 4  help */
		0xFF60A0FF, /* 5  progress */
		0xFFFFE020, /* 6  wait */
		0xFFCCCCCC, /* 7  cell */
		0xFFCCCCCC, /* 8  crosshair */
		0xFF202020, /* 9  text */
		0xFF202020, /* 10 vertical-text */
		0xFFE04040, /* 11 alias */
		0xFF60A0FF, /* 12 copy */
		0xFF60A0FF, /* 13 move */
		0xFFE03030, /* 14 no-drop */
		0xFFE03030, /* 15 not-allowed */
		0xFF40C0E0, /* 16 grab */
		0xFF40C0E0, /* 17 grabbing */
		0xFFFFC080, /* 18 e-resize */
		0xFFFFC080, /* 19 n-resize */
		0xFFFFC080, /* 20 ne-resize */
		0xFFFFC080, /* 21 nw-resize */
		0xFFFFC080, /* 22 s-resize */
		0xFFFFC080, /* 23 se-resize */
		0xFFFFC080, /* 24 sw-resize */
		0xFFFFC080, /* 25 w-resize */
		0xFFFFA0FF, /* 26 ew-resize */
		0xFFFFA0FF, /* 27 ns-resize */
		0xFFFFA0FF, /* 28 nesw-resize */
		0xFFFFA0FF, /* 29 nwse-resize */
		0xFFC080FF, /* 30 col-resize */
		0xFFC080FF, /* 31 row-resize */
		0xFFFFFFFF, /* 32 all-scroll */
		0xFFFFA0A0, /* 33 zoom-in */
		0xFFA0FFA0, /* 34 zoom-out */
		0xFFFFFFFF, /* 35 dnd-ask */
		0xFF8080FF, /* 36 all-resize */
	};
	uint32_t base = (shape < sizeof palette / sizeof palette[0])
		? palette[shape] : 0xFFFAFAFA;

	int cx = w / 2, cy = h / 2;
	int r2 = (w / 2 - 1) * (w / 2 - 1);
	for (int y = 0; y < h; y++) {
		for (int x = 0; x < w; x++) {
			int dx = x - cx, dy = y - cy;
			int d2 = dx*dx + dy*dy;
			uint32_t pix;
			if (d2 > r2) {
				pix = 0x00000000; /* transparent corners */
			} else if (d2 < (r2 / 4) && shape == 9) {
				pix = 0xFFFFFFFF; /* I-beam centre */
			} else {
				pix = base;
			}
			((uint32_t *)dst)[(size_t)y * w + x] = pix;
		}
	}
}

/* Pick the XcursorImage in `images` whose nominal size is closest to
 * `prefer`. Returns NULL only on empty input. */
static XcursorImage *
pick_best_xcursor_image(XcursorImages *images, int prefer)
{
	if (!images || images->nimage <= 0)
		return NULL;
	XcursorImage *best = images->images[0];
	int best_delta = abs((int)best->size - prefer);
	for (int i = 1; i < images->nimage; i++) {
		XcursorImage *img = images->images[i];
		int delta = abs((int)img->size - prefer);
		if (delta < best_delta) {
			best = img;
			best_delta = delta;
		}
	}
	return best;
}

/* Try libXcursor for `shape`. On success returns the picked frame +
 * the owning XcursorImages*; caller MUST XcursorImagesDestroy(*owner)
 * after copying out img->pixels. Returns NULL on miss or if the
 * env-var override disables theme lookup. */
static XcursorImage *
try_load_xcursor(uint32_t shape, int prefer_size, XcursorImages **owner)
{
	*owner = NULL;
	if (getenv("QDWIN_CURSOR_SPRITES_NO_XCURSOR"))
		return NULL;
	if (shape > MAX_SHAPE || !SHAPE_NAMES[shape])
		return NULL;
	const char *theme = getenv("XCURSOR_THEME");
	XcursorImages *imgs =
		XcursorLibraryLoadImages(SHAPE_NAMES[shape], theme, prefer_size);
	if (!imgs) return NULL;
	XcursorImage *pick = pick_best_xcursor_image(imgs, prefer_size);
	if (!pick) {
		XcursorImagesDestroy(imgs);
		return NULL;
	}
	*owner = imgs;
	return pick;
}

/* Build + register one shape. On success returns 0 and *out_via_xcursor
 * is 1 if the pixels came from the libXcursor theme, 0 if from the
 * synthetic painter. On failure returns -1 and *out_via_xcursor is
 * untouched. */
static int
build_and_register_sprite(struct cs_state *s, uint32_t shape,
			  int *out_via_xcursor)
{
	const char *sz_env = getenv("XCURSOR_SIZE");
	int prefer = sz_env ? atoi(sz_env) : 24;
	if (prefer <= 0) prefer = 24;

	int W, H, hx, hy;
	const uint32_t *src_pixels = NULL;       /* set if Xcursor hit */
	XcursorImages *xc_owner = NULL;
	XcursorImage *xc = try_load_xcursor(shape, prefer, &xc_owner);
	int via_xc = (xc != NULL);
	if (xc) {
		W  = (int)xc->width;
		H  = (int)xc->height;
		hx = (int)xc->xhot;
		hy = (int)xc->yhot;
		src_pixels = (const uint32_t *)xc->pixels;
	} else {
		/* Synthetic 24×24 disc. The hotspot table below assigns
		 * centre (12,12) to shapes whose natural hotspot is the
		 * activation point (resize cursors, drag/move/grab, text/
		 * crosshair, scroll, zoom). Tip-style shapes (default,
		 * pointer, alias, copy, no-drop, not-allowed, help, wait,
		 * progress, cell, context-menu) get (0,0) — the synthetic
		 * disc is centred at the wl_surface origin so the natural
		 * "tip" projects from the top-left when (0,0) is used.
		 * Without this table, only crosshair/text/vertical-text
		 * had a centred hotspot and resize cursors landed offset
		 * from where the user expected. */
		W = prefer; H = prefer;
		switch (shape) {
		case 8:  case 9:  case 10:  /* crosshair, text, vertical-text */
		case 13:                    /* move */
		case 16: case 17:           /* grab, grabbing */
		case 18: case 19: case 20: case 21: /* resize: e, n, ne, nw */
		case 22: case 23: case 24: case 25: /* resize: s, se, sw, w */
		case 26: case 27: case 28: case 29: /* resize: ew, ns, nesw, nwse */
		case 30: case 31:           /* col-resize, row-resize */
		case 32:                    /* all-scroll */
		case 33: case 34:           /* zoom-in, zoom-out */
		case 36:                    /* all-resize */
			hx = prefer / 2; hy = prefer / 2;
			break;
		default:
			/* default(1), context-menu(2), help(3), pointer(4),
			 * progress(5), wait(6), cell(7), alias(11), copy(12),
			 * no-drop(14), not-allowed(15), dnd-ask(35) — all
			 * tip-or-tip-like; align to surface origin. */
			hx = 0; hy = 0;
			break;
		}
	}
	int stride = W * 4;
	size_t size = (size_t)stride * H;

	int fd = make_anon_fd(size);
	if (fd < 0) {
		if (xc_owner) XcursorImagesDestroy(xc_owner);
		return -1;
	}
	uint8_t *map = mmap(NULL, size, PROT_READ | PROT_WRITE,
			    MAP_SHARED, fd, 0);
	if (map == MAP_FAILED) {
		close(fd);
		if (xc_owner) XcursorImagesDestroy(xc_owner);
		return -1;
	}
	if (src_pixels) {
		/* XcursorPixel is uint32_t in 0xAARRGGBB layout, premultiplied
		 * alpha. WL_SHM_FORMAT_ARGB8888 little-endian = bytes B,G,R,A
		 * which is the same memory layout, so a straight memcpy works. */
		memcpy(map, src_pixels, size);
	} else {
		fill_sprite_buffer(map, W, H, shape);
	}
	munmap(map, size);
	if (xc_owner) {
		XcursorImagesDestroy(xc_owner);
		xc_owner = NULL;
		xc = NULL;
		src_pixels = NULL;
	}

	struct wl_shm_pool *pool = wl_shm_create_pool(s->shm, fd, size);
	close(fd);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, W, H, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);

	struct wl_surface *surface =
		wl_compositor_create_surface(s->compositor);
	wl_surface_attach(surface, buf, 0, 0);
	wl_surface_damage_buffer(surface, 0, 0, W, H);
	wl_surface_commit(surface);
	/* DO NOT wl_buffer_destroy(buf) here. Weston wraps each wl_buffer
	 * in a weston_buffer whose lifetime is tied to the wl_buffer
	 * resource; destroying the wl_buffer drops the surface's
	 * buffer_ref, leaving the cursor surface with no pixels for the
	 * DRM cursor plane to scan. Keep both buf and surface alive for
	 * the helper's lifetime — it parks on signalfd until SIGTERM. */
	(void)buf;

	qdwin_shell_v1_set_cursor_sprite(s->shell, shape, surface, hx, hy);
	wl_display_flush(s->display);
	if (out_via_xcursor) *out_via_xcursor = via_xc;
	(void)src_pixels;
	return 0;
}

static int
build_and_register_sprite_logged(struct cs_state *s, uint32_t shape)
{
	int via_xc = 0;
	int rc = build_and_register_sprite(s, shape, &via_xc);
	if (rc == 0) {
		LOGI("registered shape=%u via=%s",
		     shape, via_xc ? "xcursor" : "synthetic");
		return via_xc;
	}
	LOGE("registration failed shape=%u rc=%d", shape, rc);
	return -1;
}

int
main(int argc, char *argv[])
{
	(void)argc; (void)argv;
	struct cs_state s = {0};

	unsetenv("WAYLAND_SOCKET");

	s.display = wl_display_connect(NULL);
	if (!s.display) {
		LOGE("wl_display_connect: %s", strerror(errno));
		return 3;
	}
	s.registry = wl_display_get_registry(s.display);
	wl_registry_add_listener(s.registry, &registry_listener, &s);
	wl_display_roundtrip(s.display);

	if (!s.compositor || !s.shm || !s.shell) {
		LOGE("missing globals: compositor=%p shm=%p shell=%p",
		     (void *)s.compositor, (void *)s.shm, (void *)s.shell);
		return 4;
	}
	if (s.shell_version < 10) {
		LOGE("qdwin_shell_v1 v%u < 10 — set_cursor_sprite unavailable",
		     s.shell_version);
		return 5;
	}
	wl_display_roundtrip(s.display);

	int xcursor_hits = 0, synthetic_hits = 0;
	const char *only = getenv("QDWIN_CURSOR_SPRITES_SHAPES");
	if (only && *only) {
		char *dup = strdup(only);
		for (char *tok = strtok(dup, ","); tok; tok = strtok(NULL, ",")) {
			uint32_t shape = (uint32_t)strtoul(tok, NULL, 10);
			if (shape >= 1 && shape <= MAX_SHAPE) {
				int via_xc = build_and_register_sprite_logged(
					&s, shape);
				if (via_xc == 1) xcursor_hits++;
				else if (via_xc == 0) synthetic_hits++;
			}
		}
		free(dup);
	} else {
		for (uint32_t shape = 1; shape <= MAX_SHAPE; shape++) {
			int via_xc = build_and_register_sprite_logged(
				&s, shape);
			if (via_xc == 1) xcursor_hits++;
			else if (via_xc == 0) synthetic_hits++;
		}
	}
	LOGI("theme summary: xcursor=%d synthetic=%d",
	     xcursor_hits, synthetic_hits);

	wl_display_flush(s.display);
	wl_display_roundtrip(s.display);
	LOGI("registration complete; pausing on signalfd "
	     "(SIGTERM exits cleanly)");

	sigset_t mask;
	sigemptyset(&mask);
	sigaddset(&mask, SIGTERM);
	sigaddset(&mask, SIGINT);
	sigprocmask(SIG_BLOCK, &mask, NULL);
	int sfd = signalfd(-1, &mask, SFD_CLOEXEC);
	if (sfd < 0) { LOGE("signalfd: %m"); return 6; }
	struct signalfd_siginfo si;
	(void)read(sfd, &si, sizeof si);
	close(sfd);

	wl_display_disconnect(s.display);
	return 0;
}
