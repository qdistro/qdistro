/*
 * qdwin-imethod-probe — live test client for qdwin's input-method-unstable-v2
 * implementation (Bucket A / P1; qdwin/qdwin.c). The privileged IME side of
 * the text-input plane.
 *
 * What it pins (the seat-gated, live half of the coverage; the headless
 * source invariants are in qdwin/test_input_method.py):
 *   - zwp_input_method_manager_v2 is advertised and binds from the authorized
 *     session client (the bind gate admits the unsandboxed session uid);
 *   - get_input_method(seat) needs a wl_seat — headless qdwin advertises none,
 *     so the functional path exits 5 (documented gate), like the text-input
 *     probe;
 *   - under a seat: a SECOND get_input_method on the same seat yields exactly
 *     one `unavailable` event and nothing else (one-IME-per-seat, per
 *     protocol — NOT a protocol error);
 *   - under a seat: grab_keyboard delivers a keymap (fd + size) AND a
 *     repeat_info before any key — proving the compositor wires the grab to
 *     the seat keyboard. (Actual key forwarding / composition needs synthetic
 *     hardware input + a paired text-input client and is a VM functional test.)
 *
 * Modes:
 *   --bind (default): bind the manager; if no seat, exit 5; else get_input_method
 *       and assert we are NOT immediately told `unavailable` (we are the sole
 *       IME), then a second get_input_method IS told `unavailable`.
 *   --grab: under a seat, get_input_method + grab_keyboard; assert keymap and
 *       repeat_info arrive. Seat-gated (exit 5 headless).
 *
 * Exit codes:
 *   0  expected postcondition held
 *   1  wrong event (unexpected unavailable, missing keymap/repeat_info, ...)
 *   2  setup error (no display / missing manager global)
 *   5  NO wl_seat advertised — get_input_method needs a seat, which only a
 *      seat-bearing backend (VM / RDP / DRM) provides. The manager global IS
 *      verified before this gate.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <wayland-client.h>
#include "input-method-unstable-v2-client-protocol.h"

static struct zwp_input_method_manager_v2 *manager;
static struct wl_seat *seat;
static int unavailable_count;
static int got_keymap;
static int got_repeat_info;

static void
reg_global(void *data, struct wl_registry *reg, uint32_t name,
	   const char *iface, uint32_t version)
{
	(void)data; (void)version;
	if (strcmp(iface, zwp_input_method_manager_v2_interface.name) == 0)
		manager = wl_registry_bind(
			reg, name, &zwp_input_method_manager_v2_interface, 1);
	else if (strcmp(iface, wl_seat_interface.name) == 0)
		seat = wl_registry_bind(reg, name, &wl_seat_interface, 1);
}

static void
reg_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{
	(void)data; (void)reg; (void)name;
}

static const struct wl_registry_listener reg_listener = {
	.global = reg_global,
	.global_remove = reg_global_remove,
};

/* zwp_input_method_v2 listener — we only care about `unavailable` here. */
static void im_activate(void *d, struct zwp_input_method_v2 *im) { (void)d; (void)im; }
static void im_deactivate(void *d, struct zwp_input_method_v2 *im) { (void)d; (void)im; }
static void im_surrounding_text(void *d, struct zwp_input_method_v2 *im,
				const char *t, uint32_t c, uint32_t a)
{ (void)d; (void)im; (void)t; (void)c; (void)a; }
static void im_text_change_cause(void *d, struct zwp_input_method_v2 *im,
				 uint32_t cause)
{ (void)d; (void)im; (void)cause; }
static void im_content_type(void *d, struct zwp_input_method_v2 *im,
			    uint32_t h, uint32_t p)
{ (void)d; (void)im; (void)h; (void)p; }
static void im_done(void *d, struct zwp_input_method_v2 *im) { (void)d; (void)im; }
static void im_unavailable(void *d, struct zwp_input_method_v2 *im)
{ (void)d; (void)im; unavailable_count++; }

static const struct zwp_input_method_v2_listener im_listener = {
	.activate = im_activate,
	.deactivate = im_deactivate,
	.surrounding_text = im_surrounding_text,
	.text_change_cause = im_text_change_cause,
	.content_type = im_content_type,
	.done = im_done,
	.unavailable = im_unavailable,
};

static void grab_keymap(void *d, struct zwp_input_method_keyboard_grab_v2 *g,
			uint32_t format, int32_t fd, uint32_t size)
{
	(void)d; (void)g; (void)format;
	if (fd >= 0 && size > 0) {
		got_keymap = 1;
		close(fd);
	}
}
static void grab_key(void *d, struct zwp_input_method_keyboard_grab_v2 *g,
		     uint32_t serial, uint32_t time, uint32_t key, uint32_t state)
{ (void)d; (void)g; (void)serial; (void)time; (void)key; (void)state; }
static void grab_modifiers(void *d, struct zwp_input_method_keyboard_grab_v2 *g,
			   uint32_t serial, uint32_t md, uint32_t ml,
			   uint32_t lk, uint32_t grp)
{ (void)d; (void)g; (void)serial; (void)md; (void)ml; (void)lk; (void)grp; }
static void grab_repeat_info(void *d, struct zwp_input_method_keyboard_grab_v2 *g,
			     int32_t rate, int32_t delay)
{ (void)d; (void)g; (void)rate; (void)delay; got_repeat_info = 1; }

static const struct zwp_input_method_keyboard_grab_v2_listener grab_listener = {
	.keymap = grab_keymap,
	.key = grab_key,
	.modifiers = grab_modifiers,
	.repeat_info = grab_repeat_info,
};

int
main(int argc, char *argv[])
{
	int do_grab = (argc > 1 && strcmp(argv[1], "--grab") == 0);
	struct wl_display *dpy;
	struct wl_registry *reg;
	struct zwp_input_method_v2 *im, *im2;
	struct zwp_input_method_keyboard_grab_v2 *grab;

	dpy = wl_display_connect(NULL);
	if (!dpy) {
		fprintf(stderr, "imethod-probe: no display\n");
		return 2;
	}
	reg = wl_display_get_registry(dpy);
	wl_registry_add_listener(reg, &reg_listener, NULL);
	wl_display_roundtrip(dpy);

	if (!manager) {
		fprintf(stderr, "imethod-probe: zwp_input_method_manager_v2 not "
				"advertised\n");
		return 2;
	}
	if (!seat) {
		/* Documented headless gate — no seat, no get_input_method. */
		fprintf(stderr, "imethod-probe: no wl_seat (headless) — manager "
				"advertised + bound; functional path is VM-only\n");
		return 5;
	}

	im = zwp_input_method_manager_v2_get_input_method(manager, seat);
	zwp_input_method_v2_add_listener(im, &im_listener, NULL);
	wl_display_roundtrip(dpy);

	if (unavailable_count != 0) {
		fprintf(stderr, "imethod-probe: sole IME was told `unavailable` "
				"(%d) — expected to be active\n", unavailable_count);
		return 1;
	}

	if (do_grab) {
		grab = zwp_input_method_v2_grab_keyboard(im);
		zwp_input_method_keyboard_grab_v2_add_listener(
			grab, &grab_listener, NULL);
		wl_display_roundtrip(dpy);
		if (!got_keymap) {
			fprintf(stderr, "imethod-probe: grab delivered no "
					"keymap\n");
			return 1;
		}
		if (!got_repeat_info) {
			fprintf(stderr, "imethod-probe: grab delivered no "
					"repeat_info\n");
			return 1;
		}
		printf("imethod-probe: grab OK (keymap + repeat_info delivered)\n");
		return 0;
	}

	/* One-IME-per-seat: a second get_input_method must get `unavailable`. */
	im2 = zwp_input_method_manager_v2_get_input_method(manager, seat);
	zwp_input_method_v2_add_listener(im2, &im_listener, NULL);
	wl_display_roundtrip(dpy);
	if (unavailable_count != 1) {
		fprintf(stderr, "imethod-probe: second IME unavailable_count=%d "
				"(expected exactly 1)\n", unavailable_count);
		return 1;
	}
	printf("imethod-probe: bind OK (sole IME active; second IME told "
	       "`unavailable` — one-per-seat)\n");
	return 0;
}
