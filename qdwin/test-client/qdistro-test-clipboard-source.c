/*
 * qdistro-test-clipboard-source — set the wayland clipboard from the
 * command line, without waiting for keyboard focus.
 *
 * wl-clipboard / wl-copy waits for wl_keyboard.enter on its hidden
 * surface before calling wl_data_device.set_selection (a defensive
 * focus check). Under weston-rdp with sdl-freerdp dummy, no real
 * keyboard input arrives, so wl-copy hangs forever — which makes it
 * unusable for headless bats coverage of the spec/10 gate.
 *
 * This helper skips the focus wait and calls set_selection
 * immediately on the first wl_seat. wl_data_device.set_selection's
 * `serial` field is supposed to be a recent input serial; the spec
 * says the compositor MAY validate, but stock weston doesn't (`FIXME:
 * Store serial and check against incoming serial here`). The helper
 * uses serial 0; the broker / qdshell selection_set listener fires
 * regardless.
 *
 * Usage:
 *   qdistro-test-clipboard-source [--mime text/plain] [--text "payload"]
 *   ... | qdistro-test-clipboard-source --mime text/plain
 *
 * On a `send` event from another client (paste), writes the payload
 * to the offered fd. Loops until SIGTERM.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <wayland-client.h>

static int running = 1;
static const char *payload = "qdistro-test-clipboard";
static size_t payload_len = 0;

static void on_sig(int s) { (void)s; running = 0; }

struct ctx {
	struct wl_display *display;
	struct wl_data_device_manager *ddm;
	struct wl_seat *seat;
	struct wl_data_device *device;
	struct wl_data_source *source;
	const char *mime;
};

static void
data_source_target(void *d, struct wl_data_source *s, const char *mime)
{ (void)d; (void)s; (void)mime; }

static void
data_source_send(void *d, struct wl_data_source *s,
		 const char *mime, int32_t fd)
{
	(void)d; (void)s; (void)mime;
	ssize_t total = 0;
	while ((size_t)total < payload_len) {
		ssize_t n = write(fd, payload + total, payload_len - total);
		if (n <= 0) {
			if (errno == EINTR) continue;
			break;
		}
		total += n;
	}
	close(fd);
	fprintf(stderr, "[qdistro-test-clipboard-source] sent %zd bytes "
			 "for mime=%s\n", total, mime);
}

static void
data_source_cancelled(void *d, struct wl_data_source *s)
{
	(void)d; (void)s;
	fprintf(stderr, "[qdistro-test-clipboard-source] selection cancelled\n");
	running = 0;
}

static void
data_source_dnd_drop_performed(void *d, struct wl_data_source *s)
{ (void)d; (void)s; }

static void
data_source_dnd_finished(void *d, struct wl_data_source *s)
{ (void)d; (void)s; }

static void
data_source_action(void *d, struct wl_data_source *s, uint32_t action)
{ (void)d; (void)s; (void)action; }

static const struct wl_data_source_listener data_source_listener = {
	.target = data_source_target,
	.send = data_source_send,
	.cancelled = data_source_cancelled,
	.dnd_drop_performed = data_source_dnd_drop_performed,
	.dnd_finished = data_source_dnd_finished,
	.action = data_source_action,
};

static void
seat_capabilities(void *d, struct wl_seat *s, uint32_t caps)
{ (void)d; (void)s; (void)caps; }
static void
seat_name(void *d, struct wl_seat *s, const char *name)
{ (void)d; (void)s; (void)name; }
static const struct wl_seat_listener seat_listener = {
	.capabilities = seat_capabilities,
	.name = seat_name,
};

static void
registry_global(void *data, struct wl_registry *reg, uint32_t name,
		const char *interface, uint32_t version)
{
	struct ctx *c = data;
	if (!strcmp(interface, wl_data_device_manager_interface.name)) {
		c->ddm = wl_registry_bind(reg, name,
					  &wl_data_device_manager_interface,
					  version > 3 ? 3 : version);
	} else if (!strcmp(interface, wl_seat_interface.name) && !c->seat) {
		c->seat = wl_registry_bind(reg, name, &wl_seat_interface,
					   version > 5 ? 5 : version);
		wl_seat_add_listener(c->seat, &seat_listener, c);
	}
}
static void
registry_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_global_remove,
};

int main(int argc, char **argv)
{
	const char *mime = "text/plain";
	const char *text_arg = NULL;
	struct option opts[] = {
		{"mime", required_argument, 0, 'm'},
		{"text", required_argument, 0, 't'},
		{0, 0, 0, 0},
	};
	int o;
	while ((o = getopt_long(argc, argv, "m:t:", opts, NULL)) != -1) {
		switch (o) {
		case 'm': mime = optarg; break;
		case 't': text_arg = optarg; break;
		default:
			fprintf(stderr, "usage: %s [--mime M] [--text TEXT]\n",
				argv[0]);
			return 2;
		}
	}
	if (text_arg) {
		payload = text_arg;
	} else {
		/* Slurp stdin into a heap buffer. */
		char *buf = NULL;
		size_t cap = 0, n = 0;
		for (;;) {
			if (cap - n < 4096) {
				cap = cap ? cap * 2 : 4096;
				buf = realloc(buf, cap);
				if (!buf) return 1;
			}
			ssize_t r = read(0, buf + n, cap - n);
			if (r < 0) { if (errno == EINTR) continue; break; }
			if (r == 0) break;
			n += r;
		}
		payload = buf;
		payload_len = n;
	}
	if (text_arg) payload_len = strlen(text_arg);

	signal(SIGTERM, on_sig);
	signal(SIGINT, on_sig);

	struct ctx c = {0};
	c.mime = mime;
	c.display = wl_display_connect(NULL);
	if (!c.display) {
		fprintf(stderr, "wl_display_connect failed\n");
		return 1;
	}
	struct wl_registry *reg = wl_display_get_registry(c.display);
	wl_registry_add_listener(reg, &registry_listener, &c);
	wl_display_roundtrip(c.display);
	if (!c.ddm || !c.seat) {
		fprintf(stderr, "missing globals — ddm=%p seat=%p\n",
			(void*)c.ddm, (void*)c.seat);
		return 1;
	}

	c.device = wl_data_device_manager_get_data_device(c.ddm, c.seat);
	c.source = wl_data_device_manager_create_data_source(c.ddm);
	wl_data_source_add_listener(c.source, &data_source_listener, &c);
	wl_data_source_offer(c.source, mime);
	wl_data_device_set_selection(c.device, c.source, 0);
	wl_display_flush(c.display);
	fprintf(stderr, "[qdistro-test-clipboard-source] set_selection mime=%s "
			 "payload_len=%zu\n", mime, payload_len);

	while (running && wl_display_dispatch(c.display) != -1)
		;

	if (c.source) wl_data_source_destroy(c.source);
	if (c.device) wl_data_device_release(c.device);
	if (c.seat) wl_seat_release(c.seat);
	if (c.ddm) wl_data_device_manager_destroy(c.ddm);
	wl_display_disconnect(c.display);
	return 0;
}
