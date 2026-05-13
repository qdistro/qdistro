/*
 * qdistro-tier1-exec — set the SELinux exec-context on this process
 * tree to qdistro_tier1_t and exec the inner command.
 *
 * Per spec/30 §"Decision: custom policy module + setexeccon wrapper",
 * Tier-1 layers TWO independent attestations of the same identity:
 *
 *   - SELinux type qdistro_tier1_t  (this wrapper)
 *   - wp_security_context_v1 tag    (qdistro-secctx-exec, called
 *                                    one level above this in the
 *                                    qdistro-tier1-spawn pipeline)
 *
 * Usage (from qdistro-tier1-spawn after secctx-exec wraps us):
 *   qdistro-tier1-exec [--context CTX] -- <cmd> [args...]
 *
 * Default --context is computed from the calling process's user +
 * role + level via getcon, swapping the type to qdistro_tier1_t. This
 * works on both Tumbleweed (unconfined_u/unconfined_r) and Fedora
 * (staff_u/staff_r after `semanage login`). Override via env
 * QDISTRO_TIER1_EXEC_CONTEXT or --context for testing.
 *
 * Exit codes:
 *   0   exec succeeded (never returned).
 *   2   bad argv / no command.
 *   3   SELinux disabled or context derivation failed.
 *   4   setexeccon() failed.
 *   5   execvp() failed.
 *
 * Design notes:
 *
 *   - The .fc labels this binary qdistro_tier1_exec_t, and the .te
 *     declares domain_auto_trans({unconfined_t,staff_t},
 *     qdistro_tier1_exec_t, qdistro_tier1_t). When the kernel honors
 *     that auto_trans, we are ALREADY in qdistro_tier1_t before
 *     main() runs, and qdistro_tier1_t is not allowed
 *     selinux_t:context_validate. So we must NOT call
 *     security_check_context() on a target context: it'd EACCES under
 *     enforcing and fail-close even when the auto_trans already did
 *     the right thing. Instead, getcon → if we're already in
 *     qdistro_tier1_t, just exec; otherwise setexeccon + exec for
 *     callers without the auto_trans rule (system_r, etc.).
 *
 *   - We deliberately do NOT call setcon() (set the *current* domain).
 *     That would require manage permissions in the calling domain
 *     and isn't needed for the launcher pattern; setexeccon arms
 *     the context for the next exec only, and the type_transition
 *     in qdistro_tier1.te does the same job through declarative
 *     policy. We keep both for layered defense: if setexeccon were
 *     stripped (LD_PRELOAD shadowing libselinux), auto_trans still
 *     fires; if .te wasn't loaded but the wrapper's caller is
 *     unconfined, setexeccon makes the transition explicit.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <selinux/context.h>
#include <selinux/selinux.h>

#define DEFAULT_TYPE "qdistro_tier1_t"

/* Build the target context by inheriting the current process's
 * user + role + level and substituting the type. This avoids
 * hard-coding staff_u/staff_r which works on Fedora's "qdistro
 * deployment migrated to staff_u via semanage login" path but not
 * on stock Tumbleweed where the __default__ login mapping leaves
 * everyone as unconfined_u.
 *
 * Returns a malloc'd string the caller must freecon(). NULL on error.
 */
static char *
context_with_type(const char *new_type)
{
	char *cur = NULL;
	if (getcon(&cur) != 0 || !cur) return NULL;
	context_t ctx = context_new(cur);
	freecon(cur);
	if (!ctx) return NULL;
	if (context_type_set(ctx, new_type) != 0) {
		context_free(ctx);
		return NULL;
	}
	const char *s = context_str(ctx);
	if (!s) { context_free(ctx); return NULL; }
	char *out = strdup(s);
	context_free(ctx);
	return out;
}

static void
usage(const char *prog)
{
	fprintf(stderr,
		"usage: %s [--context CTX] -- <cmd> [args...]\n"
		"\n"
		"Set the SELinux exec context and exec the given command.\n"
		"The default context inherits the current process's user +\n"
		"role + level, swapping the type to %s (the type that\n"
		"qdistro_tier1.te declares). CTX can be overridden via\n"
		"$QDISTRO_TIER1_EXEC_CONTEXT to a fully-formed\n"
		"user:role:type:level string.\n",
		prog, DEFAULT_TYPE);
}

int
main(int argc, char **argv)
{
	const char *override_context = getenv("QDISTRO_TIER1_EXEC_CONTEXT");
	if (override_context && !*override_context) override_context = NULL;

	static const struct option opts[] = {
		{ "context", required_argument, NULL, 'c' },
		{ "help",    no_argument,       NULL, 'h' },
		{ NULL, 0, NULL, 0 }
	};
	int o;
	/* getopt_long stops at the first non-option. We set `+` to enforce
	 * POSIXLY_CORRECT-style argument ordering: -- terminates options. */
	while ((o = getopt_long(argc, argv, "+c:h", opts, NULL)) != -1) {
		switch (o) {
		case 'c': override_context = optarg; break;
		case 'h': usage(argv[0]); return 0;
		default:  usage(argv[0]); return 2;
		}
	}
	if (optind >= argc) {
		usage(argv[0]);
		return 2;
	}

	if (!is_selinux_enabled()) {
		fprintf(stderr,
			"qdistro-tier1-exec: SELinux disabled; refusing to "
			"exec — fail-closed (set qdistro_tier1_t cannot be "
			"applied without SELinux).\n");
		return 3;
	}
	/* Two pathways land the inner command in qdistro_tier1_t:
	 *
	 *  (A) auto_trans: this binary is labelled qdistro_tier1_exec_t
	 *      (see .fc), and the policy declares
	 *        domain_auto_trans(unconfined_t, qdistro_tier1_exec_t,
	 *                          qdistro_tier1_t).
	 *      So when an unconfined_t caller execs this binary, the
	 *      kernel auto-transitions us to qdistro_tier1_t before main()
	 *      runs. The inner exec stays in qdistro_tier1_t (corecmd_
	 *      exec_bin / corecmd_exec_shell). No userspace work needed.
	 *
	 *  (B) setexeccon explicit: when we're NOT already in
	 *      qdistro_tier1_t (e.g. caller is some other domain that
	 *      doesn't have the auto_trans, or the binary is being run
	 *      from an unlabelled path), we set the exec context for the
	 *      next execvp.
	 *
	 * The earlier code did (B) unconditionally + a security_check_
	 * context() probe. Under enforcing mode that probe runs in
	 * qdistro_tier1_t (because (A) already fired), and qdistro_tier1_t
	 * is not allowed `selinux_t:security check_context` — so the
	 * probe is denied and the wrapper fails closed even though (A)
	 * has already done the right thing.
	 *
	 * The new logic: getcon — if we're already in qdistro_tier1_t,
	 * just exec. If not, setexeccon and exec.
	 */
	char *current_ctx = NULL;
	if (getcon(&current_ctx) != 0 || !current_ctx) {
		fprintf(stderr,
			"qdistro-tier1-exec: getcon failed: %s\n",
			strerror(errno));
		return 3;
	}
	context_t cur_parsed = context_new(current_ctx);
	if (!cur_parsed) {
		fprintf(stderr,
			"qdistro-tier1-exec: context_new(%s) failed\n",
			current_ctx);
		freecon(current_ctx);
		return 3;
	}
	const char *cur_type = context_type_get(cur_parsed);
	int already_in_target = (cur_type && !strcmp(cur_type, DEFAULT_TYPE));
	context_free(cur_parsed);

	char *computed = NULL;
	const char *context = override_context;
	if (!already_in_target && !context) {
		computed = context_with_type(DEFAULT_TYPE);
		if (!computed) {
			fprintf(stderr,
				"qdistro-tier1-exec: context_new failed — "
				"refusing to fall back to a hard-coded "
				"context (would be wrong on most installs)\n");
			freecon(current_ctx);
			return 3;
		}
		context = computed;
	}
	if (!already_in_target) {
		if (setexeccon(context) != 0) {
			int saved = errno;
			/* Fallback: the computed context inherited the
			 * caller's user/role, but those may not have a
			 * binding to qdistro_tier1_t (e.g. caller is
			 * virt_qemu_ga_t in system_r — only staff_r /
			 * unconfined_r have the binding per the .te).
			 * Try unconfined_u:unconfined_r — the canonical
			 * Tumbleweed admin login context that matches the
			 * .te role allow. */
			const char *fallback =
				"unconfined_u:unconfined_r:qdistro_tier1_t:s0";
			if (override_context) {
				fprintf(stderr,
					"qdistro-tier1-exec: setexeccon(%s) "
					"from %s failed: %s\n",
					context, current_ctx,
					strerror(saved));
				free(computed);
				freecon(current_ctx);
				return 4;
			}
			if (setexeccon(fallback) != 0) {
				int saved2 = errno;
				fprintf(stderr,
					"qdistro-tier1-exec: setexeccon "
					"computed=%s and fallback=%s both "
					"failed (%s / %s) from caller %s\n",
					context, fallback,
					strerror(saved), strerror(saved2),
					current_ctx);
				free(computed);
				freecon(current_ctx);
				return 4;
			}
			fprintf(stderr,
				"qdistro-tier1-exec: computed context %s "
				"rejected (%s); fell back to %s\n",
				context, strerror(saved), fallback);
		}
	}
	freecon(current_ctx); current_ctx = NULL;
	free(computed); computed = NULL;
	execvp(argv[optind], argv + optind);
	/* execvp returns only on failure. */
	fprintf(stderr,
		"qdistro-tier1-exec: execvp(%s) failed: %s\n",
		argv[optind], strerror(errno));
	return 5;
}
