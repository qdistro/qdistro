/* qdwin-logic.c — implementations of the pure logic kernels declared in
 * qdwin-logic.h. Extracted verbatim from qdwin.c so the arithmetic /
 * enum mapping has a single source of truth that is unit-testable in
 * isolation (no weston, no wayland, no global state).
 */
#include "qdwin-logic.h"

#include <limits.h>
#include <string.h>

enum libinput_config_accel_profile
qdwin_accel_profile_to_libinput(uint32_t p)
{
	return (p == QDWIN_LOGIC_ACCEL_FLAT)
		? LIBINPUT_CONFIG_ACCEL_PROFILE_FLAT
		: LIBINPUT_CONFIG_ACCEL_PROFILE_ADAPTIVE;
}

enum libinput_config_scroll_method
qdwin_scroll_method_to_libinput(uint32_t m)
{
	switch (m) {
	case QDWIN_LOGIC_SCROLL_NONE:           return LIBINPUT_CONFIG_SCROLL_NO_SCROLL;
	case QDWIN_LOGIC_SCROLL_EDGE:           return LIBINPUT_CONFIG_SCROLL_EDGE;
	case QDWIN_LOGIC_SCROLL_ON_BUTTON_DOWN: return LIBINPUT_CONFIG_SCROLL_ON_BUTTON_DOWN;
	case QDWIN_LOGIC_SCROLL_TWO_FINGER:
	default:                                return LIBINPUT_CONFIG_SCROLL_2FG;
	}
}

uint32_t
qdwin_layer_exclusive_edge(uint32_t anchor, bool edge_set, uint32_t edge_val)
{
	if (edge_set)
		return edge_val;

#define QLS_T QDWIN_LOGIC_ANCHOR_TOP
#define QLS_B QDWIN_LOGIC_ANCHOR_BOTTOM
#define QLS_L QDWIN_LOGIC_ANCHOR_LEFT
#define QLS_R QDWIN_LOGIC_ANCHOR_RIGHT
	switch (anchor) {
	case QLS_T:                          return QLS_T;
	case QLS_B:                          return QLS_B;
	case QLS_L:                          return QLS_L;
	case QLS_R:                          return QLS_R;
	case (QLS_T | QLS_L | QLS_R):        return QLS_T;
	case (QLS_B | QLS_L | QLS_R):        return QLS_B;
	case (QLS_L | QLS_T | QLS_B):        return QLS_L;
	case (QLS_R | QLS_T | QLS_B):        return QLS_R;
	default:                             return 0;
	}
#undef QLS_T
#undef QLS_B
#undef QLS_L
#undef QLS_R
}

void
qdwin_layer_compute_box(uint32_t anchor,
			int32_t desired_w, int32_t desired_h,
			int32_t margin_top, int32_t margin_right,
			int32_t margin_bottom, int32_t margin_left,
			int32_t bx, int32_t by, int32_t bw, int32_t bh,
			int32_t *out_x, int32_t *out_y,
			uint32_t *out_w, uint32_t *out_h)
{
	const uint32_t T = QDWIN_LOGIC_ANCHOR_TOP;
	const uint32_t B = QDWIN_LOGIC_ANCHOR_BOTTOM;
	const uint32_t L = QDWIN_LOGIC_ANCHOR_LEFT;
	const uint32_t R = QDWIN_LOGIC_ANCHOR_RIGHT;
	uint32_t a = anchor;
	int32_t w = desired_w;
	int32_t h = desired_h;
	int32_t x, y;

	/* Horizontal */
	if (w == 0) {
		x = bx + margin_left;
		w = bw - (margin_left + margin_right);
	} else if ((a & L) && (a & R)) {
		x = bx + bw / 2 - w / 2;
	} else if (a & L) {
		x = bx + margin_left;
	} else if (a & R) {
		x = bx + bw - w - margin_right;
	} else {
		x = bx + bw / 2 - w / 2;
	}

	/* Vertical */
	if (h == 0) {
		y = by + margin_top;
		h = bh - (margin_top + margin_bottom);
	} else if ((a & T) && (a & B)) {
		y = by + bh / 2 - h / 2;
	} else if (a & T) {
		y = by + margin_top;
	} else if (a & B) {
		y = by + bh - h - margin_bottom;
	} else {
		y = by + bh / 2 - h / 2;
	}

	if (w < 0) w = 0;
	if (h < 0) h = 0;

	*out_x = x;
	*out_y = y;
	*out_w = (uint32_t)w;
	*out_h = (uint32_t)h;
}

int32_t
qdwin_inset_inner_extent(int32_t outer, int32_t inset_lead, int32_t inset_trail)
{
	int32_t inner = outer - inset_lead - inset_trail;
	if (inner < 1)
		inner = 1;
	return inner;
}

int32_t
qdwin_client_extent_for_geometry(int32_t desired_geometry,
				 int32_t surface_extent,
				 int32_t geometry_extent,
				 int32_t learned_delta)
{
	int64_t frame_delta = (int64_t)surface_extent - geometry_extent;
	if (frame_delta < 0)
		frame_delta = 0;
	if (learned_delta > frame_delta)
		frame_delta = learned_delta;
	int64_t configure = (int64_t)desired_geometry - frame_delta;
	if (configure < 1)
		return 1;
	if (configure > INT32_MAX)
		return INT32_MAX;
	return (int32_t)configure;
}

int32_t
qdwin_committed_restore_extent(bool xwayland, int32_t surface_extent,
				 int32_t geometry_extent)
{
	if (xwayland && surface_extent > 0)
		return surface_extent;
	if (geometry_extent > 0)
		return geometry_extent;
	return surface_extent > 0 ? surface_extent : 0;
}

static int32_t
qdwin_i64_to_i32(int64_t value)
{
	if (value < INT32_MIN)
		return INT32_MIN;
	if (value > INT32_MAX)
		return INT32_MAX;
	return (int32_t)value;
}

void
qdwin_rehome_outer_rect(int32_t outer_x, int32_t outer_y,
			 int32_t outer_w, int32_t outer_h,
			 int32_t work_x, int32_t work_y,
			 int32_t work_w, int32_t work_h,
			 int32_t *out_x, int32_t *out_y)
{
	int64_t ow = outer_w > 0 ? outer_w : 1;
	int64_t oh = outer_h > 0 ? outer_h : 1;
	int64_t ww = work_w > 0 ? work_w : 1;
	int64_t wh = work_h > 0 ? work_h : 1;
	int64_t min_x = work_x;
	int64_t min_y = work_y;
	int64_t max_x = ow <= ww ? min_x + ww - ow : min_x;
	int64_t max_y = oh <= wh ? min_y + wh - oh : min_y;
	int64_t x = outer_x;
	int64_t y = outer_y;

	if (x < min_x)
		x = min_x;
	else if (x > max_x)
		x = max_x;
	if (y < min_y)
		y = min_y;
	else if (y > max_y)
		y = max_y;

	if (out_x)
		*out_x = qdwin_i64_to_i32(x);
	if (out_y)
		*out_y = qdwin_i64_to_i32(y);
}

uint32_t
qdwin_clamp_fractional_scale_120(uint32_t raw_120)
{
	if (raw_120 < QDWIN_LOGIC_FRACTIONAL_MIN)
		return QDWIN_LOGIC_FRACTIONAL_MIN;
	if (raw_120 > QDWIN_LOGIC_FRACTIONAL_MAX)
		return QDWIN_LOGIC_FRACTIONAL_MAX;
	return raw_120;
}

bool
qdwin_fractional_scale_env_valid(long n)
{
	return n >= (long)QDWIN_LOGIC_FRACTIONAL_MIN &&
	       n <= (long)QDWIN_LOGIC_FRACTIONAL_MAX;
}

bool
qdwin_global_visible(enum qdwin_cred_class cred,
		     enum qdwin_global_kind kind)
{
	switch (kind) {
	case QDWIN_GLOBAL_ORDINARY:
		/* Non-privileged inherited globals are visible to everyone. */
		return true;
	case QDWIN_GLOBAL_INPUT_METHOD:
	case QDWIN_GLOBAL_VIRTUAL_KEYBOARD:
		/* Keystroke capture / injection: hidden from sandboxed silo
		 * clients (so one silo cannot keylog or inject into another),
		 * but available to the shell and ordinary session clients. The
		 * IME/VK bind handlers apply the further allowed_ime_uid + exe
		 * pins, so an ordinary client still can't actually grab. */
		return cred != QDWIN_CRED_SECCTX;
	case QDWIN_GLOBAL_WESTON_CAPTURE:
		/* Screen-pixel capture (whole-output). UNLIKE the IME/VK
		 * managers, libweston binds weston_capture_v1 unconditionally
		 * (no bind-time uid/exe pin — access is deferred to a
		 * screenshot-authority callback, which qdwin does not register,
		 * so capture requests default-deny today). A visible-but-
		 * unbound posture is therefore a LATENT hole: the day any
		 * authority is registered, every client that can see the global
		 * regains capture with no pin. So gate it tightest — shell-only
		 * (02/S1, D7) — denying ordinary AND silo clients, not just
		 * silos. qdshell (the bound shell) is the only legitimate
		 * capturer; an ordinary-uid screenshot product, if ever added,
		 * must register an authority that re-checks identity. */
		return cred == QDWIN_CRED_SHELL;
	case QDWIN_GLOBAL_SECCTX_MANAGER:
		/* Minting security contexts: only the bound shell (or the
		 * authorized secctx-exec helper, both QDWIN_CRED_SHELL) may
		 * even see it. Ordinary and silo clients get nothing. */
		return cred == QDWIN_CRED_SHELL;
	case QDWIN_GLOBAL_IDLE_NOTIFIER:
		/* Session idle/resume is a cross-silo presence/activity signal.
		 * Keep it available to trusted session components, but hide it
		 * from sandboxed security-context clients. */
		return cred != QDWIN_CRED_SECCTX;
	case QDWIN_GLOBAL_SHELL:
		/* The trusted shell interface (qdwin_shell_v1) — the ultimate
		 * cross-silo escalation surface (focus steal, selection clear,
		 * hotkeys, lock state). Hide it from sandboxed silo clients so a
		 * compromised silo app cannot see or race the shell role
		 * (findings F0). It must stay visible to the shell itself and to
		 * ordinary admin-uid session tools: qdshell is a plain admin
		 * client (ORDINARY) until it actually calls bind_as_shell, so a
		 * shell-only (CRED_SHELL) policy would hide the global from the
		 * very client that needs to bind it and break session startup.
		 * Same posture as IME/VK/idle: not-SECCTX. The bind handler
		 * (bind_qdwin_shell) applies the allowed_uid + singleton +
		 * secctx-deny pins on top. A fully-adversarial same-uid ORDINARY
		 * session is explicitly out of scope (threat-model.md); the
		 * in-scope threat is the secctx-tagged silo, which this denies. */
		return cred != QDWIN_CRED_SECCTX;
	case QDWIN_GLOBAL_LAYER_SHELL:
		/* zwlr_layer_shell_v1 — keyboard-focusable overlays / lock-screen
		 * surfaces. Legitimately used only by the shell and the locker
		 * (both non-secctx, admin-uid); no sandboxed silo app needs it.
		 * Hide it from secctx clients (findings F4, closing qdwin's own
		 * "Production TODO" to filter it) so the bind-time gate
		 * (bind_qdwin_layer_shell) becomes a redundant second layer
		 * instead of the sole defense. not-SECCTX, same rationale as the
		 * shell global re: ordinary clients and startup. */
		return cred != QDWIN_CRED_SECCTX;
	case QDWIN_GLOBAL_LOCKER:
		/* qdwin_locker_v1 can drive lock/unlock state. qdlocker is a
		 * non-secctx session component; sandboxed silo clients should not
		 * see the global, and bind_qdwin_locker keeps the uid/exe/label
		 * identity pin as the second layer. */
		return cred != QDWIN_CRED_SECCTX;
	case QDWIN_GLOBAL_NESTED_MANAGER:
		/* qdwin_nested_manager_v1 advertises proxy toplevels into the
		 * trusted shell. Hide it from secctx clients, then bind-time uid
		 * and secctx gates plus advertise caps enforce the remaining
		 * boundary. */
		return cred != QDWIN_CRED_SECCTX;
	case QDWIN_GLOBAL_TOUCH_CALIBRATION:
		/* weston_touch_calibration drives global touch-input calibration
		 * and grabs touch during a calibration session — a cross-silo
		 * input-integrity surface (a silo could remap or capture touch for
		 * the whole seat). libweston creates it at
		 * compositor->touch_calibration with NO bind-time peer pin, so the
		 * visibility filter is the gate. Hide it from sandboxed silo
		 * clients; ordinary admin-session tools (a calibrator) keep it.
		 * Same posture as IME/VK/idle: not-SECCTX. (02/S1 inherited-global
		 * sweep — the one privileged inherited global with a stable
		 * qdwin-reachable global pointer, unlike direct-display /
		 * content-protection whose create result libweston discards.) */
		return cred != QDWIN_CRED_SECCTX;
	}
	/* Unknown kind: fail closed (deny). A new privileged global added
	 * without a policy row must not default to visible. */
	return false;
}

bool
qdwin_nested_secctx_publisher_allowed(const char *sandbox_engine,
				      const char *peer_exe)
{
	/* The production tier-2 path mounts the root-owned qdwin shell module
	 * into its read-only image and connects the inner /usr/bin/weston through
	 * a launcher-minted qdistro.tier2 security-context socket. Do not accept
	 * a basename, an empty/unreadable executable, or another silo engine. */
	return sandbox_engine && peer_exe &&
	       strcmp(sandbox_engine, "qdistro.tier2") == 0 &&
	       strcmp(peer_exe, "/usr/bin/weston") == 0;
}

bool
qdwin_nested_pixelfeed_peer_allowed(const char *peer_exe)
{
	return peer_exe &&
	       (strcmp(peer_exe, "/usr/bin/qdistro-nested-pixelfeed") == 0 ||
	        strcmp(peer_exe, "/usr/bin/qdistro-mm-remote-pixelfeed") == 0);
}

bool
qdwin_remote_nested_publisher_allowed(const char *peer_exe)
{
	return peer_exe &&
	       strcmp(peer_exe, "/usr/bin/qdistro-mm-remote-viewer-helper") == 0;
}

bool
qdwin_nested_input_peer_event_current(int event_fd, int current_peer_fd)
{
	return event_fd >= 0 && current_peer_fd >= 0 &&
	       event_fd == current_peer_fd;
}

bool
qdwin_cursor_sprite_peer_allowed(const char *peer_exe)
{
	return peer_exe &&
	       strcmp(peer_exe, "/usr/bin/qdistro-cursor-sprites") == 0;
}

bool
qdwin_selection_serial_is_stale(uint32_t current, uint32_t incoming,
				bool has_current)
{
	if (!has_current)
		return false;
	return (current - incoming) < (UINT32_MAX / 2u);
}

bool
qdwin_om_mutation_allowed(bool client_is_bound_shell,
			  bool shell_bound,
			  pid_t client_pid, uid_t client_uid,
			  pid_t shell_pid, uid_t shell_uid,
			  uid_t allowed_uid)
{
	if (client_is_bound_shell)
		return true;
	if (shell_bound) {
		return client_pid > 0 && client_pid == shell_pid &&
		       client_uid == shell_uid;
	}
	if (allowed_uid == (uid_t)-1)
		return true;
	return client_uid == allowed_uid;
}

bool
qdwin_secctx_root_launcher_attested(uid_t parent_uid,
				    uint64_t parent_start_before,
				    uint64_t parent_start_after,
				    const char *parent_exe_basename)
{
	bool stable_parent = parent_uid == 0 &&
			     parent_start_before != 0 &&
			     parent_start_after != 0 &&
			     parent_start_before == parent_start_after;

	if (!stable_parent)
		return false;
	if (!parent_exe_basename)
		return false;
	if (strcmp(parent_exe_basename, "runuser") == 0 ||
	    strcmp(parent_exe_basename, "su") == 0 ||
	    strcmp(parent_exe_basename, "sudo") == 0 ||
	    strcmp(parent_exe_basename, "pkexec") == 0)
		return true;
	/* Deliberate fail-open: a more-privileged root parent can make
	 * /proc/<pid>/exe unreadable to the unprivileged compositor. The
	 * direct-root-parent + stable-starttime checks above are the pinned
	 * precondition for accepting the structurally unreadable basename. */
	return parent_exe_basename[0] == '\0';
}

bool
qdwin_layershell_pre_shell_uid_allowed(uid_t client_uid, uid_t allowed_uid)
{
	return client_uid == allowed_uid;
}

/* iso2/11 E2: an inhibitor only holds while its surface is mapped and
 * has both a buffer and a live view. Occlusion is deliberately not part of the test, and the
 * residual that leaves is stated — see qdwin-logic.h. */
bool
qdwin_idle_inhibit_should_hold(bool have_surface, bool surface_mapped,
			       bool has_buffer, bool has_view)
{
	return have_surface && surface_mapped && has_buffer && has_view;
}
