/*
 * §6.8 S2c — qdistro-nested-pixelfeed.
 *
 * Consumer process spawned by qdshell on nested_proxy_pixel_source.
 * Connects to the outer wayland display as a wl_client, binds
 * wl_compositor + wl_shm + qdwin_shell_v1, and creates a wl_surface
 * that the compositor swaps in for the placeholder curtain via
 * qdwin_shell_v1.bind_proxy_pixels(handle, surface).
 *
 * Pixel pipeline:
 *
 *     inner weston (nested-mode publisher)
 *         |
 *         | weston backend-pipewire output (per-toplevel)
 *         v
 *     PipeWire node "weston.<output_name>"
 *         |                                   \
 *         |  pw_stream INPUT (this binary)     \  same pattern as
 *         v                                     \ qdistro-forward.c
 *     on_process: dequeue pw_buffer
 *         |
 *         |  copy → wl_shm buffer slot
 *         v
 *     wl_surface_attach + damage + commit + flush
 *         |
 *         v
 *     outer qdwin: weston_view of this surface (visible)
 *
 * The bind path is identical to S2b mvp: on startup we attach a single
 * solid-colour buffer, commit, and call bind_proxy_pixels. The
 * compositor immediately swaps the curtain for the solid buffer. The
 * PipeWire thread then replaces the buffer on every frame. If PW never
 * delivers (no nested pixels, format mismatch, etc.) the user still
 * sees the solid colour rather than a curtain — the failure mode stays
 * non-fatal.
 *
 * Args:
 *     qdistro-nested-pixelfeed <handle> <pw_node> [input_sink]
 *
 *     pw_node is qdwin's "weston.pipewire:<pid>:<output_name>" — we
 *     parse the last colon segment as the weston output name and
 *     resolve the actual PW node by TARGET_OBJECT="weston.<output>".
 *
 * Env:
 *     WAYLAND_DISPLAY      — outer display socket (default wayland-0)
 *     XDG_RUNTIME_DIR      — standard
 *     QDWIN_PIXELFEED_RGBA — 4 hex bytes (RGBA), default ff0000ff (red)
 *     QDWIN_PIXELFEED_W    — pixel width, default 800
 *     QDWIN_PIXELFEED_H    — pixel height, default 600
 *     QDWIN_PIXELFEED_HOLD — keep running this many seconds after bind
 *                            (default: forever; 0 = exit immediately
 *                            after bind, no PW thread, for tests).
 *     QDWIN_PIXELFEED_NO_PW — skip the PipeWire thread entirely; stay
 *                            on the solid colour. For tests that just
 *                            need bind_proxy_pixels coverage.
 *     QDWIN_PIXELFEED_NO_DMABUF — force the SHM path even when the outer
 *                            advertises zwp_linux_dmabuf_v1. For pinning
 *                            the legacy code path in tests.
 *
 * §6.8 dmabuf zero-copy path:
 *
 *     If the outer compositor advertises zwp_linux_dmabuf_v1 we add
 *     SPA_FORMAT_VIDEO_modifier=LINEAR to the EnumFormat. weston's
 *     backend-pipewire interprets that as "consumer can take dmabuf"
 *     and allocates a dmabuf-backed pw_buffer. on_pw_process inspects
 *     spa_buffer.datas[0].type — if SPA_DATA_DmaBuf we wrap the fd in
 *     a fresh wl_buffer via zwp_linux_dmabuf_v1.create_params.add +
 *     create_immed(W,H,XR24,0), attach + commit. No userspace memcpy.
 *     The wl_buffer release listener queues the pw_buffer back.
 *
 *     If the outer is pixman (no GPU passthrough), zwp_linux_dmabuf_v1
 *     is absent, modifier never enters EnumFormat, weston offers SHM
 *     only, and we fall through to the existing 2-slot wl_shm pool
 *     copy path. End-to-end shm path is non-fatal even when both ends
 *     can do dmabuf — set QDWIN_PIXELFEED_NO_DMABUF=1 to pin it.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>

#include <pipewire/pipewire.h>
#include <spa/param/video/format-utils.h>
#include <spa/utils/result.h>

#include "qdwin-shell-v1-client-protocol.h"
#include "linux-dmabuf-unstable-v1-client-protocol.h"

/* DRM fourccs we may publish — keep local copies so we don't pull in
 * libdrm just for two constants. SPA_VIDEO_FORMAT_BGRx == bytes B,G,R,X
 * which is DRM_FORMAT_XRGB8888; SPA_VIDEO_FORMAT_BGRA == DRM_FORMAT_ARGB8888. */
#define QDPF_DRM_FORMAT_XRGB8888 0x34325258 /* 'XR24' */
#define QDPF_DRM_FORMAT_ARGB8888 0x34325241 /* 'AR24' */
#define QDPF_DRM_MODIFIER_LINEAR 0ULL

#define LOGI(fmt, ...) \
	fprintf(stderr, "[pixelfeed %d] " fmt "\n", (int)getpid(), ##__VA_ARGS__)
#define LOGE(fmt, ...) \
	fprintf(stderr, "[pixelfeed %d ERR] " fmt "\n", (int)getpid(), ##__VA_ARGS__)

struct pf_state {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_compositor *compositor;
	struct wl_shm *shm;
	struct zwp_linux_dmabuf_v1 *dmabuf;
	uint32_t dmabuf_version;
	struct qdwin_shell_v1 *shell;
	uint32_t shell_version;
	struct wl_surface *surface;

	int width, height;       /* surface logical size */
	int dmabuf_active;       /* set once we've negotiated dmabuf with PW */
	int no_dmabuf;           /* env-gated force-shm */

	/* §6.8 S2c: PipeWire side. */
	struct pw_thread_loop *pw_loop;
	struct pw_context *pw_context;
	struct pw_core *pw_core;
	struct pw_stream *stream;
	struct spa_hook stream_listener;
	struct spa_video_info_raw format;
	int format_known;

	/* Double-buffer slot pool, allocated lazily once we know the size.
	 * Each slot owns its own memfd-backed wl_shm region + wl_buffer.
	 * busy=1 while held by the compositor. */
	struct pf_slot {
		struct wl_buffer *wl_buf;
		uint8_t *data;
		size_t size;
		int stride;
		int busy;
		struct pf_state *st;     /* back ptr for release listener */
		int idx;                 /* 0 or 1 */
	} slots[2];
	int slots_inited;

	/* Mutex protects slots[].busy and serialises wl marshals between
	 * the main thread (dispatch loop) and the PW thread (frame copy).
	 * Pattern from daemons/forward/qdistro-forward.c. */
	pthread_mutex_t mutex;

	/* Resolved PW target — derived from argv pw_node. Owned. */
	char *pw_target;
	int   pw_pid;            /* parsed from "weston.pipewire:<pid>:..." */

	volatile sig_atomic_t stop;
};

static struct pf_state g_st;

/* ---------- registry ---------- */

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct pf_state *s = data;
	if (strcmp(interface, wl_compositor_interface.name) == 0) {
		s->compositor = wl_registry_bind(reg, name,
			&wl_compositor_interface, version >= 4 ? 4 : version);
	} else if (strcmp(interface, wl_shm_interface.name) == 0) {
		s->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	} else if (strcmp(interface, qdwin_shell_v1_interface.name) == 0) {
		uint32_t v = version > 9 ? 9 : version;
		s->shell_version = v;
		s->shell = wl_registry_bind(reg, name,
			&qdwin_shell_v1_interface, v);
	} else if (strcmp(interface, zwp_linux_dmabuf_v1_interface.name) == 0) {
		/* v3 is enough for create_immed + per-buffer modifier; v4
		 * adds dmabuf-feedback which we don't yet consume. */
		uint32_t v = version > 3 ? 3 : version;
		s->dmabuf_version = v;
		s->dmabuf = wl_registry_bind(reg, name,
			&zwp_linux_dmabuf_v1_interface, v);
	}
}

static void
on_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{
	(void)data; (void)reg; (void)name;
}

static const struct wl_registry_listener registry_listener = {
	on_global, on_global_remove,
};

/* ---------- shm helpers ---------- */

static int
make_anon_fd(size_t size)
{
	int fd = memfd_create("qdistro-nested-pixelfeed-shm",
			      MFD_CLOEXEC | MFD_ALLOW_SEALING);
	if (fd < 0) return -1;
	if (ftruncate(fd, size) < 0) { close(fd); return -1; }
	return fd;
}

static uint32_t
parse_rgba(const char *s)
{
	uint8_t r = 0xFF, g = 0, b = 0, a = 0xFF;
	if (s && strlen(s) >= 8) {
		unsigned ur, ug, ub, ua;
		if (sscanf(s, "%2x%2x%2x%2x", &ur, &ug, &ub, &ua) == 4) {
			r = ur; g = ug; b = ub; a = ua;
		}
	}
	return ((uint32_t)a << 24) | ((uint32_t)r << 16)
	     | ((uint32_t)g << 8)  | (uint32_t)b;
}

/* ---------- buffer slots ----------
 *
 * Two wl_buffers backed by separate memfds, each WxH ARGB8888. Slot
 * .busy is set when we attach+commit and cleared on the wl_buffer
 * release event. PW thread picks the first non-busy slot, copies
 * into it, attaches, commits.
 *
 * Re-allocate on geometry change (PW format event with a different
 * size). For the MVP we just log + reuse if the format width/height
 * exceeds the slot's allocated size; we copy clipped to slot dims.
 */

static void
on_buffer_release(void *data, struct wl_buffer *buf)
{
	struct pf_slot *slot = data;
	(void)buf;
	pthread_mutex_lock(&slot->st->mutex);
	slot->busy = 0;
	pthread_mutex_unlock(&slot->st->mutex);
}

static const struct wl_buffer_listener buffer_listener = {
	.release = on_buffer_release,
};

/* Allocate one wl_buffer-backed slot at WxH ARGB8888. Returns 0 on
 * success. Must be called with the wl side ready (compositor+shm). */
static int
pf_slot_init(struct pf_state *st, struct pf_slot *slot, int idx,
	     int w, int h)
{
	int stride = w * 4;
	size_t size = (size_t)stride * h;

	int fd = make_anon_fd(size);
	if (fd < 0) {
		LOGE("memfd: %s", strerror(errno));
		return -1;
	}
	void *map = mmap(NULL, size, PROT_READ | PROT_WRITE,
			 MAP_SHARED, fd, 0);
	if (map == MAP_FAILED) {
		LOGE("mmap: %s", strerror(errno));
		close(fd);
		return -1;
	}

	struct wl_shm_pool *pool = wl_shm_create_pool(st->shm, fd, size);
	close(fd);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, w, h, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);

	slot->wl_buf = buf;
	slot->data   = map;
	slot->size   = size;
	slot->stride = stride;
	slot->busy   = 0;
	slot->st     = st;
	slot->idx    = idx;

	wl_buffer_add_listener(buf, &buffer_listener, slot);
	return 0;
}

static void
pf_slot_release(struct pf_slot *slot)
{
	if (slot->wl_buf) {
		wl_buffer_destroy(slot->wl_buf);
		slot->wl_buf = NULL;
	}
	if (slot->data) {
		munmap(slot->data, slot->size);
		slot->data = NULL;
	}
}

/* ---------- argv pw_node parsing ----------
 *
 * qdwin emits pw_node = "weston.pipewire:<pid>:<output_name>" or
 * "weston.pipewire:<pid>:none" when no output was assignable. We
 * resolve actual PW target by:
 *   - last `:` segment is the weston output name (or "none")
 *   - PW node target = "weston.<output_name>"
 *
 * If the format is unrecognised (no colon) we use the whole string
 * verbatim as TARGET_OBJECT. That lets future qdwin formats pass
 * through without a pixelfeed rebuild.
 */

static void
parse_pw_node(const char *arg, struct pf_state *st)
{
	st->pw_pid = 0;
	st->pw_target = NULL;

	if (!arg || !*arg)
		return;

	if (strncmp(arg, "weston.pipewire:", 16) == 0) {
		const char *p = arg + 16;
		const char *colon = strchr(p, ':');
		if (colon) {
			st->pw_pid = atoi(p);
			const char *output = colon + 1;
			if (strcmp(output, "none") != 0) {
				size_t len = 8 + strlen(output) + 1;
				st->pw_target = malloc(len);
				if (st->pw_target)
					snprintf(st->pw_target, len,
						 "weston.%s", output);
			}
			return;
		}
	}

	st->pw_target = strdup(arg);
}

/* ---------- pipewire ---------- */

static void
on_pw_state_changed(void *data, enum pw_stream_state old,
		    enum pw_stream_state state, const char *error)
{
	struct pf_state *st = data;
	LOGI("pw stream state: %s -> %s%s%s",
	     pw_stream_state_as_string(old),
	     pw_stream_state_as_string(state),
	     error ? " err=" : "", error ? error : "");

	/* §6.8 dmabuf flakiness diagnostic (s31 header). The
	 * documented failure mode is "paused→unconnected after format
	 * negotiation," producer accepts modifier but never delivers a
	 * frame. When we hit it we want a single, grep-friendly log line
	 * so the next maintainer doesn't have to cross-reference the
	 * comment in s31-pixelfeed-dmabuf.sh.
	 *
	 * UNCONNECTED is terminal for a pw_stream — calling set_active
	 * on it is a no-op. Real recovery would require destroying the
	 * stream and re-running pf_start_pipewire. Today we just flag
	 * the failure: the SHM fallback continues working and s31's
	 * informational soft-pass covers it. */
	if (state == PW_STREAM_STATE_UNCONNECTED &&
	    old == PW_STREAM_STATE_PAUSED &&
	    st->format_known) {
		LOGE("dmabuf: PW unconnected after PAUSED with format known "
		     "— back-to-back nested weston restart? (s31 doc'd flake)");
	}
}

static void
on_pw_param_changed(void *data, uint32_t id, const struct spa_pod *param)
{
	struct pf_state *st = data;
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

	st->format = info.info.raw;
	st->format_known = 1;

	/* Modifier presence in the negotiated format = producer chose the
	 * dmabuf path. We confirm with per-buffer SPA_DATA_DmaBuf in
	 * on_pw_process before flipping the wire format; this just records
	 * the negotiation outcome for logging. */
	int negotiated_dmabuf = spa_pod_find_prop(
		param, NULL, SPA_FORMAT_VIDEO_modifier) != NULL;

	LOGI("pw format: %dx%d fmt=%d modifier_negotiated=%d "
	     "(BGRA=%d RGBA=%d BGRx=%d)",
	     st->format.size.width, st->format.size.height, st->format.format,
	     negotiated_dmabuf,
	     SPA_VIDEO_FORMAT_BGRA, SPA_VIDEO_FORMAT_RGBA, SPA_VIDEO_FORMAT_BGRx);
}

/* WL_SHM_FORMAT_ARGB8888 little-endian wire layout = bytes B,G,R,A.
 * That matches SPA_VIDEO_FORMAT_BGRA / BGRx directly (BGRx maps to
 * ARGB with the X byte ignored — set alpha to 0xFF for opaque).
 *
 * Returns 1 on success, 0 if the format is unsupported (caller drops
 * the frame). */
static int
copy_pw_to_slot(const uint8_t *src, uint32_t src_stride,
		struct pf_slot *slot, int w, int h, int slot_w, int slot_h,
		uint32_t spa_fmt)
{
	int copy_w = w  < slot_w  ? w  : slot_w;
	int copy_h = h  < slot_h  ? h  : slot_h;

	uint8_t *dst = slot->data;
	int dst_stride = slot->stride;

	switch (spa_fmt) {
	case SPA_VIDEO_FORMAT_BGRA:
		for (int y = 0; y < copy_h; y++) {
			memcpy(dst + (size_t)y * dst_stride,
			       src + (size_t)y * src_stride,
			       (size_t)copy_w * 4);
		}
		break;
	case SPA_VIDEO_FORMAT_BGRx:
		for (int y = 0; y < copy_h; y++) {
			const uint8_t *sp = src + (size_t)y * src_stride;
			uint8_t *dp = dst + (size_t)y * dst_stride;
			for (int x = 0; x < copy_w; x++) {
				dp[0] = sp[0];   /* B */
				dp[1] = sp[1];   /* G */
				dp[2] = sp[2];   /* R */
				dp[3] = 0xFF;    /* opaque */
				sp += 4; dp += 4;
			}
		}
		break;
	case SPA_VIDEO_FORMAT_RGBA:
	case SPA_VIDEO_FORMAT_RGBx: {
		int has_alpha = (spa_fmt == SPA_VIDEO_FORMAT_RGBA);
		for (int y = 0; y < copy_h; y++) {
			const uint8_t *sp = src + (size_t)y * src_stride;
			uint8_t *dp = dst + (size_t)y * dst_stride;
			for (int x = 0; x < copy_w; x++) {
				dp[0] = sp[2];   /* B */
				dp[1] = sp[1];   /* G */
				dp[2] = sp[0];   /* R */
				dp[3] = has_alpha ? sp[3] : 0xFF;
				sp += 4; dp += 4;
			}
		}
		break;
	}
	default:
		return 0;
	}

	/* Pad the rest of slot rows so leftover content from a smaller
	 * frame doesn't bleed through. */
	if (copy_w < slot_w) {
		for (int y = 0; y < copy_h; y++) {
			memset(dst + (size_t)y * dst_stride + copy_w * 4, 0,
			       (size_t)(slot_w - copy_w) * 4);
		}
	}
	if (copy_h < slot_h) {
		for (int y = copy_h; y < slot_h; y++) {
			memset(dst + (size_t)y * dst_stride, 0,
			       (size_t)slot_w * 4);
		}
	}
	return 1;
}

static int attach_pw_dmabuf(struct pf_state *st, struct pw_buffer *b);

static void
on_pw_process(void *data)
{
	struct pf_state *st = data;
	static unsigned long frame_count = 0;
	if ((frame_count++ % 60) == 0)
		LOGI("on_pw_process tick frame=%lu", frame_count);

	struct pw_buffer *b = pw_stream_dequeue_buffer(st->stream);
	if (!b)
		return;

	if (!st->format_known) {
		pw_stream_queue_buffer(st->stream, b);
		return;
	}
	struct spa_buffer *buf = b->buffer;
	if (buf->n_datas == 0) {
		pw_stream_queue_buffer(st->stream, b);
		return;
	}

	/* Log the actual buffer data type once — diagnostic for whether
	 * the producer fell back to MemFd despite our modifier offer. */
	static int data_type_logged = 0;
	if (!data_type_logged) {
		LOGI("pw first-frame data type=%u (DmaBuf=%u MemFd=%u MemPtr=%u)",
		     buf->datas[0].type, SPA_DATA_DmaBuf,
		     SPA_DATA_MemFd, SPA_DATA_MemPtr);
		data_type_logged = 1;
	}

	/* Branch on per-buffer data type. Producer (weston backend-pipewire
	 * line ~720) sets datas[0].type = SPA_DATA_DmaBuf when modifier was
	 * negotiated AND the renderer can allocate; otherwise SPA_DATA_MemFd.
	 * For DmaBuf, datas[0].data is NULL (no userspace mmap). */
	if (st->dmabuf && !st->no_dmabuf &&
	    buf->datas[0].type == SPA_DATA_DmaBuf) {
		pthread_mutex_lock(&st->mutex);
		int rc = attach_pw_dmabuf(st, b);
		pthread_mutex_unlock(&st->mutex);
		if (rc) {
			/* Buffer requeue happens in the wl_buffer release
			 * callback. Hold the pw_buffer until then. */
			return;
		}
		/* Fall through to SHM path on attach failure. */
	}

	if (!buf->datas[0].data) {
		pw_stream_queue_buffer(st->stream, b);
		return;
	}

	const uint8_t *src = buf->datas[0].data;
	uint32_t src_stride = buf->datas[0].chunk->stride;
	if (src_stride == 0)
		src_stride = st->format.size.width * 4;

	pthread_mutex_lock(&st->mutex);

	if (!st->slots_inited) {
		pthread_mutex_unlock(&st->mutex);
		pw_stream_queue_buffer(st->stream, b);
		return;
	}

	struct pf_slot *pick = NULL;
	for (int i = 0; i < 2; i++) {
		if (!st->slots[i].busy) { pick = &st->slots[i]; break; }
	}
	if (!pick) {
		/* both buffers held by compositor — drop frame. */
		pthread_mutex_unlock(&st->mutex);
		pw_stream_queue_buffer(st->stream, b);
		return;
	}

	int ok = copy_pw_to_slot(src, src_stride, pick,
				 st->format.size.width,
				 st->format.size.height,
				 st->width, st->height,
				 st->format.format);
	if (!ok) {
		pthread_mutex_unlock(&st->mutex);
		pw_stream_queue_buffer(st->stream, b);
		return;
	}

	pick->busy = 1;
	wl_surface_attach(st->surface, pick->wl_buf, 0, 0);
	wl_surface_damage_buffer(st->surface, 0, 0, st->width, st->height);
	wl_surface_commit(st->surface);
	wl_display_flush(st->display);

	pthread_mutex_unlock(&st->mutex);

	pw_stream_queue_buffer(st->stream, b);
}

/* §6.8 dmabuf zero-copy attach. We build a fresh wl_buffer per frame
 * wrapping the PipeWire-allocated dmabuf, attach + commit, and queue
 * the pw_buffer back to PW only after wl_buffer.release. The wl_buffer
 * is destroyed in the release listener too. State stays minimal — no
 * slot pool — because PW holds the dmabuf pool (default 4 buffers)
 * and we just hand them through.
 *
 * Returns 1 if the wl_buffer was successfully attached + commited;
 * caller must NOT requeue the pw_buffer until release. Returns 0 on
 * failure (caller queues back immediately).
 */

struct pf_dmabuf_carry {
	struct pf_state *st;
	struct pw_buffer *pw_buf;
};

static void
on_dmabuf_buffer_release(void *data, struct wl_buffer *buf)
{
	struct pf_dmabuf_carry *c = data;
	pthread_mutex_lock(&c->st->mutex);
	wl_buffer_destroy(buf);
	pw_stream_queue_buffer(c->st->stream, c->pw_buf);
	pthread_mutex_unlock(&c->st->mutex);
	free(c);
}

static const struct wl_buffer_listener dmabuf_buffer_listener = {
	.release = on_dmabuf_buffer_release,
};

/* Map SPA_VIDEO_FORMAT_* → DRM fourcc the outer compositor recognises.
 *
 * SPA video formats are byte-order; DRM fourcc is little-endian channel
 * order (DRM_FORMAT_XRGB8888 byte layout = B,G,R,X).
 *   SPA_VIDEO_FORMAT_BGRx → bytes B,G,R,X    → DRM_FORMAT_XRGB8888
 *   SPA_VIDEO_FORMAT_BGRA → bytes B,G,R,A    → DRM_FORMAT_ARGB8888
 *   SPA_VIDEO_FORMAT_RGBx / RGBA — inner producer never picks these;
 *   accepting them would require a swizzle which we don't currently do.
 *   Returning 0 forces the SHM fallback (which has its own swizzle). */
static uint32_t
spa_to_drm_fourcc(uint32_t spa_fmt)
{
	switch (spa_fmt) {
	case SPA_VIDEO_FORMAT_BGRx:
		return QDPF_DRM_FORMAT_XRGB8888;
	case SPA_VIDEO_FORMAT_BGRA:
		return QDPF_DRM_FORMAT_ARGB8888;
	default:
		return 0;
	}
}

/* Build a wl_buffer wrapping the PW-supplied dmabuf and attach it. Caller
 * holds st->mutex. Returns 1 on success (caller does NOT requeue). */
static int
attach_pw_dmabuf(struct pf_state *st, struct pw_buffer *b)
{
	struct spa_buffer *buf = b->buffer;
	struct spa_data *d = &buf->datas[0];

	if (d->type != SPA_DATA_DmaBuf || d->fd < 0) {
		return 0;
	}

	uint32_t drm_fmt = spa_to_drm_fourcc(st->format.format);
	if (drm_fmt == 0) {
		LOGE("dmabuf: no DRM fourcc for spa_fmt=%u", st->format.format);
		return 0;
	}

	uint32_t w = st->format.size.width;
	uint32_t h = st->format.size.height;
	uint32_t stride = (uint32_t)d->chunk->stride;
	uint32_t offset = (uint32_t)d->chunk->offset;
	if (stride == 0) stride = w * 4;

	/* Modifier is always LINEAR — that's the only choice we put in
	 * EnumFormat. If the producer ever returns a different one we'd
	 * need to read it back from the negotiated format pod, but with
	 * a single-element CHOICE_ENUM that path can't fire today. */
	uint64_t modifier = QDPF_DRM_MODIFIER_LINEAR;

	struct zwp_linux_buffer_params_v1 *params =
		zwp_linux_dmabuf_v1_create_params(st->dmabuf);
	zwp_linux_buffer_params_v1_add(params, d->fd, 0, offset, stride,
		(uint32_t)(modifier >> 32), (uint32_t)(modifier & 0xFFFFFFFFu));

	struct wl_buffer *wb = zwp_linux_buffer_params_v1_create_immed(
		params, (int32_t)w, (int32_t)h, drm_fmt, 0);
	zwp_linux_buffer_params_v1_destroy(params);
	if (!wb) {
		LOGE("dmabuf: create_immed returned NULL");
		return 0;
	}

	struct pf_dmabuf_carry *c = calloc(1, sizeof(*c));
	if (!c) {
		wl_buffer_destroy(wb);
		return 0;
	}
	c->st = st;
	c->pw_buf = b;
	wl_buffer_add_listener(wb, &dmabuf_buffer_listener, c);

	wl_surface_attach(st->surface, wb, 0, 0);
	wl_surface_damage_buffer(st->surface, 0, 0, (int)w, (int)h);
	wl_surface_commit(st->surface);
	wl_display_flush(st->display);

	if (!st->dmabuf_active) {
		st->dmabuf_active = 1;
		LOGI("dmabuf: zero-copy path active "
		     "(wxh=%ux%u stride=%u drm_fmt=0x%08x mod=0x%lx)",
		     w, h, stride, drm_fmt, (unsigned long)modifier);
	}
	return 1;
}

static const struct pw_stream_events stream_events = {
	PW_VERSION_STREAM_EVENTS,
	.state_changed = on_pw_state_changed,
	.param_changed = on_pw_param_changed,
	.process       = on_pw_process,
};

/* Start the PipeWire thread loop and connect the input stream. Mirrors
 * qdistro-forward's subsystem_start. Non-fatal: if anything fails we
 * log and keep the wl side running on the solid colour. */
static int
pf_start_pipewire(struct pf_state *st)
{
	if (!st->pw_target) {
		LOGI("pw: no target — staying on solid colour");
		return -1;
	}

	st->pw_loop = pw_thread_loop_new("pixelfeed-pw", NULL);
	if (!st->pw_loop) {
		LOGE("pw_thread_loop_new failed");
		return -1;
	}
	st->pw_context = pw_context_new(
		pw_thread_loop_get_loop(st->pw_loop), NULL, 0);
	if (!st->pw_context) {
		LOGE("pw_context_new failed");
		return -1;
	}
	if (pw_thread_loop_start(st->pw_loop) < 0) {
		LOGE("pw_thread_loop_start failed");
		return -1;
	}

	pw_thread_loop_lock(st->pw_loop);
	st->pw_core = pw_context_connect(st->pw_context, NULL, 0);
	if (!st->pw_core) {
		LOGE("pw_context_connect failed: %m");
		pw_thread_loop_unlock(st->pw_loop);
		return -1;
	}

	struct pw_properties *props = pw_properties_new(
		PW_KEY_MEDIA_TYPE,     "Video",
		PW_KEY_MEDIA_CATEGORY, "Capture",
		PW_KEY_MEDIA_ROLE,     "Screen",
		PW_KEY_TARGET_OBJECT,  st->pw_target,
		PW_KEY_NODE_NAME,      "qdistro-nested-pixelfeed",
		NULL);

	st->stream = pw_stream_new(st->pw_core,
		"qdistro-nested-pixelfeed", props);
	if (!st->stream) {
		LOGE("pw_stream_new failed");
		pw_thread_loop_unlock(st->pw_loop);
		return -1;
	}
	pw_stream_add_listener(st->stream, &st->stream_listener,
			       &stream_events, st);

	/* weston backend-pipewire produces BGRx at the configured output
	 * size with framerate 0/1 (driver-pulled). We match exactly —
	 * pattern from qdistro-forward.c.
	 *
	 * §6.8 dmabuf: when the outer compositor advertises
	 * zwp_linux_dmabuf_v1 we publish a TWO-element EnumFormat list:
	 *   [0] BGRx + LINEAR modifier  → producer takes dmabuf path
	 *   [1] BGRx no modifier        → SHM fallback
	 * weston tries them in order; the dmabuf path is chosen iff
	 * pipewire_output_create_dmabuf succeeds (gl-renderer + GBM).
	 *
	 * Without the global, only the SHM-shaped descriptor is sent. */
	uint8_t pod_buf[2048];
	struct spa_pod_builder b = SPA_POD_BUILDER_INIT(pod_buf, sizeof(pod_buf));
	const struct spa_pod *params[2];
	int n_params = 0;

	int try_dmabuf = (st->dmabuf != NULL) && !st->no_dmabuf;
	if (try_dmabuf) {
		params[n_params++] = spa_pod_builder_add_object(
			&b,
			SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
			SPA_FORMAT_mediaType,    SPA_POD_Id(SPA_MEDIA_TYPE_video),
			SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
			SPA_FORMAT_VIDEO_format, SPA_POD_Id(SPA_VIDEO_FORMAT_BGRx),
			SPA_FORMAT_VIDEO_modifier,
				SPA_POD_CHOICE_ENUM_Long(2,
					(int64_t)0 /*LINEAR default*/,
					(int64_t)0 /*LINEAR alt*/),
			SPA_FORMAT_VIDEO_size,
				SPA_POD_Rectangle(&SPA_RECTANGLE((uint32_t)st->width,
								 (uint32_t)st->height)),
			SPA_FORMAT_VIDEO_framerate,
				SPA_POD_Fraction(&SPA_FRACTION(0, 1)));
	}
	params[n_params++] = spa_pod_builder_add_object(
		&b,
		SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
		SPA_FORMAT_mediaType,    SPA_POD_Id(SPA_MEDIA_TYPE_video),
		SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
		SPA_FORMAT_VIDEO_format, SPA_POD_Id(SPA_VIDEO_FORMAT_BGRx),
		SPA_FORMAT_VIDEO_size,
			SPA_POD_Rectangle(&SPA_RECTANGLE((uint32_t)st->width,
							 (uint32_t)st->height)),
		SPA_FORMAT_VIDEO_framerate,
			SPA_POD_Fraction(&SPA_FRACTION(0, 1)));

	LOGI("pw connect: try_dmabuf=%d n_params=%d", try_dmabuf, n_params);

	int rc = pw_stream_connect(
		st->stream, PW_DIRECTION_INPUT, PW_ID_ANY,
		PW_STREAM_FLAG_AUTOCONNECT |
		PW_STREAM_FLAG_MAP_BUFFERS |
		PW_STREAM_FLAG_INACTIVE,
		params, n_params);
	if (rc < 0) {
		LOGE("pw_stream_connect failed: %s", spa_strerror(rc));
		pw_thread_loop_unlock(st->pw_loop);
		return -1;
	}
	pw_stream_set_active(st->stream, true);
	pw_thread_loop_unlock(st->pw_loop);

	LOGI("pw: streaming target=%s pid=%d", st->pw_target, st->pw_pid);
	return 0;
}

static void
pf_stop_pipewire(struct pf_state *st)
{
	if (!st->pw_loop)
		return;
	pw_thread_loop_stop(st->pw_loop);
	if (st->stream) {
		pw_stream_destroy(st->stream);
		st->stream = NULL;
	}
	if (st->pw_core) {
		pw_core_disconnect(st->pw_core);
		st->pw_core = NULL;
	}
	if (st->pw_context) {
		pw_context_destroy(st->pw_context);
		st->pw_context = NULL;
	}
	pw_thread_loop_destroy(st->pw_loop);
	st->pw_loop = NULL;
}

/* ---------- signal ---------- */

static void on_signal(int sig)
{
	(void)sig;
	g_st.stop = 1;
}

/* ---------- main ---------- */

int
main(int argc, char *argv[])
{
	if (argc < 3) {
		fprintf(stderr,
			"usage: %s <handle> <pw_node> [input_sink]\n", argv[0]);
		return 2;
	}
	uint32_t handle = (uint32_t)strtoul(argv[1], NULL, 10);
	const char *pw_node = argv[2];
	const char *input_sink = (argc > 3) ? argv[3] : "";

	int width  = atoi(getenv("QDWIN_PIXELFEED_W") ?: "0");
	int height = atoi(getenv("QDWIN_PIXELFEED_H") ?: "0");
	if (width  <= 0) width  = 800;
	if (height <= 0) height = 600;
	const char *hold_env = getenv("QDWIN_PIXELFEED_HOLD");
	int hold_secs = hold_env ? atoi(hold_env) : -1;
	uint32_t pixel = parse_rgba(getenv("QDWIN_PIXELFEED_RGBA"));
	int no_pw = !!getenv("QDWIN_PIXELFEED_NO_PW") || hold_secs == 0;
	g_st.no_dmabuf = !!getenv("QDWIN_PIXELFEED_NO_DMABUF");

	LOGI("handle=%u pw_node=%s input_sink=%s size=%dx%d colour=0x%08x "
	     "hold=%d no_pw=%d",
	     handle, pw_node, input_sink, width, height, pixel,
	     hold_secs, no_pw);

	g_st.width = width;
	g_st.height = height;
	pthread_mutex_init(&g_st.mutex, NULL);
	parse_pw_node(pw_node, &g_st);

	/* §6.8 S1 gotcha: WAYLAND_SOCKET inherited from a wayland-launched
	 * parent would dup that fd — clear before connect. */
	unsetenv("WAYLAND_SOCKET");

	pw_init(&argc, &argv);

	struct sigaction sa = { .sa_handler = on_signal };
	sigemptyset(&sa.sa_mask);
	sigaction(SIGTERM, &sa, NULL);
	sigaction(SIGINT,  &sa, NULL);
	signal(SIGPIPE, SIG_IGN);

	g_st.display = wl_display_connect(NULL);
	if (!g_st.display) {
		LOGE("wl_display_connect: %s", strerror(errno));
		return 3;
	}
	g_st.registry = wl_display_get_registry(g_st.display);
	wl_registry_add_listener(g_st.registry, &registry_listener, &g_st);
	wl_display_roundtrip(g_st.display);
	if (!g_st.compositor || !g_st.shm || !g_st.shell) {
		LOGE("missing globals: compositor=%p shm=%p shell=%p",
		     (void *)g_st.compositor, (void *)g_st.shm,
		     (void *)g_st.shell);
		return 4;
	}
	if (g_st.shell_version < 9) {
		LOGE("qdwin_shell_v1 v%u < 9 — bind_proxy_pixels unavailable",
		     g_st.shell_version);
		return 5;
	}
	LOGI("dmabuf global: %s (v%u, no_dmabuf=%d)",
	     g_st.dmabuf ? "bound" : "absent",
	     g_st.dmabuf_version, g_st.no_dmabuf);
	wl_display_roundtrip(g_st.display);

	/* Allocate two slots (S2c double buffer). Slot 0 gets pre-filled
	 * with the solid colour and is used for the boot bind; slot 1
	 * stays clear until the first PW frame. */
	if (pf_slot_init(&g_st, &g_st.slots[0], 0, width, height) < 0)
		return 6;
	if (pf_slot_init(&g_st, &g_st.slots[1], 1, width, height) < 0)
		return 6;
	g_st.slots_inited = 1;

	uint32_t *pix0 = (uint32_t *)g_st.slots[0].data;
	for (size_t i = 0; i < (size_t)width * height; i++)
		pix0[i] = pixel;

	g_st.surface = wl_compositor_create_surface(g_st.compositor);

	pthread_mutex_lock(&g_st.mutex);
	g_st.slots[0].busy = 1;
	wl_surface_attach(g_st.surface, g_st.slots[0].wl_buf, 0, 0);
	wl_surface_damage_buffer(g_st.surface, 0, 0, width, height);
	wl_surface_commit(g_st.surface);
	wl_display_flush(g_st.display);
	pthread_mutex_unlock(&g_st.mutex);

	wl_display_roundtrip(g_st.display);

	qdwin_shell_v1_bind_proxy_pixels(g_st.shell, handle, g_st.surface);
	wl_display_flush(g_st.display);
	wl_display_roundtrip(g_st.display);

	LOGI("bound proxy_pixels handle=%u surface=%p", handle, (void *)g_st.surface);

	if (hold_secs == 0) {
		/* Test-mode short exit: leave the surface bound for one more
		 * roundtrip so the compositor commits the swap, then bail. */
		wl_display_roundtrip(g_st.display);
		pf_slot_release(&g_st.slots[0]);
		pf_slot_release(&g_st.slots[1]);
		wl_surface_destroy(g_st.surface);
		wl_display_roundtrip(g_st.display);
		wl_display_disconnect(g_st.display);
		free(g_st.pw_target);
		return 0;
	}

	if (!no_pw) {
		if (pf_start_pipewire(&g_st) != 0)
			LOGE("pw start failed — staying on solid colour");
	}

	/* wl dispatch loop with mutex protecting marshals against the
	 * PW thread. Pattern from qdistro-forward.c qfwd_wl_thread. */
	struct timespec deadline = {0};
	if (hold_secs > 0) {
		clock_gettime(CLOCK_MONOTONIC, &deadline);
		deadline.tv_sec += hold_secs;
	}

	while (!g_st.stop) {
		pthread_mutex_lock(&g_st.mutex);
		while (wl_display_prepare_read(g_st.display) != 0) {
			wl_display_dispatch_pending(g_st.display);
		}
		wl_display_flush(g_st.display);
		pthread_mutex_unlock(&g_st.mutex);

		struct pollfd pfd = {
			.fd = wl_display_get_fd(g_st.display),
			.events = POLLIN,
		};
		int rc = poll(&pfd, 1, 200);
		(void)rc;

		pthread_mutex_lock(&g_st.mutex);
		if (pfd.revents & POLLIN) {
			wl_display_read_events(g_st.display);
			wl_display_dispatch_pending(g_st.display);
		} else {
			wl_display_cancel_read(g_st.display);
		}
		pthread_mutex_unlock(&g_st.mutex);

		if (hold_secs > 0) {
			struct timespec now;
			clock_gettime(CLOCK_MONOTONIC, &now);
			if (now.tv_sec >= deadline.tv_sec) break;
		}
	}

	if (!no_pw)
		pf_stop_pipewire(&g_st);

	pf_slot_release(&g_st.slots[0]);
	pf_slot_release(&g_st.slots[1]);
	wl_surface_destroy(g_st.surface);
	wl_display_disconnect(g_st.display);
	pthread_mutex_destroy(&g_st.mutex);
	free(g_st.pw_target);
	pw_deinit();
	return 0;
}
