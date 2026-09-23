/*
 * qdwin-probe — test client for qdwin_shell_v1.
 *
 * Modes (mutually exclusive):
 *
 *   (default)           Bind the global, receive hello, exit. Used by
 *                       Day-2 peer-uid filter tests. Exit 0 on hello,
 *                       1 on filter rejection, 2 on other.
 *
 *   --as-shell          Bind, bind_as_shell, wait for toplevel_added,
 *                       set_border_color, exit. Used by S3.
 *
 *   --attach-inset      Bind, bind_as_shell, wait for toplevel_added,
 *                       allocate 4 solid-colour SHM wl_buffers (one per
 *                       side with configurable thickness), call
 *                       attach_decoration, wait for toplevel_geometry
 *                       carrying the inner size, exit. Used by S4.
 *                       Defaults: N=24, E=4, S=4, W=4 pixels.
 *
 * Common options:
 *   --timeout=N         Seconds to wait for events (default 5).
 *   --color=0xRRGGBBAA  Border colour for --as-shell / fill for buffers.
 *   --inset=N,E,S,W     Side thicknesses for --attach-inset.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/types.h>
#include <sys/mman.h>
#include <errno.h>
#include <poll.h>

#include <wayland-client.h>
#include "qdwin-shell-v1-client-protocol.h"

enum probe_mode {
	MODE_HELLO_ONLY,
	MODE_AS_SHELL,
	MODE_ATTACH_INSET,
};

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_compositor *compositor;
	struct wl_shm *shm;
	struct qdwin_shell_v1 *shell;

	int received_hello;
	uint32_t hello_uid;
	int received_toplevel;
	uint32_t toplevel_handle;
	int received_geometry;
	uint32_t geometry_w, geometry_h;
};

/* ------------------------------------------------------------------
 * SHM buffer creation.
 * ------------------------------------------------------------------ */

static struct wl_buffer *
create_solid_shm_buffer(struct wl_shm *shm,
			int width, int height, uint32_t rgba)
{
	int stride = width * 4;
	int size = stride * height;
	if (size <= 0)
		return NULL;

	int fd = memfd_create("qdwin-probe", MFD_CLOEXEC);
	if (fd < 0) return NULL;
	if (ftruncate(fd, size) < 0) { close(fd); return NULL; }

	uint8_t *data = mmap(NULL, size, PROT_READ | PROT_WRITE,
			     MAP_SHARED, fd, 0);
	if (data == MAP_FAILED) { close(fd); return NULL; }

	/* ARGB8888, little-endian -> memory [B,G,R,A]. */
	uint8_t R = (rgba >> 24) & 0xff;
	uint8_t G = (rgba >> 16) & 0xff;
	uint8_t B = (rgba >>  8) & 0xff;
	uint8_t A = (rgba >>  0) & 0xff;
	for (int i = 0; i < width * height; i++) {
		data[i*4 + 0] = B;
		data[i*4 + 1] = G;
		data[i*4 + 2] = R;
		data[i*4 + 3] = A;
	}
	munmap(data, size);

	struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
	close(fd);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, width, height, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	return buf;
}

/* ------------------------------------------------------------------
 * qdwin_shell_v1 listeners.
 * ------------------------------------------------------------------ */

static void
on_hello(void *data, struct qdwin_shell_v1 *shell, uint32_t uid)
{
	struct probe *p = data;
	(void)shell;
	p->received_hello = 1;
	p->hello_uid = uid;
}

static void
on_toplevel_added(void *data, struct qdwin_shell_v1 *shell,
		  uint32_t handle, uint32_t owner_uid,
		  const char *app_id, const char *title,
		  uint32_t is_xwayland)
{
	struct probe *p = data;
	(void)shell; (void)owner_uid; (void)is_xwayland;
	if (!p->received_toplevel) {
		p->received_toplevel = 1;
		p->toplevel_handle = handle;
		fprintf(stderr,
			"qdwin-probe: toplevel_added handle=%u app_id=\"%s\" title=\"%s\"\n",
			handle, app_id, title);
	}
}

static void
on_geometry(void *data, struct qdwin_shell_v1 *s, uint32_t h,
	    int32_t x, int32_t y, uint32_t w, uint32_t H)
{
	struct probe *p = data;
	(void)s; (void)x; (void)y;
	if (h == p->toplevel_handle) {
		p->received_geometry = 1;
		p->geometry_w = w;
		p->geometry_h = H;
		fprintf(stderr, "qdwin-probe: geometry handle=%u %ux%u\n",
			h, w, H);
	}
}

static void noop_state(void *d, struct qdwin_shell_v1 *s, uint32_t h, uint32_t st)
{ (void)d; (void)s; (void)h; (void)st; }
static void noop_title(void *d, struct qdwin_shell_v1 *s, uint32_t h, const char *t)
{ (void)d; (void)s; (void)h; (void)t; }
static void noop_removed(void *d, struct qdwin_shell_v1 *s, uint32_t h)
{ (void)d; (void)s; (void)h; }
static void noop_locked(void *d, struct qdwin_shell_v1 *s, uint32_t l)
{ (void)d; (void)s; (void)l; }
static const struct qdwin_shell_v1_listener shell_listener = {
	.hello = on_hello,
	.toplevel_added = on_toplevel_added,
	.toplevel_geometry = on_geometry,
	.toplevel_state = noop_state,
	.toplevel_title = noop_title,
	.toplevel_removed = noop_removed,
	.locked_changed = noop_locked,
};

/* ------------------------------------------------------------------
 * wl_registry.
 * ------------------------------------------------------------------ */

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct probe *p = data;
	if (strcmp(interface, qdwin_shell_v1_interface.name) == 0) {
		p->shell = wl_registry_bind(reg, name,
					    &qdwin_shell_v1_interface,
					    version < 1 ? version : 1);
		qdwin_shell_v1_add_listener(p->shell, &shell_listener, p);
	} else if (strcmp(interface, wl_shm_interface.name) == 0) {
		p->shm = wl_registry_bind(reg, name, &wl_shm_interface,
					  version < 1 ? version : 1);
	} else if (strcmp(interface, wl_compositor_interface.name) == 0) {
		p->compositor = wl_registry_bind(reg, name,
						 &wl_compositor_interface,
						 version < 4 ? version : 4);
	}
}

static void
on_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{ (void)data; (void)reg; (void)name; }

static const struct wl_registry_listener registry_listener = {
	.global = on_global,
	.global_remove = on_global_remove,
};

/* ------------------------------------------------------------------
 * Dispatch helper: dispatch pending, then poll up to ms for more.
 * ------------------------------------------------------------------ */

static int
dispatch_until(struct probe *p, int (*done)(struct probe *), int timeout_s)
{
	int fd = wl_display_get_fd(p->display);
	int ticks = timeout_s * 10;

	while (!done(p) && ticks > 0) {
		wl_display_dispatch_pending(p->display);
		if (done(p)) break;
		wl_display_flush(p->display);

		struct pollfd pfd = { .fd = fd, .events = POLLIN };
		int pr = poll(&pfd, 1, 100);
		if (pr > 0) {
			if (wl_display_prepare_read(p->display) == 0) {
				if (wl_display_read_events(p->display) < 0)
					return -1;
			}
			wl_display_dispatch_pending(p->display);
		}
		ticks--;
	}
	return done(p) ? 0 : -2;
}

static int done_toplevel(struct probe *p) { return p->received_toplevel; }
static int done_geometry(struct probe *p) { return p->received_geometry; }

/* ------------------------------------------------------------------
 * main
 * ------------------------------------------------------------------ */

int main(int argc, char *argv[])
{
	enum probe_mode mode = MODE_HELLO_ONLY;
	int timeout = 5;
	int persist = 0;
	uint32_t color = 0x3388ccff;
	int in_n = 24, in_e = 4, in_s = 4, in_w = 4;
	struct probe p = {0};

	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--as-shell") == 0) {
			mode = MODE_AS_SHELL;
		} else if (strcmp(argv[i], "--attach-inset") == 0) {
			mode = MODE_ATTACH_INSET;
		} else if (strncmp(argv[i], "--timeout=", 10) == 0) {
			timeout = atoi(argv[i] + 10);
		} else if (strncmp(argv[i], "--persist=", 10) == 0) {
			persist = atoi(argv[i] + 10);
		} else if (strncmp(argv[i], "--color=", 8) == 0) {
			color = (uint32_t)strtoul(argv[i] + 8, NULL, 0);
		} else if (strncmp(argv[i], "--inset=", 8) == 0) {
			sscanf(argv[i] + 8, "%d,%d,%d,%d",
			       &in_n, &in_e, &in_s, &in_w);
		}
	}

	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-probe: wl_display_connect failed: %s\n",
			strerror(errno));
		return 2;
	}

	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);
	if (wl_display_roundtrip(p.display) < 0) {
		fprintf(stderr, "qdwin-probe: registry roundtrip failed\n");
		return 2;
	}
	if (!p.shell) {
		fprintf(stderr, "qdwin-probe: qdwin_shell_v1 not advertised\n");
		return 2;
	}

	int rc = wl_display_roundtrip(p.display);
	int err = wl_display_get_error(p.display);

	if (!p.received_hello) {
		if (rc < 0 || err != 0) {
			uint32_t obj_id = 0, code = 0;
			const struct wl_interface *iface = NULL;
			code = wl_display_get_protocol_error(p.display,
							     &iface, &obj_id);
			fprintf(stderr,
				"qdwin-probe: denied (errno=%d, proto code=%u on %s#%u)\n",
				err, code, iface ? iface->name : "(unknown)", obj_id);
			return 1;
		}
		fprintf(stderr, "qdwin-probe: no hello, no error — server bug?\n");
		return 2;
	}
	printf("qdwin-probe: hello uid=%u\n", (unsigned)p.hello_uid);
	if (mode == MODE_HELLO_ONLY)
		return 0;

	if ((mode == MODE_ATTACH_INSET) && !p.shm) {
		fprintf(stderr, "qdwin-probe: wl_shm not advertised\n");
		return 2;
	}

	qdwin_shell_v1_bind_as_shell(p.shell);
	wl_display_flush(p.display);

	if (dispatch_until(&p, done_toplevel, timeout) < 0) {
		fprintf(stderr, "qdwin-probe: timed out waiting for toplevel_added\n");
		return 3;
	}

	if (mode == MODE_AS_SHELL) {
		printf("qdwin-probe: set_border_color handle=%u color=%#010x\n",
		       p.toplevel_handle, color);
		qdwin_shell_v1_set_border_color(p.shell, p.toplevel_handle,
						color);
		wl_display_flush(p.display);
		wl_display_roundtrip(p.display);
		return 0;
	}

	/* MODE_ATTACH_INSET: wrap side buffers in committed wl_surfaces. */
	if (!p.compositor) {
		fprintf(stderr, "qdwin-probe: wl_compositor not advertised\n");
		return 2;
	}

	struct chrome_side {
		int w, h;
		struct wl_buffer *buf;
		struct wl_surface *surf;
	} sides[4] = {
		{ 64, in_n, NULL, NULL },   /* N */
		{ in_e, 64, NULL, NULL },   /* E */
		{ 64, in_s, NULL, NULL },   /* S */
		{ in_w, 64, NULL, NULL },   /* W */
	};

	for (int i = 0; i < 4; i++) {
		if (sides[i].w <= 0 || sides[i].h <= 0) continue;
		sides[i].buf = create_solid_shm_buffer(
			p.shm, sides[i].w, sides[i].h, color);
		sides[i].surf = wl_compositor_create_surface(p.compositor);
		wl_surface_attach(sides[i].surf, sides[i].buf, 0, 0);
		wl_surface_damage_buffer(sides[i].surf, 0, 0,
					 sides[i].w, sides[i].h);
		wl_surface_commit(sides[i].surf);
	}
	/* Round-trip so the compositor sees the commits before we
	 * reference the surfaces in attach_decoration. */
	wl_display_roundtrip(p.display);

	printf("qdwin-probe: attach_decoration handle=%u N=%d E=%d S=%d W=%d\n",
	       p.toplevel_handle, in_n, in_e, in_s, in_w);
	qdwin_shell_v1_attach_decoration(p.shell, p.toplevel_handle,
					 sides[0].surf, sides[1].surf,
					 sides[2].surf, sides[3].surf);
	wl_display_flush(p.display);

	if (dispatch_until(&p, done_geometry, timeout) < 0) {
		fprintf(stderr, "qdwin-probe: timed out waiting for toplevel_geometry\n");
		/* Not fatal for S4: qdwin's set_size triggers an xdg
		 * configure → ack → commit → qdwin logs new size. The
		 * toplevel_geometry event only fires if qdwin explicitly
		 * sends it; our implementation sends it on committed when
		 * surface size changes. */
		return 4;
	}

	printf("qdwin-probe: inner geometry handle=%u %ux%u\n",
	       p.toplevel_handle, p.geometry_w, p.geometry_h);

	/* Keep chrome surfaces alive for --persist seconds so an
	 * external screenshot can capture them. */
	if (persist > 0) {
		printf("qdwin-probe: persisting for %d seconds\n", persist);
		int fd = wl_display_get_fd(p.display);
		int deadline_ms = persist * 1000;
		while (deadline_ms > 0) {
			wl_display_flush(p.display);
			struct pollfd pfd = { .fd = fd, .events = POLLIN };
			int pr = poll(&pfd, 1, 250);
			if (pr > 0) {
				if (wl_display_prepare_read(p.display) == 0)
					wl_display_read_events(p.display);
				wl_display_dispatch_pending(p.display);
			}
			deadline_ms -= 250;
		}
		printf("qdwin-probe: persist done, exiting\n");
	}
	return 0;
}
