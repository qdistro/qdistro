#ifndef QDISTRO_MM_REMOTE_FRAME_PROTOCOL_H
#define QDISTRO_MM_REMOTE_FRAME_PROTOCOL_H

#include <stddef.h>
#include <stdint.h>

#define QDMM_LOCAL_HEADER_SIZE 16u
#define QDMM_FRAME_HEADER_SIZE 24u
#define QDMM_MAX_MEDIA_BYTES (3u * 1024u * 1024u)
#define QDMM_MAX_LOCAL_BYTES (QDMM_MAX_MEDIA_BYTES + QDMM_LOCAL_HEADER_SIZE)
#define QDMM_DECODER_ACK_WIRE_SIZE (4u + QDMM_LOCAL_HEADER_SIZE)

struct qdmm_frame_view {
	uint64_t seq;
	uint32_t width;
	uint32_t height;
	uint32_t stride;
	const uint8_t *pixels;
	size_t pixels_len;
};

int qdmm_parse_frame(const uint8_t *message, size_t length,
		     struct qdmm_frame_view *out);

int qdmm_build_frame_socket_path(const char *runtime_dir,
				 const char *source,
				 char *out, size_t out_size);

int qdmm_build_decoder_ack(uint64_t seq,
			   uint8_t out[QDMM_DECODER_ACK_WIRE_SIZE]);

#endif
