/*
 * qdwin-stream-claimant — compositor-boundary direct claimant for the
 * multi-machine input-isolation gate (todo/multi-machine, session 7, gate A1).
 *
 * Purpose: exercise qdwin's `qdwin_stream_input_v1` claim+inject path DIRECTLY,
 * with NO FreeRDP / RDP shadow server / remote viewer in the loop, so an input
 * failure is unambiguously compositor-side (codex impl-17/impl-18).
 *
 * How it runs: qdwin spawns ONE qdistro-forward child per subscribed stream and
 * mints a one-shot `access_token` it hands to exactly that child over
 * `--access-token-fd` (and records the child's pid in `s->forward_pid`).
 * qdwin's claim() gate is BOTH token-match AND pid-match (qdwin.c) — a stream's
 * token is only claimable by the very process qdwin spawned. So a standalone
 * claimant CANNOT claim; this binary must BE the spawned "forward". It is
 * selected via the existing, trusted `QDWIN_FORWARD_BIN` seam (qdwin validates
 * the path is a root-owned, non-group/world-writable regular file). qdwin then
 * execs THIS binary with the forward argv; we read the real token from
 * `--access-token-fd` and drive the protocol directly.
 *
 * Because we ARE the spawned process, all sub-connections below share the same
 * pid == s->forward_pid and therefore pass the pid gate; the only variable under
 * test is the TOKEN and the CLAIM/INJECT protocol behaviour.
 *
 * Checks performed in one spawned process (results → QDWIN_CLAIMANT_STATUS JSON):
 *   - positive : connect, bind qdwin_stream_input_v1, claim(real token) → handle,
 *                (after a GO signal) inject pointer motion + button press/release.
 *   - already_claimed : a 2nd connection claims the SAME real token → must get the
 *                ALREADY_CLAIMED protocol error (one-shot consumption).
 *   - invalid_token   : a 3rd connection claims a bogus 32-hex token → must get the
 *                INVALID_TOKEN protocol error (public global is harmless w/o secret).
 *   - reclaim_invalid_token (opt-in, QDWIN_CLAIMANT_RECLAIM=1): after the inject,
 *                the live handle is destroyed and its connection closed (= the
 *                forward releasing its claim), then a NEW connection (same pid,
 *                so the pid gate still passes) claims the SAME real token → must
 *                get INVALID_TOKEN. Pins the one-shot contract: release must
 *                invalidate the token, not make it claimable again (iso2 10 E4).
 * (The `not_claimed` error is unreachable by construction: an inject handle only
 * exists after a successful claim, and a failed claim is a fatal protocol error —
 * so there is no live unclaimed handle to inject on. qdwin's inject handlers also
 * defensively drop on a NULL stream. Documented, not asserted.)
 *
 * The inject is GATED on a GO file (QDWIN_CLAIMANT_GO) so the harness can bring up
 * the confinement sentinel BEFORE any event is injected (the sentinel must be live
 * and binding seats during injection for its zero-delta to be non-vacuous).
 *
 * The harness does NOT trust this binary's self-report for the headline claim: the
 * real proof is the exported marker's per-seat PRESS delta (>0, on the
 * `qdwin-stream-<port>` seat) and the sentinel's delta (==0). This status file is a
 * fail-closed witness that the claim path executed and the negatives held.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>
#include "qdwin-shell-v1-client-protocol.h"

#define BTN_LEFT 0x110

static const char *g_status_path;   /* QDWIN_CLAIMANT_STATUS */
static const char *g_go_path;       /* QDWIN_CLAIMANT_GO */

static uint32_t now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint32_t)((uint64_t)ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

/* ---- one connection's binding of qdwin_stream_input_v1 ------------------- */
struct si_conn {
	struct wl_display *dpy;
	struct wl_registry *reg;
	struct qdwin_stream_input_v1 *si;
	uint32_t si_version;
};

static void reg_global(void *data, struct wl_registry *r, uint32_t name,
		       const char *iface, uint32_t version)
{
	struct si_conn *c = data;
	if (!strcmp(iface, qdwin_stream_input_v1_interface.name)) {
		uint32_t v = version >= 2 ? 2 : version;
		c->si = wl_registry_bind(r, name, &qdwin_stream_input_v1_interface, v);
		c->si_version = v;
	}
}
static void reg_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener reg_listener = {
	.global = reg_global, .global_remove = reg_global_remove,
};

/* Connect + bind the input global. Returns 0 on success. */
static int si_connect(struct si_conn *c, const char *wl_display)
{
	memset(c, 0, sizeof *c);
	c->dpy = wl_display_connect(wl_display);
	if (!c->dpy) {
		fprintf(stderr, "claimant: wl_display_connect(%s) failed\n",
			wl_display ? wl_display : "(default)");
		return -1;
	}
	c->reg = wl_display_get_registry(c->dpy);
	wl_registry_add_listener(c->reg, &reg_listener, c);
	wl_display_roundtrip(c->dpy);
	if (!c->si) {
		fprintf(stderr, "claimant: qdwin_stream_input_v1 not advertised\n");
		wl_registry_destroy(c->reg);
		wl_display_disconnect(c->dpy);
		memset(c, 0, sizeof *c);
		return -1;
	}
	return 0;
}

static void si_close(struct si_conn *c)
{
	if (c->si) qdwin_stream_input_v1_destroy(c->si);
	if (c->reg) wl_registry_destroy(c->reg);
	if (c->dpy) wl_display_disconnect(c->dpy);
	memset(c, 0, sizeof *c);
}

/* After issuing a claim that is expected to FAIL, roundtrip and report whether
 * the server posted exactly `want_code` on the qdwin_stream_input_v1 interface.
 * The fatal protocol error makes wl_display_roundtrip return < 0; poll a bounded
 * number of times so a one-roundtrip timing race can't make a real error look
 * absent (a flaky negative check is a false FAIL — fail closed, not open). */
static int expect_claim_error(struct si_conn *c, uint32_t want_code)
{
	for (int i = 0; i < 100; i++) {
		if (wl_display_roundtrip(c->dpy) < 0)
			break;                  /* the connection errored */
		if (wl_display_get_error(c->dpy) != 0)
			break;
		struct timespec ns = { .tv_sec = 0, .tv_nsec = 10 * 1000000L };
		nanosleep(&ns, NULL);
	}
	if (wl_display_get_error(c->dpy) != EPROTO)
		return 0;                       /* no protocol error → unexpected */
	const struct wl_interface *iface = NULL;
	uint32_t id = 0;
	uint32_t code = wl_display_get_protocol_error(c->dpy, &iface, &id);
	return (iface == &qdwin_stream_input_v1_interface && code == want_code);
}

/* ---- token read from the qdwin-handed fd (mirror qdistro-forward) -------- */
static char *read_token_fd(int fd)
{
	size_t cap = 4096, len = 0;
	char *buf = calloc(1, cap);
	if (!buf) return NULL;
	for (;;) {
		if (len + 1 >= cap) { free(buf); return NULL; }
		ssize_t n = read(fd, buf + len, cap - 1 - len);
		if (n < 0) { if (errno == EINTR) continue; free(buf); return NULL; }
		if (n == 0) break;
		len += (size_t)n;
	}
	while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r'))
		buf[--len] = '\0';
	return buf;
}

/* ---- status JSON (fail-closed witness) ----------------------------------- */
struct status {
	int pid;
	int bound;             /* saw the input global */
	int claim_real;        /* real claim succeeded */
	int already_claimed;   /* 2nd claim of real token got ALREADY_CLAIMED */
	int invalid_token;     /* claim of bogus token got INVALID_TOKEN */
	int go_seen;           /* GO signal observed */
	int inject_sent;       /* motion+button injected on the live handle */
	int inject_x, inject_y;
	int reclaim_checked;   /* QDWIN_CLAIMANT_RECLAIM=1: the re-claim ran */
	int reclaim_invalid_token; /* re-claim of the released token got INVALID_TOKEN */
};

static void write_status(const struct status *s)
{
	if (!g_status_path) return;
	char tmp[1024];
	snprintf(tmp, sizeof tmp, "%s.tmp", g_status_path);
	FILE *f = fopen(tmp, "wb");
	if (!f) return;
	fprintf(f, "{\"pid\":%d,\"bound\":%d,\"claim_real\":%d,"
		   "\"already_claimed\":%d,\"invalid_token\":%d,"
		   "\"go_seen\":%d,\"inject_sent\":%d,"
		   "\"inject_x\":%d,\"inject_y\":%d,"
		   "\"reclaim_checked\":%d,\"reclaim_invalid_token\":%d}\n",
		s->pid, s->bound, s->claim_real, s->already_claimed,
		s->invalid_token, s->go_seen, s->inject_sent,
		s->inject_x, s->inject_y,
		s->reclaim_checked, s->reclaim_invalid_token);
	fflush(f);
	int fd = fileno(f);
	if (fd >= 0) fsync(fd);
	fclose(f);
	rename(tmp, g_status_path);
}

int main(int argc, char **argv)
{
	const char *wl_display = NULL;
	int token_fd = -1;

	/* qdwin execs us with the forward argv; we only need two of the args
	 * and ignore the rest (--pipewire-node, --rdp-port, --rdp-password-fd,
	 * --width, --height) so the same exec contract works unchanged. */
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--wayland-display") && i + 1 < argc)
			wl_display = argv[++i];
		else if (!strcmp(argv[i], "--access-token-fd") && i + 1 < argc)
			token_fd = atoi(argv[++i]);
	}

	g_status_path = getenv("QDWIN_CLAIMANT_STATUS");
	g_go_path     = getenv("QDWIN_CLAIMANT_GO");
	const char *xs = getenv("QDWIN_CLAIMANT_INJECT_X");
	const char *ys = getenv("QDWIN_CLAIMANT_INJECT_Y");

	struct status st = { .pid = (int)getpid(),
			     .inject_x = xs ? atoi(xs) : 640,
			     .inject_y = ys ? atoi(ys) : 400 };
	write_status(&st);

	if (token_fd < 0) {
		fprintf(stderr, "claimant: no --access-token-fd\n");
		write_status(&st);
		return 2;
	}
	char *token = read_token_fd(token_fd);
	if (!token || !*token) {
		fprintf(stderr, "claimant: empty access token\n");
		write_status(&st);
		return 2;
	}

	/* (1) positive: claim the real token, keep the connection + handle open. */
	struct si_conn pos;
	if (si_connect(&pos, wl_display) < 0) { write_status(&st); return 3; }
	st.bound = 1; write_status(&st);

	struct qdwin_stream_input_handle_v1 *handle =
		qdwin_stream_input_v1_claim(pos.si, token);
	wl_display_flush(pos.dpy);
	if (wl_display_roundtrip(pos.dpy) < 0 || !handle) {
		fprintf(stderr, "claimant: real claim failed\n");
		write_status(&st);
		return 3;
	}
	st.claim_real = 1; write_status(&st);

	/* (2) already_claimed: a 2nd connection (same pid) claims the same token. */
	struct si_conn dup;
	if (si_connect(&dup, wl_display) == 0) {
		(void)qdwin_stream_input_v1_claim(dup.si, token);
		wl_display_flush(dup.dpy);
		st.already_claimed =
			expect_claim_error(&dup, QDWIN_STREAM_INPUT_V1_ERROR_ALREADY_CLAIMED);
	}
	si_close(&dup);
	write_status(&st);

	/* (3) invalid_token: claim a bogus 32-hex token. */
	struct si_conn bad;
	if (si_connect(&bad, wl_display) == 0) {
		(void)qdwin_stream_input_v1_claim(
			bad.si, "00000000000000000000000000000000");
		wl_display_flush(bad.dpy);
		st.invalid_token =
			expect_claim_error(&bad, QDWIN_STREAM_INPUT_V1_ERROR_INVALID_TOKEN);
	}
	si_close(&bad);
	write_status(&st);

	/* Gate the inject on GO so the harness can bring up the sentinel first.
	 * Keep the live (claimed) connection healthy while we wait. */
	int go_deadline_ms = 30000;
	uint32_t start = now_ms();
	while (g_go_path && access(g_go_path, F_OK) != 0) {
		if ((now_ms() - start) > (uint32_t)go_deadline_ms) break;
		if (wl_display_roundtrip(pos.dpy) < 0) {
			fprintf(stderr, "claimant: live connection died waiting for GO\n");
			write_status(&st);
			return 4;
		}
		struct timespec ns = { .tv_sec = 0, .tv_nsec = 100 * 1000000L };
		nanosleep(&ns, NULL);
	}
	st.go_seen = (!g_go_path || access(g_go_path, F_OK) == 0);
	write_status(&st);
	if (!st.go_seen) {
		fprintf(stderr, "claimant: GO never arrived\n");
		return 4;
	}

	/* Inject motion (asserts/repins focus) then a button press+release, all
	 * through the claimed per-stream handle. */
	uint32_t t = now_ms();
	qdwin_stream_input_handle_v1_inject_pointer_motion(
		handle, t, wl_fixed_from_int(st.inject_x),
		wl_fixed_from_int(st.inject_y));
	qdwin_stream_input_handle_v1_inject_pointer_button(handle, t + 1, BTN_LEFT, 1);
	qdwin_stream_input_handle_v1_inject_pointer_button(handle, t + 2, BTN_LEFT, 0);
	wl_display_flush(pos.dpy);
	if (wl_display_roundtrip(pos.dpy) < 0) {
		fprintf(stderr, "claimant: roundtrip after inject failed\n");
		write_status(&st);
		return 5;
	}
	st.inject_sent = 1;
	write_status(&st);

	/* Hold the claimed connection open briefly so the marker's telemetry
	 * (atomic-replaced on each press) is durably on disk before we exit and
	 * the handle is torn down. */
	for (int i = 0; i < 15; i++) {
		if (wl_display_roundtrip(pos.dpy) < 0) break;
		struct timespec ns = { .tv_sec = 0, .tv_nsec = 100 * 1000000L };
		nanosleep(&ns, NULL);
	}

	qdwin_stream_input_handle_v1_destroy(handle);
	wl_display_flush(pos.dpy);
	(void)wl_display_roundtrip(pos.dpy);   /* server processes the destroy */
	si_close(&pos);

	/* (4) opt-in one-shot check: the claim above is now RELEASED (handle
	 * destroyed + connection closed). Re-claim the SAME token from a fresh
	 * connection of this same pid: the token must have been invalidated on
	 * release, so the only acceptable outcome is INVALID_TOKEN. A successful
	 * re-claim here means release made the token claimable again (the
	 * pre-fix behaviour) → the check fails (fail closed). */
	const char *reclaim = getenv("QDWIN_CLAIMANT_RECLAIM");
	if (reclaim && !strcmp(reclaim, "1")) {
		struct si_conn re;
		st.reclaim_checked = 1;
		if (si_connect(&re, wl_display) == 0) {
			(void)qdwin_stream_input_v1_claim(re.si, token);
			wl_display_flush(re.dpy);
			st.reclaim_invalid_token = expect_claim_error(
				&re, QDWIN_STREAM_INPUT_V1_ERROR_INVALID_TOKEN);
		}
		si_close(&re);
		write_status(&st);
	}
	free(token);

	/* Exit success only if every required outcome held. */
	int ok = st.claim_real && st.already_claimed && st.invalid_token &&
		 st.inject_sent &&
		 (!st.reclaim_checked || st.reclaim_invalid_token);
	return ok ? 0 : 6;
}
