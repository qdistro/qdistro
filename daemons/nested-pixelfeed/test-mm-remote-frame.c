#include "mm-remote-frame-protocol.h"

#include <arpa/inet.h>
#include <endian.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define CHECK(condition, message) do { \
	if (!(condition)) { fprintf(stderr, "FAIL: %s\n", message); return 1; } \
} while (0)

static void
write_u32(uint8_t *out, uint32_t value)
{
	value = htonl(value);
	memcpy(out, &value, sizeof value);
}

static void
write_u64(uint8_t *out, uint64_t value)
{
	value = htobe64(value);
	memcpy(out, &value, sizeof value);
}

int
main(void)
{
	uint8_t message[QDMM_LOCAL_HEADER_SIZE + QDMM_FRAME_HEADER_SIZE + 48] = {0};
	memcpy(message, "QDML", 4);
	message[4] = 1;
	message[5] = 3;
	write_u64(message + 8, 7);
	uint8_t *frame = message + QDMM_LOCAL_HEADER_SIZE;
	memcpy(frame, "QDMF", 4);
	frame[4] = 1;
	frame[5] = 1;
	write_u32(frame + 8, 4);
	write_u32(frame + 12, 3);
	write_u32(frame + 16, 16);
	write_u32(frame + 20, 48);
	memset(frame + QDMM_FRAME_HEADER_SIZE, 0x5a, 48);

	struct qdmm_frame_view parsed;
	CHECK(qdmm_parse_frame(message, sizeof message, &parsed) == 0,
	      "valid frame rejected");
	CHECK(parsed.seq == 7 && parsed.width == 4 && parsed.height == 3 &&
	      parsed.stride == 16 && parsed.pixels_len == 48 &&
	      parsed.pixels[0] == 0x5a, "valid frame parsed incorrectly");

	uint8_t hostile[sizeof message];
	memcpy(hostile, message, sizeof hostile);
	hostile[5] = 9;
	CHECK(qdmm_parse_frame(hostile, sizeof hostile, &parsed) < 0,
	      "unknown local type accepted");
	memcpy(hostile, message, sizeof hostile);
	write_u64(hostile + 8, 0);
	CHECK(qdmm_parse_frame(hostile, sizeof hostile, &parsed) < 0,
	      "zero sequence accepted");
	memcpy(hostile, message, sizeof hostile);
	write_u32(hostile + QDMM_LOCAL_HEADER_SIZE + 16, 20);
	CHECK(qdmm_parse_frame(hostile, sizeof hostile, &parsed) < 0,
	      "mismatched stride accepted");
	memcpy(hostile, message, sizeof hostile);
	write_u32(hostile + QDMM_LOCAL_HEADER_SIZE + 20, 47);
	CHECK(qdmm_parse_frame(hostile, sizeof hostile, &parsed) < 0,
	      "mismatched payload length accepted");
	CHECK(qdmm_parse_frame(message, sizeof message - 1, &parsed) < 0,
	      "truncated frame accepted");

	char path[108];
	CHECK(qdmm_build_frame_socket_path(
	      "/run/user/1000", "qdistro.remote:stream_A-7", path,
	      sizeof path) == 0, "valid frame socket token rejected");
	CHECK(strcmp(path,
	      "/run/user/1000/qdistro-mm-frame-stream_A-7.sock") == 0,
	      "frame socket path differs");
	CHECK(qdmm_build_frame_socket_path(
	      "/run/user/1000", "qdistro.remote:../../evil", path,
	      sizeof path) < 0, "path traversal token accepted");
	CHECK(qdmm_build_frame_socket_path(
	      "relative", "qdistro.remote:valid", path,
	      sizeof path) < 0, "relative runtime directory accepted");
	CHECK(qdmm_build_frame_socket_path(
	      "/run/user/1000", "weston.pipewire:1:x", path,
	      sizeof path) < 0, "non-remote source accepted");

	uint8_t ack[QDMM_DECODER_ACK_WIRE_SIZE];
	CHECK(qdmm_build_decoder_ack(9, ack) == 0,
	      "decoder ack build failed");
	uint32_t ack_length;
	memcpy(&ack_length, ack, sizeof ack_length);
	CHECK(ntohl(ack_length) == QDMM_LOCAL_HEADER_SIZE &&
	      memcmp(ack + 4, "QDML", 4) == 0 && ack[8] == 1 && ack[9] == 5,
	      "decoder ack header differs");
	uint64_t ack_seq;
	memcpy(&ack_seq, ack + 12, sizeof ack_seq);
	CHECK(be64toh(ack_seq) == 9, "decoder ack sequence differs");
	CHECK(qdmm_build_decoder_ack(0, ack) < 0,
	      "zero decoder ack sequence accepted");

	puts("PASS: remote frame protocol bounds, token, and ack");
	return 0;
}
