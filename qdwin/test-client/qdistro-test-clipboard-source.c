/*
 * qdistro-test-clipboard-source — set the wayland clipboard from the
 * command line, without waiting for keyboard focus.
 *
 * wl-clipboard / wl-copy waits for wl_keyboard.enter on its hidden
 * surface before calling wl_data_device.set_selection (a defensive
 * focus check). Under weston-rdp with sdl-freerdp dummy, no real
 * keyboard input arrives, so wl-copy hangs forever — which makes it
 * unusable for headless bats coverage of the spec/10 gate.
 *
 * This helper skips the focus wait and calls set_selection
 * immediately on the first wl_seat. wl_data_device.set_selection's
 * `serial` field is supposed to be a recent input serial; the spec
 * says the compositor MAY validate, and vendored libweston DOES
 * (weston_seat_set_selection rejects a serial older than the seat's
 * selection_serial while a source holds the seat). Deny/focus clears
 * bump that serial, so a second serial=0 offer is silently dropped.
 * The helper therefore sweeps three ascending serials — the same
 * scheme qdwin-test-clipboard-emit uses — and the payload source goes
 * last so it owns the selection.
 *
 * Usage:
 *   qdistro-test-clipboard-source [--mime text/plain] [--text "payload"]
 *                                 [--toplevel] [--title "name"]
 *                                 [--emit-interval MS]
 *   ... | qdistro-test-clipboard-source --mime text/plain
 *
 * --toplevel makes the helper own a real xdg_toplevel (needed by the
 * lineage-enforce tests: qdshell only relays the source's pid when the
 * v23 sidecar tuple matches the FOCUSED toplevel's attested tag — i.e.
 * the tagged source must be the focused window's client).
 *
 * --emit-interval re-issues the serial sweep every MS ms (the first
 * offer under a cold identity-verify cache is denied; re-emitting is
 * how a registered tagged source reaches the verified allow without a
 * pid change, which would re-cool the cache).
 *
 * On a `send` event from another client (paste), writes the payload
 * to the offered fd. Loops until SIGTERM.
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
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#include "xdg-shell-client-protocol.h"

static int running = 1;
static const char *payload = "qdistro-test-clipboard";
static size_t payload_len = 0;
static long emit_interval = 0;

static void on_sig(int s) { (void)s; running = 0; }

struct ctx {
	struct wl_display *display;
	struct wl_compositor *compositor;
	struct xdg_wm_base *wm_base;
	struct wl_data_device_manager *ddm;
	struct wl_seat *seat;
	struct wl_data_device *device;
	struct wl_data_source *source;
	struct wl_data_source *probe1;
	struct wl_data_source *probe2;
	const char *mime;
	uint32_t serial_base;
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
xdg_surface_configure(void *data, struct xdg_surface *xs, uint32_t serial)
{
	(void)data;
	xdg_surface_ack_configure(xs, serial);
}
static const struct xdg_surface_listener xdg_surface_impl = {
	.configure = xdg_surface_configure,
};

static void
xdg_toplevel_configure(void *d, struct xdg_toplevel *t,
		       int32_t w, int32_t h, struct wl_array *s)
{ (void)d; (void)t; (void)w; (void)h; (void)s; }
static void
xdg_toplevel_close(void *d, struct xdg_toplevel *t)
{ (void)d; (void)t; running = 0; }
static void
xdg_toplevel_configure_bounds(void *d, struct xdg_toplevel *t,
			      int32_t w, int32_t h)
{ (void)d; (void)t; (void)w; (void)h; }
static void
xdg_toplevel_wm_capabilities(void *d, struct xdg_toplevel *t,
			     struct wl_array *s)
{ (void)d; (void)t; (void)s; }
static const struct xdg_toplevel_listener xdg_toplevel_impl = {
	.configure = xdg_toplevel_configure,
	.close = xdg_toplevel_close,
	.configure_bounds = xdg_toplevel_configure_bounds,
	.wm_capabilities = xdg_toplevel_wm_capabilities,
};

static void
data_source_target(void *d, struct wl_data_source *s, const char *mime)
{ (void)d; (void)s; (void)mime; }

static void
data_source_send(void *d, struct wl_data_source *s,
		 const char *mime, int32_t fd)
{
	(void)d; (void)s; (void)mime;
	ssize_t total = 0;
	while ((size_t)total < payload_len) {
		ssize_t n = write(fd, payload + total, payload_len - total);
		if (n <= 0) {
			if (errno == EINTR) continue;
			break;
		}
		total += n;
	}
	close(fd);
	fprintf(stderr, "[qdistro-test-clipboard-source] sent %zd bytes "
			 "for mime=%s\n", total, mime);
}

static void
data_source_cancelled(void *d, struct wl_data_source *s)
{
	(void)d; (void)s;
	fprintf(stderr, "[qdistro-test-clipboard-source] selection cancelled\n");
	/* A deny-path selection clear cancels this source. Single-shot sources
	 * exit; --emit-interval sources stay alive and re-offer at the next
	 * interval so a bound toplevel can survive pre-focus denies and keep
	 * offering until the gate allows. */
	if (emit_interval <= 0)
		running = 0;
}

static void
data_source_dnd_drop_performed(void *d, struct wl_data_source *s)
{ (void)d; (void)s; }

static void
data_source_dnd_finished(void *d, struct wl_data_source *s)
{ (void)d; (void)s; }

static void
data_source_action(void *d, struct wl_data_source *s, uint32_t action)
{ (void)d; (void)s; (void)action; }

static const struct wl_data_source_listener data_source_listener = {
	.target = data_source_target,
	.send = data_source_send,
	.cancelled = data_source_cancelled,
	.dnd_drop_performed = data_source_dnd_drop_performed,
	.dnd_finished = data_source_dnd_finished,
	.action = data_source_action,
};

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
	if (!strcmp(interface, wl_data_device_manager_interface.name)) {
		c->ddm = wl_registry_bind(reg, name,
					  &wl_data_device_manager_interface,
					  version > 3 ? 3 : version);
	} else if (!strcmp(interface, wl_compositor_interface.name)) {
		c->compositor = wl_registry_bind(reg, name,
					       &wl_compositor_interface, 1);
	} else if (!strcmp(interface, xdg_wm_base_interface.name)) {
		c->wm_base = wl_registry_bind(reg, name,
					      &xdg_wm_base_interface, 1);
		xdg_wm_base_add_listener(c->wm_base,
					 &xdg_wm_base_listener_impl, NULL);
	} else if (!strcmp(interface, wl_seat_interface.name) && !c->seat) {
		c->seat = wl_registry_bind(reg, name, &wl_seat_interface,
					   version > 5 ? 5 : version);
		wl_seat_add_listener(c->seat, &seat_listener, c);
	}
}
static void
registry_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_global_remove,
};

static long
now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

/* One serial sweep: two ascending probe sources then the real source,
 * so the real source owns the selection whichever probe wins (see the
 * stale-serial comment in main). serial_base ratchets upward each
 * emit so a re-offer is always newer than the seat's last accepted
 * serial. */
static void
emit_selection(struct ctx *c)
{
	wl_data_device_set_selection(c->device, c->probe1,
				     c->serial_base + 0x00000001u);
	wl_data_device_set_selection(c->device, c->probe2,
				     c->serial_base + 0x40000002u);
	wl_data_device_set_selection(c->device, c->source,
				     c->serial_base + 0x80000003u);
	c->serial_base += 0x01000000u;
	wl_display_flush(c->display);
}

int main(int argc, char **argv)
{
	const char *mime = "text/plain";
	const char *text_arg = NULL;
	const char *title = "qdistro-test-clipboard-source";
	int want_toplevel = 0;
	struct option opts[] = {
		{"mime", required_argument, 0, 'm'},
		{"text", required_argument, 0, 't'},
		{"toplevel", no_argument, 0, 1000},
		{"title", required_argument, 0, 1001},
		{"emit-interval", required_argument, 0, 'e'},
		{0, 0, 0, 0},
	};
	int o;
	while ((o = getopt_long(argc, argv, "m:t:e:", opts, NULL)) != -1) {
		switch (o) {
		case 'm': mime = optarg; break;
		case 't': text_arg = optarg; break;
		case 1000: want_toplevel = 1; break;
		case 1001: title = optarg; break;
		case 'e': emit_interval = strtol(optarg, NULL, 10); break;
		default:
			fprintf(stderr, "usage: %s [--mime M] [--text TEXT] "
				"[--toplevel] [--title T] [--emit-interval MS]\n",
				argv[0]);
			return 2;
		}
	}
	if (text_arg) {
		payload = text_arg;
	} else {
		/* Slurp stdin into a heap buffer. */
		char *buf = NULL;
		size_t cap = 0, n = 0;
		for (;;) {
			if (cap - n < 4096) {
				cap = cap ? cap * 2 : 4096;
				buf = realloc(buf, cap);
				if (!buf) return 1;
			}
			ssize_t r = read(0, buf + n, cap - n);
			if (r < 0) { if (errno == EINTR) continue; break; }
			if (r == 0) break;
			n += r;
		}
		payload = buf;
		payload_len = n;
	}
	if (text_arg) payload_len = strlen(text_arg);

	signal(SIGTERM, on_sig);
	signal(SIGINT, on_sig);

	struct ctx c = {0};
	c.mime = mime;
	/* QDISTRO_CLIP_SRC_DELAY_MS: sleep before connecting so a test can
	 * register this pid in the broker's launch-record store before the
	 * offer reaches the gate (lineage_enforce relays the client's
	 * pid+starttime). The delay is inside the binary itself, so the
	 * record's exe axis still matches after registration — unlike an
	 * external `sleep && exec` wrapper, which would register exe=sh. */
	const char *delay = getenv("QDISTRO_CLIP_SRC_DELAY_MS");
	if (delay && *delay) {
		long ms = strtol(delay, NULL, 10);
		if (ms > 0 && ms <= 60000) {
			struct timespec ts = { ms / 1000, (ms % 1000) * 1000000L };
			nanosleep(&ts, NULL);
		}
	}
	c.display = wl_display_connect(NULL);
	if (!c.display) {
		fprintf(stderr, "wl_display_connect failed\n");
		return 1;
	}
	struct wl_registry *reg = wl_display_get_registry(c.display);
	wl_registry_add_listener(reg, &registry_listener, &c);
	wl_display_roundtrip(c.display);
	if (!c.ddm || !c.seat) {
		fprintf(stderr, "missing globals — ddm=%p seat=%p\n",
			(void*)c.ddm, (void*)c.seat);
		return 1;
	}
	if (want_toplevel && (!c.compositor || !c.wm_base)) {
		fprintf(stderr, "--toplevel needs wl_compositor + xdg_wm_base "
			"(compositor=%p wm_base=%p)\n",
			(void*)c.compositor, (void*)c.wm_base);
		return 1;
	}

	if (want_toplevel) {
		/* A real xdg_toplevel makes this client own a window handle —
		 * the lineage-enforce tests focus it (inject-focus) so the
		 * selection's source_handle resolves to THIS client's
		 * attested tag. Commit first, then roundtrip so the
		 * compositor's toplevel_added/security_context/peer_identity
		 * sequence is on the wire before the first offer. */
		struct wl_surface *surf =
			wl_compositor_create_surface(c.compositor);
		struct xdg_surface *xsurf =
			xdg_wm_base_get_xdg_surface(c.wm_base, surf);
		xdg_surface_add_listener(xsurf, &xdg_surface_impl, NULL);
		struct xdg_toplevel *top = xdg_surface_get_toplevel(xsurf);
		xdg_toplevel_add_listener(top, &xdg_toplevel_impl, NULL);
		xdg_toplevel_set_title(top, title);
		xdg_toplevel_set_app_id(top, "qdistro-test-clipboard-source");
		wl_surface_commit(surf);
		wl_display_roundtrip(c.display);
	}

	c.device = wl_data_device_manager_get_data_device(c.ddm, c.seat);
	c.source = wl_data_device_manager_create_data_source(c.ddm);
	wl_data_source_add_listener(c.source, &data_source_listener, &c);
	wl_data_source_offer(c.source, mime);
	/* Bypass weston's stale-serial guard in weston_seat_set_selection():
	 *
	 *   if (seat->selection_data_source &&
	 *       seat->selection_serial - serial < UINT32_MAX / 2)
	 *           return;
	 *
	 * The guard rejects whenever (selection_serial - serial) lies in
	 * the lower half of the 32-bit space. The deny/focus-clear paths
	 * bump selection_serial to a fresh wl_display serial, so a second
	 * serial=0 offer while any source holds the seat is silently
	 * dropped — and a set that arrives while the shell cannot receive
	 * selection_set leaves exactly such a stuck source. The seat's
	 * serial is not readable client-side, so sweep three strictly-
	 * ascending serials whose accepting half-rings cover the whole
	 * 32-bit ring (qdwin-test-clipboard-emit uses the same scheme):
	 * once any probe wins, later serials are newer by definition and
	 * also pass; the REAL source goes last so it owns the selection.
	 */
	c.probe1 = wl_data_device_manager_create_data_source(c.ddm);
	c.probe2 = wl_data_device_manager_create_data_source(c.ddm);
	wl_data_source_offer(c.probe1, mime);
	wl_data_source_offer(c.probe2, mime);
	c.serial_base = 0x40000000u;
	emit_selection(&c);
	fprintf(stderr, "[qdistro-test-clipboard-source] set_selection mime=%s "
			 "payload_len=%zu\n", mime, payload_len);

	/* Event loop with an optional re-emit timer. wl_display_dispatch
	 * has no timeout form, so poll the wayland fd directly
	 * (prepare_read/read_events is the thread-safe dance). */
	long next_emit = emit_interval > 0 ? now_ms() + emit_interval : 0;
	struct pollfd pfd = { wl_display_get_fd(c.display), POLLIN, 0 };
	while (running) {
		while (wl_display_prepare_read(c.display) != 0)
			if (wl_display_dispatch_pending(c.display) < 0)
				goto out;
		pfd.events = POLLIN;
		if (wl_display_flush(c.display) < 0) {
			if (errno != EAGAIN) {
				wl_display_cancel_read(c.display);
				break;
			}
			pfd.events |= POLLOUT;
		}
		long timeout = -1;
		if (next_emit) {
			timeout = next_emit - now_ms();
			if (timeout < 0) timeout = 0;
		}
		int pr = poll(&pfd, 1, (int)timeout);
		if (pr < 0) {
			wl_display_cancel_read(c.display);
			if (errno == EINTR) continue;
			break;
		}
		if (pr > 0) {
			if (wl_display_read_events(c.display) < 0) break;
		} else {
			wl_display_cancel_read(c.display);
		}
		if (wl_display_dispatch_pending(c.display) < 0) break;
		if (next_emit && now_ms() >= next_emit) {
			emit_selection(&c);
			next_emit = now_ms() + emit_interval;
		}
	}
out:

	if (c.source) wl_data_source_destroy(c.source);
	if (c.device) wl_data_device_release(c.device);
	if (c.seat) wl_seat_release(c.seat);
	if (c.ddm) wl_data_device_manager_destroy(c.ddm);
	wl_display_disconnect(c.display);
	return 0;
}
