/*
 * qdistro-test-window — minimal wayland client that creates one
 * xdg_toplevel with a configurable title and a solid-colour 1×1 SHM
 * buffer, then sleeps on the dispatch loop until SIGTERM.
 *
 * Used by phase7 bats tests to verify silo-aware chrome (s38) when
 * neither weston-terminal nor weston-demos are convenient: weston-
 * terminal in particular doesn't call xdg_toplevel.set_title at
 * startup, so waypipe --title-prefix has nothing to prepend to.
 *
 * Usage:
 *   qdistro-test-window --title "Some Title" [--width N] [--height N]
 *
 * Honours $WAYLAND_DISPLAY. Exits non-zero only on connection failure.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <fcntl.h>
#include <getopt.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#include <wayland-client.h>
#include "xdg-shell-client-protocol.h"

static int running = 1;

static void on_sig(int s) { (void)s; running = 0; }

struct ctx {
	struct wl_display *display;
	struct wl_compositor *compositor;
	struct wl_shm *shm;
	struct xdg_wm_base *xdg_wm_base;
};

struct window {
	struct ctx *ctx;
	struct wl_surface *surface;
	struct wl_buffer *buffer;
	int width, height;
	int pending_width, pending_height;
	uint32_t fill;
};

static void
xdg_wm_base_ping(void *data, struct xdg_wm_base *base, uint32_t serial)
{
	(void)data;
	xdg_wm_base_pong(base, serial);
}
static const struct xdg_wm_base_listener xdg_wm_base_listener_impl = {
	.ping = xdg_wm_base_ping,
};

static void
registry_global(void *data, struct wl_registry *reg, uint32_t name,
		const char *interface, uint32_t version)
{
	struct ctx *c = data;
	if (!strcmp(interface, wl_compositor_interface.name)) {
		c->compositor = wl_registry_bind(reg, name,
						 &wl_compositor_interface,
						 version > 4 ? 4 : version);
	} else if (!strcmp(interface, wl_shm_interface.name)) {
		c->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	} else if (!strcmp(interface, xdg_wm_base_interface.name)) {
		c->xdg_wm_base = wl_registry_bind(reg, name,
						  &xdg_wm_base_interface, 1);
		xdg_wm_base_add_listener(c->xdg_wm_base,
					 &xdg_wm_base_listener_impl, NULL);
	}
}
static void registry_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_global_remove,
};

static struct wl_buffer *
make_solid_buffer(struct wl_shm *shm, int w, int h, uint32_t argb)
{
	int stride = w * 4;
	int size = stride * h;
	int fd = memfd_create("qdistro-test-window", MFD_CLOEXEC);
	if (fd < 0) return NULL;
	if (ftruncate(fd, size) < 0) { close(fd); return NULL; }
	uint32_t *p = mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
	if (p == MAP_FAILED) { close(fd); return NULL; }
	for (int i = 0; i < w * h; i++) p[i] = argb;
	struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, w, h, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	munmap(p, size);
	close(fd);
	return buf;
}

static int
window_redraw(struct window *win, int width, int height)
{
	struct wl_buffer *old = win->buffer;
	struct wl_buffer *next = make_solid_buffer(win->ctx->shm,
						 width, height, win->fill);
	if (!next)
		return -1;

	win->buffer = next;
	win->width = width;
	win->height = height;
	wl_surface_attach(win->surface, next, 0, 0);
	wl_surface_damage_buffer(win->surface, 0, 0, width, height);
	wl_surface_commit(win->surface);
	if (old)
		wl_buffer_destroy(old);
	return 0;
}

static void
xdg_surface_configure(void *data, struct xdg_surface *xs, uint32_t serial)
{
	struct window *win = data;
	xdg_surface_ack_configure(xs, serial);
	int width = win->pending_width > 0 ? win->pending_width : win->width;
	int height = win->pending_height > 0 ? win->pending_height : win->height;
	if (window_redraw(win, width, height) < 0) {
		fprintf(stderr, "make_solid_buffer failed for %dx%d\n",
			width, height);
		running = 0;
	}
	win->pending_width = 0;
	win->pending_height = 0;
}
static const struct xdg_surface_listener xdg_surface_impl = {
	.configure = xdg_surface_configure,
};

static void
xdg_toplevel_configure(void *d, struct xdg_toplevel *t,
		       int32_t w, int32_t h, struct wl_array *states)
{
	struct window *win = d;
	(void)t;
	(void)states;
	/* A zero axis means the client chooses its current/preferred extent. */
	if (w > 0)
		win->pending_width = w;
	if (h > 0)
		win->pending_height = h;
}
static void
xdg_toplevel_close(void *d, struct xdg_toplevel *t)
{ (void)d; (void)t; running = 0; }
static void
xdg_toplevel_configure_bounds(void *d, struct xdg_toplevel *t,
			      int32_t mw, int32_t mh)
{ (void)d; (void)t; (void)mw; (void)mh; }
static void
xdg_toplevel_wm_capabilities(void *d, struct xdg_toplevel *t,
			     struct wl_array *caps)
{ (void)d; (void)t; (void)caps; }
static const struct xdg_toplevel_listener xdg_toplevel_impl = {
	.configure = xdg_toplevel_configure,
	.close = xdg_toplevel_close,
	.configure_bounds = xdg_toplevel_configure_bounds,
	.wm_capabilities = xdg_toplevel_wm_capabilities,
};

int main(int argc, char **argv)
{
	const char *title = "qdistro-test-window";
	int width = 200, height = 150;
	uint32_t fill = 0xff404060;  /* dark blue-grey */
	struct option opts[] = {
		{"title",  required_argument, 0, 't'},
		{"width",  required_argument, 0, 'w'},
		{"height", required_argument, 0, 'h'},
		{"color",  required_argument, 0, 'c'},
		{0,0,0,0},
	};
	int o;
	while ((o = getopt_long(argc, argv, "t:w:h:c:", opts, NULL)) != -1) {
		switch (o) {
		case 't': title = optarg; break;
		case 'w': width  = atoi(optarg); break;
		case 'h': height = atoi(optarg); break;
		case 'c': fill   = strtoul(optarg, NULL, 0); break;
		default: fprintf(stderr, "usage: %s --title T [--width W "
					 "--height H --color 0xAARRGGBB]\n",
				 argv[0]);
			return 2;
		}
	}

	signal(SIGTERM, on_sig);
	signal(SIGINT, on_sig);

	struct ctx c = {0};
	c.display = wl_display_connect(NULL);
	if (!c.display) {
		fprintf(stderr, "wl_display_connect failed\n");
		return 1;
	}
	struct wl_registry *reg = wl_display_get_registry(c.display);
	wl_registry_add_listener(reg, &registry_listener, &c);
	wl_display_roundtrip(c.display);
	if (!c.compositor || !c.shm || !c.xdg_wm_base) {
		fprintf(stderr, "missing global(s) — compositor=%p shm=%p "
				 "xdg=%p\n", (void*)c.compositor,
			 (void*)c.shm, (void*)c.xdg_wm_base);
		return 1;
	}

	struct wl_surface *surf = wl_compositor_create_surface(c.compositor);
	struct window win = {
		.ctx = &c,
		.surface = surf,
		.width = width,
		.height = height,
		.fill = fill,
	};
	struct xdg_surface *xsurf = xdg_wm_base_get_xdg_surface(c.xdg_wm_base, surf);
	xdg_surface_add_listener(xsurf, &xdg_surface_impl, &win);
	struct xdg_toplevel *top = xdg_surface_get_toplevel(xsurf);
	xdg_toplevel_add_listener(top, &xdg_toplevel_impl, &win);
	xdg_toplevel_set_title(top, title);
	xdg_toplevel_set_app_id(top, "qdistro-test-window");
	wl_surface_commit(surf);
	wl_display_roundtrip(c.display);

	while (running && wl_display_dispatch(c.display) != -1) {
		/* idle on events; SIGTERM flips running */
	}

	xdg_toplevel_destroy(top);
	xdg_surface_destroy(xsurf);
	wl_surface_destroy(surf);
	if (win.buffer)
		wl_buffer_destroy(win.buffer);
	wl_display_disconnect(c.display);
	return 0;
}
