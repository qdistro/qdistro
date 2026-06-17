/*
 * qdistro-secctx-exec — give a non-secctx-aware Wayland client a
 * wp_security_context_v1 tag.
 *
 * The wrapper:
 *   1. Connects to the outer Wayland compositor as a normal client.
 *   2. Binds wp_security_context_manager_v1.
 *   3. Creates a listening AF_UNIX socket under XDG_RUNTIME_DIR
 *      ("wayland-secctx-<NN>") + an eventfd close-fd.
 *   4. Calls create_listener(listen_fd, close_fd) and sets
 *      sandbox_engine / app_id / instance_id, then commit.
 *   5. fork()s. The child closes our private fds, sets WAYLAND_DISPLAY
 *      to the listener basename (libwayland resolves it relative to
 *      XDG_RUNTIME_DIR), and exec()s the inner command. Any new
 *      Wayland connection from the child lands at the compositor
 *      tagged with our security context.
 *   6. The parent waits for the child. When it exits, we close the
 *      close_fd — the compositor's wl_event_loop sees HUP and tears
 *      down the security context, revoking it for any straggling
 *      grand-children that might still be holding a Wayland connection.
 *
 * Designed for tier-4: waypipe-client and other regular Wayland clients
 * do not natively support wp_security_context_v1, but launching them via this wrapper
 * gives the resulting Wayland client a `qdistro.tier4.<vm>` tag
 * that qdwin's secctx machinery + the broker rules engine can match
 * on. Equally usable for tier-3, tier-5, or any other "non-secctx-aware
 * client needs a tag" case.
 *
 * Usage:
 *   qdistro-secctx-exec --sandbox-engine ENGINE --app-id APPID
 *                       [--instance-id ID] -- <cmd> [args...]
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <errno.h>
#include <ctype.h>
#include <fcntl.h>
#include <getopt.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <limits.h>
#include <sys/eventfd.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <unistd.h>

#include <wayland-client.h>

#include "security-context-v1-client-protocol.h"

struct ctx {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wp_security_context_manager_v1 *mgr;
};

static int
env_true(const char *name)
{
	const char *v = getenv(name);
	return v && (strcmp(v, "1") == 0 || strcmp(v, "true") == 0 ||
		     strcmp(v, "yes") == 0 || strcmp(v, "on") == 0);
}

static int
is_token_char(int ch, int allow_slash)
{
	return isalnum((unsigned char)ch) || ch == '.' || ch == '_' ||
	       ch == '-' || (allow_slash && ch == '/');
}

static int
validate_token(const char *label, const char *value, size_t max_len,
	       int allow_slash)
{
	size_t len;

	if (!value || !*value) {
		fprintf(stderr, "qdistro-secctx-exec: %s is empty\n", label);
		return -1;
	}
	len = strlen(value);
	if (len > max_len) {
		fprintf(stderr, "qdistro-secctx-exec: %s is too long\n", label);
		return -1;
	}
	if (allow_slash &&
	    (value[0] == '/' || strstr(value, "//") || strstr(value, ".."))) {
		fprintf(stderr,
			"qdistro-secctx-exec: %s has invalid path-like shape\n",
			label);
		return -1;
	}
	for (size_t i = 0; i < len; i++) {
		if (!is_token_char(value[i], allow_slash)) {
			fprintf(stderr,
				"qdistro-secctx-exec: %s contains invalid byte 0x%02x\n",
				label, (unsigned char)value[i]);
			return -1;
		}
	}
	return 0;
}

static int
validate_secctx(const char *engine, const char *app_id, const char *instance_id)
{
	if (validate_token("sandbox-engine", engine, 64, 0) < 0)
		return -1;
	/* Recognized qdistro sandbox engines: the tier-2..5 app silos
	 * (qdistro.tier*) and the multi-machine display peer engine
	 * (qdistro.mm), which tags windowed remote-peer clients (e.g. the
	 * Phase-2 rung-1 FreeRDP view backend) so the shell can attribute
	 * them by secctx, not by spoofable title/pixels. */
	if (strncmp(engine, "qdistro.tier", strlen("qdistro.tier")) != 0 &&
	    strncmp(engine, "qdistro.mm", strlen("qdistro.mm")) != 0) {
		fprintf(stderr,
			"qdistro-secctx-exec: sandbox-engine must start with "
			"qdistro.tier or qdistro.mm\n");
		return -1;
	}
	if (validate_token("app-id", app_id, 128, 1) < 0)
		return -1;
	if (instance_id && *instance_id &&
	    validate_token("instance-id", instance_id, 128, 0) < 0)
		return -1;
	return 0;
}

static int
read_proc_status(pid_t pid, pid_t *ppid, uid_t *uid)
{
	char path[64];
	char line[256];
	FILE *f;
	int saw_ppid = 0, saw_uid = 0;

	snprintf(path, sizeof path, "/proc/%ld/status", (long)pid);
	f = fopen(path, "re");
	if (!f)
		return -1;
	while (fgets(line, sizeof line, f)) {
		int ppid_i;
		if (sscanf(line, "PPid:\t%d", &ppid_i) == 1) {
			*ppid = (pid_t)ppid_i;
			saw_ppid = 1;
		} else if (strncmp(line, "Uid:", 4) == 0) {
			unsigned int ruid;
			if (sscanf(line, "Uid:\t%u", &ruid) == 1) {
				*uid = (uid_t)ruid;
				saw_uid = 1;
			}
		}
	}
	fclose(f);
	return (saw_ppid && saw_uid) ? 0 : -1;
}

static int
proc_basename(pid_t pid, char *out, size_t out_sz)
{
	char path[64];
	char buf[512];
	ssize_t n;
	const char *base;

	snprintf(path, sizeof path, "/proc/%ld/exe", (long)pid);
	n = readlink(path, buf, sizeof buf - 1);
	if (n < 0)
		return -1;
	buf[n] = '\0';
	base = strrchr(buf, '/');
	base = base ? base + 1 : buf;
	snprintf(out, out_sz, "%s", base);
	return 0;
}

static uint64_t
proc_starttime(pid_t pid)
{
	char path[64];
	char buf[2048];
	int fd;
	ssize_t n;
	char *p;
	int field = 2;
	uint64_t v = 0;

	if (pid <= 0)
		return 0;
	snprintf(path, sizeof path, "/proc/%ld/stat", (long)pid);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return 0;
	n = read(fd, buf, sizeof buf - 1);
	close(fd);
	if (n <= 0)
		return 0;
	buf[n] = '\0';
	p = strrchr(buf, ')');
	if (!p)
		return 0;
	p++;
	while (*p && field < 22) {
		while (*p == ' ' || *p == '\t')
			p++;
		if (!*p)
			return 0;
		field++;
		if (field == 22)
			break;
		while (*p && *p != ' ' && *p != '\t')
			p++;
	}
	if (field != 22)
		return 0;
	while (*p >= '0' && *p <= '9') {
		v = v * 10 + (uint64_t)(*p - '0');
		p++;
	}
	return v;
}

static int
trusted_root_parent(void)
{
	pid_t pid = getppid();
	pid_t ppid = 0;
	uid_t uid = (uid_t)-1;
	uint64_t st_before, st_after;
	char base[128] = {0};

	if (pid <= 1 || read_proc_status(pid, &ppid, &uid) < 0)
		return 0;
	(void)ppid;
	if (uid != 0)
		return 0;
	st_before = proc_starttime(pid);
	if (proc_basename(pid, base, sizeof base) == 0) {
		if (strcmp(base, "runuser") == 0 ||
		    strcmp(base, "su") == 0 ||
		    strcmp(base, "sudo") == 0 ||
		    strcmp(base, "pkexec") == 0) {
			st_after = proc_starttime(pid);
			return st_before != 0 && st_after != 0 &&
			       st_before == st_after;
		}
		return 0;
	}
	/* proc_basename() readlink(/proc/<ppid>/exe) is denied when this
	 * helper runs unprivileged (e.g. tier-3/4: `runuser -u admin -- env ...
	 * qdistro-secctx-exec`) and the launcher parent is root: the kernel
	 * gates the exe symlink behind PTRACE_MODE_READ_FSCREDS, so a lower-
	 * privilege process cannot read a higher-privilege parent's exe path.
	 * We already confirmed uid==0 from /proc/<ppid>/status (which IS
	 * readable). The load-bearing trust property is exactly that: the
	 * direct parent is a root process — an unprivileged attacker cannot
	 * give their own process a uid-0 direct parent, so they cannot forge
	 * this marker. The launcher-basename allowlist is defense-in-depth
	 * that is structurally unavailable here; fall back to the root-parent
	 * + stable-starttime proof rather than fail closed against the
	 * supported production tier-3/4/5 path. */
	st_after = proc_starttime(pid);
	if (st_before != 0 && st_after != 0 && st_before == st_after) {
		fprintf(stderr,
			"qdistro-secctx-exec: trusted launcher accepted via "
			"root-parent fallback (parent pid=%ld uid=0, exe "
			"unreadable; stable starttime)\n", (long)pid);
		return 1;
	}
	return 0;
}

static int
authorized_launcher(void)
{
	if (env_true("QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED")) {
		fprintf(stderr,
			"qdistro-secctx-exec: WARN dev override allows untrusted caller\n");
		return 1;
	}
	if (geteuid() == 0)
		return 1;
	if (!env_true("QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER")) {
		fprintf(stderr,
			"qdistro-secctx-exec: refused: missing trusted launcher marker "
			"(set QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1 for dev-only use)\n");
		return 0;
	}
	if (trusted_root_parent())
		return 1;
	fprintf(stderr,
		"qdistro-secctx-exec: refused: trusted marker is not backed by "
		"a direct root launcher parent\n");
	return 0;
}

static int
path_under_runtime(const char *path, const char *xdg_runtime_dir)
{
	char runtime_real[PATH_MAX], parent[PATH_MAX], parent_real[PATH_MAX];
	const char *slash;
	size_t parent_len;

	if (!path || !*path || !xdg_runtime_dir || !*xdg_runtime_dir)
		return 0;
	if (path[0] != '/')
		return strchr(path, '/') == NULL;
	slash = strrchr(path, '/');
	if (!slash || slash == path)
		return 0;
	parent_len = (size_t)(slash - path);
	if (parent_len >= sizeof parent)
		return 0;
	memcpy(parent, path, parent_len);
	parent[parent_len] = '\0';
	if (!realpath(xdg_runtime_dir, runtime_real) ||
	    !realpath(parent, parent_real))
		return 0;
	return strcmp(runtime_real, parent_real) == 0;
}

static void
publish_launch_record(const char *lr_path, const char *xdg_runtime_dir,
		      pid_t pid)
{
	char full[256];
	const char *path = lr_path;
	const char *token = getenv("QDISTRO_LAUNCH_RECORD_TOKEN");
	int fd;
	char buf[256];
	int n;

	if (!lr_path || !*lr_path)
		return;
	if (!path_under_runtime(lr_path, xdg_runtime_dir)) {
		fprintf(stderr,
			"qdistro-secctx-exec: launch-record path refused outside XDG_RUNTIME_DIR\n");
		return;
	}
	if (lr_path[0] != '/') {
		n = snprintf(full, sizeof full, "%s/%s", xdg_runtime_dir, lr_path);
		if (n < 0 || (size_t)n >= sizeof full) {
			fprintf(stderr,
				"qdistro-secctx-exec: launch-record path too long\n");
			return;
		}
		path = full;
	}
	fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW,
		  0600);
	if (fd < 0) {
		fprintf(stderr, "qdistro-secctx-exec: launch-record path %s: %s\n",
			path, strerror(errno));
		return;
	}
	if (token && *token)
		n = snprintf(buf, sizeof buf, "%ld %s\n", (long)pid, token);
	else
		n = snprintf(buf, sizeof buf, "%ld\n", (long)pid);
	if (n > 0)
		(void)write(fd, buf, (size_t)n);
	close(fd);
}

static void
registry_global(void *data, struct wl_registry *reg, uint32_t name,
		const char *iface, uint32_t version)
{
	struct ctx *c = data;
	(void)version;
	if (strcmp(iface, "wp_security_context_manager_v1") == 0) {
		c->mgr = wl_registry_bind(reg, name,
			&wp_security_context_manager_v1_interface, 1);
	}
}

static void
registry_global_remove(void *data, struct wl_registry *reg, uint32_t name)
{
	(void)data; (void)reg; (void)name;
}

static const struct wl_registry_listener registry_listener = {
	.global = registry_global,
	.global_remove = registry_global_remove,
};

static int
make_listener(const char *xdg_runtime_dir, char *out_basename, size_t out_sz)
{
	int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
	if (fd < 0) {
		fprintf(stderr, "qdistro-secctx-exec: socket: %s\n",
			strerror(errno));
		return -1;
	}

	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	for (int i = 0; i < 32; i++) {
		snprintf(out_basename, out_sz, "wayland-secctx-%d-%d",
			 (int)getpid(), i);
		int n = snprintf(addr.sun_path, sizeof addr.sun_path,
				 "%s/%s", xdg_runtime_dir, out_basename);
		if (n < 0 || (size_t)n >= sizeof addr.sun_path) {
			fprintf(stderr, "qdistro-secctx-exec: socket path too long\n");
			close(fd);
			return -1;
		}
		unlink(addr.sun_path);
		if (bind(fd, (struct sockaddr *)&addr, sizeof addr) == 0)
			goto bound;
		if (errno != EADDRINUSE) {
			fprintf(stderr, "qdistro-secctx-exec: bind %s: %s\n",
				addr.sun_path, strerror(errno));
			close(fd);
			return -1;
		}
	}
	fprintf(stderr, "qdistro-secctx-exec: couldn't find free socket path\n");
	close(fd);
	return -1;

bound:
	if (chmod(addr.sun_path, 0600) < 0) {
		/* Non-fatal — the path is already inside our XDG_RUNTIME_DIR
		 * which is 0700. */
		(void)0;
	}
	if (listen(fd, 16) < 0) {
		fprintf(stderr, "qdistro-secctx-exec: listen: %s\n",
			strerror(errno));
		unlink(addr.sun_path);
		close(fd);
		return -1;
	}
	return fd;
}

static void
usage(FILE *f)
{
	fputs(
"usage: qdistro-secctx-exec --sandbox-engine ENGINE --app-id APPID\n"
"                          [--instance-id ID] [--display NAME]\n"
"                          -- <cmd> [args...]\n"
"\n"
"Wraps an inner command in a wp_security_context_v1 tag against the\n"
"outer Wayland compositor (default WAYLAND_DISPLAY=wayland-1, resolved\n"
"under XDG_RUNTIME_DIR). The inner cmd must connect Wayland-side via\n"
"the wrapper-injected WAYLAND_DISPLAY=wayland-secctx-<...>; everything\n"
"else (non-Wayland envvars) passes through.\n"
"\n"
"Options:\n"
"  --sandbox-engine ENGINE   Reverse-DNS engine name (e.g. qdistro.tier4).\n"
"  --app-id APPID            Sandbox-specific app identifier.\n"
"  --instance-id ID          Optional instance identifier.\n"
"  --display NAME            Outer compositor's display name (default\n"
"                            $WAYLAND_DISPLAY or wayland-1).\n"
"Security:\n"
"  Production use must come from a qdistro root launcher. Developer-only\n"
"  direct use requires QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=1.\n"
"\n", f);
}

int main(int argc, char **argv)
{
	const char *sandbox_engine = NULL;
	const char *app_id = NULL;
	const char *instance_id = NULL;
	const char *outer_display = NULL;

	enum {
		OPT_SANDBOX_ENGINE = 1000,
		OPT_APP_ID,
		OPT_INSTANCE_ID,
		OPT_DISPLAY,
	};
	struct option longopts[] = {
		{ "sandbox-engine", required_argument, NULL, OPT_SANDBOX_ENGINE },
		{ "app-id",         required_argument, NULL, OPT_APP_ID },
		{ "instance-id",    required_argument, NULL, OPT_INSTANCE_ID },
		{ "display",        required_argument, NULL, OPT_DISPLAY },
		{ "help",           no_argument,       NULL, 'h' },
		{ NULL, 0, NULL, 0 },
	};

	int c;
	while ((c = getopt_long(argc, argv, "+h", longopts, NULL)) != -1) {
		switch (c) {
		case OPT_SANDBOX_ENGINE: sandbox_engine = optarg; break;
		case OPT_APP_ID:         app_id         = optarg; break;
		case OPT_INSTANCE_ID:    instance_id    = optarg; break;
		case OPT_DISPLAY:        outer_display  = optarg; break;
		case 'h': usage(stdout); return 0;
		default:  usage(stderr); return 2;
		}
	}
	if (!sandbox_engine || !app_id) {
		fprintf(stderr,
			"qdistro-secctx-exec: --sandbox-engine and --app-id are required\n");
		usage(stderr);
		return 2;
	}
	if (optind >= argc) {
		fprintf(stderr, "qdistro-secctx-exec: missing command to exec\n");
		usage(stderr);
		return 2;
	}
	if (!authorized_launcher())
		return 13;
	if (validate_secctx(sandbox_engine, app_id, instance_id) < 0)
		return 2;

	const char *xdg = getenv("XDG_RUNTIME_DIR");
	if (!xdg || !*xdg) {
		fprintf(stderr,
			"qdistro-secctx-exec: XDG_RUNTIME_DIR not set\n");
		return 3;
	}
	if (!outer_display) outer_display = getenv("WAYLAND_DISPLAY");
	if (!outer_display) outer_display = "wayland-1";

	struct ctx ctx = {0};
	ctx.display = wl_display_connect(outer_display);
	if (!ctx.display) {
		fprintf(stderr,
			"qdistro-secctx-exec: wl_display_connect(%s) failed\n",
			outer_display);
		return 4;
	}
	ctx.registry = wl_display_get_registry(ctx.display);
	wl_registry_add_listener(ctx.registry, &registry_listener, &ctx);
	wl_display_roundtrip(ctx.display);
	if (!ctx.mgr) {
		fprintf(stderr,
			"qdistro-secctx-exec: outer compositor doesn't advertise "
			"wp_security_context_manager_v1\n");
		wl_display_disconnect(ctx.display);
		return 5;
	}

	char base[64];
	int listen_fd = make_listener(xdg, base, sizeof base);
	if (listen_fd < 0) {
		wl_display_disconnect(ctx.display);
		return 6;
	}

	int close_fd = eventfd(0, EFD_CLOEXEC);
	if (close_fd < 0) {
		fprintf(stderr, "qdistro-secctx-exec: eventfd: %s\n",
			strerror(errno));
		close(listen_fd);
		wl_display_disconnect(ctx.display);
		return 6;
	}

	struct wp_security_context_v1 *sec =
		wp_security_context_manager_v1_create_listener(
			ctx.mgr, listen_fd, close_fd);
	if (!sec) {
		fprintf(stderr, "qdistro-secctx-exec: create_listener failed\n");
		close(listen_fd);
		close(close_fd);
		wl_display_disconnect(ctx.display);
		return 7;
	}
	wp_security_context_v1_set_sandbox_engine(sec, sandbox_engine);
	wp_security_context_v1_set_app_id(sec, app_id);
	if (instance_id)
		wp_security_context_v1_set_instance_id(sec, instance_id);
	wp_security_context_v1_commit(sec);

	/* Roundtrip so the compositor processes commit before we fork. */
	if (wl_display_roundtrip(ctx.display) < 0) {
		fprintf(stderr,
			"qdistro-secctx-exec: roundtrip after commit failed\n");
		close(listen_fd);
		close(close_fd);
		wl_display_disconnect(ctx.display);
		return 8;
	}

	/* The compositor has dup'd listen_fd internally; keep our copy
	 * around until the child exits — we hold close_fd as the revoke
	 * handle. listen_fd we can close now (the compositor's dup is
	 * what accepts).  Actually safer: keep both alive in parent
	 * until child reaps, then close in order: close_fd first so the
	 * compositor sees HUP and stops accepting, then listen_fd. */

	pid_t pid = fork();
	if (pid < 0) {
		fprintf(stderr, "qdistro-secctx-exec: fork: %s\n",
			strerror(errno));
		close(listen_fd);
		close(close_fd);
		wl_display_disconnect(ctx.display);
		return 9;
	}
	if (pid == 0) {
		/* Child: drop our wl_display fd + the secctx fds. They were
		 * SOCK_CLOEXEC / EFD_CLOEXEC anyway, but be explicit. */
		wl_display_disconnect(ctx.display);
		close(listen_fd);
		close(close_fd);

		/* Inner client connects via the listener path. libwayland
		 * resolves WAYLAND_DISPLAY relative to XDG_RUNTIME_DIR when
		 * the value is not absolute. */
		setenv("WAYLAND_DISPLAY", base, 1);
		/* Drop WAYLAND_SOCKET if inherited; otherwise libwayland
		 * tries to use that fd instead of WAYLAND_DISPLAY. */
		unsetenv("WAYLAND_SOCKET");
		unsetenv("QDISTRO_SECCTX_EXEC_TRUSTED_LAUNCHER");
		unsetenv("QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED");
		unsetenv("QDISTRO_LAUNCH_RECORD_PATH");
		unsetenv("QDISTRO_LAUNCH_RECORD_TOKEN");

		execvp(argv[optind], &argv[optind]);
		fprintf(stderr, "qdistro-secctx-exec: execvp(%s): %s\n",
			argv[optind], strerror(errno));
		_exit(127);
	}

	/* Permission-lineage launch-record hook (P1-1 rollout). When the
	 * launcher set QDISTRO_LAUNCH_RECORD_PATH, publish the inner child's
	 * pid there so a trusted *root* launcher parent (e.g. spawn-tier3.sh)
	 * can RegisterLaunch it with the broker. We are the only component that
	 * knows this pid: it is our fork child, and its pid survives the
	 * execvp() above, so it is exactly the pid that connects to the
	 * compositor and that a later gate resolves. We write the bare pid; the
	 * root consumer re-reads /proc (exe/starttime/uid) *after* the inner
	 * command has exec'd and is up, and the broker re-verifies it again at
	 * RegisterLaunch — so a stale or mid-exec read can only fail closed,
	 * never mint a wrong record. Best-effort: a write failure is logged but
	 * never blocks the launch. */
	publish_launch_record(getenv("QDISTRO_LAUNCH_RECORD_PATH"), xdg, pid);

	/* Parent: wait for child. */
	int status = 0;
	pid_t r;
	do {
		r = waitpid(pid, &status, 0);
	} while (r < 0 && errno == EINTR);

	/* Revoke: close close_fd → compositor wl_event_loop reads HUP. */
	close(close_fd);
	close(listen_fd);

	/* Unlink the listener path. */
	char path[256];
	snprintf(path, sizeof path, "%s/%s", xdg, base);
	unlink(path);

	wp_security_context_v1_destroy(sec);
	wp_security_context_manager_v1_destroy(ctx.mgr);
	wl_registry_destroy(ctx.registry);
	wl_display_disconnect(ctx.display);

	if (r < 0) {
		fprintf(stderr, "qdistro-secctx-exec: waitpid: %s\n",
			strerror(errno));
		return 10;
	}
	if (WIFSIGNALED(status))
		return 128 + WTERMSIG(status);
	return WEXITSTATUS(status);
}
