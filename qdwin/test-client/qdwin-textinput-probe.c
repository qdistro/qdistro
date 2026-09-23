/*
 * qdwin-textinput-probe — test client for qdwin's text-input-unstable-v3
 * foundation (Bucket A / P1): qdwin_text_input_manager_get_text_input,
 * qdwin_text_input_update_focus, the enter/leave focus wiring, and the
 * "inert without an input-method" contract (qdwin/qdwin.c).
 *
 * What it pins:
 *   - zwp_text_input_manager_v3 is advertised and binds;
 *   - get_text_input(seat) succeeds and the FULL request set is accepted
 *     without a protocol error (enable, set_surrounding_text,
 *     set_text_change_cause, set_content_type, set_cursor_rectangle, commit,
 *     disable, commit, destroy);
 *   - the FOUNDATION-ONLY contract: with no IME wired in, the compositor
 *     emits ZERO preedit_string / commit_string / delete_surrounding_text /
 *     done events, ever — even after enable+commit (so a bound toolkit sees
 *     focus but no composed text, which is the intended state);
 *   - enter/leave is FOCUS-driven: a text_input created while one of the
 *     client's surfaces holds the keyboard focus receives exactly one
 *     `enter` carrying that surface (and still no IME events).
 *
 * Modes:
 *   --lifecycle (default): no surface mapped, so the seat focuses nothing of
 *       ours. Assert zero enter AND zero IME/done events after the full
 *       request lifecycle. Fully headless, deterministic.
 *   --focus: map an xdg_toplevel (autofocused by qdwin headless), THEN
 *       get_text_input. Assert exactly one enter carrying OUR wl_surface, and
 *       still zero IME/done events.
 *   --stress: N rounds of map+autofocus, get_text_input, destroy the FOCUSED
 *       surface while the text_input is alive, then destroy the text_input.
 *       Exercises qdwin's entered-surface destroy listener + list cleanup
 *       under real Wayland dispatch; asserts no protocol error/disconnect and
 *       no IME events. Seat-gated (exit 5 headless), like --focus.
 *
 * Exit codes:
 *   0  expected postcondition held
 *   1  wrong event count/surface, or an unexpected IME/done event
 *   2  setup error (no display / missing manager global / buffer alloc)
 *   5  NO wl_seat is advertised — get_text_input needs a wl_seat, which only
 *      a seat-bearing backend provides. The weston headless backend has no
 *      input backend and exposes no seat, so the functional assertions are
 *      unreachable headless and reachable only under a backend WITH a seat
 *      (a VM, or weston's RDP/DRM/wayland backend). Same gate as the
 *      cursor-shape probe (tests/host/17-cursor-shape.md). The manager global
 *      itself IS verified before this gate.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <errno.h>

#include <wayland-client.h>
#include "xdg-shell-client-protocol.h"
#include "text-input-unstable-v3-client-protocol.h"

struct probe {
	struct wl_display *display;
	struct wl_compositor *compositor;
	struct wl_shm *shm;
	struct wl_seat *seat;
	struct xdg_wm_base *wm_base;
	struct zwp_text_input_manager_v3 *ti_mgr;
	int saw_comp, saw_shm, saw_seat, saw_wm, saw_mgr;

	struct wl_surface *surf;        /* our surface (focus mode) */
	struct wl_surface *entered;     /* surface from the last enter, or NULL */

	/* Event counters — the heart of the assertions. */
	int n_enter, n_leave;
	int n_preedit, n_commit_string, n_delete, n_done;
};

/* ---- zwp_text_input_v3 listener ---- */

static void
ti_enter(void *data, struct zwp_text_input_v3 *ti, struct wl_surface *surface)
{
	struct probe *p = data;
	(void)ti;
	p->n_enter++;
	p->entered = surface;
}
static void
ti_leave(void *data, struct zwp_text_input_v3 *ti, struct wl_surface *surface)
{
	struct probe *p = data;
	(void)ti; (void)surface;
	p->n_leave++;
	p->entered = NULL;
}
static void
ti_preedit_string(void *data, struct zwp_text_input_v3 *ti,
		  const char *text, int32_t cb, int32_t ce)
{
	struct probe *p = data;
	(void)ti; (void)text; (void)cb; (void)ce;
	p->n_preedit++;
}
static void
ti_commit_string(void *data, struct zwp_text_input_v3 *ti, const char *text)
{
	struct probe *p = data;
	(void)ti; (void)text;
	p->n_commit_string++;
}
static void
ti_delete_surrounding_text(void *data, struct zwp_text_input_v3 *ti,
			   uint32_t before, uint32_t after)
{
	struct probe *p = data;
	(void)ti; (void)before; (void)after;
	p->n_delete++;
}
static void
ti_done(void *data, struct zwp_text_input_v3 *ti, uint32_t serial)
{
	struct probe *p = data;
	(void)ti; (void)serial;
	p->n_done++;
}
static const struct zwp_text_input_v3_listener ti_listener = {
	.enter                   = ti_enter,
	.leave                   = ti_leave,
	.preedit_string          = ti_preedit_string,
	.commit_string           = ti_commit_string,
	.delete_surrounding_text = ti_delete_surrounding_text,
	.done                    = ti_done,
};

/* ---- xdg-shell plumbing (focus mode only) ---- */

static void
wm_base_ping(void *d, struct xdg_wm_base *b, uint32_t serial)
{ (void)d; xdg_wm_base_pong(b, serial); }
static const struct xdg_wm_base_listener wm_base_listener = {
	.ping = wm_base_ping,
};
static void
xs_configure(void *d, struct xdg_surface *xs, uint32_t serial)
{ (void)d; xdg_surface_ack_configure(xs, serial); }
static const struct xdg_surface_listener xs_listener = {
	.configure = xs_configure,
};
static void
tl_configure(void *d, struct xdg_toplevel *t, int32_t w, int32_t h,
	     struct wl_array *st)
{ (void)d; (void)t; (void)w; (void)h; (void)st; }
static void tl_close(void *d, struct xdg_toplevel *t) { (void)d; (void)t; }
static void tl_bounds(void *d, struct xdg_toplevel *t, int32_t w, int32_t h)
{ (void)d; (void)t; (void)w; (void)h; }
static void tl_caps(void *d, struct xdg_toplevel *t, struct wl_array *c)
{ (void)d; (void)t; (void)c; }
static const struct xdg_toplevel_listener tl_listener = {
	.configure = tl_configure, .close = tl_close,
	.configure_bounds = tl_bounds, .wm_capabilities = tl_caps,
};

static struct wl_buffer *
make_buffer(struct wl_shm *shm, int w, int h)
{
	int stride = w * 4, size = stride * h;
	int fd = memfd_create("qdwin-textinput-probe", MFD_CLOEXEC);
	if (fd < 0) return NULL;
	if (ftruncate(fd, size) < 0) { close(fd); return NULL; }
	uint32_t *px = mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
	if (px == MAP_FAILED) { close(fd); return NULL; }
	for (int i = 0; i < w * h; i++) px[i] = 0xff3060a0;
	struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, w, h, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	munmap(px, size);
	close(fd);
	return buf;
}

/* ---- registry ---- */

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *iface, uint32_t version)
{
	struct probe *p = data;
	if (!strcmp(iface, wl_compositor_interface.name)) {
		p->compositor = wl_registry_bind(reg, name,
			&wl_compositor_interface, version > 4 ? 4 : version);
		p->saw_comp = 1;
	} else if (!strcmp(iface, wl_shm_interface.name)) {
		p->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
		p->saw_shm = 1;
	} else if (!strcmp(iface, wl_seat_interface.name)) {
		p->seat = wl_registry_bind(reg, name, &wl_seat_interface,
					   version > 5 ? 5 : version);
		p->saw_seat = 1;
	} else if (!strcmp(iface, xdg_wm_base_interface.name)) {
		p->wm_base = wl_registry_bind(reg, name,
					      &xdg_wm_base_interface, 1);
		xdg_wm_base_add_listener(p->wm_base, &wm_base_listener, NULL);
		p->saw_wm = 1;
	} else if (!strcmp(iface, zwp_text_input_manager_v3_interface.name)) {
		p->ti_mgr = wl_registry_bind(reg, name,
			&zwp_text_input_manager_v3_interface, 1);
		p->saw_mgr = 1;
	}
}
static void on_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {
	.global = on_global, .global_remove = on_global_remove,
};

static int
no_ime_events(struct probe *p)
{
	return p->n_preedit == 0 && p->n_commit_string == 0 &&
	       p->n_delete == 0 && p->n_done == 0;
}

/* A mapped xdg_toplevel that qdwin autofocuses (first-buffer map). */
struct mapped {
	struct wl_surface *surf;
	struct xdg_surface *xs;
	struct xdg_toplevel *tl;
	struct wl_buffer *buf;
};

static int
map_focused_toplevel(struct probe *p, struct mapped *m)
{
	memset(m, 0, sizeof *m);
	m->surf = wl_compositor_create_surface(p->compositor);
	m->xs = xdg_wm_base_get_xdg_surface(p->wm_base, m->surf);
	xdg_surface_add_listener(m->xs, &xs_listener, NULL);
	m->tl = xdg_surface_get_toplevel(m->xs);
	xdg_toplevel_add_listener(m->tl, &tl_listener, NULL);
	xdg_toplevel_set_title(m->tl, "qdwin-textinput-probe");
	xdg_toplevel_set_app_id(m->tl, "qdwin-textinput-probe");
	wl_surface_commit(m->surf);
	wl_display_roundtrip(p->display);
	m->buf = make_buffer(p->shm, 64, 48);
	if (!m->buf)
		return -1;
	wl_surface_attach(m->surf, m->buf, 0, 0);
	wl_surface_damage_buffer(m->surf, 0, 0, 64, 48);
	wl_surface_commit(m->surf);
	/* Let qdwin map + autofocus before the caller asks for a text_input. */
	for (int i = 0; i < 4; i++)
		wl_display_roundtrip(p->display);
	return 0;
}

static void
destroy_mapped(struct mapped *m)
{
	if (m->tl)   xdg_toplevel_destroy(m->tl);
	if (m->xs)   xdg_surface_destroy(m->xs);
	if (m->surf) wl_surface_destroy(m->surf);
	if (m->buf)  wl_buffer_destroy(m->buf);
	memset(m, 0, sizeof *m);
}

enum mode { M_LIFECYCLE, M_FOCUS, M_STRESS };

int main(int argc, char *argv[])
{
	enum mode mode = M_LIFECYCLE;
	for (int i = 1; i < argc; i++) {
		if      (!strcmp(argv[i], "--lifecycle")) mode = M_LIFECYCLE;
		else if (!strcmp(argv[i], "--focus"))     mode = M_FOCUS;
		else if (!strcmp(argv[i], "--stress"))    mode = M_STRESS;
	}
	int focus_mode = (mode == M_FOCUS);

	struct probe p = {0};
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "textinput-probe: wl_display_connect: %s\n",
			strerror(errno));
		return 2;
	}
	struct wl_registry *reg = wl_display_get_registry(p.display);
	wl_registry_add_listener(reg, &registry_listener, &p);
	wl_display_roundtrip(p.display);

	if (!p.saw_mgr || !p.ti_mgr) {
		fprintf(stderr, "textinput-probe: zwp_text_input_manager_v3 "
				"not advertised\n");
		return 2;
	}
	if (!p.compositor) {
		fprintf(stderr, "textinput-probe: missing wl_compositor\n");
		return 2;
	}
	if (!p.seat) {
		/* Manager IS advertised (checked above) but get_text_input
		 * needs a wl_seat. Headless weston has none — gate, don't fail.
		 * Same convention as the cursor-shape probe. */
		fprintf(stderr, "textinput-probe: no wl_seat advertised — "
			"manager OK, functional path is seat/VM-only "
			"(exit 5)\n");
		return 5;
	}

	if (mode != M_LIFECYCLE && (!p.shm || !p.wm_base)) {
		fprintf(stderr, "textinput-probe: --focus/--stress need wl_shm + "
				"xdg_wm_base (shm=%p wm=%p)\n",
			(void*)p.shm, (void*)p.wm_base);
		return 2;
	}

	if (mode == M_STRESS) {
		/* Churn the listener/list cleanup under real Wayland dispatch:
		 * each round maps+autofocuses a toplevel, makes a text_input,
		 * destroys the FOCUSED surface while the text_input is still
		 * alive (exercises qdwin's entered-surface destroy listener —
		 * clear-without-leave), then destroys the text_input. A
		 * use-after-free or dangling-listener would surface as a
		 * protocol error or compositor disconnect. */
		const int ROUNDS = 5;
		int rc = 0;
		for (int r = 0; r < ROUNDS; r++) {
			struct mapped m;
			if (map_focused_toplevel(&p, &m) != 0) {
				fprintf(stderr, "textinput-probe: buffer alloc "
					"failed (round %d)\n", r);
				return 2;
			}
			struct zwp_text_input_v3 *sti =
				zwp_text_input_manager_v3_get_text_input(
					p.ti_mgr, p.seat);
			zwp_text_input_v3_add_listener(sti, &ti_listener, &p);
			wl_display_roundtrip(p.display);
			destroy_mapped(&m);              /* focused surface dies */
			wl_display_roundtrip(p.display);
			zwp_text_input_v3_destroy(sti);  /* then the text_input */
			wl_display_roundtrip(p.display);
			if (wl_display_get_error(p.display) != 0) {
				fprintf(stderr, "textinput-probe: FAIL stress — "
					"protocol error/disconnect after round "
					"%d\n", r);
				rc = 1;
				break;
			}
		}
		if (rc == 0 && !no_ime_events(&p)) {
			fprintf(stderr, "textinput-probe: FAIL stress — "
				"unexpected IME event across rounds "
				"(preedit=%d commit_string=%d done=%d)\n",
				p.n_preedit, p.n_commit_string, p.n_done);
			rc = 1;
		}
		if (rc == 0)
			fprintf(stderr, "textinput-probe: OK stress — %d rounds "
				"map/focus/destroy-surface/destroy-text_input, "
				"no protocol error, no IME events\n", ROUNDS);
		wl_display_disconnect(p.display);
		return rc;
	}

	struct mapped fm;
	if (mode == M_FOCUS) {
		if (map_focused_toplevel(&p, &fm) != 0) {
			fprintf(stderr, "textinput-probe: buffer alloc failed\n");
			return 2;
		}
		p.surf = fm.surf;
	}

	struct zwp_text_input_v3 *ti =
		zwp_text_input_manager_v3_get_text_input(p.ti_mgr, p.seat);
	zwp_text_input_v3_add_listener(ti, &ti_listener, &p);
	/* get_text_input itself triggers the server's focus recompute; round
	 * trip so any immediate `enter` lands before we drive requests. */
	wl_display_roundtrip(p.display);

	/* Drive the full request set. None of this should elicit an IME event
	 * (there is no input-method) nor a `done`. */
	zwp_text_input_v3_enable(ti);
	zwp_text_input_v3_set_surrounding_text(ti, "hello", 5, 5);
	zwp_text_input_v3_set_text_change_cause(ti,
		ZWP_TEXT_INPUT_V3_CHANGE_CAUSE_INPUT_METHOD);
	zwp_text_input_v3_set_content_type(ti,
		ZWP_TEXT_INPUT_V3_CONTENT_HINT_NONE,
		ZWP_TEXT_INPUT_V3_CONTENT_PURPOSE_NORMAL);
	zwp_text_input_v3_set_cursor_rectangle(ti, 0, 0, 10, 16);
	zwp_text_input_v3_commit(ti);
	zwp_text_input_v3_disable(ti);
	zwp_text_input_v3_commit(ti);
	wl_display_roundtrip(p.display);

	int rc = 0;

	if (!no_ime_events(&p)) {
		fprintf(stderr, "textinput-probe: FAIL inert contract — "
			"preedit=%d commit_string=%d delete=%d done=%d "
			"(all must be 0; no IME is wired in)\n",
			p.n_preedit, p.n_commit_string, p.n_delete,
			p.n_done);
		rc = 1;
	}

	if (focus_mode) {
		if (p.n_enter != 1) {
			fprintf(stderr, "textinput-probe: FAIL focus — enter=%d "
				"want 1 (text_input on the focused surface must "
				"receive exactly one enter)\n", p.n_enter);
			rc = 1;
		} else if (p.entered != p.surf) {
			fprintf(stderr, "textinput-probe: FAIL focus — enter "
				"carried surface=%p, want our surface=%p\n",
				(void*)p.entered, (void*)p.surf);
			rc = 1;
		} else {
			fprintf(stderr, "textinput-probe: OK focus — one enter "
				"on our surface, no IME events\n");
		}
	} else {
		if (p.n_enter != 0) {
			fprintf(stderr, "textinput-probe: FAIL lifecycle — "
				"enter=%d want 0 (no surface of ours is "
				"focused)\n", p.n_enter);
			rc = 1;
		} else {
			fprintf(stderr, "textinput-probe: OK lifecycle — full "
				"request set accepted, no enter, no IME "
				"events\n");
		}
	}

	zwp_text_input_v3_destroy(ti);
	wl_display_roundtrip(p.display);
	wl_display_disconnect(p.display);
	return rc;
}
