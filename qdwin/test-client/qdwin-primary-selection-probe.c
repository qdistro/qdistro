/* qdwin-primary-selection-probe — research probe P-D1.
 *
 * Settles deeper-review finding D1: does the primary selection (middle-click
 * clipboard) have a cross-silo receive gate, the way wl_data_device's v15
 * send-shim does? It is a standalone Wayland client driven in two roles,
 * each wrapped with qdistro-secctx-exec so qdwin tags them as DIFFERENT
 * silos:
 *
 *   --set --mime <m> --text <s>   bind zwp_primary_selection_device_manager_v1,
 *                                 create a source, offer(m), set_selection,
 *                                 then serve `send` events (write the text to
 *                                 the supplied fd). Prints PRIMARY-SET-OK.
 *   --get --mime <m>              on the device `selection` event take the
 *                                 offer, receive(m, fd), read it. Prints
 *                                 PRIMARY-GET-DATA=<bytes> + PRIMARY-GET-OK,
 *                                 or PRIMARY-GET-NO-OFFER / -MIME-ABSENT /
 *                                 -NO-DATA (cross-silo deny → EOF/empty).
 *   --probe-manager               just report whether the manager global is
 *                                 advertised to this client:
 *                                 PRIMARY-MANAGER=present|absent (D1 caveat a).
 *
 * Cross-silo drive (in a VM): instance A `--set` wrapped as silo A, instance
 * B `--get` wrapped as silo B, no qdshell broker answering.
 *   CONCERNING (confirms D1 leak):  B prints PRIMARY-GET-DATA=<A's secret>.
 *   BENIGN (gate works):            B prints PRIMARY-GET-NO-DATA (cross-silo
 *                                   deny) / PRIMARY-MANAGER=absent / -NO-OFFER.
 *
 * Read-only behaviour probe; mutates no repo or persistent state.
 */
#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <time.h>
#include <wayland-client.h>
#include "primary-selection-unstable-v1-client-protocol.h"

enum mode { MODE_NONE, MODE_SET, MODE_GET, MODE_PROBE_MANAGER };

struct state {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_seat *seat;
	struct zwp_primary_selection_device_manager_v1 *manager;
	struct zwp_primary_selection_device_v1 *device;
	uint32_t manager_name;        /* registry name, 0 if not advertised */

	enum mode mode;
	const char *mime;
	const char *text;             /* --set */

	/* --get state */
	struct zwp_primary_selection_offer_v1 *current_offer;
	int   offer_has_mime;
	int   got_selection_event;
	int   done;                   /* terminate the dispatch loop */
};

/* ---- source (--set) listener: write the text to the receiver's fd ---- */
static void
src_send(void *data, struct zwp_primary_selection_source_v1 *src,
	 const char *mime, int32_t fd)
{
	struct state *st = data;
	(void)src; (void)mime;
	if (st->text)
		(void)!write(fd, st->text, strlen(st->text));
	close(fd);
	fprintf(stderr, "[probe] served send mime=%s\n", mime ? mime : "");
}

static void
src_cancelled(void *data, struct zwp_primary_selection_source_v1 *src)
{
	(void)data; (void)src;
	fprintf(stderr, "[probe] source cancelled\n");
}

static const struct zwp_primary_selection_source_v1_listener src_listener = {
	.send = src_send,
	.cancelled = src_cancelled,
};

/* ---- offer (--get) listener: record advertised MIME types ---- */
static void
offer_offer(void *data, struct zwp_primary_selection_offer_v1 *offer,
	    const char *mime)
{
	struct state *st = data;
	(void)offer;
	if (st->mime && mime && strcmp(st->mime, mime) == 0)
		st->offer_has_mime = 1;
	fprintf(stderr, "[probe] offer advertises mime=%s\n", mime ? mime : "");
}

static const struct zwp_primary_selection_offer_v1_listener offer_listener = {
	.offer = offer_offer,
};

/* ---- device (--get) listener ---- */
static void
device_data_offer(void *data, struct zwp_primary_selection_device_v1 *dev,
		  struct zwp_primary_selection_offer_v1 *offer)
{
	struct state *st = data;
	(void)dev;
	st->current_offer = offer;
	st->offer_has_mime = 0;
	zwp_primary_selection_offer_v1_add_listener(offer, &offer_listener, st);
}

static void
device_selection(void *data, struct zwp_primary_selection_device_v1 *dev,
		 struct zwp_primary_selection_offer_v1 *offer)
{
	struct state *st = data;
	(void)dev;
	st->got_selection_event = 1;
	st->current_offer = offer;   /* may be NULL (selection cleared) */
}

static const struct zwp_primary_selection_device_v1_listener device_listener = {
	.data_offer = device_data_offer,
	.selection = device_selection,
};

/* ---- registry ---- */
static void
registry_global(void *data, struct wl_registry *reg, uint32_t name,
		const char *iface, uint32_t version)
{
	struct state *st = data;
	if (strcmp(iface, wl_seat_interface.name) == 0) {
		st->seat = wl_registry_bind(reg, name, &wl_seat_interface,
					    version < 5 ? version : 5);
	} else if (strcmp(iface,
		   zwp_primary_selection_device_manager_v1_interface.name) == 0) {
		st->manager_name = name;
		st->manager = wl_registry_bind(reg, name,
			&zwp_primary_selection_device_manager_v1_interface, 1);
	}
}

static void
registry_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{
	(void)data; (void)reg; (void)name;
}

static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_global_remove,
};

static int
read_all(int fd, char *buf, size_t cap)
{
	size_t off = 0;
	for (;;) {
		struct pollfd pfd = { .fd = fd, .events = POLLIN };
		int pr = poll(&pfd, 1, 2000);
		if (pr <= 0)
			break;          /* timeout or error → stop */
		ssize_t n = read(fd, buf + off, cap - 1 - off);
		if (n <= 0)
			break;          /* EOF (cross-silo deny) or error */
		off += (size_t)n;
		if (off >= cap - 1)
			break;
	}
	buf[off] = '\0';
	return (int)off;
}

int
main(int argc, char **argv)
{
	struct state st = {0};
	st.mode = MODE_NONE;
	st.mime = "text/plain";

	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--set") == 0) st.mode = MODE_SET;
		else if (strcmp(argv[i], "--get") == 0) st.mode = MODE_GET;
		else if (strcmp(argv[i], "--probe-manager") == 0)
			st.mode = MODE_PROBE_MANAGER;
		else if (strcmp(argv[i], "--mime") == 0 && i + 1 < argc)
			st.mime = argv[++i];
		else if (strcmp(argv[i], "--text") == 0 && i + 1 < argc)
			st.text = argv[++i];
	}
	if (st.mode == MODE_NONE) {
		fprintf(stderr, "usage: %s --set|--get|--probe-manager "
			"[--mime M] [--text T]\n", argv[0]);
		return 2;
	}

	st.display = wl_display_connect(NULL);
	if (!st.display) {
		printf("PRIMARY-CONNECT=fail\n");
		return 1;
	}
	st.registry = wl_display_get_registry(st.display);
	wl_registry_add_listener(st.registry, &registry_listener, &st);
	wl_display_roundtrip(st.display);   /* globals */
	wl_display_roundtrip(st.display);   /* seat caps */

	printf("PRIMARY-MANAGER=%s\n", st.manager ? "present" : "absent");

	if (st.mode == MODE_PROBE_MANAGER) {
		fflush(stdout);
		wl_display_disconnect(st.display);
		return 0;
	}
	if (!st.manager || !st.seat) {
		printf("PRIMARY-%s=NO-MANAGER\n",
		       st.mode == MODE_SET ? "SET" : "GET");
		fflush(stdout);
		wl_display_disconnect(st.display);
		return 0;
	}

	st.device = zwp_primary_selection_device_manager_v1_get_device(
		st.manager, st.seat);
	zwp_primary_selection_device_v1_add_listener(st.device,
						     &device_listener, &st);
	wl_display_roundtrip(st.display);

	if (st.mode == MODE_SET) {
		struct zwp_primary_selection_source_v1 *src =
			zwp_primary_selection_device_manager_v1_create_source(
				st.manager);
		zwp_primary_selection_source_v1_add_listener(src,
							     &src_listener, &st);
		zwp_primary_selection_source_v1_offer(src, st.mime);
		zwp_primary_selection_device_v1_set_selection(st.device, src, 0);
		wl_display_roundtrip(st.display);
		printf("PRIMARY-SET-OK\n");
		fflush(stdout);
		/* Serve send events until killed by the test harness. */
		while (wl_display_dispatch(st.display) != -1)
			;
		wl_display_disconnect(st.display);
		return 0;
	}

	/* MODE_GET: wait for a selection event, then receive. */
	for (int i = 0; i < 40 && !st.got_selection_event; i++) {
		if (wl_display_roundtrip(st.display) == -1)
			break;
		struct timespec ts = { 0, 50 * 1000 * 1000 };
		nanosleep(&ts, NULL);
	}
	if (!st.got_selection_event || !st.current_offer) {
		printf("PRIMARY-GET-NO-OFFER\n");
		fflush(stdout);
		wl_display_disconnect(st.display);
		return 0;
	}
	/* Make sure we've seen the offer's mime advertisements. */
	wl_display_roundtrip(st.display);
	if (!st.offer_has_mime) {
		printf("PRIMARY-GET-MIME-ABSENT\n");
		fflush(stdout);
		wl_display_disconnect(st.display);
		return 0;
	}

	int fds[2];
	if (pipe(fds) != 0) {
		printf("PRIMARY-GET-PIPE-FAIL\n");
		fflush(stdout);
		wl_display_disconnect(st.display);
		return 0;
	}
	zwp_primary_selection_offer_v1_receive(st.current_offer, st.mime,
					       fds[1]);
	close(fds[1]);
	wl_display_flush(st.display);
	/* Pump the display so the compositor processes the receive while we
	 * read the read-end. */
	fcntl(fds[0], F_SETFL, O_NONBLOCK);
	for (int i = 0; i < 10; i++)
		wl_display_roundtrip(st.display);

	char buf[4096];
	int n = read_all(fds[0], buf, sizeof buf);
	close(fds[0]);
	if (n > 0) {
		printf("PRIMARY-GET-DATA=%s\n", buf);
		printf("PRIMARY-GET-OK\n");
	} else {
		printf("PRIMARY-GET-NO-DATA\n");   /* cross-silo deny → EOF */
	}
	fflush(stdout);
	wl_display_disconnect(st.display);
	return 0;
}
