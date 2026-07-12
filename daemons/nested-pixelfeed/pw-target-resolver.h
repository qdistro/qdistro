#ifndef QDISTRO_PW_TARGET_RESOLVER_H
#define QDISTRO_PW_TARGET_RESOLVER_H

#include <stddef.h>
#include <stdint.h>

struct qdpf_pw_client_observation {
	uint32_t id;
	int pid;
	int pid_known;
};

struct qdpf_pw_node_observation {
	uint32_t id;
	uint32_t client_id;
	uint64_t serial;
};

struct qdpf_pw_target {
	uint32_t client_id;
	uint32_t node_id;
	uint64_t serial;
};

enum qdpf_pw_resolve_result {
	QDPF_PW_RESOLVE_OK = 0,
	QDPF_PW_RESOLVE_NO_CLIENT,
	QDPF_PW_RESOLVE_AMBIGUOUS_CLIENT,
	QDPF_PW_RESOLVE_NO_NODE,
	QDPF_PW_RESOLVE_AMBIGUOUS_NODE,
};

int qdpf_parse_positive_pid(const char *text, int *pid_out);
int qdpf_parse_u32(const char *text, uint32_t *value_out);
int qdpf_parse_u64(const char *text, uint64_t *value_out);

enum qdpf_pw_resolve_result qdpf_resolve_pw_target(
	int producer_pid,
	const struct qdpf_pw_client_observation *clients,
	size_t client_count,
	const struct qdpf_pw_node_observation *nodes,
	size_t node_count,
	struct qdpf_pw_target *target_out);

#endif
