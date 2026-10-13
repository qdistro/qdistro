/*
 * qdwin-nested-grab-probe — live driver for the D9 layer-popup grab scope.
 *
 * One wl_client owns four surfaces:
 *   L   a zwlr_layer_surface (TOP layer, anchored top-left at the given
 *       margins, exclusive_zone -1 so other panels do not move it);
 *   T   a sibling xdg_toplevel (same client — the surface D9 must keep
 *       grabbed input away from);
 *   P1  an xdg_popup parented on L (zwlr_layer_surface_v1.get_popup) and
 *       grabbed with the serial of a real press on L;
 *   P2  with --nested, a second xdg_popup parented on P1 (the "nested
 *       submenu"), grabbed with the same serial.
 *
 * The probe waits for one left press on L (the caller injects it), opens
 * the popup chain with that press's serial, then logs every wl_pointer
 * enter/leave/button against the surface it lands on, plus popup_done, until
 * --duration expires or SIGTERM. The caller drives the pointer and grades the
 * log: a press while the chain is open must reach P1/P2 when it lands on them
 * and must NOT reach T (an outside press dismisses the chain instead).
 *
 * stdout, line-buffered:
 *   READY pid=<pid>
 *   GEOM <L|P1|P2> <x> <y> <w> <h>      (global, from the margins + configures)
 *   OPEN serial=<serial> nested=<0|1>
 *   EV <ENTER|LEAVE> <L|T|P1|P2|?>
 *   EV BUTTON <surface> <pressed|released>
 *   EV POPUP_DONE <P1|P2>
 *   GRAB_REFUSED <P1|P2>               (protocol error on the grab)
 * Exit: 0 after --duration/SIGTERM, 2 setup failure, 3 protocol error.
 *
 * Usage: qdwin-nested-grab-probe [--nested] [--margin-left N]
 *                                [--margin-top N] [--duration S]
 *                                [--selftest-open]
 * --selftest-open opens the chain at once WITHOUT a grab (no input, no seat
 * needed): a headless check of the surface/popup/geometry plumbing only.
 * Honours $WAYLAND_DISPLAY. Needs zwlr_layer_shell_v1, which qdwin only
 * offers to the shell client or (with no shell bound) to allowed_uid — run
 * it as the session user with qdshell stopped.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <linux/input-event-codes.h>
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
#include "wlr-layer-shell-unstable-v1-client-protocol.h"

#define L_W 200
#define L_H 80
#define T_W 240
#define T_H 160
#define P_W 160
#define P_H 100

struct popup {
	struct wl_surface *surf;
	struct xdg_surface *xsurf;
	struct xdg_popup *popup;
	struct wl_buffer *buf;
	int configured, acked, done;
	int32_t x, y;		/* configure, parent-relative */
	const char *name;
};

struct ctx {
	struct wl_display *display;
	struct wl_compositor *compositor;
	struct wl_shm *shm;
	struct xdg_wm_base *wm_base;
	struct zwlr_layer_shell_v1 *layer_shell;
	struct wl_seat *seat;
	struct wl_pointer *pointer;

	struct wl_surface *l_surf;
	struct zwlr_layer_surface_v1 *l_layer;
	int l_configured;

	struct wl_surface *t_surf;
	struct xdg_surface *t_xsurf;
	struct xdg_toplevel *t_top;
	int t_configured;

	struct popup p1, p2;
	struct wl_surface *focus;
	uint32_t open_serial;
	int no_grab;		/* --selftest-open */
	int want_open;		/* 1 = press seen, open; 2 = opened */
};

static volatile sig_atomic_t stop;
static void on_term(int s) { (void)s; stop = 1; }

static struct wl_buffer *
make_buffer(struct wl_shm *shm, int w, int h, uint32_t colour)
{
	int stride = w * 4, size = stride * h;
	int fd = memfd_create("qdwin-nested-grab-probe", MFD_CLOEXEC);
	if (fd < 0) return NULL;
	if (ftruncate(fd, size) < 0) { close(fd); return NULL; }
	uint32_t *px = mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
	if (px == MAP_FAILED) { close(fd); return NULL; }
	for (int i = 0; i < w * h; i++) px[i] = colour;
	struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, w, h, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	munmap(px, size);
	close(fd);
	return buf;
}

static const char *
surf_name(struct ctx *c, struct wl_surface *s)
{
	if (!s) return "-";
	if (s == c->l_surf) return "L";
	if (s == c->t_surf) return "T";
	if (s == c->p1.surf) return "P1";
	if (s == c->p2.surf) return "P2";
	return "?";
}

/* ---- wl_pointer ---------------------------------------------------- */

static void
ptr_enter(void *d, struct wl_pointer *p, uint32_t serial,
	  struct wl_surface *s, wl_fixed_t x, wl_fixed_t y)
{
	struct ctx *c = d;
	(void)p; (void)serial; (void)x; (void)y;
	c->focus = s;
	printf("EV ENTER %s\n", surf_name(c, s));
}
static void
ptr_leave(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *s)
{
	struct ctx *c = d;
	(void)p; (void)serial;
	printf("EV LEAVE %s\n", surf_name(c, s));
	if (c->focus == s)
		c->focus = NULL;
}
static void
ptr_motion(void *d, struct wl_pointer *p, uint32_t t, wl_fixed_t x, wl_fixed_t y)
{ (void)d; (void)p; (void)t; (void)x; (void)y; }
static void
ptr_button(void *d, struct wl_pointer *p, uint32_t serial, uint32_t time,
	   uint32_t button, uint32_t state)
{
	struct ctx *c = d;
	(void)p; (void)time;
	int pressed = state == WL_POINTER_BUTTON_STATE_PRESSED;
	printf("EV BUTTON %s %s\n", surf_name(c, c->focus),
	       pressed ? "pressed" : "released");
	if (pressed && button == BTN_LEFT && c->focus == c->l_surf &&
	    !c->want_open) {
		c->want_open = 1;
		c->open_serial = serial;
	}
}
static void ptr_axis(void *d, struct wl_pointer *p, uint32_t t, uint32_t a,
		     wl_fixed_t v)
{ (void)d; (void)p; (void)t; (void)a; (void)v; }
static void ptr_frame(void *d, struct wl_pointer *p) { (void)d; (void)p; }
static void ptr_axis_source(void *d, struct wl_pointer *p, uint32_t s)
{ (void)d; (void)p; (void)s; }
static void ptr_axis_stop(void *d, struct wl_pointer *p, uint32_t t, uint32_t a)
{ (void)d; (void)p; (void)t; (void)a; }
static void ptr_axis_discrete(void *d, struct wl_pointer *p, uint32_t a,
			      int32_t v)
{ (void)d; (void)p; (void)a; (void)v; }
static const struct wl_pointer_listener pointer_impl = {
	.enter = ptr_enter, .leave = ptr_leave, .motion = ptr_motion,
	.button = ptr_button, .axis = ptr_axis, .frame = ptr_frame,
	.axis_source = ptr_axis_source, .axis_stop = ptr_axis_stop,
	.axis_discrete = ptr_axis_discrete,
};

static void
seat_caps(void *d, struct wl_seat *seat, uint32_t caps)
{
	struct ctx *c = d;
	if ((caps & WL_SEAT_CAPABILITY_POINTER) && !c->pointer) {
		c->pointer = wl_seat_get_pointer(seat);
		wl_pointer_add_listener(c->pointer, &pointer_impl, c);
	}
}
static void seat_name(void *d, struct wl_seat *s, const char *n)
{ (void)d; (void)s; (void)n; }
static const struct wl_seat_listener seat_impl = {
	.capabilities = seat_caps, .name = seat_name,
};

/* ---- xdg ----------------------------------------------------------- */

static void wm_ping(void *d, struct xdg_wm_base *b, uint32_t serial)
{ (void)d; xdg_wm_base_pong(b, serial); }
static const struct xdg_wm_base_listener wm_impl = { .ping = wm_ping };

static void
t_xsurf_configure(void *d, struct xdg_surface *xs, uint32_t serial)
{
	struct ctx *c = d;
	xdg_surface_ack_configure(xs, serial);
	c->t_configured = 1;
}
static const struct xdg_surface_listener t_xsurf_impl = {
	.configure = t_xsurf_configure,
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

static void
p_xsurf_configure(void *d, struct xdg_surface *xs, uint32_t serial)
{
	struct popup *p = d;
	xdg_surface_ack_configure(xs, serial);
	p->acked = 1;
}
static const struct xdg_surface_listener p_xsurf_impl = {
	.configure = p_xsurf_configure,
};
static void
p_configure(void *d, struct xdg_popup *pp, int32_t x, int32_t y,
	    int32_t w, int32_t h)
{
	struct popup *p = d;
	(void)pp; (void)w; (void)h;
	p->x = x; p->y = y;
	p->configured = 1;
}
static void
p_done(void *d, struct xdg_popup *pp)
{
	struct popup *p = d;
	(void)pp;
	if (!p->done)
		printf("EV POPUP_DONE %s\n", p->name);
	p->done = 1;
}
static void p_repositioned(void *d, struct xdg_popup *pp, uint32_t t)
{ (void)d; (void)pp; (void)t; }
static const struct xdg_popup_listener p_impl = {
	.configure = p_configure, .popup_done = p_done,
	.repositioned = p_repositioned,
};

/* ---- layer surface ------------------------------------------------- */

static void
l_configure(void *d, struct zwlr_layer_surface_v1 *ls, uint32_t serial,
	    uint32_t w, uint32_t h)
{
	struct ctx *c = d;
	(void)w; (void)h;
	zwlr_layer_surface_v1_ack_configure(ls, serial);
	c->l_configured = 1;
}
static void l_closed(void *d, struct zwlr_layer_surface_v1 *ls)
{ (void)d; (void)ls; stop = 1; }
static const struct zwlr_layer_surface_v1_listener l_impl = {
	.configure = l_configure, .closed = l_closed,
};

/* ---- registry ------------------------------------------------------ */

static void
on_global(void *d, struct wl_registry *reg, uint32_t name,
	  const char *iface, uint32_t version)
{
	struct ctx *c = d;
#define MIN(a, b) ((a) < (b) ? (a) : (b))
	if (!strcmp(iface, wl_compositor_interface.name))
		c->compositor = wl_registry_bind(reg, name,
			&wl_compositor_interface, MIN(version, 4));
	else if (!strcmp(iface, wl_shm_interface.name))
		c->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	else if (!strcmp(iface, xdg_wm_base_interface.name)) {
		c->wm_base = wl_registry_bind(reg, name,
			&xdg_wm_base_interface, MIN(version, 3));
		xdg_wm_base_add_listener(c->wm_base, &wm_impl, c);
	} else if (!strcmp(iface, zwlr_layer_shell_v1_interface.name))
		c->layer_shell = wl_registry_bind(reg, name,
			&zwlr_layer_shell_v1_interface, MIN(version, 4));
	else if (!strcmp(iface, wl_seat_interface.name) && !c->seat) {
		c->seat = wl_registry_bind(reg, name, &wl_seat_interface,
					   MIN(version, 5));
		wl_seat_add_listener(c->seat, &seat_impl, c);
	}
#undef MIN
}
static void on_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener reg_impl = {
	.global = on_global, .global_remove = on_global_remove,
};

/* ---- helpers ------------------------------------------------------- */

/* Pump events for up to ms; returns -1 once the connection is dead. */
static int
pump(struct ctx *c, int ms)
{
	struct pollfd pfd = { .fd = wl_display_get_fd(c->display),
			      .events = POLLIN };
	if (wl_display_dispatch_pending(c->display) < 0)
		return -1;
	if (wl_display_flush(c->display) < 0 && errno != EAGAIN)
		return -1;
	int r = poll(&pfd, 1, ms);
	if (r > 0 && wl_display_dispatch(c->display) < 0)
		return -1;
	fflush(stdout);
	return 0;
}

static int
wait_flag(struct ctx *c, int *flag, int ms)
{
	while (!*flag && ms > 0 && !stop) {
		if (pump(c, 50) < 0)
			return -1;
		ms -= 50;
	}
	return *flag ? 0 : -1;
}

/* Create, grab and map one popup. parent_x == NULL means layer-parented. */
static int
open_popup(struct ctx *c, struct popup *p, struct xdg_surface *parent_x,
	   int anchor_w, int anchor_h, enum xdg_positioner_anchor anchor,
	   enum xdg_positioner_gravity gravity, uint32_t colour)
{
	struct xdg_positioner *pos = xdg_wm_base_create_positioner(c->wm_base);
	xdg_positioner_set_size(pos, P_W, P_H);
	xdg_positioner_set_anchor_rect(pos, 0, 0, anchor_w, anchor_h);
	xdg_positioner_set_anchor(pos, anchor);
	xdg_positioner_set_gravity(pos, gravity);

	p->surf = wl_compositor_create_surface(c->compositor);
	p->xsurf = xdg_wm_base_get_xdg_surface(c->wm_base, p->surf);
	xdg_surface_add_listener(p->xsurf, &p_xsurf_impl, p);
	p->popup = xdg_surface_get_popup(p->xsurf, parent_x, pos);
	xdg_popup_add_listener(p->popup, &p_impl, p);
	xdg_positioner_destroy(pos);
	if (!parent_x)
		zwlr_layer_surface_v1_get_popup(c->l_layer, p->popup);
	if (!c->no_grab)
		xdg_popup_grab(p->popup, c->seat, c->open_serial);
	wl_surface_commit(p->surf);

	if (wait_flag(c, &p->acked, 3000) < 0) {
		if (wl_display_get_error(c->display)) {
			printf("GRAB_REFUSED %s\n", p->name);
			return -1;
		}
		fprintf(stderr, "qdwin-nested-grab-probe: %s never configured "
			"(done=%d)\n", p->name, p->done);
		return -1;
	}
	p->buf = make_buffer(c->shm, P_W, P_H, colour);
	if (!p->buf)
		return -1;
	wl_surface_attach(p->surf, p->buf, 0, 0);
	wl_surface_damage_buffer(p->surf, 0, 0, P_W, P_H);
	wl_surface_commit(p->surf);
	if (wl_display_roundtrip(c->display) < 0) {
		printf("GRAB_REFUSED %s\n", p->name);
		return -1;
	}
	return 0;
}

int
main(int argc, char **argv)
{
	int nested = 0, margin_left = 1040, margin_top = 480, duration = 60;
	int selftest = 0;
	struct option opts[] = {
		{"nested",      no_argument,       0, 'n'},
		{"margin-left", required_argument, 0, 'x'},
		{"margin-top",  required_argument, 0, 'y'},
		{"duration",    required_argument, 0, 'd'},
		{"selftest-open", no_argument,     0, 's'},
		{0, 0, 0, 0},
	};
	int o;
	while ((o = getopt_long(argc, argv, "nx:y:d:s", opts, NULL)) != -1) {
		switch (o) {
		case 'n': nested = 1; break;
		case 'x': margin_left = atoi(optarg); break;
		case 'y': margin_top = atoi(optarg); break;
		case 'd': duration = atoi(optarg); break;
		case 's': selftest = 1; break;
		default: return 2;
		}
	}
	if (margin_left < 0 || margin_top < 0 || duration < 1 ||
	    duration > 3600) {
		fprintf(stderr, "bad arguments\n");
		return 2;
	}
	setvbuf(stdout, NULL, _IOLBF, 0);
	signal(SIGTERM, on_term);
	signal(SIGINT, on_term);

	struct ctx c = {0};
	c.p1.name = "P1";
	c.p2.name = "P2";
	c.display = wl_display_connect(NULL);
	if (!c.display) {
		fprintf(stderr, "qdwin-nested-grab-probe: connect failed\n");
		return 2;
	}
	struct wl_registry *reg = wl_display_get_registry(c.display);
	wl_registry_add_listener(reg, &reg_impl, &c);
	if (wl_display_roundtrip(c.display) < 0 ||
	    wl_display_roundtrip(c.display) < 0) {
		fprintf(stderr, "qdwin-nested-grab-probe: registry failed "
			"(layer-shell bind refused?)\n");
		return 2;
	}
	if (!c.compositor || !c.shm || !c.wm_base || !c.layer_shell ||
	    (!c.seat && !selftest)) {
		fprintf(stderr, "qdwin-nested-grab-probe: missing globals "
			"(layer_shell=%p seat=%p)\n",
			(void *)c.layer_shell, (void *)c.seat);
		return 2;
	}

	/* L: the layer anchor. */
	c.l_surf = wl_compositor_create_surface(c.compositor);
	c.l_layer = zwlr_layer_shell_v1_get_layer_surface(
		c.layer_shell, c.l_surf, NULL,
		ZWLR_LAYER_SHELL_V1_LAYER_TOP, "qdwin-nested-grab-probe");
	zwlr_layer_surface_v1_add_listener(c.l_layer, &l_impl, &c);
	zwlr_layer_surface_v1_set_size(c.l_layer, L_W, L_H);
	zwlr_layer_surface_v1_set_anchor(c.l_layer,
		ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP |
		ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT);
	zwlr_layer_surface_v1_set_margin(c.l_layer, margin_top, 0, 0,
					 margin_left);
	zwlr_layer_surface_v1_set_exclusive_zone(c.l_layer, -1);
	zwlr_layer_surface_v1_set_keyboard_interactivity(c.l_layer,
		ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE);
	wl_surface_commit(c.l_surf);
	if (wait_flag(&c, &c.l_configured, 3000) < 0) {
		fprintf(stderr, "qdwin-nested-grab-probe: layer never configured\n");
		return 2;
	}
	struct wl_buffer *lbuf = make_buffer(c.shm, L_W, L_H, 0xff2060c0);
	if (!lbuf) return 2;
	wl_surface_attach(c.l_surf, lbuf, 0, 0);
	wl_surface_damage_buffer(c.l_surf, 0, 0, L_W, L_H);
	wl_surface_commit(c.l_surf);

	/* T: the same-client sibling toplevel. */
	c.t_surf = wl_compositor_create_surface(c.compositor);
	c.t_xsurf = xdg_wm_base_get_xdg_surface(c.wm_base, c.t_surf);
	xdg_surface_add_listener(c.t_xsurf, &t_xsurf_impl, &c);
	c.t_top = xdg_surface_get_toplevel(c.t_xsurf);
	xdg_toplevel_add_listener(c.t_top, &tl_impl, &c);
	xdg_toplevel_set_title(c.t_top, "qdwin-nested-grab-probe");
	xdg_toplevel_set_app_id(c.t_top, "qdwin-nested-grab-probe");
	xdg_surface_set_window_geometry(c.t_xsurf, 0, 0, T_W, T_H);
	wl_surface_commit(c.t_surf);
	if (wait_flag(&c, &c.t_configured, 3000) < 0) {
		fprintf(stderr, "qdwin-nested-grab-probe: toplevel never configured\n");
		return 2;
	}
	struct wl_buffer *tbuf = make_buffer(c.shm, T_W, T_H, 0xff40a040);
	if (!tbuf) return 2;
	wl_surface_attach(c.t_surf, tbuf, 0, 0);
	wl_surface_damage_buffer(c.t_surf, 0, 0, T_W, T_H);
	wl_surface_commit(c.t_surf);
	if (wl_display_roundtrip(c.display) < 0)
		return 3;

	printf("READY pid=%d\n", (int)getpid());
	printf("GEOM L %d %d %d %d\n", margin_left, margin_top, L_W, L_H);
	if (selftest) {
		c.no_grab = 1;
		c.want_open = 1;
	}

	struct timespec t0;
	clock_gettime(CLOCK_MONOTONIC, &t0);
	for (;;) {
		struct timespec now;
		clock_gettime(CLOCK_MONOTONIC, &now);
		if (stop || now.tv_sec - t0.tv_sec >= duration)
			break;
		if (pump(&c, 100) < 0) {
			fprintf(stderr, "qdwin-nested-grab-probe: connection "
				"lost (error %d)\n", wl_display_get_error(c.display));
			return 3;
		}
		if (c.want_open == 1) {
			c.want_open = 2;	/* one chain per run */
			/* P1 below L; P2 to P1's left (keeps the chain on
			 * the output for the default right-side margins). */
			if (open_popup(&c, &c.p1, NULL, L_W, L_H,
				       XDG_POSITIONER_ANCHOR_BOTTOM_LEFT,
				       XDG_POSITIONER_GRAVITY_BOTTOM_RIGHT,
				       0xffc04020) < 0)
				return 3;
			int p1x = margin_left + c.p1.x, p1y = margin_top + c.p1.y;
			printf("GEOM P1 %d %d %d %d\n", p1x, p1y, P_W, P_H);
			if (nested) {
				if (open_popup(&c, &c.p2, c.p1.xsurf, P_W, P_H,
					       XDG_POSITIONER_ANCHOR_TOP_LEFT,
					       XDG_POSITIONER_GRAVITY_BOTTOM_LEFT,
					       0xffe0c020) < 0)
					return 3;
				printf("GEOM P2 %d %d %d %d\n", p1x + c.p2.x,
				       p1y + c.p2.y, P_W, P_H);
			}
			printf("OPEN serial=%u nested=%d\n", c.open_serial, nested);
		}
		/* Tear dismissed popups down child-first. */
		if (c.p2.popup && (c.p2.done || c.p1.done)) {
			xdg_popup_destroy(c.p2.popup);
			c.p2.popup = NULL;
		}
		if (c.p1.popup && c.p1.done && !c.p2.popup) {
			xdg_popup_destroy(c.p1.popup);
			c.p1.popup = NULL;
		}
	}
	wl_display_disconnect(c.display);
	return 0;
}
