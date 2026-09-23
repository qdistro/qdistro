/*
 * §6.8 S1 — header for the wl_client side of qdwin_nested_v1.
 *
 * Exposed to qdwin.c (which speaks server-side of the same protocol)
 * via opaque-pointer types.  No wayland-client.h leaks here.
 */
#ifndef QDWIN_NESTED_CLIENT_H
#define QDWIN_NESTED_CLIENT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

struct qdwin_nested_client;
struct qdwin_nested_client_pub;

struct qdwin_nested_client *
qdwin_nested_client_new(const char *display_name);

/* §6.8 S3: per-toplevel input sink. Returned path is malloc'd and
 * owned by the caller (free with qdwin_nested_input_sink_close which
 * also tears down the socket). The fd is the listener; the caller
 * must accept(2) on it via the weston event loop. Returns NULL on
 * failure (caller should advertise input_sink="" in that case). */
struct qdwin_nested_input_sink {
	int listen_fd;
	int peer_fd;       /* -1 until accept */
	char *socket_path;
	/* §6.8 Round-8 fix: per-peer accumulation buffer. The outer can
	 * blast multiple packets in rapid succession; SOCK_STREAM splits
	 * them at the kernel's whim. read_one accumulates into rbuf and
	 * returns -2 (would-block) when a full packet isn't yet ready so
	 * the caller knows to stop looping but keep the source armed. */
	uint8_t rbuf[8 + 256];
	size_t rbuf_used;
};

struct qdwin_nested_input_sink *
qdwin_nested_input_sink_open(uint32_t handle);
void qdwin_nested_input_sink_close(struct qdwin_nested_input_sink *sink);
/* Accept a pending connection on the listener fd. Returns 0 on
 * success (peer_fd populated), -1 on transient/permanent error. */
int qdwin_nested_input_sink_accept(struct qdwin_nested_input_sink *sink);
/* Read one packet header + payload. Returns the event_type byte on
 * success, -1 on read error / closed peer, or -2 when not enough
 * bytes are buffered yet (caller should stop the drain loop and wait
 * for the next FD-readable notification). */
#define QDWIN_NESTED_INPUT_SINK_AGAIN (-2)
int qdwin_nested_input_sink_read_one(struct qdwin_nested_input_sink *sink,
				     uint8_t *out_version,
				     uint16_t *out_payload_len,
				     uint8_t *payload_buf,
				     size_t payload_buf_size);

/* §6.8 S3: outer-side connector. Connects to a nested-published
 * input sink path, returns the socket fd or -1. Caller must
 * close(2) when done. */
int qdwin_nested_input_sink_connect(const char *socket_path);
/* Send one event packet. Returns 0 on success, -1 on write error. */
int qdwin_nested_input_sink_send(int peer_fd, uint8_t event_type,
				 const void *payload, uint16_t payload_len);

/* §6.8 S3b: QDNI event_type constants + payload structs. The wire
 * is little-endian (every field a fixed-width integer); all packets
 * carry an 8-byte header (magic+version+event_type+payload_len)
 * already consumed by qdwin_nested_input_sink_read_one. The payload
 * shape below is what callers serialise/deserialise inline. */
#define QDNI_EVENT_PING         1
#define QDNI_EVENT_MOTION       2
#define QDNI_EVENT_BUTTON       3
#define QDNI_EVENT_KEY          4
#define QDNI_EVENT_AXIS         5
#define QDNI_EVENT_FOCUS        6

struct qdni_motion_payload {
	uint32_t time_msec;
	int32_t  x_fixed;       /* wl_fixed-encoded surface-local x */
	int32_t  y_fixed;       /* wl_fixed-encoded surface-local y */
};

struct qdni_button_payload {
	uint32_t time_msec;
	uint32_t button;        /* Linux input event code (e.g. BTN_LEFT) */
	uint32_t state;         /* 0 = released, 1 = pressed */
};

struct qdni_key_payload {
	uint32_t time_msec;
	uint32_t key;           /* Linux keycode */
	uint32_t state;         /* 0 = released, 1 = pressed */
};

struct qdni_axis_payload {
	uint32_t time_msec;
	uint32_t axis;          /* 0 = vertical, 1 = horizontal */
	int32_t  value_fixed;   /* wl_fixed-encoded scroll delta */
};

struct qdni_focus_payload {
	uint32_t focused;       /* 0 = leave, 1 = enter */
};

/* §6.8 S3b: tiny send wrappers that fill in payload + length + call
 * qdwin_nested_input_sink_send. Caller passes the already-connected
 * peer fd (the outer-side fd from qdwin_nested_input_sink_connect).
 * Each returns 0 on success, -1 on write error. */
int qdwin_nested_input_sink_send_motion(int peer_fd, uint32_t time_msec,
					int32_t x_fixed, int32_t y_fixed);
int qdwin_nested_input_sink_send_button(int peer_fd, uint32_t time_msec,
					uint32_t button, uint32_t state);
int qdwin_nested_input_sink_send_key(int peer_fd, uint32_t time_msec,
				     uint32_t key, uint32_t state);
int qdwin_nested_input_sink_send_axis(int peer_fd, uint32_t time_msec,
				      uint32_t axis, int32_t value_fixed);
int qdwin_nested_input_sink_send_focus(int peer_fd, uint32_t focused);

void qdwin_nested_client_destroy(struct qdwin_nested_client *c);

int  qdwin_nested_client_get_fd(struct qdwin_nested_client *c);
int  qdwin_nested_client_dispatch_pending(struct qdwin_nested_client *c);
int  qdwin_nested_client_flush(struct qdwin_nested_client *c);
bool qdwin_nested_client_manager_ready(struct qdwin_nested_client *c);
uint32_t qdwin_nested_client_manager_version(struct qdwin_nested_client *c);
bool qdwin_nested_client_failed(struct qdwin_nested_client *c);

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
			      void (*on_focus_changed)(void *, uint32_t));

void qdwin_nested_client_pub_set_title(struct qdwin_nested_client_pub *pub,
				       const char *title);
void qdwin_nested_client_pub_set_app_id(struct qdwin_nested_client_pub *pub,
					const char *app_id);
void qdwin_nested_client_pub_set_geometry(struct qdwin_nested_client_pub *pub,
					  int32_t w, int32_t h);
void qdwin_nested_client_pub_destroy(struct qdwin_nested_client_pub *pub);

#endif /* QDWIN_NESTED_CLIENT_H */
