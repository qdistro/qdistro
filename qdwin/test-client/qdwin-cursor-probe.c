/*
 * qdwin-cursor-probe — test client for qdwin's wp_cursor_shape_v1
 * implementation (qdwin_cursor_shape_manager_get_pointer /
 * qdwin_cursor_shape_device_set_shape; qdwin/qdwin.c).
 *
 * What it pins (todo/codex-testing/under-tested-areas.md §3 cursor-shape):
 *   - an out-of-range shape (0 or > shape_all_resize) is REFUSED with the
 *     dedicated invalid_shape protocol error (fail-closed validation);
 *   - a valid shape is accepted and logged (theme hit OR theme miss — both
 *     are accepted; the miss path must not error);
 *   - repeated set_shape calls on one device are all accepted (no leak / no
 *     spurious error across a burst of shape changes);
 *   - set_shape after the device's wl_pointer focus changes still works.
 *
 * The cursor-shape device is obtained from wp_cursor_shape_manager_v1.
 * get_pointer(id, wl_pointer). qdwin ignores the pointer_resource argument
 * (it resolves the first compositor pointer itself), but the protocol still
 * requires a real wl_pointer object, so the probe binds wl_seat and takes
 * its wl_pointer. Headless weston exposes a virtual seat with a pointer
 * capability, so this resolves cleanly without an input backend.
 *
 * Modes:
 *   --valid        get_pointer + set_shape(default arrow). Accepted.
 *   --invalid-low  set_shape(0). Refused with invalid_shape (=1).
 *   --invalid-high set_shape(shape_all_resize + 1). Refused with
 *                  invalid_shape (=1).
 *   --burst        get_pointer + 6 set_shape calls cycling shapes. All
 *                  accepted, connection survives.
 *   --refocus      get_pointer, set_shape, then create a SECOND device and
 *                  set_shape on it (simulates cursor after focus change to
 *                  another surface/device). Both accepted.
 *
 * Exit codes:
 *   0  expected-accept path completed cleanly
 *   3  --invalid-*: set_shape raised exactly invalid_shape on
 *      wp_cursor_shape_device_v1 (the PASS signal for the reject modes)
 *   1  an expected accept failed, or an UNEXPECTED protocol error
 *   2  setup/other error (no display, cursor-shape manager not advertised)
 *   5  NO wl_seat is advertised — the cursor-shape DEVICE cannot be built
 *      (get_pointer needs a wl_pointer, which only a wl_seat provides). The
 *      weston headless backend has no input backend and exposes no seat, so
 *      this exit is the EXPECTED outcome of the headless harness; the
 *      cursor-shape set_shape surface is reachable only under a backend with
 *      a seat (a VM with a real/virtual input device). See
 *      tests/host/17-cursor-shape.md.
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
#include "cursor-shape-v1-client-protocol.h"

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_seat *seat;
	struct wl_pointer *pointer;
	struct wp_cursor_shape_manager_v1 *mgr;
	uint32_t seat_name, seat_version;
	uint32_t mgr_name, mgr_version;
	int saw_seat, saw_mgr;
	uint32_t seat_caps;
};

static void seat_caps(void *d, struct wl_seat *s, uint32_t caps)
{ struct probe *p = d; (void)s; p->seat_caps = caps; }
static void seat_name(void *d, struct wl_seat *s, const char *n)
{ (void)d; (void)s; (void)n; }
static const struct wl_seat_listener seat_listener = {
	.capabilities = seat_caps, .name = seat_name,
};

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct probe *p = data;
	(void)reg;
	if (strcmp(interface, wl_seat_interface.name) == 0) {
		p->saw_seat = 1;
		p->seat_name = name;
		p->seat_version = version < 5 ? version : 5;
	} else if (strcmp(interface,
			  wp_cursor_shape_manager_v1_interface.name) == 0) {
		p->saw_mgr = 1;
		p->mgr_name = name;
		p->mgr_version = version < 1 ? version : 1;
	}
}
static void on_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {
	.global = on_global, .global_remove = on_global_remove,
};

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
			"qdwin-cursor-probe: %s ERROR (errno=%d, proto code=%u "
			"on %s#%u)\n",
			what, err, code, iface ? iface->name : "(unknown)",
			obj_id);
		return err ? err : 1;
	}
	return 0;
}

static struct wp_cursor_shape_device_v1 *
make_device(struct probe *p)
{
	return wp_cursor_shape_manager_v1_get_pointer(p->mgr, p->pointer);
}

enum mode { M_VALID, M_LOW, M_HIGH, M_BURST, M_REFOCUS };

int main(int argc, char *argv[])
{
	enum mode mode = M_VALID;
	for (int i = 1; i < argc; i++) {
		if      (!strcmp(argv[i], "--valid"))        mode = M_VALID;
		else if (!strcmp(argv[i], "--invalid-low"))  mode = M_LOW;
		else if (!strcmp(argv[i], "--invalid-high")) mode = M_HIGH;
		else if (!strcmp(argv[i], "--burst"))        mode = M_BURST;
		else if (!strcmp(argv[i], "--refocus"))      mode = M_REFOCUS;
	}

	struct probe p = {0};
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-cursor-probe: wl_display_connect "
			"failed: %s\n", strerror(errno));
		return 2;
	}
	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);
	wl_display_roundtrip(p.display);

	if (!p.saw_mgr) {
		fprintf(stderr, "qdwin-cursor-probe: "
			"wp_cursor_shape_manager_v1 not advertised\n");
		return 2;
	}
	if (!p.saw_seat) {
		/* No seat → no wl_pointer → no cursor-shape device. This is the
		 * expected headless outcome (distinct exit 5 so the scenario can
		 * tell it apart from a real failure / setup error). */
		fprintf(stderr, "qdwin-cursor-probe: wl_seat not advertised — "
			"cursor-shape device unreachable without a seat "
			"(headless has no input backend)\n");
		return 5;
	}

	p.seat = wl_registry_bind(p.registry, p.seat_name, &wl_seat_interface,
				  p.seat_version);
	wl_seat_add_listener(p.seat, &seat_listener, &p);
	p.mgr = wl_registry_bind(p.registry, p.mgr_name,
				 &wp_cursor_shape_manager_v1_interface,
				 p.mgr_version);
	wl_display_roundtrip(p.display);

	/* A wl_pointer is needed as the get_pointer argument. wl_seat_get_pointer
	 * is legal regardless of the advertised capability bit (the request
	 * just creates the resource); qdwin resolves the real pointer itself. */
	p.pointer = wl_seat_get_pointer(p.seat);
	if (!p.pointer) {
		fprintf(stderr, "qdwin-cursor-probe: wl_seat_get_pointer "
			"returned NULL\n");
		return 2;
	}

	struct wp_cursor_shape_device_v1 *dev = make_device(&p);
	if (!dev) {
		fprintf(stderr, "qdwin-cursor-probe: get_pointer NULL\n");
		return 2;
	}
	if (roundtrip_err(&p, "get_pointer", NULL, NULL) != 0)
		return 1;

	if (mode == M_LOW || mode == M_HIGH) {
		uint32_t shape = (mode == M_LOW)
			? 0
			: (WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALL_RESIZE + 1);
		uint32_t code = 0;
		const struct wl_interface *iface = NULL;
		wp_cursor_shape_device_v1_set_shape(dev, 1 /*serial*/, shape);
		int err = roundtrip_err(&p, "set_shape(invalid)", &code, &iface);
		if (err == 0) {
			fprintf(stderr, "qdwin-cursor-probe: invalid shape %u "
				"was NOT rejected\n", shape);
			return 1;
		}
		if (iface != &wp_cursor_shape_device_v1_interface ||
		    code != WP_CURSOR_SHAPE_DEVICE_V1_ERROR_INVALID_SHAPE) {
			fprintf(stderr, "qdwin-cursor-probe: invalid shape got "
				"code=%u on %s, want invalid_shape=%d on "
				"wp_cursor_shape_device_v1\n", code,
				iface ? iface->name : "(none)",
				WP_CURSOR_SHAPE_DEVICE_V1_ERROR_INVALID_SHAPE);
			return 1;
		}
		printf("qdwin-cursor-probe: invalid shape %u REJECTED with "
		       "invalid_shape\n", shape);
		return 3;
	}

	if (mode == M_BURST) {
		uint32_t shapes[] = {
			WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_DEFAULT,
			WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_POINTER,
			WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_TEXT,
			WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_WAIT,
			WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_GRAB,
			WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALL_RESIZE,
		};
		for (unsigned i = 0; i < sizeof shapes / sizeof shapes[0]; i++) {
			wp_cursor_shape_device_v1_set_shape(dev, i + 1,
							    shapes[i]);
			if (roundtrip_err(&p, "set_shape(burst)", NULL, NULL) != 0)
				return 1;
		}
		printf("qdwin-cursor-probe: 6-shape burst accepted, connection "
		       "alive\n");
		return 0;
	}

	if (mode == M_REFOCUS) {
		wp_cursor_shape_device_v1_set_shape(dev, 1,
			WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_POINTER);
		if (roundtrip_err(&p, "set_shape #1", NULL, NULL) != 0)
			return 1;
		/* Second device (cursor after a focus change to another
		 * surface gets a fresh wp_cursor_shape_device). */
		struct wp_cursor_shape_device_v1 *dev2 = make_device(&p);
		if (!dev2 || roundtrip_err(&p, "get_pointer #2", NULL, NULL) != 0)
			return 1;
		wp_cursor_shape_device_v1_set_shape(dev2, 2,
			WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_TEXT);
		if (roundtrip_err(&p, "set_shape #2", NULL, NULL) != 0)
			return 1;
		printf("qdwin-cursor-probe: set_shape across a second device "
		       "(refocus) accepted\n");
		return 0;
	}

	/* M_VALID */
	wp_cursor_shape_device_v1_set_shape(dev, 1,
		WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_DEFAULT);
	if (roundtrip_err(&p, "set_shape(default)", NULL, NULL) != 0)
		return 1;
	printf("qdwin-cursor-probe: valid set_shape(default) accepted\n");
	return 0;
}
