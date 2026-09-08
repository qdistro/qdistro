#include "mm-remote-frame-protocol.h"

#include <arpa/inet.h>
#include <endian.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>

#define QDMM_MAX_DIMENSION 8192u
#define QDMM_MAX_TEXT_BYTES 4096u
#define QDMM_QDNI_MAGIC 0x49444e51u

static uint32_t
read_be32(const uint8_t *p)
{
	uint32_t value;
	memcpy(&value, p, sizeof value);
	return ntohl(value);
}

static uint16_t
read_be16(const uint8_t *p)
{
	uint16_t value;
	memcpy(&value, p, sizeof value);
	return ntohs(value);
}

static uint16_t
read_le16(const uint8_t *p)
{
	return (uint16_t)p[0] | (uint16_t)p[1] << 8;
}

static uint32_t
read_le32(const uint8_t *p)
{
	return (uint32_t)p[0] | (uint32_t)p[1] << 8 |
	       (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
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

static void
write_be32(uint8_t *p, uint32_t value)
{
	value = htonl(value);
	memcpy(p, &value, sizeof value);
}

static void
write_be16(uint8_t *p, uint16_t value)
{
	value = htons(value);
	memcpy(p, &value, sizeof value);
}

static void
write_le16(uint8_t *p, uint16_t value)
{
	p[0] = (uint8_t)value;
	p[1] = (uint8_t)(value >> 8);
}

static void
write_le32(uint8_t *p, uint32_t value)
{
	p[0] = (uint8_t)value;
	p[1] = (uint8_t)(value >> 8);
	p[2] = (uint8_t)(value >> 16);
	p[3] = (uint8_t)(value >> 24);
}

int
qdmm_parse_local_message(const uint8_t *message, size_t length,
			 struct qdmm_local_message_view *out)
{
	if (!message || !out || length < QDMM_LOCAL_HEADER_SIZE ||
	    length > QDMM_MAX_LOCAL_BYTES || memcmp(message, "QDML", 4) != 0 ||
	    message[4] != 1 || message[5] < QDMM_LOCAL_CONNECTED ||
	    message[5] > QDMM_LOCAL_IDENTITY ||
	    message[6] != 0 || message[7] != 0)
		return -1;
	out->kind = message[5];
	out->seq = read_be64(message + 8);
	out->payload = message + QDMM_LOCAL_HEADER_SIZE;
	out->payload_len = length - QDMM_LOCAL_HEADER_SIZE;
	return 0;
}

static int
identity_valid(const uint8_t *text, size_t length, int stream)
{
	if (!text || !length || length > 128)
		return 0;
	for (size_t i = 0; i < length; i++) {
		uint8_t c = text[i];
		int alpha_num = (c >= 'a' && c <= 'z') ||
			(c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
		if (!alpha_num && !(i > 0 && (c == '_' || c == '-' ||
		    (!stream && (c == '.' || c == ':')))))
			return 0;
	}
	return !stream || length >= 16;
}

int
qdmm_parse_identity(const uint8_t *message, size_t length,
		    struct qdmm_identity_view *out)
{
	struct qdmm_local_message_view local;
	if (!out || qdmm_parse_local_message(message, length, &local) < 0 ||
	    local.kind != QDMM_LOCAL_IDENTITY ||
	    local.payload_len < QDMM_LOCAL_IDENTITY_HEADER_SIZE)
		return -1;
	const uint8_t *identity = local.payload;
	if (memcmp(identity, "QDMI", 4) != 0 || identity[4] != 1 ||
	    identity[5] != 0)
		return -1;
	uint64_t generation = read_be64(identity + 6);
	size_t source_len = read_be16(identity + 14);
	size_t trust_len = read_be16(identity + 16);
	size_t stream_len = read_be16(identity + 18);
	if (!generation || source_len + trust_len + stream_len !=
	    local.payload_len - QDMM_LOCAL_IDENTITY_HEADER_SIZE)
		return -1;
	const uint8_t *source = identity + QDMM_LOCAL_IDENTITY_HEADER_SIZE;
	const uint8_t *trust = source + source_len;
	const uint8_t *stream = trust + trust_len;
	if (!identity_valid(source, source_len, 0) ||
	    !identity_valid(trust, trust_len, 0) ||
	    !identity_valid(stream, stream_len, 1))
		return -1;
	out->generation = generation;
	out->source_machine = source;
	out->source_machine_len = source_len;
	out->trust_domain_id = trust;
	out->trust_domain_id_len = trust_len;
	out->stream_id = stream;
	out->stream_id_len = stream_len;
	return 0;
}

static int
app_id_valid(const uint8_t *text, size_t length)
{
	if (!text || !length || length > 256)
		return 0;
	for (size_t i = 0; i < length; i++) {
		uint8_t c = text[i];
		if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
		      (c >= '0' && c <= '9') || (i > 0 &&
		      (c == '.' || c == '_' || c == '-'))))
			return 0;
	}
	return 1;
}

static int
utf8_text_valid(const uint8_t *text, size_t length)
{
	if (!text || length > QDMM_MAX_TEXT_BYTES)
		return 0;
	for (size_t i = 0; i < length;) {
		uint8_t c = text[i++];
		if (c < 0x80) {
			if (c < 0x20 || c == 0x7f)
				return 0;
			continue;
		}
		unsigned need;
		uint32_t code;
		if (c >= 0xc2 && c <= 0xdf) {
			need = 1; code = c & 0x1f;
		} else if (c >= 0xe0 && c <= 0xef) {
			need = 2; code = c & 0x0f;
		} else if (c >= 0xf0 && c <= 0xf4) {
			need = 3; code = c & 0x07;
		} else {
			return 0;
		}
		if (length - i < need)
			return 0;
		for (unsigned j = 0; j < need; j++) {
			uint8_t continuation = text[i++];
			if ((continuation & 0xc0) != 0x80)
				return 0;
			code = code << 6 | (continuation & 0x3f);
		}
		if ((need == 2 && code < 0x800) ||
		    (need == 3 && code < 0x10000) || code > 0x10ffff ||
		    (code >= 0xd800 && code <= 0xdfff))
			return 0;
	}
	return 1;
}

int
qdmm_parse_announcement(const uint8_t *message, size_t length,
			struct qdmm_announcement_view *out)
{
	struct qdmm_local_message_view local;
	if (!out || qdmm_parse_local_message(message, length, &local) < 0 ||
	    local.kind != QDMM_LOCAL_ANNOUNCE ||
	    local.payload_len < QDMM_LOCAL_ANNOUNCE_HEADER_SIZE)
		return -1;
	const uint8_t *a = local.payload;
	if (memcmp(a, "QDMA", 4) != 0 || a[4] != 1 || a[5] != 1 ||
	    a[6] != 0 || a[7] != 0)
		return -1;
	uint64_t revision = read_be64(a + 8);
	uint32_t width = read_be32(a + 16);
	uint32_t height = read_be32(a + 20);
	uint32_t stride = read_be32(a + 24);
	size_t app_len = read_be16(a + 28);
	size_t title_len = read_be16(a + 30);
	if (!revision || revision > INT64_MAX || !width || !height ||
	    width > QDMM_MAX_DIMENSION || height > QDMM_MAX_DIMENSION ||
	    stride != width * 4u || (uint64_t)stride * height +
	    QDMM_FRAME_HEADER_SIZE > QDMM_MAX_MEDIA_BYTES ||
	    app_len + title_len != local.payload_len -
	    QDMM_LOCAL_ANNOUNCE_HEADER_SIZE)
		return -1;
	const uint8_t *app_id = a + QDMM_LOCAL_ANNOUNCE_HEADER_SIZE;
	const uint8_t *title = app_id + app_len;
	if (!app_id_valid(app_id, app_len) ||
	    !utf8_text_valid(title, title_len))
		return -1;
	out->source_revision = revision;
	out->width = width;
	out->height = height;
	out->stride = stride;
	out->app_id = app_id;
	out->app_id_len = app_len;
	out->title = title;
	out->title_len = title_len;
	return 0;
}

static int
safe_ascii_component(const uint8_t *text, size_t length)
{
	if (!text || !length)
		return 0;
	for (size_t i = 0; i < length; i++) {
		uint8_t c = text[i];
		if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
		      (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-'))
			return 0;
	}
	return 1;
}

static int
pw_node_valid(const uint8_t *text, size_t length)
{
	static const char prefix[] = "weston.pipewire:";
	if (!text || length <= sizeof prefix - 1 || length > 128 ||
	    memcmp(text, prefix, sizeof prefix - 1) != 0)
		return 0;
	size_t at = sizeof prefix - 1;
	if (text[at] < '1' || text[at] > '9')
		return 0;
	size_t digits = 0;
	while (at < length && text[at] >= '0' && text[at] <= '9') {
		at++;
		digits++;
	}
	if (!digits || digits > 10 || at >= length || text[at++] != ':')
		return 0;
	return length - at <= 64 &&
	       safe_ascii_component(text + at, length - at);
}

static int
positive_decimal(const uint8_t *text, size_t length)
{
	if (!text || !length || text[0] < '1' || text[0] > '9')
		return 0;
	for (size_t i = 1; i < length; i++)
		if (text[i] < '0' || text[i] > '9')
			return 0;
	return 1;
}

static int
input_sink_valid(const uint8_t *text, size_t length)
{
	static const char prefix[] = "qdwin-nested-input-";
	static const char suffix[] = ".sock";
	if (!text || length < 2 || length > 107 || text[0] != '/')
		return 0;
	for (size_t i = 0; i + 2 < length; i++)
		if (text[i] == '/' && text[i + 1] == '.' && text[i + 2] == '.' &&
		    (i + 3 == length || text[i + 3] == '/'))
			return 0;
	const uint8_t *slash = text;
	for (size_t i = 0; i < length; i++)
		if (text[i] == '/') slash = text + i + 1;
	size_t basename_len = length - (size_t)(slash - text);
	if (basename_len <= sizeof prefix + sizeof suffix - 2 ||
	    memcmp(slash, prefix, sizeof prefix - 1) != 0 ||
	    memcmp(text + length - (sizeof suffix - 1),
		   suffix, sizeof suffix - 1) != 0)
		return 0;
	const uint8_t *numbers = slash + sizeof prefix - 1;
	size_t numbers_len = basename_len - (sizeof prefix - 1) -
		(sizeof suffix - 1);
	const uint8_t *dash = memchr(numbers, '-', numbers_len);
	if (!dash)
		return 0;
	return positive_decimal(numbers, (size_t)(dash - numbers)) &&
	       positive_decimal(dash + 1,
		numbers_len - (size_t)(dash + 1 - numbers));
}

int
qdmm_parse_source_config(const uint8_t *config, size_t length,
			 struct qdmm_source_config_view *out)
{
	if (!config || !out || length < QDMM_SOURCE_CONFIG_HEADER_SIZE ||
	    memcmp(config, "QDMS", 4) != 0 || config[4] != 1 ||
	    config[5] != 0 || config[6] != 0 || config[7] != 0)
		return -1;
	uint64_t revision = read_be64(config + 8);
	size_t pw_len = read_be16(config + 16);
	size_t input_len = read_be16(config + 18);
	size_t app_len = read_be16(config + 20);
	size_t title_len = read_be16(config + 22);
	if (!revision || revision > INT64_MAX ||
	    pw_len + input_len + app_len + title_len !=
	    length - QDMM_SOURCE_CONFIG_HEADER_SIZE)
		return -1;
	const uint8_t *pw_node = config + QDMM_SOURCE_CONFIG_HEADER_SIZE;
	const uint8_t *input_sink = pw_node + pw_len;
	const uint8_t *app_id = input_sink + input_len;
	const uint8_t *title = app_id + app_len;
	if (!pw_node_valid(pw_node, pw_len) ||
	    !input_sink_valid(input_sink, input_len) ||
	    !app_id_valid(app_id, app_len) ||
	    !utf8_text_valid(title, title_len))
		return -1;
	out->source_revision = revision;
	out->pw_node = pw_node;
	out->pw_node_len = pw_len;
	out->input_sink = input_sink;
	out->input_sink_len = input_len;
	out->app_id = app_id;
	out->app_id_len = app_len;
	out->title = title;
	out->title_len = title_len;
	return 0;
}

int
qdmm_build_announcement_wire(
	const struct qdmm_source_config_view *config,
	uint32_t width, uint32_t height, uint32_t stride,
	uint8_t *out, size_t out_size, size_t *out_len)
{
	if (!config || !out || !out_len || !config->source_revision ||
	    !width || !height || width > QDMM_MAX_DIMENSION ||
	    height > QDMM_MAX_DIMENSION || stride != width * 4u ||
	    (uint64_t)stride * height + QDMM_FRAME_HEADER_SIZE >
	    QDMM_MAX_MEDIA_BYTES || config->app_id_len > UINT16_MAX ||
	    config->title_len > UINT16_MAX)
		return -1;
	size_t payload_len = QDMM_LOCAL_ANNOUNCE_HEADER_SIZE +
		config->app_id_len + config->title_len;
	size_t message_len = QDMM_LOCAL_HEADER_SIZE + payload_len;
	if (out_size < 4 + message_len)
		return -1;
	write_be32(out, (uint32_t)message_len);
	memcpy(out + 4, "QDML", 4);
	out[8] = 1;
	out[9] = QDMM_LOCAL_ANNOUNCE;
	out[10] = 0;
	out[11] = 0;
	write_be64(out + 12, config->source_revision);
	uint8_t *a = out + 4 + QDMM_LOCAL_HEADER_SIZE;
	memcpy(a, "QDMA", 4);
	a[4] = 1;
	a[5] = 1;
	a[6] = 0;
	a[7] = 0;
	write_be64(a + 8, config->source_revision);
	write_be32(a + 16, width);
	write_be32(a + 20, height);
	write_be32(a + 24, stride);
	write_be16(a + 28, (uint16_t)config->app_id_len);
	write_be16(a + 30, (uint16_t)config->title_len);
	memcpy(a + QDMM_LOCAL_ANNOUNCE_HEADER_SIZE,
	       config->app_id, config->app_id_len);
	memcpy(a + QDMM_LOCAL_ANNOUNCE_HEADER_SIZE + config->app_id_len,
	       config->title, config->title_len);
	*out_len = 4 + message_len;
	return 0;
}

int
qdmm_build_frame_wire(uint64_t seq,
	uint32_t width, uint32_t height, uint32_t stride,
	const uint8_t *pixels, size_t pixels_len,
	uint8_t *out, size_t out_size, size_t *out_len)
{
	if (!seq || !width || !height || width > QDMM_MAX_DIMENSION ||
	    height > QDMM_MAX_DIMENSION || stride != width * 4u || !pixels ||
	    pixels_len != (uint64_t)stride * height ||
	    pixels_len + QDMM_FRAME_HEADER_SIZE > QDMM_MAX_MEDIA_BYTES)
		return -1;
	size_t message_len = QDMM_LOCAL_HEADER_SIZE + QDMM_FRAME_HEADER_SIZE +
		pixels_len;
	if (!out || !out_len || out_size < 4 + message_len)
		return -1;
	write_be32(out, (uint32_t)message_len);
	memcpy(out + 4, "QDML", 4);
	out[8] = 1;
	out[9] = QDMM_LOCAL_FRAME;
	out[10] = 0;
	out[11] = 0;
	write_be64(out + 12, seq);
	uint8_t *frame = out + 4 + QDMM_LOCAL_HEADER_SIZE;
	memcpy(frame, "QDMF", 4);
	frame[4] = 1;
	frame[5] = 1;
	frame[6] = 0;
	frame[7] = 0;
	write_be32(frame + 8, width);
	write_be32(frame + 12, height);
	write_be32(frame + 16, stride);
	write_be32(frame + 20, (uint32_t)pixels_len);
	memcpy(frame + QDMM_FRAME_HEADER_SIZE, pixels, pixels_len);
	*out_len = 4 + message_len;
	return 0;
}

int
qdmm_parse_frame(const uint8_t *message, size_t length,
		 struct qdmm_frame_view *out)
{
	struct qdmm_local_message_view local;
	if (!out || qdmm_parse_local_message(message, length, &local) < 0 ||
	    local.kind != QDMM_LOCAL_FRAME ||
	    local.payload_len < QDMM_FRAME_HEADER_SIZE)
		return -1;
	const uint8_t *frame = local.payload;
	size_t frame_len = local.payload_len;
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
	out->seq = local.seq;
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
	out[9] = QDMM_LOCAL_DECODER_ACK;
	out[10] = 0;
	out[11] = 0;
	write_be64(out + 12, seq);
	return 0;
}

static int
build_local_input(uint8_t kind, const uint8_t *payload, size_t payload_len,
		  uint8_t out[QDMM_MAX_LOCAL_INPUT_WIRE_SIZE], size_t *out_len)
{
	if (!out || !out_len || payload_len > 8)
		return -1;
	write_be32(out, QDMM_LOCAL_HEADER_SIZE + (uint32_t)payload_len);
	memcpy(out + 4, "QDML", 4);
	out[8] = 1;
	out[9] = kind;
	out[10] = 0;
	out[11] = 0;
	memset(out + 12, 0, 8);
	memcpy(out + 20, payload, payload_len);
	*out_len = 4 + QDMM_LOCAL_HEADER_SIZE + payload_len;
	return 0;
}

int
qdmm_qdni_to_local(const uint8_t *packet, size_t length,
		   uint8_t out[QDMM_MAX_LOCAL_INPUT_WIRE_SIZE], size_t *out_len)
{
	if (!packet || !out || !out_len || length < 8 ||
	    read_le32(packet) != QDMM_QDNI_MAGIC || packet[4] != 1 ||
	    read_le16(packet + 6) != length - 8)
		return -1;
	uint8_t payload[8] = {0};
	switch (packet[5]) {
	case 1:
		return length == 8 ? 1 : -1;
	case 2:
		if (length != 20) return -1;
		write_be32(payload, read_le32(packet + 12));
		write_be32(payload + 4, read_le32(packet + 16));
		return build_local_input(
			QDMM_LOCAL_MOTION, payload, 8, out, out_len);
	case 3:
	case 4: {
		if (length != 20) return -1;
		uint32_t code = read_le32(packet + 12);
		uint32_t pressed = read_le32(packet + 16);
		if (!code || code > 0xffff || pressed > 1) return -1;
		write_be32(payload, code);
		payload[4] = (uint8_t)pressed;
		return build_local_input(
			packet[5] == 3 ? QDMM_LOCAL_BUTTON : QDMM_LOCAL_KEY,
			payload, 8, out, out_len);
	}
	case 5: {
		if (length != 20) return -1;
		uint32_t axis = read_le32(packet + 12);
		if (axis > 1) return -1;
		write_be32(payload, axis);
		write_be32(payload + 4, read_le32(packet + 16));
		return build_local_input(
			QDMM_LOCAL_AXIS, payload, 8, out, out_len);
	}
	case 6: {
		if (length != 12) return -1;
		uint32_t focused = read_le32(packet + 8);
		if (focused > 1) return -1;
		payload[0] = (uint8_t)focused;
		return build_local_input(
			QDMM_LOCAL_FOCUS, payload, 4, out, out_len);
	}
	default:
		return -1;
	}
}

int
qdmm_local_to_qdni(const uint8_t *message, size_t length,
		   uint32_t time_msec,
		   uint8_t out[QDMM_MAX_QDNI_PACKET_SIZE], size_t *out_len)
{
	struct qdmm_local_message_view local;
	if (!out || !out_len ||
	    qdmm_parse_local_message(message, length, &local) < 0 || local.seq)
		return -1;
	uint8_t event_type;
	uint16_t payload_len;
	switch (local.kind) {
	case QDMM_LOCAL_MOTION:
		event_type = 2; payload_len = 12;
		break;
	case QDMM_LOCAL_BUTTON:
		event_type = 3; payload_len = 12;
		break;
	case QDMM_LOCAL_KEY:
		event_type = 4; payload_len = 12;
		break;
	case QDMM_LOCAL_AXIS:
		event_type = 5; payload_len = 12;
		break;
	case QDMM_LOCAL_FOCUS:
		event_type = 6; payload_len = 4;
		break;
	default:
		return -1;
	}
	size_t expected = local.kind == QDMM_LOCAL_FOCUS ? 4 : 8;
	if (local.payload_len != expected)
		return -1;
	if ((local.kind == QDMM_LOCAL_BUTTON || local.kind == QDMM_LOCAL_KEY) &&
	    (read_be32(local.payload) == 0 || read_be32(local.payload) > 0xffff ||
	     local.payload[4] > 1 || local.payload[5] || local.payload[6] ||
	     local.payload[7]))
		return -1;
	if (local.kind == QDMM_LOCAL_AXIS && read_be32(local.payload) > 1)
		return -1;
	if (local.kind == QDMM_LOCAL_FOCUS &&
	    (local.payload[0] > 1 || local.payload[1] || local.payload[2] ||
	     local.payload[3]))
		return -1;
	write_le32(out, QDMM_QDNI_MAGIC);
	out[4] = 1;
	out[5] = event_type;
	write_le16(out + 6, payload_len);
	if (event_type == 6) {
		write_le32(out + 8, local.payload[0]);
	} else {
		write_le32(out + 8, time_msec);
		write_le32(out + 12, read_be32(local.payload));
		if (event_type == 2 || event_type == 5)
			write_le32(out + 16, read_be32(local.payload + 4));
		else
			write_le32(out + 16, local.payload[4]);
	}
	*out_len = 8 + payload_len;
	return 0;
}
