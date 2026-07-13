/*
 * R6 source-local PipeWire/QDNI helper.
 *
 * A sealed inherited QDMS config names one local Weston PipeWire producer,
 * one local qdwin QDNI sink, and source-owned metadata. The authenticated
 * controller socket never supplies a local name or fd. Raw BGRx frames flow
 * to the controller; fixed semantic input records flow back to QDNI.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <endian.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/param/video/raw.h>
#include <spa/pod/builder.h>

#include "mm-remote-frame-protocol.h"
#include "pw-target-resolver.h"

#define LOGI(fmt, ...) fprintf(stderr, "[mm-source-helper %d] " fmt "\n", \
	(int)getpid(), ##__VA_ARGS__)
#define LOGE(fmt, ...) fprintf(stderr, "[mm-source-helper %d ERR] " fmt "\n", \
	(int)getpid(), ##__VA_ARGS__)
#define MAX_SOURCE_CONFIG_BYTES 8192u
#define MAX_PW_OBSERVATIONS 32u

struct source_client {
	struct qdpf_pw_client_observation observed;
	struct pw_client *proxy;
	struct spa_hook listener;
};

struct state {
	int controller_fd;
	int config_fd;
	int close_fd;
	int input_fd;
	uint8_t *config_bytes;
	size_t config_len;
	struct qdmm_source_config_view config;
	char *pw_node;
	char *pw_target;
	char *input_sink;
	int pw_pid;
	struct pw_thread_loop *pw_loop;
	struct pw_context *pw_context;
	struct pw_core *pw_core;
	struct pw_registry *pw_registry;
	struct pw_stream *stream;
	struct spa_hook core_listener;
	struct spa_hook registry_listener;
	struct spa_hook stream_listener;
	int pw_sync_seq;
	int pw_sync_done;
	struct qdpf_pw_node_observation candidates[MAX_PW_OBSERVATIONS];
	size_t candidate_count;
	struct source_client clients[MAX_PW_OBSERVATIONS];
	size_t client_count;
	int observations_truncated;
	struct spa_video_info_raw format;
	int format_known;
	int connected;
	int announcement_sent;
	uint64_t media_seq;
	uint8_t *last_pixels;
	size_t last_pixels_len;
	int frame_pending;
	pthread_t frame_thread;
	int frame_thread_started;
	pthread_mutex_t mutex;
	pthread_mutex_t send_mutex;
	pthread_cond_t frame_ready;
	volatile sig_atomic_t stop;
};

static struct state g_state = {
	.controller_fd = -1,
	.config_fd = -1,
	.close_fd = -1,
	.input_fd = -1,
};

static int
same_uid_peer(int fd)
{
	struct ucred peer = {0};
	socklen_t length = sizeof peer;
	return getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &length) == 0 &&
	       length == sizeof peer && peer.uid == getuid();
}

static int
send_exact_unlocked(int fd, const void *buffer, size_t length)
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
send_controller_wire(struct state *state, const uint8_t *wire, size_t length)
{
	pthread_mutex_lock(&state->send_mutex);
	int rc = send_exact_unlocked(state->controller_fd, wire, length);
	pthread_mutex_unlock(&state->send_mutex);
	return rc;
}

static int
send_controller_message(struct state *state, uint8_t kind, uint64_t seq)
{
	uint8_t wire[4 + QDMM_LOCAL_HEADER_SIZE] = {0};
	uint32_t size = htonl(QDMM_LOCAL_HEADER_SIZE);
	memcpy(wire, &size, sizeof size);
	memcpy(wire + 4, "QDML", 4);
	wire[8] = 1;
	wire[9] = kind;
	uint64_t be_seq = htobe64(seq);
	memcpy(wire + 12, &be_seq, sizeof be_seq);
	return send_controller_wire(state, wire, sizeof wire);
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
recv_controller_message(struct state *state, uint8_t **out, size_t *out_len)
{
	uint32_t wire_len;
	int rc = recv_exact(state->controller_fd, &wire_len, sizeof wire_len);
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
	rc = recv_exact(state->controller_fd, message, length);
	if (rc != 1) {
		free(message);
		return -1;
	}
	*out = message;
	*out_len = length;
	return 1;
}

static char *
copy_field(const uint8_t *field, size_t length)
{
	char *copy = malloc(length + 1);
	if (!copy)
		return NULL;
	memcpy(copy, field, length);
	copy[length] = '\0';
	return copy;
}

static int
read_sealed_config(struct state *state)
{
	struct stat st;
	if (fstat(state->config_fd, &st) < 0 || st.st_size <= 0 ||
	    st.st_size > MAX_SOURCE_CONFIG_BYTES)
		return -1;
	int seals = fcntl(state->config_fd, F_GET_SEALS);
	int required = F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE;
	if (seals < 0 || (seals & required) != required) {
		errno = EPERM;
		return -1;
	}
	state->config_len = (size_t)st.st_size;
	state->config_bytes = malloc(state->config_len);
	if (!state->config_bytes)
		return -1;
	if (lseek(state->config_fd, 0, SEEK_SET) < 0)
		return -1;
	size_t used = 0;
	while (used < state->config_len) {
		ssize_t got = read(state->config_fd,
			state->config_bytes + used, state->config_len - used);
		if (got < 0 && errno == EINTR)
			continue;
		if (got <= 0)
			return -1;
		used += (size_t)got;
	}
	if (qdmm_parse_source_config(
	    state->config_bytes, state->config_len, &state->config) < 0) {
		errno = EINVAL;
		return -1;
	}
	state->pw_node = copy_field(
		state->config.pw_node, state->config.pw_node_len);
	state->input_sink = copy_field(
		state->config.input_sink, state->config.input_sink_len);
	if (!state->pw_node || !state->input_sink)
		return -1;
	return 0;
}

static int
validate_runtime_input_path(struct state *state)
{
	const char *runtime = getenv("XDG_RUNTIME_DIR");
	size_t runtime_len = runtime ? strlen(runtime) : 0;
	return runtime && runtime[0] == '/' && runtime_len > 1 &&
	       strncmp(state->input_sink, runtime, runtime_len) == 0 &&
	       state->input_sink[runtime_len] == '/';
}

static int
connect_input_sink(struct state *state)
{
	if (!validate_runtime_input_path(state) ||
	    strlen(state->input_sink) >= sizeof(((struct sockaddr_un *)0)->sun_path)) {
		errno = EINVAL;
		return -1;
	}
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return -1;
	struct sockaddr_un address = { .sun_family = AF_UNIX };
	strcpy(address.sun_path, state->input_sink);
	if (connect(fd, (struct sockaddr *)&address, sizeof address) < 0 ||
	    !same_uid_peer(fd)) {
		int saved = errno ? errno : EPERM;
		close(fd);
		errno = saved;
		return -1;
	}
	state->input_fd = fd;
	return 0;
}

static int
parse_pw_identity(struct state *state)
{
	const char *pid_start = state->pw_node + strlen("weston.pipewire:");
	char *end = NULL;
	errno = 0;
	long pid = strtol(pid_start, &end, 10);
	if (errno || pid <= 0 || pid > INT_MAX || !end || *end != ':' ||
	    !end[1])
		return -1;
	state->pw_pid = (int)pid;
	size_t target_len = strlen("weston.") + strlen(end + 1) + 1;
	state->pw_target = malloc(target_len);
	if (!state->pw_target)
		return -1;
	snprintf(state->pw_target, target_len, "weston.%s", end + 1);
	return 0;
}

static void
on_pw_core_done(void *data, uint32_t id, int seq)
{
	struct state *state = data;
	if (id == PW_ID_CORE && seq == state->pw_sync_seq) {
		state->pw_sync_done = 1;
		pw_thread_loop_signal(state->pw_loop, false);
	}
}

static const struct pw_core_events core_events = {
	PW_VERSION_CORE_EVENTS,
	.done = on_pw_core_done,
};

static void
on_pw_client_info(void *data, const struct pw_client_info *info)
{
	struct source_client *client = data;
	if (!info || !info->props)
		return;
	const char *pid = spa_dict_lookup(info->props, PW_KEY_APP_PROCESS_ID);
	client->observed.pid_known =
		pid && qdpf_parse_positive_pid(pid, &client->observed.pid) == 0;
}

static const struct pw_client_events client_events = {
	PW_VERSION_CLIENT_EVENTS,
	.info = on_pw_client_info,
};

static void
on_pw_registry_global(void *data, uint32_t id, uint32_t permissions,
		      const char *type, uint32_t version,
		      const struct spa_dict *props)
{
	struct state *state = data;
	(void)permissions;
	(void)version;
	if (!props)
		return;
	if (strcmp(type, PW_TYPE_INTERFACE_Client) == 0) {
		if (state->client_count >= MAX_PW_OBSERVATIONS) {
			state->observations_truncated = 1;
			return;
		}
		struct source_client *entry = &state->clients[state->client_count++];
		entry->observed.id = id;
		entry->proxy = pw_registry_bind(
			state->pw_registry, id, type, PW_VERSION_CLIENT, 0);
		if (entry->proxy)
			pw_client_add_listener(
				entry->proxy, &entry->listener, &client_events, entry);
		return;
	}
	if (strcmp(type, PW_TYPE_INTERFACE_Node) != 0)
		return;
	const char *name = spa_dict_lookup(props, PW_KEY_NODE_NAME);
	const char *client = spa_dict_lookup(props, PW_KEY_CLIENT_ID);
	const char *serial = spa_dict_lookup(props, PW_KEY_OBJECT_SERIAL);
	if (!name || !client || !serial || strcmp(name, state->pw_target) != 0)
		return;
	if (state->candidate_count >= MAX_PW_OBSERVATIONS) {
		state->observations_truncated = 1;
		return;
	}
	struct qdpf_pw_node_observation *candidate =
		&state->candidates[state->candidate_count];
	if (qdpf_parse_u32(client, &candidate->client_id) != 0 ||
	    qdpf_parse_u64(serial, &candidate->serial) != 0)
		return;
	candidate->id = id;
	state->candidate_count++;
}

static const struct pw_registry_events registry_events = {
	PW_VERSION_REGISTRY_EVENTS,
	.global = on_pw_registry_global,
};

static int
send_announcement_locked(struct state *state)
{
	if (!state->connected || !state->format_known || state->announcement_sent)
		return 0;
	size_t capacity = 4 + QDMM_LOCAL_HEADER_SIZE +
		QDMM_LOCAL_ANNOUNCE_HEADER_SIZE + state->config.app_id_len +
		state->config.title_len;
	uint8_t *wire = malloc(capacity);
	if (!wire)
		return -1;
	size_t wire_len = 0;
	int rc = qdmm_build_announcement_wire(
		&state->config, state->format.size.width, state->format.size.height,
		state->format.size.width * 4u, wire, capacity, &wire_len);
	if (rc == 0)
		rc = send_controller_wire(state, wire, wire_len);
	free(wire);
	if (rc == 0) {
		state->announcement_sent = 1;
		LOGI("announced revision=%llu geometry=%ux%u",
		     (unsigned long long)state->config.source_revision,
		     state->format.size.width, state->format.size.height);
	}
	return rc;
}

static void
on_pw_state_changed(void *data, enum pw_stream_state old,
		    enum pw_stream_state stream_state, const char *error)
{
	struct state *state = data;
	LOGI("PipeWire %s -> %s%s%s",
	     pw_stream_state_as_string(old), pw_stream_state_as_string(stream_state),
	     error ? ": " : "", error ? error : "");
	if (stream_state == PW_STREAM_STATE_ERROR)
		state->stop = 1;
}

static void
on_pw_param_changed(void *data, uint32_t id, const struct spa_pod *param)
{
	struct state *state = data;
	if (id != SPA_PARAM_Format || !param)
		return;
	struct spa_video_info info = {0};
	if (spa_format_parse(param, &info.media_type, &info.media_subtype) < 0 ||
	    info.media_type != SPA_MEDIA_TYPE_video ||
	    info.media_subtype != SPA_MEDIA_SUBTYPE_raw ||
	    spa_format_video_raw_parse(param, &info.info.raw) < 0 ||
	    info.info.raw.format != SPA_VIDEO_FORMAT_BGRx ||
	    !info.info.raw.size.width || !info.info.raw.size.height ||
	    info.info.raw.size.width > 8192 || info.info.raw.size.height > 8192 ||
	    (uint64_t)info.info.raw.size.width * 4u * info.info.raw.size.height +
	    QDMM_FRAME_HEADER_SIZE > QDMM_MAX_MEDIA_BYTES) {
		LOGE("unsupported or over-limit PipeWire format");
		state->stop = 1;
		return;
	}
	pthread_mutex_lock(&state->mutex);
	state->format = info.info.raw;
	state->format_known = 1;
	if (send_announcement_locked(state) < 0)
		state->stop = 1;
	if (!state->stop && state->announcement_sent && state->last_pixels) {
		state->frame_pending = 1;
		pthread_cond_signal(&state->frame_ready);
	}
	pthread_mutex_unlock(&state->mutex);
}

static void *
frame_sender_main(void *data)
{
	struct state *state = data;
	for (;;) {
		pthread_mutex_lock(&state->mutex);
		while (!state->stop && (!state->frame_pending ||
		       !state->connected || !state->announcement_sent))
			pthread_cond_wait(&state->frame_ready, &state->mutex);
		if (state->stop) {
			pthread_mutex_unlock(&state->mutex);
			return NULL;
		}
		uint32_t width = state->format.size.width;
		uint32_t height = state->format.size.height;
		size_t pixels_len = state->last_pixels_len;
		size_t wire_capacity = 4 + QDMM_LOCAL_HEADER_SIZE +
			QDMM_FRAME_HEADER_SIZE + pixels_len;
		uint8_t *wire = malloc(wire_capacity);
		size_t wire_len = 0;
		int rc = wire ? qdmm_build_frame_wire(
			++state->media_seq, width, height, width * 4u,
			state->last_pixels, pixels_len,
			wire, wire_capacity, &wire_len) : -1;
		if (rc == 0)
			state->frame_pending = 0;
		pthread_mutex_unlock(&state->mutex);

		if (rc == 0)
			rc = send_controller_wire(state, wire, wire_len);
		free(wire);
		if (rc < 0) {
			pthread_mutex_lock(&state->mutex);
			state->stop = 1;
			pthread_cond_broadcast(&state->frame_ready);
			pthread_mutex_unlock(&state->mutex);
			return NULL;
		}
	}
}

static void
on_pw_process(void *data)
{
	struct state *state = data;
	struct pw_buffer *pw_buffer = pw_stream_dequeue_buffer(state->stream);
	if (!pw_buffer)
		return;
	struct spa_buffer *buffer = pw_buffer->buffer;
	pthread_mutex_lock(&state->mutex);
	if (!state->format_known || !buffer || !buffer->n_datas) {
		pthread_mutex_unlock(&state->mutex);
		pw_stream_queue_buffer(state->stream, pw_buffer);
		return;
	}
	struct spa_data *data0 = &buffer->datas[0];
	uint32_t width = state->format.size.width;
	uint32_t height = state->format.size.height;
	uint32_t stride = data0->chunk ? (uint32_t)data0->chunk->stride : 0;
	if (!stride)
		stride = width * 4u;
	uint32_t offset = data0->chunk ? data0->chunk->offset : 0;
	size_t source_needed = (size_t)(height - 1) * stride + width * 4u;
	if (!data0->data || !data0->chunk || stride < width * 4u ||
	    offset > data0->maxsize || source_needed > data0->maxsize - offset ||
	    source_needed > data0->chunk->size) {
		pthread_mutex_unlock(&state->mutex);
		pw_stream_queue_buffer(state->stream, pw_buffer);
		return;
	}
	size_t pixels_len = (size_t)width * 4u * height;
	uint8_t *pixels = malloc(pixels_len);
	if (!pixels) {
		pthread_mutex_unlock(&state->mutex);
		pw_stream_queue_buffer(state->stream, pw_buffer);
		return;
	}
	const uint8_t *source = (const uint8_t *)data0->data + offset;
	for (uint32_t y = 0; y < height; y++) {
		const uint8_t *src = source + (size_t)y * stride;
		uint8_t *dst = pixels + (size_t)y * width * 4u;
		memcpy(dst, src, (size_t)width * 4u);
		for (uint32_t x = 0; x < width; x++)
			dst[x * 4u + 3] = 0xff;
	}
	free(state->last_pixels);
	state->last_pixels = pixels;
	state->last_pixels_len = pixels_len;
	state->frame_pending = 1;
	pthread_cond_signal(&state->frame_ready);
	pthread_mutex_unlock(&state->mutex);
	pw_stream_queue_buffer(state->stream, pw_buffer);
}

static const struct pw_stream_events stream_events = {
	PW_VERSION_STREAM_EVENTS,
	.state_changed = on_pw_state_changed,
	.param_changed = on_pw_param_changed,
	.process = on_pw_process,
};

static int
start_pipewire(struct state *state)
{
	state->pw_loop = pw_thread_loop_new("mm-source-pipewire", NULL);
	if (!state->pw_loop)
		return -1;
	state->pw_context = pw_context_new(
		pw_thread_loop_get_loop(state->pw_loop), NULL, 0);
	if (!state->pw_context || pw_thread_loop_start(state->pw_loop) < 0)
		return -1;
	pw_thread_loop_lock(state->pw_loop);
	state->pw_core = pw_context_connect(state->pw_context, NULL, 0);
	if (!state->pw_core) {
		pw_thread_loop_unlock(state->pw_loop);
		return -1;
	}
	state->pw_registry = pw_core_get_registry(
		state->pw_core, PW_VERSION_REGISTRY, 0);
	pw_core_add_listener(
		state->pw_core, &state->core_listener, &core_events, state);
	pw_registry_add_listener(state->pw_registry, &state->registry_listener,
		&registry_events, state);
	for (int barrier = 0; barrier < 2; barrier++) {
		state->pw_sync_done = 0;
		state->pw_sync_seq = pw_core_sync(state->pw_core, PW_ID_CORE, 0);
		while (!state->pw_sync_done)
			pw_thread_loop_wait(state->pw_loop);
	}
	if (state->observations_truncated) {
		pw_thread_loop_unlock(state->pw_loop);
		errno = EOVERFLOW;
		return -1;
	}
	struct qdpf_pw_client_observation clients[MAX_PW_OBSERVATIONS];
	for (size_t i = 0; i < state->client_count; i++)
		clients[i] = state->clients[i].observed;
	struct qdpf_pw_target target = {0};
	if (qdpf_resolve_pw_target(
	    state->pw_pid, clients, state->client_count,
	    state->candidates, state->candidate_count, &target) !=
	    QDPF_PW_RESOLVE_OK) {
		pw_thread_loop_unlock(state->pw_loop);
		errno = ENOENT;
		return -1;
	}
	char target_serial[32];
	snprintf(target_serial, sizeof target_serial, "%llu",
		 (unsigned long long)target.serial);
	struct pw_properties *properties = pw_properties_new(
		PW_KEY_MEDIA_TYPE, "Video",
		PW_KEY_MEDIA_CATEGORY, "Capture",
		PW_KEY_MEDIA_ROLE, "Screen",
		PW_KEY_TARGET_OBJECT, target_serial,
		PW_KEY_NODE_NAME, "qdistro-mm-remote-source-helper",
		NULL);
	state->stream = pw_stream_new(
		state->pw_core, "qdistro-mm-remote-source-helper", properties);
	if (!state->stream) {
		pw_thread_loop_unlock(state->pw_loop);
		return -1;
	}
	pw_stream_add_listener(
		state->stream, &state->stream_listener, &stream_events, state);
	uint8_t pod_buffer[1024];
	struct spa_pod_builder builder =
		SPA_POD_BUILDER_INIT(pod_buffer, sizeof pod_buffer);
	const struct spa_pod *params[1];
	params[0] = spa_pod_builder_add_object(
		&builder,
		SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
		SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
		SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
		SPA_FORMAT_VIDEO_format, SPA_POD_Id(SPA_VIDEO_FORMAT_BGRx),
		SPA_FORMAT_VIDEO_size,
			SPA_POD_CHOICE_RANGE_Rectangle(
				&SPA_RECTANGLE(800, 600),
				&SPA_RECTANGLE(1, 1),
				&SPA_RECTANGLE(8192, 8192)),
		SPA_FORMAT_VIDEO_framerate,
			SPA_POD_Fraction(&SPA_FRACTION(0, 1)));
	int rc = pw_stream_connect(
		state->stream, PW_DIRECTION_INPUT, PW_ID_ANY,
		PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS |
		PW_STREAM_FLAG_INACTIVE, params, 1);
	if (rc >= 0)
		pw_stream_set_active(state->stream, true);
	pw_thread_loop_unlock(state->pw_loop);
	if (rc < 0) {
		errno = EPROTO;
		return -1;
	}
	LOGI("capturing producer pid=%d node=%s serial=%s",
	     state->pw_pid, state->pw_target, target_serial);
	return 0;
}

static void
stop_pipewire(struct state *state)
{
	if (!state->pw_loop)
		return;
	pw_thread_loop_stop(state->pw_loop);
	if (state->stream)
		pw_stream_destroy(state->stream);
	if (state->pw_core)
		pw_core_disconnect(state->pw_core);
	if (state->pw_context)
		pw_context_destroy(state->pw_context);
	pw_thread_loop_destroy(state->pw_loop);
	state->pw_loop = NULL;
}

static uint32_t
monotonic_msec(void)
{
	struct timespec now;
	clock_gettime(CLOCK_MONOTONIC, &now);
	return (uint32_t)((uint64_t)now.tv_sec * 1000u + now.tv_nsec / 1000000u);
}

static int
handle_controller(struct state *state)
{
	uint8_t *message = NULL;
	size_t length = 0;
	int rc = recv_controller_message(state, &message, &length);
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
			break;
		}
		pthread_mutex_lock(&state->mutex);
		state->connected = 1;
		state->media_seq = 0;
		if (send_announcement_locked(state) < 0)
			rc = -1;
		if (rc >= 0 && state->last_pixels) {
			state->frame_pending = 1;
			pthread_cond_signal(&state->frame_ready);
		}
		pthread_mutex_unlock(&state->mutex);
		break;
	case QDMM_LOCAL_DETACHED:
		if (!local.seq || local.payload_len) {
			rc = -1;
			break;
		}
		pthread_mutex_lock(&state->mutex);
		state->connected = 0;
		state->media_seq = 0;
		state->frame_pending = 0;
		pthread_mutex_unlock(&state->mutex);
		break;
	case QDMM_LOCAL_MEDIA_ACK:
		if (!local.seq || local.payload_len)
			rc = -1;
		break;
	case QDMM_LOCAL_MOTION:
	case QDMM_LOCAL_BUTTON:
	case QDMM_LOCAL_KEY:
	case QDMM_LOCAL_AXIS:
	case QDMM_LOCAL_FOCUS: {
		uint8_t qdni[QDMM_MAX_QDNI_PACKET_SIZE];
		size_t qdni_len = 0;
		if (qdmm_local_to_qdni(
		    message, length, monotonic_msec(), qdni, &qdni_len) < 0 ||
		    send_exact_unlocked(state->input_fd, qdni, qdni_len) < 0)
			rc = -1;
		break;
	}
	case QDMM_LOCAL_CLOSE_REQUEST:
		if (local.seq != state->config.source_revision ||
		    local.payload_len || send_exact_unlocked(
		    state->close_fd, "C", 1) < 0)
			rc = -1;
		break;
	default:
		rc = -1;
		break;
	}
	free(message);
	return rc < 0 ? -1 : 1;
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
on_signal(int number)
{
	(void)number;
	g_state.stop = 1;
}

static void
cleanup(struct state *state)
{
	stop_pipewire(state);
	pthread_mutex_lock(&state->mutex);
	state->stop = 1;
	pthread_cond_broadcast(&state->frame_ready);
	pthread_mutex_unlock(&state->mutex);
	if (state->frame_thread_started)
		pthread_join(state->frame_thread, NULL);
	if (state->input_fd >= 0)
		close(state->input_fd);
	if (state->controller_fd >= 0)
		close(state->controller_fd);
	if (state->config_fd >= 0)
		close(state->config_fd);
	if (state->close_fd >= 0)
		close(state->close_fd);
	free(state->config_bytes);
	free(state->pw_node);
	free(state->pw_target);
	free(state->input_sink);
	free(state->last_pixels);
	pthread_cond_destroy(&state->frame_ready);
	pthread_mutex_destroy(&state->send_mutex);
	pthread_mutex_destroy(&state->mutex);
}

int
main(int argc, char **argv)
{
	if (argc != 7 || strcmp(argv[1], "--controller-fd") != 0 ||
	    strcmp(argv[3], "--config-fd") != 0 ||
	    strcmp(argv[5], "--close-fd") != 0 ||
	    (g_state.controller_fd = parse_fd(argv[2])) < 0 ||
	    (g_state.config_fd = parse_fd(argv[4])) < 0 ||
	    (g_state.close_fd = parse_fd(argv[6])) < 0) {
		fprintf(stderr, "usage: %s --controller-fd FD --config-fd FD "
			"--close-fd FD\n", argv[0]);
		return 2;
	}
	pthread_mutex_init(&g_state.mutex, NULL);
	pthread_mutex_init(&g_state.send_mutex, NULL);
	pthread_cond_init(&g_state.frame_ready, NULL);
	if (!same_uid_peer(g_state.controller_fd) ||
	    !same_uid_peer(g_state.close_fd) || read_sealed_config(&g_state) < 0 ||
	    parse_pw_identity(&g_state) < 0 || connect_input_sink(&g_state) < 0) {
		LOGE("trusted local startup failed: %s", strerror(errno));
		cleanup(&g_state);
		return 3;
	}
	close(g_state.config_fd);
	g_state.config_fd = -1;
	struct timeval timeout = { .tv_sec = 5 };
	setsockopt(g_state.controller_fd, SOL_SOCKET, SO_RCVTIMEO,
		   &timeout, sizeof timeout);
	setsockopt(g_state.controller_fd, SOL_SOCKET, SO_SNDTIMEO,
		   &timeout, sizeof timeout);
	pw_init(&argc, &argv);
	struct sigaction action = { .sa_handler = on_signal };
	sigemptyset(&action.sa_mask);
	sigaction(SIGTERM, &action, NULL);
	sigaction(SIGINT, &action, NULL);
	signal(SIGPIPE, SIG_IGN);
	if (start_pipewire(&g_state) < 0) {
		LOGE("PipeWire startup failed: %s", strerror(errno));
		cleanup(&g_state);
		return 4;
	}
	if (pthread_create(
	    &g_state.frame_thread, NULL, frame_sender_main, &g_state) != 0) {
		LOGE("frame sender startup failed");
		cleanup(&g_state);
		return 4;
	}
	g_state.frame_thread_started = 1;

	int result = 0;
	while (!g_state.stop) {
		struct pollfd fds[2] = {
			{ .fd = g_state.controller_fd, .events = POLLIN },
			{ .fd = g_state.input_fd, .events = POLLIN },
		};
		int rc = poll(fds, 2, 1000);
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
		if (fds[1].revents & (POLLHUP | POLLERR)) {
			/* The source qdwin owns this socket. Its close is the local
			 * authoritative toplevel-lifetime signal. */
			send_controller_message(
				&g_state, QDMM_LOCAL_SOURCE_CLOSED,
				g_state.config.source_revision + 1);
			break;
		}
		if (fds[1].revents & POLLIN) {
			/* The sink is write-only by contract. Any reverse bytes are a
			 * confused or hostile local peer. */
			result = 7;
			break;
		}
	}
	cleanup(&g_state);
	return result;
}
