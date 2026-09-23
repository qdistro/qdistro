/*
 * qdistro-test-clipboard-sink — focused destination client for spec/10
 * v15 wl_data_offer.receive end-to-end testing.
 *
 * The companion to qdistro-test-clipboard-source. Creates a real
 * xdg_toplevel (so qdshell's toplevel_added event fires and the bats
 * driver can `inject-focus` to this client via the qdwin_shell_v1
 * ctrl-socket), binds wl_data_device_manager + wl_seat, and listens
 * for the wl_data_device.selection event.
 *
 * On a non-NULL selection offer, the client calls
 * wl_data_offer.receive(--mime, fd_write) and pipes the result to
 * stdout (or --output FILE), then exits 0. On the v15 path:
 *
 *   sink.receive(mime, fd) → qdwin's per-source send-shim suspends
 *   the original send → fires data_offer_receive_pending(handle) to
 *   qdshell → qdshell calls broker.CheckClipboardReceive → echoes
 *   data_offer_receive_decision(handle, allow|deny) → qdwin allows
 *   (original send fires) or denies (close(fd) → sink reads EOF).
 *
 * Why this exists: wl-paste under headless RDP never acquires
 * wl_keyboard.enter because sdl-freerdp dummy doesn't synthesize real
 * input. wl-paste's data_device sees no offer → no .receive() call →
 * the v15 wire flow is never exercised. This client takes a deliberate
 * shortcut: it only requires that the bats driver injects focus to
 * its toplevel (which qdshell does via the v14 inject-focus ctrl
 * command), at which point qdwin sends the data_device.data_offer +
 * selection events to it.
 *
 * Usage:
 *   qdistro-test-clipboard-sink --title T [--mime text/plain]
 *                               [--output FILE] [--timeout SECS]
 *
 * Exit codes:
 *   0 — read at least one byte and the offer was the requested mime.
 *   2 — usage error.
 *   3 — timed out before any selection arrived.
 *   4 — got selection but the read returned EOF immediately (deny path).
 *   5 — environment / wayland error.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
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
	struct wl_data_device_manager *ddm;
	struct wl_seat *seat;
	struct wl_data_device *device;

	const char *want_mime;
	int got_offer_with_mime;          /* 1 if current offer carries want_mime */
	struct wl_data_offer *current_offer;
	int got_selection;
	int receive_done;                 /* set when we've completed the read */
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

/* wl_data_offer listener */
static void
data_offer_offer(void *data, struct wl_data_offer *offer, const char *mime)
{
	struct ctx *c = data;
	(void)offer;
	if (c->want_mime && mime && !strcmp(mime, c->want_mime)) {
		c->got_offer_with_mime = 1;
	}
	fprintf(stderr, "[sink] data_offer: mime=%s\n", mime ? mime : "(null)");
}
static void
data_offer_source_actions(void *d, struct wl_data_offer *o, uint32_t a)
{ (void)d; (void)o; (void)a; }
static void
data_offer_action(void *d, struct wl_data_offer *o, uint32_t a)
{ (void)d; (void)o; (void)a; }
static const struct wl_data_offer_listener data_offer_listener = {
	.offer = data_offer_offer,
	.source_actions = data_offer_source_actions,
	.action = data_offer_action,
};

/* wl_data_device listener — fires data_offer (new offer) +
 * enter/leave/motion (DnD, ignored) + drop (DnD, ignored) +
 * selection (the selection is now this offer, or NULL). */
static void
data_device_data_offer(void *data, struct wl_data_device *dev,
		       struct wl_data_offer *offer)
{
	struct ctx *c = data;
	(void)dev;
	c->got_offer_with_mime = 0;
	c->current_offer = offer;
	wl_data_offer_add_listener(offer, &data_offer_listener, c);
}
static void
data_device_enter(void *d, struct wl_data_device *dev, uint32_t s,
		  struct wl_surface *surf, wl_fixed_t x, wl_fixed_t y,
		  struct wl_data_offer *offer)
{ (void)d; (void)dev; (void)s; (void)surf; (void)x; (void)y; (void)offer; }
static void
data_device_leave(void *d, struct wl_data_device *dev)
{ (void)d; (void)dev; }
static void
data_device_motion(void *d, struct wl_data_device *dev, uint32_t t,
		   wl_fixed_t x, wl_fixed_t y)
{ (void)d; (void)dev; (void)t; (void)x; (void)y; }
static void
data_device_drop(void *d, struct wl_data_device *dev)
{ (void)d; (void)dev; }
static void
data_device_selection(void *data, struct wl_data_device *dev,
		      struct wl_data_offer *offer)
{
	struct ctx *c = data;
	(void)dev;
	if (offer == NULL) {
		c->got_selection = 0;
		c->current_offer = NULL;
		fprintf(stderr, "[sink] selection cleared\n");
		return;
	}
	c->got_selection = 1;
	c->current_offer = offer;
	fprintf(stderr, "[sink] selection set; offer carries want=%d\n",
		c->got_offer_with_mime);
}
static const struct wl_data_device_listener data_device_listener = {
	.data_offer = data_device_data_offer,
	.enter = data_device_enter,
	.leave = data_device_leave,
	.motion = data_device_motion,
	.drop = data_device_drop,
	.selection = data_device_selection,
};

/* wl_seat listener — required so the compositor sees a registered
 * seat consumer (focus delivery may depend on capabilities being
 * acked).  We don't consume keyboard events, but we register the
 * listener for symmetry with seat_capabilities. */
static void
seat_capabilities(void *d, struct wl_seat *s, uint32_t caps)
{ (void)d; (void)s; (void)caps; }
static void
seat_name(void *d, struct wl_seat *s, const char *name)
{ (void)d; (void)s; (void)name; }
static const struct wl_seat_listener seat_listener = {
	.capabilities = seat_capabilities,
	.name = seat_name,
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
	} else if (!strcmp(interface,
			   wl_data_device_manager_interface.name)) {
		c->ddm = wl_registry_bind(reg, name,
					  &wl_data_device_manager_interface,
					  version > 3 ? 3 : version);
	} else if (!strcmp(interface, wl_seat_interface.name) && !c->seat) {
		c->seat = wl_registry_bind(reg, name, &wl_seat_interface,
					   version > 5 ? 5 : version);
		wl_seat_add_listener(c->seat, &seat_listener, c);
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
	int fd = memfd_create("qdistro-test-clipboard-sink", MFD_CLOEXEC);
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

static void
xdg_surface_configure(void *data, struct xdg_surface *xs, uint32_t serial)
{ (void)data; xdg_surface_ack_configure(xs, serial); }
static const struct xdg_surface_listener xdg_surface_impl = {
	.configure = xdg_surface_configure,
};

static void
xdg_toplevel_configure(void *d, struct xdg_toplevel *t,
		       int32_t w, int32_t h, struct wl_array *states)
{ (void)d; (void)t; (void)w; (void)h; (void)states; }
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

static double now_seconds(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec / 1e9;
}

int main(int argc, char **argv)
{
	const char *title = "qdistro-test-clipboard-sink";
	const char *mime = "text/plain";
	const char *output_path = NULL;
	double timeout_s = 30.0;
	struct option opts[] = {
		{"title",   required_argument, 0, 't'},
		{"mime",    required_argument, 0, 'm'},
		{"output",  required_argument, 0, 'o'},
		{"timeout", required_argument, 0, 'T'},
		{0,0,0,0},
	};
	int o;
	while ((o = getopt_long(argc, argv, "t:m:o:T:", opts, NULL)) != -1) {
		switch (o) {
		case 't': title = optarg; break;
		case 'm': mime = optarg; break;
		case 'o': output_path = optarg; break;
		case 'T': timeout_s = atof(optarg); break;
		default:
			fprintf(stderr, "usage: %s --title T [--mime M] "
					 "[--output FILE] [--timeout SECS]\n",
				argv[0]);
			return 2;
		}
	}
	if (timeout_s <= 0) timeout_s = 30.0;

	signal(SIGTERM, on_sig);
	signal(SIGINT, on_sig);

	struct ctx c = {0};
	c.want_mime = mime;
	c.display = wl_display_connect(NULL);
	if (!c.display) {
		fprintf(stderr, "wl_display_connect failed\n");
		return 5;
	}
	struct wl_registry *reg = wl_display_get_registry(c.display);
	wl_registry_add_listener(reg, &registry_listener, &c);
	wl_display_roundtrip(c.display);
	if (!c.compositor || !c.shm || !c.xdg_wm_base ||
	    !c.ddm || !c.seat) {
		fprintf(stderr, "missing globals — compositor=%p shm=%p "
				 "xdg=%p ddm=%p seat=%p\n",
			(void*)c.compositor, (void*)c.shm,
			(void*)c.xdg_wm_base, (void*)c.ddm, (void*)c.seat);
		return 5;
	}

	/* Create the toplevel so qdshell sees toplevel_added(handle). */
	struct wl_surface *surf = wl_compositor_create_surface(c.compositor);
	struct xdg_surface *xsurf =
		xdg_wm_base_get_xdg_surface(c.xdg_wm_base, surf);
	xdg_surface_add_listener(xsurf, &xdg_surface_impl, NULL);
	struct xdg_toplevel *top = xdg_surface_get_toplevel(xsurf);
	xdg_toplevel_add_listener(top, &xdg_toplevel_impl, NULL);
	xdg_toplevel_set_title(top, title);
	xdg_toplevel_set_app_id(top, "qdistro-test-clipboard-sink");
	wl_surface_commit(surf);
	wl_display_roundtrip(c.display);

	struct wl_buffer *buf = make_solid_buffer(c.shm, 200, 150, 0xff406040);
	if (!buf) { fprintf(stderr, "make_solid_buffer failed\n"); return 5; }
	wl_surface_attach(surf, buf, 0, 0);
	wl_surface_damage_buffer(surf, 0, 0, 200, 150);
	wl_surface_commit(surf);

	/* Get the data_device on the (only) seat. data_device.data_offer
	 * + .selection events fire when this client has focus on the seat.
	 * The bats driver injects focus via qdshell's ctrl-socket. */
	c.device = wl_data_device_manager_get_data_device(c.ddm, c.seat);
	wl_data_device_add_listener(c.device, &data_device_listener, &c);

	fprintf(stderr, "[sink] ready; title=%s mime=%s timeout=%.1fs\n",
		title, mime, timeout_s);

	double deadline = now_seconds() + timeout_s;
	while (running && !c.got_selection) {
		double remain = deadline - now_seconds();
		if (remain <= 0) {
			fprintf(stderr, "[sink] timeout waiting for "
					 "selection\n");
			return 3;
		}
		struct pollfd pfd = {
			.fd = wl_display_get_fd(c.display),
			.events = POLLIN,
		};
		wl_display_flush(c.display);
		int r = poll(&pfd, 1, (int)(remain * 1000));
		if (r > 0) {
			if (wl_display_dispatch(c.display) < 0) {
				fprintf(stderr,
					"[sink] wl_display_dispatch failed\n");
				return 5;
			}
		}
	}
	if (!c.got_selection) return 3;
	if (!c.current_offer) {
		fprintf(stderr, "[sink] selection arrived without offer\n");
		return 5;
	}
	if (!c.got_offer_with_mime) {
		fprintf(stderr, "[sink] offer does not carry mime=%s — "
				 "calling receive anyway (compositor "
				 "decides)\n", mime);
	}

	/* Pipe + receive(). The write-end goes to the compositor; we
	 * read from the read-end. */
	int p[2];
	if (pipe(p) < 0) {
		fprintf(stderr, "[sink] pipe: %s\n", strerror(errno));
		return 5;
	}
	wl_data_offer_receive(c.current_offer, mime, p[1]);
	close(p[1]);
	wl_display_flush(c.display);

	/* Drain the compositor side concurrently with reading the pipe.
	 * On the v15 allow path, qdwin's wrap_send writes to p[1] when
	 * the shell echoes data_offer_receive_decision; on deny, qdwin
	 * close(fd) and we see EOF immediately. */
	FILE *out = stdout;
	if (output_path) {
		out = fopen(output_path, "wb");
		if (!out) {
			fprintf(stderr, "[sink] fopen %s: %s\n",
				output_path, strerror(errno));
			return 5;
		}
	}
	double read_deadline = now_seconds() + timeout_s;
	size_t total = 0;
	for (;;) {
		double remain = read_deadline - now_seconds();
		if (remain <= 0) break;
		struct pollfd pfds[2] = {
			{ .fd = p[0], .events = POLLIN },
			{ .fd = wl_display_get_fd(c.display),
			  .events = POLLIN },
		};
		wl_display_flush(c.display);
		int rr = poll(pfds, 2, (int)(remain * 1000));
		if (rr <= 0) break;
		if (pfds[1].revents & POLLIN) {
			if (wl_display_dispatch(c.display) < 0) break;
		}
		if (pfds[0].revents & POLLIN) {
			char buf2[4096];
			ssize_t n = read(p[0], buf2, sizeof(buf2));
			if (n == 0) {
				/* EOF — finished. */
				break;
			}
			if (n < 0) {
				if (errno == EINTR) continue;
				break;
			}
			fwrite(buf2, 1, n, out);
			total += n;
		}
		if (pfds[0].revents & (POLLHUP | POLLERR)) {
			/* Try one more read for any pending bytes. */
			char buf2[4096];
			ssize_t n;
			while ((n = read(p[0], buf2, sizeof(buf2))) > 0) {
				fwrite(buf2, 1, n, out);
				total += n;
			}
			break;
		}
	}
	fflush(out);
	if (output_path) fclose(out);
	close(p[0]);

	fprintf(stderr, "[sink] received %zu bytes\n", total);

	wl_data_offer_destroy(c.current_offer);
	c.current_offer = NULL;

	wl_data_device_release(c.device);
	wl_data_device_manager_destroy(c.ddm);
	xdg_toplevel_destroy(top);
	xdg_surface_destroy(xsurf);
	wl_surface_destroy(surf);
	wl_buffer_destroy(buf);
	wl_seat_release(c.seat);
	wl_display_disconnect(c.display);

	if (total == 0) return 4;       /* deny path = EOF before any byte */
	return 0;
}
