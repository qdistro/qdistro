#include "pw-target-resolver.h"

#include <stdint.h>
#include <stdio.h>

static int failures;

#define CHECK(condition, message) do { \
	if (!(condition)) { \
		fprintf(stderr, "FAIL: %s\n", message); \
		failures++; \
	} \
} while (0)

static void
test_unique_target(void)
{
	const struct qdpf_pw_client_observation clients[] = {
		{ .id = 8, .pid = 111, .pid_known = 1 },
		{ .id = 9, .pid = 222, .pid_known = 1 },
	};
	const struct qdpf_pw_node_observation nodes[] = {
		{ .id = 20, .client_id = 8, .serial = 30 },
		{ .id = 21, .client_id = 9, .serial = 31 },
	};
	struct qdpf_pw_target target = {0};

	CHECK(qdpf_resolve_pw_target(222, clients, 2, nodes, 2, &target) ==
	      QDPF_PW_RESOLVE_OK,
	      "unique producer PID/client/node resolves");
	CHECK(target.client_id == 9 && target.node_id == 21 && target.serial == 31,
	      "resolver returns the selected client, node, and serial");
}

static void
test_fail_closed_ambiguity(void)
{
	const struct qdpf_pw_client_observation duplicate_pid[] = {
		{ .id = 8, .pid = 222, .pid_known = 1 },
		{ .id = 9, .pid = 222, .pid_known = 1 },
	};
	const struct qdpf_pw_client_observation client[] = {
		{ .id = 9, .pid = 222, .pid_known = 1 },
	};
	const struct qdpf_pw_node_observation one_node[] = {
		{ .id = 21, .client_id = 9, .serial = 31 },
	};
	const struct qdpf_pw_node_observation duplicate_node[] = {
		{ .id = 21, .client_id = 9, .serial = 31 },
		{ .id = 22, .client_id = 9, .serial = 32 },
	};
	struct qdpf_pw_target target = {0};

	CHECK(qdpf_resolve_pw_target(222, duplicate_pid, 2, one_node, 1,
				     &target) == QDPF_PW_RESOLVE_AMBIGUOUS_CLIENT,
	      "two PipeWire clients claiming the producer PID fail closed");
	CHECK(qdpf_resolve_pw_target(222, client, 1, duplicate_node, 2,
				     &target) == QDPF_PW_RESOLVE_AMBIGUOUS_NODE,
	      "two target-name nodes owned by the producer fail closed");
}

static void
test_missing_and_invalid_observations(void)
{
	const struct qdpf_pw_client_observation unknown_pid[] = {
		{ .id = 9, .pid = 222, .pid_known = 0 },
	};
	const struct qdpf_pw_client_observation client[] = {
		{ .id = 9, .pid = 222, .pid_known = 1 },
	};
	const struct qdpf_pw_node_observation zero_serial[] = {
		{ .id = 21, .client_id = 9, .serial = 0 },
	};
	struct qdpf_pw_target target = {0};

	CHECK(qdpf_resolve_pw_target(222, unknown_pid, 1, zero_serial, 1,
				     &target) == QDPF_PW_RESOLVE_NO_CLIENT,
	      "client without a valid PID is not selected");
	CHECK(qdpf_resolve_pw_target(222, client, 1, zero_serial, 1,
				     &target) == QDPF_PW_RESOLVE_NO_NODE,
	      "zero object.serial is not a usable PipeWire target");
}

static void
test_numeric_parsing_and_large_serial(void)
{
	uint32_t u32;
	uint64_t u64;
	int pid;
	const struct qdpf_pw_client_observation client[] = {
		{ .id = 9, .pid = 222, .pid_known = 1 },
	};
	const struct qdpf_pw_node_observation node[] = {
		{ .id = 21, .client_id = 9, .serial = UINT64_C(4294967297) },
	};
	struct qdpf_pw_target target = {0};

	CHECK(qdpf_parse_positive_pid("222", &pid) == 0 && pid == 222,
	      "valid positive PID parses");
	CHECK(qdpf_parse_positive_pid("0", &pid) != 0 &&
	      qdpf_parse_positive_pid("12x", &pid) != 0,
	      "zero and trailing-junk PIDs are rejected");
	CHECK(qdpf_parse_u32("4294967295", &u32) == 0 && u32 == UINT32_MAX,
	      "maximum uint32 parses");
	CHECK(qdpf_parse_u32("4294967296", &u32) != 0,
	      "overflowing uint32 is rejected");
	CHECK(qdpf_parse_u64("4294967297", &u64) == 0 &&
	      u64 == UINT64_C(4294967297),
	      "PipeWire object.serial is retained above 32 bits");
	CHECK(qdpf_resolve_pw_target(222, client, 1, node, 1, &target) ==
	      QDPF_PW_RESOLVE_OK && target.serial == UINT64_C(4294967297),
	      "resolver does not truncate a 64-bit object.serial");
}

int
main(void)
{
	test_unique_target();
	test_fail_closed_ambiguity();
	test_missing_and_invalid_observations();
	test_numeric_parsing_and_large_serial();
	if (failures)
		return 1;
	puts("pw-target-resolver: all tests passed");
	return 0;
}
