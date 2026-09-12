/*
 * qdwin-nested-probe — test client for the qdwin_nested_v1 + nested-proxy
 * gating path (bind_qdwin_nested_manager, qdwin_nested_manager_advertise_
 * toplevel, qdwin_handle_nested_proxy_decision; qdwin/qdwin.c).
 *
 * Trust model (qdwin/qdwin-nested-v1.xml + qdwin-shell-v1.xml §nested):
 *   - The qdwin_nested_manager_v1 global is peer-uid filtered at bind time
 *     (same shape as qdwin_shell_v1): uid != allowed_uid → bind refused with
 *     a wl_client implementation error on wl_display.
 *   - advertise_toplevel synthesises an OUTER-SIDE proxy toplevel (a
 *     placeholder curtain, headless-safe — no PipeWire needed) and fires
 *     qdwin_shell_v1.toplevel_added. When a v8+ shell is bound the proxy
 *     starts on the HELD layer (invisible) and the compositor fires
 *     qdwin_shell_v1.nested_proxy_pending; the shell must answer with
 *     nested_proxy_decision(handle, 0=allow|1=deny|2=defer, reason).
 *   - allow releases the proxy (visible). deny posts policy_denied on the
 *     originating qdwin_nested_toplevel_v1 and destroys the proxy. defer
 *     keeps it held. A decision on an unknown/non-pending handle is a
 *     silent no-op (stale-decision tolerance).
 *
 * This probe is BOTH the shell and the nested compositor on one wl_client:
 * it binds qdwin_shell_v1 + bind_as_shell (so it owns the shell_resource the
 * decision handler requires) AND qdwin_nested_manager_v1 (so it can
 * advertise). That keeps the scenario single-process and headless, matching
 * the 06/07/08 probe idiom. Driving the gate this way faithfully exercises
 * the advertise → pending → decision state machine; the only thing it does
 * NOT cover is a SECOND real uid binding the nested manager (the bind-uid
 * reject is instead driven with a foreign allowed_uid + --no-shell, exactly
 * as 06/07 do). The proxy pixels (PipeWire Shape-A) are out of scope: the
 * compositor uses a placeholder curtain until a v9 shell binds real pixels.
 *
 * Modes (mutually exclusive):
 *   --bind            Bind the nested manager only; report accept/refuse.
 *                     (Used for the uid bind-gate reject + accept cases.)
 *   --advertise       Bind shell + manager, advertise one toplevel; assert
 *                     the `configured` event fires AND nested_proxy_pending
 *                     fires (a v8 shell gates the proxy). Default mode.
 *   --allow           advertise, then nested_proxy_decision(handle, 0).
 *                     Assert it round-trips clean (proxy released).
 *   --deny            advertise, then nested_proxy_decision(handle, 1).
 *                     Assert the originating qdwin_nested_toplevel_v1 resource
 *                     gets exactly the policy_denied (=1) protocol error
 *                     (the enum is declared on qdwin_nested_manager_v1 but
 *                     wl_resource_post_error stamps the toplevel interface).
 *   --defer           advertise, then nested_proxy_decision(handle, 2).
 *                     Assert no protocol error, proxy stays alive.
 *   --stale-decision  advertise, then issue a decision on a BOGUS handle
 *                     (handle+9999). Assert it's a silent no-op (no error,
 *                     compositor alive) — the stale-handle tolerance.
 *   --double-decide   advertise, allow, then allow AGAIN on the same handle.
 *                     Assert the second is an idempotent no-op (no error).
 *   --destroy-order   advertise (get configured + pending), then destroy the
 *                     qdwin_nested_toplevel_v1 resource. Assert the proxy is
 *                     torn down (toplevel_removed fires) with no error/crash.
 *   --destroy-with-stream
 *                     advertise, ALLOW, subscribe_view_stream on the proxy
 *                     handle, wait for `approved` (a real PipeWire output and
 *                     a spawned qdistro-forward — not merely a request), THEN
 *                     destroy the qdwin_nested_toplevel_v1. Assert `torn_down`
 *                     fires with reason "source toplevel closed", the proxy is
 *                     removed, and the connection survives. This is the
 *                     load-bearing half of iso2/10 E2: the stream pointer is
 *                     freed at seat release, so an ordering slip here is a
 *                     real use-after-free rather than a stale read.
 *                     Exits 77 (INCONCLUSIVE) when the subscribe is denied —
 *                     no free pipewire output means no live dependent and
 *                     nothing asserted. Drive it with
 *                     `tests/host/start.sh --pipewire`.
 *   --destroy-with-popup
 *                     advertise, ALLOW, attach a 32px north chrome, print
 *                     CLICK_TARGET and block until a real pointer press
 *                     arrives as chrome_button (--click-timeout, default 30s),
 *                     show_popup with that grab serial, THEN destroy the
 *                     qdwin_nested_toplevel_v1. Assert qdwin_popup_v1.dismissed
 *                     fires, the proxy is removed, and the connection survives.
 *                     NOTE this is an EVENT-only oracle: `dismissed` firing is
 *                     necessary but not sufficient for qdwin_popup::parent
 *                     having actually been released — see the causality note
 *                     at the mode body.
 *                     show_popup is v29-gated on a live input-grab serial, so
 *                     this cannot be faked: no click means exit 77, never a
 *                     vacuous pass. Needs a VM/DRM session — see
 *                     tests/gui/22-nested-proxy-teardown.md.
 *   --destroy-with-move
 *                     advertise, ALLOW, start an interactive move on the
 *                     proxy handle, THEN destroy the qdwin_nested_toplevel_v1.
 *                     The proxy dies with a server-owned dependent still
 *                     attached — the shape of iso2/10 E2, where the proxy
 *                     destroy path freed the toplevel without releasing its
 *                     popup / move-drag / view_streams. Assert the proxy is
 *                     torn down, the connection survives, and a further
 *                     request still round-trips (a compositor that freed the
 *                     toplevel under a live grab does not get that far).
 *                     Exits 77 (INCONCLUSIVE) when the seat has no pointer:
 *                     begin_interactive_move needs one, and the headless
 *                     backend's synthesized seat has zero capabilities, so
 *                     this mode only has teeth in a VM/DRM session.
 *   --malformed       advertise with empty pw_node + empty input_sink + NULL
 *                     app_id/title (the protocol's "placeholder advertise").
 *                     Assert the compositor still creates a proxy + fires
 *                     configured (it must not crash on empty/NULL metadata).
 *
 * Exit codes:
 *   0  the mode's expected-accept postcondition held
 *  77  the mode could not be driven on this backend (see --destroy-with-move)
 *   4  --bind: the manager bind was REFUSED with the expected implementation
 *      error on wl_display (PASS signal for the unauthorized bind case)
 *   3  --deny: the originating nested toplevel got exactly policy_denied
 *      (PASS signal for the deny case)
 *   1  an expected postcondition failed, or an UNEXPECTED protocol error
 *   2  setup/other error (no display, global not advertised, ...)
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <poll.h>
#include <time.h>
#include <sys/mman.h>

#include <wayland-client.h>
#include "qdwin-shell-v1-client-protocol.h"
#include "qdwin-nested-v1-client-protocol.h"

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;

	uint32_t shell_name, shell_version;
	uint32_t mgr_name, mgr_version;
	int saw_shell, saw_mgr;

	/* Only the --destroy-with-* modes need this: a move-drag and a popup
	 * grab both require a seat with a pointer, which the headless
	 * backend's inert seat lacks. */
	struct wl_seat *seat;
	int seat_has_pointer;

	/* --destroy-with-popup builds real chrome + popup wl_surfaces. */
	struct wl_compositor *compositor;
	struct wl_shm *shm;

	/* Output mode, so the popup mode can pick a chrome side whose band is
	 * actually on-screen and therefore clickable. */
	struct wl_output *output;
	int out_w, out_h, out_scale, output_count;
	int32_t out_x, out_y, out_transform;

	/* The shell version to bind. v8 is enough for the gating modes; the
	 * popup mode needs chrome_button (v20) to learn a live grab serial and
	 * show_popup's v29 serial contract, so it asks for more. */
	uint32_t want_shell_version;

	struct qdwin_shell_v1 *shell;
	struct qdwin_nested_manager_v1 *mgr;

	/* Observed shell events. */
	int got_hello;
	int got_pending;          /* nested_proxy_pending fired */
	uint32_t pending_handle;
	int toplevel_added_count;
	uint32_t last_added_handle;
	int toplevel_removed_count;
	uint32_t last_removed_handle;

	/* Observed nested-toplevel events. */
	int got_configured;
	int32_t cfg_w, cfg_h;

	/* Geometry of the proxy, so the VM lane can aim a click at its chrome.
	 * watch_handle scopes the capture: on a populated session other windows
	 * emit toplevel_geometry too, and aiming a click with a stray window's
	 * rectangle would look like a calibration failure (codex r1). */
	int got_geometry;
	uint32_t geom_handle;
	int32_t geom_x, geom_y;
	uint32_t geom_w, geom_h;

	/* --destroy-with-stream: qdwin_view_stream_v1 verdict + teardown. */
	int stream_approved, stream_denied, stream_torn;
	char stream_reason[160];

	/* --destroy-with-popup: the chrome_button that yields a grab serial,
	 * and the popup's dismissal. */
	int got_chrome_button;
	uint32_t chrome_serial, chrome_handle;
	int popup_dismissed;
};

/* ---- qdwin_shell_v1 listener (only the fields we assert on do work) ---- */

static void l_hello(void *d, struct qdwin_shell_v1 *s, uint32_t uid)
{ struct probe *p = d; (void)s; (void)uid; p->got_hello = 1; }

static void l_toplevel_added(void *d, struct qdwin_shell_v1 *s, uint32_t handle,
			     uint32_t owner_uid, const char *app_id,
			     const char *title, uint32_t is_xwayland)
{
	struct probe *p = d;
	(void)s; (void)owner_uid; (void)app_id; (void)title; (void)is_xwayland;
	p->toplevel_added_count++;
	p->last_added_handle = handle;
}
static void l_toplevel_geometry(void *d, struct qdwin_shell_v1 *s, uint32_t h,
				int32_t x, int32_t y, uint32_t w, uint32_t ht)
{
	struct probe *p = d;
	(void)s;
	/* Record WHICH toplevel this was: the proxy's only geometry event
	 * arrives during advertise, before the mode body knows its handle, so
	 * the check has to happen at the point of use rather than here. */
	p->got_geometry = 1;
	p->geom_handle = h;
	p->geom_x = x; p->geom_y = y; p->geom_w = w; p->geom_h = ht;
}
static void l_toplevel_state(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			     uint32_t st)
{ (void)d; (void)s; (void)h; (void)st; }
static void l_toplevel_title(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			     const char *t)
{ (void)d; (void)s; (void)h; (void)t; }
static void l_toplevel_removed(void *d, struct qdwin_shell_v1 *s, uint32_t h)
{
	struct probe *p = d;
	(void)s;
	p->toplevel_removed_count++;
	p->last_removed_handle = h;
}
static void l_locked_changed(void *d, struct qdwin_shell_v1 *s, uint32_t l)
{ (void)d; (void)s; (void)l; }
static void l_seat_created(void *d, struct qdwin_shell_v1 *s, const char *n)
{ (void)d; (void)s; (void)n; }
static void l_seat_removed(void *d, struct qdwin_shell_v1 *s, const char *n)
{ (void)d; (void)s; (void)n; }
static void l_output_created(void *d, struct qdwin_shell_v1 *s, const char *n)
{ (void)d; (void)s; (void)n; }
static void l_output_removed(void *d, struct qdwin_shell_v1 *s, const char *n)
{ (void)d; (void)s; (void)n; }
static void l_launcher_requested(void *d, struct qdwin_shell_v1 *s)
{ (void)d; (void)s; }
static void l_switcher_next(void *d, struct qdwin_shell_v1 *s, int32_t dir)
{ (void)d; (void)s; (void)dir; }
static void l_switcher_commit(void *d, struct qdwin_shell_v1 *s)
{ (void)d; (void)s; }
static void l_lock_requested(void *d, struct qdwin_shell_v1 *s)
{ (void)d; (void)s; }
static void l_idle_lock_hint(void *d, struct qdwin_shell_v1 *s, uint32_t st)
{ (void)d; (void)s; (void)st; }
static void l_nested_pending(void *d, struct qdwin_shell_v1 *s, uint32_t handle,
			     const char *app_id, uint32_t origin_uid)
{
	struct probe *p = d;
	(void)s; (void)app_id; (void)origin_uid;
	p->got_pending = 1;
	p->pending_handle = handle;
}
static void l_nested_pixsrc(void *d, struct qdwin_shell_v1 *s, uint32_t handle,
			    const char *pw_node, const char *input_sink)
{ (void)d; (void)s; (void)handle; (void)pw_node; (void)input_sink; }
static void l_overlay_key(void *d, struct qdwin_shell_v1 *s, uint32_t role,
			  uint32_t sym, const char *utf8, uint32_t state)
{ (void)d; (void)s; (void)role; (void)sym; (void)utf8; (void)state; }
static void l_selection_set(void *d, struct qdwin_shell_v1 *s,
			    const char *seat_name, uint32_t source_handle,
			    const char *mime_concat, uint32_t is_primary)
{ (void)d; (void)s; (void)seat_name; (void)source_handle; (void)mime_concat;
  (void)is_primary; }

/* Slots for every event the compositor may deliver at the version we bind.
 * libwayland aborts the client on a NULL listener slot for a delivered event,
 * so the popup mode (which binds past v8) needs the whole table populated —
 * the same reason qdwin-bystander carries these. Only chrome_button does work:
 * it is how a shell-role client learns the live grab serial show_popup
 * requires (v29), because libweston does not reliably deliver wl_pointer
 * button events to surfaces owned by the shell's own wl_client. */
static void l_activation_pending(void *d, struct qdwin_shell_v1 *s, uint32_t h,
				 uint32_t src, uint32_t tgt, const char *token)
{ (void)d; (void)s; (void)h; (void)src; (void)tgt; (void)token; }
static void l_secctx(void *d, struct qdwin_shell_v1 *s, uint32_t h,
		     const char *eng, const char *app, const char *inst)
{ (void)d; (void)s; (void)h; (void)eng; (void)app; (void)inst; }
static void l_peer_identity(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			    uint32_t pid, uint32_t st, uint32_t st_hi,
			    uint32_t uid, const char *exe, const char *label)
{ (void)d; (void)s; (void)h; (void)pid; (void)st; (void)st_hi; (void)uid;
  (void)exe; (void)label; }
static void l_seat_focus_changed(void *d, struct qdwin_shell_v1 *s,
				 const char *seat, uint32_t h)
{ (void)d; (void)s; (void)seat; (void)h; }
static void l_selection_set_src_id(void *d, struct qdwin_shell_v1 *s,
				   const char *eng, const char *app,
				   const char *inst)
{ (void)d; (void)s; (void)eng; (void)app; (void)inst; }
static void l_data_offer_recv_pending(void *d, struct qdwin_shell_v1 *s,
				      uint32_t rh, const char *seat,
				      uint32_t src, uint32_t tgt,
				      const char *mime)
{ (void)d; (void)s; (void)rh; (void)seat; (void)src; (void)tgt; (void)mime; }
static void l_hotkey_pressed(void *d, struct qdwin_shell_v1 *s, uint32_t id)
{ (void)d; (void)s; (void)id; }
static void l_chrome_button(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			    uint32_t side, wl_fixed_t sx, wl_fixed_t sy,
			    uint32_t button, uint32_t state, uint32_t serial)
{
	struct probe *p = d;
	(void)s; (void)side; (void)sx; (void)sy; (void)button;
	/* Only a PRESS carries a serial that matches the seat's live grab
	 * serial; a release updates nothing show_popup will accept. */
	if (state != 1)
		return;
	p->got_chrome_button = 1;
	p->chrome_serial = serial;
	p->chrome_handle = h;
}
static void l_popup_button(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			   wl_fixed_t sx, wl_fixed_t sy, uint32_t button,
			   uint32_t state, uint32_t serial)
{ (void)d; (void)s; (void)h; (void)sx; (void)sy; (void)button; (void)state;
  (void)serial; }
static void l_toplevel_workspace(void *d, struct qdwin_shell_v1 *s, uint32_t h,
				 uint32_t index)
{ (void)d; (void)s; (void)h; (void)index; }
static void l_toplevel_app_id(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			      const char *app_id)
{ (void)d; (void)s; (void)h; (void)app_id; }

/* The listener table is version-truncated by libwayland: the compositor
 * only dispatches events the bound version actually has. We bind v8, so
 * events newer than v8 (overlay_key v17, selection_set v11, ...) never fire,
 * but their slots must still be present in the struct for ABI layout. We
 * fill all slots defensively in case a future qdwin bumps the bound version.
 */
static const struct qdwin_shell_v1_listener shell_listener = {
	.hello              = l_hello,
	.toplevel_added     = l_toplevel_added,
	.toplevel_geometry  = l_toplevel_geometry,
	.toplevel_state     = l_toplevel_state,
	.toplevel_title     = l_toplevel_title,
	.toplevel_removed   = l_toplevel_removed,
	.locked_changed     = l_locked_changed,
	.seat_created       = l_seat_created,
	.seat_removed       = l_seat_removed,
	.output_created     = l_output_created,
	.output_removed     = l_output_removed,
	.launcher_requested = l_launcher_requested,
	.switcher_next      = l_switcher_next,
	.switcher_commit    = l_switcher_commit,
	.lock_requested     = l_lock_requested,
	.idle_lock_hint     = l_idle_lock_hint,
	.nested_proxy_pending      = l_nested_pending,
	.nested_proxy_pixel_source = l_nested_pixsrc,
	.overlay_key        = l_overlay_key,
	.selection_set      = l_selection_set,
	.activation_pending = l_activation_pending,
	.toplevel_security_context = l_secctx,
	.toplevel_peer_identity    = l_peer_identity,
	.seat_focus_changed = l_seat_focus_changed,
	.selection_set_source_identity = l_selection_set_src_id,
	.data_offer_receive_pending    = l_data_offer_recv_pending,
	.hotkey_pressed     = l_hotkey_pressed,
	.chrome_button      = l_chrome_button,
	.popup_button       = l_popup_button,
	.toplevel_workspace = l_toplevel_workspace,
	.toplevel_app_id    = l_toplevel_app_id,
};

/* ---- qdwin_view_stream_v1 listener (--destroy-with-stream) ---- */

static void
vs_approved(void *d, struct qdwin_view_stream_v1 *vs, const char *node,
	    uint32_t rdp_port, const char *cert, const char *password)
{
	struct probe *p = d;
	(void)vs; (void)cert; (void)password;
	p->stream_approved = 1;
	fprintf(stderr, "qdwin-nested-probe: view_stream approved "
		"(pw_node=\"%s\" rdp_port=%u)\n", node ? node : "", rdp_port);
}
static void
vs_denied(void *d, struct qdwin_view_stream_v1 *vs, const char *reason)
{
	struct probe *p = d;
	(void)vs;
	p->stream_denied = 1;
	snprintf(p->stream_reason, sizeof p->stream_reason, "%s",
		 reason ? reason : "");
}
static void
vs_torn_down(void *d, struct qdwin_view_stream_v1 *vs, const char *reason)
{
	struct probe *p = d;
	(void)vs;
	p->stream_torn = 1;
	snprintf(p->stream_reason, sizeof p->stream_reason, "%s",
		 reason ? reason : "");
}
static const struct qdwin_view_stream_v1_listener vs_listener = {
	.approved = vs_approved, .denied = vs_denied, .torn_down = vs_torn_down,
};

/* ---- qdwin_popup_v1 listener (--destroy-with-popup) ---- */

static void
pop_dismissed(void *d, struct qdwin_popup_v1 *pop)
{
	struct probe *p = d;
	(void)pop;
	p->popup_dismissed = 1;
}
static const struct qdwin_popup_v1_listener pop_listener = {
	.dismissed = pop_dismissed,
};

/* A minimal single-colour ARGB buffer. attach_decoration and show_popup both
 * require the surface to already carry committed content, so both chrome and
 * popup surfaces need one. */
static struct wl_buffer *
make_buffer(struct wl_shm *shm, int w, int h, uint32_t argb)
{
	int stride = w * 4;
	int size = stride * h;
	int fd = memfd_create("qdwin-nested-probe", MFD_CLOEXEC);
	if (fd < 0) return NULL;
	if (ftruncate(fd, size) < 0) { close(fd); return NULL; }
	uint32_t *px = mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
	if (px == MAP_FAILED) { close(fd); return NULL; }
	for (int i = 0; i < w * h; i++) px[i] = argb;
	struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, w, h, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	munmap(px, size);
	close(fd);
	return buf;
}

/* Create a roleless wl_surface carrying one committed buffer. */
static struct wl_surface *
make_committed_surface(struct probe *p, int w, int h, uint32_t argb)
{
	struct wl_surface *surf = wl_compositor_create_surface(p->compositor);
	if (!surf) return NULL;
	struct wl_buffer *buf = make_buffer(p->shm, w, h, argb);
	if (!buf) { wl_surface_destroy(surf); return NULL; }
	wl_surface_attach(surf, buf, 0, 0);
	wl_surface_damage(surf, 0, 0, w, h);
	wl_surface_commit(surf);
	return surf;
}

/* ---- qdwin_nested_toplevel_v1 listener ---- */

static void nt_configured(void *d, struct qdwin_nested_toplevel_v1 *t,
			  int32_t w, int32_t h)
{
	struct probe *p = d;
	(void)t;
	p->got_configured = 1;
	p->cfg_w = w;
	p->cfg_h = h;
}
static void nt_close_requested(void *d, struct qdwin_nested_toplevel_v1 *t)
{ (void)d; (void)t; }
static void nt_focus_changed(void *d, struct qdwin_nested_toplevel_v1 *t,
			     uint32_t focused)
{ (void)d; (void)t; (void)focused; }

static const struct qdwin_nested_toplevel_v1_listener nt_listener = {
	.configured      = nt_configured,
	.close_requested = nt_close_requested,
	.focus_changed   = nt_focus_changed,
};

/* ---- registry ---- */

static void l_out_geometry(void *d, struct wl_output *o, int32_t x, int32_t y,
			   int32_t pw, int32_t ph, int32_t sub, const char *make,
			   const char *model, int32_t transform)
{
	struct probe *p = d;
	(void)o; (void)pw; (void)ph; (void)sub; (void)make; (void)model;
	/* Origin and transform are part of the click-target assumption, not
	 * just the mode: a rotated or non-origin output makes output-local
	 * pixel arithmetic point somewhere else (codex r3). */
	p->out_x = x;
	p->out_y = y;
	p->out_transform = transform;
}
static void l_out_mode(void *d, struct wl_output *o, uint32_t flags,
		       int32_t w, int32_t h, int32_t refresh)
{
	struct probe *p = d;
	(void)o; (void)refresh;
	if (flags & WL_OUTPUT_MODE_CURRENT) {
		p->out_w = w;
		p->out_h = h;
	}
}
static void l_out_done(void *d, struct wl_output *o) { (void)d; (void)o; }
static void l_out_scale(void *d, struct wl_output *o, int32_t f)
{
	struct probe *p = d;
	(void)o;
	p->out_scale = f;
}
static void l_out_name(void *d, struct wl_output *o, const char *n)
{ (void)d; (void)o; (void)n; }
static void l_out_description(void *d, struct wl_output *o, const char *n)
{ (void)d; (void)o; (void)n; }
static const struct wl_output_listener output_listener = {
	.geometry = l_out_geometry, .mode = l_out_mode, .done = l_out_done,
	.scale = l_out_scale, .name = l_out_name,
	.description = l_out_description,
};

static void l_seat_caps(void *d, struct wl_seat *s, uint32_t caps)
{
	struct probe *p = d;
	(void)s;
	p->seat_has_pointer = !!(caps & WL_SEAT_CAPABILITY_POINTER);
}
static void l_seat_name(void *d, struct wl_seat *s, const char *n)
{ (void)d; (void)s; (void)n; }
static const struct wl_seat_listener seat_listener = {
	.capabilities = l_seat_caps, .name = l_seat_name,
};


static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct probe *p = data;
	(void)reg;
	if (strcmp(interface, qdwin_shell_v1_interface.name) == 0) {
		p->saw_shell = 1;
		p->shell_name = name;
		/* v8 is enough for nested_proxy_pending + decision; the popup
		 * mode raises want_shell_version (chrome_button is v20). */
		p->shell_version = version < p->want_shell_version
			? version : p->want_shell_version;
	} else if (strcmp(interface,
			  qdwin_nested_manager_v1_interface.name) == 0) {
		p->saw_mgr = 1;
		p->mgr_name = name;
		p->mgr_version = version < 1 ? version : 1;
	} else if (strcmp(interface, wl_compositor_interface.name) == 0 &&
		   !p->compositor) {
		p->compositor = wl_registry_bind(reg, name,
						 &wl_compositor_interface,
						 version < 4 ? version : 4);
	} else if (strcmp(interface, wl_shm_interface.name) == 0 && !p->shm) {
		p->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	} else if (strcmp(interface, wl_output_interface.name) == 0) {
		/* Count every output, not just the one we bind: the click-target
		 * arithmetic below assumes ONE unscaled output at the origin,
		 * and silently aiming at the wrong one looks like a calibration
		 * failure rather than an unmet precondition (codex r2). */
		p->output_count++;
		if (p->output)
			return;
		/* v3 carries `scale`; name/description are v4 and go
		 * undelivered, which is why every slot is filled. */
		p->output = wl_registry_bind(reg, name, &wl_output_interface,
					     version < 3 ? version : 3);
		wl_output_add_listener(p->output, &output_listener, p);
	} else if (strcmp(interface, wl_seat_interface.name) == 0 &&
		   !p->seat) {
		p->seat = wl_registry_bind(reg, name, &wl_seat_interface,
					   version < 5 ? version : 5);
		wl_seat_add_listener(p->seat, &seat_listener, p);
	}
}
static void on_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {
	.global = on_global, .global_remove = on_global_remove,
};

/* Roundtrip; report a fatal protocol error with its code + interface. */
static int
roundtrip_err(struct probe *p, const char *what, uint32_t *out_code,
	      const struct wl_interface **out_iface)
{
	int rc = wl_display_roundtrip(p->display);
	int err = wl_display_get_error(p->display);
	if (out_code) *out_code = 0;
	if (out_iface) *out_iface = NULL;
	if (rc < 0 || err != 0) {
		uint32_t obj_id = 0, code = 0;
		const struct wl_interface *iface = NULL;
		code = wl_display_get_protocol_error(p->display, &iface, &obj_id);
		if (out_code) *out_code = code;
		if (out_iface) *out_iface = iface;
		fprintf(stderr,
			"qdwin-nested-probe: %s ERROR (errno=%d, proto code=%u "
			"on %s#%u)\n",
			what, err, code, iface ? iface->name : "(unknown)",
			obj_id);
		return err ? err : 1;
	}
	return 0;
}

/* Dispatch until one of the watched flags is set or the timeout expires.
 * Needed by the destroy-with-{stream,popup} modes: a stream approval waits on
 * a PipeWire allocation plus a qdistro-forward spawn, and a chrome_button
 * waits on a human/ydotool click — neither is a reply to a request, so
 * wl_display_roundtrip would return long before they arrive.
 * Returns 1 if a flag was set, 0 on timeout, -1 on a connection error. */
static int64_t
now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int
wait_for(struct probe *p, const int *a, const int *b, int timeout_sec)
{
	struct pollfd pfd = { .fd = wl_display_get_fd(p->display),
			      .events = POLLIN };
	/* A MONOTONIC deadline, not a budget decremented on poll timeouts:
	 * on a live desktop other windows produce events constantly, and a
	 * budget that only shrinks when poll(2) expires never expires at all
	 * (codex r1). EINTR consumes no budget for the same reason. */
	const int64_t deadline =
		now_ms() + (timeout_sec > 0 ? (int64_t)timeout_sec * 1000 : 0);
	while (!*a && !(b && *b)) {
		int64_t remaining = deadline - now_ms();
		if (remaining <= 0)
			return 0;
		while (wl_display_prepare_read(p->display) != 0) {
			if (wl_display_dispatch_pending(p->display) < 0)
				return -1;
			if (*a || (b && *b))
				return 1;
		}
		/* A full outgoing buffer needs WRITABLE readiness to drain; a
		 * POLLIN-only wait would stall until the peer happened to
		 * speak first. */
		pfd.events = POLLIN;
		if (wl_display_flush(p->display) < 0) {
			if (errno != EAGAIN) {
				wl_display_cancel_read(p->display);
				return -1;
			}
			pfd.events |= POLLOUT;
		}
		int step = remaining > 200 ? 200 : (int)remaining;
		int n = poll(&pfd, 1, step);
		if (n < 0) {
			wl_display_cancel_read(p->display);
			if (errno == EINTR)
				continue;
			return -1;
		}
		/* Terminal readiness is a CONNECTION ERROR, not a timeout. These
		 * bits are returned whether or not they were requested, and a
		 * hung-up fd stays ready forever: treating them as "not POLLIN,
		 * retry" burns a CPU until the deadline and then reports the
		 * wait as a clean timeout — which the callers turn into 77
		 * INCONCLUSIVE rather than a failure (codex r2, reproduced with
		 * a stubbed harness: 31.7M spins in one second). */
		if (pfd.revents & (POLLHUP | POLLERR | POLLNVAL)) {
			wl_display_cancel_read(p->display);
			fprintf(stderr, "qdwin-nested-probe: display fd is no "
				"longer usable (revents=%#x)\n", pfd.revents);
			return -1;
		}
		if (n == 0 || !(pfd.revents & POLLIN)) {
			wl_display_cancel_read(p->display);
			continue;
		}
		if (wl_display_read_events(p->display) < 0)
			return -1;
		if (wl_display_dispatch_pending(p->display) < 0)
			return -1;
	}
	return 1;
}

enum mode {
	M_ADVERTISE, M_BIND, M_ALLOW, M_DENY, M_DEFER,
	M_STALE, M_DOUBLE, M_DESTROY, M_DESTROY_MOVE, M_DESTROY_STREAM,
	M_DESTROY_POPUP, M_MALFORMED
};

int main(int argc, char *argv[])
{
	enum mode mode = M_ADVERTISE;
	int click_timeout_sec = 30;
	for (int i = 1; i < argc; i++) {
		if      (!strcmp(argv[i], "--bind"))           mode = M_BIND;
		else if (!strcmp(argv[i], "--advertise"))      mode = M_ADVERTISE;
		else if (!strcmp(argv[i], "--allow"))          mode = M_ALLOW;
		else if (!strcmp(argv[i], "--deny"))           mode = M_DENY;
		else if (!strcmp(argv[i], "--defer"))          mode = M_DEFER;
		else if (!strcmp(argv[i], "--stale-decision")) mode = M_STALE;
		else if (!strcmp(argv[i], "--double-decide"))  mode = M_DOUBLE;
		else if (!strcmp(argv[i], "--destroy-order"))  mode = M_DESTROY;
		else if (!strcmp(argv[i], "--destroy-with-move"))
			mode = M_DESTROY_MOVE;
		else if (!strcmp(argv[i], "--destroy-with-stream"))
			mode = M_DESTROY_STREAM;
		else if (!strcmp(argv[i], "--destroy-with-popup"))
			mode = M_DESTROY_POPUP;
		else if (!strcmp(argv[i], "--click-timeout") && i + 1 < argc)
			click_timeout_sec = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--malformed"))      mode = M_MALFORMED;
		else if (!strcmp(argv[i], "-h") ||
			 !strcmp(argv[i], "--help")) {
			/* Printed WITHOUT connecting, so a harness can probe
			 * for a mode's presence on a deployed binary before it
			 * takes the singleton shell role. */
			printf("usage: qdwin-nested-probe [MODE] "
			       "[--click-timeout SEC]\n"
			       "modes: --bind --advertise --allow --deny "
			       "--defer --stale-decision --double-decide\n"
			       "       --destroy-order --destroy-with-move "
			       "--destroy-with-stream --destroy-with-popup\n"
			       "       --malformed\n"
			       "exit: 0 pass, 1 postcondition failed, "
			       "2 setup error, 3 --deny expected error,\n"
			       "      4 --bind expected refusal, "
			       "77 INCONCLUSIVE (mode not driveable here)\n");
			return 0;
		}
	}

	struct probe p = {0};
	/* chrome_button is v20; show_popup's serial contract is v29. Every
	 * other mode stays on v8 so it keeps testing the minimum a gating
	 * shell must bind. */
	p.want_shell_version = (mode == M_DESTROY_POPUP) ? 29u : 8u;
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-nested-probe: wl_display_connect "
			"failed: %s\n", strerror(errno));
		return 2;
	}
	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);
	wl_display_roundtrip(p.display);

	if (!p.saw_mgr) {
		fprintf(stderr, "qdwin-nested-probe: qdwin_nested_manager_v1 "
			"not advertised\n");
		return 2;
	}

	/* --bind: just resolve the manager bind and report. The bind is
	 * issued here (not in on_global) so a refused bind surfaces as a
	 * clean implementation error we can classify. */
	if (mode == M_BIND) {
		p.mgr = wl_registry_bind(p.registry, p.mgr_name,
					 &qdwin_nested_manager_v1_interface,
					 p.mgr_version);
		if (!p.mgr) {
			fprintf(stderr, "qdwin-nested-probe: mgr bind NULL\n");
			return 2;
		}
		uint32_t code = 0;
		const struct wl_interface *iface = NULL;
		if (roundtrip_err(&p, "nested manager bind", &code, &iface) != 0) {
			if (iface == &wl_display_interface &&
			    code == WL_DISPLAY_ERROR_IMPLEMENTATION) {
				printf("qdwin-nested-probe: manager bind REFUSED "
				       "with implementation error\n");
				return 4;
			}
			fprintf(stderr, "qdwin-nested-probe: bind failed but not "
				"with implementation error (code=%u iface=%s)\n",
				code, iface ? iface->name : "(none)");
			return 1;
		}
		printf("qdwin-nested-probe: manager bind ACCEPTED\n");
		return 0;
	}

	/* All other modes need the shell role (the decision handler requires
	 * the issuing resource to be the bound shell). */
	if (!p.saw_shell) {
		fprintf(stderr, "qdwin-nested-probe: qdwin_shell_v1 not "
			"advertised (needed for nested gating)\n");
		return 2;
	}
	p.shell = wl_registry_bind(p.registry, p.shell_name,
				   &qdwin_shell_v1_interface, p.shell_version);
	if (!p.shell) {
		fprintf(stderr, "qdwin-nested-probe: shell bind NULL\n");
		return 2;
	}
	qdwin_shell_v1_add_listener(p.shell, &shell_listener, &p);
	qdwin_shell_v1_bind_as_shell(p.shell);
	if (roundtrip_err(&p, "bind_as_shell", NULL, NULL) != 0)
		return 1;
	if (!p.got_hello) {
		fprintf(stderr, "qdwin-nested-probe: no hello after "
			"bind_as_shell\n");
		return 1;
	}

	p.mgr = wl_registry_bind(p.registry, p.mgr_name,
				 &qdwin_nested_manager_v1_interface,
				 p.mgr_version);
	if (!p.mgr) {
		fprintf(stderr, "qdwin-nested-probe: mgr bind NULL\n");
		return 2;
	}
	if (roundtrip_err(&p, "nested manager bind", NULL, NULL) != 0)
		return 1;

	/* Advertise one inner toplevel. Malformed mode uses the protocol's
	 * documented placeholder shape: empty pw_node/input_sink + NULL
	 * app_id/title. */
	int added_before = p.toplevel_added_count;
	struct qdwin_nested_toplevel_v1 *nt;
	if (mode == M_MALFORMED) {
		/* Protocol args are not allow-null, so the "placeholder
		 * advertise" uses empty strings (the XML's documented
		 * pw_node="" placeholder shape), not NULL. This still drives
		 * the empty-metadata path through strdup("")/log handling. */
		nt = qdwin_nested_manager_v1_advertise_toplevel(
			p.mgr, "", "", "", "", (uint32_t)getuid());
	} else {
		nt = qdwin_nested_manager_v1_advertise_toplevel(
			p.mgr,
			"weston.pipewire:0:headless",  /* unresolvable, ok S2 */
			"",                            /* no input sink */
			"org.qdistro.test.nested",
			"nested probe toplevel",
			(uint32_t)getuid());
	}
	if (!nt) {
		fprintf(stderr, "qdwin-nested-probe: advertise returned NULL\n");
		return 2;
	}
	qdwin_nested_toplevel_v1_add_listener(nt, &nt_listener, &p);

	if (roundtrip_err(&p, "advertise_toplevel", NULL, NULL) != 0)
		return 1;
	/* The `configured` event is fired synchronously at the end of
	 * advertise_toplevel; a second roundtrip guarantees we've drained it
	 * plus the toplevel_added + nested_proxy_pending the same call queued. */
	wl_display_roundtrip(p.display);

	if (!p.got_configured) {
		fprintf(stderr, "qdwin-nested-probe: no `configured` event "
			"after advertise\n");
		return 1;
	}
	if (p.toplevel_added_count <= added_before) {
		fprintf(stderr, "qdwin-nested-probe: advertise did not fire "
			"toplevel_added (got %d, want >%d)\n",
			p.toplevel_added_count, added_before);
		return 1;
	}
	uint32_t handle = p.last_added_handle;

	switch (mode) {
	case M_ADVERTISE:
	case M_MALFORMED:
		/* A v8 shell is bound, so the proxy must be gated → pending. */
		if (!p.got_pending) {
			fprintf(stderr, "qdwin-nested-probe: advertise did not "
				"fire nested_proxy_pending (v8 shell should "
				"gate)\n");
			return 1;
		}
		if (p.pending_handle != handle) {
			fprintf(stderr, "qdwin-nested-probe: pending handle %u "
				"!= added handle %u\n",
				p.pending_handle, handle);
			return 1;
		}
		printf("qdwin-nested-probe: advertise ACCEPTED "
		       "(configured=%dx%d, pending handle=%u%s)\n",
		       p.cfg_w, p.cfg_h, handle,
		       mode == M_MALFORMED ? ", malformed/placeholder" : "");
		return 0;

	case M_ALLOW:
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle, 0,
						     "probe-allow");
		if (roundtrip_err(&p, "decision allow", NULL, NULL) != 0)
			return 1;
		printf("qdwin-nested-probe: allow decision round-tripped clean "
		       "(handle=%u)\n", handle);
		return 0;

	case M_DOUBLE: {
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle, 0,
						     "probe-allow-1");
		if (roundtrip_err(&p, "decision allow #1", NULL, NULL) != 0)
			return 1;
		/* Second allow on the now-non-pending handle: idempotent no-op
		 * per the XML; must not raise a protocol error. */
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle, 0,
						     "probe-allow-2");
		if (roundtrip_err(&p, "decision allow #2", NULL, NULL) != 0)
			return 1;
		printf("qdwin-nested-probe: double allow is idempotent no-op "
		       "(handle=%u)\n", handle);
		return 0;
	}

	case M_DEFER:
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle, 2,
						     "probe-defer");
		if (roundtrip_err(&p, "decision defer", NULL, NULL) != 0)
			return 1;
		/* Liveness: the proxy stays held but the compositor is fine. */
		if (wl_display_roundtrip(p.display) < 0) {
			fprintf(stderr, "qdwin-nested-probe: connection died "
				"after defer\n");
			return 1;
		}
		printf("qdwin-nested-probe: defer decision round-tripped clean, "
		       "proxy stays held (handle=%u)\n", handle);
		return 0;

	case M_STALE: {
		/* Decision on a handle that was never advertised: silent
		 * no-op, no protocol error, compositor stays alive. */
		uint32_t bogus = handle + 9999u;
		qdwin_shell_v1_nested_proxy_decision(p.shell, bogus, 0,
						     "probe-stale");
		if (roundtrip_err(&p, "stale decision", NULL, NULL) != 0) {
			fprintf(stderr, "qdwin-nested-probe: stale decision "
				"raised an error (should be silent no-op)\n");
			return 1;
		}
		if (wl_display_roundtrip(p.display) < 0) {
			fprintf(stderr, "qdwin-nested-probe: connection died "
				"after stale decision\n");
			return 1;
		}
		printf("qdwin-nested-probe: stale decision (handle=%u) was a "
		       "silent no-op\n", bogus);
		return 0;
	}

	case M_DENY: {
		uint32_t code = 0;
		const struct wl_interface *iface = NULL;
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle, 1,
						     "probe-deny");
		int err = roundtrip_err(&p, "decision deny", &code, &iface);
		if (err == 0) {
			fprintf(stderr, "qdwin-nested-probe: deny did NOT post "
				"policy_denied on the nested toplevel\n");
			return 1;
		}
		/* wl_resource_post_error stamps the wl_display error with the
		 * ERRORING RESOURCE's interface — here the originating
		 * qdwin_nested_toplevel_v1 (the compositor posts on nt->resource,
		 * qdwin.c:qdwin_handle_nested_proxy_decision case 1). The enum
		 * lives on qdwin_nested_manager_v1 but the code value is shared;
		 * assert both the code and that it landed on the nested toplevel. */
		if (iface != &qdwin_nested_toplevel_v1_interface ||
		    code != QDWIN_NESTED_MANAGER_V1_ERROR_POLICY_DENIED) {
			fprintf(stderr, "qdwin-nested-probe: deny got code=%u "
				"on %s, want policy_denied=%d on "
				"qdwin_nested_toplevel_v1\n", code,
				iface ? iface->name : "(none)",
				QDWIN_NESTED_MANAGER_V1_ERROR_POLICY_DENIED);
			return 1;
		}
		printf("qdwin-nested-probe: deny posted policy_denied on the "
		       "originating nested toplevel (handle=%u)\n", handle);
		return 3;
	}

	case M_DESTROY_MOVE: {
		int removed_before = p.toplevel_removed_count;

		/* A move-drag needs a pointer. The headless backend's
		 * synthesized seat has zero capabilities, so without one this
		 * mode would "pass" having attached no dependent at all —
		 * report INCONCLUSIVE rather than a vacuous PASS. */
		wl_display_roundtrip(p.display);
		if (!p.seat_has_pointer) {
			fprintf(stderr, "qdwin-nested-probe: no pointer on the "
				"seat — begin_interactive_move cannot start a "
				"drag here; this mode needs a VM/DRM session\n");
			return 77;
		}

		/* An allowed proxy is a legal target for shell-owned state. */
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle, 0,
						     "probe-allow");
		if (roundtrip_err(&p, "decision allow", NULL, NULL) != 0)
			return 1;
		/* Serial is ignored by begin_interactive_move (unlike
		 * show_popup, which requires a live input-grab serial and so
		 * is not reachable headlessly). */
		qdwin_shell_v1_begin_interactive_move(p.shell, handle, 0);
		if (roundtrip_err(&p, "begin_interactive_move", NULL, NULL) != 0)
			return 1;

		qdwin_nested_toplevel_v1_destroy(nt);
		if (roundtrip_err(&p, "nested toplevel destroy under a move",
				  NULL, NULL) != 0)
			return 1;
		wl_display_roundtrip(p.display);
		if (p.toplevel_removed_count <= removed_before) {
			fprintf(stderr, "qdwin-nested-probe: destroy under a "
				"move did not fire toplevel_removed "
				"(got %d, want >%d)\n",
				p.toplevel_removed_count, removed_before);
			return 1;
		}
		/* Liveness after the teardown: the compositor must still be
		 * serving this client. */
		if (wl_display_roundtrip(p.display) < 0) {
			fprintf(stderr, "qdwin-nested-probe: connection died "
				"after destroying a proxy under a move\n");
			return 1;
		}
		printf("qdwin-nested-probe: proxy destroyed under a live "
		       "move-drag; compositor alive (handle=%u)\n", handle);
		return 0;
	}

	case M_DESTROY_STREAM: {
		int removed_before = p.toplevel_removed_count;

		/* An allowed proxy is a legal stream source — subscribe_view_
		 * stream denies only while the decision is still pending. */
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle, 0,
						     "probe-allow");
		if (roundtrip_err(&p, "decision allow", NULL, NULL) != 0)
			return 1;

		struct qdwin_view_stream_v1 *vs =
			qdwin_shell_v1_subscribe_view_stream(
				p.shell, handle, "proxy-teardown-lane",
				320, 240, 0u /* read-only */);
		if (!vs) {
			fprintf(stderr, "qdwin-nested-probe: subscribe_view_"
				"stream returned NULL\n");
			return 2;
		}
		qdwin_view_stream_v1_add_listener(vs, &vs_listener, &p);
		if (roundtrip_err(&p, "subscribe_view_stream", NULL, NULL) != 0)
			return 1;

		if (wait_for(&p, &p.stream_approved, &p.stream_denied, 20) < 0) {
			fprintf(stderr, "qdwin-nested-probe: connection died "
				"waiting for the stream verdict\n");
			return 1;
		}
		/* No PipeWire backend (the headless host lane) or no free
		 * output means the dependent never became live — this mode
		 * would then assert nothing, so say so instead of passing. */
		if (p.stream_denied) {
			fprintf(stderr, "qdwin-nested-probe: subscribe DENIED "
				"(%s) — no live dependent to destroy under; "
				"this mode needs a VM with the pipewire "
				"backend\n", p.stream_reason);
			return 77;
		}
		if (!p.stream_approved) {
			fprintf(stderr, "qdwin-nested-probe: no approved/denied "
				"verdict within 20s\n");
			return 77;
		}

		/* The dependent must still be LIVE at the destruction boundary.
		 * A stream already torn down for an unrelated reason (the
		 * forward child died, the compositor locked) would let a
		 * sticky flag satisfy the assertion below without the destroy
		 * having released anything (codex r1). */
		if (p.stream_torn) {
			fprintf(stderr, "qdwin-nested-probe: stream was already "
				"torn down (%s) BEFORE the destroy — no live "
				"dependent to destroy under\n", p.stream_reason);
			return 77;
		}

		/* Destroy ONLY the advertiser's nested toplevel: the shape of
		 * a nested compositor crashing or unpublishing while the shell
		 * still holds the stream. */
		qdwin_nested_toplevel_v1_destroy(nt);
		if (roundtrip_err(&p, "nested toplevel destroy under a stream",
				  NULL, NULL) != 0)
			return 1;
		if (wait_for(&p, &p.stream_torn, NULL, 10) < 0) {
			fprintf(stderr, "qdwin-nested-probe: connection died "
				"after destroying a proxy under a stream\n");
			return 1;
		}
		if (!p.stream_torn) {
			fprintf(stderr, "qdwin-nested-probe: proxy destroy did "
				"NOT tear the stream down — the compositor "
				"kept a stream pointing at a freed toplevel\n");
			return 1;
		}
		/* The reason distinguishes the source-closed path from the
		 * forward-exited one 13-rdp-subscribe-frame already covers. */
		if (strcmp(p.stream_reason, "source toplevel closed") != 0) {
			fprintf(stderr, "qdwin-nested-probe: torn_down "
				"reason=\"%s\", want \"source toplevel "
				"closed\"\n", p.stream_reason);
			return 1;
		}
		if (p.toplevel_removed_count <= removed_before) {
			fprintf(stderr, "qdwin-nested-probe: destroy under a "
				"stream did not fire toplevel_removed "
				"(got %d, want >%d)\n",
				p.toplevel_removed_count, removed_before);
			return 1;
		}
		/* A COUNT is not enough on a populated session: some unrelated
		 * window closing would satisfy it while the proxy's own removal
		 * never fired (codex r1). */
		if (p.last_removed_handle != handle) {
			fprintf(stderr, "qdwin-nested-probe: removed handle %u "
				"!= the proxy %u\n",
				p.last_removed_handle, handle);
			return 1;
		}
		/* Liveness: a further request must still round-trip. A
		 * compositor that freed the toplevel under a live stream does
		 * not get this far. */
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle + 9999u, 0,
						     "probe-liveness");
		if (roundtrip_err(&p, "post-teardown liveness", NULL, NULL) != 0)
			return 1;
		qdwin_view_stream_v1_destroy(vs);
		if (roundtrip_err(&p, "view_stream destroy", NULL, NULL) != 0)
			return 1;
		printf("qdwin-nested-probe: proxy destroyed under a LIVE "
		       "view_stream; torn_down reason=\"%s\"; compositor "
		       "alive (handle=%u)\n", p.stream_reason, handle);
		return 0;
	}

	case M_DESTROY_POPUP: {
		int removed_before = p.toplevel_removed_count;

		if (!p.compositor || !p.shm) {
			fprintf(stderr, "qdwin-nested-probe: wl_compositor/"
				"wl_shm not advertised\n");
			return 2;
		}
		if (p.shell_version < 29) {
			fprintf(stderr, "qdwin-nested-probe: shell bound at v%u "
				"(<29) — chrome_button carries no grab serial "
				"show_popup will accept\n", p.shell_version);
			return 77;
		}
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle, 0,
						     "probe-allow");
		if (roundtrip_err(&p, "decision allow", NULL, NULL) != 0)
			return 1;

		/* The click target is computed from the proxy's real position,
		 * so the lane never hard-codes coordinates. Without a geometry
		 * event there is nothing to aim at — say so rather than print
		 * a target of (0,0) and then blame the click for missing. */
		if (wait_for(&p, &p.got_geometry, NULL, 5) < 0)
			return 1;
		/* On a populated session other windows emit toplevel_geometry
		 * too; aiming a click with a stray window's rectangle would
		 * look like a calibration failure (codex r1). */
		if (!p.got_geometry || p.geom_handle != handle) {
			fprintf(stderr, "qdwin-nested-probe: no toplevel_"
				"geometry for the proxy %u within 5s (last was "
				"for handle %u) — cannot compute a click "
				"target\n", handle, p.geom_handle);
			return 77;
		}

		/* Pick a chrome side whose band is actually ON-SCREEN, because
		 * an unclickable band makes this mode time out and report 77
		 * for a reason that has nothing to do with the property under
		 * test. The north band sits at (y - ch) and the south band at
		 * (y + content_height); a proxy at the top of the output has no
		 * north band, one at the bottom has no south band. Which side
		 * we use is irrelevant to what is tested: chrome_button fires
		 * for all four, and the popup's parent is the toplevel either
		 * way.
		 *
		 * Size comes from the `configured` event, which this mode has
		 * already asserted, not from the geometry event. */
		int cw = p.cfg_w > 0 ? p.cfg_w : 800;
		int chh = p.cfg_h > 0 ? p.cfg_h : 600;
		const int ch = 32;
		if (p.out_w <= 0 || p.out_h <= 0) {
			fprintf(stderr, "qdwin-nested-probe: no wl_output mode "
				"— cannot tell which chrome band is on-screen\n");
			return 77;
		}
		/* Declared lane constraint, asserted rather than assumed. The
		 * target below is computed in output-local pixels against the
		 * first output; a second output, or a scale factor, makes it
		 * point somewhere else entirely. */
		if (p.output_count != 1) {
			fprintf(stderr, "qdwin-nested-probe: %d outputs — the "
				"click target assumes exactly one\n",
				p.output_count);
			return 77;
		}
		if (p.out_scale > 1) {
			fprintf(stderr, "qdwin-nested-probe: output scale %d — "
				"the click target assumes an unscaled output\n",
				p.out_scale);
			return 77;
		}
		if (p.out_x != 0 || p.out_y != 0 || p.out_transform != 0) {
			fprintf(stderr, "qdwin-nested-probe: output at (%d,%d) "
				"transform=%d — the click target assumes an "
				"untransformed output at the origin\n",
				p.out_x, p.out_y, p.out_transform);
			return 77;
		}
		int north_y = p.geom_y - ch / 2;
		int south_y = p.geom_y + chh + ch / 2;
		int use_north = (north_y >= 0 && north_y < p.out_h);
		int click_y = use_north ? north_y : south_y;
		if (!use_north && (south_y < 0 || south_y >= p.out_h)) {
			fprintf(stderr, "qdwin-nested-probe: proxy at (%d,%d) "
				"%dx%d on a %dx%d output leaves neither chrome "
				"band on-screen; nothing to click\n",
				p.geom_x, p.geom_y, cw, chh,
				p.out_w, p.out_h);
			return 77;
		}
		int click_x = p.geom_x + cw / 2;
		if (click_x < 0 || click_x >= p.out_w)
			click_x = p.out_w / 2;

		struct wl_surface *chrome =
			make_committed_surface(&p, cw, ch, 0xff00aaaau);
		if (!chrome) {
			fprintf(stderr, "qdwin-nested-probe: chrome surface "
				"allocation failed\n");
			return 2;
		}
		qdwin_shell_v1_attach_decoration(
			p.shell, handle,
			use_north ? chrome : NULL, NULL,
			use_north ? NULL : chrome, NULL);
		if (roundtrip_err(&p, "attach_decoration", NULL, NULL) != 0)
			return 1;
		wl_display_roundtrip(p.display);

		/* Tell the lane exactly where to click. */
		printf("PROXY_GEOM x=%d y=%d w=%d h=%d out=%dx%d@%d,%d "
		       "outputs=%d scale=%d transform=%d side=%s chrome=%d\n",
		       p.geom_x, p.geom_y, cw, chh, p.out_w, p.out_h,
		       p.out_x, p.out_y, p.output_count,
		       p.out_scale > 0 ? p.out_scale : 1, p.out_transform,
		       use_north ? "N" : "S", ch);
		printf("CLICK_TARGET x=%d y=%d\n", click_x, click_y);
		fflush(stdout);

		if (!p.seat_has_pointer) {
			fprintf(stderr, "qdwin-nested-probe: no pointer on the "
				"seat — show_popup needs a live pointer grab "
				"serial; this mode needs a VM/DRM session\n");
			return 77;
		}

		int r = wait_for(&p, &p.got_chrome_button, NULL,
				 click_timeout_sec);
		if (r < 0) {
			fprintf(stderr, "qdwin-nested-probe: connection died "
				"waiting for a chrome click\n");
			return 1;
		}
		if (!p.got_chrome_button) {
			fprintf(stderr, "qdwin-nested-probe: no chrome_button "
				"within %ds — the lane must inject a click on "
				"CLICK_TARGET; without one there is no grab "
				"serial and no popup to destroy under\n",
				click_timeout_sec);
			return 77;
		}
		if (p.chrome_handle != handle) {
			fprintf(stderr, "qdwin-nested-probe: chrome_button was "
				"for handle %u, not the proxy %u\n",
				p.chrome_handle, handle);
			return 1;
		}

		struct wl_surface *popsurf =
			make_committed_surface(&p, 120, 80, 0xffcc2222u);
		if (!popsurf) {
			fprintf(stderr, "qdwin-nested-probe: popup surface "
				"allocation failed\n");
			return 2;
		}
		struct qdwin_popup_v1 *pop = qdwin_shell_v1_show_popup(
			p.shell, handle, popsurf, p.chrome_serial, 4, 4);
		if (!pop) {
			fprintf(stderr, "qdwin-nested-probe: show_popup "
				"returned NULL\n");
			return 2;
		}
		qdwin_popup_v1_add_listener(pop, &pop_listener, &p);
		if (roundtrip_err(&p, "show_popup", NULL, NULL) != 0) {
			fprintf(stderr, "qdwin-nested-probe: show_popup was "
				"refused (serial=%u) — no live popup to "
				"destroy under\n", p.chrome_serial);
			return 1;
		}

		/* The popup must still be LIVE at the destruction boundary. A
		 * popup already dismissed — by an outside press, or by an
		 * implementation that tears its own popup down immediately —
		 * would otherwise satisfy a sticky flag while the proxy path
		 * released nothing (codex r1). Re-check, then re-arm.
		 *
		 * This narrows the window; it does NOT establish causality, and
		 * the mode does not claim to (codex r2). Two sequences still
		 * reach 0 without the destroy having released the popup:
		 *   - an outside press processed after this sync but before the
		 *     server handles the destroy, whose `dismissed` is then
		 *     dispatched by the round-trip that follows it;
		 *   - an implementation that keeps send_dismissed but drops
		 *     qdwin_popup_teardown from the shared release routine,
		 *     which satisfies an EVENT-only oracle by construction.
		 * The protocol exposes no popup-created event and no view of
		 * server-side popup state, so closing this needs a new
		 * observation, not another round-trip. Tracked in
		 * todo/open-followups.md. The lane's mitigation is procedural:
		 * it injects exactly one click, before show_popup. */
		wl_display_roundtrip(p.display);
		if (p.popup_dismissed) {
			fprintf(stderr, "qdwin-nested-probe: popup was "
				"dismissed BEFORE the destroy — no live popup "
				"to destroy under\n");
			return 77;
		}
		p.popup_dismissed = 0;

		qdwin_nested_toplevel_v1_destroy(nt);
		if (roundtrip_err(&p, "nested toplevel destroy under a popup",
				  NULL, NULL) != 0)
			return 1;
		if (wait_for(&p, &p.popup_dismissed, NULL, 10) < 0) {
			fprintf(stderr, "qdwin-nested-probe: connection died "
				"after destroying a proxy under a popup\n");
			return 1;
		}
		if (!p.popup_dismissed) {
			fprintf(stderr, "qdwin-nested-probe: proxy destroy did "
				"NOT dismiss the popup — qdwin_popup::parent "
				"is left dangling at a freed toplevel\n");
			return 1;
		}
		if (p.toplevel_removed_count <= removed_before) {
			fprintf(stderr, "qdwin-nested-probe: destroy under a "
				"popup did not fire toplevel_removed "
				"(got %d, want >%d)\n",
				p.toplevel_removed_count, removed_before);
			return 1;
		}
		if (p.last_removed_handle != handle) {
			fprintf(stderr, "qdwin-nested-probe: removed handle %u "
				"!= the proxy %u\n",
				p.last_removed_handle, handle);
			return 1;
		}
		qdwin_shell_v1_nested_proxy_decision(p.shell, handle + 9999u, 0,
						     "probe-liveness");
		if (roundtrip_err(&p, "post-teardown liveness", NULL, NULL) != 0)
			return 1;
		printf("qdwin-nested-probe: proxy destroyed under a LIVE "
		       "chrome popup; dismissed fired; compositor alive "
		       "(handle=%u)\n", handle);
		return 0;
	}

	case M_DESTROY: {
		int removed_before = p.toplevel_removed_count;
		qdwin_nested_toplevel_v1_destroy(nt);
		if (roundtrip_err(&p, "nested toplevel destroy", NULL, NULL) != 0)
			return 1;
		wl_display_roundtrip(p.display);
		if (p.toplevel_removed_count <= removed_before) {
			fprintf(stderr, "qdwin-nested-probe: destroy did not "
				"fire toplevel_removed (got %d, want >%d)\n",
				p.toplevel_removed_count, removed_before);
			return 1;
		}
		if (p.last_removed_handle != handle) {
			fprintf(stderr, "qdwin-nested-probe: removed handle %u "
				"!= advertised handle %u\n",
				p.last_removed_handle, handle);
			return 1;
		}
		printf("qdwin-nested-probe: destroy tore down the proxy "
		       "(toplevel_removed handle=%u)\n", handle);
		return 0;
	}

	default:
		return 1;
	}
}
