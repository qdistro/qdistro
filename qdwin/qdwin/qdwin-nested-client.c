/*
 * §6.8 S1 — wl_client side of qdwin_nested_v1.
 *
 * This translation unit speaks the *client* side of the qdwin_nested_v1
 * protocol (binding the manager global on the *outer* qdwin from inside
 * a *nested* qdwin-shell-as-publisher).  Kept in its own .c to avoid
 * symbol conflicts with the server-side bits in qdwin.c that include
 * `qdwin-nested-v1-server-protocol.h`.
 *
 * qdwin.c calls these functions via the `qdwin-nested-client.h` header
 * (no Wayland types leak through) and the publisher logic stays in
 * qdwin.c — this file is just a thin wrapper.
 *
 * Threading: single-threaded.  The wl_display fd is integrated into
 * the host weston event loop; dispatch happens inline with the
 * compositor.  No locking required.
 */

#define _GNU_SOURCE  /* accept4 + SOCK_NONBLOCK | SOCK_CLOEXEC flags */
#include <errno.h>
#include <glob.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <fcntl.h>

#include <wayland-client.h>

#include "qdwin-nested-v1-client-protocol.h"
#include "qdwin-nested-client.h"

struct qdwin_nested_client {
	struct wl_display *display;
	struct wl_registry *registry;
	struct qdwin_nested_manager_v1 *manager;
	uint32_t manager_name;
	uint32_t manager_version;
	bool failed;
};

struct qdwin_nested_client_pub {
	struct qdwin_nested_client *client;
	struct qdwin_nested_toplevel_v1 *proxy;
	void *userdata;
	void (*on_configured)(void *userdata, int32_t w, int32_t h);
	void (*on_close_requested)(void *userdata);
	void (*on_focus_changed)(void *userdata, uint32_t focused);
};

static void
on_configured_evt(void *data, struct qdwin_nested_toplevel_v1 *p,
		  int32_t w, int32_t h)
{
	(void)p;
	struct qdwin_nested_client_pub *pub = data;
	if (pub->on_configured)
		pub->on_configured(pub->userdata, w, h);
}

static void
on_close_requested_evt(void *data, struct qdwin_nested_toplevel_v1 *p)
{
	(void)p;
	struct qdwin_nested_client_pub *pub = data;
	if (pub->on_close_requested)
		pub->on_close_requested(pub->userdata);
}

static void
on_focus_changed_evt(void *data, struct qdwin_nested_toplevel_v1 *p,
		     uint32_t focused)
{
	(void)p;
	struct qdwin_nested_client_pub *pub = data;
	if (pub->on_focus_changed)
		pub->on_focus_changed(pub->userdata, focused);
}

static const struct qdwin_nested_toplevel_v1_listener pub_listener = {
	.configured       = on_configured_evt,
	.close_requested  = on_close_requested_evt,
	.focus_changed    = on_focus_changed_evt,
};

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct qdwin_nested_client *c = data;
	if (strcmp(interface, qdwin_nested_manager_v1_interface.name) != 0)
		return;
	uint32_t use_ver = version > 2 ? 2 : version;
	c->manager = wl_registry_bind(reg, name,
				      &qdwin_nested_manager_v1_interface,
				      use_ver);
	c->manager_name = name;
	c->manager_version = use_ver;
}

static void
on_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{
	struct qdwin_nested_client *c = data;
	(void)reg;
	if (c->manager_name == name && c->manager) {
		qdwin_nested_manager_v1_destroy(c->manager);
		c->manager = NULL;
		c->manager_name = 0;
	}
}

static const struct wl_registry_listener registry_listener = {
	.global         = on_global,
	.global_remove  = on_global_remove,
};

struct qdwin_nested_client *
qdwin_nested_client_new(const char *display_name)
{
	struct qdwin_nested_client *c = calloc(1, sizeof *c);
	if (!c)
		return NULL;
	c->display = wl_display_connect(display_name);
	if (!c->display) {
		fprintf(stderr,
			"[qdwin-nested-client] wl_display_connect(%s) "
			"FAILED: %s\n",
			display_name ? display_name : "(NULL)",
			strerror(errno));
		free(c);
		return NULL;
	}
	c->registry = wl_display_get_registry(c->display);
	wl_registry_add_listener(c->registry, &registry_listener, c);
	wl_display_roundtrip(c->display);
	if (!c->manager) {
		/* Outer compositor doesn't expose the manager global —
		 * either it's not qdwin or the peer-uid filter rejected us. */
		c->failed = true;
		return c;
	}
	/* Force a second roundtrip so any error/event from the bind
	 * (e.g. peer-uid rejection from the outer side) lands here
	 * before we declare success. Without an explicit sync after the
	 * bind, the rejection event arrives later and we'd think the
	 * bind succeeded. */
	wl_display_roundtrip(c->display);
	return c;
}

int
qdwin_nested_client_get_fd(struct qdwin_nested_client *c)
{
	return c ? wl_display_get_fd(c->display) : -1;
}

int
qdwin_nested_client_dispatch_pending(struct qdwin_nested_client *c)
{
	if (!c)
		return -1;
	if (wl_display_prepare_read(c->display) == 0) {
		if (wl_display_read_events(c->display) < 0) {
			c->failed = true;
			return -1;
		}
	}
	return wl_display_dispatch_pending(c->display);
}

int
qdwin_nested_client_flush(struct qdwin_nested_client *c)
{
	return c ? wl_display_flush(c->display) : -1;
}

bool
qdwin_nested_client_manager_ready(struct qdwin_nested_client *c)
{
	return c && c->manager && !c->failed;
}

uint32_t
qdwin_nested_client_manager_version(struct qdwin_nested_client *c)
{
	return c ? c->manager_version : 0;
}

bool
qdwin_nested_client_failed(struct qdwin_nested_client *c)
{
	return !c || c->failed;
}

struct qdwin_nested_client_pub *
qdwin_nested_client_advertise(struct qdwin_nested_client *c,
			      const char *pw_node,
			      const char *input_sink,
			      const char *app_id,
			      const char *title,
			      uint32_t origin_uid,
			      void *userdata,
			      void (*on_configured)(void *, int32_t, int32_t),
			      void (*on_close_requested)(void *),
			      void (*on_focus_changed)(void *, uint32_t))
{
	if (!c || !c->manager)
		return NULL;
	struct qdwin_nested_client_pub *pub = calloc(1, sizeof *pub);
	if (!pub)
		return NULL;
	pub->client            = c;
	pub->userdata          = userdata;
	pub->on_configured     = on_configured;
	pub->on_close_requested = on_close_requested;
	pub->on_focus_changed  = on_focus_changed;
	pub->proxy = qdwin_nested_manager_v1_advertise_toplevel(
		c->manager,
		pw_node    ? pw_node    : "",
		input_sink ? input_sink : "",
		app_id     ? app_id     : "",
		title      ? title      : "",
		origin_uid);
	if (!pub->proxy) {
		free(pub);
		return NULL;
	}
	qdwin_nested_toplevel_v1_add_listener(pub->proxy, &pub_listener, pub);
	wl_display_flush(c->display);
	return pub;
}

void
qdwin_nested_client_pub_set_title(struct qdwin_nested_client_pub *pub,
				  const char *title)
{
	if (!pub || !pub->proxy)
		return;
	qdwin_nested_toplevel_v1_set_title(pub->proxy, title ? title : "");
	wl_display_flush(pub->client->display);
}

void
qdwin_nested_client_pub_set_app_id(struct qdwin_nested_client_pub *pub,
				   const char *app_id)
{
	if (!pub || !pub->proxy)
		return;
	qdwin_nested_toplevel_v1_set_app_id(pub->proxy, app_id ? app_id : "");
	wl_display_flush(pub->client->display);
}

void
qdwin_nested_client_pub_set_geometry(struct qdwin_nested_client_pub *pub,
				     int32_t w, int32_t h)
{
	if (!pub || !pub->proxy)
		return;
	qdwin_nested_toplevel_v1_set_geometry(pub->proxy, w, h);
	wl_display_flush(pub->client->display);
}

void
qdwin_nested_client_pub_destroy(struct qdwin_nested_client_pub *pub)
{
	if (!pub)
		return;
	if (pub->proxy)
		qdwin_nested_toplevel_v1_destroy(pub->proxy);
	if (pub->client)
		wl_display_flush(pub->client->display);
	free(pub);
}

/* ------------------------------------------------------------------
 * §6.8 S3 — input-sink socket helpers.
 *
 * The nested compositor opens one AF_UNIX socket per advertised
 * inner toplevel and listens for the outer to connect.  Bytes flow
 * outer → nested as a stream of small fixed-prefix packets:
 *
 *   uint32_t magic = 0x49444E51 ('QDNI' little-endian)
 *   uint8_t  version = 1
 *   uint8_t  event_type
 *   uint16_t payload_len
 *   uint8_t  payload[payload_len]
 *
 * S3 ships only event_type=1 PING (zero payload) which the nested
 * side logs.  S3b adds motion/button/key encoders + decoders.
 * ------------------------------------------------------------------ */

#define QDNI_MAGIC      0x49444E51u
#define QDNI_VERSION    1
#define QDNI_PING       1

struct qdwin_nested_input_sink *
qdwin_nested_input_sink_open(uint32_t handle)
{
	const char *xdg = getenv("XDG_RUNTIME_DIR");
	if (!xdg || !*xdg)
		xdg = "/tmp";
	char path[256];
	int n = snprintf(path, sizeof path,
			 "%s/qdwin-nested-input-%d-%u.sock",
			 xdg, (int)getpid(), handle);
	if (n < 0 || n >= (int)sizeof path)
		return NULL;

	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return NULL;
	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	/* findings F3: reject an over-long path rather than silently truncating
	 * (a truncated sun_path could resolve to a different, attacker-influenced
	 * socket), and NUL-terminate explicitly rather than relying on the
	 * designated initializer having zeroed the reserved last byte. */
	if (strlen(path) >= sizeof addr.sun_path) {
		close(fd);
		return NULL;
	}
	strncpy(addr.sun_path, path, sizeof addr.sun_path - 1);
	addr.sun_path[sizeof addr.sun_path - 1] = '\0';
	unlink(path);  /* in case of stale socket from a previous run */
	if (bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
		close(fd);
		return NULL;
	}
	if (listen(fd, 1) < 0) {
		close(fd);
		unlink(path);
		return NULL;
	}
	struct qdwin_nested_input_sink *s = calloc(1, sizeof *s);
	if (!s) {
		close(fd);
		unlink(path);
		return NULL;
	}
	s->listen_fd = fd;
	s->peer_fd = -1;
	/* F7: on OOM, fail the whole setup rather than return a sink whose
	 * socket_path is NULL — close() skips the unlink() of the bound socket
	 * pathname, leaking a stale socket file on disk. */
	s->socket_path = strdup(path);
	if (!s->socket_path) {
		close(fd);
		unlink(path);
		free(s);
		return NULL;
	}
	return s;
}

void
qdwin_nested_input_sink_close(struct qdwin_nested_input_sink *sink)
{
	if (!sink)
		return;
	if (sink->peer_fd >= 0)
		close(sink->peer_fd);
	if (sink->listen_fd >= 0)
		close(sink->listen_fd);
	if (sink->socket_path) {
		unlink(sink->socket_path);
		free(sink->socket_path);
	}
	free(sink);
}

int
qdwin_nested_input_sink_accept(struct qdwin_nested_input_sink *sink)
{
	if (!sink || sink->listen_fd < 0)
		return -1;
	int fd = accept4(sink->listen_fd, NULL, NULL,
			 SOCK_NONBLOCK | SOCK_CLOEXEC);
	if (fd < 0)
		return -1;
	if (sink->peer_fd >= 0)
		close(sink->peer_fd);
	sink->peer_fd = fd;
	sink->rbuf_used = 0;
	return 0;
}

int
qdwin_nested_input_sink_read_one(struct qdwin_nested_input_sink *sink,
				 uint8_t *out_version,
				 uint16_t *out_payload_len,
				 uint8_t *payload_buf,
				 size_t payload_buf_size)
{
	if (!sink || sink->peer_fd < 0)
		return -1;
	/* §6.8 Round-8 fix: pull whatever the kernel has buffered into
	 * our accumulation buffer first. A single recv() may return any
	 * mixture of partial-header / partial-payload / multiple-packets
	 * on a SOCK_STREAM; we then parse exactly one full packet out
	 * and shift the remainder. Returns -2 if we need more bytes. */
	if (sink->rbuf_used < sizeof sink->rbuf) {
		ssize_t got = recv(sink->peer_fd,
				   sink->rbuf + sink->rbuf_used,
				   sizeof sink->rbuf - sink->rbuf_used,
				   MSG_DONTWAIT);
		if (got > 0) {
			sink->rbuf_used += (size_t)got;
		} else if (got == 0) {
			/* Peer closed and no buffered bytes left. */
			if (sink->rbuf_used == 0)
				return -1;
		} else if (errno != EAGAIN && errno != EWOULDBLOCK &&
			   errno != EINTR) {
			return -1;
		}
	}
	if (sink->rbuf_used < 8)
		return QDWIN_NESTED_INPUT_SINK_AGAIN;
	uint32_t magic;
	memcpy(&magic, sink->rbuf, 4);
	if (magic != QDNI_MAGIC)
		return -1;
	uint8_t  version = sink->rbuf[4];
	uint8_t  event_type = sink->rbuf[5];
	uint16_t plen;
	memcpy(&plen, sink->rbuf + 6, 2);
	if (plen > payload_buf_size || (size_t)plen > sizeof sink->rbuf - 8)
		return -1;
	if (sink->rbuf_used < (size_t)8 + plen)
		return QDWIN_NESTED_INPUT_SINK_AGAIN;
	if (out_version)
		*out_version = version;
	if (out_payload_len)
		*out_payload_len = plen;
	if (plen > 0)
		memcpy(payload_buf, sink->rbuf + 8, plen);
	size_t consumed = 8 + plen;
	if (sink->rbuf_used > consumed)
		memmove(sink->rbuf, sink->rbuf + consumed,
			sink->rbuf_used - consumed);
	sink->rbuf_used -= consumed;
	return event_type;
}

/* Try to connect to a unix socket at `path`. Returns fd >= 0 on success,
 * -1 on failure. Caller-owned errno is left untouched on success. */
static int
qdwin_nested_input_sink_connect_one(const char *path)
{
	if (!path || !*path)
		return -1;
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return -1;
	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	/* findings F3: reject an over-long path rather than silently truncating,
	 * and NUL-terminate explicitly (see the bind site for rationale). */
	if (strlen(path) >= sizeof addr.sun_path) {
		close(fd);
		return -1;
	}
	strncpy(addr.sun_path, path, sizeof addr.sun_path - 1);
	addr.sun_path[sizeof addr.sun_path - 1] = '\0';
	if (connect(fd, (struct sockaddr *)&addr, sizeof addr) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

int
qdwin_nested_input_sink_connect(const char *socket_path)
{
	if (!socket_path || !*socket_path)
		return -1;

	/* First try the path as advertised. This works for non-isolated
	 * nested publishers (same XDG_RUNTIME_DIR view as the outer). */
	int fd = qdwin_nested_input_sink_connect_one(socket_path);
	if (fd >= 0)
		return fd;

	/* tier-2 fallback: the inner publisher lives in a podman container
	 * whose /run/user/<uid> is a bind-mount of
	 * /run/user/<uid>/qdistro-tier2/<token>/ on the host. The inner
	 * advertises an XDG_RUNTIME_DIR-relative path (e.g. "/run/user/1000/
	 * qdwin-nested-input-7-1.sock") which on the host actually resolves
	 * under the per-container dir. Glob `qdistro-tier2/<*>/<basename>`
	 * under /run/user/<uid>/ and try each match. The publisher's
	 * uniqueness pattern (pid+handle, pid is container PID 7..) makes
	 * collisions across containers essentially impossible. */
	const char *slash = strrchr(socket_path, '/');
	if (!slash)
		return -1;
	const char *basename = slash + 1;
	/* findings F2: the basename comes from an advertised path controlled by
	 * the (sandboxed) inner publisher. It is spliced UNESCAPED into the glob
	 * pattern below, so glob metacharacters (* ? [ ] \) in it would broaden
	 * matching and let a malicious inner connect to a *different* same-uid
	 * container's input-sink than the one it named, defeating the uniqueness
	 * assumption. Legitimate names are qdwin-nested-input-<pid>-<n>.sock with
	 * no metacharacters, so reject any basename that carries one (and an
	 * empty basename). Only `basename` is sanitized; the `*` container-dir
	 * wildcard in the pattern is ours and intended. */
	if (!*basename || basename[strcspn(basename, "*?[]\\")] != '\0')
		return -1;
	if (strncmp(socket_path, "/run/user/", 10) != 0)
		return -1;
	const char *uid_start = socket_path + 10;
	const char *uid_end = strchr(uid_start, '/');
	if (!uid_end || uid_end == uid_start)
		return -1;
	char pattern[256];
	int n = snprintf(pattern, sizeof pattern,
			 "/run/user/%.*s/qdistro-tier2/*/%s",
			 (int)(uid_end - uid_start), uid_start, basename);
	if (n < 0 || n >= (int)sizeof pattern)
		return -1;
	glob_t gl;
	if (glob(pattern, 0, NULL, &gl) != 0)
		return -1;
	for (size_t i = 0; i < gl.gl_pathc; ++i) {
		fd = qdwin_nested_input_sink_connect_one(gl.gl_pathv[i]);
		if (fd >= 0) {
			globfree(&gl);
			return fd;
		}
	}
	globfree(&gl);
	return -1;
}

int
qdwin_nested_input_sink_send(int peer_fd, uint8_t event_type,
			     const void *payload, uint16_t payload_len)
{
	if (peer_fd < 0)
		return -1;
	uint8_t hdr[8];
	uint32_t magic = QDNI_MAGIC;
	memcpy(hdr, &magic, 4);
	hdr[4] = QDNI_VERSION;
	hdr[5] = event_type;
	memcpy(hdr + 6, &payload_len, 2);
	ssize_t n = send(peer_fd, hdr, sizeof hdr, MSG_NOSIGNAL);
	if (n != (ssize_t)sizeof hdr)
		return -1;
	if (payload_len > 0 && payload) {
		n = send(peer_fd, payload, payload_len, MSG_NOSIGNAL);
		if (n != (ssize_t)payload_len)
			return -1;
	}
	return 0;
}

int
qdwin_nested_input_sink_send_motion(int peer_fd, uint32_t time_msec,
				    int32_t x_fixed, int32_t y_fixed)
{
	struct qdni_motion_payload p = {
		.time_msec = time_msec,
		.x_fixed   = x_fixed,
		.y_fixed   = y_fixed,
	};
	return qdwin_nested_input_sink_send(peer_fd, QDNI_EVENT_MOTION,
					    &p, sizeof p);
}

int
qdwin_nested_input_sink_send_button(int peer_fd, uint32_t time_msec,
				    uint32_t button, uint32_t state)
{
	struct qdni_button_payload p = {
		.time_msec = time_msec,
		.button    = button,
		.state     = state,
	};
	return qdwin_nested_input_sink_send(peer_fd, QDNI_EVENT_BUTTON,
					    &p, sizeof p);
}

int
qdwin_nested_input_sink_send_key(int peer_fd, uint32_t time_msec,
				 uint32_t key, uint32_t state)
{
	struct qdni_key_payload p = {
		.time_msec = time_msec,
		.key       = key,
		.state     = state,
	};
	return qdwin_nested_input_sink_send(peer_fd, QDNI_EVENT_KEY,
					    &p, sizeof p);
}

int
qdwin_nested_input_sink_send_axis(int peer_fd, uint32_t time_msec,
				  uint32_t axis, int32_t value_fixed)
{
	struct qdni_axis_payload p = {
		.time_msec   = time_msec,
		.axis        = axis,
		.value_fixed = value_fixed,
	};
	return qdwin_nested_input_sink_send(peer_fd, QDNI_EVENT_AXIS,
					    &p, sizeof p);
}

int
qdwin_nested_input_sink_send_focus(int peer_fd, uint32_t focused)
{
	struct qdni_focus_payload p = { .focused = focused };
	return qdwin_nested_input_sink_send(peer_fd, QDNI_EVENT_FOCUS,
					    &p, sizeof p);
}

void
qdwin_nested_client_destroy(struct qdwin_nested_client *c)
{
	if (!c)
		return;
	if (c->manager)
		qdwin_nested_manager_v1_destroy(c->manager);
	if (c->registry)
		wl_registry_destroy(c->registry);
	if (c->display)
		wl_display_disconnect(c->display);
	free(c);
}
