/*
 * qdwin-wsname-probe — qdwin_shell_v1.set_workspace_name (v27) ↔
 * ext-workspace-v1 name-echo conformance probe.
 *
 * Verifies cross-shell workspace-NAME parity: the shell pushes a custom
 * workspace name out-of-band via the private qdwin_shell_v1 protocol, and
 * qdwin must echo it back to EVERY ext-workspace-v1 client through the
 * standard ext_workspace_handle_v1.name event. See
 * qdwin/qdwin-shell-v1.xml set_workspace_name and todo/qdwin/other-shells.md.
 *
 * This single client wears two hats:
 *   - qdwin_shell_v1 (the WRITER): bind_as_shell, then set_workspace_name.
 *     qdwin only advertises this global to the allowed uid, so the host
 *     harness runs the probe as that uid (same as qdwin-nested-probe).
 *   - ext_workspace_manager_v1 (the READER): the SAME role any third-party
 *     bar (waybar, eww, another Quickshell) plays — it must see the name.
 *
 * Reports via EXIT CODE so a headless host test can assert without
 * screenshots:
 *   0  success
 *   1  setup error (no display / missing global / bind refused)
 *   2  name was NOT echoed on the ext-workspace handle
 *   3  empty-name revert did not restore the positional default
 *
 * Scenarios (argv):
 *   --index=K --name=STR   set workspace K's name to STR, then assert the
 *                          ext_workspace handle for K reports STR.
 *   --index=K --name=STR --expect-revert
 *                          additionally: clear the name (empty string) and
 *                          assert K reverts to the positional "K+1".
 *
 * Build: meson target qdwin-wsname-probe (see qdwin/meson.build).
 * Driven by tests/host/21-ext-workspace-names.md.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wayland-client.h>

#include "qdwin-shell-v1-client-protocol.h"
#include "ext-workspace-v1-client-protocol.h"

#define MAX_WS 64

struct state {
	struct wl_display *display;
	struct wl_registry *registry;

	struct qdwin_shell_v1 *shell;
	uint32_t shell_name;
	int shell_bound;

	struct ext_workspace_manager_v1 *mgr;
	uint32_t mgr_name;

	struct ext_workspace_handle_v1 *handles[MAX_WS];
	char *names[MAX_WS];
	int n;
};

static struct state S;

/* ---- ext_workspace_handle_v1 listener: only `name` matters here ---- */

static int
handle_index(struct ext_workspace_handle_v1 *h)
{
	for (int i = 0; i < S.n; i++)
		if (S.handles[i] == h)
			return i;
	return -1;
}

static void h_id(void *d, struct ext_workspace_handle_v1 *h, const char *id)
{ (void)d; (void)h; (void)id; }

static void
h_name(void *d, struct ext_workspace_handle_v1 *h, const char *name)
{
	(void)d;
	int i = handle_index(h);
	if (i < 0)
		return;
	free(S.names[i]);
	S.names[i] = strdup(name ? name : "");
}

static void h_coordinates(void *d, struct ext_workspace_handle_v1 *h,
			  struct wl_array *a)
{ (void)d; (void)h; (void)a; }
static void h_state(void *d, struct ext_workspace_handle_v1 *h, uint32_t s)
{ (void)d; (void)h; (void)s; }
static void h_capabilities(void *d, struct ext_workspace_handle_v1 *h,
			   uint32_t c)
{ (void)d; (void)h; (void)c; }
static void h_removed(void *d, struct ext_workspace_handle_v1 *h)
{
	(void)d;
	int i = handle_index(h);
	if (i >= 0)
		S.handles[i] = NULL;
}

static const struct ext_workspace_handle_v1_listener handle_listener = {
	.id = h_id,
	.name = h_name,
	.coordinates = h_coordinates,
	.state = h_state,
	.capabilities = h_capabilities,
	.removed = h_removed,
};

/* ---- manager listener ---- */

static void
m_workspace(void *d, struct ext_workspace_manager_v1 *m,
	    struct ext_workspace_handle_v1 *h)
{
	(void)d; (void)m;
	if (S.n >= MAX_WS)
		return;
	S.handles[S.n] = h;
	S.names[S.n] = NULL;
	ext_workspace_handle_v1_add_listener(h, &handle_listener, NULL);
	S.n++;
}

static void m_workspace_group(void *d, struct ext_workspace_manager_v1 *m,
			      struct ext_workspace_group_handle_v1 *g)
{ (void)d; (void)m; (void)g; }
static void m_done(void *d, struct ext_workspace_manager_v1 *m)
{ (void)d; (void)m; }
static void m_finished(void *d, struct ext_workspace_manager_v1 *m)
{ (void)d; (void)m; }

static const struct ext_workspace_manager_v1_listener manager_listener = {
	.workspace = m_workspace,
	.workspace_group = m_workspace_group,
	.done = m_done,
	.finished = m_finished,
};

/* ---- qdwin_shell_v1 listener: we only need `hello` to confirm bind ---- */

static void sh_hello(void *d, struct qdwin_shell_v1 *s, uint32_t uid)
{ (void)d; (void)s; (void)uid; S.shell_bound = 1; }

/* The shell protocol has many events; we ignore all but hello. The
 * generated listener struct requires every member, so stub them. We pull
 * in the field names by zero-initialising and only setting .hello — any
 * event qdwin sends to an unset slot would crash, so set every pointer to
 * a shared no-op via a designated-initialiser-free assignment loop is not
 * possible for a const struct; instead list the ones qdwin may emit on a
 * shell that does nothing but set names. In practice, on a headless
 * --no-shell compositor with no toplevels, only `hello` fires before our
 * roundtrips complete. To be safe against spurious events, route unknown
 * ones through generic stubs. */
static void sh_noop(void) {}

static const struct qdwin_shell_v1_listener shell_listener = {
	.hello = sh_hello,
	.toplevel_added = (void *)sh_noop,
	.toplevel_geometry = (void *)sh_noop,
	.toplevel_state = (void *)sh_noop,
	.toplevel_title = (void *)sh_noop,
	.toplevel_removed = (void *)sh_noop,
	.locked_changed = (void *)sh_noop,
	.seat_created = (void *)sh_noop,
	.seat_removed = (void *)sh_noop,
	.output_created = (void *)sh_noop,
	.output_removed = (void *)sh_noop,
	.launcher_requested = (void *)sh_noop,
	.switcher_next = (void *)sh_noop,
	.switcher_commit = (void *)sh_noop,
	.lock_requested = (void *)sh_noop,
	.overlay_key = (void *)sh_noop,
	.idle_lock_hint = (void *)sh_noop,
	.nested_proxy_pending = (void *)sh_noop,
	.nested_proxy_pixel_source = (void *)sh_noop,
	.selection_set = (void *)sh_noop,
	/* (set_cursor_sprite is a request, not an event — not listed) */
	.selection_set_source_identity = (void *)sh_noop,
	.activation_pending = (void *)sh_noop,
	.toplevel_security_context = (void *)sh_noop,
	.toplevel_peer_identity = (void *)sh_noop,
	.seat_focus_changed = (void *)sh_noop,
	.data_offer_receive_pending = (void *)sh_noop,
	.popup_button = (void *)sh_noop,
	.chrome_button = (void *)sh_noop,
	.hotkey_pressed = (void *)sh_noop,
	.toplevel_workspace = (void *)sh_noop,
	/* NOTE: set_display_power / set_workspace_name are REQUESTS, not
	 * events — they have no listener slot. The list above mirrors the
	 * generated qdwin_shell_v1_listener exactly (ending at
	 * toplevel_workspace). */
};

/* ---- registry ---- */

static void
reg_global(void *d, struct wl_registry *r, uint32_t name, const char *iface,
	   uint32_t version)
{
	(void)d;
	if (strcmp(iface, qdwin_shell_v1_interface.name) == 0) {
		uint32_t v = version < 27u ? version : 27u;
		S.shell = wl_registry_bind(r, name, &qdwin_shell_v1_interface, v);
		S.shell_name = name;
	} else if (strcmp(iface, ext_workspace_manager_v1_interface.name) == 0) {
		uint32_t v = version < 1u ? version : 1u;
		S.mgr = wl_registry_bind(r, name,
					 &ext_workspace_manager_v1_interface, v);
		S.mgr_name = name;
	}
}

static void reg_global_remove(void *d, struct wl_registry *r, uint32_t name)
{ (void)d; (void)r; (void)name; }

static const struct wl_registry_listener registry_listener = {
	.global = reg_global,
	.global_remove = reg_global_remove,
};

static unsigned long
parse_uint_arg(const char *argv, const char *key, int *found)
{
	size_t klen = strlen(key);
	if (strncmp(argv, key, klen) == 0) {
		*found = 1;
		return strtoul(argv + klen, NULL, 10);
	}
	return 0;
}

int
main(int argc, char **argv)
{
	uint32_t index = 0;
	const char *name = NULL;
	int want_revert = 0;
	int got_index = 0;

	for (int i = 1; i < argc; i++) {
		int f = 0;
		unsigned long v = parse_uint_arg(argv[i], "--index=", &f);
		if (f) { index = (uint32_t)v; got_index = 1; continue; }
		if (strncmp(argv[i], "--name=", 7) == 0) {
			name = argv[i] + 7;
			continue;
		}
		if (strcmp(argv[i], "--expect-revert") == 0) {
			want_revert = 1;
			continue;
		}
	}
	if (!got_index || !name) {
		fprintf(stderr, "usage: %s --index=K --name=STR "
			"[--expect-revert]\n", argv[0]);
		return 1;
	}

	S.display = wl_display_connect(NULL);
	if (!S.display) {
		fprintf(stderr, "wsname-probe: no display\n");
		return 1;
	}
	S.registry = wl_display_get_registry(S.display);
	wl_registry_add_listener(S.registry, &registry_listener, NULL);
	wl_display_roundtrip(S.display);  /* discover + bind globals */

	if (!S.shell) {
		fprintf(stderr, "wsname-probe: qdwin_shell_v1 not advertised "
			"(uid gate / wrong version?)\n");
		return 1;
	}
	if (!S.mgr) {
		fprintf(stderr, "wsname-probe: ext_workspace_manager_v1 "
			"missing\n");
		return 1;
	}

	ext_workspace_manager_v1_add_listener(S.mgr, &manager_listener, NULL);
	qdwin_shell_v1_add_listener(S.shell, &shell_listener, NULL);
	qdwin_shell_v1_bind_as_shell(S.shell);
	wl_display_roundtrip(S.display);  /* hello + initial workspace burst */

	if (!S.shell_bound) {
		fprintf(stderr, "wsname-probe: bind_as_shell not acked\n");
		return 1;
	}
	if (index >= (uint32_t)S.n) {
		fprintf(stderr, "wsname-probe: index %u >= workspace count %d\n",
			index, S.n);
		return 1;
	}

	/* Push the custom name on the private channel, flush, and read back
	 * what the ext-workspace handle reports (the third-party-bar view). */
	qdwin_shell_v1_set_workspace_name(S.shell, index, name);
	wl_display_roundtrip(S.display);  /* let qdwin echo the name event */
	wl_display_roundtrip(S.display);  /* settle the manager done */

	if (!S.handles[index] || !S.names[index] ||
	    strcmp(S.names[index], name) != 0) {
		fprintf(stderr, "wsname-probe: index %u name=\"%s\" want=\"%s\" "
			"(NOT echoed via ext-workspace)\n",
			index, S.names[index] ? S.names[index] : "(null)",
			name);
		return 2;
	}
	fprintf(stderr, "wsname-probe: index %u echoed name=\"%s\" OK\n",
		index, S.names[index]);

	if (want_revert) {
		char expect[16];
		snprintf(expect, sizeof expect, "%u", index + 1u);
		qdwin_shell_v1_set_workspace_name(S.shell, index, "");
		wl_display_roundtrip(S.display);
		wl_display_roundtrip(S.display);
		if (!S.names[index] || strcmp(S.names[index], expect) != 0) {
			fprintf(stderr, "wsname-probe: revert index %u "
				"name=\"%s\" want positional \"%s\"\n",
				index,
				S.names[index] ? S.names[index] : "(null)",
				expect);
			return 3;
		}
		fprintf(stderr, "wsname-probe: index %u reverted to \"%s\" OK\n",
			index, S.names[index]);
	}

	return 0;
}
