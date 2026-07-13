#include "mm-remote-frame-protocol.h"

#include <arpa/inet.h>
#include <endian.h>
#include <stdio.h>
#include <string.h>

#define QDMM_MAX_DIMENSION 8192u
#define QDML_TYPE_FRAME 3u
#define QDML_TYPE_DECODER_ACK 5u

static uint32_t
read_be32(const uint8_t *p)
{
	uint32_t value;
	memcpy(&value, p, sizeof value);
	return ntohl(value);
}

static uint64_t
read_be64(const uint8_t *p)
{
	uint64_t value;
	memcpy(&value, p, sizeof value);
	return be64toh(value);
}

static void
write_be64(uint8_t *p, uint64_t value)
{
	value = htobe64(value);
	memcpy(p, &value, sizeof value);
}

int
qdmm_parse_frame(const uint8_t *message, size_t length,
		 struct qdmm_frame_view *out)
{
	if (!message || !out ||
	    length < QDMM_LOCAL_HEADER_SIZE + QDMM_FRAME_HEADER_SIZE)
		return -1;
	if (memcmp(message, "QDML", 4) != 0 || message[4] != 1 ||
	    message[5] != QDML_TYPE_FRAME || message[6] != 0 ||
	    message[7] != 0)
		return -1;
	const uint8_t *frame = message + QDMM_LOCAL_HEADER_SIZE;
	size_t frame_len = length - QDMM_LOCAL_HEADER_SIZE;
	if (memcmp(frame, "QDMF", 4) != 0 || frame[4] != 1 ||
	    frame[5] != 1 || frame[6] != 0 || frame[7] != 0)
		return -1;
	uint32_t width = read_be32(frame + 8);
	uint32_t height = read_be32(frame + 12);
	uint32_t stride = read_be32(frame + 16);
	uint32_t pixels_len = read_be32(frame + 20);
	if (!width || !height || width > QDMM_MAX_DIMENSION ||
	    height > QDMM_MAX_DIMENSION || stride != width * 4u ||
	    pixels_len != frame_len - QDMM_FRAME_HEADER_SIZE ||
	    (uint64_t)stride * height != pixels_len ||
	    frame_len > QDMM_MAX_MEDIA_BYTES)
		return -1;
	out->seq = read_be64(message + 8);
	if (!out->seq)
		return -1;
	out->width = width;
	out->height = height;
	out->stride = stride;
	out->pixels = frame + QDMM_FRAME_HEADER_SIZE;
	out->pixels_len = pixels_len;
	return 0;
}

static int
token_valid(const char *token)
{
	size_t length = token ? strlen(token) : 0;
	if (!length || length > 64)
		return 0;
	for (size_t i = 0; i < length; i++) {
		char c = token[i];
		if (!((c >= 'a' && c <= 'z') ||
		      (c >= 'A' && c <= 'Z') ||
		      (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-'))
			return 0;
	}
	return 1;
}

int
qdmm_build_frame_socket_path(const char *runtime_dir, const char *source,
			     char *out, size_t out_size)
{
	static const char prefix[] = "qdistro.remote:";
	if (!runtime_dir || runtime_dir[0] != '/' || !source || !out ||
	    !out_size || strncmp(source, prefix, sizeof prefix - 1) != 0)
		return -1;
	const char *token = source + sizeof prefix - 1;
	if (!token_valid(token))
		return -1;
	int written = snprintf(out, out_size,
		"%s/qdistro-mm-frame-%s.sock", runtime_dir, token);
	return written < 0 || (size_t)written >= out_size ? -1 : 0;
}

int
qdmm_build_decoder_ack(uint64_t seq,
		       uint8_t out[QDMM_DECODER_ACK_WIRE_SIZE])
{
	if (!seq || !out)
		return -1;
	uint32_t length = htonl(QDMM_LOCAL_HEADER_SIZE);
	memcpy(out, &length, sizeof length);
	memcpy(out + 4, "QDML", 4);
	out[8] = 1;
	out[9] = QDML_TYPE_DECODER_ACK;
	out[10] = 0;
	out[11] = 0;
	write_be64(out + 12, seq);
	return 0;
}
