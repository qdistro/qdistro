/*
 * qdwin-popup-probe — direct xdg_shell client that creates one mapped
 * xdg_toplevel and then an xdg_popup whose positioner deliberately places
 * the popup far outside the parent (large offset), and prints the
 * geometry the compositor reports back in xdg_popup.configure.
 *
 * This exercises the M1 security fix end-to-end through the real protocol
 * path (a direct tier-0/1 client's xdg_popup — the spoofing surface the
 * libweston popup clamp addresses). The configure geometry is in the
 * parent window-geometry frame, the same frame as the positioner:
 *   - WITHOUT the clamp, the reported x/y echoes the requested offset
 *     (~100000) — the popup escapes onto/over other windows.
 *   - WITH the clamp, the reported x/y is pulled back so the popup stays
 *     within the parent's output (small magnitude).
 *
 * Output (stdout): one line  POPUP_GEOM <x> <y> <w> <h>
 * Exit status: 0 on success (configure received), 1 on any failure.
 *
 * Usage:
 *   qdwin-popup-probe [--offset-x N] [--offset-y N]
 *                     [--popup-w N] [--popup-h N]
 *                     [--parent-w N] [--parent-h N]
 * Honours $WAYLAND_DISPLAY.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include <wayland-client.h>
#include "xdg-shell-client-protocol.h"

struct ctx {
	struct wl_display *display;
	struct wl_compositor *compositor;
	struct wl_shm *shm;
	struct xdg_wm_base *wm_base;

	int got_toplevel_configure;
	int got_popup_configure;
	int popup_done;
	int32_t px, py, pw, ph;	/* popup configure geometry */
};

static void
wm_base_ping(void *d, struct xdg_wm_base *b, uint32_t serial)
{ (void)d; xdg_wm_base_pong(b, serial); }
static const struct xdg_wm_base_listener wm_base_impl = { .ping = wm_base_ping };

static void
registry_global(void *data, struct wl_registry *reg, uint32_t name,
		const char *iface, uint32_t version)
{
	struct ctx *c = data;
	if (!strcmp(iface, wl_compositor_interface.name))
		c->compositor = wl_registry_bind(reg, name,
						 &wl_compositor_interface,
						 version > 4 ? 4 : version);
	else if (!strcmp(iface, wl_shm_interface.name))
		c->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	else if (!strcmp(iface, xdg_wm_base_interface.name)) {
		c->wm_base = wl_registry_bind(reg, name, &xdg_wm_base_interface,
					      version > 3 ? 3 : version);
		xdg_wm_base_add_listener(c->wm_base, &wm_base_impl, NULL);
	}
}
static void registry_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_global_remove,
};

static struct wl_buffer *
make_buffer(struct wl_shm *shm, int w, int h, uint32_t colour)
{
	int stride = w * 4, size = stride * h;
	int fd = memfd_create("qdwin-popup-probe", MFD_CLOEXEC);
	if (fd < 0) return NULL;
	if (ftruncate(fd, size) < 0) { close(fd); return NULL; }
	uint32_t *p = mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
	if (p == MAP_FAILED) { close(fd); return NULL; }
	for (int i = 0; i < w * h; i++) p[i] = colour;
	struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, w, h, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	munmap(p, size);
	close(fd);
	return buf;
}

/* parent xdg_surface / toplevel */
static void
parent_surface_configure(void *data, struct xdg_surface *xs, uint32_t serial)
{
	struct ctx *c = data;
	xdg_surface_ack_configure(xs, serial);
	c->got_toplevel_configure = 1;
}
static const struct xdg_surface_listener parent_surface_impl = {
	.configure = parent_surface_configure,
};
static void tl_configure(void *d, struct xdg_toplevel *t, int32_t w, int32_t h,
			 struct wl_array *s)
{ (void)d; (void)t; (void)w; (void)h; (void)s; }
static void tl_close(void *d, struct xdg_toplevel *t) { (void)d; (void)t; }
static void tl_bounds(void *d, struct xdg_toplevel *t, int32_t w, int32_t h)
{ (void)d; (void)t; (void)w; (void)h; }
static void tl_caps(void *d, struct xdg_toplevel *t, struct wl_array *a)
{ (void)d; (void)t; (void)a; }
static const struct xdg_toplevel_listener tl_impl = {
	.configure = tl_configure, .close = tl_close,
	.configure_bounds = tl_bounds, .wm_capabilities = tl_caps,
};

/* popup xdg_surface / popup */
static void
popup_surface_configure(void *data, struct xdg_surface *xs, uint32_t serial)
{ (void)data; xdg_surface_ack_configure(xs, serial); }
static const struct xdg_surface_listener popup_surface_impl = {
	.configure = popup_surface_configure,
};
static void
popup_configure(void *data, struct xdg_popup *p, int32_t x, int32_t y,
		int32_t w, int32_t h)
{
	struct ctx *c = data;
	(void)p;
	c->px = x; c->py = y; c->pw = w; c->ph = h;
	c->got_popup_configure = 1;
}
static void popup_done(void *data, struct xdg_popup *p)
{ struct ctx *c = data; (void)p; c->popup_done = 1; }
static void popup_repositioned(void *d, struct xdg_popup *p, uint32_t t)
{ (void)d; (void)p; (void)t; }
static const struct xdg_popup_listener popup_impl = {
	.configure = popup_configure,
	.popup_done = popup_done,
	.repositioned = popup_repositioned,
};

int main(int argc, char **argv)
{
	int offx = 100000, offy = 100000;
	int popup_w = 200, popup_h = 300;
	int parent_w = 400, parent_h = 300;
	int hold_seconds = 0;
	struct option opts[] = {
		{"offset-x", required_argument, 0, 'x'},
		{"offset-y", required_argument, 0, 'y'},
		{"popup-w",  required_argument, 0, 'W'},
		{"popup-h",  required_argument, 0, 'H'},
		{"parent-w", required_argument, 0, 'p'},
		{"parent-h", required_argument, 0, 'q'},
		{"hold-seconds", required_argument, 0, 't'},
		{0,0,0,0},
	};
	int o;
	while ((o = getopt_long(argc, argv, "x:y:W:H:p:q:t:", opts, NULL)) != -1) {
		switch (o) {
		case 'x': offx = atoi(optarg); break;
		case 'y': offy = atoi(optarg); break;
		case 'W': popup_w = atoi(optarg); break;
		case 'H': popup_h = atoi(optarg); break;
		case 'p': parent_w = atoi(optarg); break;
		case 'q': parent_h = atoi(optarg); break;
		case 't': hold_seconds = atoi(optarg); break;
		default: return 2;
		}
	}

	/* Sane dimension bounds: these size SHM buffers (stride = w*4), so
	 * reject non-positive or huge values to avoid integer overflow in
	 * make_buffer(). Offsets are intentionally unbounded (the point of
	 * the probe is to request an off-output placement). */
	if (popup_w < 1 || popup_h < 1 || parent_w < 1 || parent_h < 1 ||
	    popup_w > 16384 || popup_h > 16384 ||
	    parent_w > 16384 || parent_h > 16384 ||
	    hold_seconds < 0 || hold_seconds > 3600) {
		fprintf(stderr, "dimensions must be in 1..16384\n");
		return 2;
	}

	struct ctx c = {0};
	c.display = wl_display_connect(NULL);
	if (!c.display) { fprintf(stderr, "connect failed\n"); return 1; }
	struct wl_registry *reg = wl_display_get_registry(c.display);
	wl_registry_add_listener(reg, &registry_listener, &c);
	wl_display_roundtrip(c.display);
	if (!c.compositor || !c.shm || !c.wm_base) {
		fprintf(stderr, "missing globals\n"); return 1;
	}

	/* Map a parent toplevel with a known window geometry. */
	struct wl_surface *surf = wl_compositor_create_surface(c.compositor);
	struct xdg_surface *xsurf =
		xdg_wm_base_get_xdg_surface(c.wm_base, surf);
	xdg_surface_add_listener(xsurf, &parent_surface_impl, &c);
	struct xdg_toplevel *top = xdg_surface_get_toplevel(xsurf);
	xdg_toplevel_add_listener(top, &tl_impl, &c);
	xdg_toplevel_set_title(top, "qdwin-popup-probe");
	xdg_toplevel_set_app_id(top, "qdwin-popup-probe");
	xdg_surface_set_window_geometry(xsurf, 0, 0, parent_w, parent_h);
	wl_surface_commit(surf);
	while (!c.got_toplevel_configure && wl_display_dispatch(c.display) != -1)
		;
	struct wl_buffer *buf = make_buffer(c.shm, parent_w, parent_h, 0xff404060);
	if (!buf) { fprintf(stderr, "buffer failed\n"); return 1; }
	wl_surface_attach(surf, buf, 0, 0);
	wl_surface_damage_buffer(surf, 0, 0, parent_w, parent_h);
	wl_surface_commit(surf);
	wl_display_roundtrip(c.display);	/* parent maps -> gains output */

	/* Build an overflowing positioner: anchor at the parent's top-left,
	 * gravity bottom-right, plus a large offset. Unclamped this lands the
	 * popup ~offx,offy to the lower-right, far off the output. */
	struct xdg_positioner *pos = xdg_wm_base_create_positioner(c.wm_base);
	xdg_positioner_set_size(pos, popup_w, popup_h);
	xdg_positioner_set_anchor_rect(pos, 0, 0, 1, 1);
	xdg_positioner_set_anchor(pos, XDG_POSITIONER_ANCHOR_TOP_LEFT);
	xdg_positioner_set_gravity(pos, XDG_POSITIONER_GRAVITY_BOTTOM_RIGHT);
	xdg_positioner_set_offset(pos, offx, offy);

	struct wl_surface *psurf = wl_compositor_create_surface(c.compositor);
	struct xdg_surface *pxsurf =
		xdg_wm_base_get_xdg_surface(c.wm_base, psurf);
	xdg_surface_add_listener(pxsurf, &popup_surface_impl, &c);
	struct xdg_popup *popup = xdg_surface_get_popup(pxsurf, xsurf, pos);
	xdg_popup_add_listener(popup, &popup_impl, &c);
	xdg_positioner_destroy(pos);
	wl_surface_commit(psurf);

	/* Pump until the popup is configured (or dismissed). */
	int spins = 0;
	while (!c.got_popup_configure && !c.popup_done && spins++ < 200) {
		if (wl_display_dispatch(c.display) == -1)
			break;
	}

	if (!c.got_popup_configure) {
		fprintf(stderr, "no popup configure (done=%d)\n", c.popup_done);
		return 1;
	}
	printf("POPUP_GEOM %d %d %d %d\n", c.px, c.py, c.pw, c.ph);
	fflush(stdout);

	/* Optional live-fixture mode: map a vividly distinct popup and keep the
	 * real parent+child pair alive long enough for the multi-machine R4 gate to
	 * subscribe/capture/close it. Default zero preserves the original one-shot
	 * geometry probe contract. */
	struct wl_buffer *pbuf = NULL;
	if (hold_seconds > 0) {
		pbuf = make_buffer(c.shm, popup_w, popup_h, 0xffff0060);
		if (!pbuf) { fprintf(stderr, "popup buffer failed\n"); return 1; }
		wl_surface_attach(psurf, pbuf, 0, 0);
		wl_surface_damage_buffer(psurf, 0, 0, popup_w, popup_h);
		wl_surface_commit(psurf);
		wl_display_roundtrip(c.display);
		printf("POPUP_MAPPED parent=qdwin-popup-probe\n");
		fflush(stdout);
		int remaining_ms = hold_seconds * 1000;
		while (!c.popup_done && remaining_ms > 0) {
			struct pollfd pfd = {
				.fd = wl_display_get_fd(c.display), .events = POLLIN,
			};
			int slice = remaining_ms < 100 ? remaining_ms : 100;
			int rc = poll(&pfd, 1, slice);
			if (rc < 0) break;
			if (rc > 0 && wl_display_dispatch(c.display) == -1) break;
			remaining_ms -= slice;
		}
	}

	xdg_popup_destroy(popup);
	xdg_surface_destroy(pxsurf);
	wl_surface_destroy(psurf);
	xdg_toplevel_destroy(top);
	xdg_surface_destroy(xsurf);
	wl_surface_destroy(surf);
	if (pbuf)
		wl_buffer_destroy(pbuf);
	wl_buffer_destroy(buf);
	wl_display_disconnect(c.display);
	return 0;
}
