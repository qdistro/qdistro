/*
 * qdwin-secctx-probe — test client for the wp_security_context_manager_v1
 * bind gate (bind_qdwin_secctx_manager, qdwin.c). Verifies the Option-A
 * launcher-gate from todo/decisions/secctx-identity-contract.md:
 *
 *   The compositor must REFUSE a manager bind from a generic same-uid client,
 *   and must ACCEPT it (allowing a listener commit) from the bound shell
 *   client. Production also admits the installed qdistro-secctx-exec helper;
 *   this source-tree probe is intentionally not that helper.
 *
 * Modes:
 *   (default)         bind the manager as an ordinary non-shell client,
 *                     report whether the bind was accepted or refused.
 *   --commit          on a successful bind, also create_listener + set the
 *                     secctx strings + commit, and report whether that
 *                     round-trips cleanly. This is the "can commit a
 *                     listener" half of the bind-gate item.
 *   --as-shell        first bind qdwin_shell_v1 and claim the shell role on
 *                     the same wl_client, then bind the manager. This drives
 *                     the default positive path after same-uid manager binds
 *                     were removed.
 *
 * Identity-edge modes (08-secctx-identity-edges.md) — each binds the
 * manager, create_listener's, then exercises one commit-sequence edge and
 * asserts qdwin's ACTUAL behavior (read from the qdwin_secctx setters and
 * qdwin_secctx_commit):
 *   --commit-no-appid commit with engine + instance_id but NO app_id. A
 *                     valid sequence: qdwin has no required-field check and
 *                     accepts it (and logs app_id=?). Asserts clean commit.
 *   --commit-bare     commit with NO setters at all (engine/app_id/
 *                     instance_id all unset). Also accepted; qdwin logs all
 *                     three as "?". Asserts clean commit.
 *   --commit-engine-only  set sandbox_engine only, then commit. Accepted.
 *   --double-commit   commit, then commit again on the same context. The
 *                     context is single-use: qdwin posts the protocol error
 *                     already_used (=1) on the second commit. Asserts that.
 *   --set-after-commit commit, then set_app_id. qdwin posts already_used
 *                     (=1) on the post-commit setter. Asserts that.
 *   --commit-close-destroy  regression for the secctx listener
 *                     use-after-free: commit, close the close_fd (drives
 *                     the compositor's HANGUP teardown that frees the
 *                     listener), then destroy the context resource. On a
 *                     vulnerable build this crashes the compositor; the
 *                     probe asserts the connection survives. Exit 0 =
 *                     survived, 1 = connection died (crash).
 *
 * Exit codes:
 *   0  expected-accept path completed cleanly (bind / commit round-tripped)
 *   4  default bind run: the manager bind was REFUSED with the expected
 *      implementation error on wl_display (the PASS signal for the
 *      unauthorized case in 06-secctx-bind-gate.md)
 *   1  the expected accept failed, OR the bind failed with an UNEXPECTED
 *      error (wrong interface/code) — always a FAILURE
 *   3  --double-commit / --set-after-commit: the second op was rejected
 *      with the expected already_used error on wp_security_context_v1
 *      (the PASS signal)
 *   2  setup/other error (no display, manager global not advertised, ...)
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <sys/socket.h>
#include <sys/un.h>

#include <wayland-client.h>
#include "qdwin-shell-v1-client-protocol.h"
#include "security-context-v1-client-protocol.h"

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;
	struct qdwin_shell_v1 *shell;
	struct wp_security_context_manager_v1 *mgr;
	int saw_manager;
	int saw_shell;
	uint32_t shell_name;
	uint32_t shell_version;
	uint32_t manager_name;
	uint32_t manager_version;
};

static void
shell_hello(void *data, struct qdwin_shell_v1 *shell, uint32_t uid)
{ (void)data; (void)shell; (void)uid; }

static void
shell_toplevel_added(void *data, struct qdwin_shell_v1 *shell,
		     uint32_t handle, uint32_t owner_uid,
		     const char *app_id, const char *title, uint32_t is_xwayland)
{
	(void)data; (void)shell; (void)handle; (void)owner_uid;
	(void)app_id; (void)title; (void)is_xwayland;
}

static void
shell_toplevel_geometry(void *data, struct qdwin_shell_v1 *shell,
			uint32_t handle, int32_t x, int32_t y,
			uint32_t width, uint32_t height)
{
	(void)data; (void)shell; (void)handle; (void)x; (void)y;
	(void)width; (void)height;
}

static void
shell_toplevel_state(void *data, struct qdwin_shell_v1 *shell,
		     uint32_t handle, uint32_t state)
{ (void)data; (void)shell; (void)handle; (void)state; }

static void
shell_toplevel_title(void *data, struct qdwin_shell_v1 *shell,
		     uint32_t handle, const char *title)
{ (void)data; (void)shell; (void)handle; (void)title; }

static void
shell_toplevel_removed(void *data, struct qdwin_shell_v1 *shell,
		       uint32_t handle)
{ (void)data; (void)shell; (void)handle; }

static void
shell_locked_changed(void *data, struct qdwin_shell_v1 *shell, uint32_t locked)
{ (void)data; (void)shell; (void)locked; }

static const struct qdwin_shell_v1_listener shell_listener = {
	.hello = shell_hello,
	.toplevel_added = shell_toplevel_added,
	.toplevel_geometry = shell_toplevel_geometry,
	.toplevel_state = shell_toplevel_state,
	.toplevel_title = shell_toplevel_title,
	.toplevel_removed = shell_toplevel_removed,
	.locked_changed = shell_locked_changed,
};

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct probe *p = data;
	(void)reg;

	if (strcmp(interface,
		   wp_security_context_manager_v1_interface.name) == 0) {
		p->saw_manager = 1;
		p->manager_name = name;
		p->manager_version = version;
	} else if (strcmp(interface, qdwin_shell_v1_interface.name) == 0) {
		p->saw_shell = 1;
		p->shell_name = name;
		p->shell_version = version;
	}
}

static void
on_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{ (void)data; (void)reg; (void)name; }

static const struct wl_registry_listener registry_listener = {
	.global = on_global,
	.global_remove = on_global_remove,
};

/* A real listening AF_UNIX socket for create_listener's listen_fd.
 * The compositor integrates it into its event loop on commit; a
 * non-listening fd could be rejected, so we listen() for real. Linux
 * autobind (empty sun_path) gives an abstract address with no cleanup. */
static int
make_listen_fd(void)
{
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0)
		return -1;
	struct sockaddr_un addr;
	memset(&addr, 0, sizeof(addr));
	addr.sun_family = AF_UNIX;
	/* sun_path left zeroed → autobind (abstract, kernel-assigned). */
	if (bind(fd, (struct sockaddr *)&addr,
		 sizeof(sa_family_t)) < 0) {
		close(fd);
		return -1;
	}
	if (listen(fd, 1) < 0) {
		close(fd);
		return -1;
	}
	return fd;
}

/* Roundtrip; if the server posted a fatal error, log it and return 1,
 * storing the wayland protocol-error code in *out_code and the errored
 * interface in *out_iface (both 0/NULL when the caller passes NULL or
 * there was no protocol code). Returns 0 on a clean roundtrip. */
static int
denied_or_ok_code(struct probe *p, const char *what, uint32_t *out_code,
		  const struct wl_interface **out_iface)
{
	int rc = wl_display_roundtrip(p->display);
	int err = wl_display_get_error(p->display);
	if (out_code)
		*out_code = 0;
	if (out_iface)
		*out_iface = NULL;
	if (rc < 0 || err != 0) {
		uint32_t obj_id = 0, code = 0;
		const struct wl_interface *iface = NULL;
		code = wl_display_get_protocol_error(p->display,
						     &iface, &obj_id);
		if (out_code)
			*out_code = code;
		if (out_iface)
			*out_iface = iface;
		fprintf(stderr,
			"qdwin-secctx-probe: %s REFUSED (errno=%d, proto "
			"code=%u on %s#%u)\n",
			what, err, code,
			iface ? iface->name : "(unknown)", obj_id);
		return 1;
	}
	return 0;
}

enum secctx_mode {
	M_BIND,             /* default: bind only */
	M_COMMIT,           /* full set + commit */
	M_COMMIT_NO_APPID,  /* engine + instance_id, no app_id */
	M_COMMIT_BARE,      /* no setters */
	M_COMMIT_ENGINE,    /* engine only */
	M_DOUBLE_COMMIT,    /* commit twice */
	M_SET_AFTER_COMMIT, /* commit then set_app_id */
	M_UAF_REPRO,        /* commit, HUP close_fd, then destroy the context —
			     * the secctx listener use-after-free regression */
	M_UAF_SETTER        /* commit, HUP close_fd, then set_app_id on the dead
			     * context — guards the NULL-deref the user_data
			     * clear would otherwise expose in the setters */
};

int main(int argc, char *argv[])
{
	enum secctx_mode mode = M_BIND;
	int as_shell = 0;
	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--commit") == 0)
			mode = M_COMMIT;
		else if (strcmp(argv[i], "--as-shell") == 0)
			as_shell = 1;
		else if (strcmp(argv[i], "--commit-no-appid") == 0)
			mode = M_COMMIT_NO_APPID;
		else if (strcmp(argv[i], "--commit-bare") == 0)
			mode = M_COMMIT_BARE;
		else if (strcmp(argv[i], "--commit-engine-only") == 0)
			mode = M_COMMIT_ENGINE;
		else if (strcmp(argv[i], "--double-commit") == 0)
			mode = M_DOUBLE_COMMIT;
		else if (strcmp(argv[i], "--set-after-commit") == 0)
			mode = M_SET_AFTER_COMMIT;
		else if (strcmp(argv[i], "--commit-close-destroy") == 0)
			mode = M_UAF_REPRO;
		else if (strcmp(argv[i], "--commit-close-setter") == 0)
			mode = M_UAF_SETTER;
	}

	struct probe p = {0};
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-secctx-probe: wl_display_connect "
			"failed: %s\n", strerror(errno));
		return 2;
	}

	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);

	/* First roundtrip: discover globals. The manager bind is issued below
	 * so --as-shell can claim qdwin_shell_v1 first. */
	wl_display_roundtrip(p.display);

	if (as_shell) {
		if (!p.saw_shell) {
			fprintf(stderr, "qdwin-secctx-probe: "
				"qdwin_shell_v1 not advertised\n");
			return 2;
		}
		p.shell = wl_registry_bind(
			p.registry, p.shell_name, &qdwin_shell_v1_interface,
			p.shell_version < 1 ? p.shell_version : 1);
		if (!p.shell) {
			fprintf(stderr, "qdwin-secctx-probe: qdwin_shell_v1 "
				"bind returned NULL\n");
			return 2;
		}
		qdwin_shell_v1_add_listener(p.shell, &shell_listener, &p);
		qdwin_shell_v1_bind_as_shell(p.shell);
		if (denied_or_ok_code(&p, "shell bind_as_shell",
				      NULL, NULL) != 0)
			return 1;
		p.saw_manager = 0;
		p.manager_name = 0;
		p.manager_version = 0;
		p.registry = wl_display_get_registry(p.display);
		wl_registry_add_listener(p.registry, &registry_listener, &p);
		wl_display_roundtrip(p.display);
	}

	if (!p.saw_manager) {
		/* Hardened qdwin hides the manager from ordinary clients.
		 * Non-shell probe modes use this as the expected refusal signal. */
		fprintf(stderr, "qdwin-secctx-probe: "
			"wp_security_context_manager_v1 not advertised\n");
		return 2;
	}

	/* Bind at the version qdwin advertises (cap to the one we generated
	 * against). The bind itself is what the gate accepts or refuses. */
	p.mgr = wl_registry_bind(
		p.registry, p.manager_name,
		&wp_security_context_manager_v1_interface,
		p.manager_version < 1 ? p.manager_version : 1);
	if (!p.mgr) {
		fprintf(stderr, "qdwin-secctx-probe: manager bind returned NULL\n");
		return 2;
	}

	/* Second roundtrip resolves the bind: an accepted bind is silent; a
	 * refused bind surfaces via wl_client_post_implementation_error,
	 * i.e. WL_DISPLAY_ERROR_IMPLEMENTATION (=3) on the wl_display. Verify
	 * it is SPECIFICALLY that error so the reject case can't false-pass
	 * on an unrelated protocol error or a crash-disconnect. */
	{
		uint32_t bcode = 0;
		const struct wl_interface *biface = NULL;
		if (denied_or_ok_code(&p, "manager bind", &bcode, &biface) != 0) {
			if (biface == &wl_display_interface &&
			    bcode == WL_DISPLAY_ERROR_IMPLEMENTATION) {
				/* The EXPECTED gate reject. Distinct exit code 4
				 * so a scenario can require this specific error
				 * and not false-pass on an unrelated failure
				 * (which exits 1 below). */
				printf("qdwin-secctx-probe: manager bind "
				       "REFUSED with implementation error\n");
				return 4;
			}
			fprintf(stderr, "qdwin-secctx-probe: manager bind failed "
				"but not with the expected implementation error "
				"(code=%u iface=%s)\n", bcode,
				biface ? biface->name : "(none)");
			return 1;
		}
	}

	if (mode == M_BIND) {
		printf("qdwin-secctx-probe: manager bind ACCEPTED\n");
		return 0;
	}

	int listen_fd = make_listen_fd();
	int close_fds[2];
	if (listen_fd < 0 || pipe(close_fds) < 0) {
		fprintf(stderr, "qdwin-secctx-probe: fd setup failed: %s\n",
			strerror(errno));
		if (listen_fd >= 0)
			close(listen_fd);
		return 2;
	}

	struct wp_security_context_v1 *ctx =
		wp_security_context_manager_v1_create_listener(
			p.mgr, listen_fd, close_fds[0]);
	if (!ctx) {
		fprintf(stderr, "qdwin-secctx-probe: create_listener "
			"returned NULL\n");
		close(listen_fd);
		close(close_fds[0]);
		close(close_fds[1]);
		return 2;
	}

	/* Per-mode metadata setters. qdwin has NO required-field check, so
	 * the "missing"/"bare" variants are still valid commit sequences. */
	switch (mode) {
	case M_COMMIT:
	case M_UAF_REPRO:
	case M_UAF_SETTER:
		wp_security_context_v1_set_sandbox_engine(ctx, "qdistro");
		wp_security_context_v1_set_app_id(ctx,
			"org.qdistro.test.secctx-probe");
		wp_security_context_v1_set_instance_id(ctx, "secctx-probe-1");
		break;
	case M_COMMIT_NO_APPID:
		wp_security_context_v1_set_sandbox_engine(ctx, "qdistro");
		wp_security_context_v1_set_instance_id(ctx, "secctx-probe-1");
		break;
	case M_COMMIT_ENGINE:
		wp_security_context_v1_set_sandbox_engine(ctx, "qdistro");
		break;
	case M_DOUBLE_COMMIT:
	case M_SET_AFTER_COMMIT:
		/* A normal complete set, so the FIRST commit is unambiguously
		 * valid and the reuse error is the only thing under test. */
		wp_security_context_v1_set_sandbox_engine(ctx, "qdistro");
		wp_security_context_v1_set_app_id(ctx,
			"org.qdistro.test.secctx-probe");
		wp_security_context_v1_set_instance_id(ctx, "secctx-probe-1");
		break;
	case M_COMMIT_BARE:
	default:
		break;  /* no setters */
	}

	wp_security_context_v1_commit(ctx);
	/* Push the commit + its fd transfers to the server BEFORE dropping
	 * any local fd copies, so closing them can't race the in-flight
	 * request. */
	wl_display_flush(p.display);

	/* listen_fd and the close-pipe read end (close_fds[0]) were dup'd
	 * into the server by the commit; drop our local copies. Keep the
	 * close-pipe WRITE end (close_fds[1]) open across the assertion: the
	 * compositor tears the context down on HUP of the read end, which
	 * fires once all write ends close — closing it now would let the
	 * listener be reaped mid-check. */
	close(listen_fd);
	close(close_fds[0]);

	if (mode == M_UAF_REPRO || mode == M_UAF_SETTER) {
		/* Regression for the secctx listener use-after-free
		 * (qdwin_secctx_destroy not detaching sec->resource) and the
		 * NULL-deref the user_data clear would expose in the request
		 * handlers. First confirm the commit landed. */
		if (denied_or_ok_code(&p, "listener commit", NULL, NULL) != 0) {
			close(close_fds[1]);
			return 1;
		}
		/* Close the close-pipe WRITE end so the compositor sees HANGUP
		 * on the listener's close_fd and runs qdwin_secctx_close_cb →
		 * qdwin_secctx_destroy(sec), freeing `sec`. The two roundtrips +
		 * sleeps drive a full server dispatch cycle so the HUP teardown
		 * lands before the trigger below in practice (empirically
		 * deterministic — the buggy build aborts every run). It is not a
		 * hard ordering guarantee; the scenario's `close_fd hangup` log
		 * check confirms the teardown actually ran in the case. */
		close(close_fds[1]);
		usleep(150000);
		wl_display_roundtrip(p.display);
		usleep(50000);
		wl_display_roundtrip(p.display);
		if (wl_display_get_error(p.display) != 0) {
			fprintf(stderr, "qdwin-secctx-probe: display error after "
				"close_fd HUP (before trigger) — unexpected\n");
			return 1;
		}
		/* The trigger. On a vulnerable compositor either path crashes
		 * weston (M_UAF_REPRO: UAF in the destructor; M_UAF_SETTER:
		 * UAF reading freed sec, or a NULL-deref if only the user_data
		 * was cleared without guarding the setters). On a fixed one
		 * both are safe no-ops. */
		if (mode == M_UAF_REPRO) {
			wp_security_context_v1_destroy(ctx);
		} else {
			/* set_app_id on the dead context — must hit the NULL
			 * guard, not deref a freed/NULL sec. */
			wp_security_context_v1_set_app_id(ctx, "after-hup");
		}
		wl_display_flush(p.display);
		int r = wl_display_roundtrip(p.display);
		if (r < 0 || wl_display_get_error(p.display) != 0) {
			fprintf(stderr, "qdwin-secctx-probe: compositor "
				"connection died after post-HUP %s — crash\n",
				mode == M_UAF_REPRO ? "destroy" : "set_app_id");
			return 1;
		}
		printf("qdwin-secctx-probe: commit+close+%s survived "
		       "(no crash)\n",
		       mode == M_UAF_REPRO ? "destroy" : "set_app_id");
		return 0;
	}

	uint32_t code = 0;
	const struct wl_interface *iface = NULL;
	int rc = denied_or_ok_code(&p, "listener commit", &code, &iface);

	/* Reuse edges: issue the second op only after the first commit has
	 * round-tripped cleanly, so a reuse error is unambiguous. */
	if (rc == 0 && (mode == M_DOUBLE_COMMIT ||
			mode == M_SET_AFTER_COMMIT)) {
		if (mode == M_DOUBLE_COMMIT)
			wp_security_context_v1_commit(ctx);
		else
			wp_security_context_v1_set_app_id(ctx,
				"org.qdistro.test.secctx-probe-2");
		rc = denied_or_ok_code(&p, "context reuse", &code, &iface);
		close(close_fds[1]);
		if (rc == 0) {
			fprintf(stderr, "qdwin-secctx-probe: reuse was NOT "
				"rejected\n");
			return 1;
		}
		/* Verify it is already_used ON wp_security_context_v1 — not an
		 * unrelated protocol error that happens to share code 1. */
		if (iface != &wp_security_context_v1_interface ||
		    code != WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_USED) {
			fprintf(stderr, "qdwin-secctx-probe: reuse got code=%u "
				"on %s, want already_used=%d on "
				"wp_security_context_v1\n", code,
				iface ? iface->name : "(none)",
				WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_USED);
			return 1;
		}
		printf("qdwin-secctx-probe: context reuse REJECTED with "
		       "already_used\n");
		return 3;
	}

	close(close_fds[1]);   /* signal teardown only after the assertion */
	if (rc != 0)
		return 1;

	printf("qdwin-secctx-probe: manager bind ACCEPTED, listener "
	       "committed\n");
	return 0;
}
