/*
 * qdwin-fractional-probe — test client for qdwin's wp_fractional_scale_v1
 * implementation (qdwin_fractional_scale_manager_get,
 * qdwin_fractional_scale_push, qdwin_compute_preferred_scale_for_surface;
 * qdwin/qdwin.c).
 *
 * What it pins (todo/codex-testing/under-tested-areas.md §3 fractional-scale):
 *   - get_fractional_scale(surface) immediately delivers a preferred_scale
 *     event (the initial broadcast), and the value matches the compositor's
 *     computed scale (120 = 1.0x headless default, or QDWIN_FRACTIONAL_SCALE
 *     when set — e.g. 180 = 1.5x, a genuinely non-integer scale);
 *   - a surface commit re-evaluates and re-pushes ONLY when the value
 *     changed (dedup): committing with an unchanged scale sends nothing,
 *     so the client must NOT see a duplicate preferred_scale;
 *   - the object is reference-correct: destroy is clean, no crash.
 *
 * This path needs only a wl_compositor surface — no wl_seat — so it runs
 * fully headless. The non-integer scale is forced through the documented
 * QDWIN_FRACTIONAL_SCALE env knob (qdwin clamps it to 30..960; the probe
 * passes the EXPECTED value on argv so the assertion is self-contained).
 *
 * Modes:
 *   --initial            get_fractional_scale; assert exactly one
 *                        preferred_scale with the expected value. Default.
 *   --expect=N           override the value the probe asserts (matches the
 *                        QDWIN_FRACTIONAL_SCALE the harness set). Default 120.
 *   --dedup              get + commit twice with no scale change; assert the
 *                        initial preferred_scale fires once and the two
 *                        commits send NO further preferred_scale (dedup).
 *   --multi              two get_fractional_scale on two surfaces; assert
 *                        BOTH receive the initial preferred_scale (broadcast
 *                        reaches every tracked object).
 *
 * Exit codes:
 *   0  expected postcondition held
 *   1  wrong scale value, missing/duplicate event, or unexpected error
 *   2  setup/other error (no display, manager/compositor not advertised)
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
#include "fractional-scale-v1-client-protocol.h"

struct fs_obj {
	struct wp_fractional_scale_v1 *res;
	int events;
	uint32_t last_scale;
};

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_compositor *compositor;
	struct wp_fractional_scale_manager_v1 *mgr;
	uint32_t comp_name, comp_version, mgr_name, mgr_version;
	int saw_comp, saw_mgr;
};

static void
on_preferred_scale(void *data, struct wp_fractional_scale_v1 *r, uint32_t scale)
{
	struct fs_obj *o = data;
	(void)r;
	o->events++;
	o->last_scale = scale;
}
static const struct wp_fractional_scale_v1_listener fs_listener = {
	.preferred_scale = on_preferred_scale,
};

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct probe *p = data;
	(void)reg;
	if (strcmp(interface, wl_compositor_interface.name) == 0) {
		p->saw_comp = 1;
		p->comp_name = name;
		p->comp_version = version < 4 ? version : 4;
	} else if (strcmp(interface,
			  wp_fractional_scale_manager_v1_interface.name) == 0) {
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

enum mode { M_INITIAL, M_DEDUP, M_MULTI };

int main(int argc, char *argv[])
{
	enum mode mode = M_INITIAL;
	uint32_t expect = 120;
	for (int i = 1; i < argc; i++) {
		if      (!strcmp(argv[i], "--initial")) mode = M_INITIAL;
		else if (!strcmp(argv[i], "--dedup"))   mode = M_DEDUP;
		else if (!strcmp(argv[i], "--multi"))   mode = M_MULTI;
		else if (!strncmp(argv[i], "--expect=", 9))
			expect = (uint32_t)strtoul(argv[i] + 9, NULL, 10);
	}

	struct probe p = {0};
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-fractional-probe: wl_display_connect "
			"failed: %s\n", strerror(errno));
		return 2;
	}
	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);
	wl_display_roundtrip(p.display);

	if (!p.saw_mgr) {
		fprintf(stderr, "qdwin-fractional-probe: "
			"wp_fractional_scale_manager_v1 not advertised\n");
		return 2;
	}
	if (!p.saw_comp) {
		fprintf(stderr, "qdwin-fractional-probe: wl_compositor not "
			"advertised\n");
		return 2;
	}
	p.compositor = wl_registry_bind(p.registry, p.comp_name,
					&wl_compositor_interface, p.comp_version);
	p.mgr = wl_registry_bind(p.registry, p.mgr_name,
				 &wp_fractional_scale_manager_v1_interface,
				 p.mgr_version);
	wl_display_roundtrip(p.display);

	struct wl_surface *s1 = wl_compositor_create_surface(p.compositor);
	struct fs_obj o1 = {0};
	o1.res = wp_fractional_scale_manager_v1_get_fractional_scale(p.mgr, s1);
	wp_fractional_scale_v1_add_listener(o1.res, &fs_listener, &o1);
	/* The initial preferred_scale is queued synchronously inside
	 * get_fractional_scale; a roundtrip drains it. */
	wl_display_roundtrip(p.display);

	if (wl_display_get_error(p.display) != 0) {
		fprintf(stderr, "qdwin-fractional-probe: protocol error after "
			"get_fractional_scale\n");
		return 1;
	}
	if (o1.events != 1) {
		fprintf(stderr, "qdwin-fractional-probe: expected exactly 1 "
			"initial preferred_scale, got %d\n", o1.events);
		return 1;
	}
	if (o1.last_scale != expect) {
		fprintf(stderr, "qdwin-fractional-probe: initial scale=%u, "
			"expected %u\n", o1.last_scale, expect);
		return 1;
	}

	if (mode == M_INITIAL) {
		printf("qdwin-fractional-probe: initial preferred_scale=%u as "
		       "expected\n", o1.last_scale);
		return 0;
	}

	if (mode == M_DEDUP) {
		/* Commit twice with no output/scale change. qdwin re-evaluates
		 * on each commit but qdwin_fractional_scale_push dedups against
		 * last_sent_scale, so NO further preferred_scale should arrive. */
		int before = o1.events;
		for (int i = 0; i < 2; i++) {
			wl_surface_commit(s1);
			wl_display_roundtrip(p.display);
		}
		if (wl_display_get_error(p.display) != 0) {
			fprintf(stderr, "qdwin-fractional-probe: error during "
				"dedup commits\n");
			return 1;
		}
		if (o1.events != before) {
			fprintf(stderr, "qdwin-fractional-probe: dedup FAILED — "
				"got %d extra preferred_scale event(s) after "
				"unchanged commits (want 0)\n",
				o1.events - before);
			return 1;
		}
		printf("qdwin-fractional-probe: no duplicate preferred_scale "
		       "across 2 unchanged commits (dedup holds, scale=%u)\n",
		       o1.last_scale);
		return 0;
	}

	/* M_MULTI */
	struct wl_surface *s2 = wl_compositor_create_surface(p.compositor);
	struct fs_obj o2 = {0};
	o2.res = wp_fractional_scale_manager_v1_get_fractional_scale(p.mgr, s2);
	wp_fractional_scale_v1_add_listener(o2.res, &fs_listener, &o2);
	wl_display_roundtrip(p.display);
	if (wl_display_get_error(p.display) != 0) {
		fprintf(stderr, "qdwin-fractional-probe: error on 2nd "
			"get_fractional_scale\n");
		return 1;
	}
	if (o2.events != 1 || o2.last_scale != expect) {
		fprintf(stderr, "qdwin-fractional-probe: 2nd object events=%d "
			"scale=%u (want 1 event, scale=%u)\n",
			o2.events, o2.last_scale, expect);
		return 1;
	}
	printf("qdwin-fractional-probe: both fractional-scale objects got "
	       "preferred_scale=%u\n", o1.last_scale);
	return 0;
}
