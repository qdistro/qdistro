#include "pw-target-resolver.h"

#include <errno.h>
#include <limits.h>
#include <stdlib.h>

static int
parse_unsigned(const char *text, uint64_t maximum, uint64_t *value_out)
{
	char *end = NULL;
	unsigned long long value;

	if (!text || !*text || *text == '-' || !value_out)
		return -1;
	errno = 0;
	value = strtoull(text, &end, 10);
	if (errno != 0 || !end || *end != '\0' || value > maximum)
		return -1;
	*value_out = (uint64_t)value;
	return 0;
}

int
qdpf_parse_positive_pid(const char *text, int *pid_out)
{
	uint64_t value;

	if (!pid_out || parse_unsigned(text, INT_MAX, &value) != 0 || value == 0)
		return -1;
	*pid_out = (int)value;
	return 0;
}

int
qdpf_parse_u32(const char *text, uint32_t *value_out)
{
	uint64_t value;

	if (!value_out || parse_unsigned(text, UINT32_MAX, &value) != 0)
		return -1;
	*value_out = (uint32_t)value;
	return 0;
}

int
qdpf_parse_u64(const char *text, uint64_t *value_out)
{
	return parse_unsigned(text, UINT64_MAX, value_out);
}

enum qdpf_pw_resolve_result
qdpf_resolve_pw_target(
	int producer_pid,
	const struct qdpf_pw_client_observation *clients,
	size_t client_count,
	const struct qdpf_pw_node_observation *nodes,
	size_t node_count,
	struct qdpf_pw_target *target_out)
{
	const struct qdpf_pw_client_observation *matched_client = NULL;
	const struct qdpf_pw_node_observation *matched_node = NULL;

	if (!clients || !nodes || !target_out || producer_pid <= 0)
		return QDPF_PW_RESOLVE_NO_CLIENT;

	for (size_t i = 0; i < client_count; i++) {
		if (!clients[i].pid_known || clients[i].pid != producer_pid)
			continue;
		if (matched_client && matched_client->id != clients[i].id)
			return QDPF_PW_RESOLVE_AMBIGUOUS_CLIENT;
		matched_client = &clients[i];
	}
	if (!matched_client)
		return QDPF_PW_RESOLVE_NO_CLIENT;

	for (size_t i = 0; i < node_count; i++) {
		if (nodes[i].client_id != matched_client->id || nodes[i].serial == 0)
			continue;
		if (matched_node && (matched_node->id != nodes[i].id ||
				     matched_node->serial != nodes[i].serial))
			return QDPF_PW_RESOLVE_AMBIGUOUS_NODE;
		matched_node = &nodes[i];
	}
	if (!matched_node)
		return QDPF_PW_RESOLVE_NO_NODE;

	target_out->client_id = matched_client->id;
	target_out->node_id = matched_node->id;
	target_out->serial = matched_node->serial;
	return QDPF_PW_RESOLVE_OK;
}
