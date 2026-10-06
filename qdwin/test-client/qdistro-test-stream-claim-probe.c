/*
 * qdistro-test-stream-claim-probe — standalone denial oracle for
 * qdwin_stream_input_v1.claim.
 *
 * qdwin advertises qdwin_stream_input_v1 to every client on purpose
 * ("visible to any client; the access_token in claim() is the gate",
 * qdwin.c): the token is minted per subscribed stream and handed to
 * exactly the qdistro-forward child qdwin spawned — claim also pins the
 * claimant's pid to s->forward_pid. So ANY standalone caller — and in
 * particular a secctx-tagged tier-3s bridge peer — must have its claim
 * refused with INVALID_TOKEN, whether the token is unknown or was
 * minted for somebody else.
 *
 * This probe binds the global, calls claim with a token (default: a
 * bogus 32-hex string), and asserts the compositor answers with the
 * expected fatal protocol error on the stream_input object instead of
 * producing a usable inject handle.
 *
 * Modes:
 *   (default)                claim "0000…0000"; expect INVALID_TOKEN.
 *   --token <tok>            claim <tok> instead.
 *   --expect <name>          invalid_token (default) or already_claimed.
 *
 * Exit codes: 0 expected protocol error observed; 1 wrong/no error;
 * 2 setup failure (connect/bind). Prints one result line on stdout.
 */

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <wayland-client.h>

#include "qdwin-shell-v1-client-protocol.h"

struct probe {
	struct wl_display *dpy;
	struct wl_registry *reg;
	struct qdwin_stream_input_v1 *si;
};

static void reg_global(void *data, struct wl_registry *r, uint32_t name,
		       const char *iface, uint32_t version)
{
	struct probe *p = data;
	if (!strcmp(iface, qdwin_stream_input_v1_interface.name))
		p->si = wl_registry_bind(r, name,
					 &qdwin_stream_input_v1_interface,
					 version >= 2 ? 2 : version);
}
static void reg_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener reg_listener = {
	.global = reg_global, .global_remove = reg_global_remove,
};

/* A denied claim is a FATAL protocol error: the roundtrip fails and
 * wl_display_get_protocol_error reports the interface + enum value.
 * Poll a bounded number of times first so a single-roundtrip timing
 * race can't make a real error look absent (fail closed). */
static int expect_error(struct probe *p, uint32_t want_code)
{
	for (int i = 0; i < 100; i++) {
		if (wl_display_roundtrip(p->dpy) < 0)
			break;
		if (wl_display_get_error(p->dpy) != 0)
			break;
		struct timespec ns = { .tv_sec = 0, .tv_nsec = 10 * 1000000L };
		nanosleep(&ns, NULL);
	}
	if (wl_display_get_error(p->dpy) != EPROTO) {
		fprintf(stderr, "probe: no protocol error after claim\n");
		return 0;
	}
	const struct wl_interface *iface = NULL;
	uint32_t id = 0;
	uint32_t code = wl_display_get_protocol_error(p->dpy, &iface, &id);
	if (iface != &qdwin_stream_input_v1_interface || code != want_code) {
		fprintf(stderr, "probe: wrong error iface=%s code=%u\n",
			iface ? iface->name : "(none)", code);
		return 0;
	}
	return 1;
}

int main(int argc, char **argv)
{
	const char *token = "00000000000000000000000000000000";
	uint32_t want = QDWIN_STREAM_INPUT_V1_ERROR_INVALID_TOKEN;
	const char *want_name = "invalid_token";

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--token") && i + 1 < argc)
			token = argv[++i];
		else if (!strcmp(argv[i], "--expect") && i + 1 < argc) {
			want_name = argv[++i];
			want = !strcmp(want_name, "already_claimed")
				? QDWIN_STREAM_INPUT_V1_ERROR_ALREADY_CLAIMED
				: QDWIN_STREAM_INPUT_V1_ERROR_INVALID_TOKEN;
		} else {
			fprintf(stderr, "usage: %s [--token T] "
				"[--expect invalid_token|already_claimed]\n",
				argv[0]);
			return 2;
		}
	}

	struct probe p = { 0 };
	p.dpy = wl_display_connect(NULL);
	if (!p.dpy) {
		fprintf(stderr, "probe: wl_display_connect failed\n");
		return 2;
	}
	p.reg = wl_display_get_registry(p.dpy);
	wl_registry_add_listener(p.reg, &reg_listener, &p);
	wl_display_roundtrip(p.dpy);
	if (!p.si) {
		fprintf(stderr, "probe: qdwin_stream_input_v1 not advertised\n");
		return 2;
	}

	(void)qdwin_stream_input_v1_claim(p.si, token);
	wl_display_flush(p.dpy);

	if (!expect_error(&p, want))
		return 1;
	printf("[qdistro-test-stream-claim-probe] claim -> %s (as expected)\n",
	       want_name);
	return 0;
}
