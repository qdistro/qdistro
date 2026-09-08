/*
 * R6 viewer-local nested publisher.
 *
 * The inherited controller socket carries only bounded QDML records. This
 * process mints a local random token, advertises one qdwin_nested_v1 proxy,
 * and owns mode-0600 frame and QDNI sockets beneath XDG_RUNTIME_DIR. Kernel
 * peer credentials bind both local consumers to this uid. No path, fd,
 * Wayland object id, or PipeWire name received from the network is used.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#include <wayland-client.h>

#include "mm-remote-frame-protocol.h"
#include "qdwin-nested-v1-client-protocol.h"

#define LOGI(fmt, ...) fprintf(stderr, "[mm-viewer-helper %d] " fmt "\n", \
	(int)getpid(), ##__VA_ARGS__)
#define LOGE(fmt, ...) fprintf(stderr, "[mm-viewer-helper %d ERR] " fmt "\n", \
	(int)getpid(), ##__VA_ARGS__)

struct state {
	int controller_fd;
	int frame_listen_fd;
	int frame_peer_fd;
	int input_listen_fd;
	int input_peer_fd;
	char frame_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
	char input_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
	char source_name[sizeof("qdistro.remote:") + 32];
	struct wl_display *display;
	struct wl_registry *registry;
	struct qdwin_nested_manager_v1 *manager;
	struct qdwin_nested_toplevel_v1 *proxy;
	uint32_t manager_name;
	uint32_t manager_version;
	uint64_t source_revision;
	uint32_t width;
	uint32_t height;
	uint32_t stride;
	char *app_id;
	char *title;
	char *source_machine;
	char *trust_domain_id;
	char *stream_id;
	uint64_t generation;
	uint8_t *pending_frame;
	size_t pending_frame_len;
	uint64_t pending_frame_seq;
	int pending_frame_sent;
	uint8_t input_buffer[1024];
	size_t input_used;
	uint8_t ack_buffer[QDMM_DECODER_ACK_WIRE_SIZE * 4];
	size_t ack_used;
	int attached;
	volatile sig_atomic_t stop;
};

static struct state g_state = {
	.controller_fd = -1,
	.frame_listen_fd = -1,
	.frame_peer_fd = -1,
	.input_listen_fd = -1,
	.input_peer_fd = -1,
};

static uint16_t
read_le16(const uint8_t *p)
{
	return (uint16_t)p[0] | (uint16_t)p[1] << 8;
}

static int
send_exact(int fd, const void *buffer, size_t length)
{
	const uint8_t *in = buffer;
	size_t used = 0;
	while (used < length) {
		ssize_t sent = send(fd, in + used, length - used, MSG_NOSIGNAL);
		if (sent < 0) {
			if (errno == EINTR && !g_state.stop)
				continue;
			return -1;
		}
		used += (size_t)sent;
	}
	return 0;
}

static int
recv_exact(int fd, void *buffer, size_t length)
{
	uint8_t *out = buffer;
	size_t used = 0;
	while (used < length) {
		ssize_t got = recv(fd, out + used, length - used, 0);
		if (got == 0)
			return 0;
		if (got < 0) {
			if (errno == EINTR && !g_state.stop)
				continue;
			return -1;
		}
		used += (size_t)got;
	}
	return 1;
}

static int
recv_controller_message(uint8_t **out, size_t *out_len)
{
	uint32_t wire_len;
	int rc = recv_exact(g_state.controller_fd, &wire_len, sizeof wire_len);
	if (rc <= 0)
		return rc;
	size_t length = ntohl(wire_len);
	if (length < QDMM_LOCAL_HEADER_SIZE || length > QDMM_MAX_LOCAL_BYTES) {
		errno = EMSGSIZE;
		return -1;
	}
	uint8_t *message = malloc(length);
	if (!message)
		return -1;
	rc = recv_exact(g_state.controller_fd, message, length);
	if (rc != 1) {
		free(message);
		return -1;
	}
	*out = message;
	*out_len = length;
	return 1;
}

static int
send_controller_wire(const uint8_t *wire, size_t length)
{
	return send_exact(g_state.controller_fd, wire, length);
}

static int
send_controller_message(uint8_t kind, uint64_t seq)
{
	uint8_t wire[4 + QDMM_LOCAL_HEADER_SIZE] = {0};
	uint32_t length = htonl(QDMM_LOCAL_HEADER_SIZE);
	memcpy(wire, &length, sizeof length);
	memcpy(wire + 4, "QDML", 4);
	wire[8] = 1;
	wire[9] = kind;
	uint64_t be_seq = htobe64(seq);
	memcpy(wire + 12, &be_seq, sizeof be_seq);
	return send_controller_wire(wire, sizeof wire);
}

static int
same_uid_peer(int fd)
{
	struct ucred peer = {0};
	socklen_t length = sizeof peer;
	return getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &length) == 0 &&
	       length == sizeof peer && peer.uid == getuid();
}

static int
make_listener(const char *path)
{
	if (!path || strlen(path) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
		errno = ENAMETOOLONG;
		return -1;
	}
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return -1;
	struct sockaddr_un address = { .sun_family = AF_UNIX };
	strcpy(address.sun_path, path);
	unlink(path);
	if (bind(fd, (struct sockaddr *)&address, sizeof address) < 0 ||
	    chmod(path, 0600) < 0 || listen(fd, 2) < 0) {
		int saved = errno;
		close(fd);
		unlink(path);
		errno = saved;
		return -1;
	}
	return fd;
}

static int
accept_peer(int listen_fd)
{
	int fd = accept4(listen_fd, NULL, NULL, SOCK_CLOEXEC);
	if (fd < 0)
		return -1;
	if (!same_uid_peer(fd)) {
		close(fd);
		errno = EPERM;
		return -1;
	}
	return fd;
}

static int
mint_local_endpoints(struct state *state)
{
	const char *runtime = getenv("XDG_RUNTIME_DIR");
	if (!runtime || runtime[0] != '/') {
		errno = EINVAL;
		return -1;
	}
	uint8_t random[16];
	ssize_t got;
	do {
		got = getrandom(random, sizeof random, 0);
	} while (got < 0 && errno == EINTR);
	if (got != (ssize_t)sizeof random)
		return -1;
	char token[33];
	for (size_t i = 0; i < sizeof random; i++)
		snprintf(token + i * 2, 3, "%02x", random[i]);
	if (snprintf(state->source_name, sizeof state->source_name,
	    "qdistro.remote:%s", token) >= (int)sizeof state->source_name ||
	    snprintf(state->frame_path, sizeof state->frame_path,
	    "%s/qdistro-mm-frame-%s.sock", runtime, token) >=
	    (int)sizeof state->frame_path ||
	    snprintf(state->input_path, sizeof state->input_path,
	    "%s/qdistro-mm-input-%s.sock", runtime, token) >=
	    (int)sizeof state->input_path) {
		errno = ENAMETOOLONG;
		return -1;
	}
	state->frame_listen_fd = make_listener(state->frame_path);
	if (state->frame_listen_fd < 0)
		return -1;
	state->input_listen_fd = make_listener(state->input_path);
	if (state->input_listen_fd < 0)
		return -1;
	return 0;
}

static void
registry_global(void *data, struct wl_registry *registry, uint32_t name,
		const char *interface, uint32_t version)
{
	struct state *state = data;
	if (strcmp(interface, qdwin_nested_manager_v1_interface.name) != 0)
		return;
	uint32_t use_version = version > 3 ? 3 : version;
	state->manager = wl_registry_bind(registry, name,
		&qdwin_nested_manager_v1_interface, use_version);
	state->manager_name = name;
	state->manager_version = use_version;
}

static void
registry_remove(void *data, struct wl_registry *registry, uint32_t name)
{
	struct state *state = data;
	(void)registry;
	if (state->manager_name == name) {
		state->manager = NULL;
		state->stop = 1;
	}
}

static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_remove,
};

static void
proxy_configured(void *data, struct qdwin_nested_toplevel_v1 *proxy,
		 int32_t width, int32_t height)
{
	(void)data;
	(void)proxy;
	LOGI("outer configured proxy %dx%d", width, height);
}

static void
proxy_close_requested(void *data, struct qdwin_nested_toplevel_v1 *proxy)
{
	struct state *state = data;
	(void)proxy;
	if (!state->attached || !state->source_revision)
		return;
	if (send_controller_message(
	    QDMM_LOCAL_CLOSE_REQUEST, state->source_revision) < 0) {
		LOGE("failed to forward source-mediated close request");
		state->stop = 1;
	}
}

static void
proxy_focus_changed(void *data, struct qdwin_nested_toplevel_v1 *proxy,
		    uint32_t focused)
{
	(void)data;
	(void)proxy;
	(void)focused;
	/* QDNI carries the authoritative per-proxy focus transition. */
}

static const struct qdwin_nested_toplevel_v1_listener proxy_listener = {
	.configured = proxy_configured,
	.close_requested = proxy_close_requested,
	.focus_changed = proxy_focus_changed,
};

static int
start_wayland(struct state *state)
{
	unsetenv("WAYLAND_SOCKET");
	state->display = wl_display_connect(NULL);
	if (!state->display)
		return -1;
	state->registry = wl_display_get_registry(state->display);
	wl_registry_add_listener(state->registry, &registry_listener, state);
	if (wl_display_roundtrip(state->display) < 0 || !state->manager ||
	    state->manager_version < 3) {
		errno = EPROTO;
		return -1;
	}
	return 0;
}

static char *
copy_text(const uint8_t *text, size_t length)
{
	char *copy = malloc(length + 1);
	if (!copy)
		return NULL;
	memcpy(copy, text, length);
	copy[length] = '\0';
	return copy;
}

static int
announcement_equal(struct state *state,
		   const struct qdmm_announcement_view *announcement)
{
	return state->source_revision == announcement->source_revision &&
	       state->width == announcement->width &&
	       state->height == announcement->height &&
	       state->stride == announcement->stride && state->app_id &&
	       state->title && strlen(state->app_id) == announcement->app_id_len &&
	       strlen(state->title) == announcement->title_len &&
	       memcmp(state->app_id, announcement->app_id,
		      announcement->app_id_len) == 0 &&
	       memcmp(state->title, announcement->title,
		      announcement->title_len) == 0;
}

static int
advertise_proxy(struct state *state,
		const struct qdmm_announcement_view *announcement)
{
	if (!state->source_machine || !state->trust_domain_id ||
	    !state->stream_id || !state->generation)
		return -1;
	if (state->proxy)
		return announcement_equal(state, announcement) ? 0 : -1;
	state->app_id = copy_text(announcement->app_id, announcement->app_id_len);
	state->title = copy_text(announcement->title, announcement->title_len);
	if (!state->app_id || !state->title)
		return -1;
	state->source_revision = announcement->source_revision;
	state->width = announcement->width;
	state->height = announcement->height;
	state->stride = announcement->stride;
	state->proxy = qdwin_nested_manager_v1_advertise_toplevel(
		state->manager, state->source_name, state->input_path,
		state->app_id, state->title, (uint32_t)getuid());
	if (!state->proxy)
		return -1;
	qdwin_nested_toplevel_v1_add_listener(
		state->proxy, &proxy_listener, state);
	qdwin_nested_toplevel_v1_set_geometry(
		state->proxy, (int32_t)state->width, (int32_t)state->height);
	qdwin_nested_toplevel_v1_set_remote_identity(
		state->proxy, state->source_machine, state->trust_domain_id,
		state->stream_id, (uint32_t)(state->generation >> 32),
		(uint32_t)state->generation);
	if (wl_display_roundtrip(state->display) < 0)
		return -1;
	LOGI("advertised source=%s app_id=%s geometry=%ux%u",
	     state->source_name, state->app_id, state->width, state->height);
	return 0;
}

static void
close_frame_peer(struct state *state)
{
	if (state->frame_peer_fd >= 0)
		close(state->frame_peer_fd);
	state->frame_peer_fd = -1;
	state->ack_used = 0;
	state->pending_frame_sent = 0;
}

static void
close_input_peer(struct state *state)
{
	if (state->input_peer_fd >= 0)
		close(state->input_peer_fd);
	state->input_peer_fd = -1;
	state->input_used = 0;
}

static int
send_pending_frame(struct state *state)
{
	if (state->frame_peer_fd < 0 || !state->pending_frame ||
	    state->pending_frame_sent)
		return 0;
	uint32_t length = htonl((uint32_t)state->pending_frame_len);
	if (send_exact(state->frame_peer_fd, &length, sizeof length) < 0 ||
	    send_exact(state->frame_peer_fd, state->pending_frame,
		       state->pending_frame_len) < 0) {
		close_frame_peer(state);
		return -1;
	}
	state->pending_frame_sent = 1;
	return 0;
}

static int
remember_frame(struct state *state, const uint8_t *message, size_t length,
	       const struct qdmm_frame_view *frame)
{
	if (!state->attached || frame->width != state->width ||
	    frame->height != state->height || frame->stride != state->stride)
		return -1;
	uint8_t *copy = malloc(length);
	if (!copy)
		return -1;
	memcpy(copy, message, length);
	free(state->pending_frame);
	state->pending_frame = copy;
	state->pending_frame_len = length;
	state->pending_frame_seq = frame->seq;
	state->pending_frame_sent = 0;
	send_pending_frame(state);
	return 0;
}

static int
forward_detached(struct state *state, const uint8_t *message, size_t length)
{
	free(state->pending_frame);
	state->pending_frame = NULL;
	state->pending_frame_len = 0;
	state->pending_frame_seq = 0;
	state->pending_frame_sent = 0;
	if (state->frame_peer_fd < 0)
		return 0;
	uint32_t wire_len = htonl((uint32_t)length);
	if (send_exact(state->frame_peer_fd, &wire_len, sizeof wire_len) < 0 ||
	    send_exact(state->frame_peer_fd, message, length) < 0) {
		close_frame_peer(state);
		return -1;
	}
	return 0;
}

static int
handle_controller(struct state *state)
{
	uint8_t *message = NULL;
	size_t length = 0;
	int rc = recv_controller_message(&message, &length);
	if (rc <= 0)
		return rc;
	struct qdmm_local_message_view local;
	if (qdmm_parse_local_message(message, length, &local) < 0) {
		free(message);
		return -1;
	}
	switch (local.kind) {
	case QDMM_LOCAL_CONNECTED:
		if (!local.seq || local.payload_len) {
			rc = -1;
		} else {
			LOGI("controller connected epoch=%llu",
			     (unsigned long long)local.seq);
		}
		break;
	case QDMM_LOCAL_IDENTITY: {
		struct qdmm_identity_view identity;
		if (qdmm_parse_identity(message, length, &identity) < 0 ||
		    local.seq != identity.generation) {
			rc = -1;
			break;
		}
		if (state->source_machine) {
			if (state->generation != identity.generation ||
			    strlen(state->source_machine) != identity.source_machine_len ||
			    strlen(state->trust_domain_id) != identity.trust_domain_id_len ||
			    strlen(state->stream_id) != identity.stream_id_len ||
			    memcmp(state->source_machine, identity.source_machine,
				   identity.source_machine_len) != 0 ||
			    memcmp(state->trust_domain_id, identity.trust_domain_id,
				   identity.trust_domain_id_len) != 0 ||
			    memcmp(state->stream_id, identity.stream_id,
				   identity.stream_id_len) != 0)
				rc = -1;
			break;
		}
		state->source_machine = copy_text(
			identity.source_machine, identity.source_machine_len);
		state->trust_domain_id = copy_text(
			identity.trust_domain_id, identity.trust_domain_id_len);
		state->stream_id = copy_text(identity.stream_id, identity.stream_id_len);
		if (!state->source_machine || !state->trust_domain_id ||
		    !state->stream_id) {
			rc = -1;
			break;
		}
		state->generation = identity.generation;
		LOGI("accepted controller display identity source=%s trust=%s "
		     "stream=%s generation=%llu", state->source_machine,
		     state->trust_domain_id, state->stream_id,
		     (unsigned long long)state->generation);
		break;
	}
	case QDMM_LOCAL_ANNOUNCE: {
		struct qdmm_announcement_view announcement;
		if (qdmm_parse_announcement(message, length, &announcement) < 0 ||
		    local.seq != announcement.source_revision ||
		    advertise_proxy(state, &announcement) < 0) {
			rc = -1;
			break;
		}
		state->attached = 1;
		break;
	}
	case QDMM_LOCAL_FRAME: {
		struct qdmm_frame_view frame;
		if (qdmm_parse_frame(message, length, &frame) < 0 ||
		    remember_frame(state, message, length, &frame) < 0)
			rc = -1;
		else if (frame.seq == 1)
			LOGI("received media epoch start geometry=%ux%u",
			     frame.width, frame.height);
		break;
	}
	case QDMM_LOCAL_DETACHED:
		if (!local.seq || local.payload_len) {
			rc = -1;
			break;
		}
		state->attached = 0;
		forward_detached(state, message, length);
		LOGI("controller detached epoch=%llu",
		     (unsigned long long)local.seq);
		break;
	case QDMM_LOCAL_SOURCE_CLOSED:
		if (!local.seq || local.payload_len) {
			rc = -1;
			break;
		}
		state->attached = 0;
		state->stop = 1;
		break;
	default:
		rc = -1;
		break;
	}
	free(message);
	return rc < 0 ? -1 : 1;
}

static int
drain_input(struct state *state)
{
	for (;;) {
		if (state->input_used == sizeof state->input_buffer)
			return -1;
		ssize_t got = recv(state->input_peer_fd,
			state->input_buffer + state->input_used,
			sizeof state->input_buffer - state->input_used,
			MSG_DONTWAIT);
		if (got > 0) {
			state->input_used += (size_t)got;
			continue;
		}
		if (got == 0)
			return -1;
		if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
			return -1;
		break;
	}
	while (state->input_used >= 8) {
		size_t packet_len = 8 + read_le16(state->input_buffer + 6);
		if (packet_len > QDMM_MAX_QDNI_PACKET_SIZE)
			return -1;
		if (state->input_used < packet_len)
			break;
		uint8_t wire[QDMM_MAX_LOCAL_INPUT_WIRE_SIZE];
		size_t wire_len = 0;
		int rc = qdmm_qdni_to_local(
			state->input_buffer, packet_len, wire, &wire_len);
		if (rc < 0)
			return -1;
		if (rc == 0 && state->attached &&
		    send_controller_wire(wire, wire_len) < 0)
			return -1;
		memmove(state->input_buffer, state->input_buffer + packet_len,
			state->input_used - packet_len);
		state->input_used -= packet_len;
	}
	return 0;
}

static int
drain_acks(struct state *state)
{
	for (;;) {
		if (state->ack_used == sizeof state->ack_buffer)
			return -1;
		ssize_t got = recv(state->frame_peer_fd,
			state->ack_buffer + state->ack_used,
			sizeof state->ack_buffer - state->ack_used,
			MSG_DONTWAIT);
		if (got > 0) {
			state->ack_used += (size_t)got;
			continue;
		}
		if (got == 0)
			return -1;
		if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
			return -1;
		break;
	}
	while (state->ack_used >= 4) {
		uint32_t length;
		memcpy(&length, state->ack_buffer, sizeof length);
		length = ntohl(length);
		if (length != QDMM_LOCAL_HEADER_SIZE)
			return -1;
		if (state->ack_used < QDMM_DECODER_ACK_WIRE_SIZE)
			break;
		struct qdmm_local_message_view ack;
		if (qdmm_parse_local_message(
		    state->ack_buffer + 4, QDMM_LOCAL_HEADER_SIZE, &ack) < 0 ||
		    ack.kind != QDMM_LOCAL_DECODER_ACK || !ack.seq || ack.payload_len)
			return -1;
		if (state->attached && send_controller_wire(
		    state->ack_buffer, QDMM_DECODER_ACK_WIRE_SIZE) < 0)
			return -1;
		if (ack.seq == state->pending_frame_seq) {
			free(state->pending_frame);
			state->pending_frame = NULL;
			state->pending_frame_len = 0;
			state->pending_frame_seq = 0;
			state->pending_frame_sent = 0;
		}
		memmove(state->ack_buffer,
			state->ack_buffer + QDMM_DECODER_ACK_WIRE_SIZE,
			state->ack_used - QDMM_DECODER_ACK_WIRE_SIZE);
		state->ack_used -= QDMM_DECODER_ACK_WIRE_SIZE;
	}
	return 0;
}

static void
on_signal(int number)
{
	(void)number;
	g_state.stop = 1;
}

static int
parse_fd(const char *text)
{
	if (!text || !*text)
		return -1;
	errno = 0;
	char *end = NULL;
	long value = strtol(text, &end, 10);
	if (errno || !end || *end || value < 0 || value > INT32_MAX)
		return -1;
	return (int)value;
}

static void
cleanup(struct state *state)
{
	if (state->proxy)
		qdwin_nested_toplevel_v1_destroy(state->proxy);
	if (state->manager)
		qdwin_nested_manager_v1_destroy(state->manager);
	if (state->registry)
		wl_registry_destroy(state->registry);
	if (state->display)
		wl_display_disconnect(state->display);
	close_frame_peer(state);
	close_input_peer(state);
	if (state->frame_listen_fd >= 0)
		close(state->frame_listen_fd);
	if (state->input_listen_fd >= 0)
		close(state->input_listen_fd);
	if (state->controller_fd >= 0)
		close(state->controller_fd);
	if (state->frame_path[0])
		unlink(state->frame_path);
	if (state->input_path[0])
		unlink(state->input_path);
	free(state->pending_frame);
	free(state->app_id);
	free(state->title);
	free(state->source_machine);
	free(state->trust_domain_id);
	free(state->stream_id);
}

int
main(int argc, char **argv)
{
	if (argc != 3 || strcmp(argv[1], "--controller-fd") != 0 ||
	    (g_state.controller_fd = parse_fd(argv[2])) < 0) {
		fprintf(stderr, "usage: %s --controller-fd FD\n", argv[0]);
		return 2;
	}
	if (!same_uid_peer(g_state.controller_fd)) {
		LOGE("controller fd is not a same-uid Unix peer");
		return 3;
	}
	struct timeval timeout = { .tv_sec = 5 };
	setsockopt(g_state.controller_fd, SOL_SOCKET, SO_RCVTIMEO,
		   &timeout, sizeof timeout);
	struct sigaction action = { .sa_handler = on_signal };
	sigemptyset(&action.sa_mask);
	sigaction(SIGTERM, &action, NULL);
	sigaction(SIGINT, &action, NULL);
	signal(SIGPIPE, SIG_IGN);
	if (mint_local_endpoints(&g_state) < 0 || start_wayland(&g_state) < 0) {
		LOGE("local endpoint startup failed: %s", strerror(errno));
		cleanup(&g_state);
		return 4;
	}

	int result = 0;
	while (!g_state.stop) {
		wl_display_dispatch_pending(g_state.display);
		if (wl_display_flush(g_state.display) < 0 && errno != EAGAIN) {
			result = 5;
			break;
		}
		struct pollfd fds[6] = {
			{ .fd = g_state.controller_fd, .events = POLLIN },
			{ .fd = wl_display_get_fd(g_state.display), .events = POLLIN },
			{ .fd = g_state.frame_listen_fd, .events = POLLIN },
			{ .fd = g_state.input_listen_fd, .events = POLLIN },
			{ .fd = g_state.frame_peer_fd, .events = POLLIN },
			{ .fd = g_state.input_peer_fd, .events = POLLIN },
		};
		int rc = poll(fds, 6, 1000);
		if (rc < 0) {
			if (errno == EINTR)
				continue;
			result = 5;
			break;
		}
		if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
			rc = handle_controller(&g_state);
			if (rc <= 0) {
				result = rc < 0 ? 6 : 0;
				break;
			}
		}
		if (fds[1].revents & POLLIN &&
		    wl_display_dispatch(g_state.display) < 0) {
			result = 7;
			break;
		}
		if (fds[1].revents & (POLLHUP | POLLERR)) {
			result = 7;
			break;
		}
		if (fds[2].revents & POLLIN) {
			int peer = accept_peer(g_state.frame_listen_fd);
			if (peer >= 0) {
				close_frame_peer(&g_state);
				g_state.frame_peer_fd = peer;
				struct timeval send_timeout = { .tv_sec = 5 };
				setsockopt(peer, SOL_SOCKET, SO_SNDTIMEO,
					   &send_timeout, sizeof send_timeout);
				send_pending_frame(&g_state);
			}
		}
		if (fds[3].revents & POLLIN) {
			int peer = accept_peer(g_state.input_listen_fd);
			if (peer >= 0) {
				close_input_peer(&g_state);
				g_state.input_peer_fd = peer;
			}
		}
		if (g_state.frame_peer_fd >= 0 &&
		    fds[4].revents & (POLLIN | POLLHUP | POLLERR) &&
		    drain_acks(&g_state) < 0)
			close_frame_peer(&g_state);
		if (g_state.input_peer_fd >= 0 &&
		    fds[5].revents & (POLLIN | POLLHUP | POLLERR) &&
		    drain_input(&g_state) < 0)
			close_input_peer(&g_state);
	}
	cleanup(&g_state);
	return result;
}
