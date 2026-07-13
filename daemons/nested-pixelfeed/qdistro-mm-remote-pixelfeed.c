/*
 * Viewer-local R6 remote pixelfeed.
 *
 * qdshell launches this only for a qdistro.remote:<token> nested pixel source.
 * The matching viewer helper owns a mode-0600 AF_UNIX socket under
 * XDG_RUNTIME_DIR.  This process verifies SO_PEERCRED, receives bounded QDML
 * frame messages, validates the embedded QDMF BGRx geometry, allocates fresh
 * local wl_shm buffers, and calls the unchanged qdwin_shell_v1
 * bind_proxy_pixels ownership gate.  No remote fd, Wayland id, PipeWire name,
 * or Unix path crosses the authenticated network boundary.
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
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>

#include "mm-remote-frame-protocol.h"
#include "qdwin-shell-v1-client-protocol.h"

#define LOGI(fmt, ...) \
	fprintf(stderr, "[mm-remote-pixelfeed %d] " fmt "\n", \
		(int)getpid(), ##__VA_ARGS__)
#define LOGE(fmt, ...) \
	fprintf(stderr, "[mm-remote-pixelfeed %d ERR] " fmt "\n", \
		(int)getpid(), ##__VA_ARGS__)

struct state;

struct slot {
	struct state *state;
	struct wl_buffer *buffer;
	uint8_t *data;
	size_t size;
	int busy;
};

struct state {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_compositor *compositor;
	struct wl_shm *shm;
	struct qdwin_shell_v1 *shell;
	uint32_t shell_version;
	struct wl_surface *surface;
	struct slot slots[2];
	uint32_t width;
	uint32_t height;
	uint32_t stride;
	int wire_fd;
	volatile sig_atomic_t stop;
};

static struct state g_state = { .wire_fd = -1 };

static int
wait_readable(int fd, const struct timespec *deadline)
{
	for (;;) {
		int timeout_ms = 1000;
		if (deadline) {
			struct timespec now;
			clock_gettime(CLOCK_MONOTONIC, &now);
			int64_t remaining_ms =
				(int64_t)(deadline->tv_sec - now.tv_sec) * 1000 +
				(deadline->tv_nsec - now.tv_nsec) / 1000000;
			if (remaining_ms <= 0) {
				errno = ETIMEDOUT;
				return -1;
			}
			if (remaining_ms < timeout_ms)
				timeout_ms = (int)remaining_ms;
		}
		struct pollfd pfd = { .fd = fd, .events = POLLIN };
		int rc = poll(&pfd, 1, timeout_ms);
		if (rc > 0) {
			if (pfd.revents & (POLLERR | POLLNVAL)) {
				errno = EIO;
				return -1;
			}
			return 0;
		}
		if (rc < 0 && errno != EINTR)
			return -1;
		if (g_state.stop) {
			errno = EINTR;
			return -1;
		}
	}
}

static int
recv_exact(int fd, void *buffer, size_t length, int allow_initial_idle)
{
	uint8_t *out = buffer;
	size_t used = 0;
	struct timespec deadline = {0};
	if (!allow_initial_idle) {
		clock_gettime(CLOCK_MONOTONIC, &deadline);
		deadline.tv_sec += 5;
	}
	while (used < length) {
		const struct timespec *limit =
			allow_initial_idle && used == 0 ? NULL : &deadline;
		if (wait_readable(fd, limit) < 0)
			return -1;
		ssize_t got = recv(fd, out + used, length - used, 0);
		if (got == 0)
			return 0;
		if (got < 0) {
			if (errno == EINTR && !g_state.stop)
				continue;
			return -1;
		}
		if (allow_initial_idle && used == 0) {
			clock_gettime(CLOCK_MONOTONIC, &deadline);
			deadline.tv_sec += 5;
		}
		used += (size_t)got;
	}
	return 1;
}

static int
send_exact(int fd, const void *buffer, size_t length)
{
	const uint8_t *in = buffer;
	size_t used = 0;
	while (used < length) {
		ssize_t sent = send(fd, in + used, length - used, MSG_NOSIGNAL);
		if (sent < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		used += (size_t)sent;
	}
	return 0;
}

static int
recv_local_frame(int fd, uint8_t **out, size_t *out_len)
{
	uint32_t wire_len;
	int rc = recv_exact(fd, &wire_len, sizeof wire_len, 1);
	if (rc <= 0)
		return rc;
	size_t length = ntohl(wire_len);
	if (length < QDMM_LOCAL_HEADER_SIZE || length > QDMM_MAX_LOCAL_BYTES) {
		errno = EMSGSIZE;
		return -1;
	}
	uint8_t *payload = malloc(length);
	if (!payload)
		return -1;
	rc = recv_exact(fd, payload, length, 0);
	if (rc != 1) {
		free(payload);
		return rc == 0 ? -1 : rc;
	}
	*out = payload;
	*out_len = length;
	return 1;
}

static int
send_decoder_ack(int fd, uint64_t seq)
{
	uint8_t wire[QDMM_DECODER_ACK_WIRE_SIZE];
	if (qdmm_build_decoder_ack(seq, wire) < 0)
		return -1;
	return send_exact(fd, wire, sizeof wire);
}

static int
connect_frame_socket(const char *source)
{
	const char *runtime = getenv("XDG_RUNTIME_DIR");
	struct sockaddr_un address = { .sun_family = AF_UNIX };
	if (qdmm_build_frame_socket_path(
	    runtime, source, address.sun_path, sizeof address.sun_path) < 0) {
		errno = EINVAL;
		return -1;
	}
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return -1;
	if (connect(fd, (struct sockaddr *)&address, sizeof address) < 0) {
		close(fd);
		return -1;
	}
	struct ucred peer = {0};
	socklen_t peer_len = sizeof peer;
	if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &peer_len) < 0 ||
	    peer_len != sizeof peer || peer.uid != getuid()) {
		close(fd);
		errno = EPERM;
		return -1;
	}
	return fd;
}

static void
registry_global(void *data, struct wl_registry *registry, uint32_t name,
		const char *interface, uint32_t version)
{
	struct state *state = data;
	if (strcmp(interface, wl_compositor_interface.name) == 0) {
		state->compositor = wl_registry_bind(registry, name,
			&wl_compositor_interface, version > 4 ? 4 : version);
	} else if (strcmp(interface, wl_shm_interface.name) == 0) {
		state->shm = wl_registry_bind(registry, name,
			&wl_shm_interface, 1);
	} else if (strcmp(interface, qdwin_shell_v1_interface.name) == 0) {
		state->shell_version = version > 9 ? 9 : version;
		state->shell = wl_registry_bind(registry, name,
			&qdwin_shell_v1_interface, state->shell_version);
	}
}

static void
registry_remove(void *data, struct wl_registry *registry, uint32_t name)
{
	(void)data;
	(void)registry;
	(void)name;
}

static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_remove,
};

static int
make_shm_fd(size_t size)
{
	int fd = memfd_create("qdistro-mm-remote-frame",
		MFD_CLOEXEC | MFD_ALLOW_SEALING);
	if (fd < 0)
		return -1;
	if (ftruncate(fd, (off_t)size) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

static void
buffer_release(void *data, struct wl_buffer *buffer)
{
	struct slot *slot = data;
	(void)buffer;
	slot->busy = 0;
}

static const struct wl_buffer_listener buffer_listener = {
	.release = buffer_release,
};

static int
slot_init(struct state *state, struct slot *slot)
{
	size_t size = (size_t)state->stride * state->height;
	int fd = make_shm_fd(size);
	if (fd < 0)
		return -1;
	void *data = mmap(NULL, size, PROT_READ | PROT_WRITE,
		MAP_SHARED, fd, 0);
	if (data == MAP_FAILED) {
		close(fd);
		return -1;
	}
	struct wl_shm_pool *pool = wl_shm_create_pool(state->shm, fd, size);
	close(fd);
	if (!pool) {
		munmap(data, size);
		return -1;
	}
	struct wl_buffer *buffer = wl_shm_pool_create_buffer(
		pool, 0, (int32_t)state->width, (int32_t)state->height,
		(int32_t)state->stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	if (!buffer) {
		munmap(data, size);
		return -1;
	}
	slot->state = state;
	slot->buffer = buffer;
	slot->data = data;
	slot->size = size;
	wl_buffer_add_listener(buffer, &buffer_listener, slot);
	return 0;
}

static void
slot_destroy(struct slot *slot)
{
	if (slot->buffer)
		wl_buffer_destroy(slot->buffer);
	if (slot->data)
		munmap(slot->data, slot->size);
	memset(slot, 0, sizeof *slot);
}

static int
wayland_start(struct state *state, uint32_t handle,
	      const struct qdmm_frame_view *first)
{
	state->width = first->width;
	state->height = first->height;
	state->stride = first->stride;
	unsetenv("WAYLAND_SOCKET");
	state->display = wl_display_connect(NULL);
	if (!state->display)
		return -1;
	state->registry = wl_display_get_registry(state->display);
	wl_registry_add_listener(state->registry, &registry_listener, state);
	if (wl_display_roundtrip(state->display) < 0 ||
	    !state->compositor || !state->shm || !state->shell ||
	    state->shell_version < 9)
		return -1;
	if (slot_init(state, &state->slots[0]) < 0 ||
	    slot_init(state, &state->slots[1]) < 0)
		return -1;
	state->surface = wl_compositor_create_surface(state->compositor);
	if (!state->surface)
		return -1;
	memcpy(state->slots[0].data, first->pixels, first->pixels_len);
	state->slots[0].busy = 1;
	wl_surface_attach(state->surface, state->slots[0].buffer, 0, 0);
	wl_surface_damage_buffer(state->surface, 0, 0,
		(int32_t)state->width, (int32_t)state->height);
	wl_surface_commit(state->surface);
	if (wl_display_roundtrip(state->display) < 0)
		return -1;
	qdwin_shell_v1_bind_proxy_pixels(state->shell, handle, state->surface);
	return wl_display_roundtrip(state->display) < 0 ? -1 : 0;
}

static struct slot *
free_slot(struct state *state)
{
	for (size_t i = 0; i < 2; i++) {
		if (!state->slots[i].busy)
			return &state->slots[i];
	}
	return NULL;
}

static int
dispatch_wayland(struct state *state, int timeout_ms)
{
	if (wl_display_dispatch_pending(state->display) < 0)
		return -1;
	if (wl_display_flush(state->display) < 0 && errno != EAGAIN)
		return -1;
	struct pollfd pfd = {
		.fd = wl_display_get_fd(state->display), .events = POLLIN,
	};
	int rc;
	do {
		rc = poll(&pfd, 1, timeout_ms);
	} while (rc < 0 && errno == EINTR);
	if (rc < 0)
		return -1;
	if (rc > 0 && (pfd.revents & POLLIN) &&
	    wl_display_dispatch(state->display) < 0)
		return -1;
	return 0;
}

static int
commit_frame(struct state *state, const struct qdmm_frame_view *frame)
{
	if (frame->width != state->width || frame->height != state->height ||
	    frame->stride != state->stride || frame->pixels_len !=
	    (size_t)state->stride * state->height)
		return -1;
	struct slot *slot = free_slot(state);
	struct timespec deadline;
	clock_gettime(CLOCK_MONOTONIC, &deadline);
	deadline.tv_sec += 5;
	while (!slot) {
		if (dispatch_wayland(state, 100) < 0)
			return -1;
		struct timespec now;
		clock_gettime(CLOCK_MONOTONIC, &now);
		if (now.tv_sec >= deadline.tv_sec) {
			errno = ETIMEDOUT;
			return -1;
		}
		slot = free_slot(state);
	}
	memcpy(slot->data, frame->pixels, frame->pixels_len);
	slot->busy = 1;
	wl_surface_attach(state->surface, slot->buffer, 0, 0);
	wl_surface_damage_buffer(state->surface, 0, 0,
		(int32_t)state->width, (int32_t)state->height);
	wl_surface_commit(state->surface);
	if (wl_display_flush(state->display) < 0 && errno != EAGAIN)
		return -1;
	return 0;
}

static int
commit_blank(struct state *state)
{
	struct slot *slot = free_slot(state);
	struct timespec deadline;
	clock_gettime(CLOCK_MONOTONIC, &deadline);
	deadline.tv_sec += 5;
	while (!slot) {
		if (dispatch_wayland(state, 100) < 0)
			return -1;
		struct timespec now;
		clock_gettime(CLOCK_MONOTONIC, &now);
		if (now.tv_sec >= deadline.tv_sec) {
			errno = ETIMEDOUT;
			return -1;
		}
		slot = free_slot(state);
	}
	memset(slot->data, 0, slot->size);
	slot->busy = 1;
	wl_surface_attach(state->surface, slot->buffer, 0, 0);
	wl_surface_damage_buffer(state->surface, 0, 0,
		(int32_t)state->width, (int32_t)state->height);
	wl_surface_commit(state->surface);
	if (wl_display_flush(state->display) < 0 && errno != EAGAIN)
		return -1;
	return 0;
}

static void
on_signal(int signal_number)
{
	(void)signal_number;
	g_state.stop = 1;
}

static int
parse_handle(const char *text, uint32_t *out)
{
	if (!text || !*text)
		return -1;
	errno = 0;
	char *end = NULL;
	unsigned long value = strtoul(text, &end, 10);
	if (errno || !end || *end || !value || value > UINT32_MAX)
		return -1;
	*out = (uint32_t)value;
	return 0;
}

int
main(int argc, char **argv)
{
	if (argc != 3) {
		fprintf(stderr, "usage: %s <handle> qdistro.remote:<token>\n", argv[0]);
		return 2;
	}
	uint32_t handle;
	if (parse_handle(argv[1], &handle) < 0) {
		LOGE("invalid proxy handle");
		return 2;
	}
	g_state.wire_fd = connect_frame_socket(argv[2]);
	if (g_state.wire_fd < 0) {
		LOGE("frame socket: %s", strerror(errno));
		return 3;
	}
	struct sigaction action = { .sa_handler = on_signal };
	sigemptyset(&action.sa_mask);
	sigaction(SIGTERM, &action, NULL);
	sigaction(SIGINT, &action, NULL);
	signal(SIGPIPE, SIG_IGN);

	int result = 0;
	int started = 0;
	while (!g_state.stop) {
		uint8_t *message = NULL;
		size_t length = 0;
		int rc = recv_local_frame(g_state.wire_fd, &message, &length);
		if (rc == 0)
			break;
		if (rc < 0) {
			LOGE("local frame read: %s", strerror(errno));
			result = 4;
			break;
		}
		struct qdmm_local_message_view local;
		if (qdmm_parse_local_message(message, length, &local) < 0) {
			LOGE("invalid local boundary packet");
			free(message);
			result = 5;
			break;
		}
		if (local.kind == QDMM_LOCAL_DETACHED) {
			if (!local.seq || local.payload_len ||
			    (started && commit_blank(&g_state) < 0)) {
				LOGE("invalid or failed detach blank");
				free(message);
				result = 5;
				break;
			}
			free(message);
			continue;
		}
		struct qdmm_frame_view frame;
		if (qdmm_parse_frame(message, length, &frame) < 0) {
			LOGE("invalid local frame packet");
			free(message);
			result = 5;
			break;
		}
		int already_committed = 0;
		if (!started) {
			if (wayland_start(&g_state, handle, &frame) < 0) {
				LOGE("Wayland pixel binding failed");
				free(message);
				result = 6;
				break;
			}
			started = 1;
			already_committed = 1;
			LOGI("bound handle=%u geometry=%ux%u stride=%u",
				handle, frame.width, frame.height, frame.stride);
		}
		if (!already_committed && commit_frame(&g_state, &frame) < 0) {
			LOGE("frame commit failed: %s", strerror(errno));
			free(message);
			result = 7;
			break;
		}
		if (send_decoder_ack(g_state.wire_fd, frame.seq) < 0) {
			free(message);
			result = 8;
			break;
		}
		free(message);
		dispatch_wayland(&g_state, 0);
	}

	for (size_t i = 0; i < 2; i++)
		slot_destroy(&g_state.slots[i]);
	if (g_state.surface)
		wl_surface_destroy(g_state.surface);
	if (g_state.display)
		wl_display_disconnect(g_state.display);
	if (g_state.wire_fd >= 0)
		close(g_state.wire_fd);
	return result;
}
