#ifndef QDISTRO_MM_REMOTE_FRAME_PROTOCOL_H
#define QDISTRO_MM_REMOTE_FRAME_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#define QDMM_LOCAL_HEADER_SIZE 16u
#define QDMM_LOCAL_ANNOUNCE_HEADER_SIZE 32u
#define QDMM_SOURCE_CONFIG_HEADER_SIZE 24u
#define QDMM_FRAME_HEADER_SIZE 24u
#define QDMM_MAX_MEDIA_BYTES (3u * 1024u * 1024u)
#define QDMM_MAX_LOCAL_BYTES (QDMM_MAX_MEDIA_BYTES + QDMM_LOCAL_HEADER_SIZE)
#define QDMM_DECODER_ACK_WIRE_SIZE (4u + QDMM_LOCAL_HEADER_SIZE)
#define QDMM_MAX_QDNI_PACKET_SIZE 20u
#define QDMM_MAX_LOCAL_INPUT_WIRE_SIZE (4u + QDMM_LOCAL_HEADER_SIZE + 8u)

enum qdmm_local_kind {
	QDMM_LOCAL_CONNECTED = 1,
	QDMM_LOCAL_ANNOUNCE = 2,
	QDMM_LOCAL_FRAME = 3,
	QDMM_LOCAL_SOURCE_CLOSED = 4,
	QDMM_LOCAL_DECODER_ACK = 5,
	QDMM_LOCAL_MEDIA_ACK = 6,
	QDMM_LOCAL_MOTION = 7,
	QDMM_LOCAL_BUTTON = 8,
	QDMM_LOCAL_KEY = 9,
	QDMM_LOCAL_AXIS = 10,
	QDMM_LOCAL_FOCUS = 11,
	QDMM_LOCAL_DETACHED = 12,
	QDMM_LOCAL_CLOSE_REQUEST = 15,
};

struct qdmm_local_message_view {
	uint8_t kind;
	uint64_t seq;
	const uint8_t *payload;
	size_t payload_len;
};

struct qdmm_announcement_view {
	uint64_t source_revision;
	uint32_t width;
	uint32_t height;
	uint32_t stride;
	const uint8_t *app_id;
	size_t app_id_len;
	const uint8_t *title;
	size_t title_len;
};

struct qdmm_source_config_view {
	uint64_t source_revision;
	const uint8_t *pw_node;
	size_t pw_node_len;
	const uint8_t *input_sink;
	size_t input_sink_len;
	const uint8_t *app_id;
	size_t app_id_len;
	const uint8_t *title;
	size_t title_len;
};

struct qdmm_frame_view {
	uint64_t seq;
	uint32_t width;
	uint32_t height;
	uint32_t stride;
	const uint8_t *pixels;
	size_t pixels_len;
};

int qdmm_parse_local_message(const uint8_t *message, size_t length,
			     struct qdmm_local_message_view *out);

int qdmm_parse_announcement(const uint8_t *message, size_t length,
			    struct qdmm_announcement_view *out);

int qdmm_parse_source_config(const uint8_t *config, size_t length,
			     struct qdmm_source_config_view *out);

int qdmm_build_announcement_wire(
	const struct qdmm_source_config_view *config,
	uint32_t width, uint32_t height, uint32_t stride,
	uint8_t *out, size_t out_size, size_t *out_len);

int qdmm_build_frame_wire(uint64_t seq,
	uint32_t width, uint32_t height, uint32_t stride,
	const uint8_t *pixels, size_t pixels_len,
	uint8_t *out, size_t out_size, size_t *out_len);

int qdmm_parse_frame(const uint8_t *message, size_t length,
		     struct qdmm_frame_view *out);

int qdmm_build_frame_socket_path(const char *runtime_dir,
				 const char *source,
				 char *out, size_t out_size);

int qdmm_build_decoder_ack(uint64_t seq,
			   uint8_t out[QDMM_DECODER_ACK_WIRE_SIZE]);

/* Translate existing local qdwin QDNI into the fixed-width controller seam.
 * Return 1 for a valid PING (no controller message), 0 for output, -1 on a
 * malformed or unsupported packet. */
int qdmm_qdni_to_local(const uint8_t *packet, size_t length,
		       uint8_t out[QDMM_MAX_LOCAL_INPUT_WIRE_SIZE],
		       size_t *out_len);

/* Reverse the helper record for source-local QDNI injection. */
int qdmm_local_to_qdni(const uint8_t *message, size_t length,
		       uint32_t time_msec,
		       uint8_t out[QDMM_MAX_QDNI_PACKET_SIZE],
		       size_t *out_len);

#endif
