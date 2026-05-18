/*
 * §6.7 primary-selection-unstable-v1 end-to-end probe (C).
 *
 * Background: pywayland 0.4.x can't decode new_id events on the
 * client side (ArgumentType.NewId: raise NotImplementedError in
 * pywayland/protocol_core/message.py:150). The primary-selection
 * device emits `data_offer(new_id)` on every selection, so a Python
 * consumer can't attach dispatchers to the offer. This C probe
 * exercises the full round-trip: A (producer) creates a source and
 * sets it as the primary selection; B (consumer) waits for the
 * selection event, calls offer.receive(mime, write_fd) with a pipe,
 * reads bytes until EOF, and verifies the payload matches.
 *
 * Build: cc s9-primary-selection.c primary-selection-unstable-v1.c \
 *           -o s9-primary-selection -lwayland-client
 * The -protocol.c source is generated at build time by wayland-scanner
 * from the wayland-protocols package XML.
 *
 * Run: both A and B connect to the same compositor + wl_seat. We fork
 * so each side has its own wl_display connection. Parent is B (needs
 * to be the one waiting); child is A (creates source + holds alive).
 * Child exits once parent signals completion via a sync pipe.
 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>

#include "primary-selection-unstable-v1-client-protocol.h"

static const char EXPECT[] = "hello from A\n";

struct bound {
	struct wl_compositor *compositor;
	struct wl_seat *seat;
	struct zwp_primary_selection_device_manager_v1 *pm;
};

static void
reg_global(void *data, struct wl_registry *r, uint32_t name,
	   const char *iface, uint32_t version)
{
	struct bound *b = data;
	if (!strcmp(iface, wl_compositor_interface.name) && !b->compositor)
		b->compositor = wl_registry_bind(r, name,
						 &wl_compositor_interface,
						 version < 4 ? version : 4);
	else if (!strcmp(iface, wl_seat_interface.name) && !b->seat)
		b->seat = wl_registry_bind(r, name, &wl_seat_interface,
					   version < 5 ? version : 5);
	else if (!strcmp(iface,
			 zwp_primary_selection_device_manager_v1_interface.name)
		 && !b->pm)
		b->pm = wl_registry_bind(
			r, name,
			&zwp_primary_selection_device_manager_v1_interface, 1);
}

static void
reg_remove(void *data, struct wl_registry *r, uint32_t name)
{
	(void)data; (void)r; (void)name;
}

static const struct wl_registry_listener reg_listener = {
	.global = reg_global,
	.global_remove = reg_remove,
};

static void
bind_globals(struct wl_display *dpy, struct bound *out)
{
	memset(out, 0, sizeof *out);
	struct wl_registry *reg = wl_display_get_registry(dpy);
	wl_registry_add_listener(reg, &reg_listener, out);
	wl_display_roundtrip(dpy);
	wl_registry_destroy(reg);
}

/* ------------------------------------------------------------------
 * A side (producer).
 * ------------------------------------------------------------------ */

static void
src_send(void *data, struct zwp_primary_selection_source_v1 *src,
	 const char *mime, int32_t fd)
{
	(void)data; (void)src;
	fprintf(stderr, "A: send(%s, fd=%d)\n", mime, fd);
	ssize_t n = write(fd, EXPECT, sizeof EXPECT - 1);
	(void)n;
	close(fd);
}

static void
src_cancelled(void *data, struct zwp_primary_selection_source_v1 *src)
{
	(void)data; (void)src;
	fprintf(stderr, "A: cancelled\n");
}

static const struct zwp_primary_selection_source_v1_listener src_listener = {
	.send = src_send,
	.cancelled = src_cancelled,
};

static int
run_A(int sync_done_fd)
{
	struct wl_display *dpy = wl_display_connect(NULL);
	if (!dpy) { fprintf(stderr, "A: connect failed\n"); return 20; }
	struct bound b; bind_globals(dpy, &b);
	if (!b.pm || !b.seat) {
		fprintf(stderr, "A: missing pm=%p seat=%p\n",
			(void*)b.pm, (void*)b.seat);
		return 21;
	}
	struct zwp_primary_selection_device_v1 *dev =
		zwp_primary_selection_device_manager_v1_get_device(b.pm,
								   b.seat);
	struct zwp_primary_selection_source_v1 *src =
		zwp_primary_selection_device_manager_v1_create_source(b.pm);
	zwp_primary_selection_source_v1_add_listener(src, &src_listener, NULL);
	zwp_primary_selection_source_v1_offer(src, "text/plain");
	zwp_primary_selection_source_v1_offer(src, "text/plain;charset=utf-8");
	zwp_primary_selection_device_v1_set_selection(dev, src, 0);
	wl_display_flush(dpy);
	fprintf(stderr, "A: selection set\n");

	struct pollfd pfds[2];
	pfds[0].fd = wl_display_get_fd(dpy);
	pfds[0].events = POLLIN;
	pfds[1].fd = sync_done_fd;
	pfds[1].events = POLLIN;
	for (;;) {
		wl_display_flush(dpy);
		int rc = poll(pfds, 2, 5000);
		if (rc < 0 && errno == EINTR) continue;
		if (pfds[0].revents & POLLIN) {
			if (wl_display_dispatch(dpy) < 0) {
				fprintf(stderr, "A: dispatch error\n");
				break;
			}
		}
		if (pfds[1].revents & (POLLIN | POLLHUP)) {
			fprintf(stderr, "A: parent signalled done\n");
			break;
		}
		if (rc == 0) {
			fprintf(stderr, "A: timeout waiting for send\n");
			break;
		}
	}
	zwp_primary_selection_source_v1_destroy(src);
	zwp_primary_selection_device_v1_destroy(dev);
	wl_display_disconnect(dpy);
	return 0;
}

/* ------------------------------------------------------------------
 * B side (consumer).
 * ------------------------------------------------------------------ */

struct B_state {
	struct zwp_primary_selection_offer_v1 *current_offer;
	int got_offer_mime;  /* incremented by offer.offer events */
	int got_selection;   /* 1 after selection event with non-NULL */
};

static void
offer_on_offer(void *data, struct zwp_primary_selection_offer_v1 *offer,
	       const char *mime)
{
	struct B_state *st = data;
	(void)offer;
	fprintf(stderr, "B: offer mime=%s\n", mime);
	st->got_offer_mime++;
}

static const struct zwp_primary_selection_offer_v1_listener offer_listener = {
	.offer = offer_on_offer,
};

static void
dev_data_offer(void *data, struct zwp_primary_selection_device_v1 *dev,
	       struct zwp_primary_selection_offer_v1 *offer)
{
	struct B_state *st = data;
	(void)dev;
	fprintf(stderr, "B: data_offer received\n");
	if (st->current_offer)
		zwp_primary_selection_offer_v1_destroy(st->current_offer);
	st->current_offer = offer;
	zwp_primary_selection_offer_v1_add_listener(offer, &offer_listener,
						    st);
}

static void
dev_selection(void *data, struct zwp_primary_selection_device_v1 *dev,
	      struct zwp_primary_selection_offer_v1 *offer)
{
	struct B_state *st = data;
	(void)dev;
	fprintf(stderr, "B: selection offer=%p\n", (void*)offer);
	if (offer == NULL) {
		/* Selection cleared. */
		st->got_selection = 0;
	} else {
		st->got_selection = 1;
	}
}

static const struct zwp_primary_selection_device_v1_listener dev_listener = {
	.data_offer = dev_data_offer,
	.selection = dev_selection,
};

static int
run_B(void)
{
	struct wl_display *dpy = wl_display_connect(NULL);
	if (!dpy) { fprintf(stderr, "B: connect failed\n"); return 30; }
	struct bound b; bind_globals(dpy, &b);
	if (!b.pm || !b.seat) {
		fprintf(stderr, "B: missing pm=%p seat=%p\n",
			(void*)b.pm, (void*)b.seat);
		return 31;
	}
	struct zwp_primary_selection_device_v1 *dev =
		zwp_primary_selection_device_manager_v1_get_device(b.pm,
								   b.seat);
	struct B_state st = { 0 };
	zwp_primary_selection_device_v1_add_listener(dev, &dev_listener, &st);

	/* Wait up to 3 s for selection + ≥1 offer mime. */
	for (int i = 0; i < 30; i++) {
		wl_display_roundtrip(dpy);
		if (st.current_offer && st.got_offer_mime && st.got_selection)
			break;
		struct timespec ts = { .tv_sec = 0, .tv_nsec = 100*1000*1000 };
		nanosleep(&ts, NULL);
	}
	if (!st.current_offer) {
		fprintf(stderr, "B: FAIL no offer\n");
		return 32;
	}
	if (!st.got_selection) {
		fprintf(stderr, "B: FAIL no selection event\n");
		return 33;
	}
	fprintf(stderr, "B: offer ready, issuing receive\n");

	int p[2]; if (pipe(p) != 0) return 34;
	zwp_primary_selection_offer_v1_receive(st.current_offer,
					       "text/plain", p[1]);
	wl_display_flush(dpy);
	close(p[1]);
	/* Drain display once so A's `send` gets dispatched. */
	wl_display_roundtrip(dpy);

	char buf[256] = { 0 };
	size_t total = 0;
	struct pollfd pfd = { .fd = p[0], .events = POLLIN };
	for (;;) {
		int rc = poll(&pfd, 1, 3000);
		if (rc <= 0) break;
		ssize_t n = read(p[0], buf + total,
				 sizeof buf - 1 - total);
		if (n <= 0) break;
		total += (size_t)n;
		if (total >= sizeof buf - 1) break;
	}
	close(p[0]);

	fprintf(stderr, "B: read %zu bytes: %.*s\n", total,
		(int)total, buf);

	zwp_primary_selection_offer_v1_destroy(st.current_offer);
	zwp_primary_selection_device_v1_destroy(dev);
	wl_display_disconnect(dpy);

	if (total != sizeof EXPECT - 1 || memcmp(buf, EXPECT, total) != 0) {
		fprintf(stderr, "B: FAIL content mismatch\n");
		return 35;
	}
	fprintf(stderr, "B: PASS\n");
	return 0;
}

int main(void)
{
	int sync_pipe[2];
	if (pipe(sync_pipe) != 0) { perror("pipe"); return 1; }

	pid_t pid = fork();
	if (pid < 0) { perror("fork"); return 1; }
	if (pid == 0) {
		/* Child = A. */
		close(sync_pipe[1]);
		int rc = run_A(sync_pipe[0]);
		close(sync_pipe[0]);
		_exit(rc);
	}
	close(sync_pipe[0]);

	/* Give A a moment to set the selection before B connects. */
	struct timespec ts = { .tv_sec = 0, .tv_nsec = 200*1000*1000 };
	nanosleep(&ts, NULL);

	int rc = run_B();
	/* Signal A to exit. */
	close(sync_pipe[1]);
	int status = 0;
	waitpid(pid, &status, 0);
	if (rc != 0) {
		fprintf(stderr, "FAIL: rc=%d\n", rc);
		return rc;
	}
	fprintf(stderr, "PASS: §6.7 primary-selection end-to-end (C)\n");
	return 0;
}
