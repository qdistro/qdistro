/*
 * qdwin-input-probe — qdwin_shell_v1 v28 input-config conformance probe.
 *
 * Verifies the v28 live input-config requests (set_pointer_config /
 * set_key_repeat) are advertised at the new version, accepted by the
 * compositor as the bound shell, and FAIL SAFE on out-of-range values:
 * a deliberately bogus snapshot (accel speed far past ±1000, garbage
 * enum values, an absurd repeat rate/delay) must NOT raise a protocol
 * error or disconnect — the compositor clamps/normalises server-side.
 *
 * The probe is the shell-gated WRITER: qdwin only advertises
 * qdwin_shell_v1 to the allowed uid, so the host harness runs it as that
 * uid (same as qdwin-wsname-probe / qdwin-nested-probe). On a headless
 * qdwin there are no libinput devices, so the requests apply to nothing —
 * which is exactly the no-op-safe path we assert does not error.
 *
 * Reports via EXIT CODE so a headless host test can assert without
 * screenshots:
 *   0  success (bind at >= v28, both requests round-tripped cleanly)
 *   1  setup error (no display / missing global / bind refused)
 *   2  compositor advertised qdwin_shell_v1 below v28
 *   3  a request triggered a protocol error / disconnect (NOT fail-safe)
 *
 * Build: meson target qdwin-input-probe (see qdwin/meson.build).
 * Driven by tests/host/22-input-config.md.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wayland-client.h>

#include "qdwin-shell-v1-client-protocol.h"

struct state {
	struct wl_display *display;
	struct wl_registry *registry;
	struct qdwin_shell_v1 *shell;
	uint32_t shell_name;
	uint32_t shell_version;
	int shell_bound;
};

static struct state S;

static void sh_hello(void *d, struct qdwin_shell_v1 *s, uint32_t uid)
{ (void)d; (void)s; (void)uid; S.shell_bound = 1; }

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
};

static void
reg_global(void *d, struct wl_registry *r, uint32_t name, const char *iface,
	   uint32_t version)
{
	(void)d;
	if (strcmp(iface, qdwin_shell_v1_interface.name) == 0) {
		uint32_t v = version < 28u ? version : 28u;
		S.shell = wl_registry_bind(r, name, &qdwin_shell_v1_interface, v);
		S.shell_name = name;
		S.shell_version = version;
	}
}

static void reg_global_remove(void *d, struct wl_registry *r, uint32_t name)
{ (void)d; (void)r; (void)name; }

static const struct wl_registry_listener registry_listener = {
	.global = reg_global,
	.global_remove = reg_global_remove,
};

int
main(void)
{
	S.display = wl_display_connect(NULL);
	if (!S.display) {
		fprintf(stderr, "input-probe: no display\n");
		return 1;
	}
	S.registry = wl_display_get_registry(S.display);
	wl_registry_add_listener(S.registry, &registry_listener, NULL);
	wl_display_roundtrip(S.display);

	if (!S.shell) {
		fprintf(stderr, "input-probe: qdwin_shell_v1 not advertised "
			"(uid gate / wrong version?)\n");
		return 1;
	}
	if (S.shell_version < 28u) {
		fprintf(stderr, "input-probe: qdwin_shell_v1 advertised v%u < 28\n",
			S.shell_version);
		return 2;
	}

	qdwin_shell_v1_add_listener(S.shell, &shell_listener, NULL);
	qdwin_shell_v1_bind_as_shell(S.shell);
	wl_display_roundtrip(S.display);
	if (!S.shell_bound) {
		fprintf(stderr, "input-probe: bind_as_shell not acked\n");
		return 1;
	}

	/* 1) A sane snapshot. */
	qdwin_shell_v1_set_pointer_config(S.shell,
		/*accel_speed*/ 200, /*accel_profile*/ 1 /*flat*/,
		/*natural_scroll*/ 1, /*tap_to_click*/ 1, /*left_handed*/ 0,
		/*middle_emulation*/ 1, /*disable_while_typing*/ 1,
		/*scroll_method*/ 1 /*two_finger*/);
	qdwin_shell_v1_set_key_repeat(S.shell, /*rate*/ 25, /*delay*/ 600);
	if (wl_display_roundtrip(S.display) < 0) {
		fprintf(stderr, "input-probe: disconnect after valid config\n");
		return 3;
	}

	/* 2) A deliberately out-of-range / garbage snapshot. The compositor
	 * must clamp/normalise server-side and NOT post a protocol error. */
	qdwin_shell_v1_set_pointer_config(S.shell,
		/*accel_speed*/ 999999, /*accel_profile*/ 9999,
		/*natural_scroll*/ 7, /*tap_to_click*/ 7, /*left_handed*/ 7,
		/*middle_emulation*/ 7, /*disable_while_typing*/ 7,
		/*scroll_method*/ 4242);
	qdwin_shell_v1_set_key_repeat(S.shell, /*rate*/ 0xffffffu,
				      /*delay*/ 0u);
	if (wl_display_roundtrip(S.display) < 0) {
		fprintf(stderr, "input-probe: disconnect after out-of-range "
			"config (NOT fail-safe)\n");
		return 3;
	}

	/* A final sync confirms the connection survived both snapshots. */
	if (wl_display_roundtrip(S.display) < 0) {
		fprintf(stderr, "input-probe: connection died post-config\n");
		return 3;
	}

	fprintf(stderr, "input-probe: v%u set_pointer_config + set_key_repeat "
		"accepted, out-of-range fail-safe OK\n", S.shell_version);
	return 0;
}
