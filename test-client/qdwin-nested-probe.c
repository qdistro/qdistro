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

#include <wayland-client.h>
#include "qdwin-shell-v1-client-protocol.h"
#include "qdwin-nested-v1-client-protocol.h"

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;

	uint32_t shell_name, shell_version;
	uint32_t mgr_name, mgr_version;
	int saw_shell, saw_mgr;

	/* Only --destroy-with-move needs this: a move-drag requires a seat
	 * with a pointer, which the headless backend's inert seat lacks. */
	struct wl_seat *seat;
	int seat_has_pointer;

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
{ (void)d; (void)s; (void)h; (void)x; (void)y; (void)w; (void)ht; }
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
};

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
		/* Bind v8 — enough for nested_proxy_pending + decision. */
		p->shell_version = version < 8 ? version : 8;
	} else if (strcmp(interface,
			  qdwin_nested_manager_v1_interface.name) == 0) {
		p->saw_mgr = 1;
		p->mgr_name = name;
		p->mgr_version = version < 1 ? version : 1;
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

enum mode {
	M_ADVERTISE, M_BIND, M_ALLOW, M_DENY, M_DEFER,
	M_STALE, M_DOUBLE, M_DESTROY, M_DESTROY_MOVE, M_MALFORMED
};

int main(int argc, char *argv[])
{
	enum mode mode = M_ADVERTISE;
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
		else if (!strcmp(argv[i], "--malformed"))      mode = M_MALFORMED;
	}

	struct probe p = {0};
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
