/* Test-only weston module for run-inert-relptr-test.sh.
 *
 * Creates a seat with a pointer (as qdwin does for a per-stream RDP seat)
 * and releases it when a process opens a SECOND connection (as qdwin does
 * when the view stream's server state is released) — a trigger only the
 * test client pulls, and one that needs no signal, which the weston frontend
 * reserves. Clients weston spawns itself (desktop-shell helper, ...) come in
 * over a socketpair and so report weston's own pid; they are ignored. A client that still holds that
 * seat's wl_seat or wl_pointer then owns an INERT resource — the state in
 * which qdwin gui/22 S2 crashed the compositor. */
#include <stdlib.h>
#include <sys/types.h>
#include <unistd.h>
#include <wayland-server.h>
#include <libweston/libweston.h>

/* Backend-facing libweston exports, undeclared in the installed plugin
 * header (qdwin/qdwin.c declares them the same way). */
void weston_seat_init(struct weston_seat *seat,
		      struct weston_compositor *ec,
		      const char *seat_name);
void weston_seat_release(struct weston_seat *seat);
int  weston_seat_init_pointer(struct weston_seat *seat);
void weston_seat_release_pointer(struct weston_seat *seat);

struct stale_seat {
	struct weston_seat seat;
	struct wl_listener client_created;
	pid_t pids[16];
	int npids;
	int released;
};

static void
on_client_created(struct wl_listener *listener, void *data)
{
	struct stale_seat *s =
		wl_container_of(listener, s, client_created);
	pid_t pid;
	int i;

	if (s->released)
		return;
	wl_client_get_credentials(data, &pid, NULL, NULL);
	if (pid == getpid())
		return;
	for (i = 0; i < s->npids && s->pids[i] != pid; i++)
		;
	if (i == s->npids) {
		if (s->npids < (int)(sizeof s->pids / sizeof s->pids[0]))
			s->pids[s->npids++] = pid;
		return;
	}
	s->released = 1;
	weston_seat_release_pointer(&s->seat);
	weston_seat_release(&s->seat);
	weston_log("stale-seat-test: seat released\n");
}

WL_EXPORT int
wet_module_init(struct weston_compositor *compositor,
		int *argc, char *argv[])
{
	struct stale_seat *s = calloc(1, sizeof *s);

	if (!s)
		return -1;
	weston_seat_init(&s->seat, compositor, "stale-seat-test");
	if (weston_seat_init_pointer(&s->seat) < 0)
		return -1;
	s->client_created.notify = on_client_created;
	wl_display_add_client_created_listener(compositor->wl_display,
					       &s->client_created);
	weston_log("stale-seat-test: seat ready\n");
	return 0;
}
