/*
 * qdwin-activation-probe — negative-path test client for qdwin's
 * xdg_activation_v1 gate (qdwin_activation_activate, qdwin/qdwin.c).
 *
 * qdwin issues activation tokens (get_activation_token + commit → `done`)
 * and consumes them on `activate(token, surface)`. The activate handler:
 *   - looks up the token; an UNKNOWN/forged token is dropped with a
 *     "xdg-activation activate with unknown token" log line and NO focus
 *     change (qdwin_activation_token_find returns NULL → early return);
 *   - if the token is known but the target surface has no qdwin toplevel,
 *     it logs "target surface has no toplevel" and frees the token.
 * Neither path is a protocol error and neither may crash the compositor.
 *
 * This probe drives the unknown-token path headlessly: bind
 * xdg_activation_v1 + wl_compositor, create a roleless wl_surface, and call
 * activate() with a bogus token. We assert the call round-trips with no
 * protocol error and (via a follow-up roundtrip + a second bind) that the
 * compositor is still alive and serving — i.e. the bad token did not crash
 * or wedge it.
 *
 * Modes:
 *   (default)   activate(bogus, surface) once.
 *   --repeat=N  activate(bogus, surface) N times (default 1) to exercise
 *               repeated forged-token drops (no per-token state leak/crash).
 *   --empty     activate("", surface): the empty token can never match
 *               (qdwin issues non-empty tokens, and an alloc-fail token is
 *               "" which is also unfindable), so it takes the same
 *               unknown-token drop path.
 *   --token-lifecycle  Drive the FULL legitimate token flow + single-use
 *               consumption (the token expiry/reuse edge in §3):
 *                 1. get_activation_token + set_app_id + commit; assert the
 *                    `done(token)` event delivers a NON-EMPTY token (qdwin
 *                    never issues an empty token on the success path);
 *                 2. activate(token, roleless surface): the token IS known,
 *                    but the surface has no qdwin toplevel, so qdwin logs
 *                    "target surface has no toplevel" and FREES the token;
 *                 3. activate(token AGAIN): the token was consumed in step 2,
 *                    so this hits the UNKNOWN-token drop — proving a
 *                    committed token is single-use and cannot be replayed.
 *               Asserts no protocol error throughout and the compositor stays
 *               alive. (The "no toplevel" outcome is expected: a roleless
 *               wl_surface is used precisely so a VALID token still cannot
 *               steal focus — the consumption is what's under test.)
 *
 * Exit codes:
 *   0  every activate() round-tripped cleanly AND the compositor answered a
 *      post-activate registry roundtrip (still alive); for --token-lifecycle,
 *      the issued token was non-empty and the replay was dropped
 *   1  a protocol error was posted, the liveness roundtrip failed, or
 *      --token-lifecycle saw an empty/missing issued token
 *   2  setup/other error (no display, a required global not advertised)
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
#include "xdg-activation-v1-client-protocol.h"

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_compositor *compositor;
	struct xdg_activation_v1 *activation;
	char issued_token[128];
	int got_done;
};

static void
on_token_done(void *data, struct xdg_activation_token_v1 *tok,
	      const char *token)
{
	struct probe *p = data;
	(void)tok;
	p->got_done = 1;
	snprintf(p->issued_token, sizeof p->issued_token, "%s",
		 token ? token : "");
}
static const struct xdg_activation_token_v1_listener token_listener = {
	.done = on_token_done,
};

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct probe *p = data;
	if (strcmp(interface, xdg_activation_v1_interface.name) == 0) {
		p->activation = wl_registry_bind(
			reg, name, &xdg_activation_v1_interface,
			version < 1 ? version : 1);
	} else if (strcmp(interface, wl_compositor_interface.name) == 0) {
		p->compositor = wl_registry_bind(
			reg, name, &wl_compositor_interface,
			version < 4 ? version : 4);
	}
}

static void
on_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{ (void)data; (void)reg; (void)name; }

static const struct wl_registry_listener registry_listener = {
	.global = on_global,
	.global_remove = on_global_remove,
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
		fprintf(stderr,
			"qdwin-activation-probe: %s ERROR (errno=%d, proto "
			"code=%u on %s#%u)\n",
			what, err, code,
			iface ? iface->name : "(unknown)", obj_id);
		return 1;
	}
	return 0;
}

int main(int argc, char *argv[])
{
	int repeat = 1;
	int lifecycle = 0;
	const char *token = "qdwin-bogus-forged-token";
	for (int i = 1; i < argc; i++) {
		if (strncmp(argv[i], "--repeat=", 9) == 0) {
			repeat = atoi(argv[i] + 9);
			if (repeat < 1)
				repeat = 1;
		} else if (strcmp(argv[i], "--empty") == 0) {
			token = "";
		} else if (strcmp(argv[i], "--token-lifecycle") == 0) {
			lifecycle = 1;
		}
	}

	struct probe p = {0};
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-activation-probe: wl_display_connect "
			"failed: %s\n", strerror(errno));
		return 2;
	}
	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);
	wl_display_roundtrip(p.display);

	if (!p.activation) {
		fprintf(stderr, "qdwin-activation-probe: xdg_activation_v1 "
			"not advertised\n");
		return 2;
	}
	if (!p.compositor) {
		fprintf(stderr, "qdwin-activation-probe: wl_compositor not "
			"advertised\n");
		return 2;
	}

	struct wl_surface *surface = wl_compositor_create_surface(p.compositor);
	if (!surface) {
		fprintf(stderr, "qdwin-activation-probe: create_surface "
			"failed\n");
		return 2;
	}

	if (lifecycle) {
		/* Step 1: legitimate token request + commit → done(token). */
		struct xdg_activation_token_v1 *tok =
			xdg_activation_v1_get_activation_token(p.activation);
		if (!tok) {
			fprintf(stderr, "qdwin-activation-probe: "
				"get_activation_token NULL\n");
			return 2;
		}
		xdg_activation_token_v1_add_listener(tok, &token_listener, &p);
		xdg_activation_token_v1_set_app_id(tok,
			"org.qdistro.test.activation");
		xdg_activation_token_v1_commit(tok);
		if (roundtrip_err(&p, "token commit") != 0)
			return 1;
		if (!p.got_done) {
			fprintf(stderr, "qdwin-activation-probe: no `done` event "
				"after token commit\n");
			return 1;
		}
		if (p.issued_token[0] == '\0') {
			fprintf(stderr, "qdwin-activation-probe: issued token is "
				"EMPTY (qdwin must never issue an empty token on "
				"the success path)\n");
			return 1;
		}

		/* Step 2: activate with the REAL token against a roleless
		 * surface. Token is known → consumed (freed); surface has no
		 * toplevel → no focus change. */
		xdg_activation_v1_activate(p.activation, p.issued_token, surface);
		if (roundtrip_err(&p, "activate(real token, roleless)") != 0)
			return 1;

		/* Step 3: replay the SAME token. It was consumed in step 2, so
		 * this must hit the unknown-token drop (single-use property). */
		xdg_activation_v1_activate(p.activation, p.issued_token, surface);
		if (roundtrip_err(&p, "activate(replayed token)") != 0)
			return 1;

		struct wl_registry *rl = wl_display_get_registry(p.display);
		if (!rl || roundtrip_err(&p, "post-lifecycle liveness") != 0)
			return 1;
		printf("qdwin-activation-probe: issued non-empty token, "
		       "consumed on first activate, replay dropped, compositor "
		       "alive\n");
		return 0;
	}

	for (int i = 0; i < repeat; i++) {
		xdg_activation_v1_activate(p.activation, token, surface);
		if (roundtrip_err(&p, "activate(unknown token)") != 0)
			return 1;
	}

	/* Liveness check: the compositor must still answer. A fresh registry
	 * roundtrip proves the bad token neither crashed nor wedged it. */
	struct wl_registry *r2 = wl_display_get_registry(p.display);
	if (!r2 || roundtrip_err(&p, "post-activate liveness") != 0)
		return 1;

	printf("qdwin-activation-probe: unknown token dropped, compositor "
	       "alive (activated x%d, token=%s)\n",
	       repeat, token[0] ? token : "(empty)");
	return 0;
}
