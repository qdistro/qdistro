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

static void
write_u16(uint8_t *out, uint16_t value)
{
	value = htons(value);
	memcpy(out, &value, sizeof value);
}

static void
write_le16(uint8_t *out, uint16_t value)
{
	out[0] = (uint8_t)value;
	out[1] = (uint8_t)(value >> 8);
}

static void
write_le32(uint8_t *out, uint32_t value)
{
	out[0] = (uint8_t)value;
	out[1] = (uint8_t)(value >> 8);
	out[2] = (uint8_t)(value >> 16);
	out[3] = (uint8_t)(value >> 24);
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

	static const char app_id[] = "org.example.Editor";
	static const char title[] = "Remote editor";
	uint8_t announce[QDMM_LOCAL_HEADER_SIZE +
		QDMM_LOCAL_ANNOUNCE_HEADER_SIZE + sizeof app_id - 1 +
		sizeof title - 1] = {0};
	memcpy(announce, "QDML", 4);
	announce[4] = 1;
	announce[5] = QDMM_LOCAL_ANNOUNCE;
	write_u64(announce + 8, 7);
	uint8_t *a = announce + QDMM_LOCAL_HEADER_SIZE;
	memcpy(a, "QDMA", 4);
	a[4] = 1;
	a[5] = 1;
	write_u64(a + 8, 7);
	write_u32(a + 16, 4);
	write_u32(a + 20, 3);
	write_u32(a + 24, 16);
	write_u16(a + 28, sizeof app_id - 1);
	write_u16(a + 30, sizeof title - 1);
	memcpy(a + QDMM_LOCAL_ANNOUNCE_HEADER_SIZE,
	       app_id, sizeof app_id - 1);
	memcpy(a + QDMM_LOCAL_ANNOUNCE_HEADER_SIZE + sizeof app_id - 1,
	       title, sizeof title - 1);
	struct qdmm_announcement_view announced;
	CHECK(qdmm_parse_announcement(
	      announce, sizeof announce, &announced) == 0,
	      "valid binary announcement rejected");
	CHECK(announced.source_revision == 7 && announced.width == 4 &&
	      announced.height == 3 && announced.stride == 16 &&
	      announced.app_id_len == sizeof app_id - 1 &&
	      announced.title_len == sizeof title - 1,
	      "binary announcement parsed incorrectly");
	memcpy(hostile, message, sizeof hostile);
	uint8_t bad_announce[sizeof announce];
	memcpy(bad_announce, announce, sizeof bad_announce);
	bad_announce[QDMM_LOCAL_HEADER_SIZE + 28] = 0xff;
	CHECK(qdmm_parse_announcement(
	      bad_announce, sizeof bad_announce, &announced) < 0,
	      "hostile announcement length accepted");

	uint8_t qdni[QDMM_MAX_QDNI_PACKET_SIZE] = {0};
	write_le32(qdni, 0x49444e51u);
	qdni[4] = 1;
	qdni[5] = 3;
	write_le16(qdni + 6, 12);
	write_le32(qdni + 8, 123);
	write_le32(qdni + 12, 272);
	write_le32(qdni + 16, 1);
	uint8_t local_wire[QDMM_MAX_LOCAL_INPUT_WIRE_SIZE];
	size_t local_wire_len = 0;
	CHECK(qdmm_qdni_to_local(
	      qdni, sizeof qdni, local_wire, &local_wire_len) == 0,
	      "valid QDNI button rejected");
	CHECK(local_wire_len == 28 && local_wire[9] == QDMM_LOCAL_BUTTON,
	      "QDNI button did not become fixed local button");
	uint8_t qdni_roundtrip[QDMM_MAX_QDNI_PACKET_SIZE];
	size_t qdni_roundtrip_len = 0;
	CHECK(qdmm_local_to_qdni(
	      local_wire + 4, local_wire_len - 4, 456,
	      qdni_roundtrip, &qdni_roundtrip_len) == 0,
	      "local button did not convert back to QDNI");
	CHECK(qdni_roundtrip_len == sizeof qdni && qdni_roundtrip[5] == 3 &&
	      qdni_roundtrip[8] == 0xc8 && qdni_roundtrip[9] == 0x01 &&
	      qdni_roundtrip[12] == 0x10 && qdni_roundtrip[13] == 0x01 &&
	      qdni_roundtrip[16] == 1,
	      "QDNI button round trip fields differ");
	write_le32(qdni + 16, 2);
	CHECK(qdmm_qdni_to_local(
	      qdni, sizeof qdni, local_wire, &local_wire_len) < 0,
	      "non-boolean QDNI button state accepted");
	memset(qdni, 0, sizeof qdni);
	write_le32(qdni, 0x49444e51u);
	qdni[4] = 1;
	qdni[5] = 1;
	CHECK(qdmm_qdni_to_local(
	      qdni, 8, local_wire, &local_wire_len) == 1,
	      "QDNI ping was not ignored explicitly");

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

	puts("PASS: remote frame, announcement, QDNI, token, and ack bounds");
	return 0;
}
