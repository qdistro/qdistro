#!/usr/bin/env python3
"""Behavioural test for nested-proxy destroy teardown (iso2/10 E2).

The source invariant next door pins the *shape*; this pins what actually
matters: no server-owned pointer to the toplevel survives free(tl).

Same technique as test_idle_inhibit_behaviour.py — splice the REAL function
bodies out of qdwin.c and the vendored libweston, compile them against
libwayland lists with reduced structs, and assert on the resulting state
with the freed memory poisoned (ASan when available).

The path that matters is synchronous and easy to miss by reading: tearing a
dependent down ends a pointer grab, weston_pointer_end_grab() reinstalls the
default grab and calls its focus() handler *immediately*, and qdwin's default
grab focus re-picks a view and caches the proxy it belongs to in
qdwin->active_input_proxy. With the dying proxy's curtain still mapped and
still on qdwin->toplevels, that hands the cache straight back to the
toplevel we are about to free (found by the codex round-1 review).

Cases:
  1. Fixture validity: during destroy the picker really is offered the dying
     curtain and the default-grab focus handler really runs.
  2. After destroy, active_input_proxy does not point at the freed toplevel.
  3. A different, live proxy selected during teardown is preserved.
  4. Every view_stream sourced from the proxy is terminated exactly once,
     with s->tl NULLed and the stream off the active list; a stream on
     another toplevel is untouched.
  5. Unpin runs before the curtain view is destroyed.
  6. The chrome popup is dismissed and torn down, and p->parent does not
     outlive the toplevel.
  7. The move-drag on that handle is ended.
  8. A pending proxy with no curtain (tl->view == NULL) destroys cleanly.
  9. qdwin_toplevel_release_dependents is idempotent on a live toplevel.
 10. The popup's client-owned resource and surface outlive the toplevel: the
     resource destructor and the surface destroy_signal must not reach the
     freed popup (codex round 2 found both assertions vacuous before this).
 11. Each dependent alone — move only, popup only, stream confinement only —
     so one path's grab teardown cannot mask another's absence.
 12. Stream confinement: the real seat release ends the confine grab on the
     stream's own pointer (freed at seat release, so a teardown ordered after
     it is a real use-after-free here), and leaves a pointer whose grab was
     already replaced alone.
 13. The full scenario destroys the proxy the production way — by destroying
     the advertiser's wl_resource — not by calling the destroy function.
"""

from pathlib import Path
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def fail(message):
    print(f"FAIL: {message}")
    return 1


def _load(name, mod_name):
    spec = importlib.util.spec_from_file_location(
        mod_name, Path(__file__).with_name(name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_beh = _load("test_idle_inhibit_behaviour.py", "beh")
_strip_comments = _load("test_idle_inhibit_visibility.py", "inv")._strip_comments


def extract(source, name):
    """Return the DEFINITION of `name`, comments stripped.

    qdwin.c puts the return type on its own line, so a definition is the only
    place the name starts a line. Matching the first `name(` anywhere (as the
    idle-inhibit probe does) would grab a forward declaration or a call site
    here, because these functions are declared long before they are defined.
    """
    code = _strip_comments(source)
    m = re.search(r"^" + re.escape(name) + r"\s*\(", code, re.MULTILINE)
    if not m:
        raise KeyError(name)
    begin = code.index("{", m.start())
    depth, end = 1, begin + 1
    while depth:
        depth += (code[end] == "{") - (code[end] == "}")
        end += 1
    return code[m.start():end]


PROLOGUE = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>
#include <wayland-server-core.h>

#define weston_log(...) ((void)0)
#define QDWIN_SIDES 4

struct weston_view;
struct weston_output;
struct weston_pointer;
struct weston_pointer_grab;
struct qdwin;
struct qdwin_toplevel;

/* The advertiser's per-toplevel object: destroying its resource is the real
 * way a nested proxy dies (nested compositor crash or unpublish). */
struct qdwin_nested_toplevel {
	struct wl_resource *resource;
	struct qdwin_toplevel *proxy_tl;
	char *app_id, *title, *pw_node, *input_sink;
	struct wl_list link;
};

struct weston_pointer_grab_interface {
	void (*focus)(struct weston_pointer_grab *grab);
};
struct weston_pointer_grab {
	const struct weston_pointer_grab_interface *interface;
	struct weston_pointer *pointer;
};
struct weston_seat {
	struct weston_compositor *compositor;
	/* Set for a per-stream seat so the release shims can enforce the
	 * lifetime boundary weston really has (codex round 3). */
	struct qdwin_view_stream *owner;
	struct weston_pointer *pointer_state;
	int released;
};
struct weston_coord_global { struct { double x, y; } c; };
struct weston_pointer {
	struct weston_pointer_grab *grab;
	struct weston_pointer_grab default_grab;
	struct weston_view *focus;
	struct weston_seat *seat;
	struct weston_coord_global pos;
};
struct weston_compositor { int placeholder; };

/* The one view field the spliced code needs. */
struct weston_view { int destroyed; struct weston_output *output; };

/* The popup's surface is client-owned and outlives the toplevel; its
 * destroy_signal is a REAL wl_signal, so a listener left embedded in freed
 * memory is a real use-after-free here. */
struct weston_surface { struct wl_signal destroy_signal; };

struct qdwin_chrome {
	struct weston_view *view;
	struct qdwin_toplevel *tl;
};

struct qdwin_popup {
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct weston_view *view;
	struct wl_listener surface_destroy;
	struct qdwin_toplevel *parent;
	int x, y;
	struct weston_pointer_grab grab;
	int grab_active;
};

struct qdwin_toplevel {
	struct qdwin *qdwin;
	uint32_t handle;
	int is_nested_proxy;
	bool proxy_destroying;
	bool nested_proxy_pending_decision;
	struct weston_view *view;
	struct qdwin_chrome chrome[QDWIN_SIDES];
	struct qdwin_popup *popup;
	void *proxy_curtain;
	struct weston_surface *proxy_pixel_surface;
	struct weston_view *proxy_pixel_view;
	struct wl_listener proxy_pixel_destroy_listener;
	struct qdwin_nested_toplevel *proxy_nested_owner;
	int proxy_input_sink_fd;
	char *proxy_app_id, *proxy_title;
	char *proxy_secctx_engine, *proxy_secctx_app_id, *proxy_secctx_instance;
	char *proxy_remote_source_machine, *proxy_remote_trust_domain_id;
	char *proxy_remote_stream_id;
	struct wl_list link;
};

struct qdwin_view_stream {
	struct wl_resource *resource;
	struct wl_resource *input_handle;
	struct qdwin *qdwin;
	struct qdwin_toplevel *tl;
	uint32_t toplevel_handle;
	struct weston_output *pw_output, *prev_output;
	uint32_t rdp_port;
	pid_t forward_pid;
	int torn_down_sent, server_state_released, listed, pinned;
	int input_claimed, allow_input;
	int seat_inited, confine_grab_active;
	struct weston_seat stream_seat;
	struct weston_pointer_grab confine_grab;
	char access_token[33], rdp_password[17];
	struct wl_list link;
};

struct qdwin {
	struct weston_compositor *compositor;
	struct wl_list toplevels;
	struct wl_list view_streams;
	struct qdwin_toplevel *active_input_proxy;
	int locked;
	struct weston_view *lock_view;
	int move_grab_active;
	uint32_t move_grab_handle;
	struct weston_pointer_grab move_grab;
};

static struct qdwin *qdwin_singleton;

/* ---- harness state -------------------------------------------------- */
static struct weston_view *pick_result;      /* what the picker returns */
static int pick_calls, focus_handler_calls;
static int dismissed_sent, torn_down_sent_total;
static int unpin_calls, seat_release_calls, reap_calls;
static int curtain_destroyed, view_destroy_calls;
static int unpin_after_curtain_destroy;      /* ordering violation counter */
static int sink_focus_writes;

/* A wl_resource model that honours the real destructor contract: destroying
 * a resource runs its destructor, which reads the resource's user data. A
 * teardown that forgets to clear that user data therefore reaches freed
 * memory here exactly as it would in production (codex round 2). */
struct wl_resource {
	void *user_data;
	void (*destructor)(struct wl_resource *);
	int destroyed;
};

static void qd_res_set_user_data(struct wl_resource *r, void *d)
{ if (r) r->user_data = d; }
static void *qd_res_get_user_data(struct wl_resource *r)
{ return r ? r->user_data : NULL; }
static void qd_res_destroy(struct wl_resource *r)
{
	if (!r || r->destroyed)
		return;
	r->destroyed = 1;
	if (r->destructor)
		r->destructor(r);
}
#define wl_resource_set_user_data qd_res_set_user_data
#define wl_resource_get_user_data qd_res_get_user_data
#define wl_resource_destroy qd_res_destroy

static void qdwin_popup_v1_send_dismissed(struct wl_resource *r)
{ (void)r; dismissed_sent++; }
static void qdwin_view_stream_v1_send_torn_down(struct wl_resource *r,
						const char *reason)
{ (void)r; (void)reason; torn_down_sent_total++; }
static void qdwin_send_toplevel_removed(struct qdwin *q,
					struct qdwin_toplevel *tl)
{ (void)q; (void)tl; }
static void qdwin_chrome_detach(struct qdwin_chrome *c) { c->view = NULL; }
static void weston_shell_utils_curtain_destroy(void *c)
{ (void)c; curtain_destroyed = 1; }
static void weston_view_destroy(struct weston_view *v)
{ if (v) v->destroyed = 1; view_destroy_calls++; }
static void qdwin_view_stream_reap_forward(struct qdwin_view_stream *s)
{ (void)s; reap_calls++; }
/* The real unpin touches tl->view and tl->chrome[] views; model only what
 * this test is asserting about it — that it runs while they are alive. */
static void qdwin_view_stream_unpin(struct qdwin_view_stream *s)
{
	if (!s->pinned)
		return;
	s->pinned = 0;
	unpin_calls++;
	if (curtain_destroyed || (s->tl && s->tl->view && s->tl->view->destroyed))
		unpin_after_curtain_destroy++;
}
static int seat_release_touch, seat_release_kbd, seat_release_ptr, seat_gone;
/* Ordering violations: a device or seat release observed while the stream's
 * confinement grab was still installed. */
static int seat_order_violations;
/* State captured at seat release, i.e. at the moment weston frees the
 * pointer. Read these instead of the pointer itself — after release the
 * pointer really is gone. */
static void *grab_at_release;
static int focus_calls_at_release;

static void seat_order_check(struct weston_seat *s)
{
	if (s->owner && s->owner->confine_grab_active)
		seat_order_violations++;
}
static void weston_seat_release_touch(struct weston_seat *s)
{ seat_order_check(s); seat_release_touch++; }
static void weston_seat_release_keyboard(struct weston_seat *s)
{ seat_order_check(s); seat_release_kbd++; }
/* weston_seat_release_pointer() cancels the grab but deliberately RETAINS
 * the pointer storage. */
static void weston_seat_release_pointer(struct weston_seat *s)
{ seat_order_check(s); seat_release_ptr++; }
/* weston_seat_release() → weston_pointer_destroy() FREES the pointer
 * (libweston input.c). Model that literally, so anything that reads the
 * pointer afterwards — e.g. a confinement teardown ordered after seat
 * release — is a use-after-free ASan can see. */
static void weston_seat_release(struct weston_seat *s)
{
	seat_order_check(s);
	seat_gone++;
	if (s->pointer_state) {
		grab_at_release = s->pointer_state->grab;
		focus_calls_at_release = focus_handler_calls;
		free(s->pointer_state);
		s->pointer_state = NULL;
	}
	s->released = 1;
}
static void qdwin_nested_input_sink_send_focus(int fd, int on)
{ (void)fd; (void)on; sink_focus_writes++; }
static void weston_pointer_set_focus(struct weston_pointer *p,
				     struct weston_view *v)
{ p->focus = v; }
static struct weston_view *
weston_compositor_pick_view(struct weston_compositor *c,
			    struct weston_coord_global pos)
{ (void)c; (void)pos; pick_calls++; return pick_result; }
"""

EPILOGUE = r"""
#define CHECK(cond, ...) do { \
	if (!(cond)) { printf("FAIL: " __VA_ARGS__); printf("\n"); return 1; } \
} while (0)

/* The real default-grab focus body, with a call counter around it so the
 * fixture can prove the synchronous path was exercised. */
static void harness_default_grab_focus(struct weston_pointer_grab *grab)
{
	focus_handler_calls++;
	qdwin_proxy_default_grab_focus(grab);
}

static const struct weston_pointer_grab_interface default_grab_iface = {
	.focus = harness_default_grab_focus,
};

/* A grab that is NOT ours, to pin the confine-grab ownership guard. */
static int foreign_focus_calls;
static void foreign_focus(struct weston_pointer_grab *grab)
{ (void)grab; foreign_focus_calls++; }
static const struct weston_pointer_grab_interface foreign_grab_iface = {
	.focus = foreign_focus,
};

static struct qdwin q;
static struct wl_list nested_toplevels;
static struct weston_compositor comp;
static struct weston_seat main_seat;

static void init_pointer(struct weston_pointer *p)
{
	memset(p, 0, sizeof *p);
	p->seat = &main_seat;
	p->default_grab.interface = &default_grab_iface;
	p->default_grab.pointer = p;
	p->grab = &p->default_grab;
}

static struct qdwin_toplevel *
make_proxy(uint32_t handle, struct weston_view *view)
{
	struct qdwin_toplevel *tl = calloc(1, sizeof *tl);

	assert(tl);
	tl->qdwin = &q;
	tl->handle = handle;
	tl->is_nested_proxy = 1;
	tl->view = view;
	tl->proxy_curtain = view ? (void *)0x1 : NULL;
	tl->proxy_input_sink_fd = -1;
	wl_list_init(&tl->proxy_pixel_destroy_listener.link);
	wl_list_insert(&q.toplevels, &tl->link);
	return tl;
}

/* A stream owning heap resources, wired the way subscribe_view_stream wires
 * them: each resource carries its owner and its real destructor. */
static struct qdwin_view_stream *
make_stream(struct qdwin_toplevel *tl)
{
	struct qdwin_view_stream *s = calloc(1, sizeof *s);
	struct wl_resource *sr = calloc(1, sizeof *sr);
	struct wl_resource *ir = calloc(1, sizeof *ir);

	assert(s && sr && ir);
	s->qdwin = &q;
	s->tl = tl;
	s->toplevel_handle = tl->handle;
	s->resource = sr;
	sr->user_data = s;
	sr->destructor = qdwin_stream_resource_destroyed;
	s->input_handle = ir;
	ir->user_data = s;
	ir->destructor = qdwin_stream_input_handle_resource_destroyed;
	s->pinned = 1;
	s->listed = 1;
	memset(s->access_token, 'a', sizeof s->access_token - 1);
	memset(s->rdp_password, 'p', sizeof s->rdp_password - 1);
	wl_list_insert(&q.view_streams, &s->link);
	return s;
}

/* Give the stream its own seat + pointer with the confinement grab actually
 * installed, as qdwin_stream_seat_init/confine_grab_start leave it. The
 * pointer belongs to the STREAM's seat (not the main seat) and lives on the
 * heap, because weston frees it when that seat is released. */
static struct weston_pointer *confine(struct qdwin_view_stream *s)
{
	struct weston_pointer *p = calloc(1, sizeof *p);

	assert(p);
	init_pointer(p);
	p->seat = &s->stream_seat;
	s->stream_seat.compositor = &comp;
	s->stream_seat.owner = s;
	s->stream_seat.pointer_state = p;
	s->seat_inited = 1;
	s->confine_grab.interface = &default_grab_iface;
	s->confine_grab.pointer = p;
	s->confine_grab_active = 1;
	p->grab = &s->confine_grab;
	return p;
}

/* A client-owned popup surface + resource, as show_popup leaves them. */
static struct qdwin_popup *
make_popup(struct qdwin_toplevel *tl, struct weston_surface *surf,
	   struct weston_pointer *ptr, struct wl_resource **out_res)
{
	struct qdwin_popup *p = calloc(1, sizeof *p);
	struct wl_resource *r = calloc(1, sizeof *r);

	assert(p && r);
	p->parent = tl;
	tl->popup = p;
	p->resource = r;
	r->user_data = p;
	r->destructor = qdwin_popup_resource_destroyed;
	p->surface = surf;
	p->surface_destroy.notify = qdwin_popup_surface_destroyed;
	wl_signal_add(&surf->destroy_signal, &p->surface_destroy);
	if (ptr) {
		p->grab.pointer = ptr;
		p->grab_active = 1;
		ptr->grab = &p->grab;
	}
	*out_res = r;
	return p;
}

/* ------------------------------------------------------------------ */

static int scenario_full(void)
{
	struct weston_view curtain = { 0 }, other_curtain = { 0 };
	struct weston_pointer pointer;
	struct weston_surface popup_surface;
	struct wl_resource *popup_res;
	struct qdwin_view_stream *s1, *s2, *survivor_stream;
	struct qdwin_toplevel *tl, *survivor;
	void *freed;

	init_pointer(&pointer);
	wl_signal_init(&popup_surface.destroy_signal);

	survivor = make_proxy(2, &other_curtain);
	tl = make_proxy(1, &curtain);

	s1 = make_stream(tl);
	s2 = make_stream(tl);
	survivor_stream = make_stream(survivor);
	confine(s1);

	make_popup(tl, &popup_surface, NULL, &popup_res);

	q.move_grab_active = 1;
	q.move_grab_handle = tl->handle;
	q.move_grab.pointer = &pointer;

	/* The pointer sits over the dying proxy's curtain, which is still
	 * mapped and still on q.toplevels — exactly the state in which
	 * ending a grab hands the input cache back. */
	pointer.focus = &curtain;
	pick_result = &curtain;
	q.active_input_proxy = tl;

	/* Kill the proxy the way production does: the advertiser's wl_resource
	 * goes away (nested compositor crash or unpublish). */
	{
		struct qdwin_nested_toplevel *nt = calloc(1, sizeof *nt);
		struct wl_resource *adv = calloc(1, sizeof *adv);

		assert(nt && adv);
		nt->resource = adv;
		nt->proxy_tl = tl;
		tl->proxy_nested_owner = nt;
		wl_list_insert(&nested_toplevels, &nt->link);
		adv->user_data = nt;
		adv->destructor = qdwin_nested_toplevel_resource_destroy;
		wl_resource_destroy(adv);
		free(adv);
	}
	freed = tl;

	/* 1. fixture validity: the synchronous focus path really ran and the
	 *    picker really was offered the dying curtain. */
	CHECK(focus_handler_calls > 0,
	      "no grab teardown reached the default-grab focus handler — the "
	      "fixture never exercised the failure path");
	CHECK(pick_calls > 0 && pick_result == &curtain,
	      "the picker was never offered the dying curtain");

	/* 2. the cache must not point at the freed toplevel. */
	CHECK(q.active_input_proxy != (struct qdwin_toplevel *)freed,
	      "active_input_proxy still points at the freed proxy");

	/* 3./4. streams: both of the proxy's streams inert exactly once, off
	 *    the list, s->tl cleared; the survivor's stream untouched. */
	CHECK(s1->server_state_released && s2->server_state_released,
	      "a view_stream of the destroyed proxy was not released");
	CHECK(torn_down_sent_total == 2,
	      "expected exactly 2 torn_down events, got %d",
	      torn_down_sent_total);
	CHECK(s1->tl == NULL && s2->tl == NULL,
	      "a released view_stream still points at the freed toplevel");
	CHECK(!s1->listed && !s2->listed,
	      "a released view_stream is still listed");
	CHECK(s1->input_handle == NULL && s2->input_handle == NULL,
	      "the stream input handle was not revoked");
	CHECK(!s1->resource->destroyed,
	      "the client-owned stream resource must survive as a tombstone");
	CHECK(survivor_stream->tl == survivor && survivor_stream->listed &&
	      !survivor_stream->server_state_released,
	      "an unrelated toplevel's view_stream was torn down too");
	CHECK(s1->access_token[0] == 0 && s1->rdp_password[0] == 0,
	      "a released stream kept its credentials");

	/* 5. unpin must have run while the curtain view was alive. */
	CHECK(unpin_calls == 2, "expected 2 unpins, got %d", unpin_calls);
	CHECK(unpin_after_curtain_destroy == 0,
	      "a stream was unpinned after its source view was destroyed");

	/* 6. confinement ended before the seat devices were released, and the
	 *    stream pointer is back on its default grab. */
	CHECK(!s1->confine_grab_active, "stream confinement is still active");
	CHECK(seat_order_violations == 0,
	      "a seat/device release ran while confinement was still "
	      "installed — the grab would be ended through a freed pointer");
	CHECK(grab_at_release != (void *)&s1->confine_grab,
	      "the confine grab was still installed when the pointer died");
	CHECK(focus_calls_at_release > 0,
	      "the confinement's default-focus callback never ran while the "
	      "pointer was alive");
	CHECK(seat_gone == 1 && seat_release_ptr == 1,
	      "the stream seat was not released exactly once (seat=%d ptr=%d)",
	      seat_gone, seat_release_ptr);

	/* 7. the move-drag on that handle is over. */
	CHECK(q.move_grab_active == 0,
	      "the move-drag on the destroyed handle is still active");

	/* 8. the popup was dismissed and unhooked from its client-owned
	 *    surface and resource. Both outlive the toplevel, so destroying
	 *    them now must not reach the freed popup. */
	CHECK(dismissed_sent == 1, "the chrome popup was not dismissed");
	CHECK(wl_list_empty(&popup_surface.destroy_signal.listener_list),
	      "the popup left a listener on its client-owned surface");
	wl_resource_destroy(popup_res);          /* destructor must no-op */
	wl_signal_emit(&popup_surface.destroy_signal, &popup_surface);
	CHECK(dismissed_sent == 1,
	      "a post-destroy client event re-entered the freed popup");

	/* 9. the client destroys its stream tombstones afterwards. */
	wl_resource_destroy(s1->resource);
	wl_resource_destroy(s2->resource);
	CHECK(torn_down_sent_total == 2,
	      "destroying the tombstone re-sent a terminal event");
	free(popup_res);
	return 0;
}

/* Each dependent ALONE: one path's grab teardown must not stand in for
 * another's (codex round 2). */
static int scenario_move_only(void)
{
	struct weston_view curtain = { 0 };
	struct weston_pointer pointer;
	struct qdwin_toplevel *tl;
	int before = focus_handler_calls;

	init_pointer(&pointer);
	tl = make_proxy(10, &curtain);
	q.move_grab_active = 1;
	q.move_grab_handle = tl->handle;
	q.move_grab.pointer = &pointer;
	pointer.focus = &curtain;
	pick_result = &curtain;
	q.active_input_proxy = tl;

	qdwin_nested_proxy_destroy(tl);

	CHECK(focus_handler_calls == before + 1,
	      "move-only teardown did not run the default-grab focus handler");
	CHECK(q.active_input_proxy != tl,
	      "move-only teardown left the freed proxy in the input cache");
	CHECK(!q.move_grab_active, "move-only teardown left the drag active");
	return 0;
}

static int scenario_popup_only(void)
{
	struct weston_view curtain = { 0 };
	struct weston_pointer pointer;
	struct weston_surface surf;
	struct wl_resource *res;
	struct qdwin_toplevel *tl;
	int before = focus_handler_calls, dismissed = dismissed_sent;

	init_pointer(&pointer);
	wl_signal_init(&surf.destroy_signal);
	tl = make_proxy(11, &curtain);
	make_popup(tl, &surf, &pointer, &res);
	pointer.focus = &curtain;
	pick_result = &curtain;
	q.active_input_proxy = tl;

	qdwin_nested_proxy_destroy(tl);

	CHECK(focus_handler_calls == before + 1,
	      "popup-only teardown did not end the popup's pointer grab");
	CHECK(dismissed_sent == dismissed + 1, "the popup was not dismissed");
	CHECK(q.active_input_proxy != tl,
	      "popup-only teardown left the freed proxy in the input cache");
	CHECK(wl_list_empty(&surf.destroy_signal.listener_list),
	      "the popup left a listener on its client-owned surface");
	wl_resource_destroy(res);
	wl_signal_emit(&surf.destroy_signal, &surf);
	free(res);
	return 0;
}

static int scenario_confine_only(void)
{
	struct weston_view curtain = { 0 };
	struct weston_pointer *stream_pointer;
	struct qdwin_view_stream *s;
	struct qdwin_toplevel *tl;
	int before = focus_handler_calls, violations = seat_order_violations;

	tl = make_proxy(12, &curtain);
	s = make_stream(tl);
	stream_pointer = confine(s);
	stream_pointer->focus = &curtain;
	pick_result = &curtain;
	q.active_input_proxy = tl;

	qdwin_nested_proxy_destroy(tl);

	CHECK(focus_handler_calls == before + 1,
	      "confine-only teardown did not end the confinement grab");
	CHECK(!s->confine_grab_active, "confinement survived the source");
	CHECK(seat_order_violations == violations,
	      "confinement outlived a device/seat release");
	CHECK(grab_at_release != (void *)&s->confine_grab,
	      "the confine grab was still installed when the pointer died");
	CHECK(q.active_input_proxy != tl,
	      "confine-only teardown left the freed proxy in the input cache");
	wl_resource_destroy(s->resource);
	return 0;
}

/* The ownership guard: a confine grab that was already replaced must not be
 * ended, or teardown would uninstall somebody else's grab. */
static int scenario_confine_grab_replaced(void)
{
	struct weston_view curtain = { 0 };
	struct weston_pointer *stream_pointer;
	struct weston_pointer_grab foreign = { .interface = &foreign_grab_iface };
	struct qdwin_view_stream *s;
	struct qdwin_toplevel *tl;
	int before = focus_handler_calls;

	tl = make_proxy(13, &curtain);
	s = make_stream(tl);
	stream_pointer = confine(s);
	foreign.pointer = stream_pointer;
	stream_pointer->grab = &foreign;   /* somebody else grabbed meanwhile */
	pick_result = NULL;

	qdwin_nested_proxy_destroy(tl);

	/* The foreign grab must still have been installed when the pointer
	 * died: teardown must not uninstall a grab it does not own. (Read the
	 * value captured at release — the pointer itself is gone.) */
	CHECK(grab_at_release == (void *)&foreign,
	      "teardown uninstalled a grab it did not own");
	CHECK(focus_handler_calls == before,
	      "teardown ran the default focus handler on a foreign grab");
	CHECK(!s->confine_grab_active,
	      "the stale confinement flag was not cleared");
	wl_resource_destroy(s->resource);
	return 0;
}

/* The final clear is CONDITIONAL: a different, live proxy selected by the
 * grab-focus callback during teardown must survive it. */
static int scenario_live_proxy_preserved(void)
{
	struct weston_view curtain = { 0 }, live_curtain = { 0 };
	struct weston_pointer pointer;
	struct weston_surface surf;
	struct wl_resource *res;
	struct qdwin_toplevel *tl, *live;

	init_pointer(&pointer);
	wl_signal_init(&surf.destroy_signal);
	live = make_proxy(20, &live_curtain);
	tl = make_proxy(21, &curtain);
	make_popup(tl, &surf, &pointer, &res);
	q.active_input_proxy = tl;
	/* By the time the grab ends the pointer has moved onto the other
	 * proxy's curtain. */
	pointer.focus = &live_curtain;
	pick_result = &live_curtain;

	qdwin_nested_proxy_destroy(tl);

	CHECK(q.active_input_proxy == live,
	      "a live proxy selected during teardown was discarded");
	wl_resource_destroy(res);
	free(res);
	return 0;
}

static int scenario_pending_and_idempotence(void)
{
	struct weston_view other_curtain = { 0 };
	struct weston_surface stash;
	struct qdwin_toplevel *pending, *live;
	int before;

	/* A pending proxy: no curtain, no view, a stashed pixel surface whose
	 * destroy listener is really linked and must be unhooked. */
	wl_signal_init(&stash.destroy_signal);
	pending = make_proxy(14, NULL);
	pending->nested_proxy_pending_decision = true;
	pending->proxy_pixel_surface = &stash;
	pending->proxy_pixel_destroy_listener.notify = NULL;
	wl_signal_add(&stash.destroy_signal,
		      &pending->proxy_pixel_destroy_listener);
	qdwin_nested_proxy_destroy(pending);
	CHECK(wl_list_empty(&stash.destroy_signal.listener_list),
	      "a pending proxy left its pixel listener on the client surface");

	/* The shared helper is idempotent after a real teardown. */
	live = make_proxy(15, &other_curtain);
	{
		struct qdwin_view_stream *s = make_stream(live);
		struct weston_surface surf;
		struct wl_resource *res;

		wl_signal_init(&surf.destroy_signal);
		make_popup(live, &surf, NULL, &res);
		qdwin_toplevel_release_dependents(&q, live);
		before = torn_down_sent_total;
		qdwin_toplevel_release_dependents(&q, live);
		CHECK(torn_down_sent_total == before,
		      "a second release re-emitted a terminal event");
		CHECK(live->popup == NULL,
		      "the popup back-pointer survived release");
		wl_resource_destroy(s->resource);
		wl_resource_destroy(res);
		free(res);
	}
	return 0;
}

int main(void)
{
	int rc;

	qdwin_singleton = &q;
	q.compositor = &comp;
	main_seat.compositor = &comp;
	wl_list_init(&q.toplevels);
	wl_list_init(&q.view_streams);
	wl_list_init(&nested_toplevels);

	if ((rc = scenario_full()))
		return rc;
	if ((rc = scenario_move_only()))
		return rc;
	if ((rc = scenario_popup_only()))
		return rc;
	if ((rc = scenario_confine_only()))
		return rc;
	if ((rc = scenario_confine_grab_replaced()))
		return rc;
	if ((rc = scenario_live_proxy_preserved()))
		return rc;
	if ((rc = scenario_pending_and_idempotence()))
		return rc;

	printf("ok: nested-proxy destroy teardown (13 cases)\n");
	return 0;
}
"""

QDWIN_FNS = [
    ("struct qdwin_toplevel *", "qdwin_proxy_for_view"),
    ("void", "qdwin_proxy_pointer_track_focus"),
    ("void", "qdwin_proxy_default_grab_focus"),
    ("void", "qdwin_move_grab_end_for"),
    ("void", "qdwin_popup_teardown"),
    ("void", "qdwin_popup_resource_destroyed"),
    ("void", "qdwin_popup_surface_destroyed"),
    ("void", "qdwin_stream_confine_grab_end"),
    ("void", "qdwin_stream_seat_release"),
    ("void", "qdwin_stream_input_handle_resource_destroyed"),
    ("void", "qdwin_view_stream_release_server_state"),
    ("void", "qdwin_view_stream_terminate"),
    ("void", "qdwin_stream_resource_destroyed"),
    ("void", "qdwin_toplevel_release_dependents"),
    ("void", "qdwin_nested_proxy_destroy"),
    ("void", "qdwin_nested_toplevel_resource_destroy"),
]


def main():
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        print("no C compiler; skipping")
        return 77
    if subprocess.run(["pkg-config", "--exists", "wayland-server"]).returncode:
        print("wayland-server not available; skipping")
        return 77

    qdwin_c = (ROOT / "qdwin" / "qdwin.c").read_text(encoding="utf-8")
    input_c = (ROOT / "libweston-vendored" / "src" / "libweston" /
               "input.c").read_text(encoding="utf-8")

    parts = [PROLOGUE]
    # Forward declarations: the spliced bodies call each other in both
    # directions (popup teardown ends a grab, the grab's focus handler tracks
    # the proxy cache).
    parts.append("\n".join(f"static {ret} {name}();" if False else ""
                           for ret, name in QDWIN_FNS))
    parts.append("static void qdwin_proxy_default_grab_focus("
                 "struct weston_pointer_grab *grab);")
    parts.append("static void weston_pointer_end_grab("
                 "struct weston_pointer *pointer);")
    parts.append("static void qdwin_toplevel_release_dependents("
                 "struct qdwin *qdwin, struct qdwin_toplevel *tl);")
    parts.append("static void qdwin_proxy_pointer_track_focus("
                 "struct qdwin *qdwin, struct weston_pointer *pointer);")
    parts.append("static void qdwin_view_stream_terminate("
                 "struct qdwin_view_stream *s, const char *reason, "
                 "pid_t audit_pid);")
    parts.append("static void qdwin_popup_teardown(struct qdwin_popup *p);")
    parts.append("static void qdwin_stream_confine_grab_end("
                 "struct qdwin_view_stream *s);")
    parts.append("static void qdwin_view_stream_release_server_state("
                 "struct qdwin_view_stream *s);")
    for ret, name in QDWIN_FNS:
        parts.append(f"static {ret} " + extract(qdwin_c, name))
    parts.append("static void " + extract(input_c, "weston_pointer_end_grab"))
    parts.append(EPILOGUE)

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "probe.c"
        exe = Path(td) / "probe"
        src.write_text("\n".join(p for p in parts if p), encoding="utf-8")
        cflags = subprocess.run(
            ["pkg-config", "--cflags", "--libs", "wayland-server"],
            capture_output=True, text=True, check=True).stdout.split()
        # Poison the freed toplevel so a surviving pointer is a hard failure
        # rather than a lucky read. ASan is a best-effort hardening: fall back
        # to a plain build (the assertions stand on their own).
        asan = [cc, str(src), "-g", "-fsanitize=address", "-o", str(exe)]
        # QDWIN_PROBE_NO_ASAN=1 exercises the plain-build fallback path, to
        # check which assertions stand without the sanitizer.
        if os.environ.get("QDWIN_PROBE_NO_ASAN"):
            build = subprocess.CompletedProcess(asan, 1, "", "")
        else:
            build = subprocess.run(asan + cflags, capture_output=True,
                                   text=True)
        if build.returncode:
            build = subprocess.run([cc, str(src), "-g", "-o", str(exe)] + cflags,
                                   capture_output=True, text=True)
        if build.returncode:
            print(build.stderr)
            return fail("probe did not compile")
        # The fixture deliberately leaves live toplevels allocated at exit;
        # this probe is about use-after-free, not leaks.
        env = dict(os.environ, ASAN_OPTIONS="detect_leaks=0")
        run = subprocess.run([str(exe)], capture_output=True, text=True,
                             env=env)
        sys.stdout.write(run.stdout)
        sys.stdout.write(run.stderr)
        if run.returncode in (0, 77):
            return run.returncode
        return 1


if __name__ == "__main__":
    sys.exit(main())
