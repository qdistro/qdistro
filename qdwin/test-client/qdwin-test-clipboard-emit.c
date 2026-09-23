/*
 * qdwin-test-clipboard-emit — inject a cross-silo clipboard selection event
 * via wp_security_context_v1 so that qdshell's ClipboardGate.qml fires its
 * CLIPBOARD_GATE journal line with the given src_silo / dst_silo values.
 *
 * ClipboardGate.qml tracks handle → silo from the compositor's
 * toplevel_security_context event (qdwin_shell_v1 v13+).  The
 * wp_security_context_v1 protocol lets a client tag an inbound socket
 * descriptor at the compositor level: every client that connects through the
 * listening fd inherits the sandbox_engine / app_id / instance_id tags.
 * qdwin maps sandbox_engine="qdistro-silo" + app_id=<silo_name> to the silo
 * identity it advertises to bound shells.
 *
 * Mechanism:
 *   1. Open a unix socketpair (listen_fd, close_fd).
 *   2. On the outer compositor connection, call
 *      wp_security_context_manager_v1.create_listener(listen_fd, close_fd)
 *      then set sandbox_engine="qdistro-silo", app_id=<source_silo>,
 *      instance_id="test-emit", commit().
 *   3. Connect a fresh wl_display through the listen_fd end — this
 *      connection inherits the security context.
 *   4. On the tagged connection: bind wl_data_device_manager + wl_seat,
 *      create a data_source, call set_selection(source, serial=0).
 *   5. Keep the source alive for --hold-ms (default 800 ms) to allow the
 *      compositor to deliver the selection_set event to any bound shell and
 *      the journal line to be written.
 *   6. Close the close_fd to signal done, disconnect, exit.
 *
 * Usage:
 *   qdwin-test-clipboard-emit \
 *       --source-silo <name>   # silo that "owns" the clipboard
 *       --dest-silo   <name>   # future destination (informational; ClipboardGate
 *                              #   determines the actual dest from focus — this
 *                              #   arg is printed to stderr for tracing only)
 *       --mime        <type>   # MIME type to advertise (default: text/plain)
 *       --hold-ms     <n>      # ms to hold the selection open (default: 800)
 *
 * Exit codes:
 *   0 — selection was set successfully (selection_set event delivered
 *       to compositor; ClipboardGate should have logged CLIPBOARD_GATE)
 *   1 — fatal error (compositor not reachable, missing protocol, etc.)
 *   2 — bad arguments
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>

/* Hand-rolled minimal client interface tables for security-context-v1,
 * mirroring what wayland-scanner would emit so we don't add a build
 * dependency on the generated header. Layout must match the
 * scanner output exactly:
 *
 *   types[] = { NULL, &wp_security_context_v1_interface, NULL, NULL }
 *   create_listener.signature = "nhh", types_ptr = types + 1
 *
 * The earlier version had the types table reversed AND the signature
 * as "hho", which combined with a missing NULL placeholder for the
 * new_id in wl_proxy_marshal_flags made the call silently return NULL
 * (surfaced as "create_listener failed").
 */

static const struct wl_interface wp_security_context_v1_interface;

static const struct wl_interface *security_context_v1_types[] = {
	NULL,
	&wp_security_context_v1_interface,
	NULL,
	NULL,
};

/* ---------- wp_security_context_manager_v1 -------------------------------- */
/* destroy is opcode 0, create_listener is opcode 1 — matches the order
 * in the request table below AND wayland-scanner output. Earlier this
 * was 0, which sent a malformed destroy request and surfaced as
 * EAGAIN ("create_listener failed"). */
#define WP_SECURITY_CONTEXT_MANAGER_V1_CREATE_LISTENER 1

static const struct wl_message
wp_security_context_manager_v1_requests[] = {
	{ "destroy",         "",    security_context_v1_types + 0 },
	{ "create_listener", "nhh", security_context_v1_types + 1 },
};

static const struct wl_interface wp_security_context_manager_v1_interface = {
	"wp_security_context_manager_v1", 1,
	2, wp_security_context_manager_v1_requests,
	0, NULL,
};

/* ---------- wp_security_context_v1 ---------------------------------------- */
#define WP_SECURITY_CONTEXT_V1_DESTROY          0
#define WP_SECURITY_CONTEXT_V1_SET_SANDBOX_ENGINE 1
#define WP_SECURITY_CONTEXT_V1_SET_APP_ID       2
#define WP_SECURITY_CONTEXT_V1_SET_INSTANCE_ID  3
#define WP_SECURITY_CONTEXT_V1_COMMIT           4

static const struct wl_message wp_security_context_v1_requests_msgs[] = {
	{ "destroy",           "",  security_context_v1_types + 0 },
	{ "set_sandbox_engine","s", security_context_v1_types + 0 },
	{ "set_app_id",        "s", security_context_v1_types + 0 },
	{ "set_instance_id",   "s", security_context_v1_types + 0 },
	{ "commit",            "",  security_context_v1_types + 0 },
};

static const struct wl_interface wp_security_context_v1_interface = {
	"wp_security_context_v1", 1,
	5, wp_security_context_v1_requests_msgs,
	0, NULL,
};

/* ---------- helpers -------------------------------------------------------- */

static inline struct wl_proxy *
create_security_context(struct wl_proxy *manager,
                        int listen_fd, int close_fd)
{
	/* wl_proxy_marshal_flags expects a NULL placeholder for the
	 * new_id argument (it allocates the proxy itself); without it
	 * libwayland reads the fd as the new_id and returns NULL. */
	return wl_proxy_marshal_flags(
		manager,
		WP_SECURITY_CONTEXT_MANAGER_V1_CREATE_LISTENER,
		&wp_security_context_v1_interface,
		wl_proxy_get_version(manager),
		0,
		NULL, listen_fd, close_fd);
}

static inline void
security_context_set_sandbox_engine(struct wl_proxy *ctx, const char *engine)
{
	wl_proxy_marshal_flags(ctx, WP_SECURITY_CONTEXT_V1_SET_SANDBOX_ENGINE,
	                       NULL, wl_proxy_get_version(ctx), 0, engine);
}

static inline void
security_context_set_app_id(struct wl_proxy *ctx, const char *app_id)
{
	wl_proxy_marshal_flags(ctx, WP_SECURITY_CONTEXT_V1_SET_APP_ID,
	                       NULL, wl_proxy_get_version(ctx), 0, app_id);
}

static inline void
security_context_set_instance_id(struct wl_proxy *ctx, const char *iid)
{
	wl_proxy_marshal_flags(ctx, WP_SECURITY_CONTEXT_V1_SET_INSTANCE_ID,
	                       NULL, wl_proxy_get_version(ctx), 0, iid);
}

static inline void
security_context_commit(struct wl_proxy *ctx)
{
	wl_proxy_marshal_flags(ctx, WP_SECURITY_CONTEXT_V1_COMMIT,
	                       NULL, wl_proxy_get_version(ctx), 0);
}

/* ========================================================================== */

/* State for the tagged connection that sets the clipboard. */
struct tagged_ctx {
	struct wl_display            *display;
	struct wl_data_device_manager *ddm;
	struct wl_seat               *seat;
	struct wl_data_device        *device;
	struct wl_data_source        *source;
	int                           ready;   /* 1 after set_selection sent */
};

static void
ds_target(void *d, struct wl_data_source *s, const char *mime)
{ (void)d; (void)s; (void)mime; }

static void
ds_send(void *d, struct wl_data_source *s,
        const char *mime, int32_t fd)
{
	(void)d; (void)s; (void)mime;
	static const char payload[] = "qdwin-test-clipboard-emit";
	if (write(fd, payload, sizeof(payload)-1) < 0) {
		/* best effort — ignore EPIPE / ECONNRESET */
	}
	close(fd);
}

static void
ds_cancelled(void *d, struct wl_data_source *s)
{
	(void)s;
	struct tagged_ctx *c = d;
	c->ready = -1;   /* signal done */
	fprintf(stderr, "[qdwin-test-clipboard-emit] selection cancelled\n");
}

static void ds_dnd_drop(void *d, struct wl_data_source *s) { (void)d; (void)s; }
static void ds_dnd_fin(void *d, struct wl_data_source *s)  { (void)d; (void)s; }
static void ds_action(void *d, struct wl_data_source *s, uint32_t a)
{ (void)d; (void)s; (void)a; }

static const struct wl_data_source_listener ds_listener = {
	.target              = ds_target,
	.send                = ds_send,
	.cancelled           = ds_cancelled,
	.dnd_drop_performed  = ds_dnd_drop,
	.dnd_finished        = ds_dnd_fin,
	.action              = ds_action,
};

static void
tagged_seat_caps(void *d, struct wl_seat *s, uint32_t caps)
{ (void)d; (void)s; (void)caps; }
static void
tagged_seat_name(void *d, struct wl_seat *s, const char *name)
{ (void)d; (void)s; (void)name; }
static const struct wl_seat_listener tagged_seat_listener = {
	.capabilities = tagged_seat_caps,
	.name         = tagged_seat_name,
};

static void
tagged_registry_global(void *data, struct wl_registry *reg,
                        uint32_t name, const char *iface, uint32_t ver)
{
	struct tagged_ctx *c = data;
	if (!strcmp(iface, wl_data_device_manager_interface.name)) {
		c->ddm = wl_registry_bind(reg, name,
		                          &wl_data_device_manager_interface,
		                          ver > 3 ? 3 : ver);
	} else if (!strcmp(iface, wl_seat_interface.name) && !c->seat) {
		c->seat = wl_registry_bind(reg, name, &wl_seat_interface,
		                           ver > 5 ? 5 : ver);
		wl_seat_add_listener(c->seat, &tagged_seat_listener, c);
	}
}

static void
tagged_registry_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }

static const struct wl_registry_listener tagged_registry_listener = {
	.global        = tagged_registry_global,
	.global_remove = tagged_registry_remove,
};

/* ========================================================================== */

/* Outer connection state — used to bind wp_security_context_manager_v1. */
struct outer_ctx {
	struct wl_proxy *secmgr;  /* wp_security_context_manager_v1 */
};

static void
outer_registry_global(void *data, struct wl_registry *reg,
                       uint32_t name, const char *iface, uint32_t ver)
{
	struct outer_ctx *c = data;
	if (!strcmp(iface, wp_security_context_manager_v1_interface.name)) {
		c->secmgr = wl_registry_bind(
			reg, name,
			&wp_security_context_manager_v1_interface,
			ver > 1 ? 1 : ver);
	}
}
static void
outer_registry_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }

static const struct wl_registry_listener outer_registry_listener = {
	.global        = outer_registry_global,
	.global_remove = outer_registry_remove,
};

/* ========================================================================== */

static void ms_sleep(int ms)
{
	struct timespec ts = { .tv_sec = ms / 1000,
	                       .tv_nsec = (ms % 1000) * 1000000L };
	nanosleep(&ts, NULL);
}

/* Stashed write-end of the close_fd pipe; main() closes this after the
 * hold-ms window so the compositor sees POLLHUP and tears down its
 * listener cleanly. See make_unix_socket_pair() for the rationale. */
int g_emit_close_signal_fd = -1;

/* Build a path to a unix socket inside $XDG_RUNTIME_DIR. */
static int
make_unix_socket_pair(int *listen_out, int *close_out)
{
	const char *xrd = getenv("XDG_RUNTIME_DIR");
	if (!xrd) xrd = "/run/user/1000";

	/* listen side: a real unix socket the compositor accepts on */
	int lfd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (lfd < 0) {
		perror("socket");
		return -1;
	}

	/* Use an abstract socket name to avoid filesystem cleanup */
	struct sockaddr_un sa = { .sun_family = AF_UNIX };
	snprintf(sa.sun_path + 1, sizeof(sa.sun_path) - 1,
	         "qdwin-test-emit-%d", (int)getpid());
	if (bind(lfd, (struct sockaddr *)&sa,
	         sizeof(sa.sun_family) + 1 + strlen(sa.sun_path + 1)) < 0) {
		perror("bind");
		close(lfd);
		return -1;
	}
	if (listen(lfd, 4) < 0) {
		perror("listen");
		close(lfd);
		return -1;
	}

	/* close side: any fd whose hangup tells the compositor to stop.
	 *
	 * security-context-v1 contract: the compositor polls the fd we
	 * hand it as close_fd for POLLHUP. POLLHUP on the read end of a
	 * pipe fires when ALL writers close. So:
	 *   - give the compositor the READ end (pfd[0]) as close_fd
	 *   - keep the WRITE end (pfd[1]) in our process; close it
	 *     when we want the compositor to stop accepting
	 *
	 * The previous wiring was reversed (compositor received pfd[1]
	 * and we closed pfd[0] immediately): POLLHUP on a pipe's
	 * write end fires the moment the read side is closed, so the
	 * compositor tore down the listener before our tagged client
	 * could connect through it ("close_fd hangup ... → tearing
	 * down listener" 0ms after "client accepted"). The resulting
	 * selection_set never reached the bound shell, so
	 * ClipboardGate logged the fallback src_silo=unknown verdict.
	 *
	 * Callers must close *close_out + the stashed write-end fd
	 * at the end of the hold window. */
	int pfd[2];
	if (pipe2(pfd, O_CLOEXEC) < 0) {
		perror("pipe2");
		close(lfd);
		return -1;
	}
	*listen_out = lfd;
	*close_out  = pfd[0];   /* compositor reads; ours to close after send */
	/* Stash the write end in the global so main() can close it
	 * after the hold-ms window. */
	g_emit_close_signal_fd = pfd[1];
	return 0;
}

/* Connect through the abstract socket named as listen_fd above. */
static struct wl_display *
connect_through_abstract(int listen_fd_addr_pid)
{
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0) return NULL;

	struct sockaddr_un sa = { .sun_family = AF_UNIX };
	snprintf(sa.sun_path + 1, sizeof(sa.sun_path) - 1,
	         "qdwin-test-emit-%d", listen_fd_addr_pid);
	if (connect(fd, (struct sockaddr *)&sa,
	            sizeof(sa.sun_family) + 1 + strlen(sa.sun_path + 1)) < 0) {
		perror("connect through abstract socket");
		close(fd);
		return NULL;
	}
	return wl_display_connect_to_fd(fd);
}

/* ========================================================================== */

int main(int argc, char **argv)
{
	const char *src_silo  = NULL;
	const char *dst_silo  = NULL;
	const char *mime      = "text/plain";
	int         hold_ms   = 800;

	static const struct option opts[] = {
		{ "source-silo", required_argument, 0, 's' },
		{ "dest-silo",   required_argument, 0, 'd' },
		{ "mime",        required_argument, 0, 'm' },
		{ "hold-ms",     required_argument, 0, 'H' },
		{ 0, 0, 0, 0 },
	};
	int o;
	while ((o = getopt_long(argc, argv, "s:d:m:H:", opts, NULL)) != -1) {
		switch (o) {
		case 's': src_silo = optarg; break;
		case 'd': dst_silo = optarg; break;
		case 'm': mime     = optarg; break;
		case 'H': hold_ms  = atoi(optarg); break;
		default:
			fprintf(stderr,
			        "usage: %s --source-silo S --dest-silo D "
			        "[--mime M] [--hold-ms N]\n", argv[0]);
			return 2;
		}
	}
	if (!src_silo || !dst_silo) {
		fprintf(stderr, "error: --source-silo and --dest-silo are required\n");
		return 2;
	}

	fprintf(stderr,
	        "[qdwin-test-clipboard-emit] src_silo=%s dst_silo=%s mime=%s\n",
	        src_silo, dst_silo, mime);

	/* ---- 1. Outer connection — bind wp_security_context_manager_v1 ----- */

	struct wl_display *outer = wl_display_connect(NULL);
	if (!outer) {
		fprintf(stderr, "error: wl_display_connect failed — "
		        "WAYLAND_DISPLAY not set or compositor unreachable\n");
		return 1;
	}

	struct outer_ctx octx = {0};
	struct wl_registry *oreg = wl_display_get_registry(outer);
	wl_registry_add_listener(oreg, &outer_registry_listener, &octx);
	wl_display_roundtrip(outer);

	if (!octx.secmgr) {
		fprintf(stderr, "error: wp_security_context_manager_v1 not "
		        "advertised by compositor — qdwin must be at v10+ "
		        "and security-context support compiled in\n");
		wl_display_disconnect(outer);
		return 1;
	}

	/* ---- 2. Create the tagged listening socket -------------------------- */

	int listen_fd = -1, close_fd = -1;
	if (make_unix_socket_pair(&listen_fd, &close_fd) < 0) {
		wl_display_disconnect(outer);
		return 1;
	}

	/* Create security context on the outer connection. */
	struct wl_proxy *secctx =
		create_security_context(octx.secmgr, listen_fd, close_fd);
	if (!secctx) {
		fprintf(stderr, "error: create_listener failed\n");
		close(listen_fd); close(close_fd);
		wl_display_disconnect(outer);
		return 1;
	}

	/* qdwin maps sandbox_engine="qdistro-silo" + app_id=<name> to a silo. */
	security_context_set_sandbox_engine(secctx, "qdistro-silo");
	security_context_set_app_id(secctx, src_silo);
	security_context_set_instance_id(secctx, "qdwin-test-emit");
	security_context_commit(secctx);
	wl_display_flush(outer);
	/* The compositor has its own dup'd close_fd via SCM_RIGHTS now;
	 * our local copy is redundant. Keeping it open would just delay
	 * the eventual POLLHUP for the compositor (POLLHUP only fires
	 * when EVERY writer/holder of the matching end is gone, but
	 * what matters here is that the write end (g_emit_close_signal_fd)
	 * is the only signal — closing the read end has no effect on
	 * the compositor's POLLHUP). Close it for hygiene. */
	close(close_fd);
	close_fd = -1;

	/* ---- 3. Connect tagged client through the listening socket ---------- */

	struct wl_display *tagged = connect_through_abstract((int)getpid());
	if (!tagged) {
		fprintf(stderr, "error: could not connect through security-context "
		        "socket — compositor may not have accepted it yet\n");
		close(close_fd);
		wl_display_disconnect(outer);
		return 1;
	}

	struct tagged_ctx tctx = {0};
	tctx.display = tagged;

	struct wl_registry *treg = wl_display_get_registry(tagged);
	wl_registry_add_listener(treg, &tagged_registry_listener, &tctx);
	wl_display_roundtrip(tagged);

	if (!tctx.ddm || !tctx.seat) {
		fprintf(stderr, "error: missing globals on tagged connection — "
		        "ddm=%p seat=%p\n", (void*)tctx.ddm, (void*)tctx.seat);
		wl_display_disconnect(tagged);
		close(close_fd);
		wl_display_disconnect(outer);
		return 1;
	}

	/* ---- 4. Set clipboard selection on the tagged connection ------------ */

	tctx.device = wl_data_device_manager_get_data_device(tctx.ddm, tctx.seat);
	tctx.source = wl_data_device_manager_create_data_source(tctx.ddm);
	wl_data_source_add_listener(tctx.source, &ds_listener, &tctx);
	wl_data_source_offer(tctx.source, mime);
	/* Bypass weston's stale-serial guard in weston_seat_set_selection():
	 *
	 *   if (seat->selection_data_source &&
	 *       seat->selection_serial - serial < UINT32_MAX / 2)
	 *           return;
	 *
	 * The guard rejects whenever (selection_serial - serial) lies in
	 * the lower half of the 32-bit space. We can't read the
	 * compositor's seat->selection_serial from the client side, so
	 * we sweep three strictly-ascending serials whose accepting
	 * half-rings cover the WHOLE 32-bit ring (any current S lies in
	 * at least one of their accepting halves):
	 *
	 *   R1 = 0x40000001   accepts S in [0xC0000002 .. 0x40000000]
	 *   R2 = 0x80000002   accepts S in [0x00000001 .. 0x80000001]
	 *   R3 = 0xC0000003   accepts S in [0x40000002 .. 0xC0000002]
	 *
	 * The first call uses tctx.source. Each subsequent call uses a
	 * FRESH wl_data_source — weston's seat_set_selection cancels
	 * (and may destroy) the previously-active source whenever a new
	 * one displaces it, which would trip our source's `cancelled`
	 * callback and short-circuit the hold loop. Sweeping with
	 * fresh sources keeps tctx.source untouched once it wins.
	 *
	 * Because the serials are strictly ascending, once any R wins,
	 * the next R's diff is -0x40000001 mod 2^32 = 0xBFFFFFFF (>
	 * UINT32_MAX/2) — so any later Rs also pass. The LAST winning
	 * call leaves its (fresh) source active, NOT tctx.source. We
	 * therefore order the calls so tctx.source goes LAST. */
	struct wl_data_source *probe1 =
		wl_data_device_manager_create_data_source(tctx.ddm);
	struct wl_data_source *probe2 =
		wl_data_device_manager_create_data_source(tctx.ddm);
	wl_data_source_offer(probe1, mime);
	wl_data_source_offer(probe2, mime);
	wl_data_device_set_selection(tctx.device, probe1, 0x40000001u);
	wl_data_device_set_selection(tctx.device, probe2, 0x80000002u);
	wl_data_device_set_selection(tctx.device, tctx.source, 0xC0000003u);
	wl_display_flush(tagged);

	fprintf(stderr, "[qdwin-test-clipboard-emit] set_selection sent "
	        "(src_silo=%s mime=%s); holding %d ms\n",
	        src_silo, mime, hold_ms);

	/* ---- 5. Hold the selection open so ClipboardGate can log it -------- */

	struct timespec deadline;
	clock_gettime(CLOCK_MONOTONIC, &deadline);
	deadline.tv_nsec += (long)hold_ms * 1000000L;
	if (deadline.tv_nsec >= 1000000000L) {
		deadline.tv_sec  += 1;
		deadline.tv_nsec -= 1000000000L;
	}

	while (tctx.ready >= 0) {
		struct timespec now;
		clock_gettime(CLOCK_MONOTONIC, &now);
		if (now.tv_sec > deadline.tv_sec ||
		    (now.tv_sec == deadline.tv_sec &&
		     now.tv_nsec >= deadline.tv_nsec))
			break;

		/* Non-blocking dispatch so we can honour the deadline. */
		struct pollfd pfd = {
			.fd     = wl_display_get_fd(tagged),
			.events = POLLIN,
		};
		poll(&pfd, 1, 20);
		if (pfd.revents & POLLIN)
			wl_display_dispatch_pending(tagged);
		wl_display_flush(tagged);
	}

	/* ---- 6. Clean up --------------------------------------------------- */

	if (tctx.source) wl_data_source_destroy(tctx.source);
	if (tctx.device) wl_data_device_release(tctx.device);
	if (tctx.seat)   wl_seat_release(tctx.seat);
	if (tctx.ddm)    wl_data_device_manager_destroy(tctx.ddm);
	wl_display_disconnect(tagged);

	/* Closing the write end of the close pipe signals the compositor
	 * to stop accepting on listen_fd (POLLHUP fires on the dup'd read
	 * end the compositor is polling). close_fd (our read-end copy)
	 * was already closed right after create_listener flushed. */
	if (g_emit_close_signal_fd >= 0) close(g_emit_close_signal_fd);
	if (close_fd >= 0) close(close_fd);
	close(listen_fd);
	wl_display_disconnect(outer);

	fprintf(stderr,
	        "[qdwin-test-clipboard-emit] done (src_silo=%s dst_silo=%s)\n",
	        src_silo, dst_silo);
	return 0;
}
