/*
 * qdwin-nested-identity-probe — test client for qdwin_nested_v1 nested-proxy
 * identity hardening (qdwin_nested_manager_advertise_toplevel +
 * qdwin_handle_bind_proxy_pixels in qdwin.c).
 *
 * Threat model (todo/security-hardening-carryforward.md §"qdwin and nested
 * protocols"): the advertising client (a nested compositor) supplies
 * origin_uid and input_sink in advertise_toplevel. Neither may become
 * authority on the client's word — origin_uid must be bound to the
 * advertising client's kernel-resolved peer uid, and the input_sink socket
 * peer uid must match it. This probe drives advertise_toplevel with a
 * deliberately FOREIGN origin_uid and lets the harness assert (via the
 * weston log) that qdwin overrode it back to the probe's real peer uid.
 *
 * Modes:
 *   (default)            bind qdwin_nested_manager_v1, advertise one
 *                        toplevel with origin_uid = (real uid + 1) and an
 *                        empty input_sink (display-only), wait for the
 *                        `configured` event, exit 0. The harness greps the
 *                        weston log for the origin_uid override line.
 *   --spoof-uid=N        use N as the asserted origin_uid (default uid+1).
 *   --input-sink=PATH    advertise PATH as the input_sink (lets the harness
 *                        drive the peer-cred check on a foreign-owned
 *                        socket; default empty = display-only).
 *   --timeout=N          seconds to wait for `configured` (default 5).
 *
 * Exit codes:
 *   0  advertise round-tripped and `configured` arrived (the happy path;
 *      the SECURITY assertion is the harness's log grep, not this code —
 *      the override is silent on the wire by design)
 *   2  setup error (no display, manager global not advertised, ...)
 *   1  advertise failed with a protocol error, or `configured` never came
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
#include "qdwin-nested-v1-client-protocol.h"

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;
	struct qdwin_nested_manager_v1 *mgr;
	struct qdwin_nested_toplevel_v1 *tl;
	uint32_t mgr_version;
	int saw_manager;
	int saw_configured;
	int configured_w;
	int configured_h;
};

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct probe *p = data;
	if (strcmp(interface, qdwin_nested_manager_v1_interface.name) == 0) {
		p->saw_manager = 1;
		uint32_t bind_ver = version < 2 ? version : 2;
		p->mgr_version = bind_ver;
		p->mgr = wl_registry_bind(
			reg, name, &qdwin_nested_manager_v1_interface, bind_ver);
	}
}

static void
on_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{ (void)data; (void)reg; (void)name; }

static const struct wl_registry_listener registry_listener = {
	.global = on_global,
	.global_remove = on_global_remove,
};

static void
on_configured(void *data, struct qdwin_nested_toplevel_v1 *tl,
	      int32_t width, int32_t height)
{
	struct probe *p = data;
	(void)tl;
	p->saw_configured = 1;
	p->configured_w = width;
	p->configured_h = height;
}

static void
on_close_requested(void *data, struct qdwin_nested_toplevel_v1 *tl)
{ (void)data; (void)tl; }

static void
on_focus_changed(void *data, struct qdwin_nested_toplevel_v1 *tl,
		 uint32_t focused)
{ (void)data; (void)tl; (void)focused; }

static const struct qdwin_nested_toplevel_v1_listener toplevel_listener = {
	.configured = on_configured,
	.close_requested = on_close_requested,
	.focus_changed = on_focus_changed,
};

int main(int argc, char *argv[])
{
	long spoof_uid = -1;          /* -1 → default to real uid + 1 */
	const char *input_sink = "";  /* empty = display-only */
	int timeout_s = 5;

	for (int i = 1; i < argc; i++) {
		if (strncmp(argv[i], "--spoof-uid=", 12) == 0)
			spoof_uid = strtol(argv[i] + 12, NULL, 10);
		else if (strncmp(argv[i], "--input-sink=", 13) == 0)
			input_sink = argv[i] + 13;
		else if (strncmp(argv[i], "--timeout=", 10) == 0)
			timeout_s = (int)strtol(argv[i] + 10, NULL, 10);
	}
	if (spoof_uid < 0)
		spoof_uid = (long)getuid() + 1;

	struct probe p = {0};
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-nested-identity-probe: wl_display_connect "
			"failed: %s\n", strerror(errno));
		return 2;
	}

	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);
	wl_display_roundtrip(p.display);

	if (!p.saw_manager || !p.mgr) {
		fprintf(stderr, "qdwin-nested-identity-probe: "
			"qdwin_nested_manager_v1 not advertised (peer-uid "
			"filter rejected the bind, or global absent)\n");
		return 2;
	}

	fprintf(stderr, "qdwin-nested-identity-probe: advertising origin_uid=%ld "
		"(real uid=%u) input_sink='%s'\n",
		spoof_uid, (unsigned)getuid(), input_sink);

	p.tl = qdwin_nested_manager_v1_advertise_toplevel(
		p.mgr,
		"",                       /* pw_node: empty placeholder */
		input_sink,
		"org.qdistro.test.nested-probe",
		"nested-probe",
		(uint32_t)spoof_uid);
	if (!p.tl) {
		fprintf(stderr, "qdwin-nested-identity-probe: advertise_toplevel "
			"returned NULL\n");
		return 2;
	}
	qdwin_nested_toplevel_v1_add_listener(p.tl, &toplevel_listener, &p);

	/* Wait (bounded) for the `configured` event that follows a successful
	 * advertise. A protocol error (e.g. policy_denied) surfaces as a
	 * display error. */
	for (int i = 0; i < timeout_s * 10 && !p.saw_configured; i++) {
		if (wl_display_roundtrip(p.display) < 0)
			break;
		if (wl_display_get_error(p.display) != 0)
			break;
		if (p.saw_configured)
			break;
		usleep(100000);
	}

	if (wl_display_get_error(p.display) != 0) {
		uint32_t code = 0, obj = 0;
		const struct wl_interface *iface = NULL;
		code = wl_display_get_protocol_error(p.display, &iface, &obj);
		fprintf(stderr, "qdwin-nested-identity-probe: advertise REFUSED "
			"(proto code=%u on %s#%u)\n", code,
			iface ? iface->name : "(unknown)", obj);
		return 1;
	}
	if (!p.saw_configured) {
		fprintf(stderr, "qdwin-nested-identity-probe: no `configured` event "
			"within %ds\n", timeout_s);
		return 1;
	}

	printf("qdwin-nested-identity-probe: advertise ACCEPTED, configured %dx%d "
	       "(origin_uid asserted=%ld; check weston log for the override "
	       "to real peer uid)\n",
	       p.configured_w, p.configured_h, spoof_uid);
	return 0;
}
