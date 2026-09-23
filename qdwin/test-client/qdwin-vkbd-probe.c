/*
 * qdwin-vkbd-probe — live test client for qdwin's virtual-keyboard-unstable-v1
 * implementation (Bucket A / P1 companion; qdwin/qdwin.c). The privileged
 * key-injection side a grabbing IME uses to pass non-composed keys back to apps.
 *
 * What it pins (the seat-gated, live half of the coverage; the headless source
 * invariants are in qdwin/test_virtual_keyboard.py):
 *   - zwp_virtual_keyboard_manager_v1 is advertised and binds from the
 *     authorized session client (the bind gate admits the unsandboxed session
 *     uid — the SAME gate as input-method-v2);
 *   - create_virtual_keyboard(seat) needs a wl_seat — headless qdwin advertises
 *     none, so the functional path exits 5 (documented gate), like the IME probe;
 *   - default mode: a virtual keyboard that uploads the seat's own keymap (as a
 *     real IME does — it got that keymap from the seat keyboard grab) may inject
 *     key + modifiers events with NO protocol error;
 *   - --nokeymap mode: injecting a key BEFORE a keymap is a protocol error
 *     (no_keymap) — the fail-closed contract is enforced live.
 *
 * Actual delivery of the injected key into a focused app (and the resulting
 * character) needs a paired, focused text client and is a VM functional test;
 * this probe only proves the protocol exchange.
 *
 * Exit codes:
 *   0  expected postcondition held
 *   1  wrong outcome (unexpected protocol error, or a missing expected one)
 *   2  setup error (no display / missing manager global / no keymap from seat)
 *   5  NO wl_seat advertised — create_virtual_keyboard needs a seat, which only
 *      a seat-bearing backend (VM / RDP / DRM) provides. The manager global IS
 *      verified before this gate.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <linux/input-event-codes.h>
#include <wayland-client.h>
#include "virtual-keyboard-unstable-v1-client-protocol.h"

static struct zwp_virtual_keyboard_manager_v1 *manager;
static struct wl_seat *seat;
static struct wl_keyboard *keyboard;
static int km_format = -1;
static int km_fd = -1;
static uint32_t km_size;

static void
kbd_keymap(void *d, struct wl_keyboard *k, uint32_t format, int32_t fd,
	   uint32_t size)
{
	(void)d; (void)k;
	/* Keep the first keymap the compositor hands us; we re-upload it to the
	 * virtual keyboard, exactly as an IME re-uses the grabbed keymap. */
	if (km_fd < 0) {
		km_format = (int)format;
		km_fd = fd;
		km_size = size;
	} else if (fd >= 0) {
		close(fd);
	}
}
static void kbd_enter(void *d, struct wl_keyboard *k, uint32_t s,
		      struct wl_surface *su, struct wl_array *ks)
{ (void)d; (void)k; (void)s; (void)su; (void)ks; }
static void kbd_leave(void *d, struct wl_keyboard *k, uint32_t s,
		      struct wl_surface *su)
{ (void)d; (void)k; (void)s; (void)su; }
static void kbd_key(void *d, struct wl_keyboard *k, uint32_t s, uint32_t t,
		    uint32_t key, uint32_t st)
{ (void)d; (void)k; (void)s; (void)t; (void)key; (void)st; }
static void kbd_modifiers(void *d, struct wl_keyboard *k, uint32_t s,
			  uint32_t md, uint32_t ml, uint32_t lk, uint32_t g)
{ (void)d; (void)k; (void)s; (void)md; (void)ml; (void)lk; (void)g; }
static void kbd_repeat_info(void *d, struct wl_keyboard *k, int32_t r, int32_t dl)
{ (void)d; (void)k; (void)r; (void)dl; }

static const struct wl_keyboard_listener kbd_listener = {
	.keymap = kbd_keymap,
	.enter = kbd_enter,
	.leave = kbd_leave,
	.key = kbd_key,
	.modifiers = kbd_modifiers,
	.repeat_info = kbd_repeat_info,
};

static void
seat_capabilities(void *d, struct wl_seat *s, uint32_t caps)
{
	(void)d;
	if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !keyboard) {
		keyboard = wl_seat_get_keyboard(s);
		wl_keyboard_add_listener(keyboard, &kbd_listener, NULL);
	}
}
static void seat_name(void *d, struct wl_seat *s, const char *n)
{ (void)d; (void)s; (void)n; }

static const struct wl_seat_listener seat_listener = {
	.capabilities = seat_capabilities,
	.name = seat_name,
};

static void
reg_global(void *data, struct wl_registry *reg, uint32_t name,
	   const char *iface, uint32_t version)
{
	(void)data; (void)version;
	if (strcmp(iface, zwp_virtual_keyboard_manager_v1_interface.name) == 0)
		manager = wl_registry_bind(
			reg, name, &zwp_virtual_keyboard_manager_v1_interface, 1);
	else if (strcmp(iface, wl_seat_interface.name) == 0) {
		seat = wl_registry_bind(reg, name, &wl_seat_interface, 5);
		wl_seat_add_listener(seat, &seat_listener, NULL);
	}
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

int
main(int argc, char *argv[])
{
	int nokeymap = (argc > 1 && strcmp(argv[1], "--nokeymap") == 0);
	struct wl_display *dpy;
	struct wl_registry *reg;
	struct zwp_virtual_keyboard_v1 *vk;
	int rt;

	dpy = wl_display_connect(NULL);
	if (!dpy) {
		fprintf(stderr, "vkbd-probe: no display\n");
		return 2;
	}
	reg = wl_display_get_registry(dpy);
	wl_registry_add_listener(reg, &reg_listener, NULL);
	wl_display_roundtrip(dpy);  /* registry */
	wl_display_roundtrip(dpy);  /* seat caps → wl_keyboard */
	/* The wl_keyboard.keymap event is delivered asynchronously after
	 * get_keyboard; pump a few more roundtrips until it arrives (under a real
	 * seat) so the default mode has the keymap to re-upload. Bounded so a
	 * seatless/keyboardless backend still falls through to the gate below. */
	for (int i = 0; i < 10 && seat && km_fd < 0; i++)
		wl_display_roundtrip(dpy);

	if (!manager) {
		fprintf(stderr, "vkbd-probe: zwp_virtual_keyboard_manager_v1 not "
				"advertised\n");
		return 2;
	}
	if (!seat) {
		/* Documented headless gate — no seat, no create_virtual_keyboard. */
		fprintf(stderr, "vkbd-probe: no wl_seat (headless) — manager "
				"advertised + bound; functional path is VM-only\n");
		return 5;
	}

	vk = zwp_virtual_keyboard_manager_v1_create_virtual_keyboard(manager, seat);

	if (nokeymap) {
		/* Fail-closed contract: a key before any keymap is a protocol
		 * error (no_keymap). The roundtrip must then fail. */
		zwp_virtual_keyboard_v1_key(vk, 0, KEY_A,
					    WL_KEYBOARD_KEY_STATE_PRESSED);
		rt = wl_display_roundtrip(dpy);
		if (rt != -1) {
			fprintf(stderr, "vkbd-probe: key-before-keymap did NOT "
					"raise a protocol error (got rt=%d)\n", rt);
			return 1;
		}
		printf("vkbd-probe: no_keymap OK (key before keymap rejected)\n");
		return 0;
	}

	if (km_fd < 0 || km_format < 0 || km_size == 0) {
		fprintf(stderr, "vkbd-probe: seat delivered no keymap to re-upload "
				"(no keyboard capability?)\n");
		return 2;
	}

	/* Re-upload the seat's keymap (what a real IME does), then inject a key
	 * down/up and an explicit modifier update. None of these should raise a
	 * protocol error. */
	zwp_virtual_keyboard_v1_keymap(vk, (uint32_t)km_format, km_fd, km_size);
	zwp_virtual_keyboard_v1_key(vk, 0, KEY_A, WL_KEYBOARD_KEY_STATE_PRESSED);
	zwp_virtual_keyboard_v1_key(vk, 1, KEY_A, WL_KEYBOARD_KEY_STATE_RELEASED);
	zwp_virtual_keyboard_v1_modifiers(vk, 0, 0, 0, 0);
	rt = wl_display_roundtrip(dpy);
	if (rt == -1) {
		fprintf(stderr, "vkbd-probe: protocol error during inject "
				"(err=%d)\n", wl_display_get_error(dpy));
		return 1;
	}
	printf("vkbd-probe: inject OK (keymap + key + modifiers, no protocol "
	       "error)\n");
	return 0;
}
