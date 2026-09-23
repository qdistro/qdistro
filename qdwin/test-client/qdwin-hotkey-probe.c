/*
 * qdwin-hotkey-probe — test client for qdwin's v19 global-hotkey
 * registration edges (qdwin_handle_register_hotkey /
 * qdwin_handle_unregister_hotkey / qdwin_hotkey_handler /
 * qdwin_hotkeys_purge; qdwin/qdwin.c).
 *
 * What it pins (todo/codex-testing/under-tested-areas.md §4, the REGISTER
 * half — the real keypress->hotkey_pressed DELIVERY half needs input
 * injection and is VM-only; see tests/host/19-hotkey-edges.md):
 *   - register two DISTINCT ids with the SAME (mods,key) — both register
 *     deterministically (qdwin keys bindings by id, so duplicates across ids
 *     are independent entries, each with its own weston_binding);
 *   - re-registering the SAME id with a different (mods,key) REPLACES the
 *     binding (the handler destroys the prior entry before adding the new
 *     one — idempotent re-register);
 *   - unregister removes the binding (and an unknown id is a silent no-op);
 *   - key==0 (modifier-only) is a no-op that posts NO error and installs NO
 *     binding (the handler returns before the register log line).
 *
 * The probe binds qdwin_shell_v1 (v19) + bind_as_shell so it owns the shell
 * resource the register handlers require, issues the requests, and
 * round-trips. The observable postconditions are weston-log lines
 * (`register_hotkey id=… mods=… key=…` / `unregister_hotkey id=…`) which the
 * companion scenario asserts; the probe's job is to drive the exact request
 * sequence and confirm none of them raise a protocol error.
 *
 * Modes:
 *   --dup-across-ids   register id=1 and id=2 with the SAME (mods,key).
 *   --replace          register id=1 (mods=A,key=X) then id=1 (mods=B,key=Y).
 *   --unregister       register id=1, then unregister id=1, then unregister a
 *                      never-registered id=99 (silent no-op).
 *   --modonly          register id=1 with key=0 (modifier-only) — no-op,
 *                      no error.
 *
 * Exit codes:
 *   0  the request sequence round-tripped with no protocol error
 *   1  an unexpected protocol error occurred
 *   2  setup/other error (no display, shell not advertised, bind failed)
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

/* linux input keycodes used (linux/input-event-codes.h values). */
#define KEY_E 18
#define KEY_L 38
#define KEY_SPACE 57

/* qdwin_shell_v1.modifier bits. */
#define MOD_CTRL  1u
#define MOD_ALT   2u
#define MOD_SUPER 4u

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;
	struct qdwin_shell_v1 *shell;
	uint32_t shell_name, shell_version;
	int saw_shell, got_hello, got_hotkey;
};

static void l_hello(void *d, struct qdwin_shell_v1 *s, uint32_t uid)
{ struct probe *p = d; (void)s; (void)uid; p->got_hello = 1; }
static void l_toplevel_added(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			     uint32_t ou, const char *a, const char *t,
			     uint32_t x)
{ (void)d; (void)s; (void)h; (void)ou; (void)a; (void)t; (void)x; }
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
{ (void)d; (void)s; (void)h; }
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
static void l_idle_lock_hint(void *d, struct qdwin_shell_v1 *s, uint32_t r)
{ (void)d; (void)s; (void)r; }
static void l_nested_pending(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			     const char *a, uint32_t u)
{ (void)d; (void)s; (void)h; (void)a; (void)u; }
static void l_nested_pixsrc(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			    const char *pw, const char *is)
{ (void)d; (void)s; (void)h; (void)pw; (void)is; }
static void l_overlay_key(void *d, struct qdwin_shell_v1 *s, uint32_t role,
			  uint32_t sym, const char *utf8, uint32_t st)
{ (void)d; (void)s; (void)role; (void)sym; (void)utf8; (void)st; }
static void l_selection_set(void *d, struct qdwin_shell_v1 *s,
			    const char *sn, uint32_t sh, const char *mc,
			    uint32_t pri)
{ (void)d; (void)s; (void)sn; (void)sh; (void)mc; (void)pri; }
static void l_activation_pending(void *d, struct qdwin_shell_v1 *s, uint32_t h,
				 uint32_t sh, uint32_t th, const char *a)
{ (void)d; (void)s; (void)h; (void)sh; (void)th; (void)a; }
static void l_toplevel_security_context(void *d, struct qdwin_shell_v1 *s,
					uint32_t h, const char *e,
					const char *a, const char *i)
{ (void)d; (void)s; (void)h; (void)e; (void)a; (void)i; }
static void l_seat_focus_changed(void *d, struct qdwin_shell_v1 *s,
				 const char *sn, uint32_t fh)
{ (void)d; (void)s; (void)sn; (void)fh; }
static void l_data_offer_receive_pending(void *d, struct qdwin_shell_v1 *s,
					 uint32_t rh, const char *sn,
					 uint32_t sh, uint32_t th,
					 const char *mt)
{ (void)d; (void)s; (void)rh; (void)sn; (void)sh; (void)th; (void)mt; }
static void l_hotkey_pressed(void *d, struct qdwin_shell_v1 *s, uint32_t id)
{ struct probe *p = d; (void)s; (void)id; p->got_hotkey = 1; }

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
	.toplevel_security_context = l_toplevel_security_context,
	.seat_focus_changed = l_seat_focus_changed,
	.data_offer_receive_pending = l_data_offer_receive_pending,
	.hotkey_pressed     = l_hotkey_pressed,
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
		/* v19 = register_hotkey/unregister_hotkey/hotkey_pressed. */
		p->shell_version = version < 19 ? version : 19;
	}
}
static void on_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {
	.global = on_global, .global_remove = on_global_remove,
};

static int
roundtrip_err(struct probe *p, const char *what)
{
	int rc = wl_display_roundtrip(p->display);
	int err = wl_display_get_error(p->display);
	if (rc < 0 || err != 0) {
		uint32_t obj_id = 0, code = 0;
		const struct wl_interface *iface = NULL;
		code = wl_display_get_protocol_error(p->display, &iface, &obj_id);
		fprintf(stderr, "qdwin-hotkey-probe: %s ERROR (errno=%d, proto "
			"code=%u on %s#%u)\n", what, err, code,
			iface ? iface->name : "(unknown)", obj_id);
		return err ? err : 1;
	}
	return 0;
}

enum mode { M_DUP, M_REPLACE, M_UNREG, M_MODONLY };

int main(int argc, char *argv[])
{
	enum mode mode = M_DUP;
	for (int i = 1; i < argc; i++) {
		if      (!strcmp(argv[i], "--dup-across-ids")) mode = M_DUP;
		else if (!strcmp(argv[i], "--replace"))        mode = M_REPLACE;
		else if (!strcmp(argv[i], "--unregister"))     mode = M_UNREG;
		else if (!strcmp(argv[i], "--modonly"))        mode = M_MODONLY;
	}

	struct probe p = {0};
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-hotkey-probe: wl_display_connect "
			"failed: %s\n", strerror(errno));
		return 2;
	}
	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);
	wl_display_roundtrip(p.display);

	if (!p.saw_shell) {
		fprintf(stderr, "qdwin-hotkey-probe: qdwin_shell_v1 not "
			"advertised\n");
		return 2;
	}
	if (p.shell_version < 19) {
		fprintf(stderr, "qdwin-hotkey-probe: qdwin_shell_v1 v%u < 19 "
			"(no register_hotkey)\n", p.shell_version);
		return 2;
	}
	p.shell = wl_registry_bind(p.registry, p.shell_name,
				   &qdwin_shell_v1_interface, p.shell_version);
	qdwin_shell_v1_add_listener(p.shell, &shell_listener, &p);
	qdwin_shell_v1_bind_as_shell(p.shell);
	if (roundtrip_err(&p, "bind_as_shell") != 0)
		return 1;
	if (!p.got_hello) {
		fprintf(stderr, "qdwin-hotkey-probe: no hello\n");
		return 1;
	}

	switch (mode) {
	case M_DUP:
		/* Two distinct ids, same (mods,key). qdwin keys by id, so both
		 * register independently — deterministic, not a collision. */
		qdwin_shell_v1_register_hotkey(p.shell, 1, MOD_CTRL, KEY_E);
		qdwin_shell_v1_register_hotkey(p.shell, 2, MOD_CTRL, KEY_E);
		if (roundtrip_err(&p, "dup register") != 0)
			return 1;
		printf("qdwin-hotkey-probe: registered id=1 and id=2 with "
		       "same Ctrl+E\n");
		return 0;

	case M_REPLACE:
		/* Same id, different combos: the second replaces the first. */
		qdwin_shell_v1_register_hotkey(p.shell, 1, MOD_CTRL, KEY_E);
		if (roundtrip_err(&p, "register #1") != 0)
			return 1;
		qdwin_shell_v1_register_hotkey(p.shell, 1, MOD_ALT, KEY_L);
		if (roundtrip_err(&p, "register #1 replace") != 0)
			return 1;
		printf("qdwin-hotkey-probe: re-registered id=1 (Ctrl+E -> "
		       "Alt+L)\n");
		return 0;

	case M_UNREG:
		qdwin_shell_v1_register_hotkey(p.shell, 1, MOD_SUPER, KEY_SPACE);
		if (roundtrip_err(&p, "register before unreg") != 0)
			return 1;
		qdwin_shell_v1_unregister_hotkey(p.shell, 1);
		if (roundtrip_err(&p, "unregister id=1") != 0)
			return 1;
		/* Unknown id: silent no-op, must not error. */
		qdwin_shell_v1_unregister_hotkey(p.shell, 99);
		if (roundtrip_err(&p, "unregister unknown id=99") != 0)
			return 1;
		printf("qdwin-hotkey-probe: registered+unregistered id=1, "
		       "unknown id=99 was a silent no-op\n");
		return 0;

	case M_MODONLY:
		/* key=0 (modifier-only): the handler returns before installing
		 * a binding and posts NO error. */
		qdwin_shell_v1_register_hotkey(p.shell, 1, MOD_CTRL, 0);
		if (roundtrip_err(&p, "modifier-only register (key=0)") != 0) {
			fprintf(stderr, "qdwin-hotkey-probe: key=0 register "
				"raised an error (should be a no-op)\n");
			return 1;
		}
		/* Liveness: the compositor stays alive after the no-op. */
		if (wl_display_roundtrip(p.display) < 0)
			return 1;
		printf("qdwin-hotkey-probe: modifier-only (key=0) register was "
		       "a clean no-op\n");
		return 0;
	}
	return 1;
}
