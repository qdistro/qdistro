/*
 * qdwin-locker-probe — test client for the qdwin_locker_v1 bind gate
 * (bind_qdwin_locker + qdwin_handle_bind_as_locker, qdwin/qdwin.c).
 *
 * Trust model (qdwin/qdwin-locker-v1.xml): qdwin advertises the
 * qdwin_locker_v1 global to every client, but the *bind* handler rejects
 * any client whose peer uid != qdwin->allowed_locker_uid (which, absent an
 * explicit --qdwin-allowed-locker-uid, falls back to qdwin->allowed_uid).
 * The reject is posted as a wl_client implementation error at GLOBAL BIND
 * time — before the client can issue any request on the resource. The
 * authorized client then calls bind_as_locker and gets the `ready` event.
 *
 * Log note: the uid reject is delivered to the CLIENT as a wl_client
 * implementation error ("uid N not permitted") — there is NO dedicated
 * server-log "REJECTED" line for the locker uid branch (unlike secctx).
 * The companion scenario therefore asserts on this probe's exit code plus
 * the "locker bind attempt … uid=… allowed_locker_uid=…" line and the
 * absence of a "locker bound" line.
 *
 * Headless limitation (same as 06-secctx): a single-uid host cannot run a
 * client under a second real uid, so the "unauthorized" case is driven by
 * starting qdwin with a FOREIGN allowed_uid (tests/host/start.sh
 * --allowed-uid) so the probe's real uid mismatches allowed_locker_uid and
 * the reject branch fires. This faithfully exercises the uid reject branch.
 * The optional exe/SELinux peer checks (df8f3d5) and the from_shell
 * interaction are NOT exercised here (they need a configured policy and/or
 * a second real uid — see the scenario's follow-up note).
 *
 * Modes (mutually exclusive):
 *   (default)       Bind the global, then bind_as_locker. Report whether
 *                   the sequence was accepted (got `ready`) or refused
 *                   (server posted a protocol/implementation error).
 *   --double-bind   Bind the global once; call bind_as_locker TWICE on the
 *                   same resource. Per the XML the second call is rejected
 *                   with the dedicated already_bound (=1) error. Asserts
 *                   the FIRST succeeded (ready) and the SECOND raised
 *                   exactly that error.
 *   --rebind        Bind the global TWICE (two resources on one client);
 *                   call bind_as_locker on each. The first becomes the
 *                   live locker. Because this probe's process (the locker
 *                   peer) is still ALIVE, qdwin REFUSES the second
 *                   bind_as_locker with the dedicated `locker_present`
 *                   (=5) error rather than evicting the live locker —
 *                   the FINDING #5 takeover-rejection fix. Asserts the
 *                   first bind_as_locker got `ready` and the second
 *                   raised exactly locker_present on qdwin_locker_v1.
 *                   NOTE: both binds share ONE pid, so this does not
 *                   exercise the cross-process liveness check — see
 *                   --separate-takeover.
 *   --separate-takeover
 *                   The real FINDING #5 test: two INDEPENDENT same-uid
 *                   PROCESSES (distinct pids). A forked child binds as the
 *                   locker and holds the binding open (its own wl_display
 *                   connection); the PARENT — a different pid — then opens a
 *                   fresh connection and attempts bind_as_locker. While the
 *                   child is alive qdwin must REFUSE the parent with
 *                   locker_present (qdwin_locker_peer_alive sees the child's
 *                   pid still live). The parent then KILLS the child, waits
 *                   for qdwin to observe the unbind, and re-attempts: now the
 *                   prior peer is provably dead and the bind must be ACCEPTED
 *                   (got `ready`). This is the scenario the one-pid --rebind
 *                   cannot cover.
 *
 * Exit codes:
 *   0  expected-accept path completed cleanly
 *   4  unauthorized default run: the bind was REFUSED with the expected
 *      implementation error on wl_display (the PASS signal for that mode)
 *   1  the expected accept failed, OR the bind failed with an UNEXPECTED
 *      error (wrong interface/code) — always a FAILURE
 *   3  --double-bind: second bind_as_locker raised the expected
 *      already_bound error on qdwin_locker_v1 (the PASS signal for that mode)
 *   5  --rebind: second bind_as_locker (live-peer takeover) raised the
 *      expected locker_present error (the PASS signal for that mode)
 *   6  --separate-takeover: a separate-process takeover was REFUSED with
 *      locker_present while the holder lived, and ACCEPTED after the holder
 *      died (the PASS signal for that mode)
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
#include <signal.h>
#include <sys/wait.h>
#include <sys/types.h>
#include <sys/mman.h>
#include <time.h>

#include <wayland-client.h>
#include "qdwin-locker-v1-client-protocol.h"

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;
	uint32_t locker_name;
	uint32_t locker_version;
	int saw_global;
	int got_ready;
	/* Only bound in --transparent-lock mode (map a lock surface). */
	struct wl_compositor *compositor;
	struct wl_shm *shm;
};

static void
on_ready(void *data, struct qdwin_locker_v1 *locker, uint32_t initially_locked)
{
	struct probe *p = data;
	(void)locker; (void)initially_locked;
	p->got_ready = 1;
}

static void on_locked_changed(void *d, struct qdwin_locker_v1 *l, uint32_t v)
{ (void)d; (void)l; (void)v; }
static void on_lock_requested(void *d, struct qdwin_locker_v1 *l, uint32_t v)
{ (void)d; (void)l; (void)v; }
static void on_overlay_key(void *d, struct qdwin_locker_v1 *l, uint32_t sym,
			   const char *utf8)
{ (void)d; (void)l; (void)sym; (void)utf8; }

static const struct qdwin_locker_v1_listener locker_listener = {
	.ready = on_ready,
	.locked_changed = on_locked_changed,
	.lock_requested = on_lock_requested,
	.overlay_key = on_overlay_key,
};

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct probe *p = data;
	(void)reg;
	if (strcmp(interface, qdwin_locker_v1_interface.name) == 0) {
		p->saw_global = 1;
		p->locker_name = name;
		p->locker_version = version < 1 ? version : 1;
	} else if (strcmp(interface, wl_compositor_interface.name) == 0) {
		p->compositor = wl_registry_bind(reg, name,
						 &wl_compositor_interface, 1);
	} else if (strcmp(interface, wl_shm_interface.name) == 0) {
		p->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	}
}

static void
on_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{ (void)data; (void)reg; (void)name; }

static const struct wl_registry_listener registry_listener = {
	.global = on_global,
	.global_remove = on_global_remove,
};

/* Roundtrip and report whether the server posted a fatal protocol error.
 * Returns 0 = clean, the wayland error code (>0) otherwise. On error,
 * *out_code receives the protocol error code and *out_iface the interface
 * the error was raised on (so callers can verify it is the SPECIFIC error
 * they expect, not an unrelated one that happens to share a code). */
static int
roundtrip_err(struct probe *p, const char *what, uint32_t *out_code,
	      const struct wl_interface **out_iface)
{
	int rc = wl_display_roundtrip(p->display);
	int err = wl_display_get_error(p->display);
	if (out_code)
		*out_code = 0;
	if (out_iface)
		*out_iface = NULL;
	if (rc < 0 || err != 0) {
		uint32_t obj_id = 0, code = 0;
		const struct wl_interface *iface = NULL;
		code = wl_display_get_protocol_error(p->display, &iface, &obj_id);
		if (out_code)
			*out_code = code;
		if (out_iface)
			*out_iface = iface;
		fprintf(stderr,
			"qdwin-locker-probe: %s ERROR (errno=%d, proto code=%u "
			"on %s#%u)\n",
			what, err, code,
			iface ? iface->name : "(unknown)", obj_id);
		return err ? err : 1;
	}
	return 0;
}

static struct qdwin_locker_v1 *
bind_locker(struct probe *p)
{
	struct qdwin_locker_v1 *l = wl_registry_bind(
		p->registry, p->locker_name,
		&qdwin_locker_v1_interface, p->locker_version);
	if (l)
		qdwin_locker_v1_add_listener(l, &locker_listener, p);
	return l;
}

/* Open a fresh wl_display connection, get the registry, and bind the locker
 * global. Returns 0 and fills *p on success; a nonzero exit code on failure.
 * Used by --separate-takeover so the holder child and the takeover parent
 * each have their OWN connection (hence distinct wl_client AND distinct pid). */
static int
fresh_locker_conn(struct probe *p, struct qdwin_locker_v1 **out)
{
	memset(p, 0, sizeof *p);
	p->display = wl_display_connect(NULL);
	if (!p->display) {
		fprintf(stderr, "qdwin-locker-probe: connect failed: %s\n",
			strerror(errno));
		return 2;
	}
	p->registry = wl_display_get_registry(p->display);
	wl_registry_add_listener(p->registry, &registry_listener, p);
	wl_display_roundtrip(p->display);
	if (!p->saw_global) {
		fprintf(stderr, "qdwin-locker-probe: locker global not "
			"advertised\n");
		return 2;
	}
	*out = bind_locker(p);
	if (!*out) {
		fprintf(stderr, "qdwin-locker-probe: bind returned NULL\n");
		return 2;
	}
	if (roundtrip_err(p, "locker global bind", NULL, NULL) != 0)
		return 2;
	return 0;
}

/* FINDING #5 (the real cross-process test). A forked CHILD binds as the
 * locker and holds it; the PARENT (a DIFFERENT pid) then attempts a takeover
 * over its own connection. qdwin must REFUSE with locker_present while the
 * child lives, and ACCEPT once the child is killed. Returns the probe exit
 * code (6 = full PASS). */
static int
run_separate_takeover(void)
{
	int pipefd[2];          /* child -> parent: "ready" handshake */
	if (pipe(pipefd) != 0) {
		fprintf(stderr, "qdwin-locker-probe: pipe: %s\n", strerror(errno));
		return 2;
	}

	pid_t child = fork();
	if (child < 0) {
		fprintf(stderr, "qdwin-locker-probe: fork: %s\n", strerror(errno));
		return 2;
	}

	if (child == 0) {
		/* CHILD: bind as the live locker, signal the parent, then hold
		 * the connection open (dispatching) until killed. */
		close(pipefd[0]);
		struct probe cp;
		struct qdwin_locker_v1 *cl = NULL;
		if (fresh_locker_conn(&cp, &cl) != 0)
			_exit(2);
		qdwin_locker_v1_bind_as_locker(cl);
		if (roundtrip_err(&cp, "child bind_as_locker", NULL, NULL) != 0)
			_exit(2);
		if (!cp.got_ready)
			_exit(2);
		/* Tell the parent we are the bound, live locker. */
		if (write(pipefd[1], "R", 1) != 1)
			_exit(2);
		/* Hold the binding: keep the connection alive forever. The
		 * parent kills us when it wants the takeover to be permitted. */
		for (;;) {
			if (wl_display_dispatch(cp.display) < 0)
				_exit(0);
		}
	}

	/* PARENT. */
	close(pipefd[1]);
	char c = 0;
	ssize_t n = read(pipefd[0], &c, 1);
	close(pipefd[0]);
	if (n != 1 || c != 'R') {
		fprintf(stderr, "qdwin-locker-probe: child failed to bind as "
			"the live locker\n");
		kill(child, SIGKILL);
		waitpid(child, NULL, 0);
		return 1;
	}

	/* Attempt #1: child (distinct pid) is alive => takeover MUST be
	 * refused with locker_present. */
	struct probe pp;
	struct qdwin_locker_v1 *pl = NULL;
	int rc = fresh_locker_conn(&pp, &pl);
	if (rc != 0) {
		kill(child, SIGKILL); waitpid(child, NULL, 0);
		return rc;
	}
	uint32_t code = 0;
	const struct wl_interface *iface = NULL;
	qdwin_locker_v1_bind_as_locker(pl);
	int err = roundtrip_err(&pp, "separate-process takeover (holder alive)",
				&code, &iface);
	if (err == 0) {
		fprintf(stderr, "qdwin-locker-probe: separate-process takeover "
			"was ACCEPTED while the holder is alive — live locker "
			"evicted (takeover vuln)\n");
		kill(child, SIGKILL); waitpid(child, NULL, 0);
		return 1;
	}
	if (iface != &qdwin_locker_v1_interface ||
	    code != QDWIN_LOCKER_V1_ERROR_LOCKER_PRESENT) {
		fprintf(stderr, "qdwin-locker-probe: takeover-while-alive got "
			"code=%u on %s, want locker_present=%d on "
			"qdwin_locker_v1\n", code,
			iface ? iface->name : "(none)",
			QDWIN_LOCKER_V1_ERROR_LOCKER_PRESENT);
		kill(child, SIGKILL); waitpid(child, NULL, 0);
		return 1;
	}
	/* This connection is now in the error state; drop it. */
	wl_display_disconnect(pp.display);
	printf("qdwin-locker-probe: separate-process takeover REFUSED with "
	       "locker_present while holder alive\n");

	/* Kill the holder and wait for it to actually exit so qdwin can
	 * observe the socket close and clear locker_resource. */
	kill(child, SIGKILL);
	waitpid(child, NULL, 0);

	/* Attempt #2: holder is provably dead => the takeover MUST now be
	 * accepted. qdwin clears the binding from the holder's socket-close
	 * event, so give it up to ~2s of retries to process that. */
	int accepted = 0;
	for (int attempt = 0; attempt < 20 && !accepted; attempt++) {
		struct probe rp;
		struct qdwin_locker_v1 *rl = NULL;
		if (fresh_locker_conn(&rp, &rl) != 0) {
			struct timespec ts = { 0, 100 * 1000 * 1000 };
			nanosleep(&ts, NULL);
			continue;
		}
		qdwin_locker_v1_bind_as_locker(rl);
		uint32_t rcode = 0;
		const struct wl_interface *riface = NULL;
		int rerr = roundtrip_err(&rp, "separate-process rebind (holder "
					 "dead)", &rcode, &riface);
		if (rerr == 0 && rp.got_ready) {
			accepted = 1;
			wl_display_disconnect(rp.display);
			break;
		}
		/* Still seeing locker_present: qdwin hasn't processed the
		 * holder's disconnect yet. Back off and retry. */
		wl_display_disconnect(rp.display);
		struct timespec ts = { 0, 100 * 1000 * 1000 };
		nanosleep(&ts, NULL);
	}
	if (!accepted) {
		fprintf(stderr, "qdwin-locker-probe: takeover STILL refused "
			"after the holder died — stale binding never cleared\n");
		return 1;
	}
	printf("qdwin-locker-probe: separate-process takeover ACCEPTED after "
	       "holder died (ready)\n");
	return 6;
}

/* Fully-transparent (premultiplied ARGB 0x00000000) SHM buffer. */
static struct wl_buffer *
make_transparent_buffer(struct wl_shm *shm, int w, int h)
{
	int stride = w * 4, size = stride * h;
	int fd = memfd_create("qdwin-locker-probe", MFD_CLOEXEC);
	if (fd < 0)
		return NULL;
	if (ftruncate(fd, size) < 0) { close(fd); return NULL; }
	uint32_t *px = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (px == MAP_FAILED) { close(fd); return NULL; }
	memset(px, 0, size);  /* 0x00000000 => fully transparent */
	struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, w, h, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	munmap(px, size);
	close(fd);
	return buf;
}

/* --transparent-lock: model a broken/hostile locker that attaches a fully
 * TRANSPARENT lock surface and locks. The compositor's lock curtain must still
 * occlude the desktop (black), never let lower layers show through. Bind as
 * locker, attach a transparent lock surface, set_locked(1), print a readiness
 * marker, then idle (dispatching) until the harness kills us so it can
 * screenshot the locked output. Drives tests/host/26-lock-curtain-occludes.md.
 * This never returns 0 on its own; the harness terminates it. */
static int
run_transparent_lock(void)
{
	struct probe p;
	struct qdwin_locker_v1 *l = NULL;

	if (fresh_locker_conn(&p, &l) != 0)
		return 2;
	if (!p.compositor || !p.shm) {
		fprintf(stderr, "qdwin-locker-probe: transparent-lock needs "
			"wl_compositor + wl_shm (not advertised)\n");
		return 2;
	}
	qdwin_locker_v1_bind_as_locker(l);
	if (roundtrip_err(&p, "bind_as_locker", NULL, NULL) != 0 || !p.got_ready) {
		fprintf(stderr, "qdwin-locker-probe: bind_as_locker/ready "
			"failed\n");
		return 2;
	}

	struct wl_surface *surface = wl_compositor_create_surface(p.compositor);
	struct qdwin_locker_surface_v1 *ls =
		qdwin_locker_v1_attach_lock_surface(l, surface);
	(void)ls;
	struct wl_buffer *buf = make_transparent_buffer(p.shm, 1920, 1080);
	if (!buf) {
		fprintf(stderr, "qdwin-locker-probe: transparent buffer "
			"alloc failed\n");
		return 2;
	}
	wl_surface_attach(surface, buf, 0, 0);
	wl_surface_damage(surface, 0, 0, 1920, 1080);
	wl_surface_commit(surface);
	qdwin_locker_v1_set_locked(l, 1);
	if (wl_display_roundtrip(p.display) < 0)
		return 2;

	/* Readiness marker: the harness screenshots after this line. */
	printf("qdwin-locker-probe: transparent lock engaged\n");
	fflush(stdout);

	while (wl_display_dispatch(p.display) != -1)
		;
	return 0;
}

int main(int argc, char *argv[])
{
	enum { M_DEFAULT, M_DOUBLE, M_REBIND, M_SEP_TAKEOVER,
	       M_TRANSPARENT_LOCK } mode = M_DEFAULT;
	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--double-bind") == 0)
			mode = M_DOUBLE;
		else if (strcmp(argv[i], "--rebind") == 0)
			mode = M_REBIND;
		else if (strcmp(argv[i], "--separate-takeover") == 0)
			mode = M_SEP_TAKEOVER;
		else if (strcmp(argv[i], "--transparent-lock") == 0)
			mode = M_TRANSPARENT_LOCK;
	}

	if (mode == M_SEP_TAKEOVER)
		return run_separate_takeover();
	if (mode == M_TRANSPARENT_LOCK)
		return run_transparent_lock();

	struct probe p = {0};
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-locker-probe: wl_display_connect "
			"failed: %s\n", strerror(errno));
		return 2;
	}
	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);
	wl_display_roundtrip(p.display);

	if (!p.saw_global) {
		/* The locker global is always advertised (gating is at bind
		 * time, not via the registry filter). Not seeing it is a
		 * harness/server problem, not a gate decision. */
		fprintf(stderr, "qdwin-locker-probe: qdwin_locker_v1 not "
			"advertised\n");
		return 2;
	}

	struct qdwin_locker_v1 *l = bind_locker(&p);
	if (!l) {
		fprintf(stderr, "qdwin-locker-probe: bind returned NULL\n");
		return 2;
	}

	/* Resolve the bind itself: an unauthorized uid is rejected here, at
	 * global-bind time, via wl_client_post_implementation_error — which
	 * surfaces as WL_DISPLAY_ERROR_IMPLEMENTATION (=3) on the wl_display.
	 * Verify it is SPECIFICALLY that error, not an unrelated disconnect,
	 * so the uid-reject case can't false-pass on a crash. */
	{
		uint32_t code = 0;
		const struct wl_interface *iface = NULL;
		if (roundtrip_err(&p, "locker global bind", &code, &iface) != 0) {
			if (iface == &wl_display_interface &&
			    code == WL_DISPLAY_ERROR_IMPLEMENTATION) {
				/* The EXPECTED uid reject. Distinct exit code 4
				 * so a scenario can require this specific error
				 * and not false-pass on an unrelated failure
				 * (which exits 1 below). */
				printf("qdwin-locker-probe: locker bind REFUSED "
				       "with implementation error\n");
				return 4;
			}
			fprintf(stderr, "qdwin-locker-probe: bind failed but not "
				"with the expected implementation error "
				"(code=%u iface=%s)\n", code,
				iface ? iface->name : "(none)");
			return 1;
		}
	}

	if (mode == M_REBIND) {
		/* Second resource on the SAME client. */
		struct qdwin_locker_v1 *l2 = bind_locker(&p);
		if (!l2) {
			fprintf(stderr, "qdwin-locker-probe: 2nd bind NULL\n");
			return 2;
		}
		if (roundtrip_err(&p, "locker 2nd global bind", NULL, NULL) != 0)
			return 1;
		qdwin_locker_v1_bind_as_locker(l);
		if (roundtrip_err(&p, "first bind_as_locker", NULL, NULL) != 0)
			return 1;
		if (!p.got_ready) {
			fprintf(stderr, "qdwin-locker-probe: first bind_as_locker "
				"accepted but no `ready`\n");
			return 1;
		}
		/* FINDING #5 fix: the first locker (this very process) is still
		 * alive, so qdwin must REFUSE the takeover with locker_present
		 * instead of evicting the live locker. */
		uint32_t code = 0;
		const struct wl_interface *iface = NULL;
		qdwin_locker_v1_bind_as_locker(l2);
		int err = roundtrip_err(&p, "second bind_as_locker (rebind)",
					&code, &iface);
		if (err == 0) {
			fprintf(stderr, "qdwin-locker-probe: second bind_as_locker "
				"was NOT rejected — live locker was evicted "
				"(takeover vuln)\n");
			return 1;
		}
		if (iface != &qdwin_locker_v1_interface ||
		    code != QDWIN_LOCKER_V1_ERROR_LOCKER_PRESENT) {
			fprintf(stderr, "qdwin-locker-probe: rebind got code=%u on "
				"%s, want locker_present=%d on qdwin_locker_v1\n",
				code, iface ? iface->name : "(none)",
				QDWIN_LOCKER_V1_ERROR_LOCKER_PRESENT);
			return 1;
		}
		printf("qdwin-locker-probe: rebind REFUSED with locker_present "
		       "(live locker not evicted)\n");
		return 5;
	}

	/* Default + double-bind both do a first bind_as_locker. */
	qdwin_locker_v1_bind_as_locker(l);
	if (roundtrip_err(&p, "bind_as_locker", NULL, NULL) != 0)
		return 1;
	if (!p.got_ready) {
		fprintf(stderr, "qdwin-locker-probe: bind_as_locker accepted "
			"but no `ready` event\n");
		return 1;
	}

	if (mode == M_DOUBLE) {
		uint32_t code = 0;
		const struct wl_interface *iface = NULL;
		qdwin_locker_v1_bind_as_locker(l);  /* second on same resource */
		int err = roundtrip_err(&p, "double bind_as_locker", &code,
					&iface);
		if (err == 0) {
			fprintf(stderr, "qdwin-locker-probe: second "
				"bind_as_locker was NOT rejected\n");
			return 1;
		}
		/* Verify it is the already_bound error ON qdwin_locker_v1 — not
		 * an unrelated protocol error that happens to share code 1. */
		if (iface != &qdwin_locker_v1_interface ||
		    code != QDWIN_LOCKER_V1_ERROR_ALREADY_BOUND) {
			fprintf(stderr, "qdwin-locker-probe: double bind got "
				"code=%u on %s, want already_bound=%d on "
				"qdwin_locker_v1\n", code,
				iface ? iface->name : "(none)",
				QDWIN_LOCKER_V1_ERROR_ALREADY_BOUND);
			return 1;
		}
		printf("qdwin-locker-probe: double bind_as_locker REJECTED "
		       "with already_bound\n");
		return 3;
	}

	printf("qdwin-locker-probe: locker bind ACCEPTED (ready)\n");
	return 0;
}
