/*
 * qdistro-forward — per-view RDP server.
 *
 * §6.5 S3c. Reads frames from a named PipeWire Node (the per-view
 * pipewire output qdwin allocated for one subscribed toplevel), serves
 * them as a single-monitor RDP session via libfreerdp-shadow3 on a
 * port assigned by qdwin. Spawned by qdwin's
 * qdwin_view_stream_spawn_forward; argv is qdwin-controlled.
 *
 * Architecture: see the architecture doc.
 * the per-view RDP design notes.
 *
 *     qdwin (libweston-14)  --pipewire-output-->  pw_stream INPUT
 *                                                       |
 *                                                       v
 *                                        on_process: dequeue buffer
 *                                                       |
 *                                                       v
 *                                       memcpy → rdpShadowSurface->data
 *                                       region16_union → frame_update
 *                                                       |
 *                                                       v
 *                                          libfreerdp-shadow encoder
 *                                                       |
 *                                                       v
 *                                           sdl-freerdp peer (port N)
 *
 * Input pfns (mouse/keyboard) are stubbed to log only — S5 will route
 * them back to qdwin via qdwin_stream_input_v1 over a wayland client.
 */

#include <errno.h>
#include <getopt.h>
#include <limits.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/utils/result.h>

#include <wayland-client.h>
#include "qdwin-shell-v1-client-protocol.h"

#include <freerdp/freerdp.h>
#include <freerdp/server/shadow.h>
#include <freerdp/codec/region.h>
#include <freerdp/codec/color.h>
#include <winpr/collections.h>
#include <freerdp/log.h>
#include <freerdp/settings.h>

#define TAG "qdistro-forward"
#define LOGI(fmt, ...) fprintf(stderr, "[qfwd %d] " fmt "\n", (int)getpid(), ##__VA_ARGS__)
#define LOGE(fmt, ...) fprintf(stderr, "[qfwd %d ERR] " fmt "\n", (int)getpid(), ##__VA_ARGS__)
#define QFWD_MAX_TOKEN_LEN 4096
#define QFWD_MAX_PASSWORD_LEN 4096

/* QDISTRO_FORWARD_DEBUG=1 enables the dev-only frame dumps + per-frame log spam
 * (impl-24): they do disk I/O / hold the surface lock and have no place in the
 * steady-state frame producer. Read once in main(). */
static int g_debug;

static uint32_t now_ms(void);   /* CLOCK_MONOTONIC ms; defined below */

/* ---------- argv ---------- */

struct qfwd_args {
	const char *pipewire_node;
	const char *access_token;
	char *access_token_owned;
	int rdp_port;
	const char *cert_path;
	const char *key_path;
	const char *password;
	char *password_owned;
	int password_seen;
	int width;
	int height;
	const char *wayland_display;  /* defaults to $WAYLAND_DISPLAY */
};

static struct qfwd_args g_args = {
	.width = 640,
	.height = 480,
	.password = "",
	.cert_path = "",
	.key_path = "",
};

static void usage(const char *prog)
{
	fprintf(stderr,
		"Usage: %s --pipewire-node NAME "
		"(--access-token TOK | --access-token-fd FD) "
		"--rdp-port N [--rdp-cert-path P] [--rdp-key-path P] "
		"(--rdp-password PW | --rdp-password-fd FD) "
		"[--width W] [--height H]\n",
		prog);
}

static int parse_int_range(const char *s, int min, int max, const char *name, int *out)
{
	char *end = NULL;
	long v;

	if (!s || !*s) {
		LOGE("invalid %s: empty", name);
		return -1;
	}
	errno = 0;
	v = strtol(s, &end, 10);
	if (errno || end == s || *end || v < min || v > max) {
		LOGE("invalid %s: %s", name, s);
		return -1;
	}
	*out = (int)v;
	return 0;
}

static int read_password_fd(const char *fd_arg)
{
	int fd = -1;
	char *buf = NULL;
	size_t len = 0;

	if (parse_int_range(fd_arg, 0, INT_MAX, "rdp-password-fd", &fd) < 0)
		return -1;

	buf = calloc(1, QFWD_MAX_PASSWORD_LEN + 1);
	if (!buf) {
		LOGE("password-fd: allocation failed");
		return -1;
	}

	for (;;) {
		ssize_t n;
		if (len == QFWD_MAX_PASSWORD_LEN) {
			LOGE("password-fd: password too long");
			free(buf);
			return -1;
		}
		n = read(fd, buf + len, QFWD_MAX_PASSWORD_LEN - len);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			LOGE("password-fd: read failed: %m");
			free(buf);
			return -1;
		}
		if (n == 0)
			break;
		len += (size_t)n;
	}

	while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r'))
		buf[--len] = '\0';

	g_args.password_owned = buf;
	g_args.password = buf;
	return 0;
}

static int read_access_token_fd(const char *fd_arg)
{
	int fd = -1;
	char *buf = NULL;
	size_t len = 0;

	if (parse_int_range(fd_arg, 0, INT_MAX, "access-token-fd", &fd) < 0)
		return -1;

	buf = calloc(1, QFWD_MAX_TOKEN_LEN + 1);
	if (!buf) {
		LOGE("access-token-fd: allocation failed");
		return -1;
	}

	for (;;) {
		ssize_t n;
		if (len == QFWD_MAX_TOKEN_LEN) {
			LOGE("access-token-fd: token too long");
			free(buf);
			return -1;
		}
		n = read(fd, buf + len, QFWD_MAX_TOKEN_LEN - len);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			LOGE("access-token-fd: read failed: %m");
			free(buf);
			return -1;
		}
		if (n == 0)
			break;
		len += (size_t)n;
	}

	while (len > 0 && (buf[len - 1] == '\n' || buf[len - 1] == '\r'))
		buf[--len] = '\0';

	g_args.access_token_owned = buf;
	g_args.access_token = buf;
	return 0;
}

static void clear_owned_password(void)
{
	if (!g_args.password_owned)
		return;

	volatile char *p = g_args.password_owned;
	size_t n = strlen(g_args.password_owned);
	while (n--)
		*p++ = '\0';
	free(g_args.password_owned);
	g_args.password_owned = NULL;
	g_args.password = "";
}

static void clear_owned_access_token(void)
{
	if (!g_args.access_token_owned)
		return;

	volatile char *p = g_args.access_token_owned;
	size_t n = strlen(g_args.access_token_owned);
	while (n--)
		*p++ = '\0';
	free(g_args.access_token_owned);
	g_args.access_token_owned = NULL;
	g_args.access_token = NULL;
}

static int parse_args(int argc, char **argv)
{
	static const struct option opts[] = {
		{ "pipewire-node",  required_argument, NULL, 'n' },
		{ "access-token",   required_argument, NULL, 't' },
		{ "access-token-fd",required_argument, NULL, 'T' },
		{ "rdp-port",       required_argument, NULL, 'p' },
		{ "rdp-cert-path",  required_argument, NULL, 'c' },
		{ "rdp-key-path",   required_argument, NULL, 'k' },
		{ "rdp-password",   required_argument, NULL, 'P' },
		{ "rdp-password-fd",required_argument, NULL, 'F' },
		{ "width",          required_argument, NULL, 'w' },
		{ "height",         required_argument, NULL, 'h' },
		{ "log-path",       required_argument, NULL, 'L' },
		{ "ready-marker",   required_argument, NULL, 'R' },
		{ "wayland-display",required_argument, NULL, 'W' },
		{ NULL, 0, NULL, 0 }
	};
	int c;
	while ((c = getopt_long(argc, argv, "", opts, NULL)) != -1) {
		switch (c) {
		case 'n': g_args.pipewire_node = optarg; break;
		case 't':
			if (g_args.access_token_owned) {
				LOGE("use only one of --access-token and --access-token-fd");
				return -1;
			}
			g_args.access_token = optarg;
			break;
		case 'T':
			if (g_args.access_token) {
				LOGE("use only one of --access-token and --access-token-fd");
				return -1;
			}
			if (read_access_token_fd(optarg) < 0)
				return -1;
			break;
		case 'p':
			if (parse_int_range(optarg, 1, 65535, "rdp-port", &g_args.rdp_port) < 0)
				return -1;
			break;
		case 'c': g_args.cert_path = optarg; break;
		case 'k': g_args.key_path = optarg; break;
		case 'P':
			if (g_args.password_seen) {
				LOGE("use only one of --rdp-password and --rdp-password-fd");
				return -1;
			}
			g_args.password_seen = 1;
			g_args.password = optarg;
			break;
		case 'F':
			if (g_args.password_seen) {
				LOGE("use only one of --rdp-password and --rdp-password-fd");
				return -1;
			}
			g_args.password_seen = 1;
			if (read_password_fd(optarg) < 0)
				return -1;
			break;
		case 'w':
			if (parse_int_range(optarg, 1, 4096, "width", &g_args.width) < 0)
				return -1;
			break;
		case 'h':
			if (parse_int_range(optarg, 1, 4096, "height", &g_args.height) < 0)
				return -1;
			break;
		case 'L': /* honored by python scaffold; ignored here, stderr is the log */ break;
		case 'R': /* same */ break;
		case 'W': g_args.wayland_display = optarg; break;
		default: return -1;
		}
	}
	if (!g_args.wayland_display)
		g_args.wayland_display = getenv("WAYLAND_DISPLAY");
	/* qdwin spawns us as a fork of itself; the parent's environ may
	 * not have WAYLAND_DISPLAY set (weston doesn't export it for its
	 * own children unless via wet's launcher). Default to weston's
	 * usual socket name. */
	if (!g_args.wayland_display || !*g_args.wayland_display)
		g_args.wayland_display = "wayland-1";
	if (!g_args.pipewire_node || !g_args.access_token || g_args.rdp_port <= 0) {
		usage(argv[0]);
		return -1;
	}
	if (!g_args.password || !g_args.password[0]) {
		LOGE("rdp password must be non-empty");
		usage(argv[0]);
		return -1;
	}
	if (g_args.width <= 0 || g_args.height <= 0 ||
	    g_args.width > 4096 || g_args.height > 4096) {
		LOGE("invalid dimensions %dx%d", g_args.width, g_args.height);
		return -1;
	}
	return 0;
}

/* ---------- subsystem ---------- */

typedef struct qfwd_subsystem {
	rdpShadowSubsystem base;

	/* PipeWire bits, owned here, started in subsystem_start. */
	struct pw_thread_loop *loop;
	struct pw_context *context;
	struct pw_core *core;
	struct pw_stream *stream;
	struct spa_hook stream_listener;

	/* Negotiated stream format. */
	struct spa_video_info_raw format;

	/* impl-24 solidity: negotiation/frame watchdog + diagnostics. Timestamps
	 * are CLOCK_MONOTONIC ms (0 = not-yet). Written on the pw thread (single
	 * writer per field), read by the main wait loop's watchdog — C11 atomics
	 * (not volatile) so the cross-thread access is well-defined, not a data
	 * race (codex impl-25). `format_known_ms` is published BEFORE `format_known`
	 * so a reader that sees format_known==1 always sees a non-zero ms. */
	_Atomic uint32_t stream_active_ms;    /* pw_stream_set_active(true) */
	_Atomic uint32_t format_known_ms;     /* first SPA_PARAM_Format */
	_Atomic uint32_t first_frame_ms;      /* first valid copied frame */
	_Atomic uint32_t last_frame_ms;       /* most recent valid copied frame */
	_Atomic unsigned long valid_frames;
	_Atomic unsigned long invalid_frames;
	_Atomic int format_known;     /* published (release) AFTER format_known_ms */
	int format_warned;            /* main-loop only: logged no-format error once */
	int frame_warned;             /* main-loop only: logged no-first-frame once */
} qfwd_subsystem;

static rdpShadowServer *g_server;
static qfwd_subsystem *g_subsystem;
static volatile sig_atomic_t g_stop_flag;

/* ---------- PipeWire ---------- */

static void qfwd_dump_surface_ppm(rdpShadowSurface *surface);
static volatile sig_atomic_t g_dump_request;

static void on_stream_state_changed(void *data, enum pw_stream_state old,
				    enum pw_stream_state state, const char *error)
{
	qfwd_subsystem *s = data;
	(void)old;
	if (state == PW_STREAM_STATE_ERROR) {
		/* impl-24: was silent at INFO — a stream error meant a black RDP
		 * view with no signal. Surface it loudly with the context an
		 * operator needs (the #1 cause is a --width/--height vs weston
		 * [pipewire] output-mode mismatch). NOTE: not fatal here — qdwin
		 * does not yet watch forward death, so a self-exit would leave a
		 * half-dead stream (impl-24 Q2); bounded recovery/exit is a
		 * follow-on once qdwin's forward-death watch lands. */
		LOGE("pw stream ERROR: %s (target node=%s requested=BGRx %dx%d @0/1; "
		     "format_known=%d valid_frames=%lu) — check weston [pipewire] "
		     "output mode vs --width/--height",
		     error ? error : "(no detail)", g_args.pipewire_node,
		     g_args.width, g_args.height,
		     s ? atomic_load(&s->format_known) : -1,
		     s ? atomic_load(&s->valid_frames) : 0);
		return;
	}
	LOGI("pw stream state: %s%s%s", pw_stream_state_as_string(state),
	     error ? " err=" : "", error ? error : "");
}

static void on_stream_param_changed(void *data, uint32_t id, const struct spa_pod *param)
{
	qfwd_subsystem *s = data;
	if (id != SPA_PARAM_Format || !param)
		return;

	struct spa_video_info info;
	memset(&info, 0, sizeof(info));
	if (spa_format_parse(param, &info.media_type, &info.media_subtype) < 0)
		return;
	if (info.media_type != SPA_MEDIA_TYPE_video ||
	    info.media_subtype != SPA_MEDIA_SUBTYPE_raw)
		return;
	if (spa_format_video_raw_parse(param, &info.info.raw) < 0)
		return;

	s->format = info.info.raw;
	/* publish the timestamp BEFORE format_known so the watchdog can never see
	 * format_known==1 with format_known_ms==0 (which would false-fire the
	 * no-first-frame check via `now - 0`) — codex impl-25. */
	if (!atomic_load(&s->format_known_ms))
		atomic_store(&s->format_known_ms, now_ms());
	atomic_store(&s->format_known, 1);
	LOGI("pw format: %dx%d fmt=%d (BGRA=%d RGBA=%d BGRx=%d)",
	     s->format.size.width, s->format.size.height, s->format.format,
	     SPA_VIDEO_FORMAT_BGRA, SPA_VIDEO_FORMAT_RGBA, SPA_VIDEO_FORMAT_BGRx);
}

/* Convert SPA video format to FreeRDP color format. Shadow surfaces
 * are typically PIXEL_FORMAT_BGRX32 / BGRA32 on x86. PipeWire from
 * weston's backend-pipewire produces BGRA / BGRx in practice. */
static UINT32 spa_to_freerdp_format(uint32_t spa)
{
	switch (spa) {
	case SPA_VIDEO_FORMAT_BGRA: return PIXEL_FORMAT_BGRA32;
	case SPA_VIDEO_FORMAT_BGRx: return PIXEL_FORMAT_BGRX32;
	case SPA_VIDEO_FORMAT_RGBA: return PIXEL_FORMAT_RGBA32;
	case SPA_VIDEO_FORMAT_RGBx: return PIXEL_FORMAT_RGBX32;
	case SPA_VIDEO_FORMAT_xRGB: return PIXEL_FORMAT_XRGB32;
	case SPA_VIDEO_FORMAT_xBGR: return PIXEL_FORMAT_XBGR32;
	default: return 0;
	}
}

struct qfwd_frame_view {
	const uint8_t *src;
	uint32_t stride;
	uint32_t size;
	uint32_t offset;
};

static int qfwd_validate_frame(struct spa_buffer *buf,
			       const struct spa_video_info_raw *fmt,
			       uint32_t copy_w, uint32_t copy_h,
			       struct qfwd_frame_view *out)
{
	struct spa_data *d;
	const struct spa_chunk *chunk;
	int32_t chunk_stride;
	uint32_t stride;
	uint32_t size;
	uint32_t offset;
	uint64_t row_bytes;
	uint64_t required;
	uint64_t available;

	if (!buf || buf->n_datas == 0)
		return -1;

	d = &buf->datas[0];
	chunk = d->chunk;
	if (!d->data || !chunk)
		return -1;
	if (d->maxsize == 0)
		return -1;
	if (fmt->size.width == 0 || fmt->size.height == 0 ||
	    fmt->size.width > 16384 || fmt->size.height > 16384)
		return -1;

	chunk_stride = chunk->stride;
	if (chunk_stride == 0)
		chunk_stride = (int32_t)fmt->size.width * 4;
	if (chunk_stride <= 0)
		return -1;

	stride = (uint32_t)chunk_stride;
	offset = chunk->offset;
	size = chunk->size;
	if (offset > d->maxsize)
		return -1;

	available = (uint64_t)d->maxsize - offset;
	if (size == 0) {
		uint64_t full_size = (uint64_t)stride * fmt->size.height;
		if (full_size > UINT32_MAX)
			return -1;
		size = (uint32_t)full_size;
	}
	if ((uint64_t)size > available)
		return -1;

	row_bytes = (uint64_t)copy_w * 4;
	if (copy_w == 0 || copy_h == 0 || row_bytes > stride)
		return -1;
	required = (uint64_t)(copy_h - 1) * stride + row_bytes;
	if (required > size)
		return -1;

	out->src = (const uint8_t *)d->data + offset;
	out->stride = stride;
	out->size = size;
	out->offset = offset;
	return 0;
}

static void on_stream_process(void *data)
{
	qfwd_subsystem *s = data;
	static unsigned long frame_count = 0;
	frame_count++;
	if (g_debug && (frame_count % 60) == 1)
		LOGI("on_stream_process tick frame=%lu valid=%lu invalid=%lu",
		     frame_count, atomic_load(&s->valid_frames),
		     atomic_load(&s->invalid_frames));
	struct pw_buffer *b = pw_stream_dequeue_buffer(s->stream);
	if (!b)
		return;

	if (!atomic_load_explicit(&s->format_known, memory_order_relaxed)) {
		pw_stream_queue_buffer(s->stream, b);
		return;
	}

	struct spa_buffer *buf = b->buffer;
	if (!buf) {
		pw_stream_queue_buffer(s->stream, b);
		return;
	}

	rdpShadowServer *server = s->base.server;
	if (!server) {
		pw_stream_queue_buffer(s->stream, b);
		return;
	}
	rdpShadowSurface *surface = server->surface;
	if (!surface || !surface->data) {
		pw_stream_queue_buffer(s->stream, b);
		return;
	}

	const UINT32 src_format = spa_to_freerdp_format(s->format.format);
	const uint32_t fw = (s->format.size.width  < surface->width)
		? s->format.size.width  : surface->width;
	const uint32_t fh = (s->format.size.height < surface->height)
		? s->format.size.height : surface->height;
	struct qfwd_frame_view frame;
	if (qfwd_validate_frame(buf, &s->format, fw, fh, &frame) < 0) {
		unsigned long n = atomic_fetch_add(&s->invalid_frames, 1) + 1;
		/* rate-limit the drop log so a persistently-bad source can't spam */
		if (g_debug || (n & (n - 1)) == 0)
			LOGE("dropping invalid PipeWire frame (#%lu)", n);
		pw_stream_queue_buffer(s->stream, b);
		return;
	}
	const uint8_t *src = frame.src;
	uint32_t src_stride = frame.stride;
	if (g_debug && frame_count <= 3 && frame.size >= 8) {
		uint32_t flags = buf->datas[0].chunk ? buf->datas[0].chunk->flags : 0;
		LOGI("buf: type=%u flags=0x%x size=%u stride=%u offset=%u "
		     "first8=%02x%02x%02x%02x%02x%02x%02x%02x",
		     buf->datas[0].type, flags, frame.size, frame.stride, frame.offset,
		     src[0], src[1], src[2], src[3],
		     src[4], src[5], src[6], src[7]);
	}

	EnterCriticalSection(&surface->lock);

	if (src_format != 0) {
		freerdp_image_copy_no_overlap(
			surface->data, surface->format, surface->scanline,
			0, 0, fw, fh,
			src, src_format, src_stride,
			0, 0, NULL, FREERDP_FLIP_NONE);
	} else {
		/* Unknown format: best-effort raw row copy within visible width. */
		uint32_t copy_stride = fw * 4;
		if (copy_stride > surface->scanline)
			copy_stride = surface->scanline;
		for (uint32_t y = 0; y < fh; y++) {
			memcpy(surface->data + y * surface->scanline,
			       src + y * src_stride,
			       copy_stride);
		}
	}

	RECTANGLE_16 dirty = { .left = 0, .top = 0,
			       .right = (UINT16)fw, .bottom = (UINT16)fh };
	(void)region16_union_rect(&surface->invalidRegion,
				  &surface->invalidRegion, &dirty);

	LeaveCriticalSection(&surface->lock);

	/* impl-24: frame liveness bookkeeping (read by the watchdog). Single
	 * writer (this pw thread), atomic for the cross-thread read. */
	uint32_t fnow = now_ms();
	atomic_fetch_add(&s->valid_frames, 1);
	atomic_store(&s->last_frame_ms, fnow);
	if (!atomic_load(&s->first_frame_ms))
		atomic_store(&s->first_frame_ms, fnow);

	pw_stream_queue_buffer(s->stream, b);

	/* Auto-dump frames 1, 5, 30, 60 — these give a snapshot of:
	 *   1  initial state (may be pre-pin empty frame from weston's
	 *      pipewire backend),
	 *   5  short-term post-pin state,
	 *   30 mid-term (~1s in if continuous), and
	 *   60 long-term steady state.
	 * Each rewrites /tmp/qfwd-dump.ppm so the LAST dump in the window
	 * wins — the smoke test reads the file after a few seconds and
	 * gets the freshest frame the producer happened to deliver.
	 * SIGUSR1 forces an unscheduled dump on the next frame. */
	if (g_dump_request ||
	    (g_debug && (frame_count == 1 || frame_count == 5 ||
			 frame_count == 30 || frame_count == 60))) {
		g_dump_request = 0;
		qfwd_dump_surface_ppm(surface);
	}

	shadow_subsystem_frame_update(&s->base);
}

static const struct pw_stream_events stream_events = {
	PW_VERSION_STREAM_EVENTS,
	.state_changed = on_stream_state_changed,
	.param_changed = on_stream_param_changed,
	.process = on_stream_process,
};

/* ---------- subsystem entry points ---------- */

static UINT32 qfwd_enum_monitors(MONITOR_DEF *monitors, UINT32 maxMonitors)
{
	if (maxMonitors < 1)
		return 0;
	monitors[0].left = 0;
	monitors[0].top = 0;
	monitors[0].right = g_args.width - 1;
	monitors[0].bottom = g_args.height - 1;
	monitors[0].flags = 1; /* primary */
	return 1;
}

static int qfwd_subsystem_init(rdpShadowSubsystem *base)
{
	qfwd_subsystem *s = (qfwd_subsystem *)base;
	s->base.numMonitors = qfwd_enum_monitors(s->base.monitors, 16);
	s->base.virtualScreen = s->base.monitors[0];
	s->base.captureFrameRate = 30;
	return 1;
}

static int qfwd_subsystem_uninit(rdpShadowSubsystem *base)
{
	(void)base;
	return 1;
}

static int qfwd_subsystem_start(rdpShadowSubsystem *base)
{
	qfwd_subsystem *s = (qfwd_subsystem *)base;

	s->loop = pw_thread_loop_new("qfwd-pw", NULL);
	if (!s->loop) {
		LOGE("pw_thread_loop_new failed");
		return -1;
	}
	s->context = pw_context_new(pw_thread_loop_get_loop(s->loop), NULL, 0);
	if (!s->context) {
		LOGE("pw_context_new failed");
		return -1;
	}

	if (pw_thread_loop_start(s->loop) < 0) {
		LOGE("pw_thread_loop_start failed");
		return -1;
	}

	pw_thread_loop_lock(s->loop);

	s->core = pw_context_connect(s->context, NULL, 0);
	if (!s->core) {
		LOGE("pw_context_connect failed: %m");
		pw_thread_loop_unlock(s->loop);
		return -1;
	}

	struct pw_properties *props = pw_properties_new(
		PW_KEY_MEDIA_TYPE,     "Video",
		PW_KEY_MEDIA_CATEGORY, "Capture",
		PW_KEY_MEDIA_ROLE,     "Screen",
		PW_KEY_TARGET_OBJECT,  g_args.pipewire_node,
		PW_KEY_NODE_NAME,      "qdistro-forward",
		NULL);

	s->stream = pw_stream_new(s->core, "qdistro-forward", props);
	if (!s->stream) {
		LOGE("pw_stream_new failed");
		pw_thread_loop_unlock(s->loop);
		return -1;
	}

	pw_stream_add_listener(s->stream, &s->stream_listener,
			       &stream_events, s);

	/* weston's backend-pipewire (libweston/backend-pipewire/pipewire.c)
	 * offers BGRx (DRM_FORMAT_XRGB8888) at the fixed pipewire-output
	 * size with framerate 0/1 (driver-pulled). Match exactly — broader
	 * EnumFormat (CHOICE_ENUM/CHOICE_RANGE) didn't intersect cleanly
	 * and PW reported "no more input formats". Argv-supplied --width/
	 * --height MUST match the [pipewire] output's mode in weston.ini
	 * (default 640x480). */
	uint8_t pod_buf[1024];
	struct spa_pod_builder b = SPA_POD_BUILDER_INIT(pod_buf, sizeof(pod_buf));
	const struct spa_pod *params[1];
	params[0] = spa_pod_builder_add_object(
		&b,
		SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
		SPA_FORMAT_mediaType,    SPA_POD_Id(SPA_MEDIA_TYPE_video),
		SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
		SPA_FORMAT_VIDEO_format, SPA_POD_Id(SPA_VIDEO_FORMAT_BGRx),
		SPA_FORMAT_VIDEO_size,
			SPA_POD_Rectangle(&SPA_RECTANGLE((uint32_t)g_args.width,
							 (uint32_t)g_args.height)),
		SPA_FORMAT_VIDEO_framerate,
			SPA_POD_Fraction(&SPA_FRACTION(0, 1)));

	int rc = pw_stream_connect(
		s->stream, PW_DIRECTION_INPUT, PW_ID_ANY,
		PW_STREAM_FLAG_AUTOCONNECT |
		PW_STREAM_FLAG_MAP_BUFFERS |
		PW_STREAM_FLAG_INACTIVE,
		params, 1);
	if (rc < 0) {
		LOGE("pw_stream_connect failed: %s", spa_strerror(rc));
		pw_thread_loop_unlock(s->loop);
		return -1;
	}

	pw_stream_set_active(s->stream, true);
	atomic_store(&s->stream_active_ms, now_ms()); /* impl-24: watchdog t0 */
	pw_thread_loop_unlock(s->loop);

	LOGI("subsystem started, pw target=%s rdp_port=%d %dx%d",
	     g_args.pipewire_node, g_args.rdp_port,
	     g_args.width, g_args.height);
	return 1;
}

static int qfwd_subsystem_stop(rdpShadowSubsystem *base)
{
	qfwd_subsystem *s = (qfwd_subsystem *)base;
	if (s->loop)
		pw_thread_loop_stop(s->loop);
	if (s->stream) {
		pw_stream_destroy(s->stream);
		s->stream = NULL;
	}
	if (s->core) {
		pw_core_disconnect(s->core);
		s->core = NULL;
	}
	if (s->context) {
		pw_context_destroy(s->context);
		s->context = NULL;
	}
	if (s->loop) {
		pw_thread_loop_destroy(s->loop);
		s->loop = NULL;
	}
	return 1;
}

static void qfwd_subsystem_free(rdpShadowSubsystem *base)
{
	qfwd_subsystem *s = (qfwd_subsystem *)base;
	if (!s)
		return;
	(void)qfwd_subsystem_stop(base);
	free(s);
	g_subsystem = NULL;
}

static rdpShadowSubsystem *qfwd_subsystem_new(void)
{
	qfwd_subsystem *s = calloc(1, sizeof(*s));
	if (!s)
		return NULL;
	s->base.SynchronizeEvent       = NULL;
	s->base.KeyboardEvent          = NULL;
	s->base.UnicodeKeyboardEvent   = NULL;
	s->base.MouseEvent             = NULL;
	s->base.ExtendedMouseEvent     = NULL;
	g_subsystem = s;
	return &s->base;
}

/* ---------- wayland-client side: bind qdwin_stream_input_v1, claim,
 * inject_*. Runs in a dedicated thread that owns the wl_display. The
 * shadow input pfns push events into the thread via a pthread mutex
 * around marshal+flush — wl_proxy_marshal is safe to call from any
 * thread as long as flush isn't racing with read. We serialize all
 * marshals + flushes under one mutex; the thread's dispatch loop
 * grabs the same mutex around read+dispatch. ---------- */

static struct {
	pthread_t thread;
	pthread_t frame_pulse;
	int       frame_pulse_started;
	pthread_mutex_t mutex;
	pthread_cond_t  cond_ready;
	int started;
	int stop;

	struct wl_display *display;
	struct wl_registry *registry;
	struct qdwin_stream_input_v1 *si;
	struct qdwin_stream_input_handle_v1 *handle;  /* claimed handle */
	int claim_done;       /* 0 pending, 1 success, -1 error */
	uint32_t si_version;  /* bound interface version (2 => request_frame) */
} g_wl;

static void
qfwd_wl_registry_global(void *data, struct wl_registry *r,
			uint32_t name, const char *interface,
			uint32_t version)
{
	(void)data;
	if (strcmp(interface, "qdwin_stream_input_v1") == 0) {
		/* Bind at v2 to pick up request_frame; fall back if the
		 * compositor is older. */
		uint32_t v = version < 2 ? version : 2;
		g_wl.si = wl_registry_bind(
			r, name, &qdwin_stream_input_v1_interface, v);
		g_wl.si_version = v;
		LOGI("wl: bound qdwin_stream_input_v1 @%u v%u", name, v);
	}
}
static void
qfwd_wl_registry_global_remove(void *data, struct wl_registry *r,
			       uint32_t name)
{
	(void)data; (void)r; (void)name;
}
static const struct wl_registry_listener qfwd_wl_registry_listener = {
	.global        = qfwd_wl_registry_global,
	.global_remove = qfwd_wl_registry_global_remove,
};

/* The qdwin_stream_input_handle_v1 interface has no events of its own,
 * but wl_proxy_add_listener requires SOME listener if we ever expand.
 * Skip for now. */

/* §6.5 S3c iter3: periodic request_frame pulse. Marshals under the
 * shared wl-thread mutex so it doesn't race with inject_* or the
 * dispatch loop's flush. 33 ms period = ~30 Hz.
 *
 * Gated on g_server->clients being non-empty: with no connected RDP
 * peer, waking weston 30x/s to repaint a stream nobody reads is
 * pure waste. ArrayList_Count is thread-safe in winpr. */
static void *qfwd_frame_pulse(void *_)
{
	(void)_;
	const struct timespec period = { .tv_sec = 0, .tv_nsec = 33 * 1000000L };
	while (!g_wl.stop) {
		nanosleep(&period, NULL);
		if (g_wl.stop)
			break;

		int peers = 0;
		if (g_server && g_server->clients)
			peers = (int)ArrayList_Count(g_server->clients);
		if (peers <= 0)
			continue;

		pthread_mutex_lock(&g_wl.mutex);
		if (g_wl.handle && g_wl.display) {
			qdwin_stream_input_handle_v1_request_frame(g_wl.handle);
			wl_display_flush(g_wl.display);
		}
		pthread_mutex_unlock(&g_wl.mutex);
	}
	return NULL;
}

static void *qfwd_wl_thread(void *_)
{
	(void)_;
	const char *wd = g_args.wayland_display;
	if (!wd || !*wd) {
		LOGE("wl: no WAYLAND_DISPLAY — input injection disabled");
		pthread_mutex_lock(&g_wl.mutex);
		g_wl.claim_done = -1;
		pthread_cond_broadcast(&g_wl.cond_ready);
		pthread_mutex_unlock(&g_wl.mutex);
		return NULL;
	}

	g_wl.display = wl_display_connect(wd);
	if (!g_wl.display) {
		LOGE("wl: wl_display_connect(%s) failed", wd);
		goto fail;
	}
	LOGI("wl: connected to %s", wd);

	g_wl.registry = wl_display_get_registry(g_wl.display);
	wl_registry_add_listener(g_wl.registry, &qfwd_wl_registry_listener,
				 NULL);
	wl_display_roundtrip(g_wl.display);

	if (!g_wl.si) {
		LOGE("wl: qdwin_stream_input_v1 not advertised by compositor");
		goto fail;
	}

	/* Test hook: skip claim when QDISTRO_FORWARD_NO_CLAIM=1. Leaves
	 * the stream's access_token available for an alternative claimant
	 * (e.g. a pywayland harness exercising inject_* directly). The RDP
	 * pipeline still runs; input is just a no-op in this mode. */
	if (getenv("QDISTRO_FORWARD_NO_CLAIM")) {
		LOGI("wl: QDISTRO_FORWARD_NO_CLAIM set — skipping claim");
		pthread_mutex_lock(&g_wl.mutex);
		g_wl.claim_done = 1;
		pthread_cond_broadcast(&g_wl.cond_ready);
		pthread_mutex_unlock(&g_wl.mutex);
		goto dispatch;
	}

	g_wl.handle = qdwin_stream_input_v1_claim(g_wl.si,
						  g_args.access_token);
	wl_display_flush(g_wl.display);
	wl_display_roundtrip(g_wl.display);

	if (!g_wl.handle) {
		LOGE("wl: claim returned NULL");
		goto fail;
	}
	LOGI("wl: claim sent (token=%.8s…) handle=%p",
	     g_args.access_token, (void *)g_wl.handle);

	pthread_mutex_lock(&g_wl.mutex);
	g_wl.claim_done = 1;
	pthread_cond_broadcast(&g_wl.cond_ready);
	pthread_mutex_unlock(&g_wl.mutex);

	/* §6.5 S3c iter3: if compositor supports v2, start a pulse thread
	 * that calls request_frame at ~30 Hz. weston backend-pipewire
	 * paints only on damage, so a static source view produces exactly
	 * one PipeWire frame (the pre-pin empty paint) — the RDP encoder
	 * needs continuous frames. Pulse fires regardless of whether a
	 * client is connected; cost of an unused repaint is low. */
	if (g_wl.si_version >= 2) {
		if (pthread_create(&g_wl.frame_pulse, NULL,
				   qfwd_frame_pulse, NULL) == 0) {
			g_wl.frame_pulse_started = 1;
			LOGI("wl: frame_pulse thread started (30 Hz)");
		} else {
			LOGE("wl: frame_pulse pthread_create failed");
		}
	} else {
		LOGI("wl: compositor is v%u — no request_frame support",
		     g_wl.si_version);
	}

dispatch:
	/* Dispatch loop. We use prepare/read/dispatch to avoid blocking
	 * indefinitely when the input pfn thread wants the mutex. */
	while (!g_wl.stop) {
		pthread_mutex_lock(&g_wl.mutex);
		while (wl_display_prepare_read(g_wl.display) != 0) {
			wl_display_dispatch_pending(g_wl.display);
		}
		wl_display_flush(g_wl.display);
		pthread_mutex_unlock(&g_wl.mutex);

		struct pollfd pfd = {
			.fd = wl_display_get_fd(g_wl.display),
			.events = POLLIN,
		};
		int rc = poll(&pfd, 1, 200);
		(void)rc;

		pthread_mutex_lock(&g_wl.mutex);
		if (pfd.revents & POLLIN) {
			wl_display_read_events(g_wl.display);
			wl_display_dispatch_pending(g_wl.display);
		} else {
			wl_display_cancel_read(g_wl.display);
		}
		pthread_mutex_unlock(&g_wl.mutex);
	}
	LOGI("wl: thread exiting");
	if (g_wl.frame_pulse_started) {
		/* Pulse thread watches g_wl.stop; already set by now. Join
		 * so it can't run past our handle destroy. */
		pthread_join(g_wl.frame_pulse, NULL);
		g_wl.frame_pulse_started = 0;
	}
	if (g_wl.handle) {
		qdwin_stream_input_handle_v1_destroy(g_wl.handle);
		g_wl.handle = NULL;
	}
	if (g_wl.display) {
		wl_display_flush(g_wl.display);
		wl_display_disconnect(g_wl.display);
		g_wl.display = NULL;
	}
	return NULL;
fail:
	pthread_mutex_lock(&g_wl.mutex);
	g_wl.claim_done = -1;
	pthread_cond_broadcast(&g_wl.cond_ready);
	pthread_mutex_unlock(&g_wl.mutex);
	if (g_wl.display) {
		wl_display_disconnect(g_wl.display);
		g_wl.display = NULL;
	}
	return NULL;
}

static int qfwd_wl_start(void)
{
	pthread_mutex_init(&g_wl.mutex, NULL);
	pthread_cond_init(&g_wl.cond_ready, NULL);
	if (pthread_create(&g_wl.thread, NULL, qfwd_wl_thread, NULL) != 0) {
		LOGE("wl: pthread_create failed");
		return -1;
	}
	g_wl.started = 1;

	/* Wait briefly for claim outcome so subsequent inject_* calls
	 * see g_wl.handle. Don't block forever — if claim fails, input
	 * stays disabled and shadow_server keeps serving frames. */
	struct timespec deadline;
	clock_gettime(CLOCK_REALTIME, &deadline);
	deadline.tv_sec += 3;
	pthread_mutex_lock(&g_wl.mutex);
	while (g_wl.claim_done == 0)
		if (pthread_cond_timedwait(&g_wl.cond_ready, &g_wl.mutex,
					   &deadline) != 0)
			break;
	int outcome = g_wl.claim_done;
	pthread_mutex_unlock(&g_wl.mutex);
	if (outcome != 1) {
		LOGE("wl: claim failed within 3s (outcome=%d) — proceeding without input",
		     outcome);
		return -1;
	}
	return 0;
}

static void qfwd_wl_stop(void)
{
	if (!g_wl.started)
		return;
	g_wl.stop = 1;
	pthread_join(g_wl.thread, NULL);
	pthread_cond_destroy(&g_wl.cond_ready);
	pthread_mutex_destroy(&g_wl.mutex);
}

/* Helpers — call only with g_wl.handle non-NULL. Each grabs the mutex
 * around marshal+flush so no other thread is dispatching on the
 * display at the same time. */
#define WL_INJECT_GUARD()                                       \
	if (!g_wl.handle)                                       \
		return TRUE;                                    \
	pthread_mutex_lock(&g_wl.mutex);

#define WL_INJECT_RELEASE()                                     \
	wl_display_flush(g_wl.display);                         \
	pthread_mutex_unlock(&g_wl.mutex);

/* ---------- input pfns ---------- */

/* RDP wire constants we need (from freerdp/input.h) — keep local to
 * avoid pulling the full header here. */
#ifndef PTR_FLAGS_MOVE
#define PTR_FLAGS_MOVE       0x0800
#define PTR_FLAGS_DOWN       0x8000
#define PTR_FLAGS_BUTTON1    0x1000
#define PTR_FLAGS_BUTTON2    0x2000
#define PTR_FLAGS_BUTTON3    0x4000
#define PTR_FLAGS_WHEEL      0x0200
#define PTR_FLAGS_HWHEEL     0x0400
#define PTR_FLAGS_WHEEL_NEGATIVE  0x0100
#define KBD_FLAGS_RELEASE    0x8000
#define KBD_FLAGS_EXTENDED   0x0100
#endif

/* Linux evdev codes used by inject_*. */
#define BTN_LEFT   0x110
#define BTN_RIGHT  0x111
#define BTN_MIDDLE 0x112

static uint32_t now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint32_t)((uint64_t)ts.tv_sec * 1000u + ts.tv_nsec / 1000000u);
}

/* Translate RDP scan code (PC AT set 1) to Linux evdev keycode. The
 * standard mapping is "scan + 8" for the 0x00..0x7f base set. RDP also
 * uses an extended bit (KBD_FLAGS_EXTENDED) for keys like arrows and
 * RCtrl — for the MVP we ignore the extended bit and rely on the +8
 * offset, which covers the alphanumeric block correctly. Edge cases
 * (numpad, Insert/Delete) can be refined later. */
static uint32_t rdp_scan_to_evdev(UINT16 flags, UINT8 code)
{
	(void)flags;
	return (uint32_t)code + 8;
}

static BOOL qfwd_synchronize(rdpShadowSubsystem *s, rdpShadowClient *c, UINT32 flags)
{
	(void)s; (void)c;
	WL_INJECT_GUARD();
	qdwin_stream_input_handle_v1_inject_modifiers(
		g_wl.handle,
		(uint32_t)flags, 0, 0, 0);
	WL_INJECT_RELEASE();
	return TRUE;
}
static BOOL qfwd_keyboard(rdpShadowSubsystem *s, rdpShadowClient *c,
			  UINT16 flags, UINT8 code)
{
	(void)s; (void)c;
	uint32_t key = rdp_scan_to_evdev(flags, code);
	uint32_t state = (flags & KBD_FLAGS_RELEASE) ? 0 : 1;
	WL_INJECT_GUARD();
	qdwin_stream_input_handle_v1_inject_key(
		g_wl.handle, now_ms(), key, state);
	WL_INJECT_RELEASE();
	return TRUE;
}
static BOOL qfwd_unicode(rdpShadowSubsystem *s, rdpShadowClient *c,
			 UINT16 flags, UINT16 code)
{
	(void)s; (void)c; (void)flags; (void)code;
	/* Unicode keyboard is for clients that send characters rather
	 * than scan codes (rare for sdl-freerdp). Drop for now — terminal
	 * works via the scan-code path. */
	return TRUE;
}
static BOOL qfwd_mouse(rdpShadowSubsystem *s, rdpShadowClient *c,
		       UINT16 flags, UINT16 x, UINT16 y)
{
	(void)s; (void)c;
	WL_INJECT_GUARD();
	uint32_t t = now_ms();
	if (flags & PTR_FLAGS_MOVE) {
		qdwin_stream_input_handle_v1_inject_pointer_motion(
			g_wl.handle, t,
			wl_fixed_from_int((int32_t)x),
			wl_fixed_from_int((int32_t)y));
	}
	uint32_t btn = 0;
	if (flags & PTR_FLAGS_BUTTON1)      btn = BTN_LEFT;
	else if (flags & PTR_FLAGS_BUTTON2) btn = BTN_RIGHT;
	else if (flags & PTR_FLAGS_BUTTON3) btn = BTN_MIDDLE;
	if (btn) {
		uint32_t state = (flags & PTR_FLAGS_DOWN) ? 1 : 0;
		qdwin_stream_input_handle_v1_inject_pointer_button(
			g_wl.handle, t, btn, state);
	}
	if (flags & (PTR_FLAGS_WHEEL | PTR_FLAGS_HWHEEL)) {
		/* RDP wheel value is in the low byte; sign from WHEEL_NEG. */
		int delta = (int)(flags & 0xff);
		if (flags & PTR_FLAGS_WHEEL_NEGATIVE)
			delta = -delta;
		uint32_t axis = (flags & PTR_FLAGS_HWHEEL) ? 1 : 0;
		qdwin_stream_input_handle_v1_inject_pointer_axis(
			g_wl.handle, t, axis, wl_fixed_from_int(delta));
	}
	WL_INJECT_RELEASE();
	return TRUE;
}
static BOOL qfwd_xmouse(rdpShadowSubsystem *s, rdpShadowClient *c,
			UINT16 flags, UINT16 x, UINT16 y)
{
	(void)s; (void)c; (void)flags; (void)x; (void)y;
	/* PTR_XFLAGS_BUTTON1/2 = back/forward; ignore for MVP. */
	return TRUE;
}

/* ---------- authentication ---------- */

static int qfwd_authenticate(rdpShadowSubsystem *base, rdpShadowClient *client,
			     const char *user, const char *domain, const char *pw)
{
	(void)base; (void)client; (void)user; (void)domain;
	if (!pw)
		return -1;
	if (strcmp(pw, g_args.password) != 0) {
		LOGE("auth FAIL: wrong password");
		return -1;
	}
	LOGI("auth OK for user=%s", user ? user : "(anon)");
	return 1;
}

/* ---------- entry ---------- */

static int qfwd_entry(RDP_SHADOW_ENTRY_POINTS *ep)
{
	ep->New          = qfwd_subsystem_new;
	ep->Free         = qfwd_subsystem_free;
	ep->Init         = qfwd_subsystem_init;
	ep->Uninit       = qfwd_subsystem_uninit;
	ep->Start        = qfwd_subsystem_start;
	ep->Stop         = qfwd_subsystem_stop;
	ep->EnumMonitors = qfwd_enum_monitors;
	return 1;
}

/* ---------- signal handling ---------- */

static void on_signal(int sig)
{
	if (sig == SIGUSR1) {
		g_dump_request = 1;
		return;
	}
	g_stop_flag = 1;
}

/* Write surface->data as a PPM (BGRX32 → RGB) to /tmp/qfwd-dump.ppm.
 * Called from the producer thread when SIGUSR1 has been raised — keeps
 * the surface lock held briefly. PPM is chosen for zero deps; convert
 * to PNG with `convert /tmp/qfwd-dump.ppm /tmp/qfwd-dump.png` if you
 * want a more portable format. */
static void qfwd_dump_surface_ppm(rdpShadowSurface *surface)
{
	const char *path = getenv("QDISTRO_FORWARD_DUMP_PATH");
	if (!path || !*path)
		path = "/tmp/qfwd-dump.ppm";

	/* impl-24: snapshot the surface UNDER the lock, then convert + write the
	 * PPM AFTER unlocking — never hold the encoder surface lock across disk I/O
	 * (the old code fprintf'd + fwrote every row while holding it). */
	EnterCriticalSection(&surface->lock);
	uint32_t w = surface->width;
	uint32_t h = surface->height;
	uint32_t s = surface->scanline;
	/* The conversion loop reads w*4 bytes per row from the snapshot and writes
	 * w*3 per row; guard the dimensions (incl. scanline >= w*4 and overflow)
	 * so neither can run past the snapshot, independent of word size (codex
	 * impl-25). */
	int dims_ok = w && h && s >= (uint64_t)w * 4 &&
		      h <= SIZE_MAX / s && w <= SIZE_MAX / 3;
	size_t snap_size = dims_ok ? (size_t)s * h : 0;
	uint8_t *snap = dims_ok ? malloc(snap_size) : NULL;
	if (snap)
		memcpy(snap, surface->data, snap_size);
	LeaveCriticalSection(&surface->lock);
	if (!snap) {
		LOGE("dump: bad dims or alloc failed (%ux%u scanline=%u)", w, h, s);
		return;
	}

	FILE *f = fopen(path, "wb");
	if (!f) {
		LOGE("dump: fopen %s: %m", path);
		free(snap);
		return;
	}
	fprintf(f, "P6\n%u %u\n255\n", w, h);
	uint8_t *row = malloc((size_t)w * 3);
	if (row) {
		for (uint32_t y = 0; y < h; y++) {
			const uint8_t *p = snap + (size_t)y * s;
			for (uint32_t x = 0; x < w; x++) {
				/* shadow surface stored as BGRX (PIXEL_FORMAT_BGRX32);
				 * PPM wants RGB. */
				row[x*3 + 0] = p[x*4 + 2];
				row[x*3 + 1] = p[x*4 + 1];
				row[x*3 + 2] = p[x*4 + 0];
			}
			fwrite(row, 1, (size_t)w * 3, f);
		}
		free(row);
		LOGI("dump: wrote %ux%u to %s", w, h, path);
	} else {
		LOGE("dump: row alloc failed; wrote header only to %s", path);
	}
	free(snap);
	fclose(f);
}

/* ---------- main ---------- */

int main(int argc, char **argv)
{
	/* Defensive: weston's mainloop blocks several signals on signalfd
	 * and the mask survives execve. qdwin already clears it but belt-
	 * and-suspenders so this binary works under any spawn site. */
	sigset_t empty;
	sigemptyset(&empty);
	sigprocmask(SIG_SETMASK, &empty, NULL);

	{
		const char *d = getenv("QDISTRO_FORWARD_DEBUG");
		g_debug = d && *d && strcmp(d, "0") != 0;   /* =0 / empty = off */
	}

	if (parse_args(argc, argv) < 0) {
		clear_owned_password();
		clear_owned_access_token();
		return 2;
	}
	if (!g_args.password || !g_args.password[0]) {
		LOGE("rdp password must be non-empty at startup");
		return 2;
	}

	pw_init(&argc, &argv);

	struct sigaction sa = { .sa_handler = on_signal };
	sigemptyset(&sa.sa_mask);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGINT,  &sa, NULL);
	sigaction(SIGUSR1, &sa, NULL);
	signal(SIGPIPE, SIG_IGN);

	shadow_subsystem_set_entry(qfwd_entry);

	g_server = shadow_server_new();
	if (!g_server) {
		LOGE("shadow_server_new failed");
		return 1;
	}

	/* shadow_server reads server->port directly (not FreeRDP_ServerPort);
	 * default in shadow_server_new is 3389. */
	g_server->port = (DWORD)g_args.rdp_port;

	rdpSettings *st = g_server->settings;
	freerdp_settings_set_uint32(st, FreeRDP_ColorDepth, 32);
	freerdp_settings_set_bool(st, FreeRDP_RdpSecurity, TRUE);
	freerdp_settings_set_bool(st, FreeRDP_TlsSecurity, TRUE);
	freerdp_settings_set_bool(st, FreeRDP_NlaSecurity, FALSE);
	freerdp_settings_set_bool(st, FreeRDP_NSCodec, TRUE);
	freerdp_settings_set_bool(st, FreeRDP_RemoteFxCodec, TRUE);
	freerdp_settings_set_uint32(st, FreeRDP_RemoteFxRlgrMode, RLGR3);
	/* Cert/key live on rdpShadowServer (not in rdpSettings). If unset,
	 * shadow_server_init auto-generates a self-signed cert via makecert.
	 * sdl-freerdp with /cert:ignore connects either way. */
	if (g_args.cert_path && g_args.cert_path[0]) {
		g_server->CertificateFile = strdup(g_args.cert_path);
		if (g_args.key_path && g_args.key_path[0])
			g_server->PrivateKeyFile = strdup(g_args.key_path);
	}

	/* Auth wiring: enable framework's auth path; subsystem->Authenticate
	 * (set after New) compares against g_args.password. */
	g_server->authentication = TRUE;

	if (shadow_server_init(g_server) < 0) {
		LOGE("shadow_server_init failed");
		shadow_server_free(g_server);
		return 1;
	}

	/* Hook the input pfns and Authenticate now that the subsystem
	 * exists (created by shadow_server_init via our entry function). */
	if (g_subsystem) {
		g_subsystem->base.SynchronizeEvent     = qfwd_synchronize;
		g_subsystem->base.KeyboardEvent        = qfwd_keyboard;
		g_subsystem->base.UnicodeKeyboardEvent = qfwd_unicode;
		g_subsystem->base.MouseEvent           = qfwd_mouse;
		g_subsystem->base.ExtendedMouseEvent   = qfwd_xmouse;
		g_subsystem->base.Authenticate         = qfwd_authenticate;
	}

	/* §6.5 S5b: connect to qdwin's wayland display, bind
	 * qdwin_stream_input_v1, claim(access_token). On failure we
	 * still serve frames — just no input injection. */
	qfwd_wl_start();

	if (shadow_server_start(g_server) < 0) {
		LOGE("shadow_server_start failed");
		shadow_server_uninit(g_server);
		shadow_server_free(g_server);
		return 1;
	}

	LOGI("shadow_server running, port=%d, target=%s",
	     g_args.rdp_port, g_args.pipewire_node);

	for (;;) {
		DWORD wr = WaitForSingleObject(g_server->thread, 250);
		if (wr != WAIT_TIMEOUT)
			break;
		if (g_stop_flag) {
			shadow_server_stop(g_server);
			break;
		}
		/* impl-24 watchdog: loud, actionable diagnostics on a stalled
		 * pipeline. NOT fatal (qdwin doesn't watch forward death yet —
		 * a self-exit would orphan the stream; fatal-exit + bounded
		 * reconnect is the follow-on once qdwin's child-death watch lands).
		 * Note weston's pipewire output is damage-driven (framerate 0/1),
		 * so a STATIC source legitimately produces few frames — we only
		 * warn on NO format / NO first frame, never on "frames went quiet."*/
		qfwd_subsystem *s = g_subsystem;
		uint32_t active_at = s ? atomic_load(&s->stream_active_ms) : 0;
		if (active_at) {
			uint32_t now = now_ms();
			int known = atomic_load(&s->format_known);
			uint32_t known_at = atomic_load(&s->format_known_ms);
			uint32_t first_at = atomic_load(&s->first_frame_ms);
			if (!known && !s->format_warned &&
			    (now - active_at) > 3000) {
				s->format_warned = 1;
				LOGE("no PipeWire format negotiated for node %s after 3s "
				     "(requested BGRx %dx%d @0/1) — check the weston "
				     "[pipewire] output mode matches --width/--height; "
				     "serving a black RDP view until a format is agreed",
				     g_args.pipewire_node, g_args.width, g_args.height);
			}
			int peers = (g_server && g_server->clients)
				? (int)ArrayList_Count(g_server->clients) : 0;
			if (known && !first_at && !s->frame_warned &&
			    peers > 0 && (now - known_at) > 5000) {
				s->frame_warned = 1;
				LOGE("PipeWire format negotiated but NO frame after 5s "
				     "with %d RDP peer(s) connected (node=%s, "
				     "invalid_frames=%lu) — source may be stuck or all "
				     "frames are failing validation",
				     peers, g_args.pipewire_node,
				     atomic_load(&s->invalid_frames));
			}
		}
	}
	WaitForSingleObject(g_server->thread, INFINITE);

	qfwd_wl_stop();
	shadow_server_uninit(g_server);
	shadow_server_free(g_server);
	clear_owned_password();
	clear_owned_access_token();
	pw_deinit();
	return 0;
}
