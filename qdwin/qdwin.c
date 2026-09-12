/*
 * qdwin — qdistro compositor, libweston shell plugin.
 *
 * Phase 6.1 (S2): qdwin is now the weston shell. Registers
 * weston_desktop_api, tracks per-toplevel state on dedicated layers,
 * and emits qdwin_shell_v1 events so a bound shell client can drive
 * decoration and window state.
 *
 * Surfaces start on the "held" layer (WESTON_LAYER_POSITION_HIDDEN)
 * and stay invisible until set_border_color / attach_decoration
 * moves them to "normal" (release_holding). Minimised toplevels move
 * to minimized_layer, which is deliberately *not* set_position'd so
 * weston_view_move_to_layer() calls weston_view_unmap on entry and
 * remaps on exit — matching desktop-shell's minimised pattern.
 * An opaque black background curtain sits on background_layer so the
 * pixman renderer never leaves stale pixels where a view shrank or
 * was unmapped.
 *
 * Spec: doc/qdistro/the architecture doc,
 *       doc/qdistro/ §S2.
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE  /* accept4(), SOCK_CLOEXEC for §6.10 secctx accept loop */
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/input-event-codes.h>
#include <math.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/random.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include <libweston/libweston.h>
#include <libweston/desktop.h>
#include <libweston/plugin-registry.h>
#include <libweston/shell-utils.h>
#include <libweston/xwayland-api.h>
#include <wayland-server-core.h>
#include <libinput.h>
#include <X11/Xcursor/Xcursor.h>

/* Exported by the weston frontend binary; not in the installed plugin
 * header, but resolvable at dlopen time against the executable's
 * symbol table (same trick kiosk-shell uses). */
int screenshooter_create(struct weston_compositor *ec);

/* qdwin builds against the distribution's stable libweston headers while the
 * in-tree RDP backend carries an additive tail to the v2 API. Keep a private
 * ABI view of that registered vtable: the first three entries are the stock v2
 * layout and the final entry is the input gate. Function-pointer
 * representation is the ABI contract; qdwin calls only the final typed
 * member. */
#define QDWIN_RDP_OUTPUT_API_NAME "weston_rdp_output_api_v2"
struct qdwin_rdp_output_api_extended_v2 {
	void (*head_get_monitor)(void);
	void (*output_set_mode)(void);
	void (*disable_output_resize)(void);
	bool (*output_set_input_enabled)(struct weston_output *output,
					 bool enabled);
};

static const struct qdwin_rdp_output_api_extended_v2 *
qdwin_rdp_output_get_api(struct weston_compositor *compositor)
{
	return weston_plugin_api_get(compositor, QDWIN_RDP_OUTPUT_API_NAME,
				     sizeof(struct qdwin_rdp_output_api_extended_v2));
}

#include "qdwin-shell-v1-server-protocol.h"
#include "qdwin-locker-v1-server-protocol.h"
#include "xdg-activation-v1-server-protocol.h"
#include "ext-idle-notify-v1-server-protocol.h"
#include "idle-inhibit-unstable-v1-server-protocol.h"
#include "cursor-shape-v1-server-protocol.h"
#include "fractional-scale-v1-server-protocol.h"
#include "text-input-unstable-v3-server-protocol.h"
#include "input-method-unstable-v2-server-protocol.h"
#include "virtual-keyboard-unstable-v1-server-protocol.h"
#include "ext-workspace-v1-server-protocol.h"
#include "wlr-output-management-unstable-v1-server-protocol.h"
#include "primary-selection-unstable-v1-server-protocol.h"
#include "security-context-v1-server-protocol.h"
#include "qdwin-nested-v1-server-protocol.h"
#include "wlr-layer-shell-unstable-v1-server-protocol.h"
#include "xdg-decoration-unstable-v1-server-protocol.h"

/* §6.8 S1: nested-mode publisher needs the wl_client side; symbols
 * isolated to qdwin-nested-client.c, opaque-pointer interface here. */
#include "qdwin/qdwin-nested-client.h"
#include <libweston/pipewire-plugin.h>

/* Pure logic kernels (enum maps, layer-shell geometry, fractional-scale
 * clamp) live in qdwin-logic.c so they can be unit-tested in isolation
 * (tests/unit/test-qdwin-logic.c). The wrappers below read struct fields
 * and delegate the actual arithmetic to these kernels. */
#include "qdwin/qdwin-logic.h"

/* v33 retained-frame capture: weak prototypes for the vendored-libweston
 * additions (absent from stock installed headers; see the header comment). */
#include "qdwin/qdwin-capture-retention.h"

/* §6.5 S5c: libweston-16 exports these from libweston-16.so but does not
 * declare them in the installed plugin header (they're backend-facing).
 * Declare locally so qdwin-shell can act as a virtual input backend for
 * per-stream seats. Signatures tracked against weston 15.0.0 source
 * (compatible with 14.x ABI). */
void weston_seat_init(struct weston_seat *seat,
		      struct weston_compositor *ec,
		      const char *seat_name);
void weston_seat_release(struct weston_seat *seat);
int  weston_seat_init_pointer(struct weston_seat *seat);
int  weston_seat_init_keyboard(struct weston_seat *seat,
			       struct xkb_keymap *keymap);
int  weston_seat_init_touch(struct weston_seat *seat);
void weston_seat_release_pointer(struct weston_seat *seat);
void weston_seat_release_keyboard(struct weston_seat *seat);
void weston_seat_release_touch(struct weston_seat *seat);
/* J29 14->16: the synthetic-input producers changed shape. Each now takes a
 * single pre-initialized event object (seat + timestamp live in event->base),
 * and notify_motion_absolute() was REMOVED — absolute motion is notify_motion()
 * with WESTON_POINTER_MOTION_ABS in the event mask. These are still not in the
 * installed plugin header, so declare the 16 forms locally. */
void notify_motion(const struct weston_pointer_motion_event *event);
void notify_button(const struct weston_pointer_button_event *event);
void notify_axis(const struct weston_pointer_axis_event *event);
void notify_key(const struct weston_key_event *key_event);
/* Backend-facing in libweston 16 (exported, but declared only in the private
 * libweston/backend.h). The v32 shell capture preparation request needs the
 * full_repaint_needed behavior, not schedule_repaint alone. */
void weston_output_damage(struct weston_output *output);

/* Thin wrappers that keep the old (seat, time, ...) call shape used by the
 * per-stream / nested / IME injection sites and build the 16 event via the
 * public weston_*_event_init() helpers (which populate event->base.{seat,ts}).
 * Without these the sites would pass a weston_seat* where 16 expects an event
 * pointer — a runtime crash on first injected input — and notify_motion_absolute
 * would be an undefined symbol that fails the shell's RTLD_NOW dlopen. */
static inline void
qdwin_notify_motion_absolute(struct weston_seat *seat,
			     const struct timespec *time,
			     struct weston_coord_global pos)
{
	struct weston_pointer_motion_event event;
	struct timespec ts = *time;
	weston_pointer_motion_event_init(&event, &ts, seat,
					 WESTON_POINTER_MOTION_ABS,
					 &pos, NULL, NULL);
	notify_motion(&event);
}
static inline void
qdwin_notify_button(struct weston_seat *seat, const struct timespec *time,
		    int32_t button, enum wl_pointer_button_state state)
{
	struct weston_pointer_button_event event;
	struct timespec ts = *time;
	weston_pointer_button_event_init(&event, &ts, seat,
					 (uint32_t)button, state);
	notify_button(&event);
}
static inline void
qdwin_notify_axis(struct weston_seat *seat, const struct timespec *time,
		  const struct weston_pointer_axis_event *in)
{
	struct weston_pointer_axis_event event;
	struct timespec ts = *time;
	weston_pointer_axis_event_init(&event, &ts, seat, in->axis, in->value,
				       in->has_discrete, in->discrete);
	notify_axis(&event);
}
static inline void
qdwin_notify_key(struct weston_seat *seat, const struct timespec *time,
		 uint32_t key, enum wl_keyboard_key_state state,
		 enum weston_key_state_update update_state)
{
	struct weston_key_event event;
	struct timespec ts = *time;
	weston_key_event_init(&event, &ts, seat, key, state, update_state);
	notify_key(&event);
}
struct qdwin;

enum qdwin_side {
	QDWIN_SIDE_N = 0,
	QDWIN_SIDE_E = 1,
	QDWIN_SIDE_S = 2,
	QDWIN_SIDE_W = 3,
	QDWIN_SIDES = 4,
};

struct qdwin_chrome {
	/* One per side. The shell owns the wl_surface; we own the view
	 * and a destroy listener so the view tears down if the client
	 * destroys the surface. The commit listener fires when the shell
	 * attaches a new buffer; we use it to damage_below the previous
	 * view geometry so libweston re-renders the area no longer
	 * covered when the chrome shrinks (e.g. after restore-from-max).
	 * Roleless surfaces don't get this for free the way desktop
	 * surfaces do. */
	struct weston_surface *surface;
	struct weston_view *view;
	struct wl_listener surface_destroy;
	struct wl_listener surface_commit;
	struct qdwin_toplevel *tl;   /* back-pointer for the listener */
	int side;
};

struct qdwin_toplevel;
struct qdwin_popup;
struct qdwin_view_stream;

static void qdwin_chrome_detach(struct qdwin_chrome *c);
static void qdwin_toplevel_position_chrome(struct qdwin_toplevel *tl);
static void qdwin_move_grab_end_for(struct qdwin *qdwin, uint32_t handle);
/* §6.8 S1 nested-mode forward decls (definitions near wet_shell_init). */
static void qdwin_nested_publish_toplevel(struct qdwin_toplevel *tl);
static void qdwin_nested_unpublish_toplevel(struct qdwin_toplevel *tl);
/* §6.8 S3 input-sink callbacks (definitions near nested-mode publisher). */
static int qdwin_nested_input_sink_listen_cb(int fd, uint32_t mask, void *data);
static int qdwin_nested_input_sink_peer_cb(int fd, uint32_t mask, void *data);
/* §6.8 S2 outer-proxy forward decls (definitions near wet_shell_init). */
struct qdwin_nested_toplevel;
static struct qdwin_toplevel *
qdwin_nested_proxy_create(struct qdwin *qdwin,
			  struct qdwin_nested_toplevel *owner,
			  const char *app_id, const char *title,
			  uint32_t origin_uid, int w, int h);
static void qdwin_nested_proxy_destroy(struct qdwin_toplevel *tl);
static void qdwin_nested_proxy_set_title(struct qdwin_toplevel *tl,
					 const char *title);
static void qdwin_nested_proxy_set_app_id(struct qdwin_toplevel *tl,
					  const char *app_id);
static void qdwin_nested_proxy_set_geometry(struct qdwin_toplevel *tl,
					    int w, int h);
/* §6.8 S4 admin-gate forward decls. */
static void qdwin_handle_nested_proxy_decision(struct wl_client *client,
					       struct wl_resource *resource,
					       uint32_t handle,
					       uint32_t decision,
					       const char *reason);
static void qdwin_toplevel_autofocus_if_ready(struct qdwin_toplevel *tl);
static void qdwin_toplevel_release_holding(struct qdwin_toplevel *tl,
					   const char *cause);
/* F6#2: the allow-transition core (clear pending, swap-or-capture, release
 * holding). Shared by the real shell decision handler and the env-gated S3d
 * post-allow test hook so both drive the IDENTICAL code path. Defined after
 * qdwin_nested_proxy_activate_pixel_surface; forward-declared here because the
 * S3d test fire (above the decision handler) calls it. */
static void qdwin_nested_proxy_apply_allow(struct qdwin_toplevel *tl,
					   const char *cause);
static void qdwin_toplevel_move_to_layer(struct qdwin_toplevel *tl,
					 struct weston_layer *layer);
static void qdwin_maybe_promote_lock_toplevel(struct qdwin *qdwin,
					      struct qdwin_toplevel *tl,
					      const char *cause);
static void qdwin_demote_lock_toplevel(struct qdwin *qdwin,
				       const char *cause);
static void qdwin_install_lock_curtain(struct qdwin *qdwin);
static void qdwin_remove_lock_curtain(struct qdwin *qdwin);
static void qdwin_lock_curtain_to_bottom(struct qdwin *qdwin);
static struct qdwin_toplevel *
qdwin_toplevel_from_handle(struct qdwin *qdwin, uint32_t handle);
/* v24 workspaces — mechanics + ext-workspace-v1 server. Forward-declared
 * because the qdwin_shell_v1 impl table (move_toplevel_to_workspace) and
 * the ext-workspace request handlers reference them before their
 * definitions (which sit near qdwin_toplevel_by_surface lower down). */
static bool qdwin_toplevel_is_visible(struct qdwin_toplevel *tl);
static void qdwin_toplevel_apply_workspace_visibility(struct qdwin_toplevel *tl);
static void qdwin_workspace_refocus_seats(struct qdwin *qdwin);
static void qdwin_set_active_workspace(struct qdwin *qdwin, uint32_t index);
static void qdwin_workspace_create(struct qdwin *qdwin);
static void qdwin_workspace_remove(struct qdwin *qdwin, uint32_t index);
static void qdwin_emit_toplevel_workspace(struct qdwin *qdwin,
					  struct qdwin_toplevel *tl);
/* v27: re-broadcast ext_workspace_handle_v1.name for one workspace index on
 * every bound ext-workspace manager (after the shell set/cleared a name). */
static void qdwin_ext_ws_broadcast_name(struct qdwin *qdwin, uint32_t index);
static void qdwin_ext_ws_broadcast_state(struct qdwin *qdwin);
static void qdwin_ext_ws_resync_all(struct qdwin *qdwin);
/* Output (display) management: re-emit the head/mode/state set to every
 * bound wlr-output-management manager (with a fresh serial) after an output
 * hotplug/resize. Defined near the rest of the output-management code. */
static void qdwin_om_resync_all(struct qdwin *qdwin);
/* R8/A4 return-home boundary: output_destroyed calls this while the removed
 * output is still valid, before publishing output_removed. The definition is
 * deliberately late so it can drain layer-shell, IME and primary-selection
 * objects whose complete types are declared below. */
static void qdwin_output_boundary_transition(struct qdwin *qdwin,
					      struct weston_output *removed);
static void qdwin_output_boundary_cancel_state(struct qdwin *qdwin,
					       struct weston_output *removed);
/* §6.8 cursor-sprite full theme forward decl (impl table at L3577 needs
 * the symbol; definition lives near the rest of the cursor-shape code). */
static void qdwin_handle_set_cursor_sprite(struct wl_client *client,
					   struct wl_resource *resource,
					   uint32_t shape,
					   struct wl_resource *surface_resource,
					   int32_t hotspot_x,
					   int32_t hotspot_y);

/* §6.8 S2b bind_proxy_pixels forward decl. */
static void qdwin_handle_bind_proxy_pixels(struct wl_client *client,
					   struct wl_resource *resource,
					   uint32_t handle,
					   struct wl_resource *surface);
/* Send close_requested on the nested-toplevel resource owned by the
 * proxy. Defined alongside the full qdwin_nested_toplevel struct
 * (qdwin_nested_toplevel is opaque at the point this is called from
 * qdwin_handle_request_close). */
static void qdwin_nested_proxy_send_close(struct qdwin_toplevel *tl);
static void qdwin_popup_teardown(struct qdwin_popup *p);
static void qdwin_view_stream_unpin(struct qdwin_view_stream *s);
static void qdwin_view_stream_reap_forward(struct qdwin_view_stream *s);
static void qdwin_view_stream_terminate(struct qdwin_view_stream *s,
					const char *reason, pid_t audit_pid);
/* The single dependent-teardown routine shared by BOTH toplevel destroy
 * paths (real desktop surface and nested proxy). Defined after
 * qdwin_view_stream_terminate. */
static void qdwin_toplevel_release_dependents(struct qdwin *qdwin,
					      struct qdwin_toplevel *tl);
static void qdwin_stream_seat_init(struct qdwin_view_stream *s);
static void qdwin_stream_seat_release(struct qdwin_view_stream *s);
static void qdwin_stream_confine_grab_end(struct qdwin_view_stream *s);
/* spec/10 clear_selection forward decls — defined in the
 * primary-selection-unstable-v1 block far below, but the qdwin_shell
 * impl table at line ~3600 needs them. */
struct qdwin_primary_seat;
static struct qdwin_primary_seat *
qdwin_primary_seat_find(struct qdwin *qdwin, struct weston_seat *seat);
static void
qdwin_primary_seat_clear_selection(struct qdwin_primary_seat *pseat,
				   int notify_source);
/* True when the primary seat currently owns a selection source. Defined with
 * the struct far below; declared here so the (idempotent) clear_selection
 * handler can test emptiness without the opaque type in scope. */
static bool
qdwin_primary_seat_has_source(struct qdwin_primary_seat *pseat);
/* spec/09 activation_decision forward decl — defined in the
 * xdg-activation-v1 block far below. */
static void
qdwin_handle_activation_decision(struct wl_client *client,
				 struct wl_resource *resource,
				 uint32_t handle,
				 uint32_t decision,
				 const char *reason);
struct qdwin;  /* not yet defined here */
/* v25 window-manager policy forward decls — implementations live in the
 * "live window-manager policy" block; the default pointer grab (focus
 * follows mouse + raise-on-click) and the map handler (placement) call
 * these before that block. */
struct qdwin_wm_policy;
static void qdwin_wm_policy_set_defaults(struct qdwin_wm_policy *p);
static void qdwin_ffm_consider(struct qdwin *qdwin,
			       struct weston_pointer *pointer);
static void qdwin_ffm_cancel(struct qdwin *qdwin);
static void qdwin_toplevel_raise(struct qdwin_toplevel *tl);
static void qdwin_toplevel_focus(struct qdwin *qdwin,
				 struct qdwin_toplevel *tl, int raise);
static void qdwin_compute_placement(struct qdwin *qdwin,
				    struct qdwin_toplevel *tl,
				    struct weston_surface *surface,
				    struct weston_output *out,
				    int *cx, int *cy);
static void qdwin_snap_move_position(struct qdwin *qdwin,
				     struct qdwin_toplevel *tl,
				     double *nx, double *ny);
static void qdwin_toplevel_clear_tiled(struct qdwin_toplevel *tl);
static void qdwin_activation_pending_free_all(struct qdwin *qdwin);
static void qdwin_data_offer_pending_free_all(struct qdwin *qdwin);
static void qdwin_data_source_wraps_free_all(struct qdwin *qdwin);
static void qdwin_handle_data_offer_receive_decision(
	struct wl_client *client, struct wl_resource *resource,
	uint32_t handle, const char *decision);
/* §6.10 forward decls — definitions live near wet_shell_init. */
static void qdwin_secctx_destroy_all(struct qdwin *qdwin);
struct qdwin_secctx_client;
static struct qdwin_secctx_client *
qdwin_secctx_client_lookup(struct qdwin *qdwin, struct wl_client *client);
/* Used by the input-method bind gate (defined far below near the secctx
 * code) to reject sandboxed clients from acting as the privileged IME. */
static struct qdwin_secctx_client *
qdwin_secctx_client_find(struct qdwin *qdwin, struct wl_client *client);
static const char *qdwin_secctx_client_engine(struct qdwin_secctx_client *sc);
static const char *qdwin_secctx_client_app_id(struct qdwin_secctx_client *sc);
static const char *qdwin_secctx_client_instance_id(struct qdwin_secctx_client *sc);
/* Option-B identity accessors — see todo/decisions/secctx-identity-contract.md. */
static uint32_t qdwin_secctx_client_peer_pid(struct qdwin_secctx_client *sc);
static uint64_t qdwin_secctx_client_peer_starttime(struct qdwin_secctx_client *sc);
static uint32_t qdwin_secctx_client_peer_uid(struct qdwin_secctx_client *sc);
static const char *qdwin_secctx_client_peer_exe(struct qdwin_secctx_client *sc);
static const char *qdwin_secctx_client_peer_selinux_label(struct qdwin_secctx_client *sc);
static bool
qdwin_secctx_global_filter(const struct wl_client *client,
			   const struct wl_global *global, void *data);
static void
bind_qdwin_secctx_manager(struct wl_client *client, void *data,
			  uint32_t version, uint32_t id);

/* findings F7: the standard fail-closed snapshot dup for qdwin. Returns NULL on
 * a NULL input OR on allocation failure, logging (once, rate-naive) on OOM
 * instead of failing silently. Behaviourally identical to the
 * `s ? strdup(s) : NULL` idiom it replaces — both yield NULL for a NULL input
 * AND for an OOM'd non-NULL input — but it makes the OOM visible and self-
 * documents that the site is deliberately NULL-tolerant: every qdwin consumer of
 * these snapshot fields guards the read (accessors fall back to ""), so a NULL
 * here fails closed (an OOM'd identity cannot match a silo gate) rather than
 * crashing. Use this for INTERNALLY-snapshotted strings (cached titles/app_ids,
 * proxied/text-input/IME state, identity captures). For an EXTERNALLY-set
 * protocol string in a request handler prefer wl_resource_post_no_memory() so
 * the client learns the request was dropped (e.g. the secctx set-path); for a
 * security identity the caller must additionally treat NULL as "no match" — see
 * the bind gates and test_identity_gate_oom_failclosed.py. */
static char *
qdwin_xstrdup_or_null(const char *s)
{
	char *d;
	if (!s)
		return NULL;
	d = strdup(s);
	if (!d)
		weston_log("qdwin: strdup OOM (len=%zu) — field left unset\n",
			   strlen(s));
	return d;
}

/* toplevel_state bits — must match qdwin-shell-v1.xml's event docs. */
#define QDWIN_TS_MAXIMIZED  (1u << 0)
#define QDWIN_TS_FULLSCREEN (1u << 1)
#define QDWIN_TS_MINIMIZED  (1u << 2)
#define QDWIN_TS_URGENT     (1u << 3)
#define QDWIN_TS_FOCUSED    (1u << 4)
#define QDWIN_TS_FLOATING   (1u << 5)

/* v25: live window-manager policy (qdwin_shell_v1.set_wm_policy). Mirrors
 * the qdshell WindowManager settings tab; values match the protocol enums
 * in qdwin-shell-v1.xml. */
enum qdwin_focus_policy {
	QDWIN_FOCUS_CLICK        = 0,  /* focus only on click */
	QDWIN_FOCUS_FOLLOW_MOUSE = 1,  /* focus-follows-mouse */
};
enum qdwin_placement {
	QDWIN_PLACE_CENTER      = 0,
	QDWIN_PLACE_UNDER_MOUSE = 1,
	QDWIN_PLACE_SMART       = 2,
	QDWIN_PLACE_CASCADE     = 3,
};
/* request_tile edge (matches qdwin_shell_v1.tile_edge). */
#define QDWIN_TILE_NONE  0u
#define QDWIN_TILE_LEFT  1u
#define QDWIN_TILE_RIGHT 2u

#define QDWIN_FFM_DELAY_MAX  1000u
#define QDWIN_SNAP_DIST_MIN  1u
#define QDWIN_SNAP_DIST_MAX  64u

struct qdwin_wm_policy {
	uint32_t focus_policy;   /* enum qdwin_focus_policy */
	uint32_t ffm_delay_ms;   /* 0..1000 */
	int raise_on_click;
	int raise_on_hover;
	uint32_t placement;      /* enum qdwin_placement */
	int snap_enabled;
	uint32_t snap_distance;  /* 1..64 px */
};

/* v28: live libinput pointer/touchpad config (qdwin_shell_v1.set_pointer_config)
 * and xkb key-repeat (set_key_repeat). Values match the protocol enums /
 * ranges in qdwin-shell-v1.xml. */
#define QDWIN_ACCEL_SPEED_MIN  (-1000)
#define QDWIN_ACCEL_SPEED_MAX  (1000)
#define QDWIN_KB_RATE_MAX      255u    /* wl_keyboard repeat rate (Hz), 0=off */
#define QDWIN_KB_DELAY_MIN     1u
#define QDWIN_KB_DELAY_MAX     10000u
/* D5: per-source primary-selection MIME-list cap (DoS guard). Far above any
 * legitimate target set (real sources advertise a handful to a few dozen). */
#define QDWIN_PRIMARY_MIME_MAX 256u

/* F7: per-MIME-type length cap (DoS guard) for externally-supplied mime
 * strings that we duplicate and re-broadcast (the data_offer.receive shim
 * forwards it to the shell). Real mime types are short (RFC 6838); 4096
 * matches QDWIN_SECCTX_STR_MAX and is far above any legitimate name. */
#define QDWIN_MIME_TYPE_MAX 4096u

enum qdwin_accel_profile {
	QDWIN_ACCEL_ADAPTIVE = 0,
	QDWIN_ACCEL_FLAT     = 1,
};
enum qdwin_scroll_method {
	QDWIN_SCROLL_NONE           = 0,
	QDWIN_SCROLL_TWO_FINGER     = 1,
	QDWIN_SCROLL_EDGE           = 2,
	QDWIN_SCROLL_ON_BUTTON_DOWN = 3,
};

/* Minimal ABI mirrors of libweston's backend-libinput internal structs
 * (src/libweston/libinput-seat.h, libinput-device.h). We do NOT include
 * those internal headers (they pull in a private config.h and aren't part
 * of the installed -devel package); instead we replicate the exact field
 * PREFIX we read so wl_list iteration and the libinput_device pointer land
 * at the right offsets. Reached only under the DRM/libinput backend
 * (qdwin->libinput_backend), where weston_seat IS the first member of
 * udev_seat. Keep these in sync with the vendored libweston if it bumps. */
struct qdwin_udev_seat_abi {
	struct weston_seat base;        /* MUST be first — matches udev_seat */
	struct wl_list devices_list;    /* struct evdev_device::link */
	/* (remaining udev_seat fields omitted — never read) */
};
struct qdwin_evdev_device_abi {
	struct weston_seat *seat;
	int seat_caps;                  /* enum evdev_device_seat_capability */
	struct libinput_device *device;
	struct weston_touch_device *touch_device;
	struct wl_list link;            /* into udev_seat::devices_list */
	/* (remaining evdev_device fields omitted — never read) */
};

struct qdwin_pointer_config {
	int valid;                  /* 0 until the shell pushes a snapshot */
	int32_t accel_speed;        /* milli-units, -1000..1000 */
	uint32_t accel_profile;     /* enum qdwin_accel_profile */
	int natural_scroll;
	int tap_to_click;
	int left_handed;
	int middle_emulation;
	int disable_while_typing;
	uint32_t scroll_method;     /* enum qdwin_scroll_method */
};

struct qdwin_toplevel {
	struct qdwin *qdwin;
	struct weston_desktop_surface *desktop_surface;
	struct weston_view *view;
	uint32_t handle;
	int mapped;
	int decorated;
	int last_width;
	int last_height;
	/* §6.8 S1 (nested mode): set when this toplevel has been pinned to
	 * a pipewire output and advertised to the outer qdwin. NULL on
	 * outer-mode shells. */
	struct weston_output *nested_pw_output;
	struct qdwin_nested_client_pub *nested_pub;
	struct qdwin_nested_input_sink *nested_input_sink;  /* nested-side */
	struct wl_event_source *nested_input_sink_source;
	struct wl_event_source *nested_input_peer_source;
	int32_t nested_configured_w;
	int32_t nested_configured_h;
	/* §6.8 S3b: per-toplevel weston_seat for inner-client input
	 * delivery. Created lazily on first non-PING packet so PING-only
	 * S3-mvp probes don't pay the seat-init cost. Pointer + keyboard
	 * focus are pinned to tl->view (the inner client's surface). */
	struct weston_seat nested_inner_seat;
	bool nested_inner_seat_inited;
	/* §6.8 S2 (outer mode): set when this toplevel was synthesised
	 * from a qdwin_nested_v1.advertise_toplevel — proxy_curtain owns
	 * tl->view (the placeholder pixels), proxy_app_id / proxy_title
	 * cache the metadata since desktop_surface is NULL for proxies. */
	bool is_nested_proxy;
	struct weston_curtain *proxy_curtain;
	char *proxy_app_id;
	char *proxy_title;
	uint32_t proxy_origin_uid;
	/* J12 Fix B: secctx tag of the *advertising* container connection (the
	 * tier-2 nested compositor, tagged via wp_security_context_v1),
	 * captured at advertise_toplevel time. A proxy has desktop_surface ==
	 * NULL, so qdwin_send_toplevel_security_context cannot resolve a backing
	 * wl_client — it reads these instead. proxy_secctx_set stays false for an
	 * untagged advertiser, so nothing is emitted (absence == not sandboxed,
	 * the v13 contract). */
	char *proxy_secctx_engine;
	char *proxy_secctx_app_id;
	char *proxy_secctx_instance;
	bool proxy_secctx_set;
	/* v31: authority-bound remote identity. These fields are populated only
	 * by qdwin_nested_toplevel_v1.set_remote_identity after exact publisher
	 * executable verification; app_id/title never participate in attribution. */
	char *proxy_remote_source_machine;
	char *proxy_remote_trust_domain_id;
	char *proxy_remote_stream_id;
	uint64_t proxy_remote_generation;
	/* §6.8 S2b: when the admin shell calls bind_proxy_pixels, the
	 * compositor swaps the placeholder curtain view for a view of the
	 * bound surface. proxy_pixel_view is the active view (== tl->view),
	 * proxy_pixel_surface is the underlying weston_surface (lifetime
	 * tied to the consumer wl_client). The destroy listener restores
	 * the placeholder curtain when the consumer goes away. */
	struct weston_surface *proxy_pixel_surface;
	struct weston_view *proxy_pixel_view;
	struct wl_listener proxy_pixel_destroy_listener;
	/* §6.8 S3: outer-side fd to the nested-published input sink. -1
	 * when no sink (e.g. pre-S3 advertise with input_sink=""). Used
	 * to send PING + (S3b) motion/button/key packets. */
	int proxy_input_sink_fd;
		/* §6.8 S4: pending admin gating. Proxy stays held until shell
		 * issues nested_proxy_decision. Cleared on any decision (allow
		 * → moves to normal; deny → destroys; defer → stays held with
		 * pending=true). In production/default config this is also true
		 * when no v8+ shell can decide, so no-shell advertises fail
		 * closed unless QDWIN_NESTED_BROKER_OPTIONAL=1 is set. */
		bool nested_proxy_pending_decision;
	/* iso2/10 E2: set for the whole of qdwin_nested_proxy_destroy.
	 * Teardown ends grabs, and weston_pointer_end_grab() reinstalls the
	 * default grab and runs its focus() callback SYNCHRONOUSLY — with
	 * the dying proxy's curtain still mapped and still in
	 * qdwin->toplevels, the picker would hand it straight back and
	 * qdwin_proxy_pointer_track_focus() would cache it in
	 * active_input_proxy (and write focus=1 to its input sink) moments
	 * before free(tl). A destroying proxy is not selectable. */
	bool proxy_destroying;
	/* Back-ref so the qdwin_nested_toplevel resource destroy can
	 * tear down the proxy. NULL on non-proxy toplevels. */
	struct qdwin_nested_toplevel *proxy_nested_owner;
	/* Chrome inset, derived from attached chrome surfaces' sizes. */
	int inset_n, inset_e, inset_s, inset_w;
	/* Last outer size we asked the client to configure to, so repeated
	 * attach_decoration calls don't ratchet the window smaller. */
	int outer_width;
	int outer_height;
	/* XWayland configure feedback. Some clients add their CSD/frame extent
	 * after every WM resize while libweston reports surface == geometry, so
	 * the extent cannot be inferred before the first resize. Remember the
	 * observed response delta and converge a mismatched configure at most
	 * three times. */
	int xwayland_frame_w, xwayland_frame_h;
	int xwayland_target_w, xwayland_target_h;
	int xwayland_sent_w, xwayland_sent_h;
	int xwayland_before_w, xwayland_before_h;
	unsigned int xwayland_corrections;
	bool xwayland_configure_pending;
	/* toplevel_state bitmask (QDWIN_TS_*). */
	uint32_t state;
	/* v24: which workspace this toplevel lives on. Set to the active
	 * workspace at map time; changed by move_toplevel_to_workspace.
	 * Only toplevels whose workspace == qdwin->active_workspace are
	 * composited (the rest are parked on workspace_hidden_layer). */
	uint32_t workspace;
	/* P05a: per-toplevel chrome border colour (ARGB, big-endian RGBA8888
	 * as set_border_color receives it). Defaults to 0 (= unset → fall
	 * back to qdshell's neutral chrome). qdshell paints the actual
	 * chrome surfaces via qdwin_shell_v1.attach_decoration; this field
	 * captures the per-toplevel value as received so (a) the SSD paint
	 * helper has a single source of truth for the silo-colour, (b) unit
	 * tests can introspect the per-toplevel state instead of a global,
	 * and (c) re-issued attach_decoration calls don't lose the colour
	 * the shell set earlier. */
	uint32_t border_rgba;
	int border_rgba_set;
	/* Restore geometry — captured on entry to maximise, consumed on
	 * un-maximise. Outer dims (chrome-inclusive). */
	int saved_outer_w, saved_outer_h;
	double saved_x, saved_y;
	/* v25: half-screen tiling (request_tile). `tiled` is a tile_edge
	 * value (0=none, 1=left, 2=right). The tile_saved_* fields hold the
	 * pre-tile geometry, captured on the first tile and consumed on
	 * restore — kept separate from the maximise saved_* above so a
	 * tile→maximise→restore round-trip doesn't lose the floating
	 * geometry. */
	uint32_t tiled;
	int tile_saved_outer_w, tile_saved_outer_h;
	double tile_saved_x, tile_saved_y;
	/* Chrome surfaces/views, indexed by qdwin_side. */
	struct qdwin_chrome chrome[QDWIN_SIDES];
	/* At most one popup anchored to this toplevel; a second
	 * show_popup dismisses the prior. NULL when no popup active. */
	struct qdwin_popup *popup;
	/* Last title we sent via toplevel_added or toplevel_title; we
	 * compare on every commit to detect xdg_toplevel.set_title and
	 * forward the change to qdshell. NULL until the first publish. */
	char *cached_title;
	/* Last app_id observed on this toplevel. v31 forwards post-map
	 * xdg_toplevel/WM_CLASS changes to the shell. NULL until first commit. */
	char *cached_app_id;
	struct wl_list link;           /* qdwin::toplevels */
};

struct qdwin_popup {
	struct wl_resource *resource;  /* qdwin_popup_v1 to the shell */
	struct weston_surface *surface;
	struct weston_view *view;
	struct wl_listener surface_destroy;
	struct qdwin_toplevel *parent;
	int x, y;                      /* parent-local placement */
	struct weston_pointer_grab grab;  /* §6.4 S5: dismiss-on-outside */
	int grab_active;
};

/* §6.5 S2: per-view forwarding. A stream pins one toplevel's content
 * view onto a pipewire output, so the view renders off the main RDP
 * desktop and onto an out-of-band PipeWire node that an external
 * qdistro-forward process serves to remote RDP clients.
 *
 * S2 scope: pin + emit approved with the pipewire node name.
 * qdistro-forward spawn + input injection are S3/S4. */
struct qdwin_view_stream {
	struct wl_resource *resource;     /* qdwin_view_stream_v1 */
	struct qdwin *qdwin;
	struct qdwin_toplevel *tl;
	uint32_t toplevel_handle;         /* stable after tl is destroyed */
	int listed;                       /* link is in qdwin::view_streams */
	int server_state_released;        /* server-owned state revoked exactly once */
	int torn_down_sent;               /* server-side terminal event sent */
	struct weston_output *pw_output;  /* pipewire output currently pinned */
	struct weston_output *prev_output; /* restore target on teardown */
	struct weston_coord_global prev_pos;
	int pinned;                       /* 1 while pw_output is in use */

	/* S3: external qdistro-forward proxy lifecycle */
	pid_t forward_pid;                /* 0 if not spawned */
	/* item 5 (codex impl-26): pidfd death-watch on the forward child. weston
	 * owns SIGCHLD via a signalfd and waitpid(-1)-reaps EVERY child, so qdwin
	 * (a shell plugin) cannot install a SIGCHLD handler nor reliably waitpid the
	 * forward to detect its death. Instead we hold a pidfd to the EXACT child we
	 * forked and watch it for readiness (process exit) on weston's wl_event_loop;
	 * pidfd readiness is independent of who reaps the zombie. On readiness we emit
	 * torn_down("forward exited") and run the normal stream teardown. DISARMED
	 * (source removed, fd closed, both fields reset) before any qdwin-initiated
	 * teardown so the callback can never fire against a freed stream. The fd is
	 * -1 and the source NULL whenever the watch is not armed (note: calloc zeroes
	 * the fd to 0 — a VALID fd — so it MUST be reset to -1 at creation). */
	int forward_pidfd;                /* -1 when not armed */
	struct wl_event_source *forward_pidfd_source;  /* NULL when not armed */
	uint32_t rdp_port;                /* 0 if not spawned */
	char access_token[33];            /* 32 hex chars + NUL */
	char rdp_password[17];            /* 16 hex chars + NUL */

	/* S5: input-injection channel claimed by qdistro-forward */
	int input_claimed;                /* 1 once claim() succeeded */
	int allow_input;                  /* server-side permission bit: 0 => read-only
	                                   * export, injected events are DROPPED at the
	                                   * inject_* boundary (subscribe allow_input) */
	struct wl_resource *input_handle; /* qdwin_stream_input_handle_v1, if claimed */

	/* S5c: virtual input device for this view, fed by whichever wl_client
	 * currently holds the stream_input_handle. Independent of the main
	 * RDP-desktop seat so remote input and local input can't race over
	 * focus; focus on this seat is pinned to tl->view. */
	struct weston_seat stream_seat;
	int seat_inited;

	/* IMPL-19: per-stream pointer grab that HARD-LOCKS the stream seat's
	 * pointer focus to tl->view, so injected remote input cannot leak to
	 * another view stacked at the same compositor-global coordinate. Without
	 * it, notify_motion_absolute re-picks focus geometrically and a local
	 * window overlapping the source view at the injected point would steal
	 * the press. Installed on claim, ended on handle release / seat teardown. */
	struct weston_pointer_grab confine_grab;
	int confine_grab_active;

	struct wl_list link;              /* qdwin::view_streams */
};

#define QDWIN_MAX_WORKSPACES 32
#define QDWIN_DEFAULT_WORKSPACES 4
/* Installed path(s) of the canonical locker *entrypoint script* — the
 * console-script that systemd's qdlocker.service ExecStart launches.
 * qdlocker is a Python setuptools console-script (qdlocker/pyproject.toml
 * [project.scripts]), NOT a native ELF, so /proc/<pid>/exe of the running
 * locker resolves to the *interpreter* (/usr/bin/python3.N), never to this
 * path. The earlier hardening compared allowed_locker_exe against the exe
 * path and therefore rejected the genuine locker in the default config (it
 * can never match a script path — VM ground truth 2026-06-02:
 * /proc/<pid>/exe -> /usr/bin/python3.13, argv[1] -> /usr/local/bin/qdlocker).
 * The correct identity for a console-script is the launcher's argv: the
 * kernel records the script path the shebang/systemd handed the interpreter
 * as argv[1] in /proc/<pid>/cmdline. So we default to an *entrypoint* policy:
 * exe must be a system interpreter AND argv[1] must realpath to one of these
 * canonical, root-owned entrypoint files.
 *
 * The path is profile-dependent and there is NO single canonical location:
 *   - dev / upstream pip --prefix=/usr/local (and the running daily VM):
 *     /usr/local/bin/qdlocker          (qdlocker.service ships this ExecStart)
 *   - bootstrap-installed (qdistro-bootstrap.sh rewrites ExecStart and
 *     pip --prefix=/usr or the /opt wrapper):  /usr/bin/qdlocker
 * Both files are root-owned. We therefore accept a colon-separated LIST of
 * trusted entrypoints by default so the real locker binds regardless of which
 * profile installed it, while still rejecting any same-uid impostor (whose
 * argv[1] points at neither). An explicit --qdwin-allowed-locker-entrypoint=
 * (also a colon-list) overrides the default. Overridable at build time. */
#ifndef QDWIN_DEFAULT_LOCKER_ENTRYPOINT
#define QDWIN_DEFAULT_LOCKER_ENTRYPOINT \
	"/usr/local/bin/qdlocker:/usr/bin/qdlocker"
#endif
/* v27: max stored workspace-name length (bytes). Bounds an unbounded
 * shell; over-long names are truncated rather than erroring. */
#define QDWIN_WORKSPACE_NAME_MAX 256

struct qdwin {
	struct weston_compositor *compositor;
	struct weston_desktop *desktop;
	struct wl_listener destroy_listener;

	/* Cached at first use; may be NULL if xwayland.so not loaded. Used
	 * by qdwin_send_toplevel_added to set the is_xwayland flag. */
	const struct weston_xwayland_surface_api *xwayland_surface_api;
	int xwayland_api_resolved;
	int cursor_default_warned;  /* one-shot for "no surface yet" log */

	/* qdwin_shell_v1 */
	struct wl_global *shell_global;
	struct wl_resource *shell_resource;
	struct wl_listener capture_auth_listener;
	bool capture_auth_registered;
	/* v33 retained-frame stale-serve (shell-capture sessions only): when
	 * the designated capture output cannot repaint, pending capture tasks
	 * are completed from the renderer-retained last frame and the shell
	 * is told via capture_served_stale. */
	struct wl_listener capture_task_filed_listener;
	struct wl_listener capture_retain_output_gone_listener;
	struct weston_output *capture_retain_output;
	struct wl_event_source *capture_stale_timer;
	bool capture_stale_timer_armed;
	uid_t allowed_uid;
	int locked;
	int shell_bound;
	pid_t shell_pid;
	uid_t shell_uid;

	/* qdwin_locker_v1 — peer locker (qdlocker). Same trust shape as
	 * the shell binding but on its own global so the locker is a
	 * separate process from the shell. See doc/locker.md. */
	struct wl_global *locker_global;
	struct wl_resource *locker_resource;
	uid_t allowed_locker_uid;
	/* Opt-out for the mandatory-identity default. When the admin
	 * configures neither an explicit allowed_locker_exe nor
	 * allowed_locker_label, qdwin defaults to the entrypoint policy
	 * (allowed_locker_entrypoint) so the locker identity is verified by
	 * default (fail-closed). Dev/test can consciously drop back to the
	 * weaker uid-only policy by setting --qdwin-allowed-locker-any /
	 * QDWIN_ALLOWED_LOCKER_ANY=1, which sets this flag and suppresses the
	 * default. It has no effect once an explicit exe/label/entrypoint is
	 * configured. */
	bool allowed_locker_any;
	/* Expected canonical entrypoint *script* of the locker. qdlocker is
	 * a Python console-script, so /proc/<pid>/exe is the interpreter and
	 * the real launcher path is argv[1] (see QDWIN_DEFAULT_LOCKER_ENTRY-
	 * POINT). When non-NULL the bind handler requires (a) the peer exe to
	 * be a system interpreter and (b) the peer's argv[1] to resolve
	 * (realpath) to this canonical, root-owned entrypoint file. This is
	 * the DEFAULT production policy (set to QDWIN_DEFAULT_LOCKER_ENTRY-
	 * POINT when no explicit exe/label is configured) and is also settable
	 * via --qdwin-allowed-locker-entrypoint= / env. NULL = entrypoint
	 * check disabled. */
	char *allowed_locker_entrypoint;
	/* Expected resolved /proc/<pid>/exe of the locker process. When
	 * non-NULL the bind handler rejects any peer whose exe does not
	 * match (in addition to the uid check). For a *native* locker only —
	 * qdlocker (Python) is matched via allowed_locker_entrypoint instead.
	 * NULL = exe check disabled. Set via --qdwin-allowed-locker-exe= / env. */
	char *allowed_locker_exe;
	/* Expected SELinux label of the locker process. When non-NULL the
	 * bind handler rejects any peer whose /proc/<pid>/attr/current
	 * label does not match. NULL = SELinux check disabled (deferred to
	 * service confinement). Set via --qdwin-allowed-locker-label= / env. */
	char *allowed_locker_label;
	pid_t locker_pid;
	uid_t locker_uid;
	/* /proc/<pid>/stat starttime of the bound locker process, sampled at
	 * bind time. Used by the bind handler to prove the *current* locker
	 * peer is still the same live process before refusing a takeover bind:
	 * if the pid is gone, or recycled into a different process (starttime
	 * changed), the old binding is provably dead and may be replaced. 0 =
	 * no live locker / starttime was unreadable at bind. */
	uint64_t locker_starttime;
	/* Which protocol owns the currently-attached lock_resource.
	 * 0 = shell (qdwin_lock_surface_v1), 1 = locker
	 * (qdwin_locker_surface_v1). Used so the destroy/dismiss path
	 * sends the right opcode on the right interface. */
	int lock_resource_is_locker;
	/* Set while we're mid-replace of an attached lock surface (the
	 * locker reattaches). Suppresses the spurious
	 * `locked_changed=0` that the destroy callback would otherwise
	 * fire when the old resource is torn down — the new surface is
	 * about to be attached and the locked state should stay at 1
	 * across the swap. Without this, observers see a 1→0→1 flap. */
	int lock_resource_reattach_in_progress;

	/* Layering (see spec/03).
	 * Position constants live in QDWIN_LAYER_POS_* so that init and
	 * lock/unlock hide/show helpers stay in sync. */
#define QDWIN_LAYER_POS_PANEL          0x70000000u
#define QDWIN_LAYER_POS_NOTIFICATION   0x78000000u
#define QDWIN_LAYER_POS_LAUNCHER       0x7C000000u
#define QDWIN_LAYER_POS_LSHELL_BG      0x00000003u
#define QDWIN_LAYER_POS_LSHELL_TOP     0x90000000u
	struct weston_layer background_layer;
	struct weston_layer held_layer;       /* HIDDEN until shell acks */
	struct weston_layer normal_layer;
	struct weston_layer minimized_layer;  /* unmapped; minimised views */
	struct weston_layer workspace_hidden_layer; /* v24: off-workspace views
						     * (unmapped, like minimized) */
	struct weston_layer panel_layer;      /* §6.6 S1: panels/bars above normal */
	struct weston_layer notification_layer; /* §6.6 S2: bubbles above panels */
	struct weston_layer launcher_layer;     /* §6.6 S3/S4: launcher + switcher */
	struct weston_layer lock_layer;         /* §6.6 S5: compositor lock overlay */
	struct weston_layer popup_layer;      /* above normal; chrome menus */
	/* zwlr_layer_shell_v1: BACKGROUND/BOTTOM/TOP/OVERLAY (indices 0..3
	 * matching the protocol enum). Positioned just outside qdwin's own
	 * panel ladder so external panels (waybar et al.) coexist with
	 * qdwin's native panels — see wet_shell_init for the chosen
	 * position values. */
	struct weston_layer layer_shell_layer[4];

	/* v24 workspaces. The shell owns the count + names (persisted in
	 * qdshell settings, pushed down via set_workspace_count); qdwin
	 * owns the active index and the per-toplevel assignment at runtime.
	 * Defaults to a single implicit workspace so pre-v24 shells and the
	 * pre-workspace code paths behave exactly as before. */
	uint32_t workspace_count;   /* >= 1, <= QDWIN_MAX_WORKSPACES */
	uint32_t active_workspace;  /* < workspace_count */
	/* v27: per-index custom names pushed by the shell via
	 * qdwin_shell_v1.set_workspace_name. NULL (or empty) means "use the
	 * positional default" — qdwin then advertises "1".."N" on the
	 * standard ext_workspace_handle_v1.name so every ext-workspace client
	 * sees the same names the shell would otherwise only overlay locally.
	 * Keyed by index (workspaces are positional). */
	char *workspace_names[QDWIN_MAX_WORKSPACES];

	/* §6.6 S5: single lock view. Legacy lock surfaces own a dedicated
	 * weston_view; qdlocker now uses its real Qt xdg_toplevel, whose
	 * existing view is borrowed and moved to lock_layer while locked. */
	struct weston_surface *lock_surface;
	struct weston_view *lock_view;
	int lock_view_is_toplevel;
	struct qdwin_toplevel *lock_toplevel;
	struct wl_resource *lock_resource;
	struct wl_listener lock_surface_destroy;
	struct wl_listener lock_surface_commit;

	/* §6.6 S1: active panels. Each entry reserves an exclusive zone on
	 * one output edge; maximise computes its target rect from
	 * output_rect minus the panels' zones. */
	struct wl_list panels;                /* struct qdwin_panel::link */

	/* §6.6 S2: active notification bubbles. Corner-anchored, no
	 * exclusive-zone impact. */
	struct wl_list notifications;         /* struct qdwin_notification::link */

	/* §6.6 S3/S4: launcher + switcher surfaces (same layer). */
	struct wl_list launchers;             /* struct qdwin_launcher::link */

	/* Switcher keyboard grab: Alt+Tab requires watching Alt-release
	 * directly because weston's modifier_binding only fires when the
	 * modifier was pressed alone (Tab pressed during Alt-hold
	 * disqualifies it). The grab's modifiers callback detects the
	 * Alt-released transition and fires switcher_commit. */
	struct weston_keyboard_grab switcher_grab;
	int switcher_grab_active;

	/* Titlebar drag (qdwin_shell_v1.begin_interactive_move).
	 * At most one drag at a time across all seats; second begin
	 * requests while active are silently ignored, and
	 * maximised/fullscreen toplevels refuse silently per XML. */
	struct weston_pointer_grab move_grab;
	int move_grab_active;
	uint32_t move_grab_handle;
	double move_grab_anchor_dx;  /* pointer.x - view.origin.x at start */
	double move_grab_anchor_dy;  /* pointer.y - view.origin.y at start */

	/* v19: shell-driven global hotkeys. Each entry holds a
	 * weston_compositor_add_key_binding registration; the handler
	 * sends qdwin_shell_v1.hotkey_pressed(id) on the shell resource.
	 * Bindings are torn down on shell unbind or unregister_hotkey. */
	struct wl_list hotkeys;             /* struct qdwin_hotkey::link */

	/* v25: live window-manager policy (set_wm_policy). Process-global,
	 * reset to compositor defaults when the shell binding is torn down.
	 * ffm_timer / ffm_pending_handle implement the focus-follows-mouse
	 * settle delay: pointer motion over a new toplevel (re)arms the
	 * timer for wm_policy.ffm_delay_ms; the timer focuses the toplevel
	 * only if the pointer is still over it when it fires. */
	struct qdwin_wm_policy wm_policy;
	struct wl_event_source *ffm_timer;
	uint32_t ffm_pending_handle;

	/* v26: 1 while the shell has forced the display off (set_display_power
	 * 0). Cleared (and outputs forced back on) when the shell unbinds, so a
	 * crashed shell never leaves the screen dark. */
	int display_forced_off;

	/* v28: live libinput pointer config (set_pointer_config). Process-global
	 * snapshot, applied to every pointer/touchpad device. Reset (libinput
	 * per-device defaults left in place) when the shell unbinds.
	 * libinput_backend is 1 only under the DRM/libinput backend, where the
	 * weston_seat embeds a struct udev_seat with a real devices_list; it
	 * gates the udev_seat reinterpret so we never read a bogus list on
	 * headless / RDP / nested seats. kb_repeat_* shadow the compositor's
	 * repeat rate/delay so they can be restored on unbind; default_kb_*
	 * capture the boot-time values. */
	struct qdwin_pointer_config pointer_config;
	int libinput_backend;
	int32_t default_kb_repeat_rate;
	int32_t default_kb_repeat_delay;
	int kb_repeat_overridden;

	/* Overlay keyboard grab (B3+B4): captures keys while a launcher
	 * (kind=0) or lock_surface is visible and forwards each press to
	 * the shell as overlay_key (qdwin_shell_v1 v17). The shell decodes
	 * keysym + utf8 and routes to its launcher / locker state machine.
	 * role: 0=launcher, 2=locker (1=switcher uses switcher_grab). */
	struct weston_keyboard_grab overlay_grab;
	int overlay_grab_active;
	uint32_t overlay_grab_role;

	/* Opaque black curtain covering the whole output. Ensures pixels
	 * are overwritten every frame — without this, the pixman renderer
	 * only repaints damaged regions, so stale chrome persists after
	 * a view shrinks or is unmapped (chrome shrink + minimise bugs).
	 *
	 * The curtain is hidden when a v18 shell calls attach_background;
	 * the shell-owned background surface takes its place on the
	 * background_layer. The curtain stays around as a fallback for
	 * pre-v18 shells and as a safety net if the shell's background
	 * is destroyed. */
	struct weston_curtain *background;
	/* Opaque black curtain kept at the BOTTOM of lock_layer whenever the
	 * compositor is locked. While locked, qdwin_hide_non_lock_layers()
	 * unsets every non-lock layer — including background_layer and its own
	 * black curtain — so lock_layer is the only thing composited. If the
	 * locker surface is transparent, absent, or crashed, the pixman/DRM
	 * renderer (which repaints only damaged regions) would leave stale
	 * desktop pixels on screen (the qdlocker software-GL transparent-buffer
	 * leak). This curtain paints the whole output black every frame so a
	 * broken locker can only ever yield black, never a desktop leak. The
	 * real lock view is stacked above it. Mirrors `background`. */
	struct weston_curtain *lock_curtain;
	/* v18 shell-owned desktop background. NULL until attach_background. */
	struct qdwin_background *shell_background;
	struct wl_listener output_created_listener;
	struct wl_listener output_resized_listener;
	struct wl_listener output_destroyed_listener;
	struct wl_listener seat_created_listener;

	/* One qdwin_seat_tracker per live weston_seat. Each carries a
	 * wl_listener on the seat's destroy_signal so we can fire a
	 * seat_removed event before the seat goes. */
	struct wl_list seat_trackers;     /* struct qdwin_seat_tracker::link */

	struct wl_list toplevels;          /* struct qdwin_toplevel::link */
	uint32_t next_handle;

	/* §6.5 S2: active per-view streams (struct qdwin_view_stream::link) */
	struct wl_list view_streams;
	uint32_t next_stream_port;        /* S3: allocator for qdistro-forward */

	/* §6.5 S5: per-stream input-injection global. Advertised to all
	 * clients; protected by access_token in claim(). */
	struct wl_global *stream_input_global;

	/* §6.7: xdg-activation-v1 (focus-stealing prevention + launcher
	 * tokens). One global advertised to all clients; tokens tracked
	 * on qdwin::activation_tokens. */
	struct wl_global *xdg_activation_global;
	struct wl_list activation_tokens;  /* struct qdwin_activation_token::link */
	uint32_t activation_token_counter; /* monotonic, for debug logging */
	/* spec/09: pending broker-gated activations. Each entry holds the
	 * stalled activate request until the shell calls
	 * activation_decision; on allow we run the original activate
	 * body, on deny we just free the token. */
	struct wl_list activation_pending;  /* qdwin_activation_pending::link */
	uint32_t activation_pending_next_handle;

	/* spec/10 v15 — wl_data_offer.receive per-MIME gating. The
	 * compositor wraps the active selection's data_source.send
	 * callback so receive calls land in our shim; pending entries
	 * hold the destination's pipe fd until the shell decides via
	 * qdwin_shell_v1.data_offer_receive_decision. */
	struct wl_list data_source_wraps;     /* qdwin_data_source_wrap::link */
	struct wl_list data_offer_pending;    /* qdwin_data_offer_pending::link */
	uint32_t data_offer_receive_next_handle;

	/* §6.7: ext-idle-notify-v1 + idle-inhibit-unstable-v1. One global
	 * per interface; notifications tracked for broadcast on weston's
	 * idle_signal / wake_signal. Inhibitors bump ec->idle_inhibit so
	 * weston's own idle timer defers. */
	struct wl_global *idle_notifier_global;
	struct wl_global *idle_inhibit_manager_global;
	struct wl_list idle_notifications;  /* struct qdwin_idle_notification::link */
	struct wl_list idle_inhibitors;     /* struct qdwin_idle_inhibitor::link */
	/* iso2/11 E2: backstop re-evaluation of every inhibitor's hold.
	 * Armed only while idle_inhibitors is non-empty. */
	struct wl_event_source *idle_inhibit_recheck_timer;
	/* Latched when the backstop can no longer be guaranteed. Terminal
	 * by design: surviving inhibitors go inert and new ones are
	 * refused, because a hold we cannot re-evaluate is a permanent
	 * client-controlled suppression of idle-lock and DPMS. */
	bool idle_inhibit_disabled;
	struct wl_listener idle_signal_listener;
	struct wl_listener wake_signal_listener;
	/* §6.7(a) follow-up: when weston's built-in idle timer is disabled
	 * (`idle-time=0` in weston.ini), qdwin falls back to an internal
	 * activity tracker: each notification arms its own wl_event_source for
	 * `timeout_ms` on create. The first ordinary inhibited-aware timer to
	 * fire mirrors weston's idle transition (state + idle_signal), so later
	 * real input produces wake_signal and resumes notifications. qdwin's
	 * default input grabs rearm not-yet-idle timers on key/pointer/axis
	 * activity while the compositor is already ACTIVE. The idle_signal
	 * listener is a no-op in this mode because the per-client timers are
	 * already armed relative to activity. input-idle notifications still only
	 * notify their own client and do not mutate global state. 1 when
	 * compositor->idle_time == 0, else 0. */
	int idle_internal_mode;

	/* §6.7: cursor-shape-v1 (accept + log; visual mapping deferred),
	 * wp_fractional_scale_v1 (spike: advertise, return preferred=120). */
	struct wl_global *cursor_shape_manager_global;
	struct wl_global *fractional_scale_manager_global;

	/* §6.7(b) cursor-shape theme cache. Loaded once at init via
	 * libXcursor; one XcursorImages* per protocol shape. Nominal
	 * cursor size (theme-dependent; we ask for 24). When a shape's
	 * image isn't present in the theme (theme packs vary), the
	 * entry stays NULL and set_shape logs a miss. */
	/* Indexed by shape enum; slot 0 unused (enum starts at 1).
	 * Highest value today is ALL_RESIZE = 36 → size 37. */
	XcursorImages *cursor_images[37];
	char *cursor_theme_name;
	int cursor_size;

	/* §6.8 cursor-sprite full theme: per-shape registered sprite
	 * surfaces (qdwin_shell_v1.set_cursor_sprite, v10). On set_shape
	 * the compositor preferred this cached entry over the solid
	 * fallback. The destroy listener clears the slot when the
	 * shell-owned wl_surface goes away. */
	struct qdwin_cursor_sprite {
		struct weston_surface *surface;
		int32_t hotspot_x, hotspot_y;
		struct wl_listener destroy_listener;
		/* plan3 M2: re-assert the Weston cursor-surface invariant on
		 * every commit (libweston/input.c:pointer_cursor_surface_committed
		 * clears pending+current input on each commit, not only at
		 * install time). Without this, a sprite helper that recommits
		 * with a non-empty pending.input would silently make the cursor
		 * pickable again. */
		struct wl_listener commit_listener;
		struct qdwin *qdwin;
		uint32_t shape;     /* back-ref so the listener can find us */
	} cursor_sprites[37];

	/* §6.7(c) fractional-scale tracking. One qdwin_fractional_scale
	 * per live wp_fractional_scale_v1 resource. */
	struct wl_list fractional_scales;  /* qdwin_fractional_scale::link */

	/* Bucket A / P1: text-input-v3. Open, app-facing IME text-input
	 * plane (zwp_text_input_manager_v3). One qdwin_text_input per live
	 * zwp_text_input_v3 resource; enter/leave is driven off the same
	 * keyboard focus_signal qdwin already tracks. Foundation only —
	 * inert until an input-method-v2 IME exists (no preedit/commit is
	 * ever sent). See todo/issues/qdwin/app-compat-protocol-gaps.md P1. */
	struct wl_global *text_input_manager_global;
	struct wl_list text_inputs;          /* qdwin_text_input::link */
	struct wl_list text_input_managers;  /* qdwin_text_input_manager::link */

	/* Bucket A / P1: input-method-unstable-v2 (zwp_input_method_manager_v2).
	 * The PRIVILEGED IME side that drives the open text-input-v3 plane:
	 * a session-trusted input method (fcitx5/ibus) binds the manager, grabs
	 * the seat keyboard, and pushes preedit/commit back into the focused
	 * text_input. Because it grants keystroke capture + arbitrary text
	 * injection it is identity-gated (allowed_ime_uid + secctx/sandbox deny
	 * via the global filter + single-IME-per-seat), NOT open like
	 * text-input-v3. See todo/issues/qdwin/app-compat-protocol-gaps.md P1. */
	struct wl_global *input_method_manager_global;
	struct wl_list input_methods;        /* qdwin_input_method::link */
	struct wl_list input_method_managers; /* qdwin_input_method_manager::link */
	uid_t allowed_ime_uid;               /* (uid_t)-1 => default to allowed_uid */
	char *allowed_ime_exe;               /* optional resolved-exe pin (NULL=skip) */
	char *allowed_ime_label;             /* optional SELinux-label pin (NULL=skip) */

	/* Bucket A / P1 companion: virtual-keyboard-unstable-v1
	 * (zwp_virtual_keyboard_manager_v1). The other half of a grabbing IME:
	 * once an input-method-v2 IME grabs the seat keyboard, it passes the
	 * keys it does NOT compose back to apps by injecting them through a
	 * virtual keyboard. Equally privileged (arbitrary keystroke injection
	 * into the focused app), so it is identity-gated EXACTLY like
	 * input-method-v2 — same allowed_ime_uid + secctx/sandbox deny via the
	 * global filter (shared gate: qdwin_ime_family_bind_allowed). The
	 * server injects via notify_key / notify_modifiers on the seat. See
	 * todo/open-followups.md + app-compat-protocol-gaps.md P1. */
	struct wl_global *virtual_keyboard_manager_global;
	struct wl_list virtual_keyboards;          /* qdwin_virtual_keyboard::link */
	struct wl_list virtual_keyboard_managers;  /* qdwin_virtual_keyboard_manager::link */
	/* Set to the injecting virtual keyboard's wl_client for the duration of a
	 * single notify_key/notify_modifiers injection, so an active IME keyboard
	 * grab can recognise its OWN virtual keyboard's keys and pass them through
	 * to the focused app instead of looping them back to the IME (the whole
	 * point of the companion — fcitx5 grabs, then re-injects non-composed keys
	 * which must reach the app). Mirrors sway's keyboard_get_im_grab
	 * same-client bypass. NULL except inside an injection. */
	struct wl_client *vk_injecting_client;

	/* §6.7 primary-selection-unstable-v1. One global for the
	 * device-manager; per-seat selection state lives on
	 * qdwin_primary_seat entries. */
	struct wl_global *primary_selection_manager_global;
	/* §6.10: wp_security_context_manager_v1. One global; one
	 * qdwin_secctx per registered listener; per-client tag for
	 * "this client came through listener X" lookup. */
	struct wl_global *security_context_manager_global;
	struct wl_list secctxs;          /* qdwin_secctx::link */
	struct wl_list secctx_clients;   /* qdwin_secctx_client::link */

	/* §6.8 S0/S1 (2026-04-25): qdwin_nested_v1 — nested-compositor
	 * native-toplevel passthrough. Manager-level global is peer-uid-
	 * filtered (like qdwin_shell_v1). S0 shipped the bind + advertise
	 * stub, S1 wires the publisher side: when QDWIN_NESTED_MODE=1, this
	 * shell is running INSIDE a nested compositor and acts as a
	 * publisher, binding the outer compositor's manager and forwarding
	 * each inner toplevel as a peer toplevel. See
	 * . */
	struct wl_global *nested_manager_global;
	struct wl_list primary_seats;  /* qdwin_primary_seat::link */
	struct wl_list nested_toplevels;  /* qdwin_nested_toplevel::link */

	/* Nested-mode publisher state. Active iff nested_mode is true. */
	bool nested_mode;
	struct qdwin_nested_client *nested_client;
	struct wl_event_source *nested_outer_event_source;
	int nested_next_pw_id;        /* monotonic for output naming */

	/* §6.8 S3b outer-side: which proxy currently has pointer focus
	 * (for QDNI motion/button/axis encoding). NULL when focus is on
	 * a normal toplevel or no view. Updated by the default_pointer_
	 * grab override; cleared on proxy destroy. */
	struct qdwin_toplevel *active_input_proxy;

	/* zwlr_layer_shell_v1 v5: external panels/notifications/lockscreens.
	 * Stub-stage implementation — accepts the protocol and completes
	 * the configure/ack handshake but does not yet lay out, anchor,
	 * or reserve exclusive zones. Goal at this stage: waybar (via
	 * gtk-layer-shell-0) binds the global, sends get_layer_surface +
	 * set_size + set_anchor + initial commit, and receives a configure
	 * back without protocol error. The next iteration ports
	 * wlroots scene/layer_shell_v1.c layout math + integrates with
	 * the existing weston_layer ladder. */
	struct wl_global *layer_shell_global;
	struct wl_list layer_surfaces;  /* qdwin_layer_surface::link */
	uint32_t layer_configure_serial_next;
	/* Optional admin-configured allowlist for who may BIND
	 * zwlr_layer_shell_v1. Default-unset => the historical broad/test
	 * posture (shell-client or allowed_uid) is unchanged. When set, the
	 * bind handler additionally verifies the peer's uid/exe/label and
	 * fails closed on any mismatch or unverifiable /proc read. Mirrors
	 * the locker-bind hardening. Set via
	 * --qdwin-allowed-layershell-uid= / -exe= / -label= and matching
	 * QDWIN_ALLOWED_LAYERSHELL_* env vars. */
	bool allowed_layershell_uid_set;       /* false => uid check skipped */
	uid_t allowed_layershell_uid;
	char *allowed_layershell_exe;          /* NULL => exe check skipped */
	char *allowed_layershell_label;        /* NULL => label check skipped */

	/* zxdg_decoration_manager_v1: always-server_side stub. qdshell
	 * draws chrome via qdwin_shell_v1, so toolkits should not try
	 * client-side decorations. See bind_qdwin_xdg_decoration_manager. */
	struct wl_global *xdg_decoration_manager_global;

	/* v24 workspaces: ext-workspace-v1 server. The manager global is
	 * advertised to all clients (a taskbar/dock protocol — no uid gate,
	 * unlike qdwin_shell_v1). One logical workspace list (count + active
	 * live in workspace_count / active_workspace above); each bound
	 * manager keeps its own group + per-workspace handle resources,
	 * tracked in ext_ws_managers. See
	 * todo/decisions/qdwin-workspaces-ext-protocol.md. */
	struct wl_global *ext_ws_global;
	struct wl_list ext_ws_managers;  /* qdwin_ext_ws_manager::link */

	/* Output (display) management: wlr-output-management-unstable-v1
	 * server. The manager global is advertised to all clients (an output-
	 * config protocol; no uid gate). Heads/modes are derived live from the
	 * compositor's output_list + head_list and re-synced on output
	 * hotplug. Each bound manager keeps its own head/mode resources
	 * tracked in om_managers. The current configuration serial bumps on
	 * every done; create_configuration validates the serial. See
	 * todo/decisions/qdwin-output-management.md. */
	struct wl_global *output_mgmt_global;
	struct wl_list om_managers;       /* qdwin_om_manager::link */
	uint32_t om_serial;               /* current configuration serial */
};

struct qdwin_seat_tracker;

/* §6.7 v2 event forwarders — defined later in this file. Forward-
 * declared here because bind_as_shell() replays them before the
 * definitions are in scope. */
static void qdwin_send_seat_created(struct qdwin *qdwin,
				    struct weston_seat *seat);
static void qdwin_send_output_created_evt(struct qdwin *qdwin,
					  struct weston_output *output);
static struct qdwin_seat_tracker *
qdwin_track_seat(struct qdwin *qdwin, struct weston_seat *seat);
/* spec/10 v14: replay seat_focus_changed for every tracked seat once
 * the shell binds. Defined near the seat_tracker struct. */
static void qdwin_replay_seat_focus_for_shell(struct qdwin *qdwin);
/* B3+B4 overlay grab — defined ~3700 lines below; referenced from
 * qdwin_launcher_resource_destroyed which appears before. */
static void qdwin_overlay_grab_start(struct qdwin *qdwin, uint32_t role);
static void qdwin_overlay_grab_end(struct qdwin *qdwin);
/* B6 default-cursor — defined ~6200 lines below; referenced from the
 * seat-tracker pointer-focus listener installer. */
static void qdwin_install_default_cursor_on_pointer(struct qdwin *qdwin,
						    struct weston_pointer *pointer);
static void qdwin_default_cursor_on_focus_changed(struct wl_listener *l,
						  void *data);
/* spec/10 v14 helpers — bodies live below the seat_tracker struct
 * because they walk seat_trackers via wl_list_for_each on the `link`
 * field which needs the struct to be complete. */
static void qdwin_seat_emit_focus_now(struct qdwin_seat_tracker *tr);
static int qdwin_seat_tracker_rebind_focus_listener(
	struct qdwin_seat_tracker *tr);
static void qdwin_seat_focus_recover_idle_cb(void *data);
static void qdwin_emit_seat_focus_changed(struct qdwin *qdwin,
					  struct weston_seat *seat,
					  uint32_t handle);
static struct qdwin_seat_tracker *
qdwin_seat_tracker_for_seat(struct qdwin *qdwin, struct weston_seat *seat);
/* Bucket A / P1: recompute text-input-v3 enter/leave for a seat after a
 * keyboard focus change. Defined with the rest of the text-input code far
 * below; forward-declared so the focus_signal listener can call it. */
static void qdwin_text_input_update_focus(struct qdwin *qdwin,
					  struct weston_seat *seat);
/* Drain live zwp_text_input_v3 objects + zwp_text_input_manager_v3 resources
 * at compositor teardown, before free(qdwin) — otherwise a resource outliving
 * qdwin would run its destroy handler against (or dispatch a request into) the
 * freed qdwin. Both neutralize each resource's user_data so a late callback
 * no-ops. */
static void qdwin_text_inputs_destroy_all(struct qdwin *qdwin);
static void qdwin_text_input_managers_destroy_all(struct qdwin *qdwin);
/* Bucket A / P1: input-method-v2 reconcile. After a text-input enable/focus
 * change, (re)activate or deactivate the seat's bound IME and push current
 * text-input state to it. Defined with the input-method code far below;
 * forward-declared so the text-input focus/commit paths can call it. A
 * text_input going away calls it after detaching, so the IME deactivates. */
struct qdwin_text_input;
static void qdwin_im_sync_seat(struct qdwin *qdwin, struct weston_seat *seat);
static void qdwin_im_text_input_gone(struct qdwin *qdwin,
				     struct qdwin_text_input *ti);
static void qdwin_input_methods_destroy_all(struct qdwin *qdwin);
static void qdwin_input_method_managers_destroy_all(struct qdwin *qdwin);
/* Bucket A / P1 companion: virtual-keyboard-v1 teardown drains. Defined with
 * the virtual-keyboard code far below; forward-declared so qdwin_destroy can
 * drain them (both neutralize each resource's user_data so a late callback
 * no-ops). */
static void qdwin_virtual_keyboards_destroy_all(struct qdwin *qdwin);
static void qdwin_virtual_keyboard_managers_destroy_all(struct qdwin *qdwin);
/* spec/10 v16 accessors — forward-declared so set_keyboard_focus_v2
 * (which lives near the rest of the request handlers, above the
 * seat_tracker struct definition) can read/write last_target_silo
 * without seeing the struct internals. Both no-op on NULL tr. */
static const char *qdwin_seat_tracker_silo(struct qdwin_seat_tracker *tr);
static void qdwin_seat_tracker_set_silo(struct qdwin_seat_tracker *tr,
					const char *silo);
/* §6.7(b) cursor-shape theme lifecycle, defined near the cursor-shape
 * block lower in the file. */
static void qdwin_cursor_theme_load(struct qdwin *qdwin);
static void qdwin_cursor_theme_destroy(struct qdwin *qdwin);
/* §6.7(c) fractional-scale re-broadcast on output changes. */
static void qdwin_fractional_scale_broadcast(struct qdwin *qdwin);
/* §6.6 S1 panel work-area helper — forward-declared so request_maximize
 * can call it before the panel block is defined. */
static void qdwin_output_work_area(struct qdwin *qdwin,
				   struct weston_output *out,
				   int *x, int *y, int *w, int *h);
/* Shared window-state cores, called from both the qdshell custom
 * protocol handlers and the standard xdg_toplevel.{set_maximized,
 * set_fullscreen,set_minimized} desktop_api callbacks. Without the
 * desktop_api wiring these requests are silently dropped by libweston
 * (libweston-desktop.c NULL-checks each callback before dispatch), so
 * client-drawn maximize/fullscreen/minimize buttons appear clickable
 * but do nothing. Forward-declared so the desktop_api struct above
 * the protocol handlers can reference them. */
struct qdwin_toplevel;
static void qdwin_toplevel_set_maximized(struct qdwin *qdwin,
					 struct qdwin_toplevel *tl,
					 bool maximized);
static void qdwin_toplevel_set_fullscreen(struct qdwin *qdwin,
					  struct qdwin_toplevel *tl,
					  bool fullscreen,
					  struct weston_output *output);
static void qdwin_toplevel_apply_fullscreen_geometry(struct qdwin *qdwin,
						     struct qdwin_toplevel *tl,
						     struct weston_output *output);
static void qdwin_toplevel_set_minimized(struct qdwin *qdwin,
					 struct qdwin_toplevel *tl);
static void qdwin_panels_on_output_change(struct qdwin *qdwin);
/* zwlr_layer_shell_v1 zone subtraction — defined after struct
 * qdwin_layer_surface; called from qdwin_output_work_area. */
static void qdwin_layer_shell_subtract_zones(struct qdwin *qdwin,
					     struct weston_output *out,
					     int *x, int *y, int *w, int *h);
struct qdwin_layer_surface;
static struct qdwin_layer_surface *
qdwin_layer_surface_at_pos(struct qdwin *qdwin, struct weston_coord_global pos);
static struct weston_view *
qdwin_layer_surface_view_at_pos(struct qdwin *qdwin,
				struct weston_coord_global pos);
static int
qdwin_layer_surface_blocks_toplevel_focus(struct qdwin_layer_surface *ls);
/* plan3 M4: forward-declared so qdwin_proxy_default_grab_button (which
 * sits above the qdwin_layer_surface struct definition) can call it. */
static void
qdwin_layer_surface_handle_on_demand_button(struct qdwin *qdwin,
					    struct weston_pointer *pointer);
/* §6.6 S3/S4 keybinding handlers — forward-declared because they are
 * registered in wet_shell_init which precedes their definitions. */
static void qdwin_on_launcher_key(struct weston_keyboard *kb,
				  const struct timespec *t,
				  uint32_t key, void *data);
static void qdwin_on_switcher_key(struct weston_keyboard *kb,
				  const struct timespec *t,
				  uint32_t key, void *data);
static void qdwin_on_switcher_back_key(struct weston_keyboard *kb,
				       const struct timespec *t,
				       uint32_t key, void *data);
static void qdwin_on_alt_released(struct weston_keyboard *kb,
				  enum weston_keyboard_modifier mod,
				  void *data);
/* §6.6 S5 full — Ctrl+Alt+L manual lock. */
static void qdwin_on_lock_key(struct weston_keyboard *kb,
			      const struct timespec *t,
			      uint32_t key, void *data);
static void qdwin_idle_note_activity(struct qdwin *qdwin);

/* B6: retry cursor install when pointer isn't ready at seat creation */
static int qdwin_cursor_retry_timer_cb(void *data);
static int qdwin_cursor_retry_install(struct qdwin *qdwin);

/* ------------------------------------------------------------------
 * Toplevel bookkeeping helpers.
 * ------------------------------------------------------------------ */

static struct qdwin_toplevel *
qdwin_toplevel_from_handle(struct qdwin *qdwin, uint32_t handle)
{
	struct qdwin_toplevel *tl;
	wl_list_for_each(tl, &qdwin->toplevels, link)
		if (tl->handle == handle)
			return tl;
	return NULL;
}

static uid_t
qdwin_client_uid(struct weston_desktop_surface *dsurf)
{
	struct weston_desktop_client *dclient =
		weston_desktop_surface_get_client(dsurf);
	if (!dclient)
		return (uid_t)-1;
	struct wl_client *client =
		weston_desktop_client_get_client(dclient);
	/* libweston-desktop returns dclient->client == NULL for surfaces
	 * spawned by xwayland.so: the X11 toplevel has no wl_client of its
	 * own, only the parent Xwayland server does. wl_client_get_credentials
	 * dereferences its argument, so without this guard the first X11
	 * client connect SIGSEGVs weston (verified via coredumpctl bt). For
	 * XWayland surfaces we have no per-window peer-uid signal, so we
	 * return the explicit "unknown" sentinel (uid_t)-1 rather than
	 * getuid(): attributing an X11 window to the compositor's own
	 * (admin) uid would let downstream (qdshell/broker) misclassify a
	 * legacy X11 client as trusted admin-local. The shell distinguishes
	 * these via the is_xwayland flag on toplevel_added and MUST treat
	 * uid==0xFFFFFFFF as untrusted/unknown, not as admin. Over the wire
	 * the uid is a uint32_t so (uid_t)-1 serialises to 0xFFFFFFFF. */
	if (!client)
		return (uid_t)-1;
	pid_t pid; uid_t uid; gid_t gid;
	wl_client_get_credentials(client, &pid, &uid, &gid);
	(void)pid; (void)gid;
	return uid;
}

static int
qdwin_desktop_surface_peer(struct weston_desktop_surface *dsurf,
			   pid_t *pid_out, uid_t *uid_out)
{
	struct weston_desktop_client *dclient =
		weston_desktop_surface_get_client(dsurf);
	if (!dclient)
		return 0;
	struct wl_client *client =
		weston_desktop_client_get_client(dclient);
	if (!client)
		return 0;
	pid_t pid; uid_t uid; gid_t gid;
	wl_client_get_credentials(client, &pid, &uid, &gid);
	(void)gid;
	if (pid_out)
		*pid_out = pid;
	if (uid_out)
		*uid_out = uid;
	return 1;
}

/* §6.8 nested-proxy identity hardening: read the peer uid of a connected
 * AF_UNIX socket fd via SO_PEERCRED. Returns 1 and fills *uid_out on
 * success, 0 on failure (the credential is unreadable, e.g. not a unix
 * socket). Callers MUST fail closed when this returns 0 — an unverifiable
 * peer must never be treated as a trusted same-uid owner. */
static int
qdwin_fd_peer_uid(int fd, uid_t *uid_out)
{
	if (fd < 0)
		return 0;
	struct ucred cred;
	socklen_t len = sizeof cred;
	if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &cred, &len) != 0 ||
	    len != sizeof cred)
		return 0;
	if (uid_out)
		*uid_out = cred.uid;
	return 1;
}

/* Match a toplevel to the locker process by (pid, uid).  We cannot use
 * wl_client pointer comparison because qdlocker's Qt xdg_toplevel and its
 * pywayland control connection are separate wl_clients sharing a pid. */
static int
qdwin_toplevel_is_locker_ui(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
	if (!qdwin || !tl || !tl->desktop_surface || tl->is_nested_proxy)
		return 0;
	if (!qdwin->locker_resource || qdwin->locker_pid <= 0)
		return 0;
	pid_t pid = 0;
	uid_t uid = (uid_t)-1;
	if (!qdwin_desktop_surface_peer(tl->desktop_surface, &pid, &uid))
		return 0;
	return pid == qdwin->locker_pid && uid == qdwin->locker_uid;
}

static void
qdwin_maybe_promote_lock_toplevel(struct qdwin *qdwin,
				  struct qdwin_toplevel *tl,
				  const char *cause)
{
	if (!qdwin_toplevel_is_locker_ui(qdwin, tl))
		return;

	if (qdwin->lock_toplevel && qdwin->lock_toplevel != tl)
		qdwin_demote_lock_toplevel(qdwin, "replace-lock-toplevel");

	qdwin->lock_toplevel = tl;
	qdwin->lock_surface =
		weston_desktop_surface_get_surface(tl->desktop_surface);
	qdwin->lock_view = tl->view;
	qdwin->lock_view_is_toplevel = 1;

	qdwin_toplevel_move_to_layer(tl, &qdwin->lock_layer);
	qdwin_lock_curtain_to_bottom(qdwin);
	qdwin_toplevel_set_fullscreen(qdwin, tl, true, NULL);
	weston_log("qdwin: promoted locker toplevel handle=%u to lock_layer via %s\n",
		   tl->handle, cause ? cause : "unknown");
	weston_compositor_schedule_repaint(qdwin->compositor);
}

static void
qdwin_demote_lock_toplevel(struct qdwin *qdwin, const char *cause)
{
	struct qdwin_toplevel *tl = qdwin ? qdwin->lock_toplevel : NULL;
	if (!tl)
		return;

	/* Hide the lock UI immediately on unlock. qdlocker also hides the Qt
	 * window, but that request travels over a different Wayland connection
	 * and may arrive after set_locked(0). Keeping the view on held_layer
	 * avoids a visible post-unlock flash above the restored desktop. */
	qdwin_toplevel_move_to_layer(tl, &qdwin->held_layer);
	if (qdwin->lock_view == tl->view) {
		qdwin->lock_view = NULL;
		qdwin->lock_surface = NULL;
		qdwin->lock_view_is_toplevel = 0;
	}
	qdwin->lock_toplevel = NULL;
	weston_log("qdwin: demoted locker toplevel handle=%u via %s\n",
		   tl->handle, cause ? cause : "unknown");
	weston_compositor_schedule_repaint(qdwin->compositor);
}

/* §6.10 follow-up: forward a toplevel's secctx tag to the shell as
 * `toplevel_security_context(handle, sandbox_engine, app_id, instance_id)`,
 * but only for shells bound at qdwin_shell_v1@v13 or later. Toplevels
 * whose client has no tag (unsandboxed) emit nothing — absence == not
 * sandboxed, which is the v13 contract.
 *
 * Nested proxies (is_nested_proxy=1) have no backing app wl_client — the
 * proxy synthesises its metadata from the outer's
 * qdwin_nested_manager_v1.advertise_toplevel call and desktop_surface is
 * NULL — so a client-based lookup on the proxy can't resolve the app's tag.
 * J12 Fix B: rather than forward the *inner* app's tag (which would need an
 * XML bump on qdwin-nested-v1), we use the *advertising* container's own
 * secctx tag, captured onto the proxy at advertise_toplevel time
 * (qdwin_nested_proxy_create). For the per-uid tier-2 model the advertiser
 * IS the sandboxed silo, so its tag is the correct silo/app identity. */
static void
qdwin_send_toplevel_security_context(struct qdwin *qdwin,
				     struct qdwin_toplevel *tl)
{
	if (!qdwin->shell_bound || !qdwin->shell_resource)
		return;
	if (wl_resource_get_version(qdwin->shell_resource) < 13)
		return;
	if (tl->is_nested_proxy) {
		/* J12 Fix B: a nested proxy has no backing wl_client
		 * (desktop_surface == NULL), so its secctx was captured from the
		 * *advertising* container connection at advertise_toplevel time
		 * (see qdwin_nested_proxy_create). Emit that tag — the silo/app
		 * identity the launcher badge + placeholder resolution key on.
		 * Untagged advertiser => proxy_secctx_set is false => emit
		 * nothing, preserving the absence==not-sandboxed v13 contract.
		 * The v22 peer-identity sidecar is intentionally NOT sent for
		 * proxies: their peer is the container compositor, not the app
		 * process, so it would mislead broker re-verification. */
		if (!tl->proxy_secctx_set)
			return;
		/* Parity with the non-proxy path, which forwards the accessor
		 * results (guaranteed non-NULL ""). The stash is strdup'd, so a
		 * field could be NULL only on OOM; normalise to "" so the wire
		 * arg is never NULL. */
		qdwin_shell_v1_send_toplevel_security_context(
			qdwin->shell_resource, tl->handle,
			tl->proxy_secctx_engine   ? tl->proxy_secctx_engine   : "",
			tl->proxy_secctx_app_id   ? tl->proxy_secctx_app_id   : "",
			tl->proxy_secctx_instance ? tl->proxy_secctx_instance : "");
		weston_log("qdwin: toplevel_security_context (nested proxy) "
			   "handle=%u engine=%s app_id=%s instance=%s\n",
			   tl->handle,
			   tl->proxy_secctx_engine ? tl->proxy_secctx_engine : "",
			   tl->proxy_secctx_app_id ? tl->proxy_secctx_app_id : "",
			   tl->proxy_secctx_instance ? tl->proxy_secctx_instance : "");
		return;
	}
	struct weston_desktop_client *dclient =
		weston_desktop_surface_get_client(tl->desktop_surface);
	struct wl_client *client =
		weston_desktop_client_get_client(dclient);
	struct qdwin_secctx_client *sc = qdwin_secctx_client_lookup(qdwin, client);
	if (!sc)
		return;
	const char *engine   = qdwin_secctx_client_engine(sc);
	const char *app_id   = qdwin_secctx_client_app_id(sc);
	const char *instance = qdwin_secctx_client_instance_id(sc);
	qdwin_shell_v1_send_toplevel_security_context(
		qdwin->shell_resource, tl->handle, engine, app_id, instance);
	weston_log("qdwin: toplevel_security_context handle=%u engine=%s "
		   "app_id=%s instance=%s\n",
		   tl->handle, engine, app_id, instance);
	/* v22 sidecar: stable process identity for broker-side re-verification
	 * (todo/decisions/secctx-identity-contract.md, Option B). Additive —
	 * skipped on shells bound below v22. */
	if (wl_resource_get_version(qdwin->shell_resource) >= 22) {
		uint32_t peer_pid = qdwin_secctx_client_peer_pid(sc);
		uint64_t starttime = qdwin_secctx_client_peer_starttime(sc);
		uint32_t peer_uid = qdwin_secctx_client_peer_uid(sc);
		const char *exe = qdwin_secctx_client_peer_exe(sc);
		const char *label = qdwin_secctx_client_peer_selinux_label(sc);
		uint32_t st_lo = (uint32_t)(starttime & 0xffffffffu);
		uint32_t st_hi = (uint32_t)((starttime >> 32) & 0xffffffffu);
		qdwin_shell_v1_send_toplevel_peer_identity(
			qdwin->shell_resource, tl->handle,
			peer_pid, st_lo, st_hi, peer_uid, exe, label);
		weston_log("qdwin: toplevel_peer_identity handle=%u pid=%u "
			   "starttime=%llu uid=%u exe=%s label=%s\n",
			   tl->handle, peer_pid,
			   (unsigned long long)starttime,
			   peer_uid, exe, label);
	}
}

/* The wp_security_context sandbox engine string for a toplevel's backing client,
 * or NULL if the client is untagged (unsandboxed) or this is a nested proxy
 * (whose own wl_client is the shell, not the inner app — see
 * qdwin_send_toplevel_security_context). Used by compositor policy gates such as
 * the source-mediated-close guard below. */
static const char *
qdwin_toplevel_secctx_engine(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
	if (!tl || tl->is_nested_proxy || !tl->desktop_surface)
		return NULL;
	struct weston_desktop_client *dclient =
		weston_desktop_surface_get_client(tl->desktop_surface);
	struct wl_client *client =
		weston_desktop_client_get_client(dclient);
	struct qdwin_secctx_client *sc = qdwin_secctx_client_lookup(qdwin, client);
	return sc ? qdwin_secctx_client_engine(sc) : NULL;
}

static int
qdwin_toplevel_is_xwayland(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
	if (tl->is_nested_proxy || !tl->desktop_surface)
		return 0;
	if (!qdwin->xwayland_api_resolved) {
		qdwin->xwayland_surface_api =
			weston_xwayland_surface_get_api(qdwin->compositor);
		qdwin->xwayland_api_resolved = 1;
	}
	if (!qdwin->xwayland_surface_api)
		return 0;
	struct weston_surface *surface =
		weston_desktop_surface_get_surface(tl->desktop_surface);
	if (!surface)
		return 0;
	return qdwin->xwayland_surface_api->is_xwayland_surface(surface) ? 1 : 0;
}

/* libweston-desktop may not have copied WM_CLASS into app_id yet when its
 * surface_added callback fires. XWayland already owns the property at that
 * point, so use its surface API as the authoritative fallback. v31 also
 * forwards a later value when the property was unavailable at first map. */
static const char *
qdwin_toplevel_effective_app_id(struct qdwin *qdwin,
				 struct qdwin_toplevel *tl)
{
	const char *app_id;
	struct weston_surface *surface;

	if (!tl || tl->is_nested_proxy || !tl->desktop_surface)
		return NULL;
	app_id = weston_desktop_surface_get_app_id(tl->desktop_surface);
	if (app_id && *app_id)
		return app_id;
	if (!qdwin_toplevel_is_xwayland(qdwin, tl) ||
	    !qdwin->xwayland_surface_api ||
	    !qdwin->xwayland_surface_api->get_xwayland_window_name)
		return app_id;
	surface = weston_desktop_surface_get_surface(tl->desktop_surface);
	if (!surface)
		return app_id;
	const char *wm_class = qdwin->xwayland_surface_api->
		get_xwayland_window_name(surface, WM_CLASS);
	return wm_class && *wm_class ? wm_class : app_id;
}

static void
qdwin_emit_toplevel_app_id(struct qdwin *qdwin,
			    struct qdwin_toplevel *tl,
			    const char *app_id)
{
	if (!qdwin || !tl || !qdwin->shell_bound || !qdwin->shell_resource)
		return;
	if (wl_resource_get_version(qdwin->shell_resource) < 31)
		return;
	qdwin_shell_v1_send_toplevel_app_id(qdwin->shell_resource,
					   tl->handle, app_id ? app_id : "");
}

static void
qdwin_emit_nested_proxy_remote_identity(struct qdwin *qdwin,
					struct qdwin_toplevel *tl)
{
	if (!qdwin || !tl || !tl->is_nested_proxy ||
	    !tl->proxy_remote_source_machine ||
	    !qdwin->shell_bound || !qdwin->shell_resource ||
	    wl_resource_get_version(qdwin->shell_resource) < 34)
		return;
	qdwin_shell_v1_send_nested_proxy_remote_identity(
		qdwin->shell_resource, tl->handle,
		tl->proxy_remote_source_machine,
		tl->proxy_remote_trust_domain_id,
		tl->proxy_remote_stream_id,
		(uint32_t)(tl->proxy_remote_generation >> 32),
		(uint32_t)tl->proxy_remote_generation);
}

static void
qdwin_send_toplevel_added(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
	const char *app_id;
	const char *title;
	uid_t uid;
	if (tl->is_nested_proxy) {
		/* Nested-proxy toplevels synthesise their metadata from the
		 * advertise_toplevel call — desktop_surface is NULL. */
		app_id = tl->proxy_app_id ? tl->proxy_app_id : "";
		title  = tl->proxy_title  ? tl->proxy_title  : "";
		uid    = tl->proxy_origin_uid;
	} else {
		app_id = qdwin_toplevel_effective_app_id(qdwin, tl);
		title  = weston_desktop_surface_get_title(tl->desktop_surface);
		uid    = qdwin_client_uid(tl->desktop_surface);
	}
	/* Seed the caches unconditionally — even when no shell is bound,
	 * the surface_committed diff path uses these to suppress spurious
	 * "title/app_id changed" log spam on every initial commit. */
	free(tl->cached_title);
	tl->cached_title = qdwin_xstrdup_or_null(title ? title : "");
	free(tl->cached_app_id);
	tl->cached_app_id = qdwin_xstrdup_or_null(app_id ? app_id : "");
	if (!qdwin->shell_bound || !qdwin->shell_resource)
		return;
	qdwin_shell_v1_send_toplevel_added(qdwin->shell_resource,
					   tl->handle,
					   (uint32_t)uid,
					   app_id ? app_id : "",
					   title  ? title  : "",
					   (uint32_t)qdwin_toplevel_is_xwayland(qdwin, tl));
	qdwin_send_toplevel_security_context(qdwin, tl);
	qdwin_emit_nested_proxy_remote_identity(qdwin, tl);
	/* v24 sidecar: tell the shell which workspace this window opened on.
	 * Mirrors the secctx event ordering — immediately after
	 * toplevel_added so the shell has the row before it fills fields. */
	qdwin_emit_toplevel_workspace(qdwin, tl);
}

/* v24: emit qdwin_shell_v1.toplevel_workspace (per-window occupancy
 * sidecar). Gated on a bound v24+ shell — pre-v24 shells classify every
 * window as workspace 0. */
static void
qdwin_emit_toplevel_workspace(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
	if (!qdwin->shell_bound || !qdwin->shell_resource)
		return;
	if (wl_resource_get_version(qdwin->shell_resource) < 24)
		return;
	qdwin_shell_v1_send_toplevel_workspace(qdwin->shell_resource,
					       tl->handle, tl->workspace);
}

static void
qdwin_send_toplevel_removed(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
	if (!qdwin->shell_bound || !qdwin->shell_resource)
		return;
	qdwin_shell_v1_send_toplevel_removed(qdwin->shell_resource, tl->handle);
}

static void
qdwin_send_toplevel_state(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
	if (!qdwin->shell_bound || !qdwin->shell_resource)
		return;
	qdwin_shell_v1_send_toplevel_state(qdwin->shell_resource,
					   tl->handle, tl->state);
}

/* Move the content view + every attached chrome view to the same layer.
 * Used by maximise (no layer change today, but kept for future fullscreen
 * lifting), minimise → minimized_layer, and unminimise → normal_layer. */
static void
qdwin_toplevel_move_to_layer(struct qdwin_toplevel *tl,
			     struct weston_layer *layer)
{
	if (tl->nested_proxy_pending_decision &&
	    layer == &tl->qdwin->normal_layer)
		layer = &tl->qdwin->held_layer;
	weston_view_move_to_layer(tl->view, &layer->view_list);
	for (int s = 0; s < QDWIN_SIDES; s++) {
		if (tl->chrome[s].view)
			weston_view_move_to_layer(tl->chrome[s].view,
						  &layer->view_list);
	}
}

/* First enabled output, or NULL. Maximise needs the work-area. Single
 * output today; multi-output policy lands with §6.5. */
static struct weston_output *
qdwin_primary_output(struct qdwin *qdwin)
{
	struct weston_output *out;
	wl_list_for_each(out, &qdwin->compositor->output_list, link) {
		if (!out->destroying)
			return out;
	}
	return NULL;
}

/* A1-min TEST-ONLY placement hook (codex impl-5). When env
 * QDWIN_TEST_PLACE_APPID matches a toplevel's app-id, place it at the explicit
 * GLOBAL coordinate (QDWIN_TEST_PLACE_X, QDWIN_TEST_PLACE_Y), bypassing qdwin's
 * one-output placement policy so the view's rect can overlap two outputs and
 * libweston composites it onto both (output_mask). This is a deliberately
 * narrow probe for the two-output straddle render gate — it sets ONLY the
 * initial position for one targeted app, and does NOT alter maximize, tiling,
 * fullscreen, chrome, snapping, or output-change reflow. NOT a user feature;
 * delete/replace when real multi-output WM policy (§6.5) lands. Returns 1 if it
 * placed the view (caller skips normal placement), else 0. */
static int
qdwin_test_place(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
#ifdef QDWIN_ENABLE_TEST_PLACE
	/* Compiled in ONLY for dedicated test builds (meson -Denable_test_place=true,
	 * codex impl-6 M7) — production qdwin does not contain this code path, so it
	 * cannot be driven by ambient env there. */
	(void)qdwin;
	const char *want = getenv("QDWIN_TEST_PLACE_APPID");
	if (!want || !*want || !tl || !tl->view || !tl->cached_app_id)
		return 0;
	if (strcmp(want, tl->cached_app_id) != 0)
		return 0;
	const char *xs = getenv("QDWIN_TEST_PLACE_X");
	const char *ys = getenv("QDWIN_TEST_PLACE_Y");
	if (!xs || !ys)
		return 0;
	/* strtol with validation (not atoi): reject malformed/overflowing config
	 * and bound to a sane test range instead of silently placing at garbage. */
	char *ex = NULL, *ey = NULL;
	long lx = strtol(xs, &ex, 10);
	long ly = strtol(ys, &ey, 10);
	if (ex == xs || ey == ys || *ex || *ey ||
	    lx < -32768 || lx > 32767 || ly < -32768 || ly > 32767) {
		weston_log("qdwin: TEST placement IGNORED — bad QDWIN_TEST_PLACE_X/Y "
			   "(\"%s\",\"%s\")\n", xs, ys);
		return 0;
	}
	struct weston_coord_global p = {
		.c = weston_coord((int)lx, (int)ly),
	};
	weston_view_set_position(tl->view, p);
	weston_view_update_transform(tl->view);
	weston_log("qdwin: TEST placement app_id=%s at (%ld,%ld) "
		   "[QDWIN_TEST_PLACE_* — straddle probe, NOT WM policy]\n",
		   tl->cached_app_id, lx, ly);
	return 1;
#else
	(void)qdwin;
	(void)tl;
	return 0;     /* production build: no test hook, dead branch at call site */
#endif
}

/* ------------------------------------------------------------------
 * weston_desktop_api callbacks.
 * ------------------------------------------------------------------ */

static void
qdwin_surface_added(struct weston_desktop_surface *dsurf, void *data)
{
	struct qdwin *qdwin = data;
	struct qdwin_toplevel *tl;
	struct weston_view *view;

	tl = calloc(1, sizeof *tl);
	if (!tl)
		return;

	view = weston_desktop_surface_create_view(dsurf);
	if (!view) {
		free(tl);
		return;
	}

	tl->qdwin = qdwin;
	tl->desktop_surface = dsurf;
	tl->view = view;
	tl->handle = ++qdwin->next_handle;
	tl->mapped = 0;
	tl->decorated = 0;
	/* v24: new windows open on the active workspace. */
	tl->workspace = qdwin->active_workspace;
	for (int s = 0; s < QDWIN_SIDES; s++) {
		tl->chrome[s].side = s;
		tl->chrome[s].tl = tl;
		wl_list_init(&tl->chrome[s].surface_destroy.link);
		wl_list_init(&tl->chrome[s].surface_commit.link);
	}
	wl_list_insert(&qdwin->toplevels, &tl->link);
	weston_desktop_surface_set_user_data(dsurf, tl);

	if (qdwin->nested_mode) {
		/* §6.8 S1: nested mode skips the held-layer + toplevel_added
		 * fanout. Inner toplevels go straight to the normal layer so
		 * they paint into the pipewire output we pin them onto.
		 * No outer chrome — outer qdwin's qdwin_shell_v1 will draw
		 * chrome around the proxy surface (see S2). */
		weston_view_move_to_layer(view, &qdwin->normal_layer.view_list);
		weston_log("qdwin/nested: inner toplevel handle=%u uid=%u "
			   "app_id=%s\n",
			   tl->handle, (unsigned)qdwin_client_uid(dsurf),
			   weston_desktop_surface_get_app_id(dsurf) ?: "(null)");
		/* Defer publish until first commit (so app_id/title are set
		 * by then — most clients set both before the initial commit). */
		return;
	}

	/* Land on the held layer until S3 ships: no pixels visible. */
	weston_view_move_to_layer(view, &qdwin->held_layer.view_list);

	{
		/* Log the peer pid alongside uid/app_id. The tier-2 "no phantom
		 * host-output window" regression assertion (F6#2) keys on this:
		 * once the inner nested weston runs with a pipewire-only backend
		 * (no wayland-backend host-output window), NO regular
		 * toplevel_added line should ever carry the nested-publisher's
		 * pid. With a wayland-backend that pid WOULD appear here. */
		pid_t peer_pid = 0; uid_t peer_uid = (uid_t)-1;
		qdwin_desktop_surface_peer(dsurf, &peer_pid, &peer_uid);
		(void)peer_uid;
		weston_log("qdwin: toplevel_added handle=%u uid=%u pid=%d "
			   "app_id=%s\n",
			   tl->handle, (unsigned)qdwin_client_uid(dsurf),
			   (int)peer_pid,
			   weston_desktop_surface_get_app_id(dsurf) ?: "(null)");
	}

	qdwin_send_toplevel_added(qdwin, tl);

	/* qdshell consumes qdwin_shell_v1 to track/focus toplevels, but it
	 * does not attach qdwin SSD chrome for ordinary local applications.
	 * Do not require a shell-side decoration/colour request before a
	 * normal desktop surface can become visible. Nested proxy toplevels
	 * still use their explicit allow/deny path. */
	qdwin_toplevel_release_holding(tl, "default_toplevel_policy");
	qdwin_maybe_promote_lock_toplevel(qdwin, tl, "surface_added");
}

static void
qdwin_surface_removed(struct weston_desktop_surface *dsurf, void *data)
{
	struct qdwin *qdwin = data;
	struct qdwin_toplevel *tl =
		weston_desktop_surface_get_user_data(dsurf);
	if (!tl)
		return;

	if (qdwin->lock_toplevel == tl) {
		qdwin->lock_toplevel = NULL;
		if (qdwin->lock_view == tl->view) {
			qdwin->lock_view = NULL;
			qdwin->lock_surface = NULL;
			qdwin->lock_view_is_toplevel = 0;
		}
	}

	if (qdwin->nested_mode) {
		qdwin_nested_unpublish_toplevel(tl);
		weston_log("qdwin/nested: inner toplevel_removed handle=%u\n",
			   tl->handle);
	} else {
		qdwin_send_toplevel_removed(qdwin, tl);
		weston_log("qdwin: toplevel_removed handle=%u\n", tl->handle);
	}

	qdwin_toplevel_release_dependents(qdwin, tl);

	for (int s = 0; s < QDWIN_SIDES; s++)
		qdwin_chrome_detach(&tl->chrome[s]);

	/* Focus transfer to a surviving sibling on toplevel destroy is
	 * NOT done here. By the time qdwin_surface_removed runs,
	 * libweston has already cleared kbd->focus to NULL (the
	 * weston_surface destroy_signal fires before libweston-desktop's
	 * surface_removed callback). Instead, the recovery is done from
	 * the focus_signal listener in qdwin_seat_emit_focus_now —
	 * whenever focus drops to UINT32_MAX with surviving siblings
	 * present, it re-targets the next mapped toplevel. */

	weston_desktop_surface_unlink_view(tl->view);
	weston_view_destroy(tl->view);
	wl_list_remove(&tl->link);
	weston_desktop_surface_set_user_data(dsurf, NULL);
	free(tl->cached_title);
	free(tl->cached_app_id);
	free(tl);
}

static void
qdwin_surface_committed(struct weston_desktop_surface *dsurf,
			struct weston_coord_surface buf_offset, void *data)
{
	struct qdwin *qdwin = data;
	struct qdwin_toplevel *tl =
		weston_desktop_surface_get_user_data(dsurf);
	struct weston_surface *surface;

	(void)buf_offset; (void)qdwin;
	if (!tl)
		return;

	surface = weston_desktop_surface_get_surface(dsurf);
	if (surface->width == 0)
		return;

	if (qdwin->nested_mode) {
		if (!weston_surface_is_mapped(surface)) {
			weston_surface_map(surface);
			tl->mapped = 1;
			weston_log("qdwin/nested: mapped handle=%u %dx%d\n",
				   tl->handle, surface->width, surface->height);
		}
		/* Publish on first commit so app_id/title are usually set. */
		qdwin_nested_publish_toplevel(tl);
		return;
	}

	/* xdg_toplevel.set_title diff. libweston has no title_changed
	 * callback — clients update the title via set_title and we only
	 * notice on the next commit. cached_title is seeded in
	 * qdwin_send_toplevel_added to the value already shipped, so
	 * this only triggers on *changes*. The wire event needs shell
	 * binding; the log line fires regardless so tests can assert
	 * the diff path even when no shell is connected. Note: surfaces
	 * stuck in the held bystander layer only commit once, so title
	 * updates only propagate after the surface is approved and
	 * migrates to the normal layer where frame-driven commits
	 * resume (set QDWIN_AUTO_APPROVE_TOPLEVELS=1 in dev/test). */
	{
		const char *cur = weston_desktop_surface_get_title(tl->desktop_surface);
		const char *cur_safe = cur ? cur : "";
		if (!tl->cached_title || strcmp(tl->cached_title, cur_safe) != 0) {
			free(tl->cached_title);
			tl->cached_title = qdwin_xstrdup_or_null(cur_safe);
			weston_log("qdwin: toplevel_title handle=%u title=\"%s\"\n",
				   tl->handle, cur_safe);
			if (qdwin->shell_bound && qdwin->shell_resource)
				qdwin_shell_v1_send_toplevel_title(qdwin->shell_resource,
								   tl->handle, cur_safe);
		}
	}
	/* xdg_toplevel.set_app_id / XWayland WM_CLASS diff. v31 shells receive
	 * the live value; older shells keep the one-shot value from
	 * toplevel_added. */
	{
		const char *cur = qdwin_toplevel_effective_app_id(qdwin, tl);
		const char *cur_safe = cur ? cur : "";
		if (!tl->cached_app_id || strcmp(tl->cached_app_id, cur_safe) != 0) {
			free(tl->cached_app_id);
			tl->cached_app_id = qdwin_xstrdup_or_null(cur_safe);
			weston_log("qdwin: toplevel_app_id handle=%u app_id=\"%s\"\n",
				   tl->handle, cur_safe);
			qdwin_emit_toplevel_app_id(qdwin, tl, cur_safe);
		}
	}

	/* XWayland configure feedback. Chromium and some Electron clients add a
	 * 64px CSD/frame extent to every WM resize while libweston reports
	 * surface == desktop geometry. That makes the extent invisible at send
	 * time. Once the post-configure commit changes size, learn
	 * (observed - sent) and issue a bounded corrective configure. The prior
	 * dimensions distinguish the actual response from an unrelated frame
	 * commit queued before the configure request. */
	if (tl->xwayland_configure_pending &&
	    qdwin_toplevel_is_xwayland(qdwin, tl)) {
		struct weston_geometry geometry =
			weston_desktop_surface_get_geometry(tl->desktop_surface);
		/* qdwin_shell_v1 reports XWayland's outer wl_surface extent.
		 * Desktop geometry can exclude a toolkit frame/shadow, so comparing
		 * that inner extent with an outer target prevents convergence. */
		int actual_w = surface->width > 0 ? surface->width : geometry.width;
		int actual_h = surface->height > 0 ? surface->height : geometry.height;
		bool changed = actual_w != tl->xwayland_before_w ||
			actual_h != tl->xwayland_before_h;

		if (changed) {
			if (actual_w != tl->xwayland_before_w) {
				int delta = actual_w - tl->xwayland_sent_w;
				if (delta >= 0)
					tl->xwayland_frame_w = delta;
			}
			if (actual_h != tl->xwayland_before_h) {
				int delta = actual_h - tl->xwayland_sent_h;
				if (delta >= 0)
					tl->xwayland_frame_h = delta;
			}

			if (actual_w == tl->xwayland_target_w &&
			    actual_h == tl->xwayland_target_h) {
				tl->xwayland_configure_pending = false;
				weston_log("qdwin: xwayland_configure_converged "
					   "handle=%u actual=%dx%d frame=%dx%d "
					   "corrections=%u\n",
					   tl->handle, actual_w, actual_h,
					   tl->xwayland_frame_w,
					   tl->xwayland_frame_h,
					   tl->xwayland_corrections);
			} else if (tl->xwayland_corrections < 3) {
				int configure_w = qdwin_client_extent_for_geometry(
					tl->xwayland_target_w, surface->width,
					geometry.width, tl->xwayland_frame_w);
				int configure_h = qdwin_client_extent_for_geometry(
					tl->xwayland_target_h, surface->height,
					geometry.height, tl->xwayland_frame_h);
				tl->xwayland_before_w = actual_w;
				tl->xwayland_before_h = actual_h;
				tl->xwayland_sent_w = configure_w;
				tl->xwayland_sent_h = configure_h;
				tl->xwayland_corrections++;
				weston_log("qdwin: xwayland_configure_correct "
					   "handle=%u target=%dx%d actual=%dx%d "
					   "frame=%dx%d -> client=%dx%d attempt=%u\n",
					   tl->handle, tl->xwayland_target_w,
					   tl->xwayland_target_h, actual_w, actual_h,
					   tl->xwayland_frame_w,
					   tl->xwayland_frame_h, configure_w,
					   configure_h, tl->xwayland_corrections);
				weston_desktop_surface_set_size(tl->desktop_surface,
							configure_w, configure_h);
			} else {
				tl->xwayland_configure_pending = false;
				weston_log("qdwin: xwayland_configure_gave_up "
					   "handle=%u target=%dx%d actual=%dx%d "
					   "after=%u\n",
					   tl->handle, tl->xwayland_target_w,
					   tl->xwayland_target_h, actual_w, actual_h,
					   tl->xwayland_corrections);
			}
		}
	}

	/* Keep the restore source synchronized with what the client actually
	 * committed while floating. outer_* used to retain the 800x600 seed
	 * forever, so a later maximize saved stale geometry. During XWayland
	 * convergence the target remains authoritative until the response lands. */
	if (!(tl->state & (QDWIN_TS_MAXIMIZED | QDWIN_TS_FULLSCREEN)) &&
	    tl->tiled == 0 && !tl->xwayland_configure_pending) {
		struct weston_geometry geometry =
			weston_desktop_surface_get_geometry(tl->desktop_surface);
		bool xwayland = qdwin_toplevel_is_xwayland(qdwin, tl);
		int inner_w = qdwin_committed_restore_extent(
			xwayland, surface->width, geometry.width);
		int inner_h = qdwin_committed_restore_extent(
			xwayland, surface->height, geometry.height);
		if (inner_w > 0 && inner_h > 0) {
			tl->outer_width = inner_w + tl->inset_w + tl->inset_e;
			tl->outer_height = inner_h + tl->inset_n + tl->inset_s;
		}
	}

	/* First-commit map path. S3 decides whether to migrate to
	 * the normal layer; S2 leaves it held. */
	if (!weston_surface_is_mapped(surface)) {
		weston_surface_map(surface);
		tl->mapped = 1;
		if (tl->state & QDWIN_TS_FULLSCREEN) {
			/* The client may request fullscreen before its first
			 * buffer. Keep that fullscreen placement authoritative:
			 * the normal cascade below would otherwise move the
			 * lock UI or any early-fullscreen client down/right and
			 * expose whatever is behind it. */
			qdwin_toplevel_apply_fullscreen_geometry(qdwin, tl, NULL);
		} else if (qdwin_test_place(qdwin, tl)) {
			/* A1-min test hook placed this toplevel at an explicit
			 * global position straddling outputs (see qdwin_test_place):
			 * skip the normal one-output placement policy entirely. */
		} else {
			/* Centre content on the primary output so chrome fits
			 * within visible bounds. Without this the view defaults
			 * to (0,0) and left/top chrome ends up at negative
			 * coordinates.
			 *
			 * Cascade: every previously-mapped non-nested toplevel
			 * pushes the new window down-right by 40px. Without this
			 * spawning two foots stacks them at the same coords —
			 * user only ever sees the top one and switching focus to
			 * the bottom one feels broken (text appears on a window
			 * the user can't see). 40 is small enough to keep most of
			 * the older window visible behind, large enough to read
			 * the chrome titlebar through the gap.
			 */
			struct weston_output *out = qdwin_primary_output(qdwin);
			if (out) {
				/* v25: new-window placement follows the live WM
				 * policy (centre / under-mouse / smart / cascade).
				 * The compositor default is cascade, so a
				 * shell-less / pre-v25 session keeps the historical
				 * down-right stagger. */
				int cx, cy;
				qdwin_compute_placement(qdwin, tl, surface,
							out, &cx, &cy);
				if (cx < 0) cx = 0;
				if (cy < 0) cy = 0;
				struct weston_coord_global p = { .c = weston_coord(cx, cy) };
				weston_view_set_position(tl->view, p);
				weston_view_update_transform(tl->view);
			}
		}
		{
			/* Include the placement origin (global content top-left)
			 * so journal-driven tests can target a window without
			 * re-deriving the placement math. */
			struct weston_coord_global mp =
				weston_view_get_pos_offset_global(tl->view);
			weston_log("qdwin: mapped handle=%u size=%dx%d pos=%d,%d (%s)\n",
				   tl->handle, surface->width, surface->height,
				   (int)mp.c.x, (int)mp.c.y,
				   tl->decorated ? "normal" : "held");
		}
		/* If approval (release_holding) already fired before this
		 * first commit, qdwin_toplevel_release_holding's autofocus
		 * call shortcircuited because the surface wasn't yet
		 * mapped. Re-arm now that both conditions hold. */
		qdwin_toplevel_autofocus_if_ready(tl);
		weston_compositor_schedule_repaint(qdwin->compositor);
	}

	if (tl->last_width != surface->width ||
	    tl->last_height != surface->height) {
		tl->last_width = surface->width;
		tl->last_height = surface->height;
		if (qdwin->shell_bound && qdwin->shell_resource) {
			struct weston_coord_global op =
				weston_view_get_pos_offset_global(tl->view);
			qdwin_shell_v1_send_toplevel_geometry(
				qdwin->shell_resource, tl->handle,
				(int)(op.c.x - tl->inset_w),
				(int)(op.c.y - tl->inset_n),
				(uint32_t)surface->width,
				(uint32_t)surface->height);
		}
		if (tl->decorated)
			qdwin_toplevel_position_chrome(tl);
	}
}

static void
qdwin_surface_move(struct weston_desktop_surface *dsurf,
		   struct weston_seat *seat, uint32_t serial, void *data)
{
	struct qdwin *qdwin = data;
	struct qdwin_toplevel *tl =
		weston_desktop_surface_get_user_data(dsurf);
	(void)seat;
	if (!tl)
		return;
	weston_log("qdwin: desktop_surface_move handle=%u serial=%u (stub)\n",
		   tl->handle, serial);
	/* S2: no-op. The archive validated that the shell, not the
	 * compositor, initiates drag via begin_interactive_move when
	 * the user grabs the chrome — this callback only fires for
	 * client-side move grabs (xdg_toplevel.move), which qdwin
	 * doesn't route through in the normal flow. */
	(void)qdwin;
}

static void
qdwin_surface_resize(struct weston_desktop_surface *dsurf,
		     struct weston_seat *seat, uint32_t serial,
		     enum weston_desktop_surface_edge edges, void *data)
{
	struct qdwin *qdwin = data;
	struct qdwin_toplevel *tl =
		weston_desktop_surface_get_user_data(dsurf);
	(void)seat; (void)edges;
	if (!tl)
		return;
	weston_log("qdwin: desktop_surface_resize handle=%u serial=%u (stub)\n",
		   tl->handle, serial);
	(void)qdwin;
}

/* xdg_toplevel.set_maximized / .set_fullscreen / .set_minimized
 * handlers. Without these libweston drops the requests silently
 * (libweston-desktop.c NULL-checks each callback before dispatch), so
 * client-side titlebar buttons (weston-terminal, foot+libdecor, GTK
 * CSD apps) appear clickable but do nothing. Each routes through the
 * same shared core the qdshell custom protocol uses so both paths
 * produce identical state. */
static void
qdwin_surface_maximized_requested(struct weston_desktop_surface *dsurf,
				  bool maximized, void *data)
{
	struct qdwin *qdwin = data;
	struct qdwin_toplevel *tl =
		weston_desktop_surface_get_user_data(dsurf);
	if (!tl || qdwin->locked)
		return;
	qdwin_toplevel_set_maximized(qdwin, tl, maximized);
}

static void
qdwin_surface_fullscreen_requested(struct weston_desktop_surface *dsurf,
				   bool fullscreen,
				   struct weston_output *output,
				   void *data)
{
	struct qdwin *qdwin = data;
	struct qdwin_toplevel *tl =
		weston_desktop_surface_get_user_data(dsurf);
	if (!tl)
		return;
	qdwin_toplevel_set_fullscreen(qdwin, tl, fullscreen, output);
}

static void
qdwin_surface_minimized_requested(struct weston_desktop_surface *dsurf,
				  void *data)
{
	struct qdwin *qdwin = data;
	struct qdwin_toplevel *tl =
		weston_desktop_surface_get_user_data(dsurf);
	if (!tl || qdwin->locked)
		return;
	qdwin_toplevel_set_minimized(qdwin, tl);
}

static const struct weston_desktop_api qdwin_desktop_api = {
	.struct_size = sizeof(struct weston_desktop_api),
	.surface_added = qdwin_surface_added,
	.surface_removed = qdwin_surface_removed,
	.committed = qdwin_surface_committed,
	.move = qdwin_surface_move,
	.resize = qdwin_surface_resize,
	.maximized_requested = qdwin_surface_maximized_requested,
	.fullscreen_requested = qdwin_surface_fullscreen_requested,
	.minimized_requested = qdwin_surface_minimized_requested,
};

/* ------------------------------------------------------------------
 * qdwin_shell_v1 request handlers.
 * ------------------------------------------------------------------ */

static int
qdwin_shell_require_bound(struct qdwin *qdwin, struct wl_resource *resource)
{
	if (qdwin->shell_bound && qdwin->shell_resource == resource)
		return 1;
	wl_resource_post_error(resource,
			       QDWIN_SHELL_V1_ERROR_NOT_BOUND,
			       "request issued before bind_as_shell");
	return 0;
}

static bool
qdwin_client_is_bound_shell(struct qdwin *qdwin, struct wl_client *client)
{
	return qdwin->shell_bound && qdwin->shell_resource &&
	       client == wl_resource_get_client(qdwin->shell_resource);
}

/* The single output the shell-capture authority covers. The facility exists
 * for GUI-test observation of the main head on the headless test VMs, whose
 * DRM connector is always Virtual-1; every other output — additional heads,
 * PipeWire forwards — stays at the fail-closed default. An exact-name pin,
 * not a deny-list: adding an output to a future golden must not silently
 * widen capture. */
#define QDWIN_SHELL_CAPTURE_OUTPUT "Virtual-1"

static bool
qdwin_shell_capture_output_ok(const char *name)
{
	return name && strcmp(name, QDWIN_SHELL_CAPTURE_OUTPUT) == 0;
}

/* QDWIN_ENABLE_SHELL_CAPTURE authority. libweston's capture API asks every
 * registered listener late, when a renderer pulls the pending capture task.
 * Grant only the exact wl_client that owns the live shell binding, and only
 * for the designated main output. Everyone
 * else is left at the API's fail-closed default (neither authorized nor
 * explicitly denied), so another independent authority may still make its
 * own decision without this listener weakening it. */
static void
qdwin_capture_auth_cb(struct wl_listener *listener,
		      struct weston_output_capture_attempt *attempt)
{
	struct qdwin *qdwin =
		wl_container_of(listener, qdwin, capture_auth_listener);
	const struct weston_output_capture_client *who;

	if (!attempt || !attempt->who)
		return;
	who = attempt->who;
	if (!who->client || !who->output || !who->output->name)
		return;
	if (!qdwin_client_is_bound_shell(qdwin, who->client))
		return;
	if (!qdwin_shell_capture_output_ok(who->output->name))
		return;

	attempt->authorized = true;
	weston_log("qdwin: shell capture authorized output=%s\n",
		   who->output->name);
}

/* v33 retained-frame stale-serve. Renderers retain each composited frame on
 * the designated capture output (vendored-libweston support, enabled below);
 * when a capture task is filed while the output cannot repaint — seat taken
 * away (compositor OFFSCREEN), display power forced off — or a scheduled
 * repaint never runs (wedge timer), the pending task is completed from that
 * retained frame instead of hanging until the client's timeout, and the
 * shell is told the pixels are STALE via capture_served_stale. Serving is
 * strictly less capable than VT recovery (a retained frame predates the
 * incident by definition); it converts "hang, timeout, no image" into
 * "immediate image + explicit stale verdict" for triage. */

#define QDWIN_CAPTURE_STALE_WEDGE_MS 5000

static void
qdwin_capture_serve_stale(struct qdwin *qdwin, const char *why)
{
	struct weston_output *output = qdwin->capture_retain_output;
	uint32_t age_ms = 0;
	uint64_t msc = 0;
	int served;

	if (!output)
		return;
	if (!weston_output_capture_retained_frame_info(output, &age_ms, &msc)) {
		weston_log("qdwin: capture stale-serve skipped (%s): "
			   "no retained frame yet\n", why);
		return;
	}
	if (!weston_output_has_renderer_capture_tasks(output))
		return;

	/* ORDER MATTERS: the stale event must be posted BEFORE the capture
	 * `complete` that serve_retained() sends, so the shell dispatches the
	 * stale mark before its capture pump returns and publishes the reply
	 * (both events ride the same client connection, so posting order is
	 * dispatch order). The rare mispredictions are fail-safe: if serving
	 * then completes nothing, the capture fails/times out (stale mark on
	 * no published image), or a later live repaint services it (genuine
	 * pixels conservatively downgraded to stale). */
	if (qdwin->shell_bound && qdwin->shell_resource &&
	    wl_resource_get_version(qdwin->shell_resource) >= 33)
		qdwin_shell_v1_send_capture_served_stale(
			qdwin->shell_resource, output->name, age_ms,
			(uint32_t)msc);
	served = weston_output_capture_serve_retained(output);
	if (served <= 0) {
		weston_log("qdwin: capture stale-serve (%s) completed no "
			   "tasks (rc=%d) after stale mark — capture will "
			   "fail or complete live-but-marked-stale\n",
			   why, served);
		return;
	}
	weston_log("qdwin: WARNING capture served STALE frame (%s) "
		   "output=%s tasks=%d age_ms=%u msc=%llu\n",
		   why, output->name, served, age_ms,
		   (unsigned long long)msc);
}

static int
qdwin_capture_stale_timer_cb(void *data)
{
	struct qdwin *qdwin = data;
	struct weston_output *output = qdwin->capture_retain_output;

	qdwin->capture_stale_timer_armed = false;
	if (output && weston_output_has_renderer_capture_tasks(output))
		qdwin_capture_serve_stale(qdwin, "repaint-wedge");
	return 0;
}

static void
qdwin_capture_task_filed_cb(struct wl_listener *listener, void *data)
{
	struct qdwin *qdwin =
		wl_container_of(listener, qdwin, capture_task_filed_listener);
	struct weston_output *output = data;

	if (output != qdwin->capture_retain_output)
		return;
	if (qdwin->compositor->session_active &&
	    output->power_state == WESTON_OUTPUT_POWER_NORMAL) {
		/* Healthy on paper: the scheduled repaint should service the
		 * task. Arm the wedge net anyway — if the task is still
		 * pending when the timer fires, serve stale rather than let
		 * the client time out. A serviced task empties the pending
		 * list, making the firing a no-op. Never re-arm an armed
		 * timer: a stream of new capture requests must not push the
		 * first pending task's deadline out indefinitely. */
		if (qdwin->capture_stale_timer &&
		    !qdwin->capture_stale_timer_armed) {
			qdwin->capture_stale_timer_armed = true;
			wl_event_source_timer_update(
				qdwin->capture_stale_timer,
				QDWIN_CAPTURE_STALE_WEDGE_MS);
		}
		return;
	}
	qdwin_capture_serve_stale(qdwin,
				  qdwin->compositor->session_active ?
				  "power-off" : "seat-away");
}

/* Fires on compositor output_destroyed_signal — i.e. on weston_output
 * disable as well as destroy, and BEFORE the output's capture_info (which
 * holds our task_filed link) is freed, so the remove here is safe. */
static void
qdwin_capture_retain_output_gone_cb(struct wl_listener *listener, void *data)
{
	struct qdwin *qdwin = wl_container_of(
		listener, qdwin, capture_retain_output_gone_listener);
	struct weston_output *output = data;

	if (output != qdwin->capture_retain_output)
		return;
	wl_list_remove(&qdwin->capture_task_filed_listener.link);
	wl_list_init(&qdwin->capture_task_filed_listener.link);
	qdwin->capture_retain_output = NULL;
}

/* Arm retention on the designated capture output. Idempotent; no-op unless
 * the shell-capture authority is enabled (test sessions only) and this is
 * the designated output. Re-arms via output_created after a disable/enable
 * cycle (qdwin_capture_retain_output_gone_cb cleared the watch). */
static void
qdwin_capture_retention_maybe_arm(struct qdwin *qdwin,
				  struct weston_output *output)
{
	if (!qdwin->capture_auth_registered)
		return;
	if (qdwin->capture_retain_output || !output)
		return;
	if (!qdwin_shell_capture_output_ok(output->name))
		return;
	/* Weak-symbol availability gate: NULL against a stock libweston
	 * runtime — disable the feature rather than crash (see
	 * qdwin-capture-retention.h). Checked once here; every other caller
	 * runs only after arming succeeded. */
	if (!weston_output_capture_retention_enable ||
	    !weston_output_capture_add_task_filed_listener ||
	    !weston_output_capture_retained_frame_info ||
	    !weston_output_capture_serve_retained ||
	    !weston_output_has_renderer_capture_tasks) {
		weston_log("qdwin: retained-frame capture unavailable "
			   "(stock libweston runtime lacks the vendored "
			   "retention API) — stale-serve disabled\n");
		return;
	}

	weston_output_capture_retention_enable(output);
	qdwin->capture_task_filed_listener.notify = qdwin_capture_task_filed_cb;
	weston_output_capture_add_task_filed_listener(
		output, &qdwin->capture_task_filed_listener);
	qdwin->capture_retain_output = output;
	if (!qdwin->capture_stale_timer)
		qdwin->capture_stale_timer = wl_event_loop_add_timer(
			wl_display_get_event_loop(
				qdwin->compositor->wl_display),
			qdwin_capture_stale_timer_cb, qdwin);
	weston_log("qdwin: retained-frame capture armed on output=%s\n",
		   output->name);
}

/* May this client MUTATE the display layout (zwlr_output_manager apply path)?
 *
 * Enumeration of heads/modes is advertised to every client (wlr-randr, kanshi,
 * the qdshell Display tab all read it), but the apply/test mutation path —
 * which can weston_output_disable() / move / rescale every display in every
 * silo — must be restricted to the trusted shell. Mirrors the layer-shell gate
 * (bind_qdwin_layer_shell): the bound shell client, or a second wl_client of
 * the same (pid,uid) as the shell (qdshell opens a separate connection for some
 * surfaces), or — before any shell has bound — the configured allowed_uid.
 *
 * Deliberate test fail-open: when allowed_uid is unset ((uid_t)-1) and no
 * shell is bound yet, this returns true so the historical broad posture is
 * unchanged; once a shell binds, only that shell (by client or pid/uid) passes.
 */
static bool
qdwin_om_client_may_mutate(struct qdwin *qdwin, struct wl_client *client)
{
	pid_t pid; uid_t uid; gid_t gid;

	/* findings F8: a sandboxed silo client must never drive output-layout
	 * mutation (resolution / enable-disable / mode changes — a DoS and
	 * display-spoofing lever), regardless of uid or the pre-shell window.
	 * uid == allowed_uid is satisfied by every tier-0..3 silo client
	 * (isolation-tiers.md), so deny secctx clients outright here, before the
	 * uid-based pre-shell fallback in qdwin_om_mutation_allowed can grant it.
	 * Enumeration stays open (intentional, threat-model.md); only mutation
	 * is gated. */
	if (qdwin_secctx_client_find(qdwin, client))
		return false;

	wl_client_get_credentials(client, &pid, &uid, &gid);
	return qdwin_om_mutation_allowed(
		qdwin_client_is_bound_shell(qdwin, client),
		qdwin->shell_bound && qdwin->shell_resource,
		pid, uid, qdwin->shell_pid, qdwin->shell_uid,
		qdwin->allowed_uid);
}

static void
qdwin_handle_bind_as_shell(struct wl_client *client,
			   struct wl_resource *resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	pid_t pid; uid_t uid; gid_t gid;

	wl_client_get_credentials(client, &pid, &uid, &gid);

	if (qdwin->shell_bound && qdwin->shell_resource != resource) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_ALREADY_BOUND,
				       "qdwin_shell_v1: shell role already claimed");
		return;
	}
	qdwin->shell_bound = 1;
	qdwin->shell_resource = resource;
	qdwin->shell_pid = pid;
	qdwin->shell_uid = uid;
	weston_log("qdwin: shell bound (uid=%u pid=%d); replaying %u toplevels\n",
		   (unsigned)uid, (int)pid,
		   (unsigned)wl_list_length(&qdwin->toplevels));

	wl_list_for_each(tl, &qdwin->toplevels, link)
		qdwin_send_toplevel_added(qdwin, tl);

	/* v2 replay: seats and outputs that existed before the shell
	 * bound. seat_removed / output_removed follow the normal signal
	 * path once these go away. */
	if (wl_resource_get_version(resource) >= 2) {
		struct weston_seat *seat;
		struct weston_output *output;
		wl_list_for_each(seat, &qdwin->compositor->seat_list, link)
			qdwin_send_seat_created(qdwin, seat);
		wl_list_for_each(output, &qdwin->compositor->output_list, link)
			qdwin_send_output_created_evt(qdwin, output);
	}
	/* v14 replay: seat_focus_changed for every seat we track, so the
	 * shell starts with a coherent focus map. Defined lower (the seat
	 * tracker struct lives below); forward-declared at the top. */
	if (wl_resource_get_version(resource) >= 14)
		qdwin_replay_seat_focus_for_shell(qdwin);
}

static void
qdwin_handle_destroy(struct wl_client *client, struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_toplevel_release_holding(struct qdwin_toplevel *tl, const char *cause)
{
	if (tl->decorated)
		return;
	/* §6.8 S4: nested-proxy gating overrides chrome-driven release.
	 * Until nested_proxy_decision lands with allow, chrome calls
	 * (set_border_color, attach_decoration) preserve the held layer
	 * so the user doesn't see an unauthorised inner toplevel briefly
	 * flash before the broker decides. The decision-handler clears
	 * the pending flag and re-enters this function. */
	if (tl->nested_proxy_pending_decision &&
	    strncmp(cause, "nested_proxy_decision/", 22) != 0) {
		weston_log("qdwin: holding_released handle=%u via %s deferred — "
			   "nested-proxy waiting for admin decision\n",
			   tl->handle, cause);
		return;
	}
	tl->decorated = 1;
	weston_view_move_to_layer(tl->view,
				  &tl->qdwin->normal_layer.view_list);
	/* Chrome views migrate alongside the content when they exist at
	 * this point. If set_border_color fires before attach_decoration
	 * (qdshell's order), chrome views don't exist yet — migration of
	 * each chrome view happens in qdwin_chrome_attach_side instead,
	 * which checks tl->decorated and skips the held layer. */
	for (int s = 0; s < QDWIN_SIDES; s++) {
		if (tl->chrome[s].view)
			weston_view_move_to_layer(tl->chrome[s].view,
				&tl->qdwin->normal_layer.view_list);
	}
	/* v24: a workspace switch may have moved off the window's workspace
	 * while it was still held; park it on the hidden layer if so, so it
	 * doesn't flash onto the wrong desktop on release. */
	if (tl->workspace != tl->qdwin->active_workspace)
		qdwin_toplevel_move_to_layer(tl,
			&tl->qdwin->workspace_hidden_layer);
	weston_log("qdwin: holding_released handle=%u via %s (held → normal)\n",
		   tl->handle, cause);
	weston_compositor_schedule_repaint(tl->qdwin->compositor);

	/* Auto-focus is wired in qdwin_toplevel_autofocus_if_ready, called
	 * from BOTH the release-holding path (here, in case the surface
	 * already committed a buffer) and the first-commit map path (in
	 * case release fires before first commit). See the helper's
	 * docblock for why both call sites are needed. */
	qdwin_toplevel_autofocus_if_ready(tl);
}

/* Assign keyboard focus to this toplevel on every seat with a keyboard,
 * but only when the surface is BOTH mapped (has a buffer; safe to
 * render) AND decorated (on the normal layer, not still held in the
 * bystander layer awaiting approval). Idempotent — re-arming with the
 * same focus is cheap and the kbd->focus equality check shortcircuits.
 *
 * Without this, the qdshell side has no path to drive focus
 * (Services/Qdwin/Qdwin.qml::focusWindow is a TODO stub), leaving
 * keyboard focus pinned at UINT32_MAX indefinitely — every newly-
 * spawned window is unfocused until the user clicks it. This matches
 * the default behaviour of sway/hyprland/labwc/etc.
 *
 * Called from both:
 *   - qdwin_toplevel_release_holding (held→normal transition): handles
 *     the common case where first_commit/map fires before approval.
 *   - qdwin_surface_committed first-map branch: handles the case
 *     where approval (release_holding) fires before the surface has
 *     committed any buffer (e.g. QDWIN_AUTO_APPROVE_TOPLEVELS env).
 * Only one call ends up actually emitting focus_signal; the other
 * shortcircuits on kbd->focus equality.
 *
 * Skipped when the session is locked — the locker's EXCLUSIVE layer
 * surface (ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE in
 * qdwin_layer_surface_apply) owns focus. Without the guard a
 * background app spawning during a lock could briefly steal focus
 * before the next exclusive-grab re-assertion.
 *
 * See todo/qdshell-no-focus-driving.md.
 */
static void
qdwin_toplevel_autofocus_if_ready(struct qdwin_toplevel *tl)
{
	if (!tl || !tl->qdwin || tl->qdwin->locked)
		return;
	if (!tl->decorated)  /* still in held bystander layer */
		return;
	/* v24: don't pull focus to a window parked on another workspace. */
	if (tl->workspace != tl->qdwin->active_workspace)
		return;
	if (!tl->view || !tl->view->surface)
		return;
	if (!weston_surface_is_mapped(tl->view->surface))
		return;
	struct weston_seat *seat;
	wl_list_for_each(seat, &tl->qdwin->compositor->seat_list, link) {
		struct weston_keyboard *kbd = weston_seat_get_keyboard(seat);
		if (kbd && kbd->focus != tl->view->surface) {
			struct qdwin_seat_tracker *tr =
				qdwin_seat_tracker_for_seat(tl->qdwin, seat);
			/* RDP-headless: the backend can swap the wl_keyboard
			 * after we registered our focus_signal listener, so
			 * the listener may be sitting on a stale keyboard and
			 * never fire for this activation. Reconcile it onto the
			 * live keyboard first; then the focus_signal emitted by
			 * weston_view_activate_input reaches our listener and
			 * focus propagates organically (the DRM path already
			 * had a live binding, so this is a no-op there). Use the
			 * activation path rather than bare keyboard focus: XWayland
			 * consumes libweston's activate_signal to transfer real X11
			 * input focus. */
			qdwin_seat_tracker_rebind_focus_listener(tr);
			weston_view_activate_input(tl->view, seat,
						   WESTON_ACTIVATE_FLAG_CONFIGURE);
			/* Immediate-emit backstop for any case where the
			 * focus_signal still doesn't reach us. Dedupe-safe:
			 * qdwin_seat_emit_focus_now short-circuits on
			 * last_focused_handle, so if the listener already
			 * emitted for this handle this is a no-op (no
			 * double-emit on either backend). */
			qdwin_seat_emit_focus_now(tr);
		}
	}
}

static void
qdwin_handle_set_border_color(struct wl_client *client,
			      struct wl_resource *resource,
			      uint32_t handle, uint32_t rgba)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
				       "set_border_color: unknown handle %u",
				       handle);
		return;
	}
	/* P05a: store per-toplevel so the SSD paint helper has a single
	 * source of truth for the silo chrome colour. Pre-P05a the rgba
	 * arg was logged + discarded; that meant a re-issued
	 * attach_decoration call (qdshell legitimately does this on
	 * state changes) silently dropped the colour. Now the per-tl
	 * state survives, and qdwin_toplevel_border_rgba(tl) is the
	 * accessor the paint helper + unit tests read. */
	tl->border_rgba = rgba;
	tl->border_rgba_set = 1;
	weston_log("qdwin: set_border_color handle=%u rgba=%#010x\n",
		   handle, rgba);
	qdwin_toplevel_release_holding(tl, "set_border_color");
}

/* P05a: accessor for the per-toplevel chrome border rgba.
 *
 * Returns the value last set by qdwin_shell_v1.set_border_color for
 * `tl`. When no colour has been set (border_rgba_set == 0), returns
 * `fallback` so callers don't need a separate "is-set" probe — pass
 * 0 for "transparent / use qdshell default", or a packed RGBA for a
 * compositor-side default.
 *
 * The split between `border_rgba` and `border_rgba_set` exists so a
 * legitimate set_border_color(0x000000ff) (opaque black, R=G=B=0,
 * A=0xff in RGBA8888) is not indistinguishable from "never set".
 * The SSD paint helper uses this
 * accessor instead of reading the field directly so the "unset →
 * fallback" semantics live in one place.
 */
uint32_t
qdwin_toplevel_border_rgba(struct qdwin_toplevel *tl, uint32_t fallback)
{
	if (!tl || !tl->border_rgba_set)
		return fallback;
	return tl->border_rgba;
}

/* ------------------------------------------------------------------
 * Chrome surface/view management.
 * ------------------------------------------------------------------ */

static void
qdwin_chrome_detach(struct qdwin_chrome *c)
{
	if (!c->surface)
		return;
	if (c->view) {
		weston_view_destroy(c->view);
		c->view = NULL;
	}
	wl_list_remove(&c->surface_destroy.link);
	wl_list_remove(&c->surface_commit.link);
	c->surface = NULL;
}

static void
qdwin_chrome_surface_destroyed(struct wl_listener *listener, void *data)
{
	struct qdwin_chrome *c = wl_container_of(listener, c, surface_destroy);
	(void)data;
	/* Shell's wl_surface went away before we did; drop the view. */
	if (c->view) {
		weston_view_destroy(c->view);
		c->view = NULL;
	}
	wl_list_remove(&c->surface_destroy.link);
	wl_list_init(&c->surface_destroy.link);
	wl_list_remove(&c->surface_commit.link);
	wl_list_init(&c->surface_commit.link);
	c->surface = NULL;
	weston_log("qdwin: chrome side=%d surface destroyed by client\n",
		   c->side);
}

static void
qdwin_chrome_surface_committed(struct wl_listener *listener, void *data)
{
	struct qdwin_chrome *c = wl_container_of(listener, c, surface_commit);
	(void)data;
	if (!c->view)
		return;
	/* Mark geometry dirty so the next repaint re-derives the view
	 * bbox from the (possibly resized) surface. damage_all covers the
	 * old pixels — the newer, smaller chrome won't otherwise damage
	 * the region it no longer occupies. Cheap on single-window
	 * headless tests; can be tightened to the union of old+new bounds
	 * once production scenes exercise chrome resize. */
	weston_view_geometry_dirty(c->view);
	weston_compositor_damage_all(c->tl->qdwin->compositor);
}

static void
qdwin_toplevel_position_chrome(struct qdwin_toplevel *tl)
{
	struct weston_coord_global content_pos =
		weston_view_get_pos_offset_global(tl->view);
	double x = content_pos.c.x;
	double y = content_pos.c.y;
	int cw, ch;
	if (tl->is_nested_proxy) {
		/* Nested-proxy: the curtain has no weston_desktop_surface, but
		 * we know the rect from last_width/last_height. */
		cw = tl->last_width  > 0 ? tl->last_width  : 800;
		ch = tl->last_height > 0 ? tl->last_height : 600;
	} else {
		struct weston_surface *content =
			weston_desktop_surface_get_surface(tl->desktop_surface);
		cw = content->width;
		ch = content->height;
	}

	for (int s = 0; s < QDWIN_SIDES; s++) {
		struct qdwin_chrome *c = &tl->chrome[s];
		if (!c->view)
			continue;
		double cx = x, cy = y;
		switch (s) {
		case QDWIN_SIDE_N: cx = x - tl->inset_w; cy = y - tl->inset_n; break;
		case QDWIN_SIDE_S: cx = x - tl->inset_w; cy = y + ch;          break;
		case QDWIN_SIDE_W: cx = x - tl->inset_w; cy = y;               break;
		case QDWIN_SIDE_E: cx = x + cw;          cy = y;               break;
		}
		struct weston_coord_global p = { .c = weston_coord(cx, cy) };
		weston_view_set_position(c->view, p);
		weston_view_update_transform(c->view);
	}
}

static int
qdwin_chrome_attach_side(struct qdwin_toplevel *tl, int side,
			 struct wl_resource *surface_res)
{
	struct qdwin_chrome *c = &tl->chrome[side];

	/* NULL means "detach this side". */
	if (!surface_res) {
		qdwin_chrome_detach(c);
		return 0;
	}

	struct weston_surface *ws = wl_resource_get_user_data(surface_res);
	if (!ws)
		return -1;

	/* If already attached to the same surface, leave in place —
	 * position update below handles geometry changes. */
	if (c->surface == ws && c->view)
		return 0;

	/* Otherwise drop the old, install the new. */
	qdwin_chrome_detach(c);

	c->surface = ws;
	c->side = side;
	c->tl = tl;
	c->surface_destroy.notify = qdwin_chrome_surface_destroyed;
	wl_signal_add(&ws->destroy_signal, &c->surface_destroy);
	c->surface_commit.notify = qdwin_chrome_surface_committed;
	wl_signal_add(&ws->commit_signal, &c->surface_commit);

	c->view = weston_view_create(ws);
	if (!c->view) {
		wl_list_remove(&c->surface_destroy.link);
		wl_list_remove(&c->surface_commit.link);
		c->surface = NULL;
		return -1;
	}
	/* Land on the same layer as the content view. When holding hasn't
	 * released yet (rare — attach_decoration before set_border_color)
	 * we stash on held and release_holding will migrate us. Otherwise
	 * (set_border_color already fired) the content is already on
	 * normal, and we must follow so the chrome actually composites.
	 * v24: a decorated toplevel parked on a non-active workspace lives on
	 * the hidden layer — its chrome must follow there, not flash onto the
	 * active workspace. */
	struct weston_layer *target;
	if (!tl->decorated)
		target = &tl->qdwin->held_layer;
	else if (tl->workspace != tl->qdwin->active_workspace)
		target = &tl->qdwin->workspace_hidden_layer;
	else
		target = &tl->qdwin->normal_layer;
	weston_view_move_to_layer(c->view, &target->view_list);
	/* Mark the roleless chrome surface as mapped so it actually
	 * composites. Shell-owned surfaces have no weston_desktop life-
	 * cycle to drive this for us. */
	if (!weston_surface_is_mapped(ws))
		weston_surface_map(ws);

	return 0;
}

static void
qdwin_toplevel_seed_outer_from_committed(struct qdwin_toplevel *tl)
{
	int inner_w, inner_h;

	if (tl->outer_width > 0 && tl->outer_height > 0)
		return;
	if (tl->is_nested_proxy) {
		/* The proxy curtain already is the outer rect; its separately
		 * attached chrome must not be added a second time. */
		tl->outer_width = tl->last_width > 0 ? tl->last_width : 800;
		tl->outer_height = tl->last_height > 0 ? tl->last_height : 600;
		return;
	}
	struct weston_surface *surface =
		weston_desktop_surface_get_surface(tl->desktop_surface);
	struct weston_geometry geometry =
		weston_desktop_surface_get_geometry(tl->desktop_surface);

	/* Native set_size() uses desktop geometry. XWayland's shell-visible outer
	 * rectangle is its wl_surface extent: desktop geometry can omit the
	 * client's own frame/shadow. The configure conversion below translates
	 * that saved outer target back to the client extent. */
	bool xwayland = qdwin_toplevel_is_xwayland(tl->qdwin, tl);
	inner_w = qdwin_committed_restore_extent(
		xwayland, surface ? surface->width : 0, geometry.width);
	inner_h = qdwin_committed_restore_extent(
		xwayland, surface ? surface->height : 0, geometry.height);
	if (inner_w <= 0)
		inner_w = 800;
	if (inner_h <= 0)
		inner_h = 600;
	tl->outer_width = inner_w + tl->inset_w + tl->inset_e;
	tl->outer_height = inner_h + tl->inset_n + tl->inset_s;
}

static void
qdwin_toplevel_apply_inset(struct qdwin_toplevel *tl)
{
	int outer_w, outer_h;
	int inner_w, inner_h;

	/* §6.8 S2: nested-proxy toplevels have no desktop_surface — the
	 * curtain is the content. Use last_width/last_height as the
	 * authoritative outer size and skip the desktop-surface set_size
	 * round-trip (the inner client gets resized via S3 input + the
	 * nested compositor's own xdg_toplevel.configure path). */
	if (tl->is_nested_proxy) {
		outer_w = tl->last_width  > 0 ? tl->last_width  : 800;
		outer_h = tl->last_height > 0 ? tl->last_height : 600;
		inner_w = qdwin_inset_inner_extent(outer_w, tl->inset_w, tl->inset_e);
		inner_h = qdwin_inset_inner_extent(outer_h, tl->inset_n, tl->inset_s);
		weston_log("qdwin: apply_inset handle=%u (nested-proxy) "
			   "outer=%dx%d inset=N%d E%d S%d W%d -> inner=%dx%d "
			   "(curtain stays at outer; inner client resize "
			   "deferred to S3)\n",
			   tl->handle, outer_w, outer_h,
			   tl->inset_n, tl->inset_e, tl->inset_s, tl->inset_w,
			   inner_w, inner_h);
		return;
	}

	/* Outer size = whatever we previously asked the client for, or
	 * (first time through) its current desktop geometry plus our chrome. This
	 * keeps repeated attach_decoration calls stable — we only shrink
	 * from outer to inner once per outer size.
	 *
	 * Holding-state chicken-and-egg: clients in qdwin's holding layer
	 * may not have committed a sized buffer yet when qdshell calls
	 * attach_decoration (qdshell paints chrome at its own fallback
	 * size; client is still waiting for an initial configure()). In
	 * that case surface->width/height are both 0; without a fallback
	 * here we'd compute outer=0, inner=clamp(-inset,1)=1, and ship
	 * configure(1,1) to the client, locking it to 1×1 forever. Pick
	 * a sensible default outer (800×600) when the client hasn't
	 * spoken yet — matches qdshell's chrome fallback (640+insets,
	 * 400+insets) closely enough that the first repaint after the
	 * client commits a real size produces a stable window.
	 */
	qdwin_toplevel_seed_outer_from_committed(tl);
	outer_w = tl->outer_width;
	outer_h = tl->outer_height;

	inner_w = qdwin_inset_inner_extent(outer_w, tl->inset_w, tl->inset_e);
	inner_h = qdwin_inset_inner_extent(outer_h, tl->inset_n, tl->inset_s);

	weston_log("qdwin: apply_inset handle=%u outer=%dx%d inset=N%d E%d S%d W%d -> inner=%dx%d\n",
		   tl->handle, outer_w, outer_h,
		   tl->inset_n, tl->inset_e, tl->inset_s, tl->inset_w,
		   inner_w, inner_h);

	/* XWayland's set_size path configures the client extent, while
	 * weston_desktop_surface_get_geometry() describes the visible window.
	 * Chromium CSD shadows make surface->width exceed geometry.width (64 px in
	 * the regression image). Feeding the visible extent back as the client
	 * extent adds that delta on every restore. Remove the current surface-vs-
	 * geometry delta only for XWayland: native Wayland clients such as Firefox
	 * can also publish a non-zero buffer/geometry delta, but their set_size()
	 * contract already takes the visible geometry. */
	struct weston_surface *surface =
		weston_desktop_surface_get_surface(tl->desktop_surface);
	struct weston_geometry geometry =
		weston_desktop_surface_get_geometry(tl->desktop_surface);
	bool xwayland = qdwin_toplevel_is_xwayland(tl->qdwin, tl);
	int configure_w = xwayland
		? qdwin_client_extent_for_geometry(
			inner_w, surface ? surface->width : geometry.width,
			geometry.width, tl->xwayland_frame_w)
		: inner_w;
	int configure_h = xwayland
		? qdwin_client_extent_for_geometry(
			inner_h, surface ? surface->height : geometry.height,
			geometry.height, tl->xwayland_frame_h)
		: inner_h;
	weston_log("qdwin: configure_extent handle=%u xwayland=%d desired=%dx%d surface=%dx%d geometry=%dx%d -> client=%dx%d\n",
		   tl->handle, xwayland, inner_w, inner_h,
		   surface ? surface->width : 0, surface ? surface->height : 0,
		   geometry.width, geometry.height, configure_w, configure_h);
	if (xwayland) {
		tl->xwayland_target_w = inner_w;
		tl->xwayland_target_h = inner_h;
		tl->xwayland_sent_w = configure_w;
		tl->xwayland_sent_h = configure_h;
		tl->xwayland_before_w = geometry.width > 0
			? geometry.width : (surface ? surface->width : 0);
		tl->xwayland_before_h = geometry.height > 0
			? geometry.height : (surface ? surface->height : 0);
		tl->xwayland_corrections = 0;
		tl->xwayland_configure_pending = true;
	}
	weston_desktop_surface_set_size(tl->desktop_surface,
					configure_w, configure_h);
}

static void
qdwin_handle_attach_decoration(struct wl_client *client,
			       struct wl_resource *resource,
			       uint32_t handle,
			       struct wl_resource *north,
			       struct wl_resource *east,
			       struct wl_resource *south,
			       struct wl_resource *west)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;

	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
				       "attach_decoration: unknown handle %u",
				       handle);
		return;
	}

	struct wl_resource *sides[QDWIN_SIDES] = { north, east, south, west };
	for (int s = 0; s < QDWIN_SIDES; s++) {
		if (qdwin_chrome_attach_side(tl, s, sides[s]) < 0) {
			weston_log("qdwin: attach_decoration handle=%u side=%d failed\n",
				   handle, s);
		}
	}

	/* Derive insets from the chrome surfaces' committed sizes.
	 * Shell is required to have committed before calling us. */
	tl->inset_n = tl->chrome[QDWIN_SIDE_N].surface
		? tl->chrome[QDWIN_SIDE_N].surface->height : 0;
	tl->inset_e = tl->chrome[QDWIN_SIDE_E].surface
		? tl->chrome[QDWIN_SIDE_E].surface->width  : 0;
	tl->inset_s = tl->chrome[QDWIN_SIDE_S].surface
		? tl->chrome[QDWIN_SIDE_S].surface->height : 0;
	tl->inset_w = tl->chrome[QDWIN_SIDE_W].surface
		? tl->chrome[QDWIN_SIDE_W].surface->width  : 0;

	/* P05a: read the per-toplevel border rgba via the accessor so a
	 * re-issued attach_decoration emits a journal trail confirming the
	 * silo colour survived the re-bind. This is the load-bearing
	 * consumer of qdwin_toplevel_border_rgba(); without it the field
	 * was inert. The shell paints chrome surfaces itself (qdwin SSD is
	 * stub today) but the stored rgba is the single source of truth a
	 * future SSD paint helper / focus ring will read, and logging it
	 * here means a regression that drops the field flips this line. */
	uint32_t border_rgba_seen = qdwin_toplevel_border_rgba(tl, 0u);
	weston_log("qdwin: attach_decoration handle=%u inset=N%d E%d S%d W%d "
		   "border_rgba=%#010x\n",
		   handle, tl->inset_n, tl->inset_e, tl->inset_s, tl->inset_w,
		   border_rgba_seen);

	qdwin_toplevel_release_holding(tl, "attach_decoration");

	if (tl->inset_n || tl->inset_e || tl->inset_s || tl->inset_w)
		qdwin_toplevel_apply_inset(tl);

	qdwin_toplevel_position_chrome(tl);
}

static void
qdwin_handle_request_close(struct wl_client *client,
			   struct wl_resource *resource, uint32_t handle)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;

	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		weston_log("qdwin: request_close unknown handle=%u\n", handle);
		return;
	}
	if (tl->is_nested_proxy) {
		/* §6.8 S2: close on a proxy fires close_requested on the
		 * nested toplevel resource — the nested compositor propagates
		 * to its inner client via xdg_toplevel.close. */
		qdwin_nested_proxy_send_close(tl);
		weston_log("qdwin: request_close handle=%u (nested-proxy: "
			   "fired close_requested)\n", handle);
		return;
	}
	/* Multi-machine remote windows (engine "qdistro.mm") have SOURCE-mediated
	 * close (codex impl-34 Q3): the shell routes a CloseRequest upstream and
	 * the source emits Closed; the backing pixel client (windowed FreeRDP)
	 * then exits, destroying this toplevel via client disconnect. A local
	 * request_close would xdg-close that client = the forbidden client-tree
	 * kill. Refuse it here so the COMPOSITOR enforces the invariant (qdshell
	 * close interception becomes belt+braces, not the only guard). qdshell's
	 * RemoteMachineWindows never calls request_close for these handles; this
	 * fires only on a shell bug/compromise — refuse silently (a protocol error
	 * would needlessly disconnect the shell). */
	const char *secctx_engine = qdwin_toplevel_secctx_engine(qdwin, tl);
	if (secctx_engine && strcmp(secctx_engine, "qdistro.mm") == 0) {
		weston_log("qdwin: request_close handle=%u REFUSED "
			   "(engine=qdistro.mm: close is source-mediated)\n",
			   handle);
		return;
	}
	weston_desktop_surface_close(tl->desktop_surface);
	weston_log("qdwin: request_close handle=%u dispatched\n", handle);
}

static void
qdwin_handle_request_minimize(struct wl_client *client,
			      struct wl_resource *resource, uint32_t handle)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		weston_log("qdwin: request_minimize unknown handle=%u\n", handle);
		return;
	}
	qdwin_toplevel_set_minimized(qdwin, tl);
}

static void
qdwin_toplevel_set_minimized(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
	if (tl->nested_proxy_pending_decision) {
		weston_log("qdwin: set_minimized handle=%u ignored — "
			   "nested-proxy waiting for admin decision\n", tl->handle);
		return;
	}
	if (tl->state & QDWIN_TS_MINIMIZED)
		return;
	qdwin_toplevel_move_to_layer(tl, &qdwin->minimized_layer);
	tl->state |= QDWIN_TS_MINIMIZED;
	weston_log("qdwin: set_minimized handle=%u (state=%#x)\n",
		   tl->handle, tl->state);
	qdwin_send_toplevel_state(qdwin, tl);
	weston_compositor_schedule_repaint(qdwin->compositor);
}

/* Fullscreen: like maximize but fills the entire output (covers panels
 * and layer-shell exclusive zones). On unset, restore to the saved
 * pre-fullscreen geometry. The output param may be NULL — pick the
 * primary in that case. */
static void
qdwin_toplevel_apply_fullscreen_geometry(struct qdwin *qdwin,
					 struct qdwin_toplevel *tl,
					 struct weston_output *output)
{
	struct weston_output *out = output ? output
					   : qdwin_primary_output(qdwin);
	if (!out) {
		weston_log("qdwin: fullscreen geometry handle=%u: no output\n",
			   tl->handle);
		return;
	}

	int ox = out->pos.c.x, oy = out->pos.c.y;
	int ow = out->width,   oh = out->height;
	tl->outer_width  = ow;
	tl->outer_height = oh;
	if (tl->is_nested_proxy) {
		qdwin_nested_proxy_set_geometry(tl, ow, oh);
	} else {
		weston_desktop_surface_set_fullscreen(tl->desktop_surface,
						      true);
	}

	struct weston_coord_global origin = {
		.c = weston_coord(ox + tl->inset_w,
				  oy + tl->inset_n),
	};
	weston_view_set_output(tl->view, out);
	weston_view_set_position(tl->view, origin);
	weston_view_update_transform(tl->view);
	qdwin_toplevel_apply_inset(tl);
	qdwin_toplevel_position_chrome(tl);
}

static void
qdwin_toplevel_set_fullscreen(struct qdwin *qdwin,
			      struct qdwin_toplevel *tl,
			      bool fullscreen,
			      struct weston_output *output)
{
	if (tl->nested_proxy_pending_decision) {
		weston_log("qdwin: set_fullscreen handle=%u ignored — "
			   "nested-proxy waiting for admin decision\n", tl->handle);
		return;
	}
	int want_fs = fullscreen ? 1 : 0;
	int is_fs   = (tl->state & QDWIN_TS_FULLSCREEN) ? 1 : 0;
	if (want_fs == is_fs) {
		if (want_fs)
			qdwin_toplevel_apply_fullscreen_geometry(qdwin, tl,
								output);
		weston_log("qdwin: set_fullscreen handle=%u fs=%d noop\n",
			   tl->handle, want_fs);
		return;
	}

	if (want_fs) {
		struct weston_output *out = output ? output
						   : qdwin_primary_output(qdwin);
		if (!out) {
			weston_log("qdwin: set_fullscreen handle=%u: no output\n",
				   tl->handle);
			return;
		}
		struct weston_coord_global pos =
			weston_view_get_pos_offset_global(tl->view);
		tl->saved_x = pos.c.x;
		tl->saved_y = pos.c.y;
		qdwin_toplevel_seed_outer_from_committed(tl);
		tl->saved_outer_w = tl->outer_width;
		tl->saved_outer_h = tl->outer_height;

		int ox = out->pos.c.x, oy = out->pos.c.y;
		qdwin_toplevel_apply_fullscreen_geometry(qdwin, tl, out);

		tl->state |= QDWIN_TS_FULLSCREEN;
		weston_log("qdwin: set_fullscreen handle=%u fs=1 outer=%dx%d at (%d,%d) "
			   "saved=%dx%d@(%.0f,%.0f)\n",
			   tl->handle, tl->outer_width, tl->outer_height, ox, oy,
			   tl->saved_outer_w, tl->saved_outer_h,
			   tl->saved_x, tl->saved_y);
	} else {
		if (tl->is_nested_proxy) {
			qdwin_nested_proxy_set_geometry(tl, tl->saved_outer_w,
							tl->saved_outer_h);
		} else {
			weston_desktop_surface_set_fullscreen(tl->desktop_surface,
							      false);
		}
		tl->outer_width  = tl->saved_outer_w;
		tl->outer_height = tl->saved_outer_h;
		struct weston_coord_global pos = {
			.c = weston_coord(tl->saved_x, tl->saved_y),
		};
		weston_view_set_position(tl->view, pos);
		qdwin_toplevel_apply_inset(tl);
		qdwin_toplevel_position_chrome(tl);

		tl->state &= ~QDWIN_TS_FULLSCREEN;
		weston_log("qdwin: set_fullscreen handle=%u fs=0 restored=%dx%d@(%.0f,%.0f)\n",
			   tl->handle, tl->outer_width, tl->outer_height,
			   tl->saved_x, tl->saved_y);
	}
	qdwin_send_toplevel_state(qdwin, tl);
	weston_compositor_schedule_repaint(qdwin->compositor);
}

static void
qdwin_handle_request_maximize(struct wl_client *client,
			      struct wl_resource *resource,
			      uint32_t handle, uint32_t maximized)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		weston_log("qdwin: request_maximize unknown handle=%u\n", handle);
		return;
	}
	qdwin_toplevel_set_maximized(qdwin, tl, maximized != 0);
}

static void
qdwin_toplevel_set_maximized(struct qdwin *qdwin,
			     struct qdwin_toplevel *tl,
			     bool maximized)
{
	if (tl->nested_proxy_pending_decision) {
		weston_log("qdwin: set_maximized handle=%u ignored — "
			   "nested-proxy waiting for admin decision\n", tl->handle);
		return;
	}
	int want_max = maximized ? 1 : 0;
	int is_max   = (tl->state & QDWIN_TS_MAXIMIZED) ? 1 : 0;
	if (want_max == is_max) {
		weston_log("qdwin: set_maximized handle=%u max=%d noop\n",
			   tl->handle, want_max);
		return;
	}

	if (want_max) {
		struct weston_output *out = qdwin_primary_output(qdwin);
		if (!out) {
			weston_log("qdwin: set_maximized handle=%u: no output\n",
				   tl->handle);
			return;
		}
		/* v25: maximising a tiled window saves the *tiled* rect into the
		 * maximise slot (saved_*), so un-maximise returns to the tile.
		 * We deliberately do NOT clear tl->tiled / tile_saved_* here:
		 * those hold the pre-tile floating geometry, so a later
		 * request_tile(none) still restores the original float. (Only an
		 * interactive move floats the window and clears the tile.) */
		struct weston_coord_global pos =
			weston_view_get_pos_offset_global(tl->view);
		tl->saved_x = pos.c.x;
		tl->saved_y = pos.c.y;
		/* outer_width / outer_height are 0 until the first apply_inset
		 * call (attach_decoration / set_border_color don't seed them).
		 * If we save 0 here, restore() walks back to outer=0 → apply_
		 * inset's fallback branch picks up surface->width/height —
		 * which by then is the *maximised* size the client just
		 * committed, not the pre-max size. Result: "restore" leaves
		 * the window at maximised dimensions at the saved (top-left)
		 * position. Seed outer_* from the committed desktop geometry
		 * (rather than a CSD/shadow-inflated buffer size) so the
		 * saved-restore round-trip is symmetric. */
		qdwin_toplevel_seed_outer_from_committed(tl);
		tl->saved_outer_w = tl->outer_width;
		tl->saved_outer_h = tl->outer_height;

		/* §6.6 S1: maximise fills the work area (output minus panel
		 * exclusive zones), not the full output. */
		int wx, wy, ww, wh;
		qdwin_output_work_area(qdwin, out, &wx, &wy, &ww, &wh);
		tl->outer_width  = ww;
		tl->outer_height = wh;
		if (tl->is_nested_proxy) {
			/* Nested-proxy: resize the curtain via set_geometry.
			 * Inner client gets resized via S3 input flow. */
			qdwin_nested_proxy_set_geometry(tl, ww, wh);
		} else {
			weston_desktop_surface_set_maximized(tl->desktop_surface,
							    true);
		}
		/* Place the CONTENT view inside the work area, leaving room
		 * for the chrome (N titlebar, side borders). Without this
		 * offset chrome ends up at negative coords (off-screen) and
		 * the close/maximize/minimize glyphs paint above the visible
		 * area, making the maximised window indistinguishable from
		 * fullscreen. The (wx + inset_w, wy + inset_n) anchor keeps
		 * the titlebar at (wx, wy) — the top of the work area — so
		 * the panel stays visible at the bottom and the chrome stays
		 * visible at the top. */
		struct weston_coord_global origin = {
			.c = weston_coord(wx + tl->inset_w,
					  wy + tl->inset_n),
		};
		weston_view_set_position(tl->view, origin);
		qdwin_toplevel_apply_inset(tl);
		qdwin_toplevel_position_chrome(tl);

		tl->state |= QDWIN_TS_MAXIMIZED;
		weston_log("qdwin: set_maximized handle=%u max=1 outer=%dx%d at (%d,%d) "
			   "content_at=(%d,%d) saved=%dx%d@(%.0f,%.0f)\n",
			   tl->handle, tl->outer_width, tl->outer_height, wx, wy,
			   (int)origin.c.x, (int)origin.c.y,
			   tl->saved_outer_w, tl->saved_outer_h,
			   tl->saved_x, tl->saved_y);
	} else {
		if (tl->is_nested_proxy) {
			qdwin_nested_proxy_set_geometry(tl, tl->saved_outer_w,
							tl->saved_outer_h);
		} else {
			weston_desktop_surface_set_maximized(tl->desktop_surface,
							    false);
		}
		tl->outer_width  = tl->saved_outer_w;
		tl->outer_height = tl->saved_outer_h;
		struct weston_coord_global pos = {
			.c = weston_coord(tl->saved_x, tl->saved_y),
		};
		weston_view_set_position(tl->view, pos);
		qdwin_toplevel_apply_inset(tl);
		qdwin_toplevel_position_chrome(tl);

		tl->state &= ~QDWIN_TS_MAXIMIZED;
		weston_log("qdwin: set_maximized handle=%u max=0 restored=%dx%d@(%.0f,%.0f)\n",
			   tl->handle, tl->outer_width, tl->outer_height,
			   tl->saved_x, tl->saved_y);
	}
	qdwin_send_toplevel_state(qdwin, tl);
	weston_compositor_schedule_repaint(qdwin->compositor);
}

static void
qdwin_handle_request_raise(struct wl_client *client,
			   struct wl_resource *resource, uint32_t handle)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		weston_log("qdwin: request_raise unknown handle=%u\n", handle);
		return;
	}
	if (tl->nested_proxy_pending_decision) {
		weston_log("qdwin: request_raise handle=%u ignored — "
			   "nested-proxy waiting for admin decision\n", handle);
		return;
	}

	/* For now request_raise doubles as un-minimise. Stack-raise for
	 * non-minimised toplevels is a Phase 6.4/6.5 concern. */
	if (tl->state & QDWIN_TS_MINIMIZED) {
		tl->state &= ~QDWIN_TS_MINIMIZED;
		/* v24: restore to the correct layer for its workspace — onto
		 * normal only if it's on the active workspace, else keep it
		 * parked hidden (un-minimising a window on another workspace
		 * must not leak it onto the current desktop). */
		qdwin_toplevel_apply_workspace_visibility(tl);
		weston_log("qdwin: request_raise handle=%u unminimised (state=%#x)\n",
			   handle, tl->state);
		qdwin_send_toplevel_state(qdwin, tl);
		weston_compositor_schedule_repaint(qdwin->compositor);
		return;
	}
	/* Stack-raise: re-insert the view at the front of normal_layer's
	 * view list. weston_view_move_to_layer with the current layer
	 * re-stacks; the side-effect-free no-op had to wait for the
	 * shell client to evolve a real raise contract. Without this,
	 * Alt+Tab moves keyboard focus correctly but the raised window
	 * stays buried under the previously-focused one — user can type
	 * into a hidden window but only sees the typed text once the
	 * obscuring window is closed.
	 * v24: only restack on the active workspace; raising an
	 * off-workspace window leaves it parked (no workspace-follow). */
	if (tl->workspace == qdwin->active_workspace)
		qdwin_toplevel_move_to_layer(tl, &qdwin->normal_layer);
	else
		qdwin_toplevel_apply_workspace_visibility(tl);
	weston_log("qdwin: request_raise handle=%u re-stacked\n", handle);
	weston_compositor_schedule_repaint(qdwin->compositor);
}

/* v30: shell-owned set-position. The compositor-driven counterpart to
 * begin_interactive_move — the shell names the target outer-rect top-left
 * directly (WM policy), rather than the window following the pointer. Used by
 * the multi-machine viewer to place remote managed peers at deterministic
 * overlapping geometry. Floating toplevels only: maximised/fullscreen/tiled own
 * their geometry and are refused silently. Size is preserved. */
static void
qdwin_handle_request_set_position(struct wl_client *client,
				  struct wl_resource *resource,
				  uint32_t handle, int32_t x, int32_t y)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl || !tl->view) {
		weston_log("qdwin: request_set_position unknown handle=%u\n", handle);
		return;
	}
	if (tl->nested_proxy_pending_decision) {
		weston_log("qdwin: request_set_position handle=%u ignored — "
			   "nested-proxy waiting for admin decision\n", handle);
		return;
	}
	if (tl->state & (QDWIN_TS_MAXIMIZED | QDWIN_TS_FULLSCREEN)) {
		weston_log("qdwin: request_set_position handle=%u ignored — "
			   "maximised/fullscreen owns its geometry\n", handle);
		return;
	}
	if (tl->tiled != QDWIN_TILE_NONE) {
		weston_log("qdwin: request_set_position handle=%u ignored — tiled\n",
			   handle);
		return;
	}
	if (qdwin->move_grab_active && qdwin->move_grab_handle == handle) {
		weston_log("qdwin: request_set_position handle=%u ignored — "
			   "interactive move in progress\n", handle);
		return;
	}

	/* (x,y) is the GLOBAL outer-rect top-left (the coordinate space of the
	 * toplevel_geometry event). Clamp the top-left onto the output that
	 * CONTAINS it (falling back to the primary output) so the window stays
	 * reachable (the titlebar can't be parked fully off any output) and the
	 * clamp is correct even when an output has a nonzero global origin. The
	 * content view sits inset_w/inset_n inside the outer rect; size is
	 * preserved (never resized). */
	int cx = x, cy = y;
	struct weston_output *out = NULL, *o;
	wl_list_for_each(o, &qdwin->compositor->output_list, link) {
		if (x >= (int)o->pos.c.x && x < (int)o->pos.c.x + o->width &&
		    y >= (int)o->pos.c.y && y < (int)o->pos.c.y + o->height) {
			out = o;
			break;
		}
	}
	if (!out)
		out = qdwin_primary_output(qdwin);
	if (out) {
		int max_x = (int)out->pos.c.x + out->width - 1;
		int max_y = (int)out->pos.c.y + out->height - 1;
		if (cx < (int)out->pos.c.x) cx = (int)out->pos.c.x;
		if (cy < (int)out->pos.c.y) cy = (int)out->pos.c.y;
		if (cx > max_x) cx = max_x;
		if (cy > max_y) cy = max_y;
	}

	struct weston_coord_global p = {
		.c = weston_coord(cx + tl->inset_w, cy + tl->inset_n),
	};
	weston_view_set_position(tl->view, p);
	weston_view_update_transform(tl->view);
	qdwin_toplevel_position_chrome(tl);
	weston_compositor_schedule_repaint(qdwin->compositor);

	/* The shell's tracked geometry is now stale; send the authoritative
	 * new rectangle (same outer-rect convention as the move-grab end). */
	if (qdwin->shell_bound && qdwin->shell_resource) {
		qdwin_shell_v1_send_toplevel_geometry(
			qdwin->shell_resource, handle, cx, cy,
			(uint32_t)tl->last_width, (uint32_t)tl->last_height);
	}
	weston_log("qdwin: request_set_position handle=%u outer=(%d,%d) "
		   "size=%dx%d%s\n", handle, cx, cy, tl->last_width,
		   tl->last_height,
		   (cx != x || cy != y) ? " (clamped)" : "");
}

/* v32: force a full repaint of exactly one named physical/main output before
 * the shell queues a framebuffer capture. Invalid and PipeWire names are
 * deliberately silent no-ops: a typo must fail the capture job in qdshell,
 * not kill the security-critical shell protocol connection. */
static void
qdwin_handle_prepare_output_capture(struct wl_client *client,
				    struct wl_resource *resource,
				    const char *output_name)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct weston_output *output;
	(void)client;

	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (!qdwin_shell_capture_output_ok(output_name))
		return;

	wl_list_for_each(output, &qdwin->compositor->output_list, link) {
		if (!output->name || strcmp(output->name, output_name) != 0)
			continue;
		if (output->destroying)
			return;
		weston_output_damage(output);
		weston_log("qdwin: prepare_output_capture damaged output=%s\n",
			   output->name);
		return;
	}
}

static bool
qdwin_remote_output_name_valid(const char *name)
{
	const char *p;
	if (!name || strncmp(name, "rdp-", 4) != 0 || !name[4])
		return false;
	for (p = name + 4; *p; p++)
		if (*p < '0' || *p > '9')
			return false;
	return p - (name + 4) <= 3;
}

static bool
qdwin_set_remote_output_input(struct qdwin *qdwin, const char *output_name,
			      bool enabled)
{
	const struct qdwin_rdp_output_api_extended_v2 *api;
	struct weston_output *output;
	struct wl_list *lists[2] = {
		&qdwin->compositor->output_list,
		&qdwin->compositor->pending_output_list,
	};

	if (!qdwin_remote_output_name_valid(output_name))
		return false;
	api = qdwin_rdp_output_get_api(qdwin->compositor);
	if (!api || !api->output_set_input_enabled)
		return false;
	/* A disabled pre-created RDP slot lives on pending_output_list. Safety-off
	 * must remain idempotent there during cold-start and crash recovery; only
	 * searching active outputs made the daemon reject an already-safe disable. */
	for (int i = 0; i < 2; i++) {
		wl_list_for_each(output, lists[i], link) {
			if (output->name && strcmp(output->name, output_name) == 0)
				return api->output_set_input_enabled(output, enabled);
		}
	}
	return false;
}

/* v32: the authenticated display controller reaches this only through the
 * exclusively bound qdshell transaction. RDP starts fail-closed; disabling
 * also releases backend-held keys/buttons. */
static void
qdwin_handle_set_remote_output_input(struct wl_client *client,
				     struct wl_resource *resource,
				     const char *output_name,
				     uint32_t enabled)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	bool applied;
	(void)client;

	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (wl_resource_get_version(resource) < 34) {
		wl_resource_post_error(resource, WL_DISPLAY_ERROR_INVALID_METHOD,
				       "remote output requests require qdwin shell v34");
		return;
	}
	if (enabled > 1 || !qdwin_remote_output_name_valid(output_name)) {
		weston_log("qdwin: remote input gate rejected invalid request\n");
		return;
	}
	/* Safety-off remains available while locked; safety-on does not. */
	if (enabled && qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	applied = qdwin_set_remote_output_input(
		qdwin, output_name, enabled != 0);
	weston_log("qdwin: remote input gate output=%s enabled=%u result=%s\n",
		   output_name, enabled, applied ? "applied" : "rejected");
	qdwin_shell_v1_send_remote_output_input_result(
		resource, output_name, enabled, applied ? 1u : 0u);
}

/* v33: run the R8 cancellation half before carrier close. This is deliberately
 * separate from output removal so transfer state is gone while the peer is
 * still connected and the controller can wait for an authoritative result. */
static void
qdwin_handle_drain_remote_output_state(struct wl_client *client,
				       struct wl_resource *resource,
				       const char *output_name)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct weston_output *output;
	struct wl_list *lists[2] = {
		&qdwin->compositor->output_list,
		&qdwin->compositor->pending_output_list,
	};
	bool applied = false;
	(void)client;

	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (wl_resource_get_version(resource) < 34) {
		wl_resource_post_error(resource, WL_DISPLAY_ERROR_INVALID_METHOD,
				       "remote output requests require qdwin shell v34");
		return;
	}
	if (!qdwin_remote_output_name_valid(output_name)) {
		weston_log("qdwin: remote output drain rejected invalid request\n");
		return;
	}
	for (int i = 0; i < 2 && !applied; i++) {
		wl_list_for_each(output, lists[i], link) {
			if (!output->name || strcmp(output->name, output_name) != 0)
				continue;
			qdwin_output_boundary_cancel_state(qdwin, output);
			applied = true;
			break;
		}
	}
	weston_log("qdwin: remote output drain output=%s result=%s\n",
		   output_name, applied ? "applied" : "rejected");
	qdwin_shell_v1_send_remote_output_drain_result(
		resource, output_name, applied ? 1u : 0u);
}

/* ------------------------------------------------------------------
 * Titlebar move-drag grab.
 *
 * Activated by the shell calling begin_interactive_move(handle,
 * serial) in response to a press on a chrome side. While the grab is
 * active, pointer motion translates the toplevel's content view + all
 * attached chrome views by the same delta; any button release ends
 * the grab.
 *
 * No focus changes during drag, no wl_pointer.motion forwarded to the
 * surface under the cursor (avoids phantom hover on other windows
 * while the user is mid-drag).
 * ------------------------------------------------------------------ */

static void
qdwin_move_grab_focus(struct weston_pointer_grab *grab)
{
	(void)grab;
}

static void
qdwin_move_grab_motion(struct weston_pointer_grab *grab,
		       const struct weston_pointer_motion_event *event)
{
	/* J29: 14->16 dropped `time` param (folded into event->base.ts). */
	struct weston_pointer *pointer = grab->pointer;
	weston_pointer_move(pointer, event);

	struct qdwin *qd = wl_container_of(grab, qd, move_grab);
	if (!qd->move_grab_active)
		return;
	struct qdwin_toplevel *tl =
		qdwin_toplevel_from_handle(qd, qd->move_grab_handle);
	if (!tl || !tl->view)
		return;

	double nx = pointer->pos.c.x - qd->move_grab_anchor_dx;
	double ny = pointer->pos.c.y - qd->move_grab_anchor_dy;
	/* v25: edge snapping (no-op unless wm_policy.snap_enabled). Snaps
	 * the dragged window's outer rect to work-area + neighbour edges. */
	qdwin_snap_move_position(qd, tl, &nx, &ny);
	struct weston_coord_global p = { .c = weston_coord(nx, ny) };
	weston_view_set_position(tl->view, p);
	weston_view_update_transform(tl->view);
	qdwin_toplevel_position_chrome(tl);
	weston_compositor_schedule_repaint(qd->compositor);
}

static void
qdwin_move_grab_button(struct weston_pointer_grab *grab,
		       const struct weston_pointer_button_event *button_event)
{
	/* J29: 14->16 button/state now come from the event struct. */
	uint32_t state = button_event->button_state;
	struct weston_pointer *pointer = grab->pointer;
	struct qdwin *qd = wl_container_of(grab, qd, move_grab);

	/* End on any button release. The protocol expects the user to
	 * release the same button that initiated the drag, but we don't
	 * gate on which button — clients sometimes lose the original
	 * press serial across re-grabs and ending defensively is safer. */
	if (state == WL_POINTER_BUTTON_STATE_RELEASED) {
		if (qd->move_grab_active) {
			uint32_t handle = qd->move_grab_handle;
			struct qdwin_toplevel *tl =
				qdwin_toplevel_from_handle(qd, handle);
			weston_log("qdwin: end_interactive_move handle=%u\n",
				   handle);
			qd->move_grab_active = 0;
			weston_pointer_end_grab(pointer);
			/* Tell the shell where the toplevel ended up so it
			 * can update its tracked geometry; without this
			 * tl.x/tl.y stay frozen at the pre-drag position. */
			if (tl && tl->view && qd->shell_bound &&
			    qd->shell_resource) {
				struct weston_coord_global op =
					weston_view_get_pos_offset_global(tl->view);
				qdwin_shell_v1_send_toplevel_geometry(
					qd->shell_resource, handle,
					(int)(op.c.x - tl->inset_w),
					(int)(op.c.y - tl->inset_n),
					(uint32_t)tl->last_width,
					(uint32_t)tl->last_height);
			}
		}
	}
}

static void
qdwin_move_grab_axis(struct weston_pointer_grab *grab,
		     const struct weston_pointer_axis_event *event)
{
	/* J29: 14->16 dropped `time` param. */
	(void)grab; (void)event;
}

static void
qdwin_move_grab_axis_source(struct weston_pointer_grab *grab, uint32_t source)
{
	(void)grab; (void)source;
}

static void
qdwin_move_grab_frame(struct weston_pointer_grab *grab)
{
	(void)grab;
}

static void
qdwin_move_grab_cancel(struct weston_pointer_grab *grab)
{
	struct qdwin *qd = wl_container_of(grab, qd, move_grab);
	qd->move_grab_active = 0;
}

static const struct weston_pointer_grab_interface qdwin_move_grab_iface = {
	qdwin_move_grab_focus,
	qdwin_move_grab_motion,
	qdwin_move_grab_button,
	qdwin_move_grab_axis,
	qdwin_move_grab_axis_source,
	qdwin_move_grab_frame,
	qdwin_move_grab_cancel,
};

/* End any active move-drag targeting `handle`. Called from
 * qdwin_surface_removed and from shell unbind so a destroyed toplevel
 * (or a vanished shell) doesn't leave a dangling grab. */
static void
qdwin_move_grab_end_for(struct qdwin *qdwin, uint32_t handle)
{
	if (!qdwin->move_grab_active)
		return;
	if (qdwin->move_grab_handle != handle)
		return;
	weston_log("qdwin: cancelling move-drag handle=%u "
		   "(toplevel/shell gone)\n", handle);
	qdwin->move_grab_active = 0;
	if (qdwin->move_grab.pointer)
		weston_pointer_end_grab(qdwin->move_grab.pointer);
}

static void
qdwin_handle_begin_interactive_move(struct wl_client *client,
				    struct wl_resource *resource,
				    uint32_t handle, uint32_t serial)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client; (void)serial;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}

	/* One drag at a time. Silently drop a second begin request. */
	if (qdwin->move_grab_active) {
		weston_log("qdwin: begin_interactive_move handle=%u "
			   "ignored — drag already active on handle=%u\n",
			   handle, qdwin->move_grab_handle);
		return;
	}

	struct qdwin_toplevel *tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl || !tl->view) {
		weston_log("qdwin: begin_interactive_move handle=%u "
			   "ignored — no such toplevel\n", handle);
		return;
	}
	if (tl->nested_proxy_pending_decision) {
		weston_log("qdwin: begin_interactive_move handle=%u ignored — "
			   "nested-proxy waiting for admin decision\n", handle);
		return;
	}

	/* Per XML: maximised / fullscreen refuse silently. */
	if (tl->state & (QDWIN_TS_MAXIMIZED | QDWIN_TS_FULLSCREEN)) {
		weston_log("qdwin: begin_interactive_move handle=%u "
			   "ignored — toplevel is maximised/fullscreen\n",
			   handle);
		return;
	}

	/* v25: dragging a tiled window floats it — clear the tiled flag so a
	 * later request_tile(none) doesn't snap it back to stale geometry. */
	qdwin_toplevel_clear_tiled(tl);

	/* Find a seat with an active pointer + start the grab on it.
	 * MVP: first seat wins. Multi-seat drag is uncommon and adds
	 * complexity that nothing currently exercises. */
	struct weston_seat *seat;
	struct weston_pointer *pointer = NULL;
	wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
		pointer = weston_seat_get_pointer(seat);
		if (pointer)
			break;
	}
	if (!pointer) {
		weston_log("qdwin: begin_interactive_move handle=%u "
			   "ignored — no pointer on any seat\n", handle);
		return;
	}

	struct weston_coord_global origin =
		weston_view_get_pos_offset_global(tl->view);
	qdwin->move_grab_handle = handle;
	qdwin->move_grab_anchor_dx = pointer->pos.c.x - origin.c.x;
	qdwin->move_grab_anchor_dy = pointer->pos.c.y - origin.c.y;
	qdwin->move_grab.interface = &qdwin_move_grab_iface;
	qdwin->move_grab_active = 1;
	weston_pointer_start_grab(pointer, &qdwin->move_grab);
	weston_log("qdwin: begin_interactive_move handle=%u anchor=(%.1f,%.1f)\n",
		   handle,
		   qdwin->move_grab_anchor_dx,
		   qdwin->move_grab_anchor_dy);
}

static void
qdwin_handle_begin_interactive_resize(struct wl_client *client,
				      struct wl_resource *resource,
				      uint32_t handle, uint32_t serial,
				      uint32_t edges)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	weston_log("qdwin: begin_interactive_resize handle=%u serial=%u edges=%#x (stub)\n",
		   handle, serial, edges);
}

/* ------------------------------------------------------------------
 * Popups (§6.4).
 *
 * A qdwin_popup is one shell-owned wl_surface placed on popup_layer
 * as a weston_view. The shell is responsible for painting; qdwin
 * handles placement, stacking, and teardown.
 *
 * S5: while a popup is up, qdwin owns a seat pointer grab. Button
 * presses inside the popup bbox pass through to the shell (so menu
 * items still get clicked); presses outside fire `dismissed` and
 * tear the popup down. Motion/axis/frame pass through so hover
 * highlighting on the popup surface keeps working.
 * ------------------------------------------------------------------ */

static void qdwin_popup_teardown(struct qdwin_popup *p);

static int
qdwin_popup_bbox_contains(struct qdwin_popup *p,
			  struct weston_coord_global pos)
{
	if (!p->view || !p->surface)
		return 0;
	struct weston_coord_global vp =
		weston_view_get_pos_offset_global(p->view);
	int w = p->surface->width;
	int h = p->surface->height;
	return pos.c.x >= vp.c.x && pos.c.x < vp.c.x + w &&
	       pos.c.y >= vp.c.y && pos.c.y < vp.c.y + h;
}

static void
qdwin_popup_grab_focus(struct weston_pointer_grab *grab)
{
	(void)grab;
}

static void
qdwin_popup_grab_motion(struct weston_pointer_grab *grab,
			const struct weston_pointer_motion_event *event)
{
	/* J29: 14->16 dropped `time` param. */
	struct weston_pointer *pointer = grab->pointer;
	weston_pointer_move(pointer, event);

	/* Re-pick focus so the popup surface receives enter/motion
	 * while the grab is active — hover highlighting on the menu
	 * depends on it. */
	struct weston_view *view = weston_compositor_pick_view(
		pointer->seat->compositor, pointer->pos);
	if (view != pointer->focus)
		weston_pointer_set_focus(pointer, view);

	/* J29: 14->16 send_motion dropped `time` (now in event->base.ts). */
	weston_pointer_send_motion(pointer, event);
}

static void
qdwin_popup_grab_button(struct weston_pointer_grab *grab,
			const struct weston_pointer_button_event *button_event)
{
	/* J29: 14->16 button/state now come from the event struct. */
	uint32_t button = button_event->button;
	uint32_t state = button_event->button_state;
	struct qdwin_popup *p = wl_container_of(grab, p, grab);
	struct weston_pointer *pointer = grab->pointer;

	if (state == WL_POINTER_BUTTON_STATE_PRESSED &&
	    !qdwin_popup_bbox_contains(p, pointer->pos)) {
		weston_log("qdwin: popup dismissed by outside click "
			   "at (%.0f,%.0f)\n",
			   pointer->pos.c.x, pointer->pos.c.y);
		/* Don't free `p` synchronously from inside the grab
		 * callback — libweston may still dereference the grab
		 * pointer after we return. End the grab first so
		 * pointer->grab is rebound to the default grab, then
		 * notify the client; qdshell destroys the qdwin_popup_v1
		 * proxy in response, which triggers teardown through
		 * qdwin_popup_resource_destroyed on the next dispatch. */
		if (p->grab_active) {
			weston_pointer_end_grab(pointer);
			p->grab_active = 0;
		}
		if (p->resource)
			qdwin_popup_v1_send_dismissed(p->resource);
		return;
	}
	/* J29: 14->16 send_button now takes the event struct. */
	weston_pointer_send_button(pointer, button_event);

	/* qdwin_shell_v1@v21 popup_button — forward the click to the
	 * shell as a typed event with popup-local coords. Same fix as
	 * chrome_button: weston_pointer_send_button targets the shell's
	 * own popup wl_surface, but libweston's same-client suppression
	 * means qdshell never sees the wl_pointer.button event there,
	 * making popup menu items inert. We bypass it by delivering a
	 * shell-protocol event the shell binding does receive. */
	struct qdwin *qd = (p->parent && p->parent->qdwin) ?
		p->parent->qdwin : NULL;
	if (qd && qd->shell_resource && p->parent && p->view &&
	    wl_resource_get_version(qd->shell_resource) >= 21 &&
	    qdwin_popup_bbox_contains(p, pointer->pos)) {
		struct weston_coord_global vp =
			weston_view_get_pos_offset_global(p->view);
		double sx = pointer->pos.c.x - vp.c.x;
		double sy = pointer->pos.c.y - vp.c.y;
		qdwin_shell_v1_send_popup_button(
			qd->shell_resource,
			p->parent->handle,
			wl_fixed_from_double(sx),
			wl_fixed_from_double(sy),
			button,
			state,
			/* v29: the grab serial for this button (see
			 * chrome_button: wl_display_get_serial == what
			 * notify_button stores into grab_serial after this
			 * callback returns, NOT the stale pointer->grab_serial).
			 * Echo to show_popup to open a submenu. */
			wl_display_get_serial(
				pointer->seat->compositor->wl_display));
	}
}

static void
qdwin_popup_grab_axis(struct weston_pointer_grab *grab,
		      const struct weston_pointer_axis_event *event)
{
	/* J29: 14->16 dropped `time`; send_axis takes (pointer, event). */
	weston_pointer_send_axis(grab->pointer, event);
}

static void
qdwin_popup_grab_axis_source(struct weston_pointer_grab *grab, uint32_t source)
{
	weston_pointer_send_axis_source(grab->pointer, source);
}

static void
qdwin_popup_grab_frame(struct weston_pointer_grab *grab)
{
	weston_pointer_send_frame(grab->pointer);
}

static void
qdwin_popup_grab_cancel(struct weston_pointer_grab *grab)
{
	struct qdwin_popup *p = wl_container_of(grab, p, grab);
	/* libweston has cancelled the grab; mark it inactive so teardown
	 * doesn't try to end it again. Defer actual teardown to the
	 * client resource-destroy path (same reasoning as button). */
	p->grab_active = 0;
	if (p->resource)
		qdwin_popup_v1_send_dismissed(p->resource);
}

/* ------------------------------------------------------------------
 * §6.8 S3b — default-pointer-grab override for nested-proxy input
 * forwarding.
 *
 * `weston_compositor_set_default_pointer_grab` lets a shell replace
 * the compositor-wide default grab interface with its own. Our
 * interface delegates fully to the standard delivery helpers so
 * normal client wl_pointer routing keeps working, AND additionally
 * encodes events into QDNI packets when the focused view is a
 * nested-proxy. Other grabs (popup, view-stream) still take over
 * via `weston_pointer_start_grab`; when they `end_grab`, weston
 * restores the default — i.e. our interface — so we never need to
 * re-arm.
 *
 * Single-process singleton: qdwin owns this; lookup via the
 * `qdwin_singleton` static (one shell per weston instance). The
 * grab struct itself is owned by libweston, so we can't attach
 * per-grab state.
 * ------------------------------------------------------------------ */

static struct qdwin *qdwin_singleton = NULL;  /* set in wet_shell_init */

static struct qdwin_toplevel *
qdwin_proxy_for_view(struct qdwin *qdwin, struct weston_view *v)
{
	if (!qdwin || !v)
		return NULL;
	struct qdwin_toplevel *tl;
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		if (tl->is_nested_proxy && !tl->proxy_destroying &&
		    tl->view == v)
			return tl;
	}
	return NULL;
}

static uint32_t
qdwin_proxy_time_msec(const struct timespec *ts)
{
	if (!ts) return 0;
	return (uint32_t)(ts->tv_sec * 1000u +
			  (uint32_t)(ts->tv_nsec / 1000000L));
}

/* Update qdwin->active_input_proxy to match the pointer's current
 * focus. Sends focus_leave on the previous proxy, focus_enter on the
 * new one. No-op when the active proxy is unchanged. */
static void
qdwin_proxy_pointer_track_focus(struct qdwin *qdwin,
				struct weston_pointer *pointer)
{
	/* Callers pass qdwin_singleton, which is NULL before init completes
	 * and is reset to NULL on an init-failure teardown while the default
	 * pointer grab is still installed. Guard so a stray grab callback in
	 * that window is a no-op rather than a NULL deref. */
	if (!qdwin)
		return;
	struct qdwin_toplevel *new_proxy =
		qdwin_proxy_for_view(qdwin, pointer->focus);
	if (new_proxy == qdwin->active_input_proxy)
		return;
	struct qdwin_toplevel *old = qdwin->active_input_proxy;
	if (old && old->proxy_input_sink_fd >= 0)
		qdwin_nested_input_sink_send_focus(old->proxy_input_sink_fd, 0);
	qdwin->active_input_proxy = new_proxy;
	if (new_proxy && new_proxy->proxy_input_sink_fd >= 0)
		qdwin_nested_input_sink_send_focus(
			new_proxy->proxy_input_sink_fd, 1);
}

static void
qdwin_proxy_default_grab_focus(struct weston_pointer_grab *grab)
{
	struct weston_pointer *pointer = grab->pointer;
	if (qdwin_singleton && qdwin_singleton->locked) {
		struct weston_view *lv = qdwin_singleton->lock_view;
		if (pointer->focus != lv)
			weston_pointer_set_focus(pointer, lv);
		return;
	}
	struct weston_view *view = weston_compositor_pick_view(
		pointer->seat->compositor, pointer->pos);
	if (view != pointer->focus)
		weston_pointer_set_focus(pointer, view);
	qdwin_proxy_pointer_track_focus(qdwin_singleton, pointer);
}

static void
qdwin_proxy_default_grab_motion(struct weston_pointer_grab *grab,
				const struct weston_pointer_motion_event *event)
{
	/* J29: 14->16 dropped `time` param; read it from the event struct. */
	const struct timespec *time = &event->base.ts;
	struct weston_pointer *pointer = grab->pointer;
	qdwin_idle_note_activity(qdwin_singleton);
	weston_pointer_move(pointer, event);
	if (qdwin_singleton && qdwin_singleton->locked) {
		struct weston_view *lv = qdwin_singleton->lock_view;
		if (pointer->focus != lv)
			weston_pointer_set_focus(pointer, lv);
		weston_pointer_send_motion(pointer, event);
		return;
	}
	struct weston_view *view = weston_compositor_pick_view(
		pointer->seat->compositor, pointer->pos);
	/* plan3 post-review: override the picked view with the layer-shell
	 * view ONLY when the picker resolved to a cursor sprite (a view
	 * whose surface holds the wl_pointer-cursor role). With the M2
	 * commit listener keeping cursor input regions empty, the picker
	 * should already skip cursor sprites — but if a sprite leaks a
	 * non-empty input region mid-commit, this override prevents the
	 * focus-on-cursor regression. Avoid unconditional override: the
	 * SIGABRT trace observed on the daily VM was through this exact
	 * code path, and forcing a focus change to a layer view that the
	 * picker didn't pick can hit weston_view_set_position_with_offset
	 * assertions when the layer view is mid-commit. */
	if (view && view->surface) {
		const char *role = weston_surface_get_role(view->surface);
		if (role && strcmp(role, "wl_pointer-cursor") == 0) {
			struct weston_view *layer_view =
				qdwin_layer_surface_view_at_pos(qdwin_singleton,
								 pointer->pos);
			if (layer_view)
				view = layer_view;
		}
	}
	if (view != pointer->focus)
		weston_pointer_set_focus(pointer, view);
	qdwin_proxy_pointer_track_focus(qdwin_singleton, pointer);
	/* B6: belt-and-braces — if the pointer has no sprite (no client
	 * owns the cursor over the focused area), install our default. */
	if (!pointer->sprite && qdwin_singleton)
		qdwin_install_default_cursor_on_pointer(qdwin_singleton, pointer);
	weston_pointer_send_motion(pointer, event);

	/* v25: focus-follows-mouse. Retarget keyboard focus to the toplevel
	 * under the pointer (immediately or after the settle delay) when the
	 * policy is follow_mouse; a no-op under click-to-focus. */
	if (qdwin_singleton)
		qdwin_ffm_consider(qdwin_singleton, pointer);

	struct qdwin_toplevel *tl = qdwin_singleton ?
		qdwin_singleton->active_input_proxy : NULL;
	if (tl && tl->view && tl->view->surface &&
	    tl->proxy_input_sink_fd >= 0) {
		weston_view_update_transform(tl->view);
		struct weston_coord_global gpos = pointer->pos;
		struct weston_coord_surface cs =
			weston_coord_global_to_surface(tl->view, gpos);
		qdwin_nested_input_sink_send_motion(
			tl->proxy_input_sink_fd,
			qdwin_proxy_time_msec(time),
			wl_fixed_from_double(cs.c.x),
			wl_fixed_from_double(cs.c.y));
	}
}

/* Hit-test a global pointer position against every chrome side of every
 * toplevel and return the first match (ordered by toplevel iteration —
 * with z-stack mattering little for chrome since each toplevel's chrome
 * is co-located with its content).
 *
 * Why this exists: weston_compositor_pick_view returns the cursor
 * sprite view (role=wl_pointer-cursor) when the cursor is composited
 * into the framebuffer (pixman renderer + cursor-sprites helper, the
 * post-task(135) configuration). pointer->focus is therefore the
 * cursor view, not the chrome surface underneath, breaking any
 * click handling that relies on pointer->focus. Doing the hit-test
 * ourselves against tracked chrome views sidesteps the picker and
 * removes the dependency on whatever weston's view-ordering happens
 * to be after a cursor-set transition. */
static struct qdwin_toplevel *
qdwin_chrome_at_pos(struct qdwin *qdwin, struct weston_coord_global pos,
		    int *side_out, double *sx_out, double *sy_out)
{
	if (!qdwin)
		return NULL;
	struct qdwin_toplevel *tl;
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		if (tl->nested_proxy_pending_decision)
			continue;
		for (int s = 0; s < QDWIN_SIDES; s++) {
			struct weston_view *cv = tl->chrome[s].view;
			struct weston_surface *cs = tl->chrome[s].surface;
			if (!cv || !cs || cs->width <= 0 || cs->height <= 0)
				continue;
			weston_view_update_transform(cv);
			struct weston_coord_global vp =
				weston_view_get_pos_offset_global(cv);
			double x0 = vp.c.x, y0 = vp.c.y;
			double x1 = x0 + cs->width, y1 = y0 + cs->height;
			if (pos.c.x < x0 || pos.c.x >= x1 ||
			    pos.c.y < y0 || pos.c.y >= y1)
				continue;
			if (side_out) *side_out = s;
			if (sx_out) *sx_out = pos.c.x - x0;
			if (sy_out) *sy_out = pos.c.y - y0;
			return tl;
		}
	}
	return NULL;
}

/* Same as qdwin_chrome_at_pos but matches the content view too. Returns
 * the toplevel whose content OR chrome bbox contains `pos`. Used by
 * click-to-focus (it doesn't care which side, only which toplevel). */
static struct qdwin_toplevel *
qdwin_toplevel_at_pos(struct qdwin *qdwin, struct weston_coord_global pos)
{
	if (!qdwin)
		return NULL;
	int dummy;
	struct qdwin_toplevel *tl =
		qdwin_chrome_at_pos(qdwin, pos, &dummy, NULL, NULL);
	if (tl)
		return tl;
	/* Stacking-aware hit test (rung-1 A4 fix): `qdwin->toplevels` is TRACKING
	 * order, NOT z-order — returning the first list match gives whichever
	 * toplevel was created/tracked earliest, so in an OVERLAP a lower window
	 * steals the click and `request_raise` has no effect on pointer routing
	 * (the click-to-raise/focus target below is derived from this lookup).
	 * Walk the normal_layer view list FRONT-TO-BACK (topmost first) and return
	 * the first mapped toplevel whose content rect contains the point, so the
	 * raised/topmost peer wins pointer hit-testing. Minimised/held toplevels
	 * are not in normal_layer, so they are correctly never hit. */
	struct weston_view *v;
	wl_list_for_each(v, &qdwin->normal_layer.view_list.link, layer_link.link) {
		struct qdwin_toplevel *cand = NULL, *t;
		wl_list_for_each(t, &qdwin->toplevels, link) {
			if (t->view == v) { cand = t; break; }
		}
		if (!cand || cand->nested_proxy_pending_decision)
			continue;
		struct weston_surface *cs = v->surface;
		if (!cs || cs->width <= 0 || cs->height <= 0)
			continue;
		weston_view_update_transform(v);
		struct weston_coord_global vp =
			weston_view_get_pos_offset_global(v);
		if (pos.c.x >= vp.c.x &&
		    pos.c.x < vp.c.x + cs->width &&
		    pos.c.y >= vp.c.y &&
		    pos.c.y < vp.c.y + cs->height)
			return cand;
	}
	return NULL;
}

static void
qdwin_proxy_default_grab_button(struct weston_pointer_grab *grab,
				const struct weston_pointer_button_event *button_event)
{
	/* J29: 14->16 time/button/state now come from the event struct. */
	const struct timespec *time = &button_event->base.ts;
	uint32_t button = button_event->button;
	uint32_t state = button_event->button_state;
	struct weston_pointer *pointer = grab->pointer;
	qdwin_idle_note_activity(qdwin_singleton);
	if (qdwin_singleton && qdwin_singleton->locked) {
		struct weston_view *lv = qdwin_singleton->lock_view;
		if (pointer->focus != lv)
			weston_pointer_set_focus(pointer, lv);
		weston_pointer_send_button(pointer, button_event);
		return;
	}
	struct qdwin_layer_surface *layer_surface =
		qdwin_layer_surface_at_pos(qdwin_singleton, pointer->pos);
	struct weston_view *layer_view =
		qdwin_layer_surface_view_at_pos(qdwin_singleton, pointer->pos);
	int layer_blocks_toplevel_focus =
		qdwin_layer_surface_blocks_toplevel_focus(layer_surface);
	if (layer_blocks_toplevel_focus && layer_view &&
	    pointer->focus != layer_view)
		weston_pointer_set_focus(pointer, layer_view);

	/* plan3 H1 (post-deep-review): a press that landed on a layer-shell
	 * surface is governed by that surface's keyboard-interactivity (NONE
	 * leaves focus alone, ON_DEMAND transfers, EXCLUSIVE was already
	 * granted at map time). The toplevel click-to-focus block below
	 * would otherwise steal keyboard focus to the toplevel behind the
	 * layer surface — that broke Quickshell text fields whose layer
	 * panel overlaps a window. */
	int press_on_layer =
		(state == WL_POINTER_BUTTON_STATE_PRESSED && button == BTN_LEFT &&
		 layer_view != NULL && layer_blocks_toplevel_focus);

	/* plan3 M4: zwlr_layer_surface_v1 ON_DEMAND keyboard interactivity.
	 * On left-button press over a layer-shell surface that requested
	 * ON_DEMAND, transfer keyboard focus to that surface so that text
	 * fields inside Quickshell popups can receive input. The helper
	 * is defined later (the qdwin_layer_surface struct is incomplete
	 * here) — see qdwin_layer_surface_handle_on_demand_button. */
	if (state == WL_POINTER_BUTTON_STATE_PRESSED && button == BTN_LEFT)
		qdwin_layer_surface_handle_on_demand_button(qdwin_singleton,
							    pointer);
	/* Click-to-focus: on left-button press, raise + focus the toplevel
	 * under the cursor (whether the pointer is on its content surface
	 * or on a chrome side). Without this, clicking a background window
	 * delivers the button event but doesn't change z-order or
	 * keyboard focus — the user can't pick a window with the mouse.
	 *
	 * Done BEFORE delivering the button so the click also lands on
	 * the now-focused window in the same dispatch (matches gnome /
	 * kwin / kiosk-shell behaviour).
	 *
	 * Skip this block when the press landed on a layer-shell view —
	 * layer-shell focus is owned by the layer-shell interactivity mode,
	 * not by click-to-raise. See press_on_layer above. */
	if (state == WL_POINTER_BUTTON_STATE_PRESSED && button == BTN_LEFT &&
	    !press_on_layer && qdwin_singleton) {
		/* Look up the toplevel under the pointer by position rather
		 * than by pointer->focus — see qdwin_chrome_at_pos comment
		 * for why pointer->focus is unreliable when the cursor is
		 * composited into the framebuffer. */
		struct qdwin_toplevel *tl_under =
			qdwin_toplevel_at_pos(qdwin_singleton, pointer->pos);
		/* Diagnostic: log the pointer position and resolved toplevel
		 * handle so the journal reveals coord-vs-pick on click-to-focus
		 * (whether the click coord missed the window or the pick
		 * returned NULL/the wrong toplevel). */
		weston_log("qdwin: click-focus pick pos=%d,%d -> handle=%u\n",
			   (int)pointer->pos.c.x, (int)pointer->pos.c.y,
			   tl_under ? tl_under->handle : 0xffffffffu);
		if (tl_under) {
			/* Raise on the normal layer (re-stack to top). v25:
			 * gated on the raise-on-click policy — focus still
			 * transfers below, but the z-order only changes when
			 * raise_on_click is set (default on). */
			if (qdwin_singleton->wm_policy.raise_on_click)
				qdwin_toplevel_move_to_layer(tl_under,
						     &qdwin_singleton->normal_layer);
			/* Move keyboard focus if it isn't already here. */
			struct weston_keyboard *kb =
				weston_seat_get_keyboard(pointer->seat);
			struct weston_surface *content = tl_under->view ?
				tl_under->view->surface : NULL;
			if (tl_under->view && pointer->focus != tl_under->view)
				weston_pointer_set_focus(pointer,
							 tl_under->view);
			if (kb && content && kb->focus != content) {
				weston_keyboard_set_focus(kb, content);
				/* Tell the shell so it can update its
				 * internal seat-focus state. The emit
				 * helper self-checks shell version. */
				qdwin_emit_seat_focus_changed(
					qdwin_singleton, pointer->seat,
					tl_under->handle);
			}
			weston_compositor_schedule_repaint(
				qdwin_singleton->compositor);
		}
	}
	/* J29: 14->16 send_button now takes the event struct. */
	weston_pointer_send_button(pointer, button_event);

	/* qdwin_shell_v1@v20 chrome_button — forward button events on
	 * shell-owned chrome surfaces to the shell directly, bypassing two
	 * libweston-side gotchas:
	 *
	 * (1) The same-client suppression: pointer events libweston tries
	 *     to deliver to surfaces owned by the shell client are not
	 *     reliably routed through wl_pointer (see
	 *     ), so qdshell never sees the
	 *     wl_pointer.button event for clicks on its own chrome.
	 *
	 * (2) The cursor-sprite picker shadow: with pixman renderer +
	 *     post-task(135) cursor-sprites helper, weston composites the
	 *     cursor into the primary framebuffer as a regular weston_view
	 *     and weston_compositor_pick_view returns it as the topmost
	 *     view at the pointer position. So pointer->focus is the
	 *     cursor sprite (role=wl_pointer-cursor), not the chrome
	 *     underneath — using pointer->focus to look up the chrome
	 *     toplevel here would miss every click. We hit-test against
	 *     tracked chrome bboxes directly instead.
	 *
	 * Without this whole path, clicks on the close / maximize /
	 * minimize glyphs that qdshell paints on the titlebar are silently
	 * dropped; see the chrome_button XML event docs for the contract.
	 */
	if (qdwin_singleton && qdwin_singleton->shell_resource &&
	    wl_resource_get_version(qdwin_singleton->shell_resource) >= 20) {
		int side = -1;
		double sx = 0, sy = 0;
		struct qdwin_toplevel *chrome_tl =
			qdwin_chrome_at_pos(qdwin_singleton, pointer->pos,
					    &side, &sx, &sy);
		if (chrome_tl && side >= 0) {
			qdwin_shell_v1_send_chrome_button(
				qdwin_singleton->shell_resource,
				chrome_tl->handle,
				(uint32_t)side,
				wl_fixed_from_double(sx),
				wl_fixed_from_double(sy),
				button,
				state,
				/* v29: the grab serial for this button. NOT
				 * pointer->grab_serial — that is still the
				 * PREVIOUS press here, because notify_button()
				 * assigns grab_serial only AFTER this grab
				 * callback returns (input.c). wl_display_get_serial
				 * is the serial just used for the wl_pointer.button
				 * send above, and is exactly what notify_button
				 * will store into grab_serial (nothing between
				 * allocates a serial), so the shell can echo it to
				 * show_popup. Meaningful on press. */
				wl_display_get_serial(
					pointer->seat->compositor->wl_display));
		}
	}

	struct qdwin_toplevel *tl = qdwin_singleton ?
		qdwin_singleton->active_input_proxy : NULL;
	if (tl && tl->proxy_input_sink_fd >= 0)
		qdwin_nested_input_sink_send_button(
			tl->proxy_input_sink_fd,
			qdwin_proxy_time_msec(time), button, state);
}

static void
qdwin_proxy_default_grab_axis(struct weston_pointer_grab *grab,
			      const struct weston_pointer_axis_event *event)
{
	/* J29: 14->16 dropped `time` param; read it from the event struct. */
	const struct timespec *time = &event->base.ts;
	qdwin_idle_note_activity(qdwin_singleton);
	if (qdwin_singleton && qdwin_singleton->locked) {
		weston_pointer_send_axis(grab->pointer, event);
		return;
	}
	weston_pointer_send_axis(grab->pointer, event);
	struct qdwin_toplevel *tl = qdwin_singleton ?
		qdwin_singleton->active_input_proxy : NULL;
	if (tl && tl->proxy_input_sink_fd >= 0)
		qdwin_nested_input_sink_send_axis(
			tl->proxy_input_sink_fd,
			qdwin_proxy_time_msec(time),
			event->axis,
			wl_fixed_from_double(event->value));
}

static void
qdwin_proxy_default_grab_axis_source(struct weston_pointer_grab *grab,
				     uint32_t source)
{
	weston_pointer_send_axis_source(grab->pointer, source);
}

static void
qdwin_proxy_default_grab_frame(struct weston_pointer_grab *grab)
{
	weston_pointer_send_frame(grab->pointer);
}

static void
qdwin_proxy_default_grab_cancel(struct weston_pointer_grab *grab)
{
	(void)grab;
	/* libweston cancels the default grab when shutting down; nothing
	 * to free since we own no per-grab state. */
}

static const struct weston_pointer_grab_interface
qdwin_proxy_default_pointer_grab_iface = {
	qdwin_proxy_default_grab_focus,
	qdwin_proxy_default_grab_motion,
	qdwin_proxy_default_grab_button,
	qdwin_proxy_default_grab_axis,
	qdwin_proxy_default_grab_axis_source,
	qdwin_proxy_default_grab_frame,
	qdwin_proxy_default_grab_cancel,
};

/* ------------------------------------------------------------------
 * §6.8 S3c — always-active keyboard grab for nested-proxy input
 * forwarding.
 *
 * libweston has no `weston_compositor_set_default_keyboard_grab`
 * mirror of the pointer one, but `weston_keyboard.default_grab` is a
 * public field whose `interface` pointer can be swapped per seat.
 * We do that for every weston_keyboard at seat creation (and on caps
 * changes for hot-pluggable keyboards). When other grabs (popup,
 * input-method) start and end via weston_keyboard_start_grab, the
 * keyboard's grab pointer flips to them and back to default — and
 * because we mutated the default's interface itself, our grab is
 * always what "default" means.
 *
 * Methods delegate to weston_keyboard_send_key/send_modifiers for
 * normal client routing, and additionally encode QDNI key events
 * when active_input_proxy is set (the same pointer-driven state
 * machine S3b uses). Modifiers don't need a separate QDNI message:
 * the nested side calls notify_key with STATE_UPDATE_AUTOMATIC and
 * the inner xkb tracker derives mod state from keycodes alone.
 * ------------------------------------------------------------------ */

static void
qdwin_proxy_default_grab_key(struct weston_keyboard_grab *grab,
			     const struct weston_key_event *key_event)
{
	/* J29: 14->16 time/key/state now come from the event struct. */
	const struct timespec *time = &key_event->base.ts;
	uint32_t key = key_event->key;
	uint32_t state = key_event->key_state;
	struct weston_keyboard *kb = grab->keyboard;
	/* Fail-secure: while the compositor is locked, no key may reach a
	 * client through the default grab. During a normal lock the role=2
	 * overlay grab is the active keyboard grab and routes keys to the
	 * locker as overlay_key events, so this path is not hit. It IS hit in
	 * the window between a locker crash and the fresh locker reattaching:
	 * qdwin holds the lock (see qdwin_lock_surface_destroyed_cb /
	 * qdwin_locker_resource_destroy) but the overlay grab has been torn
	 * down, leaving the default grab active. Delivering keys here would
	 * leak keystrokes to the focused desktop client behind the black lock
	 * screen. Drop them instead. (Pointer input is already contained by
	 * qdwin_proxy_default_grab_{focus,motion,button} forcing focus to the
	 * lock view while locked.) */
	if (qdwin_singleton && qdwin_singleton->locked)
		return;
	qdwin_idle_note_activity(qdwin_singleton);
	/* J29: 14->16 send_key now takes the event struct. */
	weston_keyboard_send_key(kb, key_event);

	struct qdwin_toplevel *tl = qdwin_singleton ?
		qdwin_singleton->active_input_proxy : NULL;
	if (tl && tl->proxy_input_sink_fd >= 0)
		qdwin_nested_input_sink_send_key(
			tl->proxy_input_sink_fd,
			qdwin_proxy_time_msec(time), key, state);
}

static void
qdwin_proxy_default_grab_modifiers(struct weston_keyboard_grab *grab,
				   uint32_t serial,
				   uint32_t mods_depressed,
				   uint32_t mods_latched,
				   uint32_t mods_locked,
				   uint32_t group)
{
	/* Fail-secure: like the key path above, drop modifier updates to the
	 * focused client while locked. During a normal lock the overlay grab is
	 * active so this default grab isn't hit; this guards the post-crash
	 * lock-held window so Shift/Ctrl/Alt state does not leak to the hidden
	 * desktop client. */
	if (qdwin_singleton && qdwin_singleton->locked)
		return;
	weston_keyboard_send_modifiers(grab->keyboard, serial,
				       mods_depressed, mods_latched,
				       mods_locked, group);
	/* No QDNI modifiers event — see comment block above. */
}

static void
qdwin_proxy_default_grab_kb_cancel(struct weston_keyboard_grab *grab)
{
	(void)grab;
}

static const struct weston_keyboard_grab_interface
qdwin_proxy_default_keyboard_grab_iface = {
	qdwin_proxy_default_grab_key,
	qdwin_proxy_default_grab_modifiers,
	qdwin_proxy_default_grab_kb_cancel,
};

/* Swap the keyboard's default-grab interface for ours. Idempotent
 * (calling repeatedly is a no-op once the iface ptr matches). The
 * field is part of weston_keyboard's public ABI; libweston-16 has
 * carried it stable across the 14.x point releases. */
static void
qdwin_install_default_keyboard_grab(struct weston_seat *seat)
{
	struct weston_keyboard *kb = weston_seat_get_keyboard(seat);
	if (!kb)
		return;
	if (kb->default_grab.interface ==
	    &qdwin_proxy_default_keyboard_grab_iface)
		return;
	kb->default_grab.interface =
		&qdwin_proxy_default_keyboard_grab_iface;
	weston_log("qdwin/nested-proxy: default keyboard grab installed "
		   "on seat '%s'\n",
		   seat->seat_name ? seat->seat_name : "(unnamed)");
}

static const struct weston_pointer_grab_interface qdwin_popup_grab_iface = {
	qdwin_popup_grab_focus,
	qdwin_popup_grab_motion,
	qdwin_popup_grab_button,
	qdwin_popup_grab_axis,
	qdwin_popup_grab_axis_source,
	qdwin_popup_grab_frame,
	qdwin_popup_grab_cancel,
};

/* D7: shared input grab-serial check. A popup grab request must carry the
 * serial of the input event (pointer button / key / touch-down) that triggered
 * it; accept only if it matches a live grab serial on the seat. Used by both
 * qdwin_handle_show_popup and the layer-popup grab handler so the two popup
 * grab paths stay symmetric. Mirrors weston_desktop_seat_popup_grab_start. */
static bool
qdwin_seat_grab_serial_matches(struct weston_seat *wseat, uint32_t serial)
{
	if (!wseat)
		return false;
	struct weston_pointer *pointer = weston_seat_get_pointer(wseat);
	struct weston_keyboard *keyboard = weston_seat_get_keyboard(wseat);
	struct weston_touch *touch = weston_seat_get_touch(wseat);
	return (pointer && pointer->grab_serial == serial) ||
	       (keyboard && keyboard->grab_serial == serial) ||
	       (touch && touch->grab_serial == serial);
}

static void
qdwin_popup_start_grab(struct qdwin_popup *p, struct weston_compositor *ec)
{
	struct weston_seat *seat;
	wl_list_for_each(seat, &ec->seat_list, link) {
		struct weston_pointer *pointer = weston_seat_get_pointer(seat);
		if (!pointer)
			continue;
		p->grab.interface = &qdwin_popup_grab_iface;
		weston_pointer_start_grab(pointer, &p->grab);
		p->grab_active = 1;
		return;  /* MVP: first seat with a pointer wins */
	}
}

static void
qdwin_popup_surface_destroyed(struct wl_listener *listener, void *data)
{
	struct qdwin_popup *p =
		wl_container_of(listener, p, surface_destroy);
	(void)data;
	weston_log("qdwin: popup surface destroyed by client — dismissing\n");
	qdwin_popup_teardown(p);
}

static void
qdwin_popup_teardown(struct qdwin_popup *p)
{
	if (p->grab_active) {
		weston_pointer_end_grab(p->grab.pointer);
		p->grab_active = 0;
	}
	if (p->parent && p->parent->popup == p)
		p->parent->popup = NULL;
	if (p->view) {
		weston_view_destroy(p->view);
		p->view = NULL;
	}
	if (p->surface) {
		wl_list_remove(&p->surface_destroy.link);
		p->surface = NULL;
	}
	if (p->resource) {
		/* Detach the listener slot so the resource-destroy path
		 * stops calling back into a freed qdwin_popup. The
		 * resource itself is destroyed by wl_resource_destroy
		 * (called below or by libwayland on client teardown). */
		wl_resource_set_user_data(p->resource, NULL);
	}
	free(p);
}

static void
qdwin_popup_handle_destroy(struct wl_client *client,
			   struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_popup_resource_destroyed(struct wl_resource *resource)
{
	struct qdwin_popup *p = wl_resource_get_user_data(resource);
	if (!p)
		return;
	/* Client dropped the popup object — tear down the view. */
	p->resource = NULL;
	qdwin_popup_teardown(p);
}

static const struct qdwin_popup_v1_interface qdwin_popup_impl = {
	.destroy = qdwin_popup_handle_destroy,
};

static void
qdwin_handle_show_popup(struct wl_client *client,
			struct wl_resource *resource,
			uint32_t popup_id,
			uint32_t parent,
			struct wl_resource *surface_res,
			uint32_t serial,
			int32_t x, int32_t y)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);

	if (!qdwin_shell_require_bound(qdwin, resource))
		return;

	/* D6: uniform locked gate. Like request_fullscreen/tile/maximize, a
	 * popup must not be created while the compositor is locked (the lock
	 * screen owns the display). Reject BEFORE creating the qdwin_popup_v1
	 * new_id object below. set_display_power is the documented exception. */
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}

	/* D7 (v29): require a valid input grab serial, matching the layer-popup
	 * grab path. show_popup starts a pointer grab; the serial ties that grab
	 * to the real interaction that opened the menu, so a stale or fabricated
	 * serial cannot silently install a compositor-wide pointer grab. Find the
	 * seat show_popup will grab (first with a pointer — same selection as
	 * qdwin_popup_start_grab) and validate against its live grab serial,
	 * BEFORE creating the new_id object. */
	struct weston_seat *grab_seat = NULL, *gs;
	wl_list_for_each(gs, &qdwin->compositor->seat_list, link) {
		if (weston_seat_get_pointer(gs)) {
			grab_seat = gs;
			break;
		}
	}
	if (!qdwin_seat_grab_serial_matches(grab_seat, serial)) {
		weston_log("qdwin: show_popup refused: stale/invalid "
			   "serial=%u\n", serial);
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_INVALID_GRAB,
				       "show_popup: invalid grab serial %u",
				       serial);
		return;
	}

	struct qdwin_toplevel *tl = qdwin_toplevel_from_handle(qdwin, parent);
	if (!tl) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
				       "show_popup: unknown parent handle %u",
				       parent);
		return;
	}
	struct weston_surface *surface = wl_resource_get_user_data(surface_res);
	if (!surface) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
				       "show_popup: surface has no weston state");
		return;
	}

	/* One popup per toplevel. If another is already active, dismiss
	 * it before creating the new one. */
	if (tl->popup) {
		qdwin_popup_v1_send_dismissed(tl->popup->resource);
		qdwin_popup_teardown(tl->popup);
	}

	struct qdwin_popup *p = calloc(1, sizeof *p);
	if (!p) {
		wl_client_post_no_memory(client);
		return;
	}
	p->parent = tl;
	p->surface = surface;
	p->x = x;
	p->y = y;

	p->resource = wl_resource_create(client, &qdwin_popup_v1_interface, 1,
					 popup_id);
	if (!p->resource) {
		free(p);
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(p->resource, &qdwin_popup_impl,
				       p, qdwin_popup_resource_destroyed);

	p->view = weston_view_create(surface);
	if (!p->view) {
		wl_resource_destroy(p->resource);
		/* resource_destroyed ran free(p) already */
		return;
	}

	/* Position in output-absolute coords. The shell expresses x,y
	 * relative to the outer rect of the parent (origin = N chrome
	 * top-left). Our chrome N view sits at exactly that origin, so
	 * anchor there; if chrome hasn't been attached yet, fall back to
	 * the content view origin. */
	double anchor_x, anchor_y;
	struct weston_view *anchor = tl->chrome[QDWIN_SIDE_N].view
		? tl->chrome[QDWIN_SIDE_N].view
		: tl->view;
	struct weston_coord_global ap =
		weston_view_get_pos_offset_global(anchor);
	anchor_x = ap.c.x;
	anchor_y = ap.c.y;
	struct weston_coord_global pos = {
		.c = weston_coord(anchor_x + x, anchor_y + y),
	};
	weston_view_set_position(p->view, pos);
	weston_view_update_transform(p->view);
	weston_view_move_to_layer(p->view, &qdwin->popup_layer.view_list);

	p->surface_destroy.notify = qdwin_popup_surface_destroyed;
	wl_signal_add(&surface->destroy_signal, &p->surface_destroy);

	if (!weston_surface_is_mapped(surface))
		weston_surface_map(surface);

	tl->popup = p;
	qdwin_popup_start_grab(p, qdwin->compositor);
	weston_compositor_damage_all(qdwin->compositor);
	weston_log("qdwin: show_popup parent=%u at (%d,%d) -> view placed at (%.0f,%.0f) grab=%d\n",
		   parent, x, y, pos.c.x, pos.c.y, p->grab_active);
}

/* ------------------------------------------------------------------
 * §6.5 S2: per-view stream subscribe/destroy handlers.
 *
 * MVP scope: pin one toplevel's content+chrome views onto a
 * pipewire output, emit approved with the pipewire node name so
 * an external consumer can bind. RDP fields in approved are stubs
 * until qdistro-forward lands in S3.
 * ------------------------------------------------------------------ */

static int
qdwin_output_has_stream(struct qdwin *qdwin, struct weston_output *o)
{
	struct qdwin_view_stream *s;
	wl_list_for_each(s, &qdwin->view_streams, link)
		if (s->pw_output == o)
			return 1;
	return 0;
}

static struct weston_output *
qdwin_find_free_pipewire_output(struct qdwin *qdwin)
{
	struct weston_output *o;
	wl_list_for_each(o, &qdwin->compositor->output_list, link) {
		if (strncmp(o->name, "pipewire", 8) != 0)
			continue;
		if (qdwin_output_has_stream(qdwin, o))
			continue;
		return o;
	}
	return NULL;
}

static struct weston_output *
qdwin_find_non_pipewire_output(struct qdwin *qdwin)
{
	struct weston_output *o;
	wl_list_for_each(o, &qdwin->compositor->output_list, link)
		if (strncmp(o->name, "pipewire", 8) != 0)
			return o;
	return NULL;
}

static void
qdwin_view_stream_pin(struct qdwin_view_stream *s)
{
	struct qdwin_toplevel *tl = s->tl;
	struct weston_output *pw = s->pw_output;
	if (!tl || !tl->view || !pw)
		return;
	if (tl->nested_proxy_pending_decision)
		return;

	/* Remember where the view was so we can restore on teardown. */
	s->prev_pos = weston_view_get_pos_offset_global(tl->view);
	s->prev_output = tl->view->output;

	/* Move view inside the pipewire output's bounding rect so it stops
	 * intersecting the primary output and therefore stops rendering
	 * there. Position flush to the pipewire output origin plus a small
	 * inset so chrome N has room above. */
	struct weston_coord_global pos = {
		.c = weston_coord(pw->pos.c.x + 32, pw->pos.c.y + 32),
	};
	weston_view_set_position(tl->view, pos);
	weston_view_set_output(tl->view, pw);
	for (int side = 0; side < QDWIN_SIDES; side++)
		if (tl->chrome[side].view)
			weston_view_set_output(tl->chrome[side].view, pw);
	qdwin_toplevel_position_chrome(tl);
	weston_view_update_transform(tl->view);
	/* Damage the surface(s) so the next pipewire-output repaint
	 * actually composites the view bytes — without this, the first
	 * PipeWire frame can be the pre-pin empty framebuffer of the
	 * pipewire output, which the consumer captures and shows as
	 * mostly black. */
	/* J29: 14->16 weston_surface_damage() removed. weston_view_geometry_dirty()
	 * dirties the view's paint nodes + schedules a repaint, forcing the moved
	 * view (and chrome) to recomposite on the pipewire output. */
	if (tl->view->surface)
		weston_view_geometry_dirty(tl->view);
	for (int side = 0; side < QDWIN_SIDES; side++)
		if (tl->chrome[side].view && tl->chrome[side].view->surface)
			weston_view_geometry_dirty(tl->chrome[side].view);
	weston_output_schedule_repaint(pw);
	s->pinned = 1;
}

static void
qdwin_view_stream_unpin(struct qdwin_view_stream *s)
{
	if (!s->pinned)
		return;
	s->pinned = 0;
	struct qdwin_toplevel *tl = s->tl;
	if (!tl || !tl->view)
		return;

	struct weston_output *target = s->prev_output;
	if (!target)
		target = qdwin_find_non_pipewire_output(s->qdwin);
	if (target) {
		weston_view_set_output(tl->view, target);
		for (int side = 0; side < QDWIN_SIDES; side++)
			if (tl->chrome[side].view)
				weston_view_set_output(tl->chrome[side].view, target);
	}
	weston_view_set_position(tl->view, s->prev_pos);
	qdwin_toplevel_position_chrome(tl);
	weston_view_update_transform(tl->view);
	if (target)
		weston_output_schedule_repaint(target);
}

/* Fill buf with n bytes from the kernel CSPRNG. getrandom(2) may return
 * short or -1/EINTR (blocking flag 0, interrupted before the entropy pool is
 * ready); loop over those. Any other failure (ENOSYS, EFAULT) is hard: return
 * -1 and let the caller refuse to mint a token rather than fabricate one from
 * a clock (iso2 `11` E4). */
static int
qdwin_getrandom_full(unsigned char *buf, size_t n)
{
	size_t off = 0;
	int tries = 0;
	while (off < n) {
		ssize_t got = getrandom(buf + off, n - off, 0);
		if (got < 0) {
			if (errno == EINTR && ++tries < 64)
				continue;
			return -1;
		}
		if (got == 0 && ++tries >= 64)
			return -1;
		off += (size_t)got;
	}
	return 0;
}

/* Hex-encode n random bytes into out (out_len must be 2*n+1). */
static int
qdwin_hex_token(char *out, size_t out_len, size_t n_bytes)
{
	unsigned char buf[32];
	if (n_bytes > sizeof buf)
		n_bytes = sizeof buf;
	if (qdwin_getrandom_full(buf, n_bytes) < 0) {
		out[0] = '\0';
		return -1;
	}
	static const char hex[] = "0123456789abcdef";
	size_t slots = (out_len - 1) / 2;
	if (slots > n_bytes)
		slots = n_bytes;
	for (size_t i = 0; i < slots; i++) {
		out[2*i]     = hex[buf[i] >> 4];
		out[2*i + 1] = hex[buf[i] & 0xf];
	}
	out[2*slots] = '\0';
	return 0;
}

/* Reject QDWIN_FORWARD_BIN unless it is a regular file owned by root
 * with no group/world write bits — matches qdlocker config-hardening
 * predicate. Compositor process may be reached by the session user, so
 * a writable forwarder would let them inject code into the compositor's
 * forked child. */
static int
qdwin_forward_bin_is_trusted(const char *path)
{
	struct stat st;
	if (lstat(path, &st) != 0) {
		weston_log("qdwin: WARN QDWIN_FORWARD_BIN=%s lstat failed: %m; "
			   "falling back to compiled-in path\n", path);
		return 0;
	}
	if (!S_ISREG(st.st_mode)) {
		weston_log("qdwin: WARN QDWIN_FORWARD_BIN=%s is not a regular "
			   "file; falling back to compiled-in path\n", path);
		return 0;
	}
	if (st.st_uid != 0) {
		weston_log("qdwin: WARN QDWIN_FORWARD_BIN=%s not owned by root "
			   "(uid=%u); falling back to compiled-in path\n",
			   path, (unsigned)st.st_uid);
		return 0;
	}
	if (st.st_mode & (S_IWGRP | S_IWOTH)) {
		weston_log("qdwin: WARN QDWIN_FORWARD_BIN=%s is group/world-"
			   "writable; falling back to compiled-in path\n", path);
		return 0;
	}
	return 1;
}

static const char *
qdwin_forward_bin_path(void)
{
	const char *env = getenv("QDWIN_FORWARD_BIN");
	if (env && *env && qdwin_forward_bin_is_trusted(env))
		return env;
	return "/usr/bin/qdistro-forward";
}

/* The wayland socket qdistro-forward must connect back to in order to claim the
 * per-stream input-injection channel (qdwin_stream_input_v1). This is qdwin's OWN
 * compositor socket — weston exports it as WAYLAND_DISPLAY in our process env when
 * it adds the socket (`--socket=NAME`), so the forward lands on whatever socket
 * this qdwin actually listens on, not a hardcoded "wayland-0". Was hardcoded
 * "wayland-0", which forced any input-capable export to run qdwin on exactly that
 * socket (session-4 input-confinement foot-gun); reading the env removes that
 * coupling. Falls back to "wayland-0" only if the env is somehow unset. */
static const char *
qdwin_forward_wayland_display(void)
{
	const char *disp = getenv("WAYLAND_DISPLAY");
	if (disp && *disp)
		return disp;
	return "wayland-0";
}

static int
qdwin_forward_write_secret(int fd, const char *secret)
{
	const char *p = secret ? secret : "";
	size_t left = strlen(p);

	while (left > 0) {
		ssize_t n = write(fd, p, left);
		if (n < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		p += n;
		left -= (size_t)n;
	}
	return 0;
}

/* item 5 (codex impl-26): open a pidfd to a child we forked. glibc may expose
 * pidfd_open(2) directly, but we don't want a configure-time probe — go straight
 * through the syscall, guarded so a kernel/headers without SYS_pidfd_open fails
 * loudly (the target host is kernel 6.18, where this is always present). */
static int
qdwin_pidfd_open(pid_t pid)
{
#ifdef SYS_pidfd_open
	return (int)syscall(SYS_pidfd_open, pid, 0u);
#else
	(void)pid;
	errno = ENOSYS;
	return -1;
#endif
}

/* Signal the process identified by an open pidfd, never by a potentially
 * recycled numeric PID. Keep the raw syscall fallback local so this builds on
 * libc versions that do not yet declare pidfd_send_signal(2). */
static int
qdwin_pidfd_send_signal(int pidfd, int sig)
{
#if defined(SYS_pidfd_send_signal)
	return (int)syscall(SYS_pidfd_send_signal, pidfd, sig, NULL, 0u);
#elif defined(__NR_pidfd_send_signal)
	return (int)syscall(__NR_pidfd_send_signal, pidfd, sig, NULL, 0u);
#else
	(void)pidfd;
	(void)sig;
	errno = ENOSYS;
	return -1;
#endif
}

/* item 5: DISARM the forward death-watch — idempotent. Removes the event source
 * and closes the pidfd, resetting both fields so a later call (or the death
 * callback itself) is a no-op. MUST run before the stream struct is freed and
 * before any qdwin-initiated reap, so the event loop can never dispatch
 * qdwin_forward_pidfd_ready against a freed/partly-torn-down stream. */
static void
qdwin_view_stream_disarm_pidfd(struct qdwin_view_stream *s)
{
	struct wl_event_source *src = s->forward_pidfd_source;
	int fd = s->forward_pidfd;
	s->forward_pidfd_source = NULL;
	s->forward_pidfd = -1;
	/* libwayland defers the source free to post-dispatch, so removing the
	 * currently-dispatching source from inside its own callback is safe. */
	if (src)
		wl_event_source_remove(src);
	if (fd >= 0)
		close(fd);
}

/* Revoke everything that makes a stream live, exactly once, independently of
 * the client-owned qdwin_view_stream_v1 resource. A client may ignore the
 * terminal event indefinitely; it must not retain a PipeWire output, input
 * capability, seat, child process, or a discoverable access token while its
 * lightweight protocol tombstone waits for destroy/disconnect. */
static void
qdwin_view_stream_release_server_state(struct qdwin_view_stream *s)
{
	if (s->server_state_released)
		return;
	s->server_state_released = 1;

	if (s->input_handle) {
		struct wl_resource *h = s->input_handle;
		s->input_handle = NULL;
		s->input_claimed = 0;
		wl_resource_set_user_data(h, NULL);
		wl_resource_destroy(h);
	}
	qdwin_view_stream_reap_forward(s);
	qdwin_view_stream_unpin(s);
	qdwin_stream_seat_release(s);
	if (s->listed) {
		wl_list_remove(&s->link);
		wl_list_init(&s->link);
		s->listed = 0;
	}

	/* No released stream remains token-addressable (it is already off the
	 * active list), and erase the credentials retained by its tombstone. */
	memset(s->access_token, 0, sizeof s->access_token);
	memset(s->rdp_password, 0, sizeof s->rdp_password);
	s->input_claimed = 0;
	s->allow_input = 0;
	s->tl = NULL;
	s->pw_output = NULL;
	s->prev_output = NULL;
	s->rdp_port = 0;
	weston_log("qdwin: view_stream_server_state_released handle=%u\n",
		   s->toplevel_handle);
}

/* The sole server-originated terminal transition. It is deliberately
 * idempotent: competing boundaries (source close, lock entry, forward death)
 * may be observed in one event-loop turn, but the client gets exactly one
 * event and server-owned state is revoked exactly once. The wl_resource stays
 * alive as an inert client-owned tombstone until destroy/disconnect. */
static void
qdwin_view_stream_terminate(struct qdwin_view_stream *s, const char *reason,
			    pid_t audit_pid)
{
	if (!s)
		return;
	if (s->resource && !s->torn_down_sent) {
		s->torn_down_sent = 1;
		weston_log("qdwin: view_stream_torn_down handle=%u pid=%d "
			   "reason=\"%s\"\n", s->toplevel_handle,
			   (int)audit_pid, reason);
		qdwin_view_stream_v1_send_torn_down(s->resource, reason);
	}
	qdwin_view_stream_release_server_state(s);
}

/* Detach everything that holds a pointer to, or a grab on, `tl` before the
 * toplevel is freed. BOTH destroy paths must call this: a real xdg_toplevel
 * via qdwin_surface_removed, and a nested proxy via
 * qdwin_nested_proxy_destroy (iso2/10 E2 — the proxy path used to free `tl`
 * with live view_streams and popups still pointing at it, so an advertiser
 * disconnect while qdshell had a stream or a chrome popup on the proxy was a
 * compositor use-after-free, not a client disconnect).
 *
 * Call it while tl->view is still alive: unpinning a stream and ending a
 * per-stream grab both still need a valid tl/view. It is idempotent — after
 * it runs, tl owns no popup, no move-grab targets its handle, and no stream
 * in qdwin->view_streams has s->tl == tl (termination NULLs s->tl and
 * removes the stream from the list, hence the SAFE iteration). */
static void
qdwin_toplevel_release_dependents(struct qdwin *qdwin, struct qdwin_toplevel *tl)
{
	if (!qdwin || !tl)
		return;

	if (tl->popup) {
		qdwin_popup_v1_send_dismissed(tl->popup->resource);
		qdwin_popup_teardown(tl->popup);
	}
	qdwin_move_grab_end_for(qdwin, tl->handle);

	struct qdwin_view_stream *vs, *next;
	wl_list_for_each_safe(vs, next, &qdwin->view_streams, link)
		if (vs->tl == tl)
			qdwin_view_stream_terminate(vs, "source toplevel closed",
						    vs->forward_pid);
}

/* item 5: the forward child exited on its own (crash, exec failure, or a future
 * fatal PipeWire error). weston's signalfd handler waitpid(-1)-reaps it; we just
 * learn of the death via pidfd readiness and run the ONE teardown path: tell the
 * subscriber (torn_down "forward exited"), immediately revoke all server-owned
 * state, and retain only the inert client-owned protocol resource. Its
 * torn_down handler normally sends the destructor request; an ignoring client
 * retains no output/input/token/list capability. Destroying the resource here
 * would race a compliant client's destructor and disconnect it with "invalid
 * object". We do NOT waitpid (weston owns reaping). */
static int
qdwin_forward_pidfd_ready(int fd, uint32_t mask, void *data)
{
	struct qdwin_view_stream *s = data;
	(void)fd;
	(void)mask;
	pid_t dead = s->forward_pid;
	/* Disarm FIRST: stop this (level-triggered) source from re-firing and stop
	 * the upcoming resource-destroyed reap from touching the source/fd again. */
	qdwin_view_stream_disarm_pidfd(s);
	/* A readable pidfd identifies the exited child, but the numeric PID may be
	 * reused as soon as weston reaps it. Relinquish ownership BEFORE generic
	 * cleanup so reap_forward can never SIGTERM an unrelated reused PID. Keep
	 * the saved value only as audit data for the terminal journal record. */
	s->forward_pid = 0;
	weston_log("qdwin: qdistro-forward pid=%d exited; tearing down view_stream "
		   "rdp_port=%u (forward exited)\n", (int)dead, s->rdp_port);
	qdwin_view_stream_terminate(s, "forward exited", dead);
	return 0;
}

static int
qdwin_view_stream_spawn_forward(struct qdwin_view_stream *s,
				const char *pw_node_name,
				int view_width, int view_height)
{
	int token_pipe[2] = { -1, -1 };
	int password_pipe[2] = { -1, -1 };
	/* Resolve qdwin's real wayland socket in the PARENT (getenv is not
	 * async-signal-safe to call in the post-fork child); the child references
	 * this pointer into environ, valid across fork. Log the choice + its source:
	 * a fallback to "wayland-0" in a real session means WAYLAND_DISPLAY was
	 * unset/wrong, which would be an input-routing bug worth surfacing (not a
	 * silent default). */
	const char *wl_env = getenv("WAYLAND_DISPLAY");
	const char *wl_display = qdwin_forward_wayland_display();
	weston_log("qdwin: qdistro-forward will use wayland display %s (%s)\n",
		   wl_display,
		   (wl_env && *wl_env) ? "from WAYLAND_DISPLAY" : "FALLBACK wayland-0");
	if (pipe(token_pipe) != 0) {
		weston_log("qdwin: pipe failed for qdistro-forward token: %m\n");
		return -1;
	}
	if (pipe(password_pipe) != 0) {
		weston_log("qdwin: pipe failed for qdistro-forward password: %m\n");
		close(token_pipe[0]);
		close(token_pipe[1]);
		return -1;
	}

	pid_t pid = fork();
	if (pid < 0) {
		weston_log("qdwin: fork failed for qdistro-forward: %m\n");
		close(token_pipe[0]);
		close(token_pipe[1]);
		close(password_pipe[0]);
		close(password_pipe[1]);
		return -1;
	}
	if (pid == 0) {
		close(token_pipe[1]);
		close(password_pipe[1]);
		/* Child. Reset signal handlers weston installed on our behalf. */
		signal(SIGPIPE, SIG_DFL);
		signal(SIGTERM, SIG_DFL);
		signal(SIGINT, SIG_DFL);
		/* Weston's mainloop blocks SIGTERM/SIGINT/SIGCHLD/SIGUSR1/SIGUSR2
		 * (they arrive via a signalfd drained by the event loop). The
		 * signal mask is inherited across fork+execve, so without this
		 * the child can install a SIGTERM handler but never see it
		 * delivered — waitpid of the eventual reap blocks forever.
		 * Clear the full mask so the child runs with the standard
		 * "no signals blocked" state. */
		sigset_t empty;
		sigemptyset(&empty);
		sigprocmask(SIG_SETMASK, &empty, NULL);

		char port_arg[16];
		char token_fd_arg[16];
		char password_fd_arg[16];
		char width_arg[16];
		char height_arg[16];
		snprintf(port_arg,   sizeof port_arg,   "%u", s->rdp_port);
		snprintf(token_fd_arg, sizeof token_fd_arg, "%d", token_pipe[0]);
		snprintf(password_fd_arg, sizeof password_fd_arg, "%d", password_pipe[0]);
		snprintf(width_arg,  sizeof width_arg,  "%d", view_width  > 0 ? view_width  : 640);
		snprintf(height_arg, sizeof height_arg, "%d", view_height > 0 ? view_height : 480);

		const char *bin = qdwin_forward_bin_path();
		execl(bin, bin,
		      "--pipewire-node", pw_node_name,
		      "--access-token-fd", token_fd_arg,
		      "--rdp-port", port_arg,
		      "--rdp-password-fd", password_fd_arg,
		      "--wayland-display", wl_display,
		      "--width", width_arg,
		      "--height", height_arg,
		      (char *)NULL);
		fprintf(stderr, "exec %s failed: %m\n", bin);
		_exit(127);
	}
	int secret_write_failed = 0;
	if (qdwin_forward_write_secret(token_pipe[1], s->access_token) < 0) {
		weston_log("qdwin: writing qdistro-forward token failed: %m\n");
		secret_write_failed = 1;
	}
	if (qdwin_forward_write_secret(password_pipe[1], s->rdp_password) < 0) {
		weston_log("qdwin: writing qdistro-forward password failed: %m\n");
		secret_write_failed = 1;
	}
	close(token_pipe[1]);
	close(password_pipe[1]);
	close(token_pipe[0]);
	close(password_pipe[0]);
	if (secret_write_failed) {
		kill(pid, SIGTERM);
		waitpid(pid, NULL, WNOHANG);
		return -1;
	}
	s->forward_pid = pid;
	/* item 5 (codex impl-26): arm the pidfd death-watch on THIS exact child,
	 * synchronously in the parent BEFORE we return to weston's event loop — so
	 * weston's signalfd reaper cannot have run yet and pidfd_open still has a live
	 * (possibly already-zombie-but-unreaped) target. A failure to arm makes the
	 * stream non-item-5-compliant, so we kill the forward and fail the spawn
	 * rather than leave a death-blind stream. */
	int pidfd = qdwin_pidfd_open(pid);
	if (pidfd < 0) {
		weston_log("qdwin: pidfd_open(qdistro-forward pid=%d) failed: %m; "
			   "killing forward + failing stream\n", (int)pid);
		/* Still synchronous in the fork/arm window: the event loop has not
		 * resumed and cannot reap/reuse this child PID. This is the ONLY
		 * permitted numeric-PID signal fallback. */
		if (kill(pid, SIGTERM) != 0 && errno != ESRCH)
			weston_log("qdwin: pre-arm SIGTERM pid=%d failed: %m\n",
				   (int)pid);
		waitpid(pid, NULL, WNOHANG);
		s->forward_pid = 0;
		return -1;
	}
	/* Prove pidfd_send_signal is available before publishing this child to the
	 * event loop. Signal 0 is non-mutating. On old kernels/libc-header mixes,
	 * fail the spawn inside the same synchronous no-reuse window instead of
	 * creating a live stream that could only be stopped by numeric PID later. */
	if (qdwin_pidfd_send_signal(pidfd, 0) != 0) {
		weston_log("qdwin: pidfd_send_signal probe failed for "
			   "qdistro-forward pid=%d: %m; failing stream\n", (int)pid);
		if (kill(pid, SIGTERM) != 0 && errno != ESRCH)
			weston_log("qdwin: pre-arm SIGTERM pid=%d failed: %m\n",
				   (int)pid);
		close(pidfd);
		waitpid(pid, NULL, WNOHANG);
		s->forward_pid = 0;
		return -1;
	}
	struct wl_event_source *src = wl_event_loop_add_fd(
		wl_display_get_event_loop(s->qdwin->compositor->wl_display),
		pidfd, WL_EVENT_READABLE, qdwin_forward_pidfd_ready, s);
	if (!src) {
		weston_log("qdwin: wl_event_loop_add_fd(pidfd) failed for "
			   "qdistro-forward pid=%d; killing forward + failing stream\n",
			   (int)pid);
		/* The local pidfd still identifies the exact child. Signal through it
		 * before close; do not fall back to the numeric PID. */
		if (qdwin_pidfd_send_signal(pidfd, SIGTERM) != 0 && errno != ESRCH)
			weston_log("qdwin: pre-arm pidfd SIGTERM pid=%d failed: %m\n",
				   (int)pid);
		close(pidfd);
		waitpid(pid, NULL, WNOHANG);
		s->forward_pid = 0;
		return -1;
	}
	s->forward_pidfd = pidfd;
	s->forward_pidfd_source = src;
	weston_log("qdwin: spawned qdistro-forward pid=%d port=%u node=%s "
		   "(pidfd death-watch armed)\n",
		   (int)pid, s->rdp_port, pw_node_name);
	return 0;
}

static void
qdwin_view_stream_reap_forward(struct qdwin_view_stream *s)
{
	pid_t pid = s->forward_pid;
	if (pid <= 0) {
		qdwin_view_stream_disarm_pidfd(s);
		return;
	}

	/* The pidfd remains open until after signaling, so PID reuse cannot retarget
	 * teardown. A live stream must always have an armed pidfd; if an invariant
	 * violation leaves none, fail closed by refusing an unsafe numeric kill. */
	if (s->forward_pidfd >= 0) {
		if (qdwin_pidfd_send_signal(s->forward_pidfd, SIGTERM) != 0 &&
		    errno != ESRCH)
			weston_log("qdwin: pidfd SIGTERM qdistro-forward pid=%d "
				   "failed: %m\n", (int)pid);
	} else {
		weston_log("qdwin: refusing numeric SIGTERM without pidfd for "
			   "qdistro-forward pid=%d\n", (int)pid);
	}
	qdwin_view_stream_disarm_pidfd(s);
	/* Non-blocking reap; let weston's SIGCHLD-or-idle loop catch it.
	 * If we block here we stall the wayland dispatch. ECHILD is EXPECTED:
	 * weston's signalfd handler waitpid(-1)-reaps EVERY child, so it may have
	 * already reaped this forward — the result is best-effort only and is never
	 * used to decide teardown (the pidfd death-watch alone emits torn_down). */
	int status;
	pid_t got = waitpid(pid, &status, WNOHANG);
	if (got == pid) {
		weston_log("qdwin: qdistro-forward pid=%d reaped status=%d\n",
			   (int)pid, status);
	}
	s->forward_pid = 0;
}

static void
qdwin_stream_resource_destroyed(struct wl_resource *resource)
{
	struct qdwin_view_stream *s = wl_resource_get_user_data(resource);
	if (!s)
		return;
	qdwin_view_stream_release_server_state(s);
	free(s);
}

static void
qdwin_stream_handle_destroy(struct wl_client *client,
			    struct wl_resource *resource)
{
	(void)client;
	/* destroy is itself the client's terminal acknowledgement/cancellation.
	 * torn_down is server-originated only; replying here would either duplicate
	 * an earlier terminal event or target a proxy the client just destroyed. */
	wl_resource_destroy(resource);
}

static const struct qdwin_view_stream_v1_interface qdwin_stream_impl = {
	.destroy = qdwin_stream_handle_destroy,
};

static void
qdwin_handle_subscribe_view_stream(struct wl_client *client,
				   struct wl_resource *shell_resource,
				   uint32_t stream_id,
				   uint32_t handle,
				   const char *peer_label,
				   int32_t desired_width,
				   int32_t desired_height,
				   uint32_t allow_input)
{
	struct qdwin *qdwin = wl_resource_get_user_data(shell_resource);

	if (!qdwin_shell_require_bound(qdwin, shell_resource))
		return;

	struct qdwin_toplevel *tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		wl_resource_post_error(shell_resource,
				       QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
				       "subscribe_view_stream: unknown handle %u",
				       handle);
		return;
	}

	struct wl_resource *stream_resource =
		wl_resource_create(client, &qdwin_view_stream_v1_interface,
				   1, stream_id);
	if (!stream_resource) {
		wl_client_post_no_memory(client);
		return;
	}

	if (tl->nested_proxy_pending_decision) {
		wl_resource_set_implementation(stream_resource,
					       &qdwin_stream_impl,
					       NULL, NULL);
		qdwin_view_stream_v1_send_denied(
			stream_resource,
			"nested proxy is waiting for admin decision");
		weston_log("qdwin: subscribe_view_stream denied handle=%u "
			   "(nested-proxy pending admin decision)\n", handle);
		return;
	}

	struct weston_output *pw = qdwin_find_free_pipewire_output(qdwin);
	if (!pw) {
		/* Attach a minimal impl so destroy still frees the resource;
		 * then immediately emit denied. No stream struct allocated. */
		wl_resource_set_implementation(stream_resource,
					       &qdwin_stream_impl,
					       NULL, NULL);
		qdwin_view_stream_v1_send_denied(
			stream_resource,
			"no free pipewire output "
			"(increase [pipewire] num-outputs in weston.ini)");
		weston_log("qdwin: subscribe_view_stream denied "
			   "handle=%u peer_label=\"%s\" (no pw output)\n",
			   handle, peer_label ? peer_label : "");
		return;
	}

	struct qdwin_view_stream *s = calloc(1, sizeof *s);
	if (!s) {
		wl_resource_destroy(stream_resource);
		wl_client_post_no_memory(client);
		return;
	}
	s->resource = stream_resource;
	s->qdwin = qdwin;
	s->tl = tl;
	s->toplevel_handle = handle;
	s->pw_output = pw;
	/* item 5: calloc zeroed forward_pidfd to 0 (a valid fd); reset to -1 so the
	 * disarm helper never closes stdin if the watch was never armed. */
	s->forward_pidfd = -1;
	s->forward_pidfd_source = NULL;
	/* The subscriber's allow_input is now ENFORCED (was ignored): a read-only
	 * export (allow_input=0) keeps its pixel stream + per-stream seat (for focus
	 * locking) but DROPS every injected event at the inject_* boundary below, so a
	 * read-only export cannot be driven by the remote subscriber's forward. */
	s->allow_input = allow_input ? 1 : 0;
	wl_list_insert(&qdwin->view_streams, &s->link);
	s->listed = 1;

	wl_resource_set_implementation(stream_resource, &qdwin_stream_impl,
				       s, qdwin_stream_resource_destroyed);

	qdwin_view_stream_pin(s);

	char node_name[128];
	snprintf(node_name, sizeof node_name, "weston.%s", pw->name);

	/* S3: spawn the external proxy. Generate token + password and
	 * reserve a port; the proxy uses these for its RDP listener. The
	 * forward must agree with weston's backend-pipewire on the stream
	 * dimensions — backend-pipewire offers a fixed-size raw video
	 * format at the output's mode, so pass the pipewire-output's
	 * mode (NOT the toplevel's surface size). desired_* from the
	 * subscriber is treated as a hint only and ignored if it doesn't
	 * fit; for now we always use the pipewire output's size. */
	(void)desired_width; (void)desired_height;
	s->rdp_port = qdwin->next_stream_port++;
	if (qdwin_hex_token(s->access_token, sizeof s->access_token, 16) < 0 ||
	    qdwin_hex_token(s->rdp_password, sizeof s->rdp_password, 8) < 0) {
		qdwin_view_stream_v1_send_denied(
			stream_resource,
			"kernel random source unavailable");
		weston_log("qdwin: subscribe_view_stream denied "
			   "handle=%u peer_label=\"%s\" (getrandom failed)\n",
			   handle, peer_label ? peer_label : "");
		wl_resource_destroy(stream_resource);
		return;
	}

	/* Bring the per-stream virtual input device up before we advertise
	 * the stream to the subscriber — once the wl_client sees the
	 * approved event it may connect and start pushing events
	 * immediately (via claim on qdwin_stream_input_v1). */
	qdwin_stream_seat_init(s);
	int spawn_w = (pw->current_mode && pw->current_mode->width  > 0)
		? pw->current_mode->width  : 640;
	int spawn_h = (pw->current_mode && pw->current_mode->height > 0)
		? pw->current_mode->height : 480;
	if (qdwin_view_stream_spawn_forward(s, node_name, spawn_w, spawn_h) < 0) {
		qdwin_view_stream_v1_send_denied(
			stream_resource,
			"failed to start qdistro-forward");
		wl_resource_destroy(stream_resource);
		return;
	}

	/* Cert path: reuse qdwin's main RDP cert for now; per-stream certs
	 * are a hardening item. */
	const char *cert_path = getenv("QDWIN_RDP_CERT")
		? getenv("QDWIN_RDP_CERT")
		: "";
	qdwin_view_stream_v1_send_approved(stream_resource,
					   node_name,
					   s->rdp_port,
					   cert_path,
					   s->rdp_password);
	weston_log("qdwin: view_stream approved handle=%u peer_label=\"%s\" "
		   "pw=%s output_pos=(%.0f,%.0f) rdp_port=%u "
		   "forward_pid=%d allow_input=%u\n",
		   handle, peer_label ? peer_label : "",
		   pw->name, pw->pos.c.x, pw->pos.c.y,
		   s->rdp_port, (int)s->forward_pid,
		   (unsigned)allow_input);
}

/* ------------------------------------------------------------------
 * §6.5 S5: qdwin_stream_input_v1 — a wl_client (qdistro-forward today,
 * potentially an AI agent or test harness tomorrow) injects pointer/
 * keyboard events into one specific view through this private channel.
 * claim() validates the access_token (one-shot) issued by the spawn
 * argv; inject_* are dispatched through a per-stream weston_seat whose
 * pointer/keyboard focus is pinned to tl->view. The per-stream seat is
 * the "virtual input device" abstraction — the compositor doesn't care
 * whether the events originated from RDP, an AI policy, or a fuzzer.
 * ------------------------------------------------------------------ */

/* IMPL-19 per-stream confinement grab. Modeled on the locked-session branch of
 * the default proxy grab: it FORCES pointer focus to the source view (tl->view)
 * and delivers there, ignoring the compositor-global picker — so injected remote
 * input is confined to the exported window even when a local view overlaps it at
 * the same global coordinate. If the source view is gone / input is no longer
 * allowed, events are DROPPED rather than falling back to geometric focus. */
static struct weston_view *
qdwin_stream_confine_target(struct qdwin_view_stream *s)
{
	if (!s || !s->allow_input || !s->tl || s->tl->nested_proxy_pending_decision)
		return NULL;
	if (!s->tl->view || !s->tl->view->surface)
		return NULL;
	return s->tl->view;
}

static void
qdwin_stream_confine_focus(struct weston_pointer_grab *grab)
{
	struct qdwin_view_stream *s = wl_container_of(grab, s, confine_grab);
	struct weston_view *v = qdwin_stream_confine_target(s);
	if (v && grab->pointer->focus != v)
		weston_pointer_set_focus(grab->pointer, v);
}

static void
qdwin_stream_confine_motion(struct weston_pointer_grab *grab,
			    const struct weston_pointer_motion_event *event)
{
	/* J29: 14->16 dropped `time` param. */
	struct qdwin_view_stream *s = wl_container_of(grab, s, confine_grab);
	struct weston_pointer *pointer = grab->pointer;
	weston_pointer_move(pointer, event);
	struct weston_view *v = qdwin_stream_confine_target(s);
	if (!v)
		return;                  /* source view gone: drop, don't re-pick */
	if (pointer->focus != v)
		weston_pointer_set_focus(pointer, v);
	weston_pointer_send_motion(pointer, event);
}

static void
qdwin_stream_confine_button(struct weston_pointer_grab *grab,
			    const struct weston_pointer_button_event *button_event)
{
	/* J29: 14->16 button/state now come from the event struct. */
	struct qdwin_view_stream *s = wl_container_of(grab, s, confine_grab);
	struct weston_pointer *pointer = grab->pointer;
	struct weston_view *v = qdwin_stream_confine_target(s);
	if (!v)
		return;
	if (pointer->focus != v)
		weston_pointer_set_focus(pointer, v);
	weston_pointer_send_button(pointer, button_event);
}

static void
qdwin_stream_confine_axis(struct weston_pointer_grab *grab,
			  const struct weston_pointer_axis_event *event)
{
	/* J29: 14->16 dropped `time` param. */
	struct qdwin_view_stream *s = wl_container_of(grab, s, confine_grab);
	struct weston_pointer *pointer = grab->pointer;
	struct weston_view *v = qdwin_stream_confine_target(s);
	if (!v)
		return;
	if (pointer->focus != v)
		weston_pointer_set_focus(pointer, v);
	weston_pointer_send_axis(pointer, event);
}

static void
qdwin_stream_confine_axis_source(struct weston_pointer_grab *grab,
				 uint32_t source)
{
	struct qdwin_view_stream *s = wl_container_of(grab, s, confine_grab);
	if (!qdwin_stream_confine_target(s))
		return;                  /* drop when no source view, like motion/axis */
	weston_pointer_send_axis_source(grab->pointer, source);
}

static void
qdwin_stream_confine_frame(struct weston_pointer_grab *grab)
{
	struct qdwin_view_stream *s = wl_container_of(grab, s, confine_grab);
	if (!qdwin_stream_confine_target(s))
		return;
	weston_pointer_send_frame(grab->pointer);
}

static void
qdwin_stream_confine_cancel(struct weston_pointer_grab *grab)
{
	/* Weston calls cancel when another grab replaces ours (or on teardown).
	 * After that our grab is no longer the pointer's active grab, so clear
	 * the active flag — otherwise a later grab_end would call
	 * weston_pointer_end_grab on a pointer whose grab is now someone else's
	 * and tear down the wrong grab (codex impl-20). */
	struct qdwin_view_stream *s = wl_container_of(grab, s, confine_grab);
	s->confine_grab_active = 0;
}

static const struct weston_pointer_grab_interface qdwin_stream_confine_grab_iface = {
	qdwin_stream_confine_focus,
	qdwin_stream_confine_motion,
	qdwin_stream_confine_button,
	qdwin_stream_confine_axis,
	qdwin_stream_confine_axis_source,
	qdwin_stream_confine_frame,
	qdwin_stream_confine_cancel,
};

static void
qdwin_stream_confine_grab_start(struct qdwin_view_stream *s)
{
	if (s->confine_grab_active || !s->seat_inited)
		return;
	struct weston_pointer *ptr = weston_seat_get_pointer(&s->stream_seat);
	if (!ptr)
		return;
	s->confine_grab.interface = &qdwin_stream_confine_grab_iface;
	weston_pointer_start_grab(ptr, &s->confine_grab);
	s->confine_grab_active = 1;
	weston_log("qdwin: stream seat '%s' confinement grab started (port=%u)\n",
		   s->stream_seat.seat_name ? s->stream_seat.seat_name : "?",
		   s->rdp_port);
}

static void
qdwin_stream_confine_grab_end(struct qdwin_view_stream *s)
{
	if (!s->confine_grab_active)
		return;
	s->confine_grab_active = 0;
	/* Only end the grab if OURS is still the pointer's active grab — if it
	 * was already replaced/cancelled, ending here would tear down whatever
	 * grab is active now (codex impl-20). */
	struct weston_pointer *ptr = s->confine_grab.pointer;
	if (ptr && ptr->grab == &s->confine_grab)
		weston_pointer_end_grab(ptr);
}

static void
qdwin_stream_seat_init(struct qdwin_view_stream *s)
{
	if (s->seat_inited)
		return;

	char seat_name[64];
	snprintf(seat_name, sizeof seat_name,
		 "qdwin-stream-%u", s->rdp_port);
	weston_seat_init(&s->stream_seat, s->qdwin->compositor, seat_name);

	if (weston_seat_init_pointer(&s->stream_seat) < 0) {
		weston_log("qdwin: stream seat init_pointer failed (port=%u)\n",
			   s->rdp_port);
	}

	/* Inherit the compositor-wide xkb keymap — per-stream layouts are
	 * out of scope; if the remote user wants a different layout they
	 * can set it on their RDP client (which sends scan codes we
	 * translate identically regardless of layout). */
	struct xkb_keymap *keymap = NULL;
	if (s->qdwin->compositor->xkb_info)
		keymap = s->qdwin->compositor->xkb_info->keymap;
	if (weston_seat_init_keyboard(&s->stream_seat, keymap) < 0) {
		weston_log("qdwin: stream seat init_keyboard failed (port=%u)\n",
			   s->rdp_port);
	}

	/* Advertise touch capability even though qdistro-forward doesn't
	 * translate RDP touch today. A client sees caps=pointer|keyboard|
	 * touch and can react to the presence/absence of touch support
	 * without needing a protocol bump when we later add
	 * inject_touch_* requests. Cost of no-op touch state is negligible. */
	if (weston_seat_init_touch(&s->stream_seat) < 0) {
		weston_log("qdwin: stream seat init_touch failed (port=%u)\n",
			   s->rdp_port);
	}

	s->seat_inited = 1;
	weston_log("qdwin: stream seat '%s' created\n", seat_name);
}

static void
qdwin_stream_seat_release(struct qdwin_view_stream *s)
{
	if (!s->seat_inited)
		return;
	/* IMPL-19: end the confinement grab BEFORE releasing the pointer, so we
	 * don't leave a grab pointing at a freed pointer. */
	qdwin_stream_confine_grab_end(s);
	weston_seat_release_touch(&s->stream_seat);
	weston_seat_release_keyboard(&s->stream_seat);
	weston_seat_release_pointer(&s->stream_seat);
	weston_seat_release(&s->stream_seat);
	s->seat_inited = 0;
}

/* Pin (or re-pin) pointer/keyboard focus to the source view. Called on
 * the first inject and on every pointer_motion, so transient focus
 * changes triggered elsewhere in libweston can't let events leak to
 * other surfaces. Cheap when focus is already correct. */
static void
qdwin_stream_seat_assert_focus(struct qdwin_view_stream *s)
{
	if (!s->seat_inited || !s->tl || !s->tl->view || !s->tl->view->surface)
		return;
	if (s->tl->nested_proxy_pending_decision)
		return;

	struct weston_pointer *ptr = weston_seat_get_pointer(&s->stream_seat);
	if (ptr && ptr->focus != s->tl->view)
		weston_pointer_set_focus(ptr, s->tl->view);

	struct weston_keyboard *kbd = weston_seat_get_keyboard(&s->stream_seat);
	if (kbd && kbd->focus != s->tl->view->surface)
		weston_keyboard_set_focus(kbd, s->tl->view->surface);
}

static inline struct timespec
qdwin_ts_from_msec(uint32_t time_msec)
{
	struct timespec ts;
	ts.tv_sec  = time_msec / 1000u;
	ts.tv_nsec = (long)(time_msec % 1000u) * 1000000L;
	return ts;
}

static struct qdwin_view_stream *
qdwin_view_stream_by_token(struct qdwin *qdwin, const char *token)
{
	if (!token || !token[0])
		return NULL;
	struct qdwin_view_stream *s;
	wl_list_for_each(s, &qdwin->view_streams, link) {
		if (s->access_token[0] &&
		    strncmp(s->access_token, token,
			    sizeof s->access_token) == 0)
			return s;
	}
	return NULL;
}

/* qdwin_stream_input_handle_v1 destructor + inject_* */

static void
qdwin_stream_input_handle_resource_destroyed(struct wl_resource *resource)
{
	struct qdwin_view_stream *s = wl_resource_get_user_data(resource);
	if (!s)
		return;
	if (s->input_handle == resource) {
		s->input_handle = NULL;
		s->input_claimed = 0;
		/* The protocol calls the token one-shot: once a claim has been
		 * released it must not be claimable again with the same token
		 * (iso2 `10` E4). Erase it; by_token rejects empty tokens. */
		memset(s->access_token, 0, sizeof s->access_token);
		qdwin_stream_confine_grab_end(s);   /* IMPL-19: drop the focus lock */
		weston_log("qdwin: stream_input handle released "
			   "(rdp_port=%u)\n", s->rdp_port);
	}
}

static void
qdwin_stream_input_handle_destroy(struct wl_client *c, struct wl_resource *r)
{
	(void)c;
	wl_resource_destroy(r);
}

static void
qdwin_stream_input_inject_pointer_motion(
	struct wl_client *c, struct wl_resource *r,
	uint32_t time_msec, wl_fixed_t x, wl_fixed_t y)
{
	(void)c;
	struct qdwin_view_stream *s = wl_resource_get_user_data(r);
	if (!s || !s->qdwin || s->qdwin->locked ||
	    !s->allow_input || !s->seat_inited ||
	    !s->tl || !s->tl->view || !s->tl->view->surface)
		return;                  /* allow_input=0 => read-only: drop the event */

	qdwin_stream_seat_assert_focus(s);

	/* x,y arrive in surface-local logical pixels (wl_fixed). Translate
	 * to compositor-global space via the view's transform. The view's
	 * cached matrix may be dirty between paint cycles if the view
	 * moved, so force an update before the transform call —
	 * weston_coord_surface_to_global asserts !transform.dirty. */
	weston_view_update_transform(s->tl->view);
	struct weston_coord_surface cs = {
		.c = { .x = wl_fixed_to_double(x),
		       .y = wl_fixed_to_double(y) },
		.coordinate_space_id = s->tl->view->surface,
	};
	struct weston_coord_global gpos =
		weston_coord_surface_to_global(s->tl->view, cs);

	struct timespec ts = qdwin_ts_from_msec(time_msec);
	qdwin_notify_motion_absolute(&s->stream_seat, &ts, gpos);

	if (getenv("QDWIN_STREAM_INPUT_DEBUG"))
		weston_log("qdwin: notify_motion_absolute stream=%u "
			   "surf=(%.1f,%.1f) global=(%.1f,%.1f)\n",
			   s->rdp_port,
			   wl_fixed_to_double(x), wl_fixed_to_double(y),
			   gpos.c.x, gpos.c.y);
}

static void
qdwin_stream_input_inject_pointer_button(
	struct wl_client *c, struct wl_resource *r,
	uint32_t time_msec, uint32_t button, uint32_t state)
{
	(void)c;
	struct qdwin_view_stream *s = wl_resource_get_user_data(r);
	if (!s || !s->qdwin || s->qdwin->locked ||
	    !s->allow_input || !s->seat_inited ||
	    !s->tl || !s->tl->view || !s->tl->view->surface)
		return;                  /* allow_input=0 or detached target (toplevel
					  * torn down, stream awaiting forward exit):
					  * fail closed — after the confine grab ends
					  * the default grab would route this to an
					  * UNRELATED window (mm-merge review HIGH) */
	struct timespec ts = qdwin_ts_from_msec(time_msec);
	qdwin_notify_button(&s->stream_seat, &ts, (int32_t)button,
		      state ? WL_POINTER_BUTTON_STATE_PRESSED
			    : WL_POINTER_BUTTON_STATE_RELEASED);
	if (getenv("QDWIN_STREAM_INPUT_DEBUG"))
		weston_log("qdwin: notify_button stream=%u btn=0x%x state=%u\n",
			   s->rdp_port, button, state);
}

static void
qdwin_stream_input_inject_pointer_axis(
	struct wl_client *c, struct wl_resource *r,
	uint32_t time_msec, uint32_t axis, wl_fixed_t value)
{
	(void)c;
	struct qdwin_view_stream *s = wl_resource_get_user_data(r);
	if (!s || !s->qdwin || s->qdwin->locked ||
	    !s->allow_input || !s->seat_inited ||
	    !s->tl || !s->tl->view || !s->tl->view->surface)
		return;                  /* allow_input=0 or detached target (toplevel
					  * torn down, stream awaiting forward exit):
					  * fail closed — after the confine grab ends
					  * the default grab would route this to an
					  * UNRELATED window (mm-merge review HIGH) */
	struct weston_pointer_axis_event ev = {
		.axis = axis,  /* 0=vertical, 1=horizontal (wl_pointer.axis) */
		.value = wl_fixed_to_double(value),
		.has_discrete = false,
		.discrete = 0,
	};
	struct timespec ts = qdwin_ts_from_msec(time_msec);
	qdwin_notify_axis(&s->stream_seat, &ts, &ev);
}

static void
qdwin_stream_input_inject_key(
	struct wl_client *c, struct wl_resource *r,
	uint32_t time_msec, uint32_t key, uint32_t state)
{
	(void)c;
	struct qdwin_view_stream *s = wl_resource_get_user_data(r);
	if (!s || !s->qdwin || s->qdwin->locked ||
	    !s->allow_input || !s->seat_inited ||
	    !s->tl || !s->tl->view || !s->tl->view->surface)
		return;                  /* allow_input=0 or detached target (toplevel
					  * torn down, stream awaiting forward exit):
					  * fail closed — after the confine grab ends
					  * the default grab would route this to an
					  * UNRELATED window (mm-merge review HIGH) */

	qdwin_stream_seat_assert_focus(s);

	struct timespec ts = qdwin_ts_from_msec(time_msec);
	qdwin_notify_key(&s->stream_seat, &ts, key,
		   state ? WL_KEYBOARD_KEY_STATE_PRESSED
			 : WL_KEYBOARD_KEY_STATE_RELEASED,
		   STATE_UPDATE_AUTOMATIC);
	if (getenv("QDWIN_STREAM_INPUT_DEBUG"))
		weston_log("qdwin: notify_key stream=%u key=%u state=%u\n",
			   s->rdp_port, key, state);
}

static void
qdwin_stream_input_inject_modifiers(
	struct wl_client *c, struct wl_resource *r,
	uint32_t depressed, uint32_t latched,
	uint32_t locked, uint32_t group)
{
	(void)c;
	/* RDP "synchronize" conveys LED state (NumLock/CapsLock/ScrollLock)
	 * on keyboard-activation events; it is NOT a xkb modifier mask.
	 * Real modifier keys (Shift/Ctrl/Alt/Super) flow through inject_key
	 * as regular press/release events, which update xkb state via
	 * notify_key. So this request is advisory — we log and drop. If a
	 * future claimant needs explicit modifier overrides, the right
	 * route is to add a new request that takes evdev codes, not masks. */
	struct qdwin_view_stream *s = wl_resource_get_user_data(r);
	if (!s || !s->qdwin || s->qdwin->locked || !s->allow_input)
		return;                  /* allow_input=0 => read-only: drop (completeness) */
	weston_log("qdwin: inject modifiers (advisory) stream=%u "
		   "dep=0x%x lat=0x%x lock=0x%x grp=%u\n",
		   s ? s->rdp_port : 0, depressed, latched, locked, group);
}

/* §6.5 S3c iter3: tie PipeWire frame production to RDP encoder demand.
 * Weston's backend-pipewire only emits a frame when the compositor
 * finishes a paint on the pipewire output, and paints only on damage.
 * A static source view + an RDP client pulling frames at 30 Hz means
 * the client sees exactly one frame forever. request_frame damages
 * the view's surface + schedules a repaint of the pipewire output; a
 * consumer (qdistro-forward's shadow encoder) calls it on its own
 * cadence to drive continuous frame flow. */
static void
qdwin_stream_input_handle_request_frame(
	struct wl_client *c, struct wl_resource *r)
{
	(void)c;
	struct qdwin_view_stream *s = wl_resource_get_user_data(r);
	if (!s || !s->pinned || !s->pw_output)
		return;
	/* J29: 14->16 weston_surface_damage() removed; weston_view_geometry_dirty()
	 * re-dirties the view's paint nodes so the next pipewire-output repaint
	 * pulls a fresh frame (drives continuous frame flow for the shadow encoder). */
	if (s->tl && s->tl->view && s->tl->view->surface)
		weston_view_geometry_dirty(s->tl->view);
	for (int side = 0; side < QDWIN_SIDES; side++) {
		struct weston_view *cv = s->tl ? s->tl->chrome[side].view : NULL;
		if (cv && cv->surface)
			weston_view_geometry_dirty(cv);
	}
	weston_output_schedule_repaint(s->pw_output);
}

static const struct qdwin_stream_input_handle_v1_interface
qdwin_stream_input_handle_impl = {
	.destroy = qdwin_stream_input_handle_destroy,
	.inject_pointer_motion = qdwin_stream_input_inject_pointer_motion,
	.inject_pointer_button = qdwin_stream_input_inject_pointer_button,
	.inject_pointer_axis   = qdwin_stream_input_inject_pointer_axis,
	.inject_key            = qdwin_stream_input_inject_key,
	.inject_modifiers      = qdwin_stream_input_inject_modifiers,
	.request_frame         = qdwin_stream_input_handle_request_frame,
};

/* qdwin_stream_input_v1.claim — token check + handle resource bind. */

static void
qdwin_stream_input_handle_claim(
	struct wl_client *client, struct wl_resource *input_resource,
	uint32_t handle_id, const char *access_token)
{
	struct qdwin *qdwin = wl_resource_get_user_data(input_resource);

	/* Inherit the bound interface version so request_frame (v2) is
	 * callable iff the client asked for v2. */
	uint32_t version = wl_resource_get_version(input_resource);
	struct wl_resource *handle_res = wl_resource_create(
		client, &qdwin_stream_input_handle_v1_interface,
		(int)version, handle_id);
	if (!handle_res) {
		wl_client_post_no_memory(client);
		return;
	}

	struct qdwin_view_stream *s =
		qdwin_view_stream_by_token(qdwin, access_token);
	if (!s) {
		/* Bind handle so destroy works, then post error. */
		wl_resource_set_implementation(
			handle_res, &qdwin_stream_input_handle_impl,
			NULL, NULL);
		wl_resource_post_error(
			input_resource,
			QDWIN_STREAM_INPUT_V1_ERROR_INVALID_TOKEN,
			"claim: token did not match any live stream");
		weston_log("qdwin: stream_input claim INVALID_TOKEN\n");
		return;
	}
	if (s->input_claimed) {
		wl_resource_set_implementation(
			handle_res, &qdwin_stream_input_handle_impl,
			NULL, NULL);
		wl_resource_post_error(
			input_resource,
			QDWIN_STREAM_INPUT_V1_ERROR_ALREADY_CLAIMED,
			"claim: token already consumed (rdp_port=%u)",
			s->rdp_port);
		weston_log("qdwin: stream_input claim ALREADY_CLAIMED "
			   "rdp_port=%u\n", s->rdp_port);
		return;
	}

	pid_t pid = 0; uid_t uid = 0; gid_t gid = 0;
	wl_client_get_credentials(client, &pid, &uid, &gid);
	if (s->forward_pid <= 0 || pid != s->forward_pid) {
		wl_resource_set_implementation(
			handle_res, &qdwin_stream_input_handle_impl,
			NULL, NULL);
		wl_resource_post_error(
			input_resource,
			QDWIN_STREAM_INPUT_V1_ERROR_INVALID_TOKEN,
			"claim: token is reserved for qdistro-forward pid=%d",
			(int)s->forward_pid);
		weston_log("qdwin: stream_input claim PID_MISMATCH "
			   "rdp_port=%u expected=%d peer pid=%d uid=%u\n",
			   s->rdp_port, (int)s->forward_pid, (int)pid,
			   (unsigned)uid);
		return;
	}

	wl_resource_set_implementation(
		handle_res, &qdwin_stream_input_handle_impl, s,
		qdwin_stream_input_handle_resource_destroyed);
	s->input_claimed = 1;
	s->input_handle = handle_res;

	/* IMPL-19: lock the per-stream pointer focus to the source view for the
	 * life of the claim, so injected input can't leak to an overlapping view. */
	qdwin_stream_confine_grab_start(s);

	weston_log("qdwin: stream_input claim OK rdp_port=%u peer pid=%d "
		   "uid=%u\n", s->rdp_port, (int)pid, (unsigned)uid);
}

static const struct qdwin_stream_input_v1_interface qdwin_stream_input_impl = {
	.claim = qdwin_stream_input_handle_claim,
};

static void
bind_qdwin_stream_input(struct wl_client *client, void *data,
			uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *r = wl_resource_create(
		client, &qdwin_stream_input_v1_interface,
		(int)version, id);
	if (!r) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(r, &qdwin_stream_input_impl,
				       qdwin, NULL);
}

/* §6.6 S1 — panel role (layer, placement, work-area exclusive zone).
 *
 * A panel is a shell-owned wl_surface pinned to one output edge,
 * composited above normal toplevels but below popups, reserving an
 * exclusive zone on its edge so xdg-toplevel maximise does not cover
 * it. One qdwin_panel per live `qdwin_panel_v1` resource; panels live
 * on `qdwin::panels`. Panel placement is recomputed on each surface
 * commit (so buffer-size changes reflow the view) and on output
 * add/resize (see qdwin_panels_reposition_all).
 *
 * The exclusive rectangle contributed to work-area accounting:
 *   edge 0 (top)    : reserves a band at output_y .. output_y+thickness
 *   edge 1 (bottom) : reserves a band at output_y+output_h-thickness ..
 *                     output_y+output_h
 *   edge 2 (left)   : reserves a band at output_x .. output_x+thickness
 *   edge 3 (right)  : reserves a band at output_x+output_w-thickness ..
 *                     output_x+output_w
 *
 * Work-area helper qdwin_output_work_area() is used by request_maximize
 * so the client's maximised size excludes all panels on the relevant
 * output.
 */

struct qdwin_panel {
	struct qdwin *qdwin;
	struct wl_resource *resource;   /* qdwin_panel_v1 */
	struct weston_surface *surface;
	struct weston_view *view;
	struct weston_output *output;
	uint32_t edge;
	uint32_t thickness;
	uint32_t exclusive;
	int last_x, last_y, last_w, last_h;
	int geometry_sent;
	struct wl_listener surface_destroy;
	struct wl_listener surface_commit;
	struct wl_list link;            /* qdwin::panels */
};

static void
qdwin_panel_compute_rect(struct qdwin_panel *p,
			 int *ox, int *oy, int *ow, int *oh)
{
	struct weston_output *out = p->output;
	int x = (int)out->pos.c.x;
	int y = (int)out->pos.c.y;
	int w = out->width;
	int h = out->height;
	int t = (int)p->thickness;
	if (t < 0) t = 0;
	switch (p->edge) {
	case 0: /* top */
		*ox = x; *oy = y; *ow = w; *oh = t; break;
	case 1: /* bottom */
		*ox = x; *oy = y + h - t; *ow = w; *oh = t; break;
	case 2: /* left */
		*ox = x; *oy = y; *ow = t; *oh = h; break;
	case 3: /* right */
		*ox = x + w - t; *oy = y; *ow = t; *oh = h; break;
	default:
		*ox = x; *oy = y; *ow = w; *oh = t; break;
	}
}

static void
qdwin_panel_send_geometry(struct qdwin_panel *p, int x, int y, int w, int h)
{
	if (!p->resource)
		return;
	if (p->geometry_sent && p->last_x == x && p->last_y == y &&
	    p->last_w == w && p->last_h == h)
		return;
	p->last_x = x; p->last_y = y; p->last_w = w; p->last_h = h;
	p->geometry_sent = 1;
	qdwin_panel_v1_send_geometry(p->resource, x, y, w, h);
}

static void
qdwin_panel_place(struct qdwin_panel *p)
{
	if (!p->view || !p->output)
		return;
	int x, y, w, h;
	qdwin_panel_compute_rect(p, &x, &y, &w, &h);
	struct weston_coord_global pos = { .c = weston_coord(x, y) };
	weston_view_set_position(p->view, pos);
	/* Map on first commit or after a rebuild. */
	if (p->surface && !weston_surface_is_mapped(p->surface))
		weston_surface_map(p->surface);
	qdwin_panel_send_geometry(p, x, y, w, h);
}

static void
qdwin_panel_surface_commit(struct wl_listener *l, void *data)
{
	struct qdwin_panel *p = wl_container_of(l, p, surface_commit);
	(void)data;
	qdwin_panel_place(p);
	weston_compositor_schedule_repaint(p->qdwin->compositor);
}

static void
qdwin_panel_drop(struct qdwin_panel *p, int send_dismissed)
{
	if (!p) return;
	if (send_dismissed && p->resource)
		qdwin_panel_v1_send_dismissed(p->resource);
	if (p->view) {
		weston_view_destroy(p->view);
		p->view = NULL;
	}
	if (p->surface) {
		wl_list_remove(&p->surface_destroy.link);
		wl_list_remove(&p->surface_commit.link);
		p->surface = NULL;
	}
	wl_list_remove(&p->link);
	wl_list_init(&p->link);
}

static void
qdwin_panel_surface_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_panel *p = wl_container_of(l, p, surface_destroy);
	(void)data;
	/* Surface went away — view is invalid. Drop view + listeners but
	 * keep the qdwin_panel_v1 resource alive until the client destroys
	 * it; fire dismissed so the shell knows. */
	if (p->view) {
		weston_view_destroy(p->view);
		p->view = NULL;
	}
	wl_list_remove(&p->surface_commit.link);
	wl_list_init(&p->surface_commit.link);
	wl_list_remove(&p->surface_destroy.link);
	wl_list_init(&p->surface_destroy.link);
	p->surface = NULL;
	if (p->resource)
		qdwin_panel_v1_send_dismissed(p->resource);
	qdwin_panels_on_output_change(p->qdwin);
}

static void
qdwin_panel_destroy_req(struct wl_client *client,
			struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct qdwin_panel_v1_interface qdwin_panel_impl = {
	.destroy = qdwin_panel_destroy_req,
};

static void
qdwin_panel_resource_destroyed(struct wl_resource *resource)
{
	struct qdwin_panel *p = wl_resource_get_user_data(resource);
	if (!p) return;
	struct qdwin *qdwin = p->qdwin;
	p->resource = NULL;
	qdwin_panel_drop(p, 0);
	free(p);
	/* Maximised toplevels may need to grow into the reclaimed zone. */
	qdwin_panels_on_output_change(qdwin);
}

static void
qdwin_handle_attach_panel(struct wl_client *client,
			  struct wl_resource *resource,
			  uint32_t id,
			  struct wl_resource *surface_resource,
			  uint32_t edge,
			  uint32_t thickness,
			  uint32_t exclusive)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct wl_resource *panel_res;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	panel_res = wl_resource_create(client, &qdwin_panel_v1_interface,
				       wl_resource_get_version(resource), id);
	if (!panel_res) {
		wl_client_post_no_memory(client);
		return;
	}
	if (edge > 3) {
		weston_log("qdwin: attach_panel invalid edge=%u\n", edge);
		wl_resource_destroy(panel_res);
		return;
	}
	struct weston_surface *surface = surface_resource
		? wl_resource_get_user_data(surface_resource) : NULL;
	if (!surface) {
		weston_log("qdwin: attach_panel missing surface\n");
		wl_resource_destroy(panel_res);
		return;
	}
	struct weston_output *out = qdwin_primary_output(qdwin);
	if (!out) {
		weston_log("qdwin: attach_panel no output yet — deferring\n");
		/* Still create the resource; we just don't have an output to
		 * place on. On first output_created we'll reflow. */
	}
	struct qdwin_panel *p = calloc(1, sizeof *p);
	if (!p) {
		wl_client_post_no_memory(client);
		wl_resource_destroy(panel_res);
		return;
	}
	p->qdwin = qdwin;
	p->resource = panel_res;
	p->surface = surface;
	p->output = out;
	p->edge = edge;
	p->thickness = thickness;
	p->exclusive = exclusive;
	wl_list_init(&p->link);
	wl_list_init(&p->surface_destroy.link);
	wl_list_init(&p->surface_commit.link);
	p->surface_destroy.notify = qdwin_panel_surface_destroyed;
	wl_signal_add(&surface->destroy_signal, &p->surface_destroy);
	p->surface_commit.notify = qdwin_panel_surface_commit;
	wl_signal_add(&surface->commit_signal, &p->surface_commit);

	p->view = weston_view_create(surface);
	if (!p->view) {
		weston_log("qdwin: attach_panel weston_view_create failed\n");
		wl_list_remove(&p->surface_destroy.link);
		wl_list_remove(&p->surface_commit.link);
		free(p);
		wl_resource_destroy(panel_res);
		return;
	}
	weston_view_move_to_layer(p->view, &qdwin->panel_layer.view_list);
	wl_list_insert(&qdwin->panels, &p->link);
	wl_resource_set_implementation(panel_res, &qdwin_panel_impl,
				       p, qdwin_panel_resource_destroyed);
	if (out)
		qdwin_panel_place(p);
	weston_log("qdwin: attach_panel edge=%u thickness=%u exclusive=%u "
		   "output=%s\n", edge, thickness, exclusive,
		   out ? out->name : "(none)");
	/* Let existing maximised toplevels re-fit. */
	qdwin_panels_on_output_change(qdwin);
}

/* Compute the work area for `out` = output rect minus all panel
 * exclusive zones on this output (or all panels if their output is
 * not yet assigned, to stay conservative). */
static void
qdwin_output_work_area(struct qdwin *qdwin, struct weston_output *out,
		       int *ox, int *oy, int *ow, int *oh)
{
	if (!out) { *ox = *oy = 0; *ow = *oh = 0; return; }
	int x = (int)out->pos.c.x, y = (int)out->pos.c.y;
	int w = out->width, h = out->height;

	/* Native qdwin panels (qdwin_attach_panel API). */
	struct qdwin_panel *p;
	wl_list_for_each(p, &qdwin->panels, link) {
		if (p->output && p->output != out) continue;
		int t = (int)p->exclusive;
		if (t <= 0) continue;
		switch (p->edge) {
		case 0: y += t; h -= t; break; /* top */
		case 1: h -= t; break;         /* bottom */
		case 2: x += t; w -= t; break; /* left */
		case 3: w -= t; break;         /* right */
		default: break;
		}
	}

	/* zwlr_layer_shell_v1 panels (Phase 1.3). See
	 * qdwin_layer_shell_subtract_zones(), defined later in the file
	 * (after struct qdwin_layer_surface). Stacking-order subtraction
	 * is not modelled — for xdg-toplevel maximize the order is
	 * irrelevant; we just need the union of reserved space removed. */
	qdwin_layer_shell_subtract_zones(qdwin, out, &x, &y, &w, &h);

	if (w < 1) w = 1;
	if (h < 1) h = 1;
	*ox = x; *oy = y; *ow = w; *oh = h;
}

/* Called when outputs change geometry or panels are added/removed.
 * Repositions every panel + nudges maximised toplevels back into the
 * current work area so their visible rect stays correct. */
static void
qdwin_panels_on_output_change(struct qdwin *qdwin)
{
	struct qdwin_panel *p;
	wl_list_for_each(p, &qdwin->panels, link) {
		if (!p->output)
			p->output = qdwin_primary_output(qdwin);
		if (p->output)
			qdwin_panel_place(p);
	}
	/* Re-apply max/restore geometry for any currently-maximised
	 * toplevel. */
	struct qdwin_toplevel *tl;
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		if (!(tl->state & QDWIN_TS_MAXIMIZED))
			continue;
		struct weston_output *out = qdwin_primary_output(qdwin);
		if (!out) continue;
		int wx, wy, ww, wh;
		qdwin_output_work_area(qdwin, out, &wx, &wy, &ww, &wh);
		tl->outer_width = ww;
		tl->outer_height = wh;
		struct weston_coord_global pos = {
			.c = weston_coord(wx, wy),
		};
		weston_view_set_position(tl->view, pos);
		/* Keep panel-driven maximize reflow on the same shadow-aware path
		 * as ordinary maximize/restore. */
		qdwin_toplevel_apply_inset(tl);
		qdwin_toplevel_position_chrome(tl);
	}
	weston_compositor_schedule_repaint(qdwin->compositor);
}

/* §6.6 S2 — notification bubble role.
 *
 * A notification is a shell-owned wl_surface placed on
 * notification_layer (above panels, below popups). Corner-anchored
 * to the primary output with a (offset_x, offset_y) offset. No
 * exclusive zone — bubbles overlay content. Lifecycle is shell-
 * driven: compositor fires `dismissed` on surface destroy or output
 * removal, never on a timeout of its own. */

struct qdwin_notification {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct weston_view *view;
	struct weston_output *output;
	uint32_t anchor;        /* 0=TR 1=TL 2=BR 3=BL */
	uint32_t offset_x;
	uint32_t offset_y;
	struct wl_listener surface_destroy;
	struct wl_listener surface_commit;
	struct wl_list link;
};

static void
qdwin_notification_place(struct qdwin_notification *n)
{
	if (!n->view || !n->output || !n->surface)
		return;
	int ow = n->output->width;
	int oh = n->output->height;
	int ox = (int)n->output->pos.c.x;
	int oy = (int)n->output->pos.c.y;
	int bw = n->surface->width;
	int bh = n->surface->height;
	if (bw <= 0) bw = 1;
	if (bh <= 0) bh = 1;
	int x = ox, y = oy;
	switch (n->anchor) {
	case 0: /* top-right */
		x = ox + ow - bw - (int)n->offset_x;
		y = oy + (int)n->offset_y;
		break;
	case 1: /* top-left */
		x = ox + (int)n->offset_x;
		y = oy + (int)n->offset_y;
		break;
	case 2: /* bottom-right */
		x = ox + ow - bw - (int)n->offset_x;
		y = oy + oh - bh - (int)n->offset_y;
		break;
	case 3: /* bottom-left */
		x = ox + (int)n->offset_x;
		y = oy + oh - bh - (int)n->offset_y;
		break;
	}
	struct weston_coord_global pos = { .c = weston_coord(x, y) };
	weston_view_set_position(n->view, pos);
	if (!weston_surface_is_mapped(n->surface))
		weston_surface_map(n->surface);
}

static void
qdwin_notification_commit(struct wl_listener *l, void *data)
{
	struct qdwin_notification *n = wl_container_of(l, n, surface_commit);
	(void)data;
	qdwin_notification_place(n);
	weston_compositor_schedule_repaint(n->qdwin->compositor);
}

static void
qdwin_notification_surface_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_notification *n =
		wl_container_of(l, n, surface_destroy);
	(void)data;
	if (n->view) {
		weston_view_destroy(n->view);
		n->view = NULL;
	}
	wl_list_remove(&n->surface_commit.link);
	wl_list_init(&n->surface_commit.link);
	wl_list_remove(&n->surface_destroy.link);
	wl_list_init(&n->surface_destroy.link);
	n->surface = NULL;
	if (n->resource)
		qdwin_notification_v1_send_dismissed(n->resource);
}

static void
qdwin_notification_destroy_req(struct wl_client *client,
			       struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct qdwin_notification_v1_interface qdwin_notification_impl = {
	.destroy = qdwin_notification_destroy_req,
};

static void
qdwin_notification_resource_destroyed(struct wl_resource *resource)
{
	struct qdwin_notification *n = wl_resource_get_user_data(resource);
	if (!n) return;
	if (n->view) {
		weston_view_destroy(n->view);
		n->view = NULL;
	}
	if (n->surface) {
		wl_list_remove(&n->surface_commit.link);
		wl_list_remove(&n->surface_destroy.link);
		n->surface = NULL;
	}
	wl_list_remove(&n->link);
	free(n);
}

static void
qdwin_handle_attach_notification(struct wl_client *client,
				 struct wl_resource *resource,
				 uint32_t id,
				 struct wl_resource *surface_resource,
				 uint32_t anchor,
				 uint32_t offset_x,
				 uint32_t offset_y)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	struct wl_resource *note_res = wl_resource_create(
		client, &qdwin_notification_v1_interface,
		wl_resource_get_version(resource), id);
	if (!note_res) {
		wl_client_post_no_memory(client);
		return;
	}
	if (anchor > 3) {
		weston_log("qdwin: attach_notification invalid anchor=%u\n",
			   anchor);
		wl_resource_destroy(note_res);
		return;
	}
	struct weston_surface *surface = surface_resource
		? wl_resource_get_user_data(surface_resource) : NULL;
	if (!surface) {
		weston_log("qdwin: attach_notification missing surface\n");
		wl_resource_destroy(note_res);
		return;
	}
	struct qdwin_notification *n = calloc(1, sizeof *n);
	if (!n) {
		wl_client_post_no_memory(client);
		wl_resource_destroy(note_res);
		return;
	}
	n->qdwin = qdwin;
	n->resource = note_res;
	n->surface = surface;
	n->output = qdwin_primary_output(qdwin);
	n->anchor = anchor;
	n->offset_x = offset_x;
	n->offset_y = offset_y;
	wl_list_init(&n->link);
	wl_list_init(&n->surface_commit.link);
	wl_list_init(&n->surface_destroy.link);
	n->surface_destroy.notify = qdwin_notification_surface_destroyed;
	wl_signal_add(&surface->destroy_signal, &n->surface_destroy);
	n->surface_commit.notify = qdwin_notification_commit;
	wl_signal_add(&surface->commit_signal, &n->surface_commit);
	n->view = weston_view_create(surface);
	if (!n->view) {
		wl_list_remove(&n->surface_destroy.link);
		wl_list_remove(&n->surface_commit.link);
		free(n);
		wl_resource_destroy(note_res);
		return;
	}
	weston_view_move_to_layer(n->view, &qdwin->notification_layer.view_list);
	wl_list_insert(&qdwin->notifications, &n->link);
	wl_resource_set_implementation(note_res, &qdwin_notification_impl,
				       n, qdwin_notification_resource_destroyed);
	qdwin_notification_place(n);
	weston_log("qdwin: attach_notification anchor=%u offset=%u,%u\n",
		   anchor, offset_x, offset_y);
}

/* §6.6 S3/S4 — launcher + switcher role. */

struct qdwin_launcher {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct weston_view *view;
	struct weston_output *output;
	uint32_t kind;             /* 0=launcher 1=switcher */
	struct wl_listener surface_destroy;
	struct wl_listener surface_commit;
	struct wl_list link;
};

static void
qdwin_launcher_place(struct qdwin_launcher *ln)
{
	if (!ln->view || !ln->output || !ln->surface)
		return;
	int bw = ln->surface->width;
	int bh = ln->surface->height;
	if (bw <= 0 || bh <= 0)
		return;
	int ox = (int)ln->output->pos.c.x;
	int oy = (int)ln->output->pos.c.y;
	int x = ox + (ln->output->width - bw) / 2;
	int y = oy + (ln->output->height - bh) / 2;
	struct weston_coord_global pos = { .c = weston_coord(x, y) };
	weston_view_set_position(ln->view, pos);
	if (!weston_surface_is_mapped(ln->surface))
		weston_surface_map(ln->surface);
}

static void
qdwin_launcher_commit(struct wl_listener *l, void *data)
{
	struct qdwin_launcher *ln = wl_container_of(l, ln, surface_commit);
	(void)data;
	qdwin_launcher_place(ln);
	weston_compositor_schedule_repaint(ln->qdwin->compositor);
}

static void
qdwin_launcher_surface_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_launcher *ln = wl_container_of(l, ln, surface_destroy);
	(void)data;
	if (ln->view) { weston_view_destroy(ln->view); ln->view = NULL; }
	wl_list_remove(&ln->surface_commit.link);
	wl_list_init(&ln->surface_commit.link);
	wl_list_remove(&ln->surface_destroy.link);
	wl_list_init(&ln->surface_destroy.link);
	ln->surface = NULL;
	if (ln->resource)
		qdwin_launcher_v1_send_dismissed(ln->resource);
}

static void
qdwin_launcher_destroy_req(struct wl_client *client,
			   struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct qdwin_launcher_v1_interface qdwin_launcher_impl = {
	.destroy = qdwin_launcher_destroy_req,
};

static void
qdwin_launcher_resource_destroyed(struct wl_resource *resource)
{
	struct qdwin_launcher *ln = wl_resource_get_user_data(resource);
	if (!ln) return;
	struct qdwin *qdwin = ln->qdwin;
	uint32_t kind = ln->kind;
	if (ln->view) { weston_view_destroy(ln->view); ln->view = NULL; }
	if (ln->surface) {
		wl_list_remove(&ln->surface_commit.link);
		wl_list_remove(&ln->surface_destroy.link);
		ln->surface = NULL;
	}
	wl_list_remove(&ln->link);
	free(ln);
	/* B3: drop the keyboard grab when the launcher overlay goes
	 * away. Locker takes a separate grab on its own attach path. */
	if (qdwin && kind == 0 &&
	    qdwin->overlay_grab_active &&
	    qdwin->overlay_grab_role == 0)
		qdwin_overlay_grab_end(qdwin);
}

/* §6.6 S3/S4 keybinding handlers. The compositor owns the key grab
 * and fires shell events; the shell turns events into attach_launcher
 * calls. If no shell is bound, the bindings are a logged no-op. */

static void
qdwin_on_launcher_key(struct weston_keyboard *kb,
		      const struct timespec *t,
		      uint32_t key, void *data)
{
	struct qdwin *qdwin = data;
	(void)kb; (void)t; (void)key;
	if (!qdwin->shell_resource) {
		weston_log("qdwin: launcher key pressed; no shell bound\n");
		return;
	}
	weston_log("qdwin: launcher_requested\n");
	qdwin_shell_v1_send_launcher_requested(qdwin->shell_resource);
}

/* §6.6 S3/S4 switcher keyboard grab.
 *
 * Why a grab and not the original modifier-release binding:
 *   weston_compositor_add_modifier_binding(MODIFIER_ALT, ...) only fires
 *   when the modifier was pressed alone for the entire hold. Pressing
 *   Tab during Alt-hold disqualifies the modifier-release fire — exactly
 *   the case we need for Alt+Tab. Verified 2026-04-30 by capturing
 *   /dev/input/event0 with evtest while user pressed Alt+Tab+release:
 *   all four events arrived in order, but the binding never fired.
 *
 * The grab is installed from qdwin_on_switcher_key the first time the
 * user presses Alt+Tab, and stays active until Alt is released
 * (modifiers callback) or any non-Tab key is pressed (key callback,
 * cancel path). While active:
 *   - Tab presses → switcher_next event (cycle)
 *   - Shift+Tab presses → switcher_next(-1)
 *   - Esc → switcher_dismiss (cancel without commit) — TODO if needed
 *   - Other keys → end grab + commit (mirrors gnome behaviour)
 *   - Modifier change with Alt no longer set → end grab + commit
 *
 * The grab eats Tab events so apps don't see them; other keys are
 * delivered after the grab ends + commit fires.
 */
static void qdwin_switcher_grab_end(struct qdwin *qdwin);

static void
qdwin_switcher_grab_key(struct weston_keyboard_grab *grab,
			const struct weston_key_event *ke)
{
	/* J29: 14->16 key/state now come from the event struct (time dropped). */
	uint32_t key = ke->key;
	uint32_t state_w = ke->key_state;
	struct qdwin *qdwin = wl_container_of(grab, qdwin, switcher_grab);
	if (!qdwin->shell_resource)
		return;
	if (state_w != WL_KEYBOARD_KEY_STATE_PRESSED)
		return;
	if (key == KEY_TAB) {
		struct weston_keyboard *kb = grab->keyboard;
		uint32_t shift_mask = (kb->xkb_info && kb->xkb_info->shift_mod != XKB_MOD_INVALID)
			? (1u << kb->xkb_info->shift_mod) : 0;
		int dir = (shift_mask && (kb->modifiers.mods_depressed & shift_mask))
			? -1 : 1;
		weston_log("qdwin: switcher_next dir=%d\n", dir);
		qdwin_shell_v1_send_switcher_next(qdwin->shell_resource, dir);
		return;
	}
	/* Any non-Tab key cancels with commit (matches gnome behaviour). */
	qdwin_switcher_grab_end(qdwin);
	weston_log("qdwin: switcher_commit cause=non-tab-key\n");
	qdwin_shell_v1_send_switcher_commit(qdwin->shell_resource);
}

static void
qdwin_switcher_grab_modifiers(struct weston_keyboard_grab *grab,
			      uint32_t serial,
			      uint32_t mods_depressed,
			      uint32_t mods_latched,
			      uint32_t mods_locked,
			      uint32_t group)
{
	struct qdwin *qdwin = wl_container_of(grab, qdwin, switcher_grab);
	struct weston_keyboard *kb = grab->keyboard;
	(void)serial; (void)mods_latched; (void)mods_locked; (void)group;

	/* Forward modifier changes to clients (default behaviour) so
	 * downstream apps see Shift/Ctrl/etc. transitions. */
	weston_keyboard_send_modifiers(kb, serial, mods_depressed,
				       mods_latched, mods_locked, group);

	if (!kb->xkb_info || kb->xkb_info->alt_mod == XKB_MOD_INVALID)
		return;
	uint32_t alt_mask = 1u << kb->xkb_info->alt_mod;
	if (mods_depressed & alt_mask)
		return;     /* Alt still held */
	/* Alt released → commit + end grab. */
	weston_log("qdwin: switcher_commit cause=alt-released\n");
	if (qdwin->shell_resource)
		qdwin_shell_v1_send_switcher_commit(qdwin->shell_resource);
	qdwin_switcher_grab_end(qdwin);
}

static void
qdwin_switcher_grab_cancel(struct weston_keyboard_grab *grab)
{
	struct qdwin *qdwin = wl_container_of(grab, qdwin, switcher_grab);
	qdwin->switcher_grab_active = 0;
}

static const struct weston_keyboard_grab_interface qdwin_switcher_grab_iface = {
	qdwin_switcher_grab_key,
	qdwin_switcher_grab_modifiers,
	qdwin_switcher_grab_cancel,
};

static void
qdwin_switcher_grab_start(struct qdwin *qdwin, struct weston_keyboard *kb)
{
	if (qdwin->switcher_grab_active || !kb)
		return;
	qdwin->switcher_grab.interface = &qdwin_switcher_grab_iface;
	qdwin->switcher_grab.keyboard = kb;
	weston_keyboard_start_grab(kb, &qdwin->switcher_grab);
	qdwin->switcher_grab_active = 1;
}

static void
qdwin_switcher_grab_end(struct qdwin *qdwin)
{
	if (!qdwin->switcher_grab_active)
		return;
	struct weston_keyboard *kb = qdwin->switcher_grab.keyboard;
	qdwin->switcher_grab_active = 0;
	if (kb)
		weston_keyboard_end_grab(kb);
}

/* B3+B4 overlay grab — keyboard input for visible launcher / locker.
 * The compositor grabs keys while the overlay is alive and forwards
 * each press as qdwin_shell_v1.overlay_key(role, sym, utf8, state).
 * The shell decodes utf8 for printable chars (Compose/dead-key safe)
 * and matches sym for named keys.
 */
static void
qdwin_overlay_grab_key(struct weston_keyboard_grab *grab,
		       const struct weston_key_event *ke)
{
	/* J29: 14->16 key/state now come from the event struct (time dropped). */
	uint32_t key = ke->key;
	uint32_t state_w = ke->key_state;
	struct qdwin *qdwin = wl_container_of(grab, qdwin, overlay_grab);
	if (state_w != WL_KEYBOARD_KEY_STATE_PRESSED)
		return;     /* releases absorbed; v17 forwards press only */
	struct weston_keyboard *kb = grab->keyboard;
	if (!kb || !kb->xkb_state.state)
		return;
	/* keycode in evdev space; xkb adds the 8 X11 offset. */
	xkb_keycode_t kc = key + 8;
	xkb_keysym_t sym = xkb_state_key_get_one_sym(kb->xkb_state.state, kc);
	char utf8[16] = {0};
	xkb_state_key_get_utf8(kb->xkb_state.state, kc, utf8, sizeof utf8);
	weston_log("qdwin: overlay_key role=%u sym=%u utf8=\"%s\" state=PRESSED\n",
		   qdwin->overlay_grab_role, (uint32_t)sym, utf8);
	/* Security boundary: locker keystrokes go to the locker process,
	 * NOT the shell. The shell never observes the password buffer.
	 * See qdlocker/tests/gui/05-keystroke-isolation.md. The fallback
	 * to the shell remains for launcher/switcher overlays (roles 0/1)
	 * which qdshell still owns. */
	if (qdwin->overlay_grab_role == 2 /* locker */ && qdwin->locker_resource) {
		qdwin_locker_v1_send_overlay_key(qdwin->locker_resource,
						 (uint32_t)sym, utf8);
		return;
	}
	if (qdwin->shell_resource)
		qdwin_shell_v1_send_overlay_key(qdwin->shell_resource,
						qdwin->overlay_grab_role,
						(uint32_t)sym, utf8,
						WL_KEYBOARD_KEY_STATE_PRESSED);
}

static void
qdwin_overlay_grab_modifiers(struct weston_keyboard_grab *grab,
			     uint32_t serial,
			     uint32_t mods_depressed,
			     uint32_t mods_latched,
			     uint32_t mods_locked,
			     uint32_t group)
{
	struct qdwin *qdwin = wl_container_of(grab, qdwin, overlay_grab);
	/* Locker containment (role=2): the locker receives keystrokes as
	 * qdwin_locker_v1.overlay_key with the *already shifted* utf8 resolved
	 * from the compositor's own xkb_state (see qdwin_overlay_grab_key), so
	 * it never needs wl_keyboard.modifiers — and the grab steals key events
	 * from normal focus delivery, so wl_keyboard.key is not delivered to any
	 * client either. But weston_keyboard_send_modifiers() goes to the
	 * keyboard-/pointer-focus client, which during the locker crash→rebind
	 * window can still be a hidden desktop client (the old lock toplevel was
	 * demoted, the fresh one not yet promoted/focused). Forwarding modifier
	 * transitions (Shift/Ctrl/Alt) there would leak input state to a desktop
	 * client while the screen is locked — exactly what the default-grab
	 * locked guard (qdwin_proxy_default_grab_modifiers) drops. So suppress
	 * modifier delivery entirely for the locker role; keep forwarding it for
	 * the launcher/switcher overlays (roles 0/1), which qdshell owns and are
	 * not lock-screen security boundaries. The compositor's xkb_state is
	 * updated by weston core before this callback regardless, so suppressing
	 * delivery does NOT change the shifted utf8 the locker receives. */
	if (qdwin->overlay_grab_role == 2 /* locker */)
		return;
	/* Forward modifier state to clients so e.g. Shift on a launcher
	 * field follows the right xkb path. */
	weston_keyboard_send_modifiers(grab->keyboard, serial,
				       mods_depressed, mods_latched,
				       mods_locked, group);
}

static void
qdwin_overlay_grab_cancel(struct weston_keyboard_grab *grab)
{
	struct qdwin *qdwin = wl_container_of(grab, qdwin, overlay_grab);
	qdwin->overlay_grab_active = 0;
}

static const struct weston_keyboard_grab_interface qdwin_overlay_grab_iface = {
	qdwin_overlay_grab_key,
	qdwin_overlay_grab_modifiers,
	qdwin_overlay_grab_cancel,
};

static void
qdwin_overlay_grab_start(struct qdwin *qdwin, uint32_t role)
{
	/* Pick the default seat's keyboard. Multi-seat overlays would
	 * need per-seat grabs; out of scope for v17. */
	struct weston_seat *seat;
	struct weston_keyboard *kb = NULL;
	wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
		kb = weston_seat_get_keyboard(seat);
		if (kb) break;
	}
	if (!kb)
		return;
	if (qdwin->overlay_grab_active) {
		/* Already grabbed — just update the role (e.g. launcher
		 * → locker takeover). The grab struct itself stays. */
		qdwin->overlay_grab_role = role;
		return;
	}
	qdwin->overlay_grab.interface = &qdwin_overlay_grab_iface;
	qdwin->overlay_grab.keyboard = kb;
	qdwin->overlay_grab_role = role;
	weston_keyboard_start_grab(kb, &qdwin->overlay_grab);
	qdwin->overlay_grab_active = 1;
}

static void
qdwin_overlay_grab_end(struct qdwin *qdwin)
{
	if (!qdwin->overlay_grab_active)
		return;
	struct weston_keyboard *kb = qdwin->overlay_grab.keyboard;
	uint32_t role = qdwin->overlay_grab_role;
	qdwin->overlay_grab_active = 0;
	if (kb) {
		weston_keyboard_end_grab(kb);

		/* The locker grab deliberately suppresses wl_keyboard.modifiers
		 * while locked so Ctrl/Alt/Shift state cannot leak to the hidden
		 * desktop client. libweston still updates kb->modifiers internally,
		 * but ending a grab does not replay that current state to the focused
		 * client. Without this snapshot the client retains the Ctrl+Alt from
		 * the lock chord and ordinary post-unlock typing becomes terminal
		 * control sequences. Resynchronise only after a normal unlock. A
		 * locker crash ends the grab while qdwin->locked remains true; sending
		 * there would violate the fail-secure input-containment contract. */
		if (role == 2 && !qdwin->locked) {
			uint32_t serial = wl_display_next_serial(
				qdwin->compositor->wl_display);
			weston_keyboard_send_modifiers(
				kb, serial,
				kb->modifiers.mods_depressed,
				kb->modifiers.mods_latched,
				kb->modifiers.mods_locked,
				kb->modifiers.group);
			weston_log("qdwin: locker modifier state resynchronized "
				   "after unlock\n");
		}
	}
}

static void
qdwin_on_switcher_key(struct weston_keyboard *kb,
		      const struct timespec *t,
		      uint32_t key, void *data)
{
	struct qdwin *qdwin = data;
	(void)t; (void)key;
	if (!qdwin->shell_resource) {
		weston_log("qdwin: switcher key pressed; no shell bound\n");
		return;
	}
	/* Just install the grab. The grab's key callback fires
	 * switcher_next for this Tab press AND subsequent ones — sending
	 * switcher_next from here too would double-fire (one from
	 * binding, one from grab) and net-cancel on a 2-window setup. */
	qdwin_switcher_grab_start(qdwin, kb);
}

static void
qdwin_on_switcher_back_key(struct weston_keyboard *kb,
			   const struct timespec *t,
			   uint32_t key, void *data)
{
	struct qdwin *qdwin = data;
	(void)t; (void)key;
	if (!qdwin->shell_resource) {
		weston_log("qdwin: switcher back-key pressed; no shell bound\n");
		return;
	}
	/* See qdwin_on_switcher_key — grab's key callback also handles
	 * Shift+Tab via the modifier check, so don't send here. */
	qdwin_switcher_grab_start(qdwin, kb);
}

static void
qdwin_on_alt_released(struct weston_keyboard *kb,
		      enum weston_keyboard_modifier mod, void *data)
{
	/* Kept as a fallback for the alone-Alt press-release path
	 * (user dismisses an idle switcher with a tap). With the
	 * keyboard grab installed on first Alt+Tab, the typical chord
	 * path is handled by qdwin_switcher_grab_modifiers. */
	struct qdwin *qdwin = data;
	(void)kb;
	if (mod != MODIFIER_ALT || !qdwin->shell_resource)
		return;
	weston_log("qdwin: switcher_commit cause=mod-fallback\n");
	qdwin_shell_v1_send_switcher_commit(qdwin->shell_resource);
}

/* §6.6 S5 full — Ctrl+Alt+L fires the manual-lock event. The shell
 * owns the lock decision (spec/04 §PyQt-locker: "locker is a subsystem
 * of the admin compositor"); the compositor only relays the keystroke
 * so the shell can show an auth UI and call set_locked(1) itself. */
static void
qdwin_on_lock_key(struct weston_keyboard *kb,
		  const struct timespec *t,
		  uint32_t key, void *data)
{
	struct qdwin *qdwin = data;
	(void)kb; (void)t; (void)key;
	/* `reason=3` is the manual hotkey (XML lock_requested enum).
	 * Include in the log so tests that grep for the reason can
	 * distinguish this from the idle / lid / suspend paths once
	 * those are wired. */
	weston_log("qdwin: lock_requested reason=3=manual\n");
	/* Fan out to whichever lock client is bound. Locker is the new
	 * path (qdlocker); shell is the deprecated path. When a locker
	 * is bound it takes the event exclusively — sending to both
	 * causes both to call set_locked(1) and double-fires the state
	 * machine. Until qdshell's Modules/LockScreen is deleted, the
	 * shell-only branch remains as a fallback for sessions without
	 * a peer locker. */
	if (qdwin->locker_resource) {
		qdwin_locker_v1_send_lock_requested(qdwin->locker_resource,
			3 /* reason=manual, per XML */);
	} else if (qdwin->shell_resource &&
		   wl_resource_get_version(qdwin->shell_resource) >= 7) {
		qdwin_shell_v1_send_lock_requested(qdwin->shell_resource);
	} else {
		weston_log("qdwin: lock key pressed; no locker or shell bound\n");
	}
}

static void
qdwin_handle_attach_launcher(struct wl_client *client,
			     struct wl_resource *resource,
			     uint32_t id,
			     struct wl_resource *surface_resource,
			     uint32_t kind)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	struct wl_resource *ln_res = wl_resource_create(
		client, &qdwin_launcher_v1_interface,
		wl_resource_get_version(resource), id);
	if (!ln_res) { wl_client_post_no_memory(client); return; }
	struct weston_surface *surface = surface_resource
		? wl_resource_get_user_data(surface_resource) : NULL;
	if (!surface) {
		wl_resource_destroy(ln_res);
		return;
	}
	struct qdwin_launcher *ln = calloc(1, sizeof *ln);
	if (!ln) {
		wl_client_post_no_memory(client);
		wl_resource_destroy(ln_res);
		return;
	}
	ln->qdwin = qdwin;
	ln->resource = ln_res;
	ln->surface = surface;
	ln->output = qdwin_primary_output(qdwin);
	ln->kind = kind;
	wl_list_init(&ln->link);
	wl_list_init(&ln->surface_commit.link);
	wl_list_init(&ln->surface_destroy.link);
	ln->surface_destroy.notify = qdwin_launcher_surface_destroyed;
	wl_signal_add(&surface->destroy_signal, &ln->surface_destroy);
	ln->surface_commit.notify = qdwin_launcher_commit;
	wl_signal_add(&surface->commit_signal, &ln->surface_commit);
	ln->view = weston_view_create(surface);
	if (!ln->view) {
		wl_list_remove(&ln->surface_destroy.link);
		wl_list_remove(&ln->surface_commit.link);
		free(ln);
		wl_resource_destroy(ln_res);
		return;
	}
	weston_view_move_to_layer(ln->view, &qdwin->launcher_layer.view_list);
	wl_list_insert(&qdwin->launchers, &ln->link);
	wl_resource_set_implementation(ln_res, &qdwin_launcher_impl,
				       ln, qdwin_launcher_resource_destroyed);
	qdwin_launcher_place(ln);
	weston_log("qdwin: attach_launcher kind=%u\n", kind);
	/* B3: install overlay keyboard grab for kind=0 (launcher). The
	 * switcher (kind=1) already has its own switcher_grab driven by
	 * Alt+Tab; double-grabbing would conflict. v17+ clients handle
	 * the overlay_key event we'll start emitting; older shells just
	 * see the grab eat their input — same effective behaviour as
	 * before this change (overlay was never typeable).
	 */
	if (kind == 0 &&
	    wl_resource_get_version(resource) >= 17) {
		qdwin_overlay_grab_start(qdwin, /* role=launcher */ 0);
	}
}

/* §6.6 S5 — lock surface + set_locked. */

static void
qdwin_hide_non_lock_layers(struct qdwin *qdwin)
{
	weston_log("qdwin: hiding non-lock layers for lock\n");
	weston_layer_unset_position(&qdwin->background_layer);
	weston_layer_unset_position(&qdwin->normal_layer);
	weston_layer_unset_position(&qdwin->panel_layer);
	weston_layer_unset_position(&qdwin->notification_layer);
	weston_layer_unset_position(&qdwin->launcher_layer);
	weston_layer_unset_position(&qdwin->popup_layer);
	for (int i = 0; i < 4; i++)
		weston_layer_unset_position(&qdwin->layer_shell_layer[i]);
}

static void
qdwin_show_non_lock_layers(struct qdwin *qdwin)
{
	weston_log("qdwin: restoring non-lock layers after unlock\n");
	weston_layer_set_position(&qdwin->background_layer,
				  WESTON_LAYER_POSITION_BACKGROUND);
	weston_layer_set_position(&qdwin->normal_layer,
				  WESTON_LAYER_POSITION_NORMAL);
	weston_layer_set_position(&qdwin->panel_layer, QDWIN_LAYER_POS_PANEL);
	weston_layer_set_position(&qdwin->notification_layer, QDWIN_LAYER_POS_NOTIFICATION);
	weston_layer_set_position(&qdwin->launcher_layer, QDWIN_LAYER_POS_LAUNCHER);
	weston_layer_set_position(&qdwin->popup_layer,
				  WESTON_LAYER_POSITION_UI);
	weston_layer_set_position(&qdwin->layer_shell_layer[0], QDWIN_LAYER_POS_LSHELL_BG);
	weston_layer_set_position(&qdwin->layer_shell_layer[1],
				  WESTON_LAYER_POSITION_BOTTOM_UI);
	weston_layer_set_position(&qdwin->layer_shell_layer[2], QDWIN_LAYER_POS_LSHELL_TOP);
	weston_layer_set_position(&qdwin->layer_shell_layer[3],
				  WESTON_LAYER_POSITION_TOP_UI);
}

static void
qdwin_lock_surface_place(struct qdwin *qdwin)
{
	if (!qdwin->lock_view || !qdwin->lock_surface)
		return;
	struct weston_output *out = qdwin_primary_output(qdwin);
	if (!out) return;
	struct weston_coord_global pos = { .c = weston_coord(out->pos.c.x,
							     out->pos.c.y) };
	weston_view_set_position(qdwin->lock_view, pos);
	if (!weston_surface_is_mapped(qdwin->lock_surface))
		weston_surface_map(qdwin->lock_surface);
}

static void
qdwin_lock_surface_commit_cb(struct wl_listener *l, void *data)
{
	struct qdwin *qdwin = wl_container_of(l, qdwin, lock_surface_commit);
	(void)data;
	qdwin_lock_surface_place(qdwin);
	if (qdwin->locked)
		qdwin_lock_curtain_to_bottom(qdwin);
	weston_compositor_schedule_repaint(qdwin->compositor);
}

static void
qdwin_lock_surface_destroyed_cb(struct wl_listener *l, void *data)
{
	struct qdwin *qdwin = wl_container_of(l, qdwin, lock_surface_destroy);
	(void)data;
	if (qdwin->lock_view && !qdwin->lock_view_is_toplevel) {
		weston_view_destroy(qdwin->lock_view);
		qdwin->lock_view = NULL;
	}
	if (qdwin->lock_view_is_toplevel)
		qdwin->lock_view = NULL;
	qdwin->lock_view_is_toplevel = 0;
	wl_list_remove(&qdwin->lock_surface_commit.link);
	wl_list_init(&qdwin->lock_surface_commit.link);
	wl_list_remove(&qdwin->lock_surface_destroy.link);
	wl_list_init(&qdwin->lock_surface_destroy.link);
	qdwin->lock_surface = NULL;
	/* Send the dismiss opcode on whichever interface owns this
	 * lock_resource. Sending the shell's dismissed event on a
	 * qdwin_locker_surface_v1 resource posts a protocol error and
	 * tears down the locker connection. */
	if (qdwin->lock_resource) {
		if (qdwin->lock_resource_is_locker) {
			/* qdwin_locker_surface_v1 has no dismissed event;
			 * the client observes the destroy via wl_resource
			 * tracking + the next locked_changed=0. */
		} else {
			qdwin_lock_surface_v1_send_dismissed(qdwin->lock_resource);
		}
	}
	if (qdwin->locked && !qdwin->lock_resource_reattach_in_progress) {
		/* F9 fail-secure: the lock surface was destroyed while the
		 * compositor is still locked and no reattach is in flight —
		 * i.e. the locker crashed/was killed, NOT an explicit
		 * set_locked(0) (which clears `locked` first, so we would not
		 * reach this branch). Do NOT auto-unlock: hold the lock so the
		 * desktop stays hidden behind the black lock screen until a
		 * fresh locker (systemd Restart=always) rebinds and reattaches.
		 * This mirrors qdwin_locker_resource_destroy(). Demote any
		 * promoted lock toplevel and force a repaint so the now-empty
		 * lock layer is composited to black (demote only repaints when
		 * a toplevel exists; the legacy shell lock path uses a raw
		 * view, so repaint explicitly). */
		qdwin_demote_lock_toplevel(qdwin, "lock-surface-destroy");
		/* Reassert the black curtain so the now-empty lock layer is
		 * actually composited to black (the demoted toplevel is gone;
		 * without the curtain the renderer would leave stale pixels). */
		qdwin_install_lock_curtain(qdwin);
		weston_compositor_schedule_repaint(qdwin->compositor);
		weston_log("qdwin: lock held (fail-secure) "
			   "cause=lock-surface-destroy — awaiting fresh locker\n");
	}
	/* B4: drop overlay keyboard grab when locker goes away. */
	if (qdwin->overlay_grab_active && qdwin->overlay_grab_role == 2)
		qdwin_overlay_grab_end(qdwin);
}

static void
qdwin_lock_surface_destroy_req(struct wl_client *client,
			       struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct qdwin_lock_surface_v1_interface qdwin_lock_surface_impl = {
	.destroy = qdwin_lock_surface_destroy_req,
};

/* Locker-side surface vtable. The locker XML declares a distinct
 * qdwin_locker_surface_v1 type (separate from the shell's
 * qdwin_lock_surface_v1) so wayland-scanner doesn't emit duplicate
 * struct definitions when both protocols are compiled in. The
 * underlying state is the same — both vtables share the
 * qdwin->lock_* fields and the qdwin_lock_surface_resource_destroyed
 * teardown. */
static void
qdwin_locker_surface_ack_configure(struct wl_client *client,
				   struct wl_resource *resource,
				   uint32_t serial)
{
	(void)client; (void)resource; (void)serial;
}

static const struct qdwin_locker_surface_v1_interface qdwin_locker_surface_impl = {
	.destroy = qdwin_lock_surface_destroy_req,
	.ack_configure = qdwin_locker_surface_ack_configure,
};

static void
qdwin_lock_surface_resource_destroyed(struct wl_resource *resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	if (!qdwin) return;
	qdwin->lock_resource = NULL;
	if (qdwin->lock_view && !qdwin->lock_view_is_toplevel) {
		weston_view_destroy(qdwin->lock_view);
		qdwin->lock_view = NULL;
	}
	if (qdwin->lock_view_is_toplevel)
		qdwin->lock_view = NULL;
	qdwin->lock_view_is_toplevel = 0;
	if (qdwin->lock_surface) {
		wl_list_remove(&qdwin->lock_surface_commit.link);
		wl_list_remove(&qdwin->lock_surface_destroy.link);
		qdwin->lock_surface = NULL;
	}
	if (qdwin->locked && !qdwin->lock_resource_reattach_in_progress) {
		/* F9 fail-secure: lock resource destroyed while still locked
		 * and no reattach in flight (locker crash, not an explicit
		 * unlock — set_locked(0) clears `locked` first). Hold the lock
		 * rather than auto-unlock; mirror qdwin_locker_resource_destroy()
		 * and qdwin_lock_surface_destroyed_cb(). Demote any promoted
		 * toplevel and repaint so the empty lock layer goes black. */
		qdwin_demote_lock_toplevel(qdwin, "lock-resource-destroy");
		weston_compositor_schedule_repaint(qdwin->compositor);
		weston_log("qdwin: lock held (fail-secure) "
			   "cause=lock-resource-destroy — awaiting fresh locker\n");
	}
	/* Suppress the analogous flap on the surface-destroy callback
	 * by leaving `locked` untouched when a reattach is in flight;
	 * see lock_resource_reattach_in_progress on struct qdwin. */
	qdwin->lock_resource_is_locker = 0;
	/* B4 fix: when the shell calls proxy.destroy() on the lock_surface,
	 * the resource-destroyed handler fires (this one). Without ending
	 * the overlay grab here, the keyboard stays grabbed by an
	 * already-dead role=2 grab; subsequent keys generate overlay_key
	 * events that qdshell drops (lk.locked is False), so input goes
	 * nowhere visible until another grab cycle. */
	if (qdwin->overlay_grab_active && qdwin->overlay_grab_role == 2)
		qdwin_overlay_grab_end(qdwin);
}

/* v18 attach_background — shell-owned fullscreen surface that
 * replaces the compositor curtain on the background_layer. The shell
 * sets a default cursor on pointer.enter so the SPICE viewer always
 * has cursor data to render. */
struct qdwin_background {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct weston_view *view;
	struct wl_listener surface_destroy;
};

static void
qdwin_background_send_geometry(struct qdwin_background *bg)
{
	struct weston_output *out = qdwin_primary_output(bg->qdwin);
	if (!out || !bg->resource)
		return;
	qdwin_background_v1_send_geometry(bg->resource,
					  (int)out->pos.c.x,
					  (int)out->pos.c.y,
					  out->width, out->height);
}

static void
qdwin_background_place(struct qdwin_background *bg)
{
	struct weston_output *out = qdwin_primary_output(bg->qdwin);
	if (!out || !bg->view)
		return;
	struct weston_coord_global pos = { .c = weston_coord(out->pos.c.x,
							     out->pos.c.y) };
	weston_view_set_position(bg->view, pos);
	if (!weston_surface_is_mapped(bg->surface))
		weston_surface_map(bg->surface);
	weston_view_update_transform(bg->view);
}

static void
qdwin_background_surface_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_background *bg =
		wl_container_of(l, bg, surface_destroy);
	(void)data;
	if (bg->view) {
		weston_view_destroy(bg->view);
		bg->view = NULL;
	}
	wl_list_remove(&bg->surface_destroy.link);
	wl_list_init(&bg->surface_destroy.link);
	bg->surface = NULL;
	if (bg->resource)
		qdwin_background_v1_send_dismissed(bg->resource);
}

static void
qdwin_background_resource_destroyed(struct wl_resource *resource)
{
	struct qdwin_background *bg = wl_resource_get_user_data(resource);
	if (!bg)
		return;
	if (bg->view) {
		weston_view_destroy(bg->view);
		bg->view = NULL;
	}
	if (bg->surface) {
		wl_list_remove(&bg->surface_destroy.link);
		bg->surface = NULL;
	}
	struct qdwin *qdwin = bg->qdwin;
	if (qdwin->shell_background == bg)
		qdwin->shell_background = NULL;
	free(bg);
	/* Bring the compositor curtain back so the desktop isn't
	 * transparent if the shell never reattaches. */
	if (qdwin->background && qdwin->background->view) {
		weston_view_move_to_layer(qdwin->background->view,
					  &qdwin->background_layer.view_list);
		weston_compositor_schedule_repaint(qdwin->compositor);
	}
}

static void
qdwin_background_destroy_req(struct wl_client *client,
			     struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct qdwin_background_v1_interface qdwin_background_impl = {
	.destroy = qdwin_background_destroy_req,
};

static void
qdwin_handle_attach_background(struct wl_client *client,
			       struct wl_resource *resource,
			       uint32_t id,
			       struct wl_resource *surface_resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	struct wl_resource *bg_res = wl_resource_create(
		client, &qdwin_background_v1_interface,
		wl_resource_get_version(resource), id);
	if (!bg_res) { wl_client_post_no_memory(client); return; }
	struct weston_surface *surface = surface_resource
		? wl_resource_get_user_data(surface_resource) : NULL;
	if (!surface) {
		wl_resource_destroy(bg_res);
		return;
	}
	if (qdwin->shell_background) {
		/* Replace any previous background. */
		wl_resource_destroy(qdwin->shell_background->resource);
	}
	struct qdwin_background *bg = calloc(1, sizeof *bg);
	if (!bg) {
		wl_client_post_no_memory(client);
		wl_resource_destroy(bg_res);
		return;
	}
	bg->qdwin = qdwin;
	bg->resource = bg_res;
	bg->surface = surface;
	wl_list_init(&bg->surface_destroy.link);
	bg->surface_destroy.notify = qdwin_background_surface_destroyed;
	wl_signal_add(&surface->destroy_signal, &bg->surface_destroy);
	bg->view = weston_view_create(surface);
	if (!bg->view) {
		wl_list_remove(&bg->surface_destroy.link);
		free(bg);
		wl_resource_destroy(bg_res);
		return;
	}
	weston_view_move_to_layer(bg->view,
				  &qdwin->background_layer.view_list);
	/* Hide the compositor curtain — its weston_view is opaque and
	 * compositor-owned, so weston_compositor_pick_view picks it for
	 * pointer focus instead of our shell-owned bg surface. Detach
	 * from any layer so it isn't composited or focusable. The
	 * curtain stays allocated; we re-add it to the background layer
	 * if the shell background goes away. */
	if (qdwin->background && qdwin->background->view) {
		weston_view_move_to_layer(qdwin->background->view, NULL);
	}
	wl_resource_set_implementation(bg_res, &qdwin_background_impl,
				       bg, qdwin_background_resource_destroyed);
	qdwin->shell_background = bg;
	qdwin_background_place(bg);
	qdwin_background_send_geometry(bg);
	weston_log("qdwin: attach_background output=%s\n",
		   qdwin_primary_output(qdwin) ?
		   qdwin_primary_output(qdwin)->name : "(none)");
}

static void
qdwin_handle_attach_lock_surface(struct wl_client *client,
				 struct wl_resource *resource,
				 uint32_t id,
				 struct wl_resource *surface_resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	struct wl_resource *ls_res = wl_resource_create(
		client, &qdwin_lock_surface_v1_interface,
		wl_resource_get_version(resource), id);
	if (!ls_res) { wl_client_post_no_memory(client); return; }
	struct weston_surface *surface = surface_resource
		? wl_resource_get_user_data(surface_resource) : NULL;
	if (!surface) { wl_resource_destroy(ls_res); return; }
	/* Drop any previous lock surface. */
	if (qdwin->lock_resource)
		wl_resource_destroy(qdwin->lock_resource);

	qdwin->lock_resource = ls_res;
	qdwin->lock_resource_is_locker = 0;
	qdwin->lock_surface = surface;
	qdwin->lock_view_is_toplevel = 0;
	qdwin->lock_toplevel = NULL;
	qdwin->lock_view = weston_view_create(surface);
	if (!qdwin->lock_view) {
		qdwin->lock_surface = NULL;
		qdwin->lock_resource = NULL;
		wl_resource_destroy(ls_res);
		return;
	}
	weston_view_move_to_layer(qdwin->lock_view,
				  &qdwin->lock_layer.view_list);
	wl_list_init(&qdwin->lock_surface_destroy.link);
	wl_list_init(&qdwin->lock_surface_commit.link);
	qdwin->lock_surface_destroy.notify = qdwin_lock_surface_destroyed_cb;
	wl_signal_add(&surface->destroy_signal,
		      &qdwin->lock_surface_destroy);
	qdwin->lock_surface_commit.notify = qdwin_lock_surface_commit_cb;
	wl_signal_add(&surface->commit_signal,
		      &qdwin->lock_surface_commit);
	wl_resource_set_implementation(ls_res, &qdwin_lock_surface_impl,
				       qdwin,
				       qdwin_lock_surface_resource_destroyed);
	qdwin_lock_surface_place(qdwin);
	weston_log("qdwin: attach_lock_surface\n");
	/* Only grab keyboard input while the compositor is actually locked.
	 * The locker may attach its surface during normal unlocked startup; an
	 * eager role=2 grab there would swallow global bindings like Ctrl+Space. */
	if (qdwin->locked && wl_resource_get_version(resource) >= 17)
		qdwin_overlay_grab_start(qdwin, /* role=locker */ 2);
}

static void
qdwin_handle_set_locked(struct wl_client *client,
			struct wl_resource *resource,
			uint32_t locked)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	int want = locked ? 1 : 0;
	if (qdwin->locked == want) return;
	if (want && !qdwin->lock_surface) {
		weston_log("qdwin: set_locked(1) without lock surface — "
			   "attach_lock_surface first\n");
		return;
	}
	qdwin->locked = want;
	if (want) {
		struct qdwin_view_stream *stream, *next;
		wl_list_for_each_safe(stream, next, &qdwin->view_streams, link)
			qdwin_view_stream_terminate(stream, "compositor locked",
						    stream->forward_pid);
		if (wl_resource_get_version(resource) >= 17)
			qdwin_overlay_grab_start(qdwin, /* role=locker */ 2);
		qdwin_install_lock_curtain(qdwin);
		qdwin_hide_non_lock_layers(qdwin);
	} else {
		qdwin_remove_lock_curtain(qdwin);
		if (qdwin->overlay_grab_active &&
		    qdwin->overlay_grab_role == 2)
			qdwin_overlay_grab_end(qdwin);
		qdwin_show_non_lock_layers(qdwin);
	}
	weston_log("qdwin: set_locked=%d (lock_surface=%p)\n",
		   want, (void*)qdwin->lock_surface);
	weston_log("qdwin: locked_changed=%d cause=set_locked\n", want);
	if (qdwin->shell_resource)
		qdwin_shell_v1_send_locked_changed(qdwin->shell_resource,
						   want);
	weston_compositor_schedule_repaint(qdwin->compositor);
}

/* spec/10: shell rejected the just-fired selection_set. Drop the seat's
 * selection (and the primary selection, when is_primary=1). */
static void
qdwin_handle_clear_selection(struct wl_client *client,
			     struct wl_resource *resource,
			     const char *seat_name,
			     uint32_t is_primary)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct weston_seat *seat;
	(void)client;
	if (!qdwin || !seat_name)
		return;
	/* Shell-role gate: clear_selection is "Sent by the shell" (after a
	 * broker deny verdict) and wipes the seat clipboard / primary
	 * selection. Same privileged class as set_keyboard_focus — gate it so
	 * a pre-bind allowed_uid client holding a qdwin_shell_v1 resource can't
	 * wipe selections before any shell binds. */
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	/* D6: uniform locked gate (same class as fullscreen/tile). A deny
	 * verdict that arrives while locked is deferred until unlock rather
	 * than mutating the seat selection behind the lock screen. */
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	seat = NULL;
	struct weston_seat *s;
	wl_list_for_each(s, &qdwin->compositor->seat_list, link) {
		if (s->seat_name && strcmp(s->seat_name, seat_name) == 0) {
			seat = s;
			break;
		}
	}
	if (!seat) {
		weston_log("qdwin: clear_selection unknown seat=%s\n", seat_name);
		return;
	}
	/* Idempotent (defense-in-depth — clipboard.md "deny-storm robustness").
	 * Clearing an already-empty seat/kind selection is a no-op: skip the
	 * weston_seat_set_selection / primary clear AND its broadcast of
	 * selection(NULL) to every device. The producer-side storm is bounded by
	 * qdshell's deny-clear coalescer; this just makes the redundant tail
	 * cost nothing on the compositor. The no-op path is intentionally SILENT
	 * — emitting a log line per suppressed clear would reintroduce exactly
	 * the journal flood the coalescer exists to prevent (qdwin has no
	 * debug-level log channel separate from weston_log). */
	if (is_primary) {
		struct qdwin_primary_seat *pseat =
			qdwin_primary_seat_find(qdwin, seat);
		if (!pseat || !qdwin_primary_seat_has_source(pseat))
			return;
		qdwin_primary_seat_clear_selection(pseat, 1);
		weston_log("qdwin: clear_selection seat=%s primary cleared\n",
			   seat_name);
	} else {
		if (!seat->selection_data_source)
			return;
		weston_seat_set_selection(seat, NULL,
					  wl_display_next_serial(
						  qdwin->compositor->wl_display));
		weston_log("qdwin: clear_selection seat=%s clipboard cleared\n",
			   seat_name);
	}
}

/* spec/10 v14: admin shell injects keyboard focus on a seat. Used by
 * the qdshell ctrl-socket bats hooks (no real input under sdl-freerdp
 * dummy) and by future click-to-focus on chrome.
 *
 * Two effects:
 *
 *   1. Drops the seat selection (and its primary equivalent) via
 *      weston_seat_set_selection(NULL, fresh_serial) so that a
 *      follow-up set_selection at any serial wins. This dual-
 *      purpose:
 *      - Defense-in-depth on top of qdshell's cross-silo clear.
 *      - Bumps seat->selection_serial. Test/probe sources use
 *        serial=0 in their set_selection calls (no real input
 *        serial available); without the bump, libweston's stale-
 *        serial guard rejects subsequent serial=0 sets after the
 *        first round. The focus_aware_clear from qdshell DOES
 *        clear via shell.clear_selection, but that's a one-shot;
 *        without this bump the seat's serial monotonically grows
 *        each set_selection and quickly outpaces serial=0.
 *
 *   2. Calls weston_keyboard_set_focus(kbd, target_surface). This is
 *      load-bearing: qdwin_emit_selection_set's first source-handle
 *      resolution is `qdwin_toplevel_for_keyboard_focus`, so without
 *      a real focus shift the silo's selection_set lands at admin's
 *      compositor with handle=UINT32_MAX (the wl_client / secctx
 *      fallbacks miss when the source comes via waypipe and the
 *      destination toplevel lives behind a different wl_client).
 *      The previous version of this handler tripped an infinite loop
 *      in libweston — but that was the listener double-add bug fixed
 *      in qdwin_on_seat_updated_caps (one tracker had its
 *      kbd_focus_listener.link relinked into a fresh focus_signal
 *      list while still belonging to the old one). With the listener
 *      tracked exactly once, weston_keyboard_set_focus's
 *      wl_signal_emit walks our listener once and returns.
 *
 * spec/10 v15 limitation, RESOLVED in v16: when a same-silo sink
 * takes focus via this v1 request, the unconditional clear kills the
 * active selection along with bumping the serial. The clean fix lives
 * in `set_keyboard_focus_v2` (since=16): the shell passes the target
 * toplevel's silo identity, qdwin compares with the per-seat last
 * v2 silo, and only clears on cross-silo. See task 050 (a clean v1-
 * level removal attempt that was reverted because it broke phase7-
 * clipboard-gate's serial=0 set chain — the load-bearing role is
 * actually clearing data_source so subsequent serial=0 sets pass
 * weston's stale-serial guard, not the serial bump itself). v1
 * callers keep the unconditional behaviour for backward compat.
 *
 * The focus_signal listener emits seat_focus_changed; if for some
 * reason it didn't fire (no keyboard at install time) we force-emit.
 */
static void
qdwin_handle_set_keyboard_focus(struct wl_client *client,
				struct wl_resource *resource,
				const char *seat_name,
				uint32_t target_handle)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin || !seat_name)
		return;
	/* Shell-role gate: only the bound shell may redirect keyboard focus
	 * and clear seat/primary selections. Without this, before any client
	 * claims the role via bind_as_shell, any allowed_uid client holding a
	 * qdwin_shell_v1 resource could steer focus and wipe selections.
	 * Matches the rest of the shell request surface (move_toplevel_to_
	 * workspace, set_workspace_name, ...). */
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	/* findings F1: refuse focus redirection while locked, matching
	 * request_fullscreen/tile/maximize/etc. which already post ERROR_LOCKED.
	 * The locker routes input via its overlay keyboard grab
	 * (qdwin_overlay_grab_start, role=2), NOT through set_keyboard_focus, so
	 * gating here cannot break unlock; it removes the inconsistency whereby
	 * the shell could steer focus to a background app while the screen is
	 * locked (a latent lock-bypass if grab handling ever changes). */
	if (qdwin->locked) {
		wl_resource_post_error(resource, QDWIN_SHELL_V1_ERROR_LOCKED,
				       "locked");
		return;
	}
	struct weston_seat *seat = NULL;
	struct weston_seat *s;
	wl_list_for_each(s, &qdwin->compositor->seat_list, link) {
		if (s->seat_name && strcmp(s->seat_name, seat_name) == 0) {
			seat = s; break;
		}
	}
	if (!seat) {
		weston_log("qdwin: set_keyboard_focus unknown seat=%s\n",
			   seat_name);
		return;
	}
	struct weston_keyboard *kbd = weston_seat_get_keyboard(seat);
	struct weston_surface *target = NULL;
	struct weston_view *target_view = NULL;
		if (target_handle != UINT32_MAX) {
			struct qdwin_toplevel *tl;
			wl_list_for_each(tl, &qdwin->toplevels, link) {
				if (tl->handle == target_handle) {
					if (tl->nested_proxy_pending_decision) {
						weston_log("qdwin: set_keyboard_focus "
							   "handle=%u ignored — nested-proxy "
							   "waiting for admin decision\n",
							   target_handle);
						return;
					}
					if (tl->view && tl->view->surface) {
						target = tl->view->surface;
						target_view = tl->view;
					}
					break;
			}
		}
		if (!target) {
			weston_log("qdwin: set_keyboard_focus handle=%u "
				   "not mapped\n", target_handle);
			return;
		}
	}
	/* Defense-in-depth + serial bump. See the comment block above
	 * for why this is unconditional even when qdshell's cross-silo
	 * focus_aware_clear path also fires. */
	weston_seat_set_selection(seat, NULL,
				  wl_display_next_serial(
					  qdwin->compositor->wl_display));
	struct qdwin_primary_seat *pseat = qdwin_primary_seat_find(qdwin, seat);
	if (pseat)
		qdwin_primary_seat_clear_selection(pseat, 1);
	if (target_view) {
		/* Activate (not just set keyboard focus): weston_view_activate_input
		 * calls weston_seat_set_keyboard_focus AND emits
		 * compositor->activate_signal, which weston's Xwayland WM uses to set
		 * X11 input focus (xcb_set_input_focus / WM_TAKE_FOCUS). A bare
		 * weston_keyboard_set_focus emits only the keyboard focus_signal, so
		 * XWayland clients got Wayland focus but never X11 focus and received
		 * no keys. Native Wayland clients are unaffected. */
		weston_view_activate_input(target_view, seat,
					   WESTON_ACTIVATE_FLAG_CONFIGURE);
	} else if (kbd) {
		/* Clearing focus (target_handle == UINT32_MAX): no view to activate,
		 * so drop keyboard focus directly. */
		weston_keyboard_set_focus(kbd, target);
	} else {
		weston_log("qdwin: set_keyboard_focus seat=%s has no keyboard "
			   "— wire-only emit\n", seat_name);
	}
	weston_log("qdwin: set_keyboard_focus seat=%s handle=%u\n",
		   seat_name, target_handle);
	/* Always force-emit seat_focus_changed for the target. The
	 * focus_signal listener may have already fired from
	 * weston_keyboard_set_focus, in which case this is a redundant
	 * emit (qdshell's on_seat_focus_changed is idempotent for the
	 * same focused_silo). When kbd is NULL or the focus_signal
	 * iteration didn't run our listener yet, this guarantees the
	 * shell sees the new state. */
	qdwin_emit_seat_focus_changed(qdwin, seat, target_handle);
}

/* spec/10 v16: silo-aware focus injection. See protocol XML and the
 * docblock above qdwin_handle_set_keyboard_focus for the load-bearing
 * role of the unconditional clear in v1.
 *
 * The v2 handler skips the clear when target_silo matches the seat
 * tracker's last_target_silo (same-silo focus move). Both must be
 * non-empty for a same-silo determination — empty target_silo or
 * empty stored silo counts as cross-silo for safety, which preserves
 * the v14 behaviour for callers who don't yet know the silo.
 *
 * UINT32_MAX target_handle (clear focus) resets last_target_silo to
 * NULL regardless of the passed string, so the next non-clear v2 call
 * always counts as cross-silo and clears.
 *
 * The handler intentionally shares everything below the gate with the
 * v1 path (focus change + force-emit seat_focus_changed). On the v15
 * sink test path, the source's seat selection survives the focus
 * move; weston's existing code in weston_keyboard_set_focus then
 * sends the active wl_data_device.selection event to the new focus
 * client, which is exactly what the sink needs to call
 * wl_data_offer.receive against. */
static void
qdwin_handle_set_keyboard_focus_v2(struct wl_client *client,
				   struct wl_resource *resource,
				   const char *seat_name,
				   uint32_t target_handle,
				   const char *target_silo)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin || !seat_name)
		return;
	/* Shell-role gate (see set_keyboard_focus v1 for rationale): only the
	 * bound shell may inject silo-aware keyboard focus / clear selections. */
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	/* findings F1: refuse focus redirection while locked (see v1). */
	if (qdwin->locked) {
		wl_resource_post_error(resource, QDWIN_SHELL_V1_ERROR_LOCKED,
				       "locked");
		return;
	}
	struct weston_seat *seat = NULL;
	struct weston_seat *s;
	wl_list_for_each(s, &qdwin->compositor->seat_list, link) {
		if (s->seat_name && strcmp(s->seat_name, seat_name) == 0) {
			seat = s; break;
		}
	}
	if (!seat) {
		weston_log("qdwin: set_keyboard_focus_v2 unknown seat=%s\n",
			   seat_name);
		return;
	}
	struct weston_keyboard *kbd = weston_seat_get_keyboard(seat);
	struct weston_surface *target = NULL;
	struct weston_view *target_view = NULL;
		if (target_handle != UINT32_MAX) {
			struct qdwin_toplevel *tl;
			wl_list_for_each(tl, &qdwin->toplevels, link) {
				if (tl->handle == target_handle) {
					if (tl->nested_proxy_pending_decision) {
						weston_log("qdwin: set_keyboard_focus_v2 "
							   "handle=%u ignored — nested-proxy "
							   "waiting for admin decision\n",
							   target_handle);
						return;
					}
					if (tl->view && tl->view->surface) {
						target = tl->view->surface;
						target_view = tl->view;
					}
					break;
			}
		}
		if (!target) {
			weston_log("qdwin: set_keyboard_focus_v2 handle=%u "
				   "not mapped\n", target_handle);
			return;
		}
	}

	struct qdwin_seat_tracker *tr =
		qdwin_seat_tracker_for_seat(qdwin, seat);
	const char *prev_silo = qdwin_seat_tracker_silo(tr);
	const char *new_silo = target_silo ? target_silo : "";
	int cross_silo = 1;
	if (target_handle == UINT32_MAX) {
		/* Clearing focus always counts as cross-silo. */
		cross_silo = 1;
	} else if (*new_silo && *prev_silo &&
		   strcmp(new_silo, prev_silo) == 0) {
		cross_silo = 0;
	}

	if (cross_silo) {
		weston_seat_set_selection(seat, NULL,
					  wl_display_next_serial(
						  qdwin->compositor->wl_display));
		struct qdwin_primary_seat *pseat =
			qdwin_primary_seat_find(qdwin, seat);
		if (pseat)
			qdwin_primary_seat_clear_selection(pseat, 1);
	}

	if (target_view) {
		/* weston_view_activate_input calls weston_seat_set_keyboard_focus
		 * (the public helper, so the new focus client still receives the
		 * active selection event via wl_data_device_set_keyboard_focus —
		 * the v2 same-silo data path is preserved) AND emits
		 * compositor->activate_signal, which weston's Xwayland WM uses to
		 * set X11 input focus. Without the activate_signal, XWayland clients
		 * got Wayland focus but no X11 focus and received no keys. */
		weston_view_activate_input(target_view, seat,
					   WESTON_ACTIVATE_FLAG_CONFIGURE);
	} else if (kbd) {
		/* Clearing focus (target_handle == UINT32_MAX): no view to
		 * activate; drop keyboard focus directly via the seat helper. */
		weston_seat_set_keyboard_focus(seat, target);
	} else {
		weston_log("qdwin: set_keyboard_focus_v2 seat=%s has no "
			   "keyboard — wire-only emit\n", seat_name);
	}

	/* Update per-seat last_target_silo. UINT32_MAX clear resets to
	 * NULL so the next non-clear call is cross-silo. */
	qdwin_seat_tracker_set_silo(
		tr, (target_handle == UINT32_MAX || !*new_silo)
		    ? NULL
		    : new_silo);

	weston_log("qdwin: set_keyboard_focus_v2 seat=%s handle=%u "
		   "silo='%s' cross_silo=%d (prev='%s')\n",
		   seat_name, target_handle, new_silo, cross_silo, prev_silo);

	qdwin_emit_seat_focus_changed(qdwin, seat, target_handle);
}

/* ------------------------------------------------------------------
 * v25: live window-manager policy (set_wm_policy / request_fullscreen /
 * request_tile). Focus-follows-mouse, raise-on-click/hover, new-window
 * placement, edge snapping and half-screen tiling. The policy is a
 * single idempotent snapshot pushed by the shell; the compositor stores
 * it and changes behaviour immediately.
 * ------------------------------------------------------------------ */

/* Compositor defaults: the historical pre-v25 behaviour (click-to-focus,
 * raise on click, centre+cascade placement, no snapping). Restored when
 * the shell unbinds so a shell-less compositor keeps the old feel. */
static void
qdwin_wm_policy_set_defaults(struct qdwin_wm_policy *p)
{
	p->focus_policy   = QDWIN_FOCUS_CLICK;
	p->ffm_delay_ms   = 0;
	p->raise_on_click = 1;
	p->raise_on_hover = 0;
	p->placement      = QDWIN_PLACE_CASCADE;
	p->snap_enabled   = 0;
	p->snap_distance  = 16;
}

/* First seat with a pointer (qdwin is single-seat in practice; we walk
 * the list defensively). NULL if no pointer-capable seat exists. */
static struct weston_pointer *
qdwin_first_pointer(struct qdwin *qdwin)
{
	struct weston_seat *seat;
	wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
		struct weston_pointer *ptr = weston_seat_get_pointer(seat);
		if (ptr)
			return ptr;
	}
	return NULL;
}

/* The output whose geometry contains the global coordinate, else the
 * primary output. */
static struct weston_output *
qdwin_output_at_global(struct qdwin *qdwin, double gx, double gy)
{
	struct weston_output *out;
	wl_list_for_each(out, &qdwin->compositor->output_list, link) {
		if (gx >= out->pos.c.x && gx < out->pos.c.x + out->width &&
		    gy >= out->pos.c.y && gy < out->pos.c.y + out->height)
			return out;
	}
	return qdwin_primary_output(qdwin);
}

/* Re-stack a toplevel to the top of the normal layer (only meaningful on
 * the active workspace — raising an off-workspace window would leak it
 * onto the current desktop, so we leave it parked). */
static void
qdwin_toplevel_raise(struct qdwin_toplevel *tl)
{
	if (!tl || !tl->qdwin)
		return;
	if (tl->nested_proxy_pending_decision)
		return;
	if (tl->state & QDWIN_TS_MINIMIZED)
		return;
	if (tl->workspace != tl->qdwin->active_workspace)
		return;
	qdwin_toplevel_move_to_layer(tl, &tl->qdwin->normal_layer);
	weston_compositor_schedule_repaint(tl->qdwin->compositor);
}

/* Set keyboard focus to a toplevel across all seats (mirrors
 * qdwin_toplevel_autofocus_if_ready but driven by policy, not by map),
 * emitting seat_focus_changed and optionally raising it. */
static void
qdwin_toplevel_focus(struct qdwin *qdwin, struct qdwin_toplevel *tl, int raise)
{
	if (!qdwin || !tl || qdwin->locked)
		return;
	if (tl->nested_proxy_pending_decision)
		return;
	if (!tl->view || !tl->view->surface ||
	    !weston_surface_is_mapped(tl->view->surface))
		return;
	if (tl->workspace != qdwin->active_workspace)
		return;
	if (raise)
		qdwin_toplevel_raise(tl);
	struct weston_seat *seat;
	wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
		struct weston_keyboard *kbd = weston_seat_get_keyboard(seat);
		if (kbd && kbd->focus != tl->view->surface) {
			weston_keyboard_set_focus(kbd, tl->view->surface);
			qdwin_emit_seat_focus_changed(qdwin, seat, tl->handle);
		}
	}
}

/* focus-follows-mouse settle timer. Fires wm_policy.ffm_delay_ms after
 * the pointer last moved onto a new toplevel; focuses it only if the
 * pointer is still over the same toplevel (so a fast pass-through doesn't
 * pull focus). */
static int
qdwin_ffm_timer_cb(void *data)
{
	struct qdwin *qdwin = data;
	if (!qdwin || qdwin->locked || qdwin->move_grab_active)
		return 0;
	if (qdwin->wm_policy.focus_policy != QDWIN_FOCUS_FOLLOW_MOUSE)
		return 0;
	struct weston_pointer *ptr = qdwin_first_pointer(qdwin);
	if (!ptr)
		return 0;
	struct qdwin_toplevel *tl = qdwin_toplevel_at_pos(qdwin, ptr->pos);
	if (!tl || tl->handle != qdwin->ffm_pending_handle)
		return 0;  /* pointer moved away before the timer fired */
	qdwin_toplevel_focus(qdwin, tl, qdwin->wm_policy.raise_on_hover);
	return 0;
}

/* Cancel any pending focus-follows-mouse retarget (pointer left all
 * toplevels, focus policy changed, or shell unbound). */
static void
qdwin_ffm_cancel(struct qdwin *qdwin)
{
	qdwin->ffm_pending_handle = 0;
	if (qdwin->ffm_timer)
		wl_event_source_timer_update(qdwin->ffm_timer, 0);
}

/* Called from the default pointer-grab motion handler. Under
 * focus-follows-mouse, retarget keyboard focus to the toplevel under the
 * pointer, immediately when ffm_delay_ms == 0 or after a settle delay. */
static void
qdwin_ffm_consider(struct qdwin *qdwin, struct weston_pointer *pointer)
{
	if (!qdwin || qdwin->locked || qdwin->move_grab_active)
		return;
	if (qdwin->wm_policy.focus_policy != QDWIN_FOCUS_FOLLOW_MOUSE)
		return;
	struct qdwin_toplevel *tl = qdwin_toplevel_at_pos(qdwin, pointer->pos);
	if (!tl) {
		/* Pointer over the background / a panel — leave focus where
		 * it is (sloppy-focus, matching xfwm/kwin), just disarm any
		 * pending retarget. */
		qdwin_ffm_cancel(qdwin);
		return;
	}
	/* Already focused here — nothing to do. */
	struct weston_keyboard *kbd =
		weston_seat_get_keyboard(pointer->seat);
	if (kbd && tl->view && kbd->focus == tl->view->surface) {
		qdwin_ffm_cancel(qdwin);
		return;
	}
	if (qdwin->wm_policy.ffm_delay_ms == 0) {
		qdwin->ffm_pending_handle = 0;
		qdwin_toplevel_focus(qdwin, tl, qdwin->wm_policy.raise_on_hover);
		return;
	}
	/* Arm / re-arm the settle timer for this toplevel. */
	if (!qdwin->ffm_timer) {
		qdwin->ffm_timer = wl_event_loop_add_timer(
			wl_display_get_event_loop(qdwin->compositor->wl_display),
			qdwin_ffm_timer_cb, qdwin);
		if (!qdwin->ffm_timer)
			return;
	}
	if (qdwin->ffm_pending_handle != tl->handle) {
		qdwin->ffm_pending_handle = tl->handle;
		uint32_t d = qdwin->wm_policy.ffm_delay_ms;
		if (d > QDWIN_FFM_DELAY_MAX)
			d = QDWIN_FFM_DELAY_MAX;
		wl_event_source_timer_update(qdwin->ffm_timer, (int)d);
	}
}

/* Total overlap area (px^2) of a candidate slot (ox,oy,sw,sh) against
 * every other mapped, non-nested toplevel. Used by SMART placement to
 * score candidate slots. */
static long
qdwin_placement_overlap(struct qdwin *qdwin, struct qdwin_toplevel *tl,
			int ox, int oy, int sw, int sh)
{
	long overlap = 0;
	struct qdwin_toplevel *t;
	wl_list_for_each(t, &qdwin->toplevels, link) {
		if (t == tl || t->is_nested_proxy || !t->mapped || !t->view)
			continue;
		struct weston_coord_global vp =
			weston_view_get_pos_offset_global(t->view);
		struct weston_surface *ts = t->view->surface;
		int tw = ts ? ts->width : 0;
		int th = ts ? ts->height : 0;
		int ix = (ox > (int)vp.c.x) ? ox : (int)vp.c.x;
		int iy = (oy > (int)vp.c.y) ? oy : (int)vp.c.y;
		int ax = (ox + sw < (int)vp.c.x + tw) ? ox + sw
						      : (int)vp.c.x + tw;
		int ay = (oy + sh < (int)vp.c.y + th) ? oy + sh
						      : (int)vp.c.y + th;
		if (ax > ix && ay > iy)
			overlap += (long)(ax - ix) * (ay - iy);
	}
	return overlap;
}

/* Compute the new-window content top-left (global px) per the placement
 * policy. Mirrors the legacy centre+cascade math; cx/cy are the content
 * origin the caller hands to weston_view_set_position. */
static void
qdwin_compute_placement(struct qdwin *qdwin, struct qdwin_toplevel *tl,
			struct weston_surface *surface,
			struct weston_output *out, int *cx, int *cy)
{
	int sw = surface->width  > 0 ? surface->width  : 800;
	int sh = surface->height > 0 ? surface->height : 600;
	int wx, wy, ww, wh;
	qdwin_output_work_area(qdwin, out, &wx, &wy, &ww, &wh);
	int center_x = wx + (ww - sw) / 2;
	int center_y = wy + (wh - sh) / 2;

	int px = center_x, py = center_y;
	switch (qdwin->wm_policy.placement) {
	case QDWIN_PLACE_CENTER:
		break;  /* px/py already centred */
	case QDWIN_PLACE_UNDER_MOUSE: {
		struct weston_pointer *ptr = qdwin_first_pointer(qdwin);
		if (ptr) {
			px = (int)ptr->pos.c.x - sw / 2;
			py = (int)ptr->pos.c.y - sh / 2;
		}
		break;
	}
	case QDWIN_PLACE_SMART: {
		/* Pick the candidate slot (a coarse grid over the work area)
		 * with the least total overlap against existing mapped
		 * toplevels, preferring the centre on ties.
		 *
		 * The centre slot is scored first and seeds best_*; grid slots
		 * only win with STRICTLY less overlap. This is what makes the
		 * common cases land where the user expects: with no other
		 * windows (or no overlap at centre) every slot ties at zero, so
		 * the new window centres rather than piling into the work-area
		 * top-left corner. (A prior version seeded best_overlap=-1 and
		 * accepted the first grid slot — gx=gy=0, the corner — on the
		 * first zero-overlap hit, so the first window always landed at
		 * (wx,wy) instead of centred.) */
		const int STEPS = 5;
		long best_overlap =
			qdwin_placement_overlap(qdwin, tl, center_x, center_y,
						sw, sh);
		int best_x = center_x, best_y = center_y;
		if (best_overlap > 0) {
			for (int gy = 0; gy < STEPS; gy++) {
				for (int gx = 0; gx < STEPS; gx++) {
					int ox = wx + (STEPS > 1 ?
						(ww - sw) * gx / (STEPS - 1) : 0);
					int oy = wy + (STEPS > 1 ?
						(wh - sh) * gy / (STEPS - 1) : 0);
					if (ox < wx) ox = wx;
					if (oy < wy) oy = wy;
					long overlap = qdwin_placement_overlap(
						qdwin, tl, ox, oy, sw, sh);
					if (overlap < best_overlap) {
						best_overlap = overlap;
						best_x = ox;
						best_y = oy;
						if (overlap == 0)
							goto smart_done;
					}
				}
			}
		}
smart_done:
		px = best_x;
		py = best_y;
		break;
	}
	case QDWIN_PLACE_CASCADE:
	default: {
		int siblings = 0;
		struct qdwin_toplevel *t;
		wl_list_for_each(t, &qdwin->toplevels, link) {
			if (t == tl || t->is_nested_proxy)
				continue;
			if (t->mapped)
				siblings++;
		}
		int offset = (siblings * 40) % 200;  /* wrap at 5 */
		px = center_x + offset;
		py = center_y + offset;
		break;
	}
	}
	if (px < wx) px = wx;
	if (py < wy) py = wy;
	*cx = px;
	*cy = py;
}

/* Snap one edge value `v` to `target` when within snap_distance; returns
 * the (possibly snapped) value and records whether it snapped via *did. */
static double
qdwin_snap1(double v, double target, uint32_t dist, int *did)
{
	if (!*did && fabs(v - target) <= (double)dist) {
		*did = 1;
		return target;
	}
	return v;
}

/* Edge snapping during an interactive move. nx/ny are the dragged
 * window's content origin; we snap its OUTER rectangle (chrome-inclusive)
 * to the work-area edges and to other mapped toplevels' outer edges,
 * then translate back to a content origin. */
static void
qdwin_snap_move_position(struct qdwin *qdwin, struct qdwin_toplevel *tl,
			 double *nx, double *ny)
{
	if (!qdwin->wm_policy.snap_enabled)
		return;
	uint32_t dist = qdwin->wm_policy.snap_distance;
	if (dist < QDWIN_SNAP_DIST_MIN) dist = QDWIN_SNAP_DIST_MIN;
	if (dist > QDWIN_SNAP_DIST_MAX) dist = QDWIN_SNAP_DIST_MAX;

	struct weston_surface *surf =
		tl->view ? tl->view->surface : NULL;
	int ow = tl->outer_width  > 0 ? tl->outer_width
		: (surf ? surf->width  : 0) + tl->inset_w + tl->inset_e;
	int oh = tl->outer_height > 0 ? tl->outer_height
		: (surf ? surf->height : 0) + tl->inset_n + tl->inset_s;
	if (ow <= 0 || oh <= 0)
		return;

	double left = *nx - tl->inset_w;
	double top  = *ny - tl->inset_n;

	struct weston_output *out =
		qdwin_output_at_global(qdwin, left, top);
	if (!out)
		return;
	int wx, wy, ww, wh;
	qdwin_output_work_area(qdwin, out, &wx, &wy, &ww, &wh);

	int did_x = 0, did_y = 0;
	/* Work-area edges. */
	left = qdwin_snap1(left, wx, dist, &did_x);
	left = qdwin_snap1(left, wx + ww - ow, dist, &did_x);
	top  = qdwin_snap1(top, wy, dist, &did_y);
	top  = qdwin_snap1(top, wy + wh - oh, dist, &did_y);

	/* Other windows' outer edges (left↔right, right↔left, top/bottom).
	 * Only visible windows on the active workspace are snap targets —
	 * minimised / off-workspace toplevels are parked on hidden layers with
	 * stale geometry, so snapping to them would pull toward an invisible
	 * window. */
	struct qdwin_toplevel *t;
	wl_list_for_each(t, &qdwin->toplevels, link) {
		if (t == tl || t->is_nested_proxy || !t->mapped || !t->view)
			continue;
		if (t->state & QDWIN_TS_MINIMIZED)
			continue;
		if (t->workspace != qdwin->active_workspace)
			continue;
		struct weston_coord_global vp =
			weston_view_get_pos_offset_global(t->view);
		double tleft = vp.c.x - t->inset_w;
		double ttop  = vp.c.y - t->inset_n;
		struct weston_surface *ts = t->view->surface;
		int tow = t->outer_width  > 0 ? t->outer_width
			: (ts ? ts->width  : 0) + t->inset_w + t->inset_e;
		int toh = t->outer_height > 0 ? t->outer_height
			: (ts ? ts->height : 0) + t->inset_n + t->inset_s;
		left = qdwin_snap1(left, tleft + tow, dist, &did_x); /* abut right */
		left = qdwin_snap1(left, tleft - ow, dist, &did_x);  /* abut left  */
		left = qdwin_snap1(left, tleft, dist, &did_x);       /* align left */
		top  = qdwin_snap1(top, ttop + toh, dist, &did_y);
		top  = qdwin_snap1(top, ttop - oh, dist, &did_y);
		top  = qdwin_snap1(top, ttop, dist, &did_y);
	}

	*nx = left + tl->inset_w;
	*ny = top  + tl->inset_n;
}

/* Clear the tiled flag without moving the window — used when another
 * state change (maximise, fullscreen, interactive move) supersedes the
 * tile so a later request_tile(none) doesn't try to "restore" stale
 * geometry. */
static void
qdwin_toplevel_clear_tiled(struct qdwin_toplevel *tl)
{
	tl->tiled = QDWIN_TILE_NONE;
}

/* request_tile core: tile to a half of the work area, or restore. */
static void
qdwin_toplevel_set_tiled(struct qdwin *qdwin, struct qdwin_toplevel *tl,
			 uint32_t edge)
{
	if (tl->nested_proxy_pending_decision) {
		weston_log("qdwin: tile handle=%u ignored — nested-proxy "
			   "waiting for admin decision\n", tl->handle);
		return;
	}
	if (edge != QDWIN_TILE_LEFT && edge != QDWIN_TILE_RIGHT)
		edge = QDWIN_TILE_NONE;

	/* Restore. */
	if (edge == QDWIN_TILE_NONE) {
		if (tl->tiled == QDWIN_TILE_NONE)
			return;  /* not tiled — no-op */
		tl->outer_width  = tl->tile_saved_outer_w;
		tl->outer_height = tl->tile_saved_outer_h;
		struct weston_coord_global pos = {
			.c = weston_coord(tl->tile_saved_x, tl->tile_saved_y),
		};
		weston_view_set_position(tl->view, pos);
		/* Nested-proxy toplevels (tier-4 VM windows) have no
		 * desktop_surface; apply_inset's nested branch ignores
		 * outer_* and the inner client is resized via the curtain's
		 * set_geometry, exactly as maximise/fullscreen do. */
		if (tl->is_nested_proxy)
			qdwin_nested_proxy_set_geometry(tl, tl->outer_width,
							tl->outer_height);
		else
			qdwin_toplevel_apply_inset(tl);
		qdwin_toplevel_position_chrome(tl);
		tl->tiled = QDWIN_TILE_NONE;
		weston_log("qdwin: tile handle=%u restored %dx%d@(%.0f,%.0f)\n",
			   tl->handle, tl->outer_width, tl->outer_height,
			   tl->tile_saved_x, tl->tile_saved_y);
		weston_compositor_schedule_repaint(qdwin->compositor);
		return;
	}

	/* Tiling a maximised/fullscreen window first leaves that state so
	 * the saved floating geometry is the pre-special-state one. */
	if (tl->state & QDWIN_TS_MAXIMIZED)
		qdwin_toplevel_set_maximized(qdwin, tl, false);
	if (tl->state & QDWIN_TS_FULLSCREEN)
		qdwin_toplevel_set_fullscreen(qdwin, tl, false, NULL);

	struct weston_coord_global cur =
		weston_view_get_pos_offset_global(tl->view);
	struct weston_output *out =
		qdwin_output_at_global(qdwin, cur.c.x, cur.c.y);
	if (!out) {
		weston_log("qdwin: tile handle=%u: no output\n", tl->handle);
		return;
	}

	/* Save the pre-tile geometry only on the first tile (so left↔right
	 * re-tiling keeps the original floating geometry to restore to). */
	if (tl->tiled == QDWIN_TILE_NONE) {
		qdwin_toplevel_seed_outer_from_committed(tl);
		tl->tile_saved_outer_w = tl->outer_width;
		tl->tile_saved_outer_h = tl->outer_height;
		tl->tile_saved_x = cur.c.x;
		tl->tile_saved_y = cur.c.y;
	}

	int wx, wy, ww, wh;
	qdwin_output_work_area(qdwin, out, &wx, &wy, &ww, &wh);
	/* Left gets floor(ww/2); right gets the remainder so the two halves
	 * abut with no gap on an odd-width work area. */
	int left_w = ww / 2;
	int tile_w = (edge == QDWIN_TILE_RIGHT) ? (ww - left_w) : left_w;
	int tx     = (edge == QDWIN_TILE_RIGHT) ? (wx + left_w) : wx;

	tl->outer_width  = tile_w;
	tl->outer_height = wh;
	struct weston_coord_global origin = {
		.c = weston_coord(tx + tl->inset_w, wy + tl->inset_n),
	};
	weston_view_set_position(tl->view, origin);
	if (tl->is_nested_proxy)
		qdwin_nested_proxy_set_geometry(tl, tl->outer_width,
						tl->outer_height);
	else
		qdwin_toplevel_apply_inset(tl);
	qdwin_toplevel_position_chrome(tl);
	tl->tiled = edge;
	weston_log("qdwin: tile handle=%u edge=%s outer=%dx%d at (%d,%d)\n",
		   tl->handle, edge == QDWIN_TILE_LEFT ? "left" : "right",
		   tl->outer_width, tl->outer_height, tx, wy);
	weston_compositor_schedule_repaint(qdwin->compositor);
}

static void
qdwin_handle_set_wm_policy(struct wl_client *client,
			   struct wl_resource *resource,
			   uint32_t focus_policy, uint32_t ffm_delay_ms,
			   uint32_t raise_on_click, uint32_t raise_on_hover,
			   uint32_t placement, uint32_t snap_enabled,
			   uint32_t snap_distance)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;

	struct qdwin_wm_policy *p = &qdwin->wm_policy;
	p->focus_policy = (focus_policy == QDWIN_FOCUS_FOLLOW_MOUSE)
		? QDWIN_FOCUS_FOLLOW_MOUSE : QDWIN_FOCUS_CLICK;
	p->ffm_delay_ms = ffm_delay_ms > QDWIN_FFM_DELAY_MAX
		? QDWIN_FFM_DELAY_MAX : ffm_delay_ms;
	p->raise_on_click = raise_on_click ? 1 : 0;
	p->raise_on_hover = raise_on_hover ? 1 : 0;
	switch (placement) {
	case QDWIN_PLACE_CENTER:
	case QDWIN_PLACE_UNDER_MOUSE:
	case QDWIN_PLACE_SMART:
	case QDWIN_PLACE_CASCADE:
		p->placement = placement;
		break;
	default:
		p->placement = QDWIN_PLACE_SMART;
		break;
	}
	p->snap_enabled = snap_enabled ? 1 : 0;
	if (snap_distance < QDWIN_SNAP_DIST_MIN)
		snap_distance = QDWIN_SNAP_DIST_MIN;
	if (snap_distance > QDWIN_SNAP_DIST_MAX)
		snap_distance = QDWIN_SNAP_DIST_MAX;
	p->snap_distance = snap_distance;

	/* Leaving follow-mouse: drop any armed retarget. */
	if (p->focus_policy != QDWIN_FOCUS_FOLLOW_MOUSE)
		qdwin_ffm_cancel(qdwin);

	weston_log("qdwin: set_wm_policy focus=%u ffm_delay=%u raise_click=%d "
		   "raise_hover=%d placement=%u snap=%d dist=%u\n",
		   p->focus_policy, p->ffm_delay_ms, p->raise_on_click,
		   p->raise_on_hover, p->placement, p->snap_enabled,
		   p->snap_distance);
}

static void
qdwin_handle_request_fullscreen(struct wl_client *client,
				struct wl_resource *resource,
				uint32_t handle, uint32_t fullscreen)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		wl_resource_post_error(resource,
			QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
			"request_fullscreen: unknown handle %u", handle);
		return;
	}
	/* Like maximise: fullscreening a tiled window saves the tiled rect into
	 * the fullscreen slot (saved_*) so un-fullscreen returns to the tile,
	 * while tl->tiled / tile_saved_* keep the pre-tile float for a later
	 * request_tile(none). Do NOT clear the tile here. */
	qdwin_toplevel_set_fullscreen(qdwin, tl, fullscreen != 0, NULL);
}

static void
qdwin_handle_request_tile(struct wl_client *client,
			  struct wl_resource *resource,
			  uint32_t handle, uint32_t edge)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (qdwin->locked) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_LOCKED, "locked");
		return;
	}
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		wl_resource_post_error(resource,
			QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
			"request_tile: unknown handle %u", handle);
		return;
	}
	qdwin_toplevel_set_tiled(qdwin, tl, edge);
}

/* v26: force all outputs on/off (DPMS), the display-power side of the
 * Power settings. The shell owns the idle timing (ext-idle-notify) and
 * the action policy; here the compositor only enacts the power state.
 * weston_output_power_off/on cease/restore rendering and drive DPMS where
 * the backend supports it (a no-op on headless, whose outputs have a NULL
 * set_dpms — safe to call). */
static void
qdwin_set_all_outputs_power(struct qdwin *qdwin, int on)
{
	struct weston_output *output;
	int n = 0;
	wl_list_for_each(output, &qdwin->compositor->output_list, link) {
		if (on)
			weston_output_power_on(output);
		else
			weston_output_power_off(output);
		n++;
	}
	weston_log("qdwin: set_display_power on=%d (%d output%s)\n",
		   on, n, n == 1 ? "" : "s");
}

static void
qdwin_handle_set_display_power(struct wl_client *client,
			       struct wl_resource *resource, uint32_t on)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	/* No locked-state refusal: turning the display back on must work even
	 * while locked (the locker is up, the user wakes the screen). */
	qdwin->display_forced_off = on ? 0 : 1;
	qdwin_set_all_outputs_power(qdwin, on != 0);
}

/* ------------------------------------------------------------------
 * v28: live libinput pointer/touchpad config (set_pointer_config) and
 * xkb key-repeat (set_key_repeat).
 *
 * Pointer config is applied per libinput device through the public
 * libinput_device_config_* API. We reach the devices by walking each
 * weston_seat and, under the libinput/DRM backend, treating it as the
 * struct udev_seat that embeds it (weston_seat is udev_seat's first
 * member) — exactly how libweston's own backend-libinput touches device
 * config. On backends with no libinput devices (e.g. headless) the
 * snapshot is stored but applies to nothing, which is a safe no-op.
 *
 * Each field is guarded by libinput's own *_is_available / *_has_*
 * capability query so a mixed mouse + touchpad seat takes the relevant
 * subset per device and never errors on an unsupported field.
 * ------------------------------------------------------------------ */

/* The accel-profile / scroll-method enum mappers are pure kernels living
 * in qdwin-logic.c (declared in qdwin-logic.h). The qdwin-side enum
 * values (QDWIN_ACCEL_*, QDWIN_SCROLL_*) match the QDWIN_LOGIC_* mirrors,
 * so the existing call sites pass the raw uint32_t straight through.
 * Build-time guards keep the two enum value sets in lock-step. */
_Static_assert(QDWIN_ACCEL_ADAPTIVE == QDWIN_LOGIC_ACCEL_ADAPTIVE,
	       "accel-profile enum drift vs qdwin-logic.h");
_Static_assert(QDWIN_ACCEL_FLAT == QDWIN_LOGIC_ACCEL_FLAT,
	       "accel-profile enum drift vs qdwin-logic.h");
_Static_assert(QDWIN_SCROLL_NONE == QDWIN_LOGIC_SCROLL_NONE,
	       "scroll-method enum drift vs qdwin-logic.h");
_Static_assert(QDWIN_SCROLL_TWO_FINGER == QDWIN_LOGIC_SCROLL_TWO_FINGER,
	       "scroll-method enum drift vs qdwin-logic.h");
_Static_assert(QDWIN_SCROLL_EDGE == QDWIN_LOGIC_SCROLL_EDGE,
	       "scroll-method enum drift vs qdwin-logic.h");
_Static_assert(QDWIN_SCROLL_ON_BUTTON_DOWN == QDWIN_LOGIC_SCROLL_ON_BUTTON_DOWN,
	       "scroll-method enum drift vs qdwin-logic.h");

/* Apply the stored snapshot to one libinput device, honouring each
 * capability guard. Safe to call on any device kind. */
static void
qdwin_pointer_config_apply_device(const struct qdwin_pointer_config *pc,
				  struct libinput_device *dev)
{
	if (!pc->valid || !dev)
		return;

	if (libinput_device_config_accel_is_available(dev)) {
		double speed = (double)pc->accel_speed / 1000.0;
		libinput_device_config_accel_set_speed(dev, speed);
		uint32_t profiles = libinput_device_config_accel_get_profiles(dev);
		enum libinput_config_accel_profile want =
			qdwin_accel_profile_to_libinput(pc->accel_profile);
		if (profiles & want)
			libinput_device_config_accel_set_profile(dev, want);
	}
	if (libinput_device_config_scroll_has_natural_scroll(dev))
		libinput_device_config_scroll_set_natural_scroll_enabled(
			dev, pc->natural_scroll ? 1 : 0);
	if (libinput_device_config_tap_get_finger_count(dev) > 0)
		libinput_device_config_tap_set_enabled(
			dev, pc->tap_to_click
				? LIBINPUT_CONFIG_TAP_ENABLED
				: LIBINPUT_CONFIG_TAP_DISABLED);
	if (libinput_device_config_left_handed_is_available(dev))
		libinput_device_config_left_handed_set(
			dev, pc->left_handed ? 1 : 0);
	if (libinput_device_config_middle_emulation_is_available(dev))
		libinput_device_config_middle_emulation_set_enabled(
			dev, pc->middle_emulation
				? LIBINPUT_CONFIG_MIDDLE_EMULATION_ENABLED
				: LIBINPUT_CONFIG_MIDDLE_EMULATION_DISABLED);
	if (libinput_device_config_dwt_is_available(dev))
		libinput_device_config_dwt_set_enabled(
			dev, pc->disable_while_typing
				? LIBINPUT_CONFIG_DWT_ENABLED
				: LIBINPUT_CONFIG_DWT_DISABLED);
	{
		uint32_t methods = libinput_device_config_scroll_get_methods(dev);
		enum libinput_config_scroll_method want =
			qdwin_scroll_method_to_libinput(pc->scroll_method);
		/* NO_SCROLL is always representable; others are gated. */
		if (want == LIBINPUT_CONFIG_SCROLL_NO_SCROLL || (methods & want))
			libinput_device_config_scroll_set_method(dev, want);
	}
}

/* Reset one libinput device to its per-device defaults (used on shell
 * unbind so a shell-less compositor reverts to stock behaviour). Each field
 * is guarded by the same capability query as the apply path. */
static void
qdwin_pointer_config_reset_device(struct libinput_device *dev)
{
	if (!dev)
		return;
	if (libinput_device_config_accel_is_available(dev)) {
		libinput_device_config_accel_set_speed(
			dev, libinput_device_config_accel_get_default_speed(dev));
		enum libinput_config_accel_profile def =
			libinput_device_config_accel_get_default_profile(dev);
		if (libinput_device_config_accel_get_profiles(dev) & def)
			libinput_device_config_accel_set_profile(dev, def);
	}
	if (libinput_device_config_scroll_has_natural_scroll(dev))
		libinput_device_config_scroll_set_natural_scroll_enabled(dev,
			libinput_device_config_scroll_get_default_natural_scroll_enabled(dev));
	if (libinput_device_config_tap_get_finger_count(dev) > 0)
		libinput_device_config_tap_set_enabled(dev,
			libinput_device_config_tap_get_default_enabled(dev));
	if (libinput_device_config_left_handed_is_available(dev))
		libinput_device_config_left_handed_set(dev,
			libinput_device_config_left_handed_get_default(dev));
	if (libinput_device_config_middle_emulation_is_available(dev))
		libinput_device_config_middle_emulation_set_enabled(dev,
			libinput_device_config_middle_emulation_get_default_enabled(dev));
	if (libinput_device_config_dwt_is_available(dev))
		libinput_device_config_dwt_set_enabled(dev,
			libinput_device_config_dwt_get_default_enabled(dev));
	{
		enum libinput_config_scroll_method def =
			libinput_device_config_scroll_get_default_method(dev);
		if (def == LIBINPUT_CONFIG_SCROLL_NO_SCROLL ||
		    (libinput_device_config_scroll_get_methods(dev) & def))
			libinput_device_config_scroll_set_method(dev, def);
	}
}

/* Walk every libinput device on every libinput-backed seat, invoking `fn`
 * (apply current snapshot, or reset to defaults). Returns the number of
 * devices touched — 0 on a backend with no libinput seats (e.g. headless).
 *
 * The struct udev_seat cast is only valid under the DRM/libinput backend
 * (qdwin->libinput_backend); a plain weston_seat is NOT embedded in a
 * udev_seat, so reading devices_list off one would walk past the object.
 * Even under the libinput backend, qdwin's own synthetic seats (created
 * directly via weston_seat_init — RDP "rdp-*", per-stream "qdwin-stream-*",
 * nested inner "qdwin-nested-*") are NOT udev_seats; the libinput backend
 * never uses the "qdwin-" prefix, so skipping it (plus "rdp-") leaves
 * exactly the real libinput seats. */
static int
qdwin_pointer_config_walk(struct qdwin *qdwin,
			  void (*fn)(struct libinput_device *dev,
				     const struct qdwin_pointer_config *pc),
			  const struct qdwin_pointer_config *pc)
{
	struct weston_seat *seat;
	int n = 0;
	if (!qdwin->libinput_backend)
		return 0;
	wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
		const char *name = seat->seat_name ? seat->seat_name : "";
		if (strncmp(name, "rdp-", 4) == 0 ||
		    strncmp(name, "qdwin-", 6) == 0)
			continue;
		struct qdwin_udev_seat_abi *useat =
			(struct qdwin_udev_seat_abi *)seat;
		struct qdwin_evdev_device_abi *device;
		wl_list_for_each(device, &useat->devices_list, link) {
			fn(device->device, pc);
			n++;
		}
	}
	return n;
}

/* Walker adapters. */
static void
qdwin_pointer_apply_cb(struct libinput_device *dev,
		       const struct qdwin_pointer_config *pc)
{
	qdwin_pointer_config_apply_device(pc, dev);
}
static void
qdwin_pointer_reset_cb(struct libinput_device *dev,
		       const struct qdwin_pointer_config *pc)
{
	(void)pc;
	qdwin_pointer_config_reset_device(dev);
}

static int
qdwin_pointer_config_apply_all(struct qdwin *qdwin)
{
	if (!qdwin->pointer_config.valid)
		return 0;
	return qdwin_pointer_config_walk(qdwin, qdwin_pointer_apply_cb,
					 &qdwin->pointer_config);
}

static int
qdwin_pointer_config_reset_all(struct qdwin *qdwin)
{
	return qdwin_pointer_config_walk(qdwin, qdwin_pointer_reset_cb, NULL);
}

static void
qdwin_handle_set_pointer_config(struct wl_client *client,
				struct wl_resource *resource,
				int32_t accel_speed, uint32_t accel_profile,
				uint32_t natural_scroll, uint32_t tap_to_click,
				uint32_t left_handed, uint32_t middle_emulation,
				uint32_t disable_while_typing,
				uint32_t scroll_method)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	/* D6: refuse input-device config while locked — but as a logged DROP,
	 * not a fatal protocol error. qdshell pushes this snapshot
	 * unconditionally during connect-and-bind, so a shell that (re)starts
	 * while the session is locked (shell crash + systemd restart during a
	 * lock) would be killed here by the fatal error, crash-loop into
	 * StartLimitBurst=start-limit-hit, and never come back — not even
	 * after unlock (qdlocker/tests/gui/06). Dropping keeps the lock
	 * invariant intact (no input-config mutation behind the lock screen)
	 * while letting the restarted shell live; the config is re-applied on
	 * the next settings change or shell restart. */
	if (qdwin->locked) {
		weston_log("qdwin: set_pointer_config refused while locked "
			   "(dropped, non-fatal)\n");
		return;
	}

	struct qdwin_pointer_config *pc = &qdwin->pointer_config;
	if (accel_speed < QDWIN_ACCEL_SPEED_MIN)
		accel_speed = QDWIN_ACCEL_SPEED_MIN;
	if (accel_speed > QDWIN_ACCEL_SPEED_MAX)
		accel_speed = QDWIN_ACCEL_SPEED_MAX;
	pc->accel_speed = accel_speed;
	pc->accel_profile = (accel_profile == QDWIN_ACCEL_FLAT)
		? QDWIN_ACCEL_FLAT : QDWIN_ACCEL_ADAPTIVE;
	pc->natural_scroll = natural_scroll ? 1 : 0;
	pc->tap_to_click = tap_to_click ? 1 : 0;
	pc->left_handed = left_handed ? 1 : 0;
	pc->middle_emulation = middle_emulation ? 1 : 0;
	pc->disable_while_typing = disable_while_typing ? 1 : 0;
	switch (scroll_method) {
	case QDWIN_SCROLL_NONE:
	case QDWIN_SCROLL_TWO_FINGER:
	case QDWIN_SCROLL_EDGE:
	case QDWIN_SCROLL_ON_BUTTON_DOWN:
		pc->scroll_method = scroll_method;
		break;
	default:
		pc->scroll_method = QDWIN_SCROLL_TWO_FINGER;
		break;
	}
	pc->valid = 1;

	int n = qdwin_pointer_config_apply_all(qdwin);
	weston_log("qdwin: set_pointer_config accel=%d profile=%u natural=%d "
		   "tap=%d left=%d middle=%d dwt=%d scroll=%u (%d device%s)\n",
		   pc->accel_speed, pc->accel_profile, pc->natural_scroll,
		   pc->tap_to_click, pc->left_handed, pc->middle_emulation,
		   pc->disable_while_typing, pc->scroll_method,
		   n, n == 1 ? "" : "s");
}

/* Re-send wl_keyboard.repeat_info to every bound keyboard resource on every
 * seat, so a live rate/delay change reaches already-connected clients (the
 * compositor otherwise only sends repeat_info at keyboard-bind time). */
static void
qdwin_resend_repeat_info(struct qdwin *qdwin)
{
	struct weston_seat *seat;
	wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
		struct weston_keyboard *kbd = weston_seat_get_keyboard(seat);
		struct wl_resource *res;
		if (!kbd)
			continue;
		wl_resource_for_each(res, &kbd->resource_list) {
			if (wl_resource_get_version(res) >=
			    WL_KEYBOARD_REPEAT_INFO_SINCE_VERSION)
				wl_keyboard_send_repeat_info(
					res, qdwin->compositor->kb_repeat_rate,
					qdwin->compositor->kb_repeat_delay);
		}
		wl_resource_for_each(res, &kbd->focus_resource_list) {
			if (wl_resource_get_version(res) >=
			    WL_KEYBOARD_REPEAT_INFO_SINCE_VERSION)
				wl_keyboard_send_repeat_info(
					res, qdwin->compositor->kb_repeat_rate,
					qdwin->compositor->kb_repeat_delay);
		}
	}
}

static void
qdwin_handle_set_key_repeat(struct wl_client *client,
			    struct wl_resource *resource,
			    uint32_t rate, uint32_t delay)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	/* D6: refuse while locked as a logged, NON-fatal drop — see
	 * set_pointer_config above for why (a shell restarted during a lock
	 * must survive its startup config push). */
	if (qdwin->locked) {
		weston_log("qdwin: set_key_repeat refused while locked "
			   "(dropped, non-fatal)\n");
		return;
	}

	if (rate > QDWIN_KB_RATE_MAX)
		rate = QDWIN_KB_RATE_MAX;
	if (delay < QDWIN_KB_DELAY_MIN)
		delay = QDWIN_KB_DELAY_MIN;
	if (delay > QDWIN_KB_DELAY_MAX)
		delay = QDWIN_KB_DELAY_MAX;

	/* Remember the compositor default once so unbind can restore it. */
	if (!qdwin->kb_repeat_overridden) {
		qdwin->default_kb_repeat_rate = qdwin->compositor->kb_repeat_rate;
		qdwin->default_kb_repeat_delay = qdwin->compositor->kb_repeat_delay;
		qdwin->kb_repeat_overridden = 1;
	}
	qdwin->compositor->kb_repeat_rate = (int32_t)rate;
	qdwin->compositor->kb_repeat_delay = (int32_t)delay;
	qdwin_resend_repeat_info(qdwin);
	weston_log("qdwin: set_key_repeat rate=%u delay=%u\n", rate, delay);
}

/* ------------------------------------------------------------------
 * v19: shell-driven global hotkeys (register_hotkey/unregister_hotkey/
 * hotkey_pressed). Backed by weston_compositor_add_key_binding which
 * is already excluded by every active grab — overlays, switcher, and
 * lock all naturally suppress hotkey delivery.
 * ------------------------------------------------------------------ */

struct qdwin_hotkey {
	uint32_t id;
	uint32_t modifiers;          /* qdwin_shell_v1.modifier bitmask */
	uint32_t key;                /* linux input keycode */
	struct weston_binding *binding;
	struct qdwin *qdwin;
	struct wl_list link;
};

static struct qdwin_hotkey *
qdwin_hotkey_find(struct qdwin *qdwin, uint32_t id)
{
	struct qdwin_hotkey *h;
	wl_list_for_each(h, &qdwin->hotkeys, link) {
		if (h->id == id) return h;
	}
	return NULL;
}

static void
qdwin_hotkey_destroy(struct qdwin_hotkey *h)
{
	if (h->binding) weston_binding_destroy(h->binding);
	wl_list_remove(&h->link);
	free(h);
}

static void
qdwin_hotkey_handler(struct weston_keyboard *keyboard,
		     const struct timespec *time,
		     uint32_t key, void *data)
{
	struct qdwin_hotkey *h = data;
	(void)keyboard; (void)time; (void)key;
	weston_log("qdwin: hotkey_pressed id=%u\n", h->id);
	if (h->qdwin->shell_resource) {
		qdwin_shell_v1_send_hotkey_pressed(h->qdwin->shell_resource,
						   h->id);
	}
}

/* qdwin_shell_v1.modifier bits → enum weston_keyboard_modifier. */
static enum weston_keyboard_modifier
qdwin_hotkey_mods_to_weston(uint32_t mods)
{
	enum weston_keyboard_modifier wmods = 0;
	if (mods & 1) wmods |= MODIFIER_CTRL;
	if (mods & 2) wmods |= MODIFIER_ALT;
	if (mods & 4) wmods |= MODIFIER_SUPER;
	if (mods & 8) wmods |= MODIFIER_SHIFT;
	return wmods;
}

static void
qdwin_handle_register_hotkey(struct wl_client *client,
			     struct wl_resource *resource,
			     uint32_t id,
			     uint32_t modifiers,
			     uint32_t key)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin || key == 0) return;
	/* Shell-role gate (deeper-review D2): every other state-mutating
	 * qdwin_shell_v1 request requires the bound shell. A registered hotkey
	 * installs a compositor-wide key binding (intercepts the chord before
	 * normal delivery), so an unbound allowed_uid client holding a shell
	 * resource could hijack chords / unregister the real shell's hotkeys.
	 * Match the rest of the request table. */
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;

	/* Idempotent re-register: drop any existing entry under this id. */
	struct qdwin_hotkey *existing = qdwin_hotkey_find(qdwin, id);
	if (existing) qdwin_hotkey_destroy(existing);

	struct qdwin_hotkey *h = calloc(1, sizeof(*h));
	if (!h) return;
	h->id = id;
	h->modifiers = modifiers;
	h->key = key;
	h->qdwin = qdwin;
	h->binding = weston_compositor_add_key_binding(
		qdwin->compositor, key,
		qdwin_hotkey_mods_to_weston(modifiers),
		qdwin_hotkey_handler, h);
	if (!h->binding) {
		free(h);
		weston_log("qdwin: register_hotkey id=%u failed (binding alloc)\n",
			   id);
		return;
	}
	wl_list_insert(&qdwin->hotkeys, &h->link);
	weston_log("qdwin: register_hotkey id=%u mods=0x%x key=%u\n",
		   id, modifiers, key);
}

static void
qdwin_handle_unregister_hotkey(struct wl_client *client,
			       struct wl_resource *resource, uint32_t id)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin) return;
	/* Shell-role gate (deeper-review D2): mirror register_hotkey so an
	 * unbound client cannot unregister the bound shell's hotkeys. */
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	struct qdwin_hotkey *h = qdwin_hotkey_find(qdwin, id);
	if (h) {
		qdwin_hotkey_destroy(h);
		weston_log("qdwin: unregister_hotkey id=%u\n", id);
	}
}

static void
qdwin_hotkeys_purge(struct qdwin *qdwin)
{
	struct qdwin_hotkey *h, *tmp;
	wl_list_for_each_safe(h, tmp, &qdwin->hotkeys, link) {
		qdwin_hotkey_destroy(h);
	}
}

/* v24 sidecar: move a window to another workspace ("send to workspace
 * N" UX). The workspace list/active state is ext-workspace-v1's job;
 * this window-centric move is not expressible there, so it lives on the
 * private shell binding. */
static void
qdwin_handle_move_toplevel_to_workspace(struct wl_client *client,
					struct wl_resource *resource,
					uint32_t handle, uint32_t index)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl;
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	tl = qdwin_toplevel_from_handle(qdwin, handle);
	if (!tl) {
		wl_resource_post_error(resource,
			QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
			"move_toplevel_to_workspace: unknown handle %u", handle);
		return;
	}
	if (index >= qdwin->workspace_count) {
		weston_log("qdwin: move_toplevel_to_workspace ignored handle=%u "
			   "index=%u (count=%u)\n", handle, index,
			   qdwin->workspace_count);
		return;
	}
	if (tl->workspace != index) {
		tl->workspace = index;
		qdwin_toplevel_apply_workspace_visibility(tl);
		qdwin_workspace_refocus_seats(qdwin);
		weston_compositor_schedule_repaint(qdwin->compositor);
		weston_log("qdwin: toplevel handle=%u moved to workspace %u\n",
			   handle, index);
	}
	qdwin_emit_toplevel_workspace(qdwin, tl);
}

/* v27: set the per-index workspace display name the compositor advertises
 * via ext_workspace_handle_v1.name, so the user's custom names (qdshell
 * settings) reach ALL ext-workspace clients — see
 * qdwin-shell-v1.xml set_workspace_name and todo/qdwin/other-shells.md.
 * Empty name reverts to the positional default. Out-of-range index is
 * ignored (the shell may push ahead of create_workspace). Idempotent. */
static void
qdwin_handle_set_workspace_name(struct wl_client *client,
				struct wl_resource *resource,
				uint32_t index, const char *name)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	char *stored;
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (index >= QDWIN_MAX_WORKSPACES) {
		weston_log("qdwin: set_workspace_name ignored index=%u (max %u)\n",
			   index, (unsigned)QDWIN_MAX_WORKSPACES);
		return;
	}
	/* Empty name => revert to positional default (drop any stored name). */
	if (!name || !name[0]) {
		if (qdwin->workspace_names[index]) {
			free(qdwin->workspace_names[index]);
			qdwin->workspace_names[index] = NULL;
			weston_log("qdwin: workspace %u name cleared "
				   "(positional default)\n", index);
			qdwin_ext_ws_broadcast_name(qdwin, index);
		}
		return;
	}
	/* Idempotent: a redundant set is a cheap no-op (no name/done churn). */
	if (qdwin->workspace_names[index] &&
	    strcmp(qdwin->workspace_names[index], name) == 0)
		return;
	/* Bound the stored string (fail-safe against an unbounded shell). */
	stored = strndup(name, QDWIN_WORKSPACE_NAME_MAX);
	if (!stored) {
		wl_client_post_no_memory(client);
		return;
	}
	free(qdwin->workspace_names[index]);
	qdwin->workspace_names[index] = stored;
	weston_log("qdwin: workspace %u name=\"%s\"\n", index, stored);
	/* Only echoed to clients if the index is currently a live workspace;
	 * the broadcast helper skips indices beyond the current handles. The
	 * stored value still applies the next time handles are (re)created. */
	qdwin_ext_ws_broadcast_name(qdwin, index);
}

static const struct qdwin_shell_v1_interface qdwin_shell_impl = {
	.bind_as_shell = qdwin_handle_bind_as_shell,
	.destroy = qdwin_handle_destroy,
	.set_border_color = qdwin_handle_set_border_color,
	.attach_decoration = qdwin_handle_attach_decoration,
	.request_close = qdwin_handle_request_close,
	.request_minimize = qdwin_handle_request_minimize,
	.request_maximize = qdwin_handle_request_maximize,
	.request_raise = qdwin_handle_request_raise,
	.begin_interactive_move = qdwin_handle_begin_interactive_move,
	.begin_interactive_resize = qdwin_handle_begin_interactive_resize,
	.show_popup = qdwin_handle_show_popup,
	.subscribe_view_stream = qdwin_handle_subscribe_view_stream,
	.attach_panel = qdwin_handle_attach_panel,
	.attach_notification = qdwin_handle_attach_notification,
	.attach_launcher = qdwin_handle_attach_launcher,
	.attach_lock_surface = qdwin_handle_attach_lock_surface,
	.set_locked = qdwin_handle_set_locked,
	.nested_proxy_decision = qdwin_handle_nested_proxy_decision,
	.bind_proxy_pixels = qdwin_handle_bind_proxy_pixels,
	.set_cursor_sprite = qdwin_handle_set_cursor_sprite,
	.clear_selection = qdwin_handle_clear_selection,
	.activation_decision = qdwin_handle_activation_decision,
	.set_keyboard_focus = qdwin_handle_set_keyboard_focus,
	.data_offer_receive_decision = qdwin_handle_data_offer_receive_decision,
	.set_keyboard_focus_v2 = qdwin_handle_set_keyboard_focus_v2,
	.attach_background = qdwin_handle_attach_background,
	.register_hotkey = qdwin_handle_register_hotkey,
	.unregister_hotkey = qdwin_handle_unregister_hotkey,
	.move_toplevel_to_workspace = qdwin_handle_move_toplevel_to_workspace,
	.set_wm_policy = qdwin_handle_set_wm_policy,
	.request_fullscreen = qdwin_handle_request_fullscreen,
	.request_tile = qdwin_handle_request_tile,
	.set_display_power = qdwin_handle_set_display_power,
	.set_workspace_name = qdwin_handle_set_workspace_name,
	.set_pointer_config = qdwin_handle_set_pointer_config,
	.set_key_repeat = qdwin_handle_set_key_repeat,
	.request_set_position = qdwin_handle_request_set_position,
	.prepare_output_capture = qdwin_handle_prepare_output_capture,
	.set_remote_output_input = qdwin_handle_set_remote_output_input,
	.drain_remote_output_state = qdwin_handle_drain_remote_output_state,
};

static void
qdwin_shell_resource_destroy(struct wl_resource *resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	if (qdwin->shell_resource == resource) {
		struct weston_output *output;
		/* v32: shell loss is authority loss. Revoke every live RDP seat
		 * before clearing the binding; the backend releases held input. */
		wl_list_for_each(output, &qdwin->compositor->output_list, link) {
			if (qdwin_remote_output_name_valid(output->name))
				qdwin_set_remote_output_input(
					qdwin, output->name, false);
		}
		qdwin_hotkeys_purge(qdwin);
		if (qdwin->move_grab_active) {
			qdwin->move_grab_active = 0;
			if (qdwin->move_grab.pointer)
				weston_pointer_end_grab(qdwin->move_grab.pointer);
		}
		/* v25: drop live WM policy back to the compositor default so a
		 * shell-less compositor keeps the historical feel, and disarm
		 * any pending focus-follows-mouse retarget. */
		qdwin_ffm_cancel(qdwin);
		qdwin_wm_policy_set_defaults(&qdwin->wm_policy);
		/* v26: never leave the display dark if the shell that turned it
		 * off goes away. */
		if (qdwin->display_forced_off) {
			qdwin->display_forced_off = 0;
			qdwin_set_all_outputs_power(qdwin, 1);
		}
		/* v28: revert every libinput device to its per-device defaults
		 * (so a shell-less compositor keeps stock behaviour) and drop the
		 * live snapshot, then restore the compositor's boot-time
		 * key-repeat. Reset must run BEFORE clearing valid is irrelevant
		 * (reset_all ignores the snapshot), but we clear valid after so a
		 * stray late apply can't re-push. */
		if (qdwin->pointer_config.valid)
			qdwin_pointer_config_reset_all(qdwin);
		qdwin->pointer_config.valid = 0;
		if (qdwin->kb_repeat_overridden) {
			qdwin->compositor->kb_repeat_rate =
				qdwin->default_kb_repeat_rate;
			qdwin->compositor->kb_repeat_delay =
				qdwin->default_kb_repeat_delay;
			qdwin->kb_repeat_overridden = 0;
			qdwin_resend_repeat_info(qdwin);
		}
		/* A replacement shell must never inherit outstanding approvals. */
		qdwin_data_offer_pending_free_all(qdwin);
		qdwin->shell_resource = NULL;
		qdwin->shell_bound = 0;
		qdwin->shell_pid = 0;
		qdwin->shell_uid = 0;
		weston_log("qdwin: shell unbound\n");
	}
}

static char *qdwin_proc_exe(pid_t pid);

static void
bind_qdwin_shell(struct wl_client *client, void *data,
		 uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource;
	pid_t pid; uid_t uid; gid_t gid;

	wl_client_get_credentials(client, &pid, &uid, &gid);

	weston_log("qdwin: bind attempt pid=%d uid=%u (allowed_uid=%u)\n",
		   (int)pid, (unsigned)uid, (unsigned)qdwin->allowed_uid);

	if (uid != qdwin->allowed_uid) {
		wl_client_post_implementation_error(
			client,
			"qdwin_shell_v1: uid %u not permitted (allowed uid=%u)",
			(unsigned)uid, (unsigned)qdwin->allowed_uid);
		return;
	}

	/* Helpers that need a protocol resource without the shell role:
	 * the nested pixel consumer (bind_proxy_pixels) and the
	 * cursor-sprites supplier (set_cursor_sprite). Each is pinned to
	 * an exact root-installed exe. Keep the single-shell rule for
	 * every other peer. */
	if (qdwin->shell_bound && !qdwin_client_is_bound_shell(qdwin, client)) {
		char *peer_exe = qdwin_proc_exe(pid);
		bool helper =
			qdwin_nested_pixelfeed_peer_allowed(peer_exe) ||
			qdwin_cursor_sprite_peer_allowed(peer_exe);
		free(peer_exe);
		if (!helper) {
			wl_client_post_implementation_error(
				client,
				"qdwin_shell_v1: shell role already claimed");
			return;
		}
	}

	/* Defense-in-depth (findings F0): reject sandboxed silo clients
	 * outright. The global filter already hides qdwin_shell_v1 from secctx
	 * clients (QDWIN_GLOBAL_SHELL), but uid == allowed_uid is satisfied by
	 * every tier-0..3 silo client (isolation-tiers.md), so a filter bug
	 * must not be the only thing standing between a silo and the shell
	 * role. A silo client is never the legitimate shell. */
	if (qdwin_secctx_client_find(qdwin, client)) {
		wl_client_post_implementation_error(
			client,
			"qdwin_shell_v1: sandboxed (security-context) clients "
			"may not claim the shell role");
		return;
	}

	resource = wl_resource_create(client, &qdwin_shell_v1_interface,
				      version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource, &qdwin_shell_impl,
				       qdwin, qdwin_shell_resource_destroy);

	qdwin_shell_v1_send_hello(resource, (uint32_t)uid);
	weston_log("qdwin: bind accepted for uid=%u, hello sent\n",
		   (unsigned)uid);
}

/* ------------------------------------------------------------------
 * qdwin_locker_v1 — peer locker binding. See doc/locker.md.
 *
 * Mirrors the shell-side attach_lock_surface / set_locked handlers
 * above, sharing the lock_surface / lock_view / lock_resource state.
 * The shell-side variants stay as a deprecation shim until qdshell
 * drops its Modules/LockScreen QML; once that lands they can be
 * removed and the version of qdwin_shell_v1 bumped.
 * ------------------------------------------------------------------ */

/* Forward-declared here (full definition lives with the other /proc
 * helpers) so the locker bind handler below can prove peer liveness. */
static uint64_t qdwin_proc_starttime(pid_t pid);

/* Is the currently-bound locker peer still live? Returns 1 if a locker is
 * bound and we cannot PROVE it dead, 0 only if it is provably gone or its
 * pid has been recycled into a different process. Only a 0 here permits a
 * takeover bind to replace the existing binding.
 *
 * This MUST fail CLOSED: a return of 0 lets a same-uid attacker evict the
 * real qdlocker and then set_locked(0) to unlock the session, so any doubt
 * about liveness must resolve to "alive" (deny takeover). In particular
 * qdwin_proc_starttime() returns 0 on ANY /proc read failure — including a
 * transient EMFILE/fd-exhaustion that a same-uid attacker can deliberately
 * induce — NOT only when the pid is gone. Treating st_now==0 as "dead"
 * would hand the attacker exactly the takeover this gate exists to stop.
 *
 * So we never infer death from an unreadable starttime alone. Death is
 * concluded only from kill(pid,0)==ESRCH (the pid truly no longer exists)
 * or from a starttime that is readable AND differs from the baseline (the
 * pid was recycled into a different process). A readable-but-matching
 * starttime is alive; an UNreadable starttime on a still-existing pid is
 * treated as alive (fail closed). */
static int
qdwin_locker_peer_alive(struct qdwin *qdwin)
{
	if (!qdwin->locker_resource || qdwin->locker_pid <= 0)
		return 0;

	/* Authoritative death signal: the pid no longer exists at all.
	 * kill(pid,0) returning -1/ESRCH is unambiguous; -1/EPERM means the
	 * pid exists but is owned by someone we can't signal (still alive);
	 * 0 means it exists and we could signal it (alive). */
	if (kill(qdwin->locker_pid, 0) != 0 && errno == ESRCH)
		return 0;

	uint64_t st_now = qdwin_proc_starttime(qdwin->locker_pid);
	if (qdwin->locker_starttime != 0) {
		/* Trusted baseline. If we can read a starttime now and it
		 * DIFFERS, the pid was recycled into a different process =>
		 * the original peer is dead/replaceable. If the starttimes
		 * MATCH, it is the same live process. If we cannot read one
		 * now (st_now==0, e.g. attacker-induced fd exhaustion) but the
		 * pid still exists (checked above), we must NOT conclude death:
		 * fail closed and treat it as alive. */
		if (st_now != 0)
			return st_now == qdwin->locker_starttime;
		return 1;   /* unreadable starttime, pid present => alive */
	}
	/* No baseline starttime (e.g. /proc was unreadable at bind). The pid
	 * still exists (kill check above), so treat the present-but-
	 * unverifiable peer as alive — fail closed, never evict. */
	return 1;
}

static void
qdwin_locker_resource_destroy(struct wl_resource *resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	if (qdwin->locker_resource == resource) {
		/* Demote any promoted lock toplevel before zeroing identity
		 * fields — after this point qdwin_toplevel_is_locker_ui can
		 * no longer match, so the toplevel would be orphaned on the
		 * lock layer with no way to demote it later. */
		qdwin_demote_lock_toplevel(qdwin, "locker_disconnect");
		qdwin->locker_resource = NULL;
		qdwin->locker_pid = 0;
		qdwin->locker_uid = (uid_t)-1;
		qdwin->locker_starttime = 0;
		weston_log("qdwin: locker unbound\n");
		if (qdwin->overlay_grab_active && qdwin->overlay_grab_role == 2)
			qdwin_overlay_grab_end(qdwin);
		/* Fail-secure: if the locker disappears while the compositor is
		 * locked, keep the LOCK layer composited — the screen stays
		 * black until a fresh locker binds and maps its Qt toplevel. We
		 * do NOT auto-unlock on locker death — see doc/locker.md
		 * §Lifecycle. The two surface-teardown callbacks
		 * (qdwin_lock_surface_destroyed_cb /
		 * qdwin_lock_surface_resource_destroyed) log this same audit line
		 * when they hold; emit it here too so the PRIMARY locker-death
		 * path (wl_client disconnect) leaves a journal record whenever it
		 * holds the lock — including the surfaceless case (locker died
		 * after set_locked(1) but with no promoted toplevel to demote
		 * above), which would otherwise hold completely silently. The
		 * explicit repaint forces the now-empty lock layer to recomposite
		 * to black (demote only repaints when a toplevel existed). */
		if (qdwin->locked) {
			weston_compositor_schedule_repaint(qdwin->compositor);
			weston_log("qdwin: lock held (fail-secure) "
				   "cause=locker_disconnect — awaiting fresh locker\n");
		}
	}
}

static void
qdwin_handle_bind_as_locker(struct wl_client *client,
			    struct wl_resource *resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	pid_t pid; uid_t uid; gid_t gid;
	wl_client_get_credentials(client, &pid, &uid, &gid);
	(void)gid;
	if (qdwin->locker_resource == resource) {
		/* Same resource binding twice. Use the dedicated
		 * already_bound error code (was using `role` which is for
		 * "surface already has a role" — wrong category). */
		wl_resource_post_error(resource,
			QDWIN_LOCKER_V1_ERROR_ALREADY_BOUND,
			"qdwin_locker_v1: bind_as_locker already called on this resource");
		return;
	}
	if (qdwin->locker_resource) {
		/* A fresh bind while another locker is already bound. Only one
		 * locker may hold the role at a time. Historically qdwin
		 * destroyed the old binding so the new client became
		 * authoritative — but that let ANY same-uid client evict the
		 * real qdlocker and then call set_locked(0) to unlock the
		 * session (CRITICAL: locker takeover). Fail closed instead: if
		 * the current locker's peer is still alive, REFUSE the takeover
		 * and leave the existing binding untouched. Only permit a
		 * replacement once the prior peer is proven dead (pid gone or
		 * recycled), which is the legitimate "qdlocker crashed, a fresh
		 * one is starting" path. */
		if (qdwin_locker_peer_alive(qdwin)) {
			weston_log("qdwin: locker bind REFUSED pid=%d uid=%u — a "
				   "locker is already bound (pid=%d) and its peer "
				   "is alive; takeover denied\n",
				   (int)pid, (unsigned)uid,
				   (int)qdwin->locker_pid);
			wl_resource_post_error(resource,
				QDWIN_LOCKER_V1_ERROR_LOCKER_PRESENT,
				"qdwin_locker_v1: another locker is already bound "
				"and alive; takeover refused");
			return;
		}
		weston_log("qdwin: replacing stale locker binding (old pid=%d "
			   "is dead) with pid=%d\n",
			   (int)qdwin->locker_pid, (int)pid);
		wl_resource_destroy(qdwin->locker_resource);
	}
	qdwin->locker_resource = resource;
	qdwin->locker_pid = pid;
	qdwin->locker_uid = uid;
	qdwin->locker_starttime = qdwin_proc_starttime(pid);
	weston_log("qdwin: locker bound pid=%d uid=%u (initially_locked=%d)\n",
		   (int)pid, (unsigned)uid, qdwin->locked);
	qdwin_locker_v1_send_ready(resource, qdwin->locked ? 1u : 0u);
	/* If the session is ALREADY locked when this locker binds, it is a
	 * fresh locker inheriting the lock after the previous one died
	 * (systemd Restart=always; the old role=2 overlay grab was torn down
	 * in qdwin_locker_resource_destroy). The inherited-lock path does NOT
	 * re-issue set_locked(1) — qdlocker suppresses it as a duplicate — so
	 * the overlay keyboard grab that routes keystrokes to the locker would
	 * otherwise never be re-armed: the lock screen would reappear (toplevel
	 * re-promoted on map) but silently swallow every keystroke, locking the
	 * user out with no way to type the unlock password. Re-arm it here.
	 * This does NOT widen the input-leak surface: role=2 overlay key events
	 * go only to qdwin->locker_resource (qdwin_overlay_grab_key), and role=2
	 * modifier events are suppressed (qdwin_overlay_grab_modifiers) so they
	 * cannot reach a still-focused desktop client during the crash→rebind
	 * window. Before this grab is armed the default keyboard grab drops keys
	 * and modifiers while locked (qdwin_proxy_default_grab_{key,modifiers}),
	 * so input is contained throughout — this only restores delivery to the
	 * legitimately re-bound locker, which is exactly the secure path. */
	if (qdwin->locked)
		qdwin_overlay_grab_start(qdwin, /* role=locker */ 2);
}

static void
qdwin_handle_locker_attach_lock_surface(struct wl_client *client,
					struct wl_resource *resource,
					uint32_t id,
					struct wl_resource *surface_resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	if (qdwin->locker_resource != resource) {
		wl_resource_post_error(resource,
			QDWIN_LOCKER_V1_ERROR_NOT_BOUND,
			"qdwin_locker_v1: not bound (call bind_as_locker first)");
		return;
	}
	struct wl_resource *ls_res = wl_resource_create(
		client, &qdwin_locker_surface_v1_interface,
		wl_resource_get_version(resource), id);
	if (!ls_res) { wl_client_post_no_memory(client); return; }
	struct weston_surface *surface = surface_resource
		? wl_resource_get_user_data(surface_resource) : NULL;
	if (!surface) { wl_resource_destroy(ls_res); return; }
	/* Drop any previously-attached lock surface (whether from the
	 * shell-deprecated path or a prior locker attach). Set
	 * `reattach_in_progress` around the destroy so the resource-
	 * destroyed callback doesn't fire a spurious locked_changed=0
	 * between the old surface tearing down and the new one being
	 * committed — without this, clients observe a 1→0→1 flap on
	 * every locker reattach mid-lock. */
	if (qdwin->lock_resource) {
		qdwin->lock_resource_reattach_in_progress = 1;
		wl_resource_destroy(qdwin->lock_resource);
		qdwin->lock_resource_reattach_in_progress = 0;
	}

	qdwin->lock_resource = ls_res;
	qdwin->lock_resource_is_locker = 1;
	qdwin->lock_surface = surface;
	qdwin->lock_view_is_toplevel = 0;
	qdwin->lock_toplevel = NULL;
	qdwin->lock_view = weston_view_create(surface);
	if (!qdwin->lock_view) {
		qdwin->lock_surface = NULL;
		qdwin->lock_resource = NULL;
		qdwin->lock_resource_is_locker = 0;
		wl_resource_destroy(ls_res);
		return;
	}
	weston_view_move_to_layer(qdwin->lock_view,
				  &qdwin->lock_layer.view_list);
	wl_list_init(&qdwin->lock_surface_destroy.link);
	wl_list_init(&qdwin->lock_surface_commit.link);
	qdwin->lock_surface_destroy.notify = qdwin_lock_surface_destroyed_cb;
	wl_signal_add(&surface->destroy_signal, &qdwin->lock_surface_destroy);
	qdwin->lock_surface_commit.notify = qdwin_lock_surface_commit_cb;
	wl_signal_add(&surface->commit_signal, &qdwin->lock_surface_commit);
	wl_resource_set_implementation(ls_res, &qdwin_locker_surface_impl,
				       qdwin,
				       qdwin_lock_surface_resource_destroyed);
	qdwin_lock_surface_place(qdwin);
	weston_log("qdwin: locker attach_lock_surface\n");
	/* The locker can attach while the session is still unlocked. Defer the
	 * role=2 keyboard grab until set_locked(1), and restore normal global
	 * keybindings again on set_locked(0). */
	if (qdwin->locked)
		qdwin_overlay_grab_start(qdwin, /* role=locker */ 2);
}

static void
qdwin_handle_locker_set_locked(struct wl_client *client,
			       struct wl_resource *resource,
			       uint32_t locked)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (qdwin->locker_resource != resource) {
		wl_resource_post_error(resource,
			QDWIN_LOCKER_V1_ERROR_NOT_BOUND,
			"qdwin_locker_v1: not bound (call bind_as_locker first)");
		return;
	}
	int want = locked ? 1 : 0;
	if (qdwin->locked == want) return;
	qdwin->locked = want;
	if (want) {
		struct qdwin_view_stream *stream, *next;
		wl_list_for_each_safe(stream, next, &qdwin->view_streams, link)
			qdwin_view_stream_terminate(stream, "compositor locked",
						    stream->forward_pid);
		/* Install the curtain first so the toplevels promoted below
		 * (which insert at the head of lock_layer) naturally land above
		 * it; qdwin_maybe_promote_lock_toplevel re-bottoms the curtain
		 * after each move as belt-and-suspenders. */
		qdwin_install_lock_curtain(qdwin);
		struct qdwin_toplevel *tl;
		wl_list_for_each(tl, &qdwin->toplevels, link)
			qdwin_maybe_promote_lock_toplevel(qdwin, tl,
							  "locker_set_locked");
		qdwin_overlay_grab_start(qdwin, /* role=locker */ 2);
		qdwin_hide_non_lock_layers(qdwin);
	} else {
		qdwin_remove_lock_curtain(qdwin);
		qdwin_demote_lock_toplevel(qdwin, "locker_set_locked=0");
		if (qdwin->overlay_grab_active &&
		    qdwin->overlay_grab_role == 2)
			qdwin_overlay_grab_end(qdwin);
		qdwin_show_non_lock_layers(qdwin);
	}
	weston_log("qdwin: locker set_locked=%d (lock_surface=%p)\n",
		   want, (void*)qdwin->lock_surface);
	weston_log("qdwin: locked_changed=%d cause=locker_set_locked\n", want);
	/* Fan out to both protocols so the shell sees the transition
	 * even though the locker drove it. */
	if (qdwin->shell_resource)
		qdwin_shell_v1_send_locked_changed(qdwin->shell_resource, want);
	if (qdwin->locker_resource)
		qdwin_locker_v1_send_locked_changed(qdwin->locker_resource, want);
	weston_compositor_schedule_repaint(qdwin->compositor);
}

static void
qdwin_handle_lock_acknowledged(struct wl_client *client,
			       struct wl_resource *resource,
			       uint32_t reason)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (qdwin->locker_resource != resource) {
		wl_resource_post_error(resource,
			QDWIN_LOCKER_V1_ERROR_NOT_BOUND,
			"qdwin_locker_v1: lock_acknowledged before bind_as_locker");
		return;
	}
	if (reason > 3) {
		wl_resource_post_error(resource,
			QDWIN_LOCKER_V1_ERROR_INVALID_REASON,
			"qdwin_locker_v1: lock_acknowledged reason=%u out of range", reason);
		return;
	}
	weston_log("qdwin: lock_acknowledged reason=%u\n", reason);
}

/* Defined later (Option-B identity-capture helpers); the locker bind
 * handler below uses them for the optional exe/entrypoint/SELinux peer
 * checks. */
static char *qdwin_proc_exe(pid_t pid);
static char *qdwin_proc_argv1(pid_t pid);
static char *qdwin_proc_selinux_label(pid_t pid);
static uint64_t qdwin_proc_starttime(pid_t pid);
static bool qdwin_exe_is_system_interpreter(const char *exe);
static bool qdwin_path_is_trusted_entrypoint(const char *cand,
					     const char *expected);

static const struct qdwin_locker_v1_interface qdwin_locker_impl = {
	.bind_as_locker = qdwin_handle_bind_as_locker,
	.attach_lock_surface = qdwin_handle_locker_attach_lock_surface,
	.set_locked = qdwin_handle_locker_set_locked,
	.lock_acknowledged = qdwin_handle_lock_acknowledged,
};

/* §P10: callback referenced only when role=host; mark unused to avoid
 * -Wunused-function under role=guest builds without rewrapping every
 * forward declaration. */
__attribute__((unused))
static void
bind_qdwin_locker(struct wl_client *client, void *data,
		  uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource;
	pid_t pid; uid_t uid; gid_t gid;

	wl_client_get_credentials(client, &pid, &uid, &gid);
	weston_log("qdwin: locker bind attempt pid=%d uid=%u "
		   "(allowed_locker_uid=%u)\n",
		   (int)pid, (unsigned)uid,
		   (unsigned)qdwin->allowed_locker_uid);

	if (qdwin_secctx_client_find(qdwin, client)) {
		weston_log("qdwin: locker bind rejected pid=%d uid=%u "
			   "(secctx client)\n",
			   (int)pid, (unsigned)uid);
		wl_client_post_implementation_error(client,
			"qdwin_locker_v1: secctx clients may not bind");
		return;
	}

	if (uid != qdwin->allowed_locker_uid) {
		wl_client_post_implementation_error(client,
			"qdwin_locker_v1: uid %u not permitted "
			"(allowed locker uid=%u)",
			(unsigned)uid, (unsigned)qdwin->allowed_locker_uid);
		return;
	}

	/* Production hardening (see qdwin/qdwin-locker-v1.xml): when an
	 * expected locker exe and/or SELinux label is configured, verify
	 * the peer process matches before handing out the locker resource.
	 * The helpers map readlink/read failures to "" so an unreadable
	 * /proc entry can never accidentally pass a non-empty expectation:
	 * a configured check against "" fails closed.
	 *
	 * Starttime double-read — exactly what it does and does NOT cover:
	 * wl_client_get_credentials() returns the pid the kernel pinned at
	 * connect time, but we read /proc/<pid>/... now, so the pid could in
	 * principle have changed meaning between connect and these reads. We
	 * bracket the exe/label reads with two /proc/<pid>/stat starttime
	 * samples and reject if either is unreadable (0) or they differ.
	 *
	 *   Covers: a pid that was recycled (different process, hence a
	 *   different starttime) *during* our own read window — the two
	 *   samples won't match, so we fail closed.
	 *
	 *   Does NOT cover:
	 *     - pid reuse that completed BEFORE the first sample: both reads
	 *       then observe the same (new) process consistently, so the
	 *       guard cannot tell the bind's pid was already stale.
	 *     - a same-process execve() (e.g. into a different SELinux
	 *       domain): starttime is the fork/clone time and is unchanged
	 *       by exec, so the two samples match even though the exe/label
	 *       we read may no longer reflect what bound.
	 * In short this proves the pid named one stable process across the
	 * read, not that it still names the process that connected. The
	 * residual same-process-exec / pre-read-reuse window is closed by
	 * relying on service confinement (see doc/locker.md). */
	if (qdwin->allowed_locker_entrypoint || qdwin->allowed_locker_exe ||
	    qdwin->allowed_locker_label) {
		uint64_t st_before = qdwin_proc_starttime(pid);

		/* DEFAULT production identity for the Python console-script
		 * qdlocker: its /proc/<pid>/exe is the interpreter, so we verify
		 * (a) the exe is a system interpreter (python under a trusted
		 * bindir) AND (b) argv[1] resolves to the canonical, root-owned
		 * entrypoint file. A casual same-uid impostor (the shell, a
		 * desktop helper, a naive rogue) satisfies neither, so it is
		 * rejected — while the genuine qdlocker passes. (argv is
		 * process-writable, so a deliberate forge of BOTH a python exe
		 * and an argv[1]=<entrypoint> is the residual window; the
		 * stat()-the-named-file requirement plus service confinement
		 * keep that out of reach of an ordinary same-uid client. A
		 * dedicated locker uid / SELinux label remains the strongest
		 * posture and overrides this.) */
		if (qdwin->allowed_locker_entrypoint) {
			char *exe = qdwin_proc_exe(pid);
			char *argv1 = qdwin_proc_argv1(pid);
			bool exe_ok = qdwin_exe_is_system_interpreter(exe);
			bool ep_ok = qdwin_path_is_trusted_entrypoint(
				argv1, qdwin->allowed_locker_entrypoint);
			if (!exe_ok || !ep_ok) {
				weston_log("qdwin: locker bind rejected pid=%d "
					   "exe='%s' argv1='%s' (expected a system "
					   "interpreter launching entrypoint '%s'; "
					   "exe_ok=%d entrypoint_ok=%d)\n",
					   (int)pid, exe ? exe : "(unreadable)",
					   argv1 ? argv1 : "(unreadable)",
					   qdwin->allowed_locker_entrypoint,
					   (int)exe_ok, (int)ep_ok);
				wl_client_post_implementation_error(client,
					"qdwin_locker_v1: peer locker entrypoint "
					"not permitted");
				free(exe);
				free(argv1);
				return;
			}
			free(exe);
			free(argv1);
		}

		if (qdwin->allowed_locker_exe) {
			char *exe = qdwin_proc_exe(pid);
			/* exe==NULL means the helper hit OOM; treat as
			 * "unverifiable" => reject (fail closed). */
			int ok = (exe && strcmp(exe, qdwin->allowed_locker_exe) == 0);
			if (!ok) {
				weston_log("qdwin: locker bind rejected pid=%d exe='%s' "
					   "(expected '%s')\n",
					   (int)pid, exe ? exe : "(unreadable)",
					   qdwin->allowed_locker_exe);
				wl_client_post_implementation_error(client,
					"qdwin_locker_v1: peer executable not permitted");
				free(exe);
				return;
			}
			free(exe);
		}

		if (qdwin->allowed_locker_label) {
			char *label = qdwin_proc_selinux_label(pid);
			/* label==NULL means OOM; treat as unverifiable => reject. */
			int ok = (label && strcmp(label, qdwin->allowed_locker_label) == 0);
			if (!ok) {
				weston_log("qdwin: locker bind rejected pid=%d label='%s' "
					   "(expected '%s')\n",
					   (int)pid, label ? label : "(unreadable)",
					   qdwin->allowed_locker_label);
				wl_client_post_implementation_error(client,
					"qdwin_locker_v1: peer SELinux label not permitted");
				free(label);
				return;
			}
			free(label);
		}

		uint64_t st_after = qdwin_proc_starttime(pid);
		if (st_before == 0 || st_after == 0 || st_before != st_after) {
			weston_log("qdwin: locker bind rejected pid=%d — process "
				   "identity unstable across /proc read "
				   "(starttime %llu -> %llu)\n",
				   (int)pid,
				   (unsigned long long)st_before,
				   (unsigned long long)st_after);
			wl_client_post_implementation_error(client,
				"qdwin_locker_v1: peer process identity could not be "
				"verified");
			return;
		}
	}

	resource = wl_resource_create(client, &qdwin_locker_v1_interface,
				      version, id);
	if (!resource) { wl_client_post_no_memory(client); return; }
	wl_resource_set_implementation(resource, &qdwin_locker_impl, qdwin,
				       qdwin_locker_resource_destroy);
}

/* ------------------------------------------------------------------
 * Background curtain.
 * ------------------------------------------------------------------ */

/* Install/resize an opaque black curtain covering every enabled output.
 * Without it, the pixman renderer leaves stale pixels wherever nothing
 * redraws — old chrome stays after a shrink, minimised views stay
 * visible, etc. Called on init and whenever outputs change. */
static void
qdwin_refresh_background(struct qdwin *qdwin)
{
	int max_x = 0, max_y = 0;
	struct weston_output *out;

	wl_list_for_each(out, &qdwin->compositor->output_list, link) {
		if (out->destroying)
			continue;
		int right = out->pos.c.x + out->width;
		int bottom = out->pos.c.y + out->height;
		if (right > max_x)
			max_x = right;
		if (bottom > max_y)
			max_y = bottom;
	}
	if (max_x == 0 || max_y == 0)
		return;

	if (qdwin->background) {
		weston_shell_utils_curtain_destroy(qdwin->background);
		qdwin->background = NULL;
	}
	struct weston_curtain_params params = {
		.r = 0.0f, .g = 0.0f, .b = 0.0f, .a = 1.0f,
		.pos = { .c = weston_coord(0, 0) },
		.width = max_x,
		.height = max_y,
	};
	qdwin->background =
		weston_shell_utils_curtain_create(qdwin->compositor, &params);
	if (!qdwin->background) {
		weston_log("qdwin: curtain_create failed\n");
		return;
	}
	weston_view_move_to_layer(qdwin->background->view,
				  &qdwin->background_layer.view_list);
	weston_log("qdwin: background curtain %dx%d installed\n",
		   max_x, max_y);

	/* Libweston leaves new outputs repaint-inhibited until the shell has
	 * installed content which makes their first frame safe. The opaque
	 * curtain above is that content. Mark every live output ready only after
	 * it exists in the scene graph; the operation is deliberately idempotent
	 * so output-created and resize refreshes can share this path. Without
	 * this call, damage (including capture preparation) never schedules a
	 * repaint and an output may display only an inherited scanout buffer. */
	wl_list_for_each(out, &qdwin->compositor->output_list, link) {
		if (!out->destroying)
			weston_output_set_ready(out);
	}
}

/* Move the lock curtain to the BOTTOM of lock_layer. weston_layer.view_list
 * is front-to-back (head = topmost); weston_view_move_to_layer() inserts at
 * the head, so we then relink the curtain to the tail. Re-asserted after any
 * path that can restack a real lock view (promote/place/commit) so the curtain
 * always sits behind the actual lock UI. Weston 14 has no
 * weston_layer_entry_insert(), hence the raw wl_list splice. */
static void
qdwin_lock_curtain_to_bottom(struct qdwin *qdwin)
{
	struct weston_view *view;

	if (!qdwin->lock_curtain)
		return;
	view = qdwin->lock_curtain->view;
	if (view->layer_link.layer != &qdwin->lock_layer)
		weston_view_move_to_layer(view, &qdwin->lock_layer.view_list);
	wl_list_remove(&view->layer_link.link);
	wl_list_insert(qdwin->lock_layer.view_list.link.prev,
		       &view->layer_link.link);
	view->layer_link.layer = &qdwin->lock_layer;
	qdwin->compositor->view_list_needs_rebuild = true;
	/* J29: 14->16 weston_surface_damage() removed; dirty the view to force the
	 * relayered lock curtain to recomposite. */
	weston_view_geometry_dirty(view);
}

static void
qdwin_remove_lock_curtain(struct qdwin *qdwin)
{
	if (!qdwin->lock_curtain)
		return;
	weston_shell_utils_curtain_destroy(qdwin->lock_curtain);
	qdwin->lock_curtain = NULL;
	qdwin->compositor->view_list_needs_rebuild = true;
}

/* (Re)create the opaque black lock curtain sized to the spanning output bbox
 * and park it at the bottom of lock_layer. No-op-removes it if not locked, so
 * it is safe to call from output-resize while the lock state is unknown. */
static void
qdwin_install_lock_curtain(struct qdwin *qdwin)
{
	int min_x = 0, min_y = 0, max_x = 0, max_y = 0;
	struct weston_output *out;
	bool have_output = false;

	if (!qdwin->locked) {
		qdwin_remove_lock_curtain(qdwin);
		return;
	}

	/* Full spanning bbox, tracking min AND max so outputs at negative
	 * global coordinates are covered — a lock curtain that missed an
	 * output would leak the desktop there once non-lock layers are hidden. */
	wl_list_for_each(out, &qdwin->compositor->output_list, link) {
		if (out->destroying)
			continue;
		int left = out->pos.c.x, top = out->pos.c.y;
		int right = left + out->width, bottom = top + out->height;
		if (!have_output) {
			min_x = left; min_y = top;
			max_x = right; max_y = bottom;
			have_output = true;
			continue;
		}
		if (left < min_x) min_x = left;
		if (top < min_y) min_y = top;
		if (right > max_x) max_x = right;
		if (bottom > max_y) max_y = bottom;
	}
	if (!have_output || max_x <= min_x || max_y <= min_y) {
		/* No usable output geometry (e.g. all outputs transiently gone
		 * during a hotplug reconfigure). Deliberately KEEP any existing
		 * curtain rather than remove it: while locked, a stale black
		 * curtain is fail-secure; dropping it could momentarily expose
		 * the desktop. output-create/resize will re-install at the
		 * correct bbox once an output returns. */
		return;
	}

	if (qdwin->lock_curtain)
		qdwin_remove_lock_curtain(qdwin);

	struct weston_curtain_params params = {
		.r = 0.0f, .g = 0.0f, .b = 0.0f, .a = 1.0f,
		.pos = { .c = weston_coord(min_x, min_y) },
		.width = max_x - min_x,
		.height = max_y - min_y,
		.capture_input = false,  /* never steal pointer/overlay grab */
	};
	qdwin->lock_curtain =
		weston_shell_utils_curtain_create(qdwin->compositor, &params);
	if (!qdwin->lock_curtain) {
		weston_log("qdwin: lock curtain create failed\n");
		return;
	}
	weston_view_move_to_layer(qdwin->lock_curtain->view,
				  &qdwin->lock_layer.view_list);
	qdwin_lock_curtain_to_bottom(qdwin);
	weston_log("qdwin: lock curtain %dx%d @ (%d,%d) installed\n",
		   max_x - min_x, max_y - min_y, min_x, min_y);
}

/* ------------------------------------------------------------------
 * §6.7 seat / output lifecycle forwarders.
 *
 * Forward `weston_compositor` signals onto the bound shell as v2
 * events. Version-gated on the bound resource: a v1 shell silently
 * does not get them. Replay of existing seats/outputs happens in
 * bind_as_shell.
 * ------------------------------------------------------------------ */

struct qdwin_seat_tracker {
	struct qdwin *qdwin;
	struct weston_seat *seat;
	char *seat_name;
	struct wl_listener destroy_listener;
	struct wl_listener updated_caps_listener;  /* §6.8 S3c */
	struct wl_listener selection_listener;     /* spec/10 */
	struct wl_listener kbd_focus_listener;     /* spec/10 v14 focus-aware */
	int kbd_focus_listener_installed;
	/* The weston_keyboard object kbd_focus_listener is currently bound
	 * to. Used to detect a backend keyboard swap (RDP-headless replaces
	 * the wl_keyboard during peer-activate seat bring-up) and rebind the
	 * focus_signal listener onto the live keyboard even when no
	 * updated_caps_signal reaches us. NULL when no listener installed. */
	struct weston_keyboard *kbd_focus_listener_kbd;
	struct wl_listener pointer_focus_listener; /* B6 default-cursor restore */
	int pointer_focus_listener_installed;
	uint32_t last_focused_handle;              /* dedup seat_focus_changed */
	/* spec/10 v16: last `target_silo` from set_keyboard_focus_v2.
	 * NULL or empty means "unknown". A v2 call with target_silo equal
	 * to this string skips the unconditional pre-focus selection-
	 * clear so a same-silo sink toplevel can still receive an active
	 * data_offer. UINT32_MAX-handle (clear-focus) calls reset to NULL
	 * so the next non-clear v2 call always counts as cross-silo. */
	char *last_target_silo;
	/* Deferred focus-transfer source: scheduled when focus drops to
	 * UINT32_MAX so the recovery runs on the next event-loop pass,
	 * after any concurrent toplevel destroys have finished tearing
	 * down. Doing the transfer synchronously inside the focus_signal
	 * listener loops infinitely when the focused window is itself
	 * being destroyed (its wl_list_remove hasn't fired yet, so the
	 * recovery picks it back up, only for libweston to clear focus
	 * again as the destroy continues). The idle defers past the
	 * destroy chain. NULL when nothing scheduled. */
	struct wl_event_source *focus_recover_idle;
	struct wl_list link;               /* qdwin::seat_trackers */
};

/* Forward decl — defined below near the can_receive helpers. */
static int qdwin_shell_can_receive_v11(struct qdwin *qdwin);
static struct qdwin_toplevel *
qdwin_toplevel_for_keyboard_focus(struct qdwin *qdwin,
				  struct weston_seat *seat);

static int
qdwin_shell_can_receive_v2(struct qdwin *qdwin)
{
	return qdwin->shell_bound && qdwin->shell_resource &&
	       wl_resource_get_version(qdwin->shell_resource) >= 2;
}

static int
qdwin_shell_can_receive_v11(struct qdwin *qdwin)
{
	return qdwin->shell_bound && qdwin->shell_resource &&
	       wl_resource_get_version(qdwin->shell_resource) >= 11;
}

static int
qdwin_shell_can_receive_v14(struct qdwin *qdwin)
{
	return qdwin->shell_bound && qdwin->shell_resource &&
	       wl_resource_get_version(qdwin->shell_resource) >= 14;
}

static int
qdwin_shell_can_receive_v15(struct qdwin *qdwin)
{
	return qdwin->shell_bound && qdwin->shell_resource &&
	       wl_resource_get_version(qdwin->shell_resource) >= 15;
}

/* Walk qdwin->toplevels for the one whose desktop_surface's main view is
 * the seat's keyboard focus. NULL when the focus surface has no qdwin
 * toplevel (compositor-owned chrome/popup, lock surface, or unmapped).
 * Used by selection-set to identify the source toplevel — Wayland only
 * permits clients with keyboard focus to set selection. */
static struct qdwin_toplevel *
qdwin_toplevel_for_keyboard_focus(struct qdwin *qdwin,
				  struct weston_seat *seat)
{
	struct weston_keyboard *kbd = seat ? seat->keyboard_state : NULL;
	struct weston_surface *focus = kbd ? kbd->focus : NULL;
	struct qdwin_toplevel *tl;
	if (!focus)
		return NULL;
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		if (tl->view && tl->view->surface == focus)
			return tl;
		/* §6.8 S2b: a proxy may have its pixel surface as focus. */
		if (tl->proxy_pixel_surface && tl->proxy_pixel_surface == focus)
			return tl;
	}
	return NULL;
}

/* Forward decls — secctx accessors live further down. */
struct qdwin_secctx_client;
static struct qdwin_secctx_client *
qdwin_secctx_client_lookup(struct qdwin *qdwin, struct wl_client *client);
static const char *qdwin_secctx_client_app_id(struct qdwin_secctx_client *sc);

/* Fallback when the selection source's wl_client owns one of qdwin's
 * tracked toplevels but that toplevel isn't currently keyboard-focused
 * (e.g. an RDP backend with no real input delivery, a compositor-grab
 * popup, an off-screen scratch surface). Returns the FIRST matching
 * toplevel — for the broker gate the silo identity is per-wl_client
 * not per-toplevel, so any toplevel from the same wl_client carries
 * the same secctx tag. NULL when no toplevel matches. */
static struct qdwin_toplevel *
qdwin_toplevel_for_client(struct qdwin *qdwin, struct wl_client *client)
{
	if (!client)
		return NULL;
	struct qdwin_toplevel *tl;
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		struct weston_surface *ws = NULL;
		if (tl->desktop_surface)
			ws = weston_desktop_surface_get_surface(
				tl->desktop_surface);
		if (!ws || !ws->resource)
			continue;
		if (wl_resource_get_client(ws->resource) == client)
			return tl;
	}
	return NULL;
}

/* Last-chance fallback: when the selection source comes from a different
 * wl_client than every tracked toplevel (typical of waypipe-tier3 where
 * each app launch creates a fresh waypipe-client wl_client, and the
 * test source-helper is a separate launch from the test window), match
 * on the *secctx app_id* instead. Same silo → same app_id → returns the
 * first toplevel from any wl_client tagged with the same app_id.
 * NULL when the source has no secctx or no toplevel shares it. */
static struct qdwin_toplevel *
qdwin_toplevel_for_secctx_app_id(struct qdwin *qdwin,
				 struct wl_client *client)
{
	if (!client)
		return NULL;
	struct qdwin_secctx_client *src_sc =
		qdwin_secctx_client_lookup(qdwin, client);
	/* deeper-review D3: match on the (engine, app_id) silo identity, not
	 * app_id alone. app_id alone collides across engines — e.g.
	 * engine=qdistro.tier2/app_id=firefox vs engine=qdistro.tier3/app_id=
	 * firefox are *different* silos — which would conflate them and feed the
	 * broker a false "same silo" verdict → cross-silo paste. Adding the
	 * engine closes that collision precisely.
	 *
	 * instance_id is deliberately NOT required: this resolver exists to
	 * bridge a selection *source* to a *toplevel* that are separate launches
	 * within the SAME silo (each app launch is a fresh waypipe-client
	 * wl_client with its own instance_id — see the header comment), so
	 * requiring instance equality would defeat the resolver's whole purpose.
	 * The (engine, app_id) pair is the silo identity; instance distinguishes
	 * launches within it. NULL on any missing component → caller default-
	 * denies and the broker's authoritative VerifyClientIdentity decides. */
	const char *src_engine = qdwin_secctx_client_engine(src_sc);
	const char *src_app_id = qdwin_secctx_client_app_id(src_sc);
	if (!src_engine || !*src_engine || !src_app_id || !*src_app_id)
		return NULL;
	struct qdwin_toplevel *tl;
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		struct weston_surface *ws = NULL;
		if (tl->desktop_surface)
			ws = weston_desktop_surface_get_surface(
				tl->desktop_surface);
		if (!ws || !ws->resource)
			continue;
		struct wl_client *tlc = wl_resource_get_client(ws->resource);
		struct qdwin_secctx_client *tl_sc =
			qdwin_secctx_client_lookup(qdwin, tlc);
		const char *tl_engine = qdwin_secctx_client_engine(tl_sc);
		const char *tl_app_id = qdwin_secctx_client_app_id(tl_sc);
		if (tl_engine && *tl_engine && tl_app_id && *tl_app_id &&
		    strcmp(tl_engine, src_engine) == 0 &&
		    strcmp(tl_app_id, src_app_id) == 0)
			return tl;
	}
	return NULL;
}

/* Cross-process fallback removed: investigated 2026-04-27 evening on
 * qdwin-final-tier45-260427-1419 with diagnostic logging. The waypipe
 * silo→admin set_selection path arrives at qdwin's selection_signal
 * with src->resource == NULL (the data_source carried by the seat is
 * actually weston's own clipboard.c synthetic clipboard_source after
 * the original is read+cached, OR — more importantly — weston's stale-
 * serial guard in weston_seat_set_selection rejects waypipe's serial=0
 * second set when the admin helper just set at serial=0). Either way
 * src_client is NULL by the time qdwin sees it, so a per-PID fallback
 * doesn't run. Fix needs to land before the serial guard — out of
 * scope for a shell plugin. spec/10 §"e2e gate hits weston serial
 * guard" carries the analysis. */

/* Pack mime types from a wl_array of char* into an LF-joined buffer.
 * Caller frees the returned string (NULL when input is empty). The
 * choice of LF (not NUL or comma) keeps the wire arg as a plain
 * Wayland string without escaping; mime types never contain whitespace
 * per RFC 6838.
 *
 * Mime types are ASCII per RFC 6838 §4.2 — the wire is UTF-8 (Wayland
 * strings) and pywayland validates UTF-8 strictly. Some Wayland
 * clients (wl-clipboard, GTK in some configurations) advertise types
 * with non-printable or non-ASCII bytes. We silently sanitize: any
 * byte outside printable-ASCII (0x20..0x7E) gets replaced with '?',
 * and an entry that would collapse to empty is dropped. Logging the
 * substitutions would be noisy and adds no admin value — the gate is
 * still applied, just on a sanitized type name. */
static char *
qdwin_pack_mime_types(struct wl_array *types)
{
	if (!types || types->size == 0)
		return strdup("");
	size_t total = 0;
	char **p;
	wl_array_for_each(p, types) {
		const char *s = p ? *p : NULL;
		if (s)
			total += strlen(s) + 1;  /* +1 for LF or terminating NUL */
	}
	if (total == 0)
		return strdup("");
	char *out = malloc(total + 1);
	if (!out)
		return NULL;
	size_t off = 0;
	int first = 1;
	wl_array_for_each(p, types) {
		const char *s = p ? *p : NULL;
		if (!s)
			continue;
		size_t n = strlen(s);
		if (n == 0)
			continue;
		if (!first)
			out[off++] = '\n';
		for (size_t i = 0; i < n; i++) {
			unsigned char b = (unsigned char)s[i];
			out[off++] = (b >= 0x20 && b <= 0x7E) ? (char)b : '?';
		}
		first = 0;
	}
	out[off] = '\0';
	return out;
}

static void
qdwin_emit_selection_set(struct qdwin *qdwin, struct weston_seat *seat,
			 struct wl_array *mime_types, int is_primary,
			 struct wl_client *src_client_fallback)
{
	if (!qdwin_shell_can_receive_v11(qdwin))
		return;
	struct qdwin_toplevel *tl =
		qdwin_toplevel_for_keyboard_focus(qdwin, seat);
	/* spec/10 fallback: when no toplevel has keyboard focus (RDP
	 * dummy backend, compositor-grab popup, off-screen scratch
	 * surface), fall back to ANY toplevel owned by the data source's
	 * wl_client. The broker gate is per-wl_client not per-toplevel
	 * (silo identity is carried by the security-context tag attached
	 * to the wl_client), so any matching toplevel produces a usable
	 * source_handle. Closes the long-standing s39 e2e hole where
	 * sdl-freerdp /v: dummy never delivered wl_keyboard.enter. */
	if (!tl)
		tl = qdwin_toplevel_for_client(qdwin, src_client_fallback);
	/* Last-chance: a different wl_client may share the source's silo
	 * (typical of waypipe-tier3 where each app launch is a fresh
	 * waypipe-client wl_client). Match on secctx app_id. */
	if (!tl)
		tl = qdwin_toplevel_for_secctx_app_id(qdwin,
						      src_client_fallback);
	uint32_t handle = tl ? tl->handle : UINT32_MAX;
	char *joined = qdwin_pack_mime_types(mime_types);
	const char *name = (seat && seat->seat_name) ? seat->seat_name : "";
	/* v23 sidecar (selection_set_source_identity): when the wl_client
	 * that issued set_selection carries a wp_security_context_v1 tag,
	 * emit its (engine, app_id, instance_id) tuple IMMEDIATELY BEFORE
	 * selection_set. Lets the shell derive src_silo from the wire
	 * (ClipboardGate.qml) instead of from the keyboard-focused
	 * toplevel handle — which collapses to "admin shell" when a
	 * tagged client emits set_selection without owning a focused
	 * toplevel (XWayland helper, waypipe-tier3 bridge, browser-extension
	 * silo proxy). Untagged sources skip the sidecar entirely and the
	 * shell falls back to the v11 focus-handle path. */
	if (wl_resource_get_version(qdwin->shell_resource) >= 23 &&
	    src_client_fallback) {
		struct qdwin_secctx_client *src_sc =
			qdwin_secctx_client_lookup(qdwin, src_client_fallback);
		if (src_sc) {
			const char *src_engine   = qdwin_secctx_client_engine(src_sc);
			const char *src_app_id   = qdwin_secctx_client_app_id(src_sc);
			const char *src_instance = qdwin_secctx_client_instance_id(src_sc);
			qdwin_shell_v1_send_selection_set_source_identity(
				qdwin->shell_resource,
				src_engine   ? src_engine   : "",
				src_app_id   ? src_app_id   : "",
				src_instance ? src_instance : "");
			weston_log("qdwin: selection_set_source_identity "
				   "engine=%s app_id=%s instance=%s\n",
				   src_engine   ? src_engine   : "",
				   src_app_id   ? src_app_id   : "",
				   src_instance ? src_instance : "");
		}
	}
	qdwin_shell_v1_send_selection_set(qdwin->shell_resource,
					  name, handle,
					  joined ? joined : "",
					  is_primary ? 1u : 0u);
	weston_log("qdwin: selection_set seat=%s handle=%u "
		   "primary=%d mimes='%s'\n",
		   name, handle, is_primary,
		   joined ? joined : "");
	free(joined);
}

/* spec/10 v15 — wl_data_offer.receive per-MIME / per-recipient gating.
 *
 * v14's `seat_focus_changed` lets the shell drop the seat selection on
 * cross-silo focus moves — binary, all-or-nothing. v15 adds receive-
 * time policy so the shell can authorise a specific (mime_type,
 * destination) pair against the rules engine while leaving other
 * mime types denied.
 *
 * Mechanism: when
 * selection_signal fires we wrap the new data_source's `send` callback
 * with our shim. libweston later invokes `send(source, mime, fd)` on
 * every wl_data_offer.receive call from a destination. The shim
 * defers the original `send` until the shell answers via
 * `data_offer_receive_decision`. On allow → original_send(fd); on deny
 * → close(fd) (destination sees EOF → empty paste).
 *
 * The wrap is per-source (one weston_data_source = one wrap entry,
 * one libweston `destroy_signal` listener for cleanup). Enforcement is
 * installed independently of shell presence; absent/pre-v15 shells deny.
 * libweston accepts receive only for source->offer, whose public resource
 * retains the authenticated receiver at this synchronous callback boundary.
 */

struct qdwin_data_source_wrap {
	struct qdwin *qdwin;
	struct weston_data_source *source;
	struct weston_seat *seat;
	void (*original_send)(struct weston_data_source *,
			      const char *, int32_t);
	struct wl_listener destroy_listener;
	struct wl_list link;  /* qdwin::data_source_wraps */
};

struct qdwin_data_offer_pending {
	struct qdwin *qdwin;
	struct qdwin_data_source_wrap *wrap;  /* NULL after src destroyed */
	uint32_t handle;
	int fd;        /* -1 once handed off / closed */
	char *mime;
	struct wl_event_source *timeout_source;
	struct wl_listener target_destroy_listener;
	struct wl_list link;  /* qdwin::data_offer_pending */
};

static struct qdwin_data_source_wrap *
qdwin_data_source_wrap_find(struct qdwin *qdwin,
			    struct weston_data_source *src)
{
	struct qdwin_data_source_wrap *w;
	wl_list_for_each(w, &qdwin->data_source_wraps, link) {
		if (w->source == src)
			return w;
	}
	return NULL;
}

static void
qdwin_data_offer_pending_close(struct qdwin_data_offer_pending *p, int allow)
{
	if (allow && p->wrap && p->wrap->original_send && p->fd >= 0) {
		p->wrap->original_send(p->wrap->source, p->mime, p->fd);
	} else if (p->fd >= 0) {
		close(p->fd);
	}
	p->fd = -1;
}

static void
qdwin_data_offer_pending_free(struct qdwin_data_offer_pending *p)
{
	wl_list_remove(&p->target_destroy_listener.link);
	wl_list_remove(&p->link);
	if (p->timeout_source)
		wl_event_source_remove(p->timeout_source);
	if (p->fd >= 0)
		close(p->fd);
	free(p->mime);
	free(p);
}

static void
qdwin_data_offer_pending_free_all(struct qdwin *qdwin)
{
	struct qdwin_data_offer_pending *p, *tmp;
	wl_list_for_each_safe(p, tmp,
			      &qdwin->data_offer_pending, link) {
		qdwin_data_offer_pending_free(p);
	}
}

static void
qdwin_data_offer_target_destroyed(struct wl_listener *listener, void *data)
{
	struct qdwin_data_offer_pending *p =
		wl_container_of(listener, p, target_destroy_listener);
	(void)data;
	qdwin_data_offer_pending_free(p);
}

static int
qdwin_data_offer_pending_timeout_cb(void *data)
{
	struct qdwin_data_offer_pending *p = data;
	weston_log("qdwin: data_offer_receive_pending handle=%u "
		   "timed out → deny\n", p->handle);
	qdwin_data_offer_pending_close(p, 0);
	qdwin_data_offer_pending_free(p);
	return 0;
}

static void
qdwin_data_source_wrap_on_source_destroy(struct wl_listener *l, void *data)
{
	struct qdwin_data_source_wrap *w =
		wl_container_of(l, w, destroy_listener);
	(void)data;
	struct qdwin_data_offer_pending *p, *tmp;
	wl_list_for_each_safe(p, tmp,
			      &w->qdwin->data_offer_pending, link) {
		if (p->wrap != w)
			continue;
		/* Source is going away; pending approvals become inert. */
		qdwin_data_offer_pending_free(p);
	}
	wl_list_remove(&w->link);
	wl_list_remove(&w->destroy_listener.link);
	free(w);
}

static void
qdwin_data_source_wraps_free_all(struct qdwin *qdwin)
{
	struct qdwin_data_source_wrap *w, *tmp;
	wl_list_for_each_safe(w, tmp, &qdwin->data_source_wraps, link) {
		wl_list_remove(&w->link);
		wl_list_remove(&w->destroy_listener.link);
		if (w->source && w->original_send)
			w->source->send = w->original_send;
		free(w);
	}
}

static void qdwin_data_source_send_shim(struct weston_data_source *source,
					 const char *mime, int32_t fd);

/* Install before publishing a selection, including while the shell is down. */
static void
qdwin_install_data_source_wrap(struct qdwin *qdwin,
			       struct weston_seat *seat,
			       struct weston_data_source *src)
{
	if (!qdwin || !src)
		return;
	if (src->send == qdwin_data_source_send_shim)
		return;  /* already wrapped, or permanently denied after wrap OOM */
	if (!src->send)
		return;  /* nothing to wrap */
	struct qdwin_data_source_wrap *w = calloc(1, sizeof(*w));
	if (!w) {
		/* No wrap means the shim denies. Never leave original_send live. */
		src->send = qdwin_data_source_send_shim;
		return;
	}
	w->qdwin = qdwin;
	w->source = src;
	w->seat = seat;
	w->original_send = src->send;
	src->send = qdwin_data_source_send_shim;
	w->destroy_listener.notify = qdwin_data_source_wrap_on_source_destroy;
	wl_signal_add(&src->destroy_signal, &w->destroy_listener);
	wl_list_insert(&qdwin->data_source_wraps, &w->link);
	weston_log("qdwin: v15 data_source wrap installed src=%p seat=%s\n",
		   src,
		   (seat && seat->seat_name) ? seat->seat_name : "(none)");
}

/* libweston invokes our shim on every wl_data_offer.receive against the
 * wrapped source. We hold the fd, advertise to the shell, and wait. */
static void
qdwin_data_source_send_shim(struct weston_data_source *source,
			    const char *mime, int32_t fd)
{
	struct qdwin *qdwin = qdwin_singleton;
	struct qdwin_data_source_wrap *w = qdwin ?
		qdwin_data_source_wrap_find(qdwin, source) : NULL;
	if (!w || !qdwin || !qdwin_shell_can_receive_v15(qdwin)) {
		close(fd);
		return;
	}

	/* F7: bound the externally-supplied mime before we dup + re-broadcast
	 * it to the shell. An absurdly long type is a heap/event DoS; there is
	 * no client wl_resource to post an error on in this send-shim, so fail
	 * closed by dropping the receive (close fd — the requester sees EOF). */
	if (mime && strnlen(mime, QDWIN_MIME_TYPE_MAX + 1u) > QDWIN_MIME_TYPE_MAX) {
		weston_log("qdwin: data_offer.receive dropped — mime too long "
			   "(cap %u)\n", QDWIN_MIME_TYPE_MAX);
		close(fd);
		return;
	}

	/* data_offer_receive checks offer == source->offer immediately before
	 * invoking us. Read that resource, never keyboard focus or a different
	 * connection sharing an app_id. Internal sends without an offer and
	 * proxy endpoints without their own tracked toplevel fail closed. */
	struct wl_client *src_client = source->resource ?
		wl_resource_get_client(source->resource) : NULL;
	struct wl_client *dst_client = source->offer && source->offer->resource ?
		wl_resource_get_client(source->offer->resource) : NULL;
	struct qdwin_toplevel *src_tl = qdwin_toplevel_for_client(qdwin, src_client);
	struct qdwin_toplevel *dst_tl = qdwin_toplevel_for_client(qdwin, dst_client);
	if (!src_tl || !dst_tl) {
		close(fd);
		return;
	}

	struct qdwin_data_offer_pending *p = calloc(1, sizeof(*p));
	if (!p) {
		close(fd);
		return;
	}
	p->qdwin = qdwin;
	p->wrap = w;
	p->fd = fd;
	p->mime = strdup(mime ? mime : "");
	if (!p->mime) {
		close(fd);
		free(p);
		return;
	}
	p->handle = ++qdwin->data_offer_receive_next_handle;
	if (p->handle == 0)
		p->handle = ++qdwin->data_offer_receive_next_handle;
	wl_list_insert(&qdwin->data_offer_pending, &p->link);
	p->target_destroy_listener.notify = qdwin_data_offer_target_destroyed;
	wl_client_add_destroy_listener(dst_client, &p->target_destroy_listener);

	uint32_t source_handle = src_tl->handle;
	uint32_t target_handle = dst_tl->handle;
	const char *seat_name = (w->seat && w->seat->seat_name) ?
		w->seat->seat_name : "";

	struct wl_event_loop *loop =
		wl_display_get_event_loop(qdwin->compositor->wl_display);
	p->timeout_source = wl_event_loop_add_timer(
		loop, qdwin_data_offer_pending_timeout_cb, p);
	if (!p->timeout_source ||
	    wl_event_source_timer_update(p->timeout_source, 2000) < 0) {
		qdwin_data_offer_pending_free(p);
		return;
	}

	qdwin_shell_v1_send_data_offer_receive_pending(
		qdwin->shell_resource, p->handle, seat_name,
		source_handle, target_handle, p->mime);
	weston_log("qdwin: data_offer_receive_pending handle=%u seat=%s "
		   "src=%u dst=%u mime='%s' fd=%d\n",
		   p->handle, seat_name, source_handle, target_handle,
		   p->mime, p->fd);
}

static struct qdwin_data_offer_pending *
qdwin_data_offer_pending_find(struct qdwin *qdwin, uint32_t handle)
{
	struct qdwin_data_offer_pending *p;
	wl_list_for_each(p, &qdwin->data_offer_pending, link)
		if (p->handle == handle)
			return p;
	return NULL;
}

static void
qdwin_handle_data_offer_receive_decision(struct wl_client *client,
					 struct wl_resource *resource,
					 uint32_t handle,
					 const char *decision)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	struct qdwin_data_offer_pending *p =
		qdwin_data_offer_pending_find(qdwin, handle);
	if (!p) {
		weston_log("qdwin: data_offer_receive_decision unknown "
			   "handle=%u\n", handle);
		return;
	}
	int allow = (decision && strcmp(decision, "allow") == 0);
	weston_log("qdwin: data_offer_receive_decision handle=%u → %s\n",
		   handle, allow ? "allow" : "deny");
	qdwin_data_offer_pending_close(p, allow);
	qdwin_data_offer_pending_free(p);
}

/* weston_seat::selection_signal fires after weston_seat_set_selection.
 * data is the seat. We read seat->selection_data_source — NULL on
 * clear, populated on a real set. We only emit on a real set; clears
 * are tracked by the shell separately (it knows it just called
 * clear_selection). */
static void
qdwin_on_seat_selection_changed(struct wl_listener *listener, void *data)
{
	struct qdwin_seat_tracker *tr =
		wl_container_of(listener, tr, selection_listener);
	struct weston_seat *seat = data;
	if (!seat)
		return;
	struct weston_data_source *src = seat->selection_data_source;
	if (!src) {
		/* Selection was cleared — don't emit; the shell either
		 * triggered the clear itself (via clear_selection request)
		 * or the focused client called set_selection(NULL)
		 * voluntarily, in which case there's no policy to apply. */
		return;
	}
	/* spec/10 v15: install the per-source send-shim BEFORE we tell
	 * the shell about the selection. Once the shell starts processing
	 * selection_set, destinations may already begin calling
	 * wl_data_offer.receive; the shim must already be in place to
	 * intercept. Absent/pre-v15 shells remain fail-closed. */
	qdwin_install_data_source_wrap(tr->qdwin, seat, src);
	/* Pass the data source's wl_client so emit_selection_set can
	 * fall back to a toplevel owned by that client when no toplevel
	 * has keyboard focus. */
	struct wl_client *src_client = src->resource ?
		wl_resource_get_client(src->resource) : NULL;
	qdwin_emit_selection_set(tr->qdwin, seat, &src->mime_types, 0,
				 src_client);
}

/* spec/10 v14: shell observes keyboard-focus moves so it can drop the
 * seat selection on cross-silo focus transitions ("Qubes-style"). The
 * compositor side emits `seat_focus_changed` whenever weston_keyboard's
 * focus surface changes; the shell maps surfaces to silos via the
 * toplevel handle and decides whether to call `clear_selection`. */
static void
qdwin_emit_seat_focus_changed(struct qdwin *qdwin, struct weston_seat *seat,
			      uint32_t handle)
{
	if (!qdwin_shell_can_receive_v14(qdwin))
		return;
	const char *name = (seat && seat->seat_name) ? seat->seat_name : "";
	qdwin_shell_v1_send_seat_focus_changed(qdwin->shell_resource,
					       name, handle);
	weston_log("qdwin: seat_focus_changed seat=%s handle=%u\n",
		   name, handle);
}

/* Walk toplevels for one whose primary view surface == s. NULL if s is
 * a panel/lock/popup/proxy or not tracked. */
static struct qdwin_toplevel *
qdwin_toplevel_by_surface(struct qdwin *qdwin, struct weston_surface *s)
{
	if (!s)
		return NULL;
	struct qdwin_toplevel *tl;
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		if (tl->view && tl->view->surface == s)
			return tl;
		if (tl->proxy_pixel_surface && tl->proxy_pixel_surface == s)
			return tl;
	}
	return NULL;
}

static void
qdwin_seat_emit_focus_now(struct qdwin_seat_tracker *tr)
{
	if (!tr || !tr->seat)
		return;
	struct weston_keyboard *kbd = weston_seat_get_keyboard(tr->seat);
	struct weston_surface *focus = kbd ? kbd->focus : NULL;
	struct qdwin_toplevel *tl = qdwin_toplevel_by_surface(tr->qdwin, focus);
	uint32_t handle = tl ? tl->handle : UINT32_MAX;
	if (handle == tr->last_focused_handle)
		return;
	uint32_t prev = tr->last_focused_handle;
	tr->last_focused_handle = handle;
	/* Unconditional ground-truth log — independent of shell binding state
	 * so focus transitions are observable in the journal even before
	 * qdshell has bound qdwin_shell_v1 at v14+ (see
	 * todo/qdwin-focus-events.md). The shell-facing protocol emit below
	 * is still gated on v14 inside qdwin_emit_seat_focus_changed. */
	weston_log("qdwin: focus handle=%u (was %u) seat=%s\n",
		   handle, prev,
		   (tr->seat && tr->seat->seat_name) ? tr->seat->seat_name : "");
	qdwin_emit_seat_focus_changed(tr->qdwin, tr->seat, handle);

	/* Focus-transfer recovery: when focus drops to "no toplevel"
	 * (handle == UINT32_MAX) while live siblings exist, auto-recover
	 * to the next mapped+decorated toplevel. Deferred to the next
	 * event-loop pass via wl_event_loop_add_idle — see the docblock
	 * on focus_recover_idle for why synchronous recovery here
	 * infinite-loops when the dropping window is itself mid-destroy.
	 *
	 * Skipped when locked (locker grab owns focus). One idle queued
	 * per tracker at a time; if focus oscillates we'd over-schedule
	 * but the de-dupe at the top of this function makes the second
	 * idle a no-op. */
	if (handle == UINT32_MAX && kbd && !tr->qdwin->locked &&
	    !tr->focus_recover_idle) {
		struct wl_event_loop *loop = wl_display_get_event_loop(
			tr->qdwin->compositor->wl_display);
		if (loop)
			tr->focus_recover_idle = wl_event_loop_add_idle(
				loop, qdwin_seat_focus_recover_idle_cb, tr);
	}
}

/* Idle callback firing one event-loop iteration after focus dropped
 * to UINT32_MAX. Re-checks state (focus might have legitimately moved
 * elsewhere in the meantime) and assigns focus to the head-most
 * mapped+decorated toplevel if any remain. See the focus_recover_idle
 * docblock for the rationale on deferring rather than recovering
 * synchronously. */
static void
qdwin_seat_focus_recover_idle_cb(void *data)
{
	struct qdwin_seat_tracker *tr = data;
	tr->focus_recover_idle = NULL;
	if (!tr->seat || !tr->qdwin || tr->qdwin->locked)
		return;
	struct weston_keyboard *kbd = weston_seat_get_keyboard(tr->seat);
	if (!kbd)
		return;
	/* A dismissed popup can remain as keyboard->focus for the event-loop
	 * turn in which its wl_surface becomes unmapped.  Treat that stale focus
	 * exactly like NULL so closing a modal returns input to a live sibling.
	 * A genuinely mapped popup or toplevel still owns focus and must not be
	 * pre-empted by recovery. */
	if (kbd->focus && weston_surface_is_mapped(kbd->focus))
		return;
	struct qdwin_toplevel *succ = NULL;
	struct qdwin_toplevel *cand;
	wl_list_for_each(cand, &tr->qdwin->toplevels, link) {
		/* v24: only recover focus to a visible window — one that is
		 * decorated, mapped, not minimised, and on the active
		 * workspace. Skips windows parked on the hidden workspace
		 * layer. */
		if (qdwin_toplevel_is_visible(cand)) {
			succ = cand;
			break;
		}
	}
	if (succ)
		/* Unlike bare weston_keyboard_set_focus(), activation also emits
		 * libweston's activate_signal. XWayland uses that signal to move
		 * real X11 input focus back from a dismissed transient. */
		weston_view_activate_input(succ->view, tr->seat,
					   WESTON_ACTIVATE_FLAG_CONFIGURE);
}

/* ==================================================================
 * v24 workspaces — compositor mechanics + ext-workspace-v1 server.
 *
 * Mechanics: each toplevel carries a `workspace` index. Only toplevels
 * on qdwin->active_workspace are composited; the rest are parked on the
 * (unmapped) workspace_hidden_layer — the same hide mechanism weston's
 * reference desktop-shell uses for inactive workspaces. The standard
 * ext-workspace-v1 protocol is the control/observation surface; the
 * per-window→workspace mapping the bar needs for occupancy rides the
 * qdwin_shell_v1.toplevel_workspace sidecar (emitted elsewhere).
 * See todo/decisions/qdwin-workspaces-ext-protocol.md.
 * ================================================================== */

/* A toplevel is "visible" when it would actually paint on the current
 * workspace: decorated (past the held bystander layer), not minimised,
 * on the active workspace, and backed by a mapped surface. */
static bool
qdwin_toplevel_is_visible(struct qdwin_toplevel *tl)
{
	if (!tl || !tl->qdwin || !tl->decorated)
		return false;
	if (tl->state & QDWIN_TS_MINIMIZED)
		return false;
	if (tl->workspace != tl->qdwin->active_workspace)
		return false;
	return tl->view && tl->view->surface &&
	       weston_surface_is_mapped(tl->view->surface);
}

/* Park or restore a toplevel's views according to its workspace vs the
 * active one. No-op for state the workspace layer must not touch:
 * nested-inner toplevels (no workspaces in nested mode), the locker UI
 * (LOCK layer), still-held windows (release_holding re-evaluates), and
 * minimised windows (stay on the minimized layer until unminimised). */
static void
qdwin_toplevel_apply_workspace_visibility(struct qdwin_toplevel *tl)
{
	struct qdwin *qdwin;
	if (!tl || !tl->qdwin)
		return;
	qdwin = tl->qdwin;
	if (qdwin->nested_mode)
		return;
	if (qdwin_toplevel_is_locker_ui(qdwin, tl))
		return;
	if (!tl->decorated)
		return;
	if (tl->state & QDWIN_TS_MINIMIZED)
		return;
	if (tl->workspace == qdwin->active_workspace)
		qdwin_toplevel_move_to_layer(tl, &qdwin->normal_layer);
	else
		qdwin_toplevel_move_to_layer(tl, &qdwin->workspace_hidden_layer);
}

/* After a workspace change, fix keyboard focus on every seat: keep the
 * current focus if it is still visible, otherwise move to the front-most
 * visible toplevel on the active workspace (or clear focus when the
 * workspace is empty). Mirrors the auto-focus-on-map contract. */
static void
qdwin_workspace_refocus_seats(struct qdwin *qdwin)
{
	struct qdwin_toplevel *want = NULL, *cand;
	struct weston_seat *seat;
	if (qdwin->locked)
		return;  /* locker grab owns focus while locked */
	wl_list_for_each(cand, &qdwin->toplevels, link) {
		if (qdwin_toplevel_is_visible(cand)) {
			want = cand;
			break;
		}
	}
	wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
		struct weston_keyboard *kbd = weston_seat_get_keyboard(seat);
		struct qdwin_toplevel *cur;
		if (!kbd)
			continue;
		cur = qdwin_toplevel_by_surface(qdwin, kbd->focus);
		if (cur && qdwin_toplevel_is_visible(cur))
			continue;  /* still valid on the active workspace */
		if (want)
			weston_view_activate_input(want->view, seat,
						   WESTON_ACTIVATE_FLAG_CONFIGURE);
		else
			weston_keyboard_set_focus(kbd, NULL);
		qdwin_seat_emit_focus_now(qdwin_seat_tracker_for_seat(qdwin, seat));
	}
}

/* Switch the active workspace. Out-of-range is ignored. Always
 * re-broadcasts ext-workspace state (so an activate of the already-active
 * workspace is a confirmation, matching the protocol's best-effort
 * semantics). */
static void
qdwin_set_active_workspace(struct qdwin *qdwin, uint32_t index)
{
	struct qdwin_toplevel *tl;
	if (index >= qdwin->workspace_count) {
		weston_log("qdwin: set_active_workspace ignored index=%u (count=%u)\n",
			   index, qdwin->workspace_count);
		return;
	}
	if (index != qdwin->active_workspace) {
		qdwin->active_workspace = index;
		wl_list_for_each(tl, &qdwin->toplevels, link)
			qdwin_toplevel_apply_workspace_visibility(tl);
		qdwin_workspace_refocus_seats(qdwin);
		weston_compositor_schedule_repaint(qdwin->compositor);
		weston_log("qdwin: active_workspace=%u/%u\n",
			   qdwin->active_workspace, qdwin->workspace_count);
	}
	qdwin_ext_ws_broadcast_state(qdwin);
}

/* Append a workspace (shell drove the count up). */
static void
qdwin_workspace_create(struct qdwin *qdwin)
{
	if (qdwin->workspace_count >= QDWIN_MAX_WORKSPACES) {
		weston_log("qdwin: create_workspace ignored — at max %u\n",
			   (unsigned)QDWIN_MAX_WORKSPACES);
		return;
	}
	qdwin->workspace_count++;
	weston_log("qdwin: workspace created, count=%u\n", qdwin->workspace_count);
	qdwin_ext_ws_resync_all(qdwin);
}

/* Remove workspace `index`. Workspaces are positional ("1".."N"), so
 * windows on the removed workspace fall back one slot and windows above
 * it shift down to keep indices contiguous. The active index and every
 * window's visibility are recomputed. */
static void
qdwin_workspace_remove(struct qdwin *qdwin, uint32_t index)
{
	struct qdwin_toplevel *tl;
	if (qdwin->workspace_count <= 1) {
		weston_log("qdwin: remove_workspace ignored — at min 1\n");
		return;
	}
	if (index >= qdwin->workspace_count)
		return;
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		if (tl->workspace == index)
			tl->workspace = (index > 0) ? index - 1 : 0;
		else if (tl->workspace > index)
			tl->workspace--;
	}
	qdwin->workspace_count--;
	if (qdwin->active_workspace > index)
		qdwin->active_workspace--;
	else if (qdwin->active_workspace == index && index > 0)
		qdwin->active_workspace--;
	if (qdwin->active_workspace >= qdwin->workspace_count)
		qdwin->active_workspace = qdwin->workspace_count - 1;
	wl_list_for_each(tl, &qdwin->toplevels, link)
		qdwin_toplevel_apply_workspace_visibility(tl);
	qdwin_workspace_refocus_seats(qdwin);
	weston_compositor_schedule_repaint(qdwin->compositor);
	weston_log("qdwin: workspace %u removed, count=%u active=%u\n",
		   index, qdwin->workspace_count, qdwin->active_workspace);
	/* Window→workspace assignments shifted; refresh the sidecar. */
	wl_list_for_each(tl, &qdwin->toplevels, link)
		qdwin_emit_toplevel_workspace(qdwin, tl);
	qdwin_ext_ws_resync_all(qdwin);
}

/* ---- ext-workspace-v1 server objects ---------------------------- */

/* A request staged on a manager between commits. The protocol requires
 * activate/create_workspace/remove to take effect atomically on `commit`,
 * not eagerly when the request arrives. */
enum qdwin_ext_ws_pending_kind {
	QDWIN_EXT_WS_PENDING_ACTIVATE,   /* activate handle at .index */
	QDWIN_EXT_WS_PENDING_CREATE,     /* create a new workspace */
	QDWIN_EXT_WS_PENDING_REMOVE,     /* remove handle at .index */
};

struct qdwin_ext_ws_pending_op {
	struct wl_list link;             /* qdwin_ext_ws_manager::pending */
	enum qdwin_ext_ws_pending_kind kind;
	uint32_t index;                  /* workspace index for ACTIVATE/REMOVE */
};

struct qdwin_ext_ws_manager {
	struct wl_list link;             /* qdwin::ext_ws_managers */
	struct qdwin *qdwin;
	struct wl_resource *resource;    /* ext_workspace_manager_v1 */
	struct wl_resource *group;       /* the single ext_workspace_group_handle_v1 */
	struct wl_resource *handles[QDWIN_MAX_WORKSPACES];  /* per workspace index */
	uint32_t handle_count;
	struct wl_list pending;          /* staged ops, applied on commit */
	bool stopped;
};

/* Per-handle back-reference so a handle request resolves to its
 * (manager, workspace index). */
struct qdwin_ext_ws_handle_ref {
	struct qdwin_ext_ws_manager *mgr;
	uint32_t index;
};

static void
qdwin_ext_ws_send_handle_details(struct qdwin *qdwin,
				 struct wl_resource *h, uint32_t index)
{
	char namebuf[16];
	char idbuf[24];
	uint32_t state = (index == qdwin->active_workspace)
		? EXT_WORKSPACE_HANDLE_V1_STATE_ACTIVE : 0;
	struct wl_array coords;
	uint32_t *c;
	const char *name;
	snprintf(idbuf, sizeof idbuf, "qdwin-ws-%u", index);
	snprintf(namebuf, sizeof namebuf, "%u", index + 1u);
	/* id: stable across the workspace's lifetime (positional).
	 * name: the user's custom name pushed via
	 * qdwin_shell_v1.set_workspace_name (v27), so EVERY ext-workspace
	 * client sees it — falling back to the positional "1".."N" when the
	 * shell never set one (empty/NULL). coordinates: 1-D = index, so a
	 * bar can order cells. */
	name = (index < QDWIN_MAX_WORKSPACES &&
		qdwin->workspace_names[index] &&
		qdwin->workspace_names[index][0])
		? qdwin->workspace_names[index] : namebuf;
	ext_workspace_handle_v1_send_id(h, idbuf);
	ext_workspace_handle_v1_send_name(h, name);
	wl_array_init(&coords);
	c = wl_array_add(&coords, sizeof *c);
	if (c)
		*c = index;
	ext_workspace_handle_v1_send_coordinates(h, &coords);
	wl_array_release(&coords);
	ext_workspace_handle_v1_send_capabilities(h,
		EXT_WORKSPACE_HANDLE_V1_WORKSPACE_CAPABILITIES_ACTIVATE |
		EXT_WORKSPACE_HANDLE_V1_WORKSPACE_CAPABILITIES_REMOVE);
	ext_workspace_handle_v1_send_state(h, state);
}

static void qdwin_ext_ws_handle_resource_destroy(struct wl_resource *resource);
static const struct ext_workspace_handle_v1_interface qdwin_ext_ws_handle_impl;

static void
qdwin_ext_ws_manager_create_handles(struct qdwin_ext_ws_manager *mgr)
{
	struct qdwin *qdwin = mgr->qdwin;
	struct wl_client *client = wl_resource_get_client(mgr->resource);
	uint32_t ver = wl_resource_get_version(mgr->resource);
	uint32_t i;
	for (i = 0; i < qdwin->workspace_count && i < QDWIN_MAX_WORKSPACES; i++) {
		struct qdwin_ext_ws_handle_ref *ref;
		struct wl_resource *h = wl_resource_create(client,
			&ext_workspace_handle_v1_interface, ver, 0);
		if (!h)
			continue;
		ref = calloc(1, sizeof *ref);
		if (!ref) {
			wl_resource_destroy(h);
			continue;
		}
		ref->mgr = mgr;
		ref->index = i;
		wl_resource_set_implementation(h, &qdwin_ext_ws_handle_impl, ref,
					       qdwin_ext_ws_handle_resource_destroy);
		mgr->handles[i] = h;
		ext_workspace_manager_v1_send_workspace(mgr->resource, h);
		qdwin_ext_ws_send_handle_details(qdwin, h, i);
		if (mgr->group)
			ext_workspace_group_handle_v1_send_workspace_enter(
				mgr->group, h);
	}
	mgr->handle_count = i;
}

static void
qdwin_ext_ws_manager_destroy_handles(struct qdwin_ext_ws_manager *mgr)
{
	uint32_t i;
	for (i = 0; i < mgr->handle_count; i++) {
		struct wl_resource *h = mgr->handles[i];
		struct qdwin_ext_ws_handle_ref *ref;
		if (!h)
			continue;
		if (mgr->group)
			ext_workspace_group_handle_v1_send_workspace_leave(
				mgr->group, h);
		ext_workspace_handle_v1_send_removed(h);
		/* Per spec the handle is inert after `removed` — only `destroy`
		 * is honoured. Sever the ref's manager link so a stale
		 * activate/remove on the soon-to-be-recreated index becomes a
		 * no-op (its handler null-checks ref->mgr). The resource stays
		 * alive until the client destroys it; the destructor then frees
		 * the ref (and won't touch our slot since mgr is NULL). */
		ref = wl_resource_get_user_data(h);
		if (ref)
			ref->mgr = NULL;
		mgr->handles[i] = NULL;
	}
	mgr->handle_count = 0;
}

static void
qdwin_ext_ws_manager_resync(struct qdwin_ext_ws_manager *mgr)
{
	if (mgr->stopped)
		return;
	qdwin_ext_ws_manager_destroy_handles(mgr);
	qdwin_ext_ws_manager_create_handles(mgr);
	ext_workspace_manager_v1_send_done(mgr->resource);
}

static void
qdwin_ext_ws_resync_all(struct qdwin *qdwin)
{
	struct qdwin_ext_ws_manager *mgr;
	wl_list_for_each(mgr, &qdwin->ext_ws_managers, link)
		qdwin_ext_ws_manager_resync(mgr);
}

static void
qdwin_ext_ws_broadcast_state(struct qdwin *qdwin)
{
	struct qdwin_ext_ws_manager *mgr;
	wl_list_for_each(mgr, &qdwin->ext_ws_managers, link) {
		uint32_t i;
		bool sent = false;
		if (mgr->stopped)
			continue;
		for (i = 0; i < mgr->handle_count && i < qdwin->workspace_count; i++) {
			uint32_t state;
			if (!mgr->handles[i])
				continue;
			state = (i == qdwin->active_workspace)
				? EXT_WORKSPACE_HANDLE_V1_STATE_ACTIVE : 0;
			ext_workspace_handle_v1_send_state(mgr->handles[i], state);
			sent = true;
		}
		if (sent)
			ext_workspace_manager_v1_send_done(mgr->resource);
	}
}

/* v27: re-send ext_workspace_handle_v1.name for one workspace index on every
 * live manager after the shell changed (or cleared) its name. The name value
 * is recomputed from qdwin->workspace_names[index] (positional fallback when
 * unset) — identical to what qdwin_ext_ws_send_handle_details would emit. No
 * resync of the handle list: only the name changed. */
static void
qdwin_ext_ws_broadcast_name(struct qdwin *qdwin, uint32_t index)
{
	struct qdwin_ext_ws_manager *mgr;
	char namebuf[16];
	const char *name;
	if (index >= QDWIN_MAX_WORKSPACES)
		return;
	snprintf(namebuf, sizeof namebuf, "%u", index + 1u);
	name = (qdwin->workspace_names[index] && qdwin->workspace_names[index][0])
		? qdwin->workspace_names[index] : namebuf;
	wl_list_for_each(mgr, &qdwin->ext_ws_managers, link) {
		if (mgr->stopped)
			continue;
		if (index >= mgr->handle_count || !mgr->handles[index])
			continue;
		ext_workspace_handle_v1_send_name(mgr->handles[index], name);
		ext_workspace_manager_v1_send_done(mgr->resource);
	}
}

/* ---- atomic-commit staging ---------------------------------------
 * activate/create_workspace/remove are buffered per manager resource and
 * applied as a batch from the manager's `commit` handler. */

static void
qdwin_ext_ws_pending_clear(struct qdwin_ext_ws_manager *mgr)
{
	struct qdwin_ext_ws_pending_op *op, *tmp;
	wl_list_for_each_safe(op, tmp, &mgr->pending, link) {
		wl_list_remove(&op->link);
		free(op);
	}
}

static void
qdwin_ext_ws_pending_add(struct qdwin_ext_ws_manager *mgr,
			 enum qdwin_ext_ws_pending_kind kind, uint32_t index)
{
	struct qdwin_ext_ws_pending_op *op = calloc(1, sizeof *op);
	if (!op) {
		wl_client_post_no_memory(wl_resource_get_client(mgr->resource));
		return;
	}
	op->kind = kind;
	op->index = index;
	/* Preserve request order: requests are replayed front-to-back. */
	wl_list_insert(mgr->pending.prev, &op->link);
}

static void
qdwin_ext_ws_manager_flush(struct qdwin_ext_ws_manager *mgr)
{
	struct qdwin *qdwin = mgr->qdwin;
	struct qdwin_ext_ws_pending_op *op, *tmp;
	if (mgr->stopped || !qdwin) {
		qdwin_ext_ws_pending_clear(mgr);
		return;
	}
	wl_list_for_each_safe(op, tmp, &mgr->pending, link) {
		switch (op->kind) {
		case QDWIN_EXT_WS_PENDING_ACTIVATE:
			qdwin_set_active_workspace(qdwin, op->index);
			break;
		case QDWIN_EXT_WS_PENDING_CREATE:
			qdwin_workspace_create(qdwin);
			break;
		case QDWIN_EXT_WS_PENDING_REMOVE:
			qdwin_workspace_remove(qdwin, op->index);
			break;
		}
		wl_list_remove(&op->link);
		free(op);
	}
}

/* ---- ext_workspace_handle_v1 requests ---- */

static void
qdwin_ext_ws_handle_destroy(struct wl_client *client, struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_ext_ws_handle_activate(struct wl_client *client, struct wl_resource *resource)
{
	struct qdwin_ext_ws_handle_ref *ref = wl_resource_get_user_data(resource);
	(void)client;
	if (!ref || !ref->mgr || !ref->mgr->qdwin)
		return;
	/* Staged until commit (atomic per protocol). */
	qdwin_ext_ws_pending_add(ref->mgr, QDWIN_EXT_WS_PENDING_ACTIVATE,
				 ref->index);
}

static void
qdwin_ext_ws_handle_deactivate(struct wl_client *client, struct wl_resource *resource)
{
	/* Single-active model: there is always exactly one active
	 * workspace, so an explicit deactivate is a no-op. */
	(void)client; (void)resource;
}

static void
qdwin_ext_ws_handle_assign(struct wl_client *client, struct wl_resource *resource,
			   struct wl_resource *group)
{
	/* One group spans the whole desktop; reassigning is a no-op. */
	(void)client; (void)resource; (void)group;
}

static void
qdwin_ext_ws_handle_remove(struct wl_client *client, struct wl_resource *resource)
{
	struct qdwin_ext_ws_handle_ref *ref = wl_resource_get_user_data(resource);
	(void)client;
	if (!ref || !ref->mgr || !ref->mgr->qdwin)
		return;
	/* Staged until commit (atomic per protocol). */
	qdwin_ext_ws_pending_add(ref->mgr, QDWIN_EXT_WS_PENDING_REMOVE,
				 ref->index);
}

static const struct ext_workspace_handle_v1_interface qdwin_ext_ws_handle_impl = {
	.destroy = qdwin_ext_ws_handle_destroy,
	.activate = qdwin_ext_ws_handle_activate,
	.deactivate = qdwin_ext_ws_handle_deactivate,
	.assign = qdwin_ext_ws_handle_assign,
	.remove = qdwin_ext_ws_handle_remove,
};

static void
qdwin_ext_ws_handle_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_ext_ws_handle_ref *ref = wl_resource_get_user_data(resource);
	if (!ref)
		return;
	if (ref->mgr && ref->index < QDWIN_MAX_WORKSPACES &&
	    ref->mgr->handles[ref->index] == resource)
		ref->mgr->handles[ref->index] = NULL;
	free(ref);
}

/* ---- ext_workspace_group_handle_v1 requests ---- */

static void
qdwin_ext_ws_group_create_workspace(struct wl_client *client,
				    struct wl_resource *resource,
				    const char *workspace)
{
	struct qdwin_ext_ws_manager *mgr = wl_resource_get_user_data(resource);
	(void)client; (void)workspace;  /* name is positional; ignored */
	if (!mgr || !mgr->qdwin)
		return;
	/* Staged until commit (atomic per protocol). */
	qdwin_ext_ws_pending_add(mgr, QDWIN_EXT_WS_PENDING_CREATE, 0);
}

static void
qdwin_ext_ws_group_destroy(struct wl_client *client, struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct ext_workspace_group_handle_v1_interface qdwin_ext_ws_group_impl = {
	.create_workspace = qdwin_ext_ws_group_create_workspace,
	.destroy = qdwin_ext_ws_group_destroy,
};

static void
qdwin_ext_ws_group_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_ext_ws_manager *mgr = wl_resource_get_user_data(resource);
	if (mgr && mgr->group == resource)
		mgr->group = NULL;
}

/* ---- ext_workspace_manager_v1 requests ---- */

static void
qdwin_ext_ws_manager_commit(struct wl_client *client, struct wl_resource *resource)
{
	struct qdwin_ext_ws_manager *mgr = wl_resource_get_user_data(resource);
	(void)client;
	if (!mgr)
		return;
	/* Apply the batch of requests staged since the last commit
	 * atomically. The applied workspace ops broadcast/resync to all
	 * managers themselves. */
	qdwin_ext_ws_manager_flush(mgr);
}

static void
qdwin_ext_ws_manager_stop(struct wl_client *client, struct wl_resource *resource)
{
	struct qdwin_ext_ws_manager *mgr = wl_resource_get_user_data(resource);
	(void)client;
	if (!mgr)
		return;
	mgr->stopped = true;
	/* Per spec, requests staged but never committed are discarded. */
	qdwin_ext_ws_pending_clear(mgr);
	ext_workspace_manager_v1_send_finished(mgr->resource);
	/* finished is a destructor event — tear the resource down now. The
	 * manager resource destructor frees the struct and unlinks it. */
	wl_resource_destroy(resource);
}

static const struct ext_workspace_manager_v1_interface qdwin_ext_ws_manager_impl = {
	.commit = qdwin_ext_ws_manager_commit,
	.stop = qdwin_ext_ws_manager_stop,
};

static void
qdwin_ext_ws_manager_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_ext_ws_manager *mgr = wl_resource_get_user_data(resource);
	uint32_t i;
	if (!mgr)
		return;
	wl_list_remove(&mgr->link);
	qdwin_ext_ws_pending_clear(mgr);
	/* Null back-pointers so any late group/handle destructor (the
	 * client may destroy them after the manager) does not deref freed
	 * memory. The resources themselves are destroyed by the client. */
	if (mgr->group)
		wl_resource_set_user_data(mgr->group, NULL);
	for (i = 0; i < QDWIN_MAX_WORKSPACES; i++) {
		if (mgr->handles[i]) {
			struct qdwin_ext_ws_handle_ref *ref =
				wl_resource_get_user_data(mgr->handles[i]);
			if (ref)
				ref->mgr = NULL;
		}
	}
	free(mgr);
}

/* Find the wl_output protocol object that `client` has bound for `output`,
 * or NULL if it hasn't bound that output's global yet. wl_output resources
 * live on the driving weston_head's resource_list. */
static struct wl_resource *
qdwin_output_resource_for_client(struct weston_output *output,
				 struct wl_client *client)
{
	struct weston_head *head;
	wl_list_for_each(head, &output->head_list, output_link) {
		struct wl_resource *res;
		wl_resource_for_each(res, &head->resource_list) {
			if (wl_resource_get_client(res) == client)
				return res;
		}
	}
	return NULL;
}

/* Associate the desktop-spanning group with each output this client can see
 * (output_enter). Per-monitor bars key off this to know the group covers
 * their output. Skips outputs the client hasn't bound yet. */
static void
qdwin_ext_ws_group_send_output_enters(struct qdwin_ext_ws_manager *mgr)
{
	struct qdwin *qdwin = mgr->qdwin;
	struct wl_client *client = wl_resource_get_client(mgr->resource);
	struct weston_output *output;
	if (!mgr->group)
		return;
	wl_list_for_each(output, &qdwin->compositor->output_list, link) {
		struct wl_resource *ores =
			qdwin_output_resource_for_client(output, client);
		if (ores)
			ext_workspace_group_handle_v1_send_output_enter(
				mgr->group, ores);
	}
}

/* Hotplug: an output appeared or disappeared. Notify every live manager's
 * group so per-monitor bars track the new/removed wl_output. `enter` selects
 * output_enter vs output_leave. */
static void
qdwin_ext_ws_broadcast_output(struct qdwin *qdwin, struct weston_output *output,
			      bool enter)
{
	struct qdwin_ext_ws_manager *mgr;
	wl_list_for_each(mgr, &qdwin->ext_ws_managers, link) {
		struct wl_resource *ores;
		if (mgr->stopped || !mgr->group)
			continue;
		ores = qdwin_output_resource_for_client(output,
				wl_resource_get_client(mgr->resource));
		if (!ores)
			continue;
		if (enter)
			ext_workspace_group_handle_v1_send_output_enter(
				mgr->group, ores);
		else
			ext_workspace_group_handle_v1_send_output_leave(
				mgr->group, ores);
		ext_workspace_manager_v1_send_done(mgr->resource);
	}
}

static void
bind_ext_workspace_manager(struct wl_client *client, void *data,
			   uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct qdwin_ext_ws_manager *mgr = calloc(1, sizeof *mgr);
	if (!mgr) {
		wl_client_post_no_memory(client);
		return;
	}
	mgr->qdwin = qdwin;
	wl_list_init(&mgr->pending);
	mgr->resource = wl_resource_create(client,
		&ext_workspace_manager_v1_interface, version, id);
	if (!mgr->resource) {
		free(mgr);
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(mgr->resource, &qdwin_ext_ws_manager_impl,
				       mgr, qdwin_ext_ws_manager_resource_destroy);
	wl_list_insert(&qdwin->ext_ws_managers, &mgr->link);

	mgr->group = wl_resource_create(client,
		&ext_workspace_group_handle_v1_interface, version, 0);
	if (mgr->group) {
		wl_resource_set_implementation(mgr->group,
			&qdwin_ext_ws_group_impl, mgr,
			qdwin_ext_ws_group_resource_destroy);
		ext_workspace_manager_v1_send_workspace_group(mgr->resource,
							      mgr->group);
		ext_workspace_group_handle_v1_send_capabilities(mgr->group,
			EXT_WORKSPACE_GROUP_HANDLE_V1_GROUP_CAPABILITIES_CREATE_WORKSPACE);
		/* Associate the group with the client's outputs so per-monitor
		 * bars know this group spans them. */
		qdwin_ext_ws_group_send_output_enters(mgr);
	}
	qdwin_ext_ws_manager_create_handles(mgr);
	ext_workspace_manager_v1_send_done(mgr->resource);
	weston_log("qdwin: ext_workspace_manager bound (count=%u active=%u)\n",
		   qdwin->workspace_count, qdwin->active_workspace);
}

/* ==================================================================
 * Output (display) management — wlr-output-management-unstable-v1.
 *
 * qdwin implements the standard wlroots output-management protocol so the
 * qdshell Display layout tab (and tools like wlr-randr / kanshi) can
 * enumerate heads + modes and apply a whole layout ATOMICALLY with a
 * test/apply boundary. We model the producer/consumer exactly on the
 * ext-workspace-v1 code above: one global advertised to all clients, per-
 * manager resource lists, full re-sync on output hotplug, inert objects on
 * teardown.
 *
 * Live-apply surface (what weston-16 can enact on the running backend):
 *   - position   weston_output_move()
 *   - scale      weston_output_set_scale()
 *   - transform  weston_output_set_transform()  (rotation/flip)
 *   - mode       weston_output_mode_switch_to_temporary()
 *   - enable/disable  weston_output_enable()/weston_output_disable()
 * If any leg fails mid-apply we ROLL BACK every output we already touched to
 * its captured prior state and answer `failed` — never a partial layout.
 *
 * Untrusted strings: head name/description/make/model/serial come straight
 * from libweston (DRM connector + EDID). We forward them as protocol strings
 * (the client renders them as PlainText); qdwin never shell-interpolates
 * them. See todo/decisions/qdwin-output-management.md.
 * ================================================================== */

#define QDWIN_OM_VERSION 4  /* zwlr_output_manager_v1 version we advertise */

struct qdwin_om_manager {
	struct wl_list link;            /* qdwin::om_managers */
	struct qdwin *qdwin;
	struct wl_resource *resource;   /* zwlr_output_manager_v1 */
	struct wl_list heads;           /* qdwin_om_head::link */
	bool stopped;
	/* Authorization snapshot taken at bind time: whether the binding client
	 * is the trusted shell and may therefore drive the apply/test mutation
	 * path. Enumeration stays open regardless. */
	bool may_mutate;
};

struct qdwin_om_mode {
	struct wl_list link;            /* qdwin_om_head::modes */
	struct qdwin_om_head *head;
	struct wl_resource *resource;   /* zwlr_output_mode_v1 */
	struct weston_mode *mode;       /* the weston mode this advertises */
};

struct qdwin_om_head {
	struct wl_list link;            /* qdwin_om_manager::heads */
	struct qdwin_om_manager *mgr;
	struct wl_resource *resource;   /* zwlr_output_head_v1 */
	struct weston_head *head;       /* the libweston head this represents */
	struct weston_output *output;   /* head's output, or NULL if disabled */
	struct wl_list modes;           /* qdwin_om_mode::link */
};

/* ---- captured per-output state, for atomic rollback ---- */
struct qdwin_om_saved {
	struct weston_output *output;
	bool was_enabled;
	struct weston_coord_global pos;
	int32_t scale;
	uint32_t transform;
	struct weston_mode *mode;
};

/* ---- a single head's requested config inside a configuration object ---- */
struct qdwin_om_cfg_head {
	struct wl_list link;            /* qdwin_om_config::cfg_heads */
	struct wl_resource *resource;   /* zwlr_output_configuration_head_v1 */
	struct qdwin_om_config *config;
	struct weston_head *head;       /* head target (dedup key; never NULL) */
	struct weston_output *output;   /* head's output, or NULL if disabled */
	bool enabled;                   /* enable_head vs disable_head */
	/* set_* flags + values; unset legs keep the output's current value */
	bool set_mode;
	struct weston_mode *mode;       /* an existing mode (set_mode) */
	bool set_custom_mode;
	int32_t custom_w, custom_h, custom_refresh;
	bool set_position;
	int32_t pos_x, pos_y;
	bool set_transform;
	uint32_t transform;
	bool set_scale;
	wl_fixed_t scale;
};

struct qdwin_om_config {
	struct qdwin *qdwin;
	struct wl_resource *resource;   /* zwlr_output_configuration_v1 */
	uint32_t serial;                /* serial passed at create time */
	bool used;                      /* apply/test already issued */
	bool may_mutate;                /* inherited from the binding manager */
	struct wl_list cfg_heads;       /* qdwin_om_cfg_head::link */
};

static const struct zwlr_output_head_v1_interface qdwin_om_head_impl;
static const struct zwlr_output_mode_v1_interface qdwin_om_mode_impl;
static const struct zwlr_output_configuration_v1_interface qdwin_om_config_impl;
static const struct zwlr_output_configuration_head_v1_interface
	qdwin_om_cfg_head_impl;
static void qdwin_om_head_resource_destroy(struct wl_resource *resource);
static void qdwin_om_mode_resource_destroy(struct wl_resource *resource);

/* Find the qdwin_om_head a configuration request refers to, by matching the
 * head resource against the live manager's head list. Returns NULL if the
 * head is inert (output unplugged) or belongs to another manager. */
static struct qdwin_om_head *
qdwin_om_head_from_resource(struct wl_resource *res)
{
	struct qdwin_om_head *h = wl_resource_get_user_data(res);
	return h;  /* user_data is the qdwin_om_head; may have output==NULL */
}

/* Emit one head's full property burst on creation. The head may be disabled
 * (no driving output): we still advertise it (per spec, heads are advertised
 * even when turned off) with enabled=0 and no mode/position/scale. */
static void
qdwin_om_send_head_details(struct qdwin_om_head *omh)
{
	struct weston_output *out = omh->output;
	struct weston_head *wh = omh->head;
	struct wl_resource *hr = omh->resource;
	struct weston_mode *m;
	const char *name, *make, *model, *serial;
	if (!wh)
		return;
	name = weston_head_get_name(wh);
	if (!name || !*name)
		name = out && out->name ? out->name : "UNKNOWN";
	/* name is required + immutable per head. */
	zwlr_output_head_v1_send_name(hr, name);
	/* description: human-readable, also untrusted. Build from the head's
	 * make/model if present, else fall back to the name. */
	make = wh->make;
	model = wh->model;
	serial = wh->serial_number;
	{
		char desc[256];
		if (make || model)
			snprintf(desc, sizeof desc, "%s %s",
				 make ? make : "", model ? model : "");
		else
			snprintf(desc, sizeof desc, "%s", name);
		zwlr_output_head_v1_send_description(hr, desc);
	}
	if (wh->mm_width > 0 || wh->mm_height > 0)
		zwlr_output_head_v1_send_physical_size(hr, wh->mm_width,
						       wh->mm_height);

	if (!out) {
		/* Truly outputless head (no driving weston_output at all): no
		 * modes, position, scale, and it cannot be (re-)enabled. */
		zwlr_output_head_v1_send_enabled(hr, 0);
		if (wl_resource_get_version(hr) >= 2) {
			if (make)
				zwlr_output_head_v1_send_make(hr, make);
			if (model)
				zwlr_output_head_v1_send_model(hr, model);
			if (serial)
				zwlr_output_head_v1_send_serial_number(hr, serial);
		}
		return;
	}

	/* Modes. Create one zwlr_output_mode_v1 per weston_mode. The mode_list
	 * is populated whether the output is enabled or disabled, so we always
	 * advertise modes for a present output (a disabled output's modes are
	 * what a re-enable picks from). zwlr_output_mode_v1 is capped at version
	 * 3; the manager/head are version 4, so clamp the mode resource version
	 * to the mode interface's max so we never create a v4 mode object. */
	wl_list_for_each(m, &out->mode_list, link) {
		struct qdwin_om_mode *omm = calloc(1, sizeof *omm);
		struct wl_resource *mr;
		uint32_t mode_ver = wl_resource_get_version(hr);
		if (mode_ver > (uint32_t)zwlr_output_mode_v1_interface.version)
			mode_ver = (uint32_t)zwlr_output_mode_v1_interface.version;
		if (!omm)
			continue;
		mr = wl_resource_create(wl_resource_get_client(hr),
			&zwlr_output_mode_v1_interface,
			mode_ver, 0);
		if (!mr) {
			free(omm);
			continue;
		}
		omm->head = omh;
		omm->resource = mr;
		omm->mode = m;
		wl_resource_set_implementation(mr, &qdwin_om_mode_impl, omm,
					       qdwin_om_mode_resource_destroy);
		wl_list_insert(&omh->modes, &omm->link);
		zwlr_output_head_v1_send_mode(hr, mr);
		zwlr_output_mode_v1_send_size(mr, m->width, m->height);
		if (m->refresh > 0)
			zwlr_output_mode_v1_send_refresh(mr, m->refresh);
		if (m->flags & WL_OUTPUT_MODE_PREFERRED)
			zwlr_output_mode_v1_send_preferred(mr);
	}

	/* Dynamic state. */
	zwlr_output_head_v1_send_enabled(hr, out->enabled ? 1 : 0);
	if (out->enabled) {
		/* current_mode references one of the mode resources above. */
		struct qdwin_om_mode *omm;
		wl_list_for_each(omm, &omh->modes, link) {
			if (omm->mode == out->current_mode) {
				zwlr_output_head_v1_send_current_mode(hr,
					omm->resource);
				break;
			}
		}
		zwlr_output_head_v1_send_position(hr,
			(int32_t)out->pos.c.x, (int32_t)out->pos.c.y);
		zwlr_output_head_v1_send_transform(hr, out->transform);
		zwlr_output_head_v1_send_scale(hr,
			wl_fixed_from_int(out->current_scale > 0
					  ? out->current_scale : 1));
	}
	/* make/model/serial (v2+) — untrusted EDID-derived strings, forwarded
	 * verbatim as protocol strings (PlainText on the client). */
	if (wl_resource_get_version(hr) >= 2) {
		if (make)
			zwlr_output_head_v1_send_make(hr, make);
		if (model)
			zwlr_output_head_v1_send_model(hr, model);
		if (serial)
			zwlr_output_head_v1_send_serial_number(hr, serial);
	}
}

/* Tear down a head's mode resources (mark inert) and the head itself. */
static void
qdwin_om_head_destroy_modes(struct qdwin_om_head *omh)
{
	struct qdwin_om_mode *omm, *tmp;
	wl_list_for_each_safe(omm, tmp, &omh->modes, link) {
		zwlr_output_mode_v1_send_finished(omm->resource);
		omm->mode = NULL;        /* inert */
		wl_list_remove(&omm->link);
		/* The resource stays alive until the client destroys it; its
		 * user_data still points at omm. Sever the head link and the
		 * mode pointer; the destructor frees omm. */
		omm->head = NULL;
	}
}

static void
qdwin_om_manager_destroy_heads(struct qdwin_om_manager *mgr)
{
	struct qdwin_om_head *omh, *tmp;
	wl_list_for_each_safe(omh, tmp, &mgr->heads, link) {
		qdwin_om_head_destroy_modes(omh);
		zwlr_output_head_v1_send_finished(omh->resource);
		/* Inert: clear BOTH the head and output back-pointers. The
		 * weston_head behind a finished resource may be freed before the
		 * client destroys the resource, so a late enable_head/disable_head
		 * on this resource must resolve to a NULL head (→ realize rejects
		 * the config) rather than dereference a dangling weston_head. */
		omh->head = NULL;
		omh->output = NULL;
		wl_list_remove(&omh->link);
		omh->mgr = NULL;
	}
}

static void
qdwin_om_manager_create_heads(struct qdwin_om_manager *mgr)
{
	struct qdwin *qdwin = mgr->qdwin;
	struct wl_client *client = wl_resource_get_client(mgr->resource);
	uint32_t ver = wl_resource_get_version(mgr->resource);
	struct weston_head *wh = NULL;

	/* One head per libweston head — enabled OR disabled. Per the protocol
	 * heads are advertised even when turned off (a disabled head reports
	 * enabled=0 with no driving output), so the layout client can re-enable
	 * one. Non-desktop heads (HMDs etc.) are skipped — they are not part of
	 * the desktop layout. */
	while ((wh = weston_compositor_iterate_heads(qdwin->compositor, wh))) {
		struct qdwin_om_head *omh;
		struct wl_resource *hr;
		if (weston_head_is_non_desktop(wh))
			continue;
		omh = calloc(1, sizeof *omh);
		if (!omh)
			continue;
		hr = wl_resource_create(client, &zwlr_output_head_v1_interface,
					ver, 0);
		if (!hr) {
			free(omh);
			continue;
		}
		omh->mgr = mgr;
		omh->head = wh;
		/* The head's output object survives a disable: weston moves a
		 * disabled output to pending_output_list with enabled=false but
		 * keeps head->output set (its head_list + mode_list stay intact),
		 * so weston_output_enable() can later re-enable it. We therefore
		 * keep omh->output whenever the head HAS an output, NULL only when
		 * the head is truly outputless. The advertised `enabled` flag is
		 * driven off out->enabled, not off the pointer — so a disabled
		 * output can still be re-enabled by a (revert) apply. */
		omh->output = weston_head_get_output(wh);
		wl_list_init(&omh->modes);
		omh->resource = hr;
		wl_resource_set_implementation(hr, &qdwin_om_head_impl, omh,
					       qdwin_om_head_resource_destroy);
		wl_list_insert(&mgr->heads, &omh->link);
		zwlr_output_manager_v1_send_head(mgr->resource, hr);
		qdwin_om_send_head_details(omh);
	}
}

static void
qdwin_om_manager_resync(struct qdwin_om_manager *mgr)
{
	if (mgr->stopped)
		return;
	qdwin_om_manager_destroy_heads(mgr);
	qdwin_om_manager_create_heads(mgr);
	zwlr_output_manager_v1_send_done(mgr->resource, mgr->qdwin->om_serial);
}

static void
qdwin_om_resync_all(struct qdwin *qdwin)
{
	struct qdwin_om_manager *mgr;
	/* Each layout change bumps the serial so stale configurations created
	 * against an older serial are rejected (`cancelled`). */
	qdwin->om_serial++;
	wl_list_for_each(mgr, &qdwin->om_managers, link)
		qdwin_om_manager_resync(mgr);
}

/* ---- zwlr_output_mode_v1 ---- */
static void
qdwin_om_mode_release(struct wl_client *client, struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}
static const struct zwlr_output_mode_v1_interface qdwin_om_mode_impl = {
	.release = qdwin_om_mode_release,
};
static void
qdwin_om_mode_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_om_mode *omm = wl_resource_get_user_data(resource);
	if (!omm)
		return;
	if (omm->head)
		wl_list_remove(&omm->link);
	free(omm);
}

/* ---- zwlr_output_head_v1 ---- */
static void
qdwin_om_head_release(struct wl_client *client, struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}
static const struct zwlr_output_head_v1_interface qdwin_om_head_impl = {
	.release = qdwin_om_head_release,
};
static void
qdwin_om_head_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_om_head *omh = wl_resource_get_user_data(resource);
	if (!omh)
		return;
	/* Mode resources owned by this head still reference omh via ->head.
	 * They are destroyed by the client independently; sever the link so
	 * their destructor doesn't touch a freed list head. */
	{
		struct qdwin_om_mode *omm;
		wl_list_for_each(omm, &omh->modes, link)
			omm->head = NULL;
	}
	if (omh->mgr)
		wl_list_remove(&omh->link);
	free(omh);
}

/* ---- zwlr_output_configuration_head_v1 ---- */
static void
qdwin_om_cfg_head_set_mode(struct wl_client *client, struct wl_resource *res,
			   struct wl_resource *mode_res)
{
	struct qdwin_om_cfg_head *ch = wl_resource_get_user_data(res);
	struct qdwin_om_mode *omm;
	(void)client;
	if (!ch)
		return;
	if (ch->set_mode || ch->set_custom_mode) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_HEAD_V1_ERROR_ALREADY_SET,
			"mode already set");
		return;
	}
	omm = mode_res ? wl_resource_get_user_data(mode_res) : NULL;
	/* The mode must belong to THIS head (by head identity, not by shared
	 * output — cloned heads can share one weston_output, so comparing the
	 * output would wrongly accept another head's mode resource). */
	if (!omm || !omm->mode || !omm->head ||
	    omm->head->head != ch->head) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_HEAD_V1_ERROR_INVALID_MODE,
			"mode does not belong to head");
		return;
	}
	ch->set_mode = true;
	ch->mode = omm->mode;
}
static void
qdwin_om_cfg_head_set_custom_mode(struct wl_client *client,
				  struct wl_resource *res,
				  int32_t w, int32_t h, int32_t refresh)
{
	struct qdwin_om_cfg_head *ch = wl_resource_get_user_data(res);
	(void)client;
	if (!ch)
		return;
	if (ch->set_mode || ch->set_custom_mode) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_HEAD_V1_ERROR_ALREADY_SET,
			"mode already set");
		return;
	}
	if (w <= 0 || h <= 0 || refresh < 0) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_HEAD_V1_ERROR_INVALID_CUSTOM_MODE,
			"invalid custom mode");
		return;
	}
	ch->set_custom_mode = true;
	ch->custom_w = w;
	ch->custom_h = h;
	ch->custom_refresh = refresh;
}
static void
qdwin_om_cfg_head_set_position(struct wl_client *client,
			       struct wl_resource *res, int32_t x, int32_t y)
{
	struct qdwin_om_cfg_head *ch = wl_resource_get_user_data(res);
	(void)client;
	if (!ch)
		return;
	if (ch->set_position) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_HEAD_V1_ERROR_ALREADY_SET,
			"position already set");
		return;
	}
	ch->set_position = true;
	ch->pos_x = x;
	ch->pos_y = y;
}
static void
qdwin_om_cfg_head_set_transform(struct wl_client *client,
				struct wl_resource *res, int32_t transform)
{
	struct qdwin_om_cfg_head *ch = wl_resource_get_user_data(res);
	(void)client;
	if (!ch)
		return;
	if (ch->set_transform) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_HEAD_V1_ERROR_ALREADY_SET,
			"transform already set");
		return;
	}
	if (transform < WL_OUTPUT_TRANSFORM_NORMAL ||
	    transform > WL_OUTPUT_TRANSFORM_FLIPPED_270) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_HEAD_V1_ERROR_INVALID_TRANSFORM,
			"transform value outside enum");
		return;
	}
	ch->set_transform = true;
	ch->transform = (uint32_t)transform;
}
static void
qdwin_om_cfg_head_set_scale(struct wl_client *client,
			    struct wl_resource *res, wl_fixed_t scale)
{
	struct qdwin_om_cfg_head *ch = wl_resource_get_user_data(res);
	(void)client;
	if (!ch)
		return;
	if (ch->set_scale) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_HEAD_V1_ERROR_ALREADY_SET,
			"scale already set");
		return;
	}
	if (scale <= 0) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_HEAD_V1_ERROR_INVALID_SCALE,
			"scale negative or zero");
		return;
	}
	ch->set_scale = true;
	ch->scale = scale;
}
static void
qdwin_om_cfg_head_set_adaptive_sync(struct wl_client *client,
				    struct wl_resource *res, uint32_t state)
{
	/* weston-16 has no per-output VRR toggle we can drive live; accept the
	 * request but treat it as a no-op (the layout still applies). We do not
	 * pretend it took effect — the next done re-reports the real state. */
	(void)client; (void)res; (void)state;
}
static const struct zwlr_output_configuration_head_v1_interface
	qdwin_om_cfg_head_impl = {
	.set_mode = qdwin_om_cfg_head_set_mode,
	.set_custom_mode = qdwin_om_cfg_head_set_custom_mode,
	.set_position = qdwin_om_cfg_head_set_position,
	.set_transform = qdwin_om_cfg_head_set_transform,
	.set_scale = qdwin_om_cfg_head_set_scale,
	.set_adaptive_sync = qdwin_om_cfg_head_set_adaptive_sync,
};
static void
qdwin_om_cfg_head_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_om_cfg_head *ch = wl_resource_get_user_data(resource);
	if (!ch)
		return;
	/* Unlinked + freed by the owning configuration's destructor; only free
	 * here if it is still linked (defensive — config destroy clears the
	 * list first). */
	if (ch->link.next)
		wl_list_remove(&ch->link);
	free(ch);
}

/* ---- zwlr_output_configuration_v1 ---- */
/* Dedup key is the weston_head (stable whether the head is enabled or not),
 * NOT the output (which is NULL for all disabled heads and would collide). */
static struct qdwin_om_cfg_head *
qdwin_om_config_find_head(struct qdwin_om_config *cfg,
			  struct weston_head *wh)
{
	struct qdwin_om_cfg_head *ch;
	wl_list_for_each(ch, &cfg->cfg_heads, link)
		if (ch->head == wh)
			return ch;
	return NULL;
}

static void
qdwin_om_config_enable_head(struct wl_client *client, struct wl_resource *res,
			    uint32_t id, struct wl_resource *head_res)
{
	struct qdwin_om_config *cfg = wl_resource_get_user_data(res);
	struct qdwin_om_head *omh = qdwin_om_head_from_resource(head_res);
	struct qdwin_om_cfg_head *ch;
	struct wl_resource *chr;
	struct weston_head *wh = omh ? omh->head : NULL;
	struct weston_output *out = omh ? omh->output : NULL;
	if (!cfg)
		return;
	if (cfg->used) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_V1_ERROR_ALREADY_USED,
			"configuration already applied/tested");
		return;
	}
	if (wh && qdwin_om_config_find_head(cfg, wh)) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_V1_ERROR_ALREADY_CONFIGURED_HEAD,
			"head configured twice");
		return;
	}
	ch = calloc(1, sizeof *ch);
	chr = wl_resource_create(client,
		&zwlr_output_configuration_head_v1_interface,
		wl_resource_get_version(res), id);
	if (!ch || !chr) {
		free(ch);
		if (chr)
			wl_resource_destroy(chr);
		wl_client_post_no_memory(client);
		return;
	}
	ch->config = cfg;
	ch->resource = chr;
	ch->head = wh;        /* NULL only if the head went inert mid-config */
	ch->output = out;     /* NULL if the head is currently disabled */
	ch->enabled = true;
	wl_resource_set_implementation(chr, &qdwin_om_cfg_head_impl, ch,
				       qdwin_om_cfg_head_resource_destroy);
	wl_list_insert(&cfg->cfg_heads, &ch->link);
}

static void
qdwin_om_config_disable_head(struct wl_client *client, struct wl_resource *res,
			     struct wl_resource *head_res)
{
	struct qdwin_om_config *cfg = wl_resource_get_user_data(res);
	struct qdwin_om_head *omh = qdwin_om_head_from_resource(head_res);
	struct qdwin_om_cfg_head *ch;
	struct weston_head *wh = omh ? omh->head : NULL;
	struct weston_output *out = omh ? omh->output : NULL;
	(void)client;
	if (!cfg)
		return;
	if (cfg->used) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_V1_ERROR_ALREADY_USED,
			"configuration already applied/tested");
		return;
	}
	if (wh && qdwin_om_config_find_head(cfg, wh)) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_V1_ERROR_ALREADY_CONFIGURED_HEAD,
			"head configured twice");
		return;
	}
	ch = calloc(1, sizeof *ch);
	if (!ch) {
		wl_client_post_no_memory(client);
		return;
	}
	ch->config = cfg;
	ch->head = wh;
	ch->output = out;
	ch->enabled = false;
	wl_list_insert(&cfg->cfg_heads, &ch->link);
}

/* Count every output (enabled + pending/disabled) so the caller can size
 * the rollback buffer and refuse an apply it could not fully roll back. */
static uint32_t
qdwin_om_output_count(struct qdwin *qdwin)
{
	struct weston_output *out;
	uint32_t n = 0;
	wl_list_for_each(out, &qdwin->compositor->output_list, link)
		n++;
	wl_list_for_each(out, &qdwin->compositor->pending_output_list, link)
		n++;
	return n;
}

/* Capture the current state of every output (enabled AND disabled/pending) so
 * a failed apply can be rolled back atomically — including re-disabling an
 * output the apply just re-enabled. Returns count, fills `saved[]`
 * (caller-sized to qdwin_om_output_count). */
static uint32_t
qdwin_om_capture(struct qdwin *qdwin, struct qdwin_om_saved *saved,
		 uint32_t max)
{
	struct weston_output *out;
	uint32_t n = 0;
	struct wl_list *lists[2] = {
		&qdwin->compositor->output_list,
		&qdwin->compositor->pending_output_list,
	};
	for (int l = 0; l < 2; l++) {
		wl_list_for_each(out, lists[l], link) {
			if (n >= max)
				break;
			saved[n].output = out;
			saved[n].was_enabled = out->enabled;
			saved[n].pos = out->pos;
			saved[n].scale = out->current_scale;
			saved[n].transform = out->transform;
			saved[n].mode = out->current_mode;
			n++;
		}
	}
	return n;
}

static void
qdwin_om_restore(struct qdwin_om_saved *saved, uint32_t n)
{
	for (uint32_t i = 0; i < n; i++) {
		struct weston_output *out = saved[i].output;
		if (!out)
			continue;
		/* If we disabled this output during the failed apply, bring it
		 * back up FIRST (it comes up with whatever mode/scale/transform it
		 * had when disabled, i.e. the captured prior state) so the
		 * mode/scale/transform restores below run on a LIVE output —
		 * switch_mode / set_transform are not valid on a disabled output
		 * (same constraint as the forward apply path). Conversely an
		 * output we ENABLED during the apply but that ended DISABLED needs
		 * no per-output config restore — it is back to its prior off state.
		 * Only touch mode/scale/transform when the output is live. */
		if (saved[i].was_enabled && !out->enabled)
			weston_output_enable(out);
		if (out->enabled) {
			if (saved[i].mode && saved[i].mode != out->current_mode)
				weston_output_mode_switch_to_temporary(out,
					saved[i].mode, saved[i].scale);
			if (saved[i].scale != out->current_scale)
				weston_output_set_scale(out, saved[i].scale);
			if (saved[i].transform != out->transform)
				weston_output_set_transform(out, saved[i].transform);
		}
		weston_output_move(out, saved[i].pos);
		/* An output we ENABLED during the failed apply must be re-disabled
		 * so `failed` leaves the prior layout intact (protocol req). */
		if (!saved[i].was_enabled && out->enabled)
			weston_output_disable(out);
	}
}

/* Validate then apply (or just test) a configuration. Returns true on
 * success. On a failed apply every already-touched output is restored. */
static bool
qdwin_om_config_realize(struct qdwin_om_config *cfg, bool test_only)
{
	struct qdwin *qdwin = cfg->qdwin;
	struct qdwin_om_cfg_head *ch;
#define QDWIN_OM_MAX_OUTPUTS 32
	struct qdwin_om_saved saved[QDWIN_OM_MAX_OUTPUTS];
	uint32_t saved_n;

	/* Refuse (rather than silently half-roll-back) a configuration on a
	 * machine with more outputs than the rollback buffer can hold. 32 is
	 * far beyond any real desktop; this is a hard safety net, not a limit
	 * users hit. Checked up front so both test and apply agree. */
	if (qdwin_om_output_count(qdwin) > QDWIN_OM_MAX_OUTPUTS)
		return false;

	/* Validation pass (always run, for both test and apply):
	 *  - a cfg_head must not reference a head that went inert mid-config
	 *  - ENABLING a head with NO weston_output at all is rejected: qdwin
	 *    cannot conjure a backend output from a bare head, so per the
	 *    no-faking rule we say `failed`. A head whose output merely sits
	 *    DISABLED (still attached, in pending_output_list) CAN be re-enabled
	 *    via weston_output_enable() — this is what confirm-or-revert relies
	 *    on to restore a layout that turned an output off. Disabling an
	 *    already-disabled output is a no-op.
	 *  - a set_mode mode must belong to the head (re-checked here)
	 *  - a custom mode must match some advertised mode (we do not support
	 *    truly arbitrary modelines on weston-16; reject otherwise)
	 *  - at least one head must remain enabled. */
	bool any_enabled = false;
	wl_list_for_each(ch, &cfg->cfg_heads, link) {
		if (!ch->head)
			return false;   /* head went inert mid-config */
		if (ch->enabled && !ch->output)
			return false;   /* no output object at all → can't enable */
		if (ch->enabled)
			any_enabled = true;
		if (ch->enabled && ch->set_custom_mode) {
			struct weston_mode *m;
			bool match = false;
			wl_list_for_each(m, &ch->output->mode_list, link) {
				if (m->width == ch->custom_w &&
				    m->height == ch->custom_h &&
				    (ch->custom_refresh == 0 ||
				     m->refresh == (uint32_t)ch->custom_refresh)) {
					ch->set_mode = true;
					ch->mode = m;
					match = true;
					break;
				}
			}
			if (!match)
				return false;
		}
	}
	if (!any_enabled)
		return false;       /* refuse an all-disabled layout */

	if (test_only)
		return true;

	/* Apply pass — capture for rollback first. */
	saved_n = qdwin_om_capture(qdwin, saved,
				   sizeof saved / sizeof saved[0]);

	wl_list_for_each(ch, &cfg->cfg_heads, link) {
		struct weston_output *out = ch->output;
		if (!ch->enabled) {
			/* Disable requested. weston-16's headless/most backends
			 * keep a single output; disabling the only output is
			 * already excluded by the any_enabled check. A head that is
			 * already disabled has no output → nothing to do. */
			if (out)
				weston_output_disable(out);
			continue;
		}
		/* A disabled output cannot safely take a mode switch or a
		 * scale/transform change: those drive the backend / rebuild the
		 * output geometry, which is invalid while the output's rendering
		 * surface is torn down. So bring a disabled output UP FIRST with
		 * its preserved current mode/scale/transform (weston_output_enable
		 * asserts those are already set, which they are for a previously-
		 * enabled output), then apply mode/scale/transform/position on the
		 * now-LIVE output — the same path an already-enabled output takes. */
		if (!out->enabled && weston_output_enable(out) < 0) {
			qdwin_om_restore(saved, saved_n);
			return false;
		}
		if (ch->set_mode && ch->mode &&
		    ch->mode != out->current_mode) {
			int32_t sc = ch->set_scale
				? wl_fixed_to_int(ch->scale)
				: out->current_scale;
			if (weston_output_mode_switch_to_temporary(out,
					ch->mode, sc) < 0) {
				qdwin_om_restore(saved, saved_n);
				return false;
			}
		}
		if (ch->set_scale) {
			int32_t sc = wl_fixed_to_int(ch->scale);
			if (sc < 1)
				sc = 1;
			weston_output_set_scale(out, sc);
		}
		if (ch->set_transform)
			weston_output_set_transform(out, ch->transform);
		if (ch->set_position) {
			struct weston_coord_global pos = {
				.c = weston_coord(ch->pos_x, ch->pos_y),
			};
			weston_output_move(out, pos);
		}
	}

	/* Re-derive dependent shell state (background/panels/fractional-scale)
	 * the same way the output_changed listener does. */
	qdwin_refresh_background(qdwin);
	if (qdwin->locked)
		qdwin_install_lock_curtain(qdwin);  /* resize to new bbox */
	qdwin_fractional_scale_broadcast(qdwin);
	qdwin_panels_on_output_change(qdwin);
	return true;
}

/* Protocol requires every advertised head to be configured (enabled or
 * disabled) exactly once. Post `unconfigured_head` if a desktop head is
 * missing from the configuration. Returns true if the config is complete. */
static bool
qdwin_om_config_all_heads_set(struct qdwin_om_config *cfg)
{
	struct qdwin *qdwin = cfg->qdwin;
	struct weston_head *wh = NULL;
	while ((wh = weston_compositor_iterate_heads(qdwin->compositor, wh))) {
		if (weston_head_is_non_desktop(wh))
			continue;
		if (!qdwin_om_config_find_head(cfg, wh)) {
			wl_resource_post_error(cfg->resource,
				ZWLR_OUTPUT_CONFIGURATION_V1_ERROR_UNCONFIGURED_HEAD,
				"head not configured");
			return false;
		}
	}
	return true;
}

static void
qdwin_om_config_apply(struct wl_client *client, struct wl_resource *res)
{
	struct qdwin_om_config *cfg = wl_resource_get_user_data(res);
	if (!cfg)
		return;
	/* Authorization gate (security): only the trusted shell may mutate the
	 * display layout. Enumeration is open, but an unauthorized client that
	 * reaches apply is rejected with a protocol error (fatal to that
	 * client), mirroring the layer-shell bind gate. Checked before `used`
	 * so an unauthorized client can never even consume the one-shot. */
	if (!cfg->may_mutate) {
		weston_log("qdwin: zwlr_output_configuration apply REJECTED — "
			   "client not authorized to mutate display layout\n");
		wl_client_post_implementation_error(client,
			"zwlr_output_configuration_v1: not authorized to apply "
			"a display configuration");
		return;
	}
	if (cfg->used) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_V1_ERROR_ALREADY_USED,
			"configuration already applied/tested");
		return;
	}
	cfg->used = true;
	/* Serial check FIRST: if the serial is stale (an output hotplugged /
	 * changed since the client's last done) the client's view of the head
	 * set is outdated, so cancel gracefully rather than posting a fatal
	 * `unconfigured_head` protocol error for a head the client never saw. */
	if (cfg->serial != cfg->qdwin->om_serial) {
		zwlr_output_configuration_v1_send_cancelled(res);
		return;
	}
	if (!qdwin_om_config_all_heads_set(cfg))
		return;  /* protocol error already posted */
	if (qdwin_om_config_realize(cfg, false)) {
		zwlr_output_configuration_v1_send_succeeded(res);
		/* Broadcast the new current configuration to all managers. */
		qdwin_om_resync_all(cfg->qdwin);
	} else {
		zwlr_output_configuration_v1_send_failed(res);
	}
}

static void
qdwin_om_config_test(struct wl_client *client, struct wl_resource *res)
{
	struct qdwin_om_config *cfg = wl_resource_get_user_data(res);
	if (!cfg)
		return;
	/* Same authorization gate as apply. `test` itself is side-effect-free
	 * (qdwin_om_config_realize returns before touching any output when
	 * test_only), but an unauthorized client has no business driving the
	 * configuration machinery at all — reject uniformly, fail-closed. */
	if (!cfg->may_mutate) {
		weston_log("qdwin: zwlr_output_configuration test REJECTED — "
			   "client not authorized to mutate display layout\n");
		wl_client_post_implementation_error(client,
			"zwlr_output_configuration_v1: not authorized to test "
			"a display configuration");
		return;
	}
	if (cfg->used) {
		wl_resource_post_error(res,
			ZWLR_OUTPUT_CONFIGURATION_V1_ERROR_ALREADY_USED,
			"configuration already applied/tested");
		return;
	}
	cfg->used = true;
	/* Serial check before completeness check (see apply): a stale config is
	 * cancelled, not failed with a protocol error. */
	if (cfg->serial != cfg->qdwin->om_serial) {
		zwlr_output_configuration_v1_send_cancelled(res);
		return;
	}
	if (!qdwin_om_config_all_heads_set(cfg))
		return;  /* protocol error already posted */
	if (qdwin_om_config_realize(cfg, true))
		zwlr_output_configuration_v1_send_succeeded(res);
	else
		zwlr_output_configuration_v1_send_failed(res);
}

static void
qdwin_om_config_destroy(struct wl_client *client, struct wl_resource *res)
{
	(void)client;
	wl_resource_destroy(res);
}

static const struct zwlr_output_configuration_v1_interface
	qdwin_om_config_impl = {
	.enable_head = qdwin_om_config_enable_head,
	.disable_head = qdwin_om_config_disable_head,
	.apply = qdwin_om_config_apply,
	.test = qdwin_om_config_test,
	.destroy = qdwin_om_config_destroy,
};

static void
qdwin_om_config_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_om_config *cfg = wl_resource_get_user_data(resource);
	struct qdwin_om_cfg_head *ch, *tmp;
	if (!cfg)
		return;
	/* Per spec, destroying the configuration also destroys every
	 * zwlr_output_configuration_head_v1 created via it (the child interface
	 * has NO destroy request, so the compositor must do it). Destroying the
	 * enable_head child resources runs their resource destructor which frees
	 * the struct; disable_head children have no resource (calloc'd directly),
	 * so free them here. We unlink first so the child destructor (which also
	 * unlinks via the ch->link.next guard) is a no-op on the list. */
	wl_list_for_each_safe(ch, tmp, &cfg->cfg_heads, link) {
		wl_list_remove(&ch->link);
		ch->link.next = NULL;
		ch->config = NULL;
		if (ch->resource) {
			/* enable_head child — destroy the resource; its destructor
			 * frees ch (and re-checks ch->link.next, now NULL → no-op). */
			wl_resource_destroy(ch->resource);
		} else {
			/* disable_head child — no resource backing it; free directly. */
			free(ch);
		}
	}
	free(cfg);
}

/* ---- zwlr_output_manager_v1 ---- */
static void
qdwin_om_manager_create_configuration(struct wl_client *client,
				      struct wl_resource *res,
				      uint32_t id, uint32_t serial)
{
	struct qdwin_om_manager *mgr = wl_resource_get_user_data(res);
	struct qdwin_om_config *cfg;
	struct wl_resource *cr;
	if (!mgr)
		return;
	cfg = calloc(1, sizeof *cfg);
	cr = wl_resource_create(client,
		&zwlr_output_configuration_v1_interface,
		wl_resource_get_version(res), id);
	if (!cfg || !cr) {
		free(cfg);
		if (cr)
			wl_resource_destroy(cr);
		wl_client_post_no_memory(client);
		return;
	}
	cfg->qdwin = mgr->qdwin;
	cfg->resource = cr;
	cfg->serial = serial;
	cfg->may_mutate = mgr->may_mutate;
	wl_list_init(&cfg->cfg_heads);
	wl_resource_set_implementation(cr, &qdwin_om_config_impl, cfg,
				       qdwin_om_config_resource_destroy);
}

static void
qdwin_om_manager_stop(struct wl_client *client, struct wl_resource *res)
{
	struct qdwin_om_manager *mgr = wl_resource_get_user_data(res);
	(void)client;
	if (!mgr)
		return;
	mgr->stopped = true;
	zwlr_output_manager_v1_send_finished(mgr->resource);
	/* finished is a destructor event — tear down now. */
	wl_resource_destroy(res);
}

static const struct zwlr_output_manager_v1_interface qdwin_om_manager_impl = {
	.create_configuration = qdwin_om_manager_create_configuration,
	.stop = qdwin_om_manager_stop,
};

static void
qdwin_om_manager_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_om_manager *mgr = wl_resource_get_user_data(resource);
	struct qdwin_om_head *omh, *tmp;
	if (!mgr)
		return;
	wl_list_remove(&mgr->link);
	/* Null head/mode back-pointers; the client destroys those resources. */
	wl_list_for_each_safe(omh, tmp, &mgr->heads, link) {
		struct qdwin_om_mode *omm;
		wl_list_for_each(omm, &omh->modes, link)
			omm->head = NULL;
		wl_list_remove(&omh->link);
		omh->mgr = NULL;
		omh->head = NULL;
		omh->output = NULL;
	}
	free(mgr);
}

static void
bind_output_manager(struct wl_client *client, void *data,
		    uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct qdwin_om_manager *mgr = calloc(1, sizeof *mgr);
	if (!mgr) {
		wl_client_post_no_memory(client);
		return;
	}
	mgr->qdwin = qdwin;
	wl_list_init(&mgr->heads);
	/* Enumeration is open to every client; the apply/test mutation path is
	 * gated to the trusted shell. Snapshot the decision now against the
	 * binding client's credentials (consistent with qdwin's connect-time
	 * peer-identity posture). */
	mgr->may_mutate = qdwin_om_client_may_mutate(qdwin, client);
	mgr->resource = wl_resource_create(client,
		&zwlr_output_manager_v1_interface, version, id);
	if (!mgr->resource) {
		free(mgr);
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(mgr->resource, &qdwin_om_manager_impl,
				       mgr, qdwin_om_manager_resource_destroy);
	wl_list_insert(&qdwin->om_managers, &mgr->link);
	qdwin_om_manager_create_heads(mgr);
	zwlr_output_manager_v1_send_done(mgr->resource, qdwin->om_serial);
	weston_log("qdwin: zwlr_output_manager bound (serial=%u)\n",
		   qdwin->om_serial);
}

static void
qdwin_on_keyboard_focus_changed(struct wl_listener *listener, void *data)
{
	struct qdwin_seat_tracker *tr =
		wl_container_of(listener, tr, kbd_focus_listener);
	(void)data;
	/* P1: keep text-input-v3 enter/leave in lockstep with keyboard
	 * focus. Runs unconditionally (not behind the toplevel-handle dedup
	 * in qdwin_seat_emit_focus_now) because text-input cares about the
	 * focused wl_surface, which can change between non-toplevel surfaces
	 * the handle dedup would collapse. */
	qdwin_text_input_update_focus(tr->qdwin, tr->seat);
	qdwin_seat_emit_focus_now(tr);
}

static void
qdwin_replay_seat_focus_for_shell(struct qdwin *qdwin)
{
	/* Emit one seat_focus_changed per tracked seat, unconditionally,
	 * so the shell starts with a coherent focus map. We bypass
	 * `qdwin_seat_emit_focus_now`'s dedup guard since a freshly bound
	 * shell has no prior focus state and needs the full snapshot
	 * (including the "no toplevel focused" / UINT32_MAX case). */
	struct qdwin_seat_tracker *tr;
	wl_list_for_each(tr, &qdwin->seat_trackers, link) {
		struct weston_keyboard *kbd =
			weston_seat_get_keyboard(tr->seat);
		struct weston_surface *focus = kbd ? kbd->focus : NULL;
		struct qdwin_toplevel *tl =
			qdwin_toplevel_by_surface(tr->qdwin, focus);
		uint32_t handle = tl ? tl->handle : UINT32_MAX;
		tr->last_focused_handle = handle;
		qdwin_emit_seat_focus_changed(qdwin, tr->seat, handle);
	}
}

/* Lookup a seat tracker by weston_seat. Currently unused — kept as a
 * forward-compatible helper for future code paths that need the seat
 * tracker without walking from a wl_listener via wl_container_of. */
static struct qdwin_seat_tracker *
qdwin_seat_tracker_for_seat(struct qdwin *qdwin, struct weston_seat *seat)
{
	struct qdwin_seat_tracker *tr;
	wl_list_for_each(tr, &qdwin->seat_trackers, link)
		if (tr->seat == seat)
			return tr;
	return NULL;
}

/* spec/10 v16: accessor helpers that let request handlers above the
 * struct definition (e.g. qdwin_handle_set_keyboard_focus_v2) read
 * and write last_target_silo without seeing the struct layout. */
static const char *
qdwin_seat_tracker_silo(struct qdwin_seat_tracker *tr)
{
	if (!tr || !tr->last_target_silo)
		return "";
	return tr->last_target_silo;
}

static void
qdwin_seat_tracker_set_silo(struct qdwin_seat_tracker *tr, const char *silo)
{
	if (!tr)
		return;
	free(tr->last_target_silo);
	tr->last_target_silo = (silo && *silo) ? qdwin_xstrdup_or_null(silo) : NULL;
}

static void
qdwin_install_focus_listener_if_needed(struct qdwin_seat_tracker *tr)
{
	if (!tr || tr->kbd_focus_listener_installed)
		return;
	struct weston_keyboard *kbd = weston_seat_get_keyboard(tr->seat);
	if (!kbd)
		return;
	tr->kbd_focus_listener.notify = qdwin_on_keyboard_focus_changed;
	wl_signal_add(&kbd->focus_signal, &tr->kbd_focus_listener);
	tr->kbd_focus_listener_installed = 1;
	tr->kbd_focus_listener_kbd = kbd;
	/* Emit current state so the shell starts coherent — this is also
	 * what bind_as_shell relies on for late-binding shells. */
	tr->last_focused_handle = UINT32_MAX;  /* force a fresh emit */
	qdwin_seat_emit_focus_now(tr);
}

/* Reconcile kbd_focus_listener against the seat's *live* weston_keyboard.
 *
 * Background: the RDP-headless backend brings its seat up lazily inside
 * rdp_peer_activate() — weston_seat_init() fires seat_created_signal (so
 * qdwin_track_seat runs and tries to install the focus listener) BEFORE
 * weston_seat_init_keyboard() has created seat->keyboard_state. The
 * keyboard then appears via updated_caps_signal, which our
 * qdwin_on_seat_updated_caps handler observes to (re)install the listener.
 *
 * That covers the normal ordering, but it leaves the listener's correctness
 * implicitly dependent on updated_caps_signal firing for every keyboard
 * swap. This helper makes the binding self-healing and ordering-independent:
 * it compares the keyboard the listener is bound to against the keyboard the
 * seat currently exposes and, if they differ (a swap, or a first-time
 * appearance the caps path missed), moves the listener onto the live object.
 *
 * Called from the autofocus path right before weston_keyboard_set_focus(),
 * so that the subsequent focus_signal emission reaches a listener that is
 * guaranteed to be on the current keyboard — letting focus propagate
 * organically on RDP-headless rather than relying solely on the
 * immediate-emit fallback. The immediate-emit remains as a dedupe-safe
 * backstop (qdwin_seat_emit_focus_now short-circuits on last_focused_handle).
 *
 * Returns 1 if a (re)bind happened, 0 otherwise. Idempotent: a no-op when
 * the listener is already on the live keyboard. */
static int
qdwin_seat_tracker_rebind_focus_listener(struct qdwin_seat_tracker *tr)
{
	if (!tr || !tr->seat)
		return 0;
	struct weston_keyboard *kbd = weston_seat_get_keyboard(tr->seat);
	if (tr->kbd_focus_listener_installed &&
	    tr->kbd_focus_listener_kbd == kbd)
		return 0;  /* already bound to the live keyboard (or both NULL) */
	/* Drop any stale binding. Removing a stale focus_signal link before
	 * re-arming is mandatory: a listener must never sit on two signal
	 * lists at once or wl_signal_emit would walk a corrupted list — the
	 * exact wedge (100% CPU) seen on weston-rdp keyboard re-init. */
	if (tr->kbd_focus_listener_installed) {
		wl_list_remove(&tr->kbd_focus_listener.link);
		tr->kbd_focus_listener_installed = 0;
		tr->kbd_focus_listener_kbd = NULL;
	}
	if (!kbd)
		return 0;  /* no live keyboard yet; caps path will retry */
	qdwin_install_focus_listener_if_needed(tr);
	return tr->kbd_focus_listener_kbd == kbd;
}

/* B6: install a pointer-focus listener so we can restore the default
 * cursor sprite whenever pointer focus leaves a client surface. Plus
 * install the default sprite immediately on the pointer if it has no
 * sprite yet (e.g. cursor-sprites helper already registered the
 * default by the time the seat tracker exists). */
static void
qdwin_install_pointer_focus_listener_if_needed(struct qdwin_seat_tracker *tr)
{
	if (!tr || tr->pointer_focus_listener_installed)
		return;
	struct weston_pointer *p = weston_seat_get_pointer(tr->seat);
	if (!p)
		return;
	tr->pointer_focus_listener.notify = qdwin_default_cursor_on_focus_changed;
	wl_signal_add(&p->focus_signal, &tr->pointer_focus_listener);
	tr->pointer_focus_listener_installed = 1;
	if (!p->sprite)
		qdwin_install_default_cursor_on_pointer(tr->qdwin, p);
}

static void
qdwin_send_seat_created(struct qdwin *qdwin, struct weston_seat *seat)
{
	if (!qdwin_shell_can_receive_v2(qdwin))
		return;
	qdwin_shell_v1_send_seat_created(qdwin->shell_resource,
					 seat->seat_name ? seat->seat_name : "");
}

static void
qdwin_send_seat_removed(struct qdwin *qdwin, const char *seat_name)
{
	if (!qdwin_shell_can_receive_v2(qdwin))
		return;
	qdwin_shell_v1_send_seat_removed(qdwin->shell_resource,
					 seat_name ? seat_name : "");
}

static void
qdwin_seat_tracker_destroy(struct qdwin_seat_tracker *tr, int emit_removed)
{
	if (!tr)
		return;
	if (emit_removed)
		qdwin_send_seat_removed(tr->qdwin, tr->seat_name);
	wl_list_remove(&tr->destroy_listener.link);
	wl_list_remove(&tr->updated_caps_listener.link);
	wl_list_remove(&tr->selection_listener.link);
	if (tr->kbd_focus_listener_installed) {
		wl_list_remove(&tr->kbd_focus_listener.link);
		tr->kbd_focus_listener_installed = 0;
		tr->kbd_focus_listener_kbd = NULL;
	}
	if (tr->pointer_focus_listener_installed) {
		wl_list_remove(&tr->pointer_focus_listener.link);
		tr->pointer_focus_listener_installed = 0;
	}
	if (tr->focus_recover_idle) {
		wl_event_source_remove(tr->focus_recover_idle);
		tr->focus_recover_idle = NULL;
	}
	wl_list_remove(&tr->link);
	tr->seat = NULL;
	free(tr->last_target_silo);
	free(tr->seat_name);
	free(tr);
}

static void
qdwin_send_output_created_evt(struct qdwin *qdwin, struct weston_output *output)
{
	if (!qdwin_shell_can_receive_v2(qdwin))
		return;
	qdwin_shell_v1_send_output_created(qdwin->shell_resource,
					   output->name ? output->name : "");
}

static void
qdwin_send_output_removed(struct qdwin *qdwin, struct weston_output *output)
{
	if (!qdwin_shell_can_receive_v2(qdwin))
		return;
	qdwin_shell_v1_send_output_removed(qdwin->shell_resource,
					   output->name ? output->name : "");
}

static void
qdwin_on_seat_destroyed(struct wl_listener *listener, void *data)
{
	struct qdwin_seat_tracker *tr =
		wl_container_of(listener, tr, destroy_listener);
	(void)data;
	qdwin_seat_tracker_destroy(tr, 1);
}

/* §6.8 S3c: caps just changed on this seat — a keyboard may have just
 * appeared (RDP backend's keyboard arrives after seat creation, USB
 * keyboards hot-plug). Reinstall the default keyboard grab if so. */
static void
qdwin_on_seat_updated_caps(struct wl_listener *listener, void *data)
{
	struct qdwin_seat_tracker *tr =
		wl_container_of(listener, tr, updated_caps_listener);
	struct weston_seat *seat = data;
	if (seat == tr->seat) {
		qdwin_install_default_keyboard_grab(seat);
		/* spec/10 v14: keyboard may have just appeared or been swapped
		 * (RDP backend). Reconcile the focus listener against the live
		 * keyboard; the helper removes any stale binding before
		 * re-arming so the listener can never be on two focus_signal
		 * lists at once (which would have wl_signal_emit iterating a
		 * corrupted list — the wedged-at-100%-CPU weston-rdp re-init
		 * case). */
		qdwin_seat_tracker_rebind_focus_listener(tr);
		/* B6: same drill for the pointer focus signal. */
		if (tr->pointer_focus_listener_installed) {
			wl_list_remove(&tr->pointer_focus_listener.link);
			tr->pointer_focus_listener_installed = 0;
		}
		qdwin_install_pointer_focus_listener_if_needed(tr);
	}
}

static struct qdwin_seat_tracker *
qdwin_track_seat(struct qdwin *qdwin, struct weston_seat *seat)
{
	struct qdwin_seat_tracker *tr = calloc(1, sizeof *tr);
	if (!tr)
		return NULL;
	tr->qdwin = qdwin;
	tr->seat = seat;
	tr->seat_name = strdup(seat->seat_name ? seat->seat_name : "");
	if (!tr->seat_name) {
		free(tr);
		return NULL;
	}
	tr->destroy_listener.notify = qdwin_on_seat_destroyed;
	wl_signal_add(&seat->destroy_signal, &tr->destroy_listener);
	tr->updated_caps_listener.notify = qdwin_on_seat_updated_caps;
	wl_signal_add(&seat->updated_caps_signal, &tr->updated_caps_listener);
	tr->selection_listener.notify = qdwin_on_seat_selection_changed;
	wl_signal_add(&seat->selection_signal, &tr->selection_listener);
	tr->last_focused_handle = UINT32_MAX;
	wl_list_insert(&qdwin->seat_trackers, &tr->link);
	/* §6.8 S3c: install on whatever keyboard is already present. */
	qdwin_install_default_keyboard_grab(seat);
	/* spec/10 v14: install kbd focus listener now if a keyboard exists.
	 * For RDP backends without a keyboard at seat-creation time, the
	 * updated_caps handler re-tries when the keyboard appears. */
	qdwin_install_focus_listener_if_needed(tr);
	/* B6: pointer focus listener for default-cursor restoration. */
	qdwin_install_pointer_focus_listener_if_needed(tr);
	qdwin_install_data_source_wrap(qdwin, seat, seat->selection_data_source);
	return tr;
}

static void
qdwin_on_seat_created(struct wl_listener *listener, void *data)
{
	struct qdwin *qdwin =
		wl_container_of(listener, qdwin, seat_created_listener);
	struct weston_seat *seat = data;
	qdwin_track_seat(qdwin, seat);
	qdwin_send_seat_created(qdwin, seat);
}

static void
qdwin_on_output_destroyed(struct wl_listener *listener, void *data)
{
	struct qdwin *qdwin =
		wl_container_of(listener, qdwin, output_destroyed_listener);
	struct weston_output *output = data;
	/* Rescue every surface and close session-scoped input/data state while
	 * `output` geometry is still readable. Publishing removal first lets the
	 * shell react while compositor objects still point at the dying output. */
	qdwin_output_boundary_transition(qdwin, output);
	qdwin_send_output_removed(qdwin, output);
	/* ext-workspace: drop the group's association with this output. */
	if (output)
		qdwin_ext_ws_broadcast_output(qdwin, output, false);
	qdwin_om_resync_all(qdwin);
}

static void
qdwin_on_output_changed(struct wl_listener *listener, void *data)
{
	struct qdwin *qdwin =
		wl_container_of(listener, qdwin, output_created_listener);
	struct weston_output *output = data;
	qdwin_refresh_background(qdwin);
	qdwin_fractional_scale_broadcast(qdwin);
	qdwin_panels_on_output_change(qdwin);
	if (output)
		qdwin_send_output_created_evt(qdwin, output);
	/* v33: (re-)arm retained-frame capture on the designated output. */
	if (output)
		qdwin_capture_retention_maybe_arm(qdwin, output);
	/* ext-workspace: associate the group with the newly created/enabled
	 * output. Clients that bind this output's wl_output global only after
	 * this fires are handled at their bind time (a client binding the
	 * manager re-runs output_enter for all its outputs); for clients that
	 * bound the wl_output late we rely on output-management's resync and the
	 * fact that the group already spans the desktop. */
	if (output)
		qdwin_ext_ws_broadcast_output(qdwin, output, true);
	/* v26: if the shell has forced the display off, a newly created /
	 * re-enabled output must come up powered off too — otherwise a hotplug
	 * or output-management re-enable during the display-off idle leaves that
	 * output lit until the next set_display_power. */
	if (qdwin->display_forced_off && output)
		weston_output_power_off(output);
	qdwin_om_resync_all(qdwin);
}

static void
qdwin_on_output_resized(struct wl_listener *listener, void *data)
{
	struct qdwin *qdwin =
		wl_container_of(listener, qdwin, output_resized_listener);
	(void)data;
	qdwin_refresh_background(qdwin);
	qdwin_fractional_scale_broadcast(qdwin);
	qdwin_panels_on_output_change(qdwin);
	qdwin_om_resync_all(qdwin);
}

/* ------------------------------------------------------------------
 * Lifecycle.
 * ------------------------------------------------------------------ */

/* iso2/11 E2 (codex r1 #4): destroying the idle-inhibit global does NOT
 * destroy the inhibitor resources bound through it. Any survivor would
 * later run its resource-destroy handler against a freed struct qdwin
 * and unlink through a freed list head, so drain them explicitly. */
static void qdwin_idle_inhibitors_destroy_all(struct qdwin *qdwin);

static void
qdwin_destroy(struct wl_listener *listener, void *data)
{
	struct qdwin *qdwin = wl_container_of(listener, qdwin,
					      destroy_listener);
	(void)data;
	qdwin_idle_inhibitors_destroy_all(qdwin);
	if (qdwin->background) {
		weston_shell_utils_curtain_destroy(qdwin->background);
		qdwin->background = NULL;
	}
	if (qdwin->lock_curtain) {
		weston_shell_utils_curtain_destroy(qdwin->lock_curtain);
		qdwin->lock_curtain = NULL;
	}
	wl_list_remove(&qdwin->output_created_listener.link);
	wl_list_remove(&qdwin->output_resized_listener.link);
	wl_list_remove(&qdwin->output_destroyed_listener.link);
	wl_list_remove(&qdwin->seat_created_listener.link);
	if (qdwin->capture_auth_registered) {
		wl_list_remove(&qdwin->capture_auth_listener.link);
		qdwin->capture_auth_registered = false;
		wl_list_remove(&qdwin->capture_retain_output_gone_listener.link);
		wl_list_remove(&qdwin->capture_task_filed_listener.link);
		qdwin->capture_retain_output = NULL;
		if (qdwin->capture_stale_timer) {
			wl_event_source_remove(qdwin->capture_stale_timer);
			qdwin->capture_stale_timer = NULL;
		}
	}
	{
		struct qdwin_seat_tracker *tr, *tmp;
		wl_list_for_each_safe(tr, tmp, &qdwin->seat_trackers, link)
			qdwin_seat_tracker_destroy(tr, 0);
	}
	qdwin_activation_pending_free_all(qdwin);
	qdwin_data_offer_pending_free_all(qdwin);
	qdwin_data_source_wraps_free_all(qdwin);
	if (qdwin->desktop)
		weston_desktop_destroy(qdwin->desktop);
	if (qdwin->shell_global)
		wl_global_destroy(qdwin->shell_global);
	if (qdwin->locker_global)
		wl_global_destroy(qdwin->locker_global);
	if (qdwin->stream_input_global)
		wl_global_destroy(qdwin->stream_input_global);
	if (qdwin->xdg_activation_global)
		wl_global_destroy(qdwin->xdg_activation_global);
	if (qdwin->idle_notifier_global)
		wl_global_destroy(qdwin->idle_notifier_global);
	if (qdwin->idle_inhibit_manager_global)
		wl_global_destroy(qdwin->idle_inhibit_manager_global);
	if (qdwin->cursor_shape_manager_global)
		wl_global_destroy(qdwin->cursor_shape_manager_global);
	if (qdwin->fractional_scale_manager_global)
		wl_global_destroy(qdwin->fractional_scale_manager_global);
	if (qdwin->primary_selection_manager_global)
		wl_global_destroy(qdwin->primary_selection_manager_global);
	/* Drain input methods before text_inputs: an IME may end an active
	 * keyboard grab and references text_input objects via active_ti (cleared
	 * without dereference in qdwin_im_detach). */
	if (qdwin->input_method_manager_global)
		wl_global_destroy(qdwin->input_method_manager_global);
	qdwin_input_methods_destroy_all(qdwin);
	qdwin_input_method_managers_destroy_all(qdwin);
	/* P1 companion: virtual keyboards inject into the seat but hold no
	 * references to text_inputs/input_methods, so their drain order is
	 * independent; drain here alongside the IME family. */
	if (qdwin->virtual_keyboard_manager_global)
		wl_global_destroy(qdwin->virtual_keyboard_manager_global);
	qdwin_virtual_keyboards_destroy_all(qdwin);
	qdwin_virtual_keyboard_managers_destroy_all(qdwin);
	if (qdwin->text_input_manager_global)
		wl_global_destroy(qdwin->text_input_manager_global);
	qdwin_text_inputs_destroy_all(qdwin);
	qdwin_text_input_managers_destroy_all(qdwin);
	if (qdwin->security_context_manager_global)
		wl_global_destroy(qdwin->security_context_manager_global);
	qdwin_secctx_destroy_all(qdwin);
	if (qdwin->nested_manager_global)
		wl_global_destroy(qdwin->nested_manager_global);
	if (qdwin->layer_shell_global)
		wl_global_destroy(qdwin->layer_shell_global);
	if (qdwin->xdg_decoration_manager_global)
		wl_global_destroy(qdwin->xdg_decoration_manager_global);
	if (qdwin->ext_ws_global)
		wl_global_destroy(qdwin->ext_ws_global);
	if (qdwin->output_mgmt_global)
		wl_global_destroy(qdwin->output_mgmt_global);
	if (qdwin->ffm_timer) {
		wl_event_source_remove(qdwin->ffm_timer);
		qdwin->ffm_timer = NULL;
	}
	if (qdwin->nested_outer_event_source) {
		wl_event_source_remove(qdwin->nested_outer_event_source);
		qdwin->nested_outer_event_source = NULL;
	}
	if (qdwin->nested_client) {
		qdwin_nested_client_destroy(qdwin->nested_client);
		qdwin->nested_client = NULL;
	}
	if (qdwin->idle_signal_listener.notify)
		wl_list_remove(&qdwin->idle_signal_listener.link);
	if (qdwin->wake_signal_listener.notify)
		wl_list_remove(&qdwin->wake_signal_listener.link);
	qdwin_cursor_theme_destroy(qdwin);
	weston_layer_fini(&qdwin->background_layer);
	weston_layer_fini(&qdwin->held_layer);
	weston_layer_fini(&qdwin->normal_layer);
	weston_layer_fini(&qdwin->minimized_layer);
	weston_layer_fini(&qdwin->workspace_hidden_layer);
	weston_layer_fini(&qdwin->panel_layer);
	weston_layer_fini(&qdwin->notification_layer);
	weston_layer_fini(&qdwin->launcher_layer);
	weston_layer_fini(&qdwin->lock_layer);
	weston_layer_fini(&qdwin->popup_layer);
	for (int i = 0; i < 4; i++)
		weston_layer_fini(&qdwin->layer_shell_layer[i]);
	wl_list_remove(&qdwin->destroy_listener.link);
	/* v27: release any per-index custom workspace names (strndup'd). */
	for (uint32_t wi = 0; wi < QDWIN_MAX_WORKSPACES; wi++)
		free(qdwin->workspace_names[wi]);
	free(qdwin->allowed_locker_entrypoint);
	free(qdwin->allowed_locker_exe);
	free(qdwin->allowed_locker_label);
	free(qdwin->allowed_ime_exe);
	free(qdwin->allowed_ime_label);
	free(qdwin->allowed_layershell_exe);
	free(qdwin->allowed_layershell_label);
	free(qdwin);
}

/* ------------------------------------------------------------------
 * §6.7 idle-inhibit-unstable-v1 + ext-idle-notify-v1.
 *
 * Two protocols that share the idle-state concept, so they live in
 * one block.
 *
 * idle-inhibit: each zwp_idle_inhibitor_v1 bumps ec->idle_inhibit
 * while its backing wl_surface is alive *and mapped*; weston's own
 * idle timer defers while the counter is non-zero. The spec allows
 * ignoring the inhibitor while the surface is occluded / unmapped.
 * iso2/11 E2: we take the unmapped half of that permission and
 * decline the occluded half. Unmapped matters because a silo could
 * otherwise create a wl_surface, never attach a buffer, take an
 * inhibitor, and suppress idle-lock + DPMS for the rest of the
 * session with nothing on screen. Occluded/minimised keeps
 * inhibiting on purpose: "video in a silo keeps the session awake"
 * is a product feature and a covered media window is exactly the
 * case users expect to keep playing. The policy predicate itself
 * lives in qdwin-logic.c (qdwin_idle_inhibit_should_hold).
 *
 * Re-evaluation (qdwin_idle_inhibitor_sync) is driven two ways, and
 * both are needed:
 *
 *   - the surface's own map_signal / unmap_signal, which makes the
 *     common case (attach a NULL buffer, destroy the window) release
 *     the hold immediately; and
 *   - a periodic backstop timer, armed only while an inhibitor
 *     exists.
 *
 * The backstop is not belt-and-braces. weston_surface_is_mapped() is
 * a *recursive* query up the subsurface tree
 * (libweston/compositor.c), so the effective answer for surface S can
 * change with no signal on S at all: unmapping or destroying S's
 * parent flips S's effective mapped state while emitting only the
 * parent's signal (codex r1 #1). Subscribing to a fixed ancestor set
 * does not fix it either, since a plain surface can acquire the
 * subsurface role after the inhibitor is created. Polling the (tiny)
 * inhibitor list is the cheap, complete answer; a second of latency
 * is irrelevant against a multi-minute idle timeout.
 *
 * The hold additionally requires a currently attached buffer and at
 * least one live weston_view. Both are stale-state guards that the
 * mapped bit alone does not give us:
 *
 *   - no buffer: a bufferless child of an unmapped parent keeps
 *     surface->is_mapped true and reads as mapped again the moment
 *     the parent remaps (codex r1 #2).
 *   - no views: weston_subsurface_destroy() destroys every view of
 *     the surface but clears neither its mapped bit nor its buffer,
 *     and clearing ->committed makes the recursive mapped query stop
 *     walking to the (unmapped) parent — so destroying just the
 *     wl_subsurface, keeping the wl_surface, leaves an orphan that
 *     reads as mapped forever (codex r2 #2).
 *
 * Both are also independently defensible: a surface with no buffer or
 * no view is showing nothing. Minimisation is unaffected — qdwin
 * minimises by moving the view to another layer, so the view lives.
 *
 * The backstop is armed on the empty -> non-empty transition of the
 * inhibitor list and re-armed only by its own callback. Arming per
 * create would let a client churning throwaway inhibitors push the
 * deadline out indefinitely and strand another client's stale hold.
 * If the timer cannot be created, create_inhibitor fails with
 * no_memory rather than accepting a hold it could never re-evaluate;
 * if its callback's self-rearm ever fails, idle-inhibit is disabled
 * for the rest of the session: every hold is released, surviving inhibitors go
 * inert (they cannot re-acquire on a later map), and further
 * create_inhibitor calls are refused. That latch is deliberate and
 * terminal — merely releasing the holds would be momentary, since a
 * re-map would take one straight back and nothing would ever
 * re-evaluate it. Fail closed: an un-re-evaluatable hold is a
 * permanent client-controlled suppression of idle-lock and DPMS.
 *
 * RESIDUAL RISK, accepted deliberately: this is a product policy, not
 * a visibility security boundary. The predicate proves a mapped
 * surface with a buffer and a live view; it does NOT prove that any
 * view is mapped, is on a displayed layer, intersects an output, or
 * draws a non-transparent pixel. A 1x1 fully transparent mapped
 * surface with a buffer still inhibits indefinitely, as does a mapped
 * surface positioned off every output, or one whose views are
 * retained but not displayed. There is no cheap predicate in
 * this weston that proves visible content without also breaking the
 * covered/minimised-video behaviour we are required to keep
 * (view->output_visibility_mask is repaint-derived and documented
 * stale until repaint; the opaque region is a client hint). What is
 * closed is the zero-effort version: never-mapped and effectively
 * unmapped surfaces cannot hold. Release is not instantaneous in
 * every case — a change no signal reports (an ancestor's unmap, a
 * destroyed subsurface role) is picked up at the next successful
 * backstop sweep, normally within a second plus event-loop delay.
 *
 * ext-idle-notify: every notification is linked from
 * qdwin::idle_notifications. Firing strategy (§6.7(a)):
 *
 *   - If `timeout_ms <= ec->idle_time * 1000`, we fire `idled` on
 *     weston's idle_signal directly. The notification actually fires
 *     at weston's coarser idle, which is later than the client asked
 *     for — spec-compliant ("at least timeout").
 *   - If `timeout_ms >  ec->idle_time * 1000`, we arm a per-
 *     notification wl_event_source timer at idle_signal for
 *     `timeout_ms - idle_time*1000` ms, so `idled` fires at roughly
 *     the right absolute offset. Without this, long-timeout clients
 *     would fire ~idle_time early, which *is* a spec violation.
 *
 * On wake_signal we always disarm the timer and send `resumed` if
 * the notification was idle.
 *
 * If weston is configured with idle_time=0 (built-in idle timer
 * disabled), qdwin internal-idle mode arms timers immediately at
 * notification creation and rearms them on input activity/wake_signal.
 * ------------------------------------------------------------------ */

struct qdwin_idle_notification {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	uint32_t timeout_ms;
	uint64_t last_activity_msec;
	int is_idle;
	int ignore_inhibit;   /* v2 get_input_idle_notification */
	/* §6.7(a): per-notification delay timer. Armed on weston idle_signal
	 * when timeout_ms > weston idle_time*1000, so the notification fires
	 * at its requested offset instead of at weston's coarse idle. */
	struct wl_event_source *timer;
	struct wl_list link;  /* qdwin::idle_notifications */
};

#define QDWIN_IDLE_INTERNAL_INHIBIT_POLL_MS 1000u

static uint64_t
qdwin_now_msec(void)
{
	struct timespec now;
	weston_compositor_get_time(&now);
	return (uint64_t)now.tv_sec * 1000u + (uint64_t)now.tv_nsec / 1000000u;
}

static int
qdwin_idle_internal_next_delay(uint64_t now_msec,
			       const struct qdwin_idle_notification *n)
{
	uint64_t elapsed = now_msec - n->last_activity_msec;
	uint64_t remaining;
	if (elapsed >= n->timeout_ms)
		return 0;
	remaining = n->timeout_ms - elapsed;
	if (remaining == 0)
		remaining = 1;
	return (int)remaining;
}

static void
qdwin_idle_note_activity(struct qdwin *qdwin)
{
	struct qdwin_idle_notification *n;
	uint64_t now_msec;
	if (!qdwin || !qdwin->idle_internal_mode)
		return;
	now_msec = qdwin_now_msec();
	wl_list_for_each(n, &qdwin->idle_notifications, link) {
		if (n->is_idle) {
			if (qdwin->compositor->state != WESTON_COMPOSITOR_ACTIVE)
				continue;
			n->is_idle = 0;
			ext_idle_notification_v1_send_resumed(n->resource);
		}
		n->last_activity_msec = now_msec;
		if (n->timer && n->timeout_ms > 0)
			wl_event_source_timer_update(
				n->timer,
				qdwin_idle_internal_next_delay(now_msec, n));
	}
}

struct qdwin_idle_inhibitor {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct wl_listener surface_destroy_listener;
	struct wl_listener surface_map_listener;
	struct wl_listener surface_unmap_listener;
	int active;           /* currently contributing to ec->idle_inhibit */
	struct wl_list link;  /* qdwin::idle_inhibitors */
};

static void
qdwin_idle_inhibitor_activate(struct qdwin_idle_inhibitor *inh)
{
	if (inh->active)
		return;
	inh->active = 1;
	inh->qdwin->compositor->idle_inhibit++;
}

static void
qdwin_idle_inhibitor_deactivate(struct qdwin_idle_inhibitor *inh)
{
	struct weston_compositor *ec = inh->qdwin->compositor;

	if (!inh->active)
		return;
	inh->active = 0;
	if (ec->idle_inhibit > 0)
		ec->idle_inhibit--;
	/* codex r1 #3: weston's idle_handler() returns early while
	 * inhibited and its timer is one-shot — returning 1 from a
	 * wl_event_source timer callback does not make it periodic. If the
	 * deadline passed under the hold, dropping to zero would otherwise
	 * leave idle-lock/DPMS deferred until unrelated input. Give the
	 * session a fresh idle interval instead, but never wake a
	 * compositor that is already idle/asleep. */
	if (ec->idle_inhibit == 0 && ec->idle_time > 0 &&
	    ec->state == WESTON_COMPOSITOR_ACTIVE && ec->idle_source)
		wl_event_source_timer_update(ec->idle_source,
					     ec->idle_time * 1000);
}

/* iso2/11 E2: single re-evaluation point. Safe to call at any time;
 * activate/deactivate are idempotent via inh->active. */
static void
qdwin_idle_inhibitor_sync(struct qdwin_idle_inhibitor *inh)
{
	bool hold;

	/* codex r3 #1: once the backstop is gone, nothing may hold again —
	 * not this inhibitor on a later map, not a fresh one. Without this
	 * latch, release_all() is momentary and a re-map re-acquires a hold
	 * that will never be re-evaluated. */
	if (inh->qdwin->idle_inhibit_disabled) {
		qdwin_idle_inhibitor_deactivate(inh);
		return;
	}
	hold = qdwin_idle_inhibit_should_hold(
		inh->surface != NULL,
		inh->surface && weston_surface_is_mapped(inh->surface),
		inh->surface && inh->surface->buffer_ref.buffer != NULL,
		inh->surface && !wl_list_empty(&inh->surface->views));

	/* No log here on purpose (codex r1 #7): a client can cycle buffer
	 * attachment cheaply, and this runs per transition per inhibitor. */
	if (hold == (inh->active != 0))
		return;
	if (hold)
		qdwin_idle_inhibitor_activate(inh);
	else
		qdwin_idle_inhibitor_deactivate(inh);
}

static void
qdwin_idle_inhibitor_surface_mapped(struct wl_listener *l, void *data)
{
	struct qdwin_idle_inhibitor *inh =
		wl_container_of(l, inh, surface_map_listener);
	(void)data;
	qdwin_idle_inhibitor_sync(inh);
}

static void
qdwin_idle_inhibitor_surface_unmapped(struct wl_listener *l, void *data)
{
	struct qdwin_idle_inhibitor *inh =
		wl_container_of(l, inh, surface_unmap_listener);
	(void)data;
	qdwin_idle_inhibitor_sync(inh);
}

static void
qdwin_idle_inhibitor_surface_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_idle_inhibitor *inh =
		wl_container_of(l, inh, surface_destroy_listener);
	(void)data;
	inh->surface = NULL;
	qdwin_idle_inhibitor_sync(inh);
	wl_list_remove(&inh->surface_destroy_listener.link);
	wl_list_init(&inh->surface_destroy_listener.link);
	wl_list_remove(&inh->surface_map_listener.link);
	wl_list_init(&inh->surface_map_listener.link);
	wl_list_remove(&inh->surface_unmap_listener.link);
	wl_list_init(&inh->surface_unmap_listener.link);
}

static void
qdwin_idle_inhibitor_destroy(struct wl_client *client,
			     struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct zwp_idle_inhibitor_v1_interface qdwin_idle_inhibitor_impl = {
	.destroy = qdwin_idle_inhibitor_destroy,
};

static void
qdwin_idle_inhibitor_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_idle_inhibitor *inh = wl_resource_get_user_data(resource);
	if (!inh)
		return;
	qdwin_idle_inhibitor_deactivate(inh);
	if (inh->surface) {
		wl_list_remove(&inh->surface_destroy_listener.link);
		wl_list_remove(&inh->surface_map_listener.link);
		wl_list_remove(&inh->surface_unmap_listener.link);
		inh->surface = NULL;
	}
	wl_list_remove(&inh->link);
	free(inh);
}

/* iso2/11 E2 backstop (codex r1 #1/#2): re-evaluate every inhibitor.
 * See the §6.7 banner for why per-surface signals alone are not
 * sufficient. Self-rearming while any inhibitor is alive. */
#define QDWIN_IDLE_INHIBIT_RECHECK_MS 1000

/* Release every hold. Used when the backstop can no longer be guaranteed:
 * a hold we cannot re-evaluate is a permanent, client-controlled
 * suppression of idle-lock and DPMS, so we fail closed. */
static void
qdwin_idle_inhibitors_release_all(struct qdwin *qdwin)
{
	struct qdwin_idle_inhibitor *inh;

	wl_list_for_each(inh, &qdwin->idle_inhibitors, link)
		qdwin_idle_inhibitor_deactivate(inh);
}

static bool
qdwin_idle_inhibit_recheck_schedule(struct qdwin *qdwin)
{
	if (!qdwin->idle_inhibit_recheck_timer)
		return false;
	return wl_event_source_timer_update(
		       qdwin->idle_inhibit_recheck_timer,
		       QDWIN_IDLE_INHIBIT_RECHECK_MS) == 0;
}

static int
qdwin_idle_inhibit_recheck(void *data)
{
	struct qdwin *qdwin = data;
	struct qdwin_idle_inhibitor *inh, *tmp;

	wl_list_for_each_safe(inh, tmp, &qdwin->idle_inhibitors, link)
		qdwin_idle_inhibitor_sync(inh);

	/* Self-rearm owns the cadence. Deliberately NOT re-armed from the
	 * create path (codex r2 #1): re-arming per create would let a
	 * client that churns throwaway inhibitors push the deadline out
	 * forever and strand another client's stale hold. */
	if (!wl_list_empty(&qdwin->idle_inhibitors) &&
	    !qdwin_idle_inhibit_recheck_schedule(qdwin)) {
		weston_log("qdwin: idle-inhibit recheck timer failed to "
			   "re-arm; disabling idle-inhibit and releasing all "
			   "holds (fail-closed, terminal)\n");
		qdwin->idle_inhibit_disabled = true;
		qdwin_idle_inhibitors_release_all(qdwin);
	}
	return 1;
}

/* Create the backstop timer if it does not exist yet. Returns false if
 * idle-inhibit has been terminally disabled by an earlier re-arm failure,
 * or if the timer could not be created; in either case no hold may be
 * accepted. A create-time failure is per-request and does not latch — a
 * later create may retry. */
static bool
qdwin_idle_inhibit_recheck_ensure(struct qdwin *qdwin)
{
	struct wl_event_loop *loop;

	/* Checked before the timer shortcut: a surviving timer pointer is
	 * not evidence that anything is still scheduled (codex r3 #1). */
	if (qdwin->idle_inhibit_disabled)
		return false;
	if (qdwin->idle_inhibit_recheck_timer)
		return true;
	loop = wl_display_get_event_loop(qdwin->compositor->wl_display);
	if (!loop)
		return false;
	qdwin->idle_inhibit_recheck_timer = wl_event_loop_add_timer(
		loop, qdwin_idle_inhibit_recheck, qdwin);
	return qdwin->idle_inhibit_recheck_timer != NULL;
}

static void
qdwin_idle_inhibitors_destroy_all(struct qdwin *qdwin)
{
	struct qdwin_idle_inhibitor *inh, *tmp;

	wl_list_for_each_safe(inh, tmp, &qdwin->idle_inhibitors, link)
		wl_resource_destroy(inh->resource);
	if (qdwin->idle_inhibit_recheck_timer) {
		wl_event_source_remove(qdwin->idle_inhibit_recheck_timer);
		qdwin->idle_inhibit_recheck_timer = NULL;
	}
}

static void
qdwin_idle_inhibit_manager_destroy(struct wl_client *client,
				   struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_idle_inhibit_create_inhibitor(struct wl_client *client,
				    struct wl_resource *resource,
				    uint32_t id,
				    struct wl_resource *surface_resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct weston_surface *surface =
		surface_resource ? wl_resource_get_user_data(surface_resource)
				 : NULL;
	struct qdwin_idle_inhibitor *inh;
	struct wl_resource *inh_resource;
	bool was_empty;

	/* codex r2 #3: never accept a hold we cannot later re-evaluate.
	 * Without the backstop an ancestor unmap would leave a permanent
	 * +1 on ec->idle_inhibit, so a timer we cannot create is a refusal,
	 * not a degraded mode. */
	if (!qdwin_idle_inhibit_recheck_ensure(qdwin)) {
		wl_client_post_no_memory(client);
		return;
	}

	inh = calloc(1, sizeof *inh);
	if (!inh) {
		wl_client_post_no_memory(client);
		return;
	}
	inh_resource = wl_resource_create(
		client, &zwp_idle_inhibitor_v1_interface,
		wl_resource_get_version(resource), id);
	if (!inh_resource) {
		free(inh);
		wl_client_post_no_memory(client);
		return;
	}
	inh->qdwin = qdwin;
	inh->resource = inh_resource;
	inh->surface = surface;
	wl_list_init(&inh->surface_destroy_listener.link);
	wl_list_init(&inh->surface_map_listener.link);
	wl_list_init(&inh->surface_unmap_listener.link);
	if (surface) {
		inh->surface_destroy_listener.notify =
			qdwin_idle_inhibitor_surface_destroyed;
		wl_signal_add(&surface->destroy_signal,
			      &inh->surface_destroy_listener);
		/* iso2/11 E2: the hold follows the surface's mapped state
		 * for the life of the inhibitor, not just at create time. */
		inh->surface_map_listener.notify =
			qdwin_idle_inhibitor_surface_mapped;
		wl_signal_add(&surface->map_signal,
			      &inh->surface_map_listener);
		inh->surface_unmap_listener.notify =
			qdwin_idle_inhibitor_surface_unmapped;
		wl_signal_add(&surface->unmap_signal,
			      &inh->surface_unmap_listener);
	}
	was_empty = wl_list_empty(&qdwin->idle_inhibitors);
	wl_list_insert(&qdwin->idle_inhibitors, &inh->link);
	wl_resource_set_implementation(inh_resource, &qdwin_idle_inhibitor_impl,
				       inh,
				       qdwin_idle_inhibitor_resource_destroy);
	/* Not an unconditional activate: an inhibitor taken on a surface
	 * that is not mapped yet holds nothing until it maps. */
	qdwin_idle_inhibitor_sync(inh);
	/* Arm on the empty -> non-empty transition only; the callback owns
	 * every later deadline (codex r2 #1). */
	if (was_empty && !qdwin_idle_inhibit_recheck_schedule(qdwin)) {
		wl_resource_destroy(inh_resource);
		wl_client_post_no_memory(client);
		return;
	}
	weston_log("qdwin: idle-inhibit created (mapped=%d, count=%u)\n",
		   surface ? (int)weston_surface_is_mapped(surface) : 0,
		   (unsigned)qdwin->compositor->idle_inhibit);
}

static const struct zwp_idle_inhibit_manager_v1_interface
qdwin_idle_inhibit_manager_impl = {
	.destroy         = qdwin_idle_inhibit_manager_destroy,
	.create_inhibitor= qdwin_idle_inhibit_create_inhibitor,
};

static void
bind_qdwin_idle_inhibit_manager(struct wl_client *client, void *data,
				uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource = wl_resource_create(
		client, &zwp_idle_inhibit_manager_v1_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource,
				       &qdwin_idle_inhibit_manager_impl,
				       qdwin, NULL);
}

/* --- zwlr_layer_shell_v1 (stub) ------------------------------------ */
/*
 * Stub implementation. Goals at this layer:
 *   1. Accept the protocol cleanly: bind global, dispatch every request
 *      defined in the v5 XML, store all state on per-surface objects.
 *   2. Drive the configure/ack handshake so a client (waybar via
 *      gtk-layer-shell-0) can complete protocol setup.
 *   3. Set the wl_surface role to "zwlr_layer_surface_v1" so weston
 *      enforces commit-without-role / role-conflict per spec.
 *
 * NOT yet implemented (next iteration):
 *   - Layout: anchor / exclusive_zone / margin → output rect math.
 *     Direct port of wlroots types/scene/layer_shell_v1.c (~150 lines)
 *     calling weston_view_set_position instead of scene_node_set_position.
 *   - Mapping: creating weston_views on the per-zwlr_layer
 *     weston_layer (BACKGROUND/BOTTOM_UI/UI/TOP_UI ladder slots).
 *   - Exclusive-zone propagation into qdwin's xdg-toplevel maximize.
 *   - get_popup integration (xdg_popup positioner against layer bounds).
 *   - Keyboard interactivity modes (none / exclusive / on_demand).
 *
 * Reference: $HOME/doc/quickshell/src/wayland/wlr_layershell/
 *            wlr-layer-shell-unstable-v1.xml (vendored to
 *            qdwin/wlr-layer-shell-unstable-v1.xml).
 *            wlroots/types/wlr_layer_shell_v1.c is the protocol-glue
 *            blueprint, but with libweston API substitutions.
 */

#define QDWIN_LAYER_SURFACE_ROLE "zwlr_layer_surface_v1"

struct qdwin_layer_surface {
	struct qdwin *qdwin;
	struct wl_resource *resource;          /* zwlr_layer_surface_v1 */
	struct weston_surface *surface;
	struct weston_output *output;          /* may be NULL → compositor-pick */
	char *namespace;
	uint32_t layer;                        /* zwlr_layer_shell_v1.layer */

	/* Per-surface state, pending vs current (applied on commit). */
	struct {
		uint32_t desired_w, desired_h;
		uint32_t anchor;
		int32_t  exclusive_zone;
		struct { int32_t top, right, bottom, left; } margin;
		uint32_t kbd_interactivity;
		uint32_t exclusive_edge;
	} pending, current;

	int initial_commit_seen;               /* first commit (no buffer) */
	int initial_configure_sent;            /* sent first configure event */
	uint32_t last_configure_serial;
	uint32_t last_acked_serial;

	/* Layout outputs of the most recent compute (used to detect when a
	 * fresh configure is needed and to position the view). */
	int32_t  box_x, box_y;
	uint32_t box_w, box_h;

	struct weston_view *view;              /* NULL until first post-ack
					        * commit with a buffer */
	int mapped;

	struct wl_listener commit_listener;
	struct wl_listener surface_destroy_listener;
	struct wl_list popups;                 /* qdwin_layer_popup::link */
	struct wl_list link;                   /* qdwin::layer_surfaces */
};

struct qdwin_layer_popup {
	struct qdwin_layer_surface *parent;
	struct wl_resource *popup_resource;    /* xdg_popup */
	struct weston_desktop_surface *desktop_surface;
	struct weston_surface *surface;
	struct weston_view *view;
	struct wl_listener surface_commit_listener;
	struct wl_listener surface_destroy_listener;
	/* plan3 H2 (deep-review): the underlying wl_surface destroy listener
	 * above only fires when the client destroys the wl_surface. A client
	 * can destroy the xdg_popup *role* resource independently (per the
	 * xdg-shell protocol) and libweston's resource-destroy path tears
	 * the desktop_surface down without touching the wl_surface. Without
	 * this listener, a destroyed xdg_popup would leave qdwin_layer_popup
	 * linked under ls->popups with a stale popup_resource — later
	 * commits/dismissals would deref freed libweston state. */
	struct wl_listener popup_resource_destroy_listener;
	struct wl_list link;                   /* qdwin_layer_surface::popups */

	/* plan3 H1: layer-popup grab state. Set when the client calls
	 * xdg_popup.grab and the vendored libweston layer-grab handler
	 * delegates to qdwin. One pointer grab at a time per qdwin
	 * instance (mirrors upstream xdg_popup grab serialisation). */
	struct weston_pointer_grab grab;
	int grab_active;
	/* plan3 H3 (deep-review): seat destroy listener so we know the
	 * cached grab.pointer became dangling before we attempt to end the
	 * grab on it during destroy/dismiss. */
	struct wl_listener seat_destroy_listener;
	struct weston_seat *grab_seat;
};

static void qdwin_layer_popup_destroy(struct qdwin_layer_popup *lp);

/* Pick a default output if the client passed NULL: first in the
 * compositor's output_list. Returns NULL if no outputs are connected
 * (client gets configure with desired or 0×0; we'll re-resolve on next
 * commit when an output may have appeared). */
static struct weston_output *
qdwin_layer_surface_resolve_output(struct qdwin_layer_surface *ls)
{
	if (ls->output)
		return ls->output;
	if (wl_list_empty(&ls->qdwin->compositor->output_list))
		return NULL;
	struct weston_output *o = NULL;
	o = wl_container_of(ls->qdwin->compositor->output_list.next, o, link);
	return o;
}

static int
qdwin_layer_surface_contains(struct qdwin_layer_surface *ls,
			     struct weston_coord_global pos)
{
	if (!ls || !ls->mapped || !ls->view || !ls->surface ||
	    ls->surface->width <= 0 || ls->surface->height <= 0)
		return 0;

	weston_view_update_transform(ls->view);
	struct weston_coord_global vp =
		weston_view_get_pos_offset_global(ls->view);
	if (pos.c.x < vp.c.x || pos.c.x >= vp.c.x + ls->surface->width ||
	    pos.c.y < vp.c.y || pos.c.y >= vp.c.y + ls->surface->height)
		return 0;

	/* Quickshell uses PanelWindow masks/input regions to make full-screen
	 * transparent layer surfaces click-through except for the bar/panel
	 * widgets. Treating the whole mapped bbox as pickable makes those
	 * pass-through surfaces block normal toplevel click-to-focus. Mirror
	 * libweston's picker check here because qdwin has to reason about
	 * layer surfaces before deciding whether to skip toplevel focus. */
	struct weston_coord_surface sp =
		weston_coord_global_to_surface(ls->view, pos);
	if (!pixman_region32_contains_point(&ls->surface->input,
					    sp.c.x, sp.c.y, NULL))
		return 0;
	if (ls->view->geometry.scissor_enabled &&
	    !pixman_region32_contains_point(&ls->view->geometry.scissor,
					    sp.c.x, sp.c.y, NULL))
		return 0;
	return 1;
}

static struct qdwin_layer_surface *
qdwin_layer_surface_at_pos(struct qdwin *qdwin, struct weston_coord_global pos)
{
	if (!qdwin)
		return NULL;

	struct qdwin_layer_surface *ls;
	wl_list_for_each(ls, &qdwin->layer_surfaces, link) {
		if (qdwin_layer_surface_contains(ls, pos))
			return ls;
	}
	return NULL;
}

static struct weston_view *
qdwin_layer_surface_view_at_pos(struct qdwin *qdwin,
				struct weston_coord_global pos)
{
	struct qdwin_layer_surface *ls =
		qdwin_layer_surface_at_pos(qdwin, pos);
	return ls ? ls->view : NULL;
}

static int
qdwin_layer_surface_blocks_toplevel_focus(struct qdwin_layer_surface *ls)
{
	if (!ls)
		return 0;
	if (ls->current.kbd_interactivity !=
	    ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE)
		return 1;

	/* Quickshell keeps a transparent full-output background layer alive
	 * so it can dim/click-close when panels open. With no panel open that
	 * layer has keyboard-interactivity NONE and should not suppress
	 * ordinary click-to-focus for application windows underneath it. */
	struct weston_output *out = qdwin_layer_surface_resolve_output(ls);
	if (out && ls->surface &&
	    ls->current.exclusive_zone <= 0 &&
	    ls->surface->width >= out->width &&
	    ls->surface->height >= out->height)
		return 0;

	return 1;
}

/* plan3 M4: ON_DEMAND keyboard interactivity handler. Called from the
 * default pointer grab on every left-button press. If the press lands
 * on a layer-shell surface that requested ON_DEMAND, transfer keyboard
 * focus there. EXCLUSIVE focus is handled at map time in
 * qdwin_layer_surface_apply; NONE never changes focus.
 *
 * plan3 NEW-H2: do NOT steal focus from an EXCLUSIVE layer surface.
 * A lock screen / OSK / on-call panel that requested EXCLUSIVE expects
 * its keyboard focus to be sticky; a click into a panel/overlay that
 * happens to request ON_DEMAND would otherwise silently take focus. We
 * walk every mapped layer surface and abort the transfer if any other
 * one with EXCLUSIVE currently owns kb->focus. */
static void
qdwin_layer_surface_handle_on_demand_button(struct qdwin *qdwin,
					    struct weston_pointer *pointer)
{
	if (!qdwin || !pointer)
		return;
	struct qdwin_layer_surface *ls =
		qdwin_layer_surface_at_pos(qdwin, pointer->pos);
	if (!ls || !ls->surface)
		return;
	if (ls->current.kbd_interactivity !=
	    ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_ON_DEMAND)
		return;
	struct weston_keyboard *kb = weston_seat_get_keyboard(pointer->seat);
	if (!kb || kb->focus == ls->surface)
		return;

	/* NEW-H2 guard: refuse if an EXCLUSIVE layer surface currently
	 * owns keyboard focus. Compare by ls->surface so a mapped
	 * EXCLUSIVE layer surface that re-mapped into a different
	 * weston_surface (rare) does not match accidentally. */
	struct qdwin_layer_surface *other;
	wl_list_for_each(other, &qdwin->layer_surfaces, link) {
		if (other == ls || !other->surface)
			continue;
		if (other->current.kbd_interactivity !=
		    ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE)
			continue;
		if (kb->focus == other->surface) {
			weston_log("qdwin: layer-shell ON_DEMAND focus "
				   "skipped (EXCLUSIVE held by ns=%s) "
				   "target ns=%s\n",
				   other->namespace ? other->namespace
						    : "(null)",
				   ls->namespace ? ls->namespace : "(null)");
			return;
		}
	}

	weston_keyboard_set_focus(kb, ls->surface);
	weston_log("qdwin: layer-shell ON_DEMAND focus -> ns=%s seat=%s\n",
		   ls->namespace ? ls->namespace : "(null)",
		   (pointer->seat && pointer->seat->seat_name)
			   ? pointer->seat->seat_name : "");
}

/* Per spec: when exclusive_edge is unset (0), derive from anchor.
 * - Anchored to one edge → that edge.
 * - Anchored to three edges (one "free" edge) → the opposite of the
 *   free edge.
 * - Otherwise: NONE (no zone reserved).
 * Encoded as the same bitfield as anchor: TOP/BOTTOM/LEFT/RIGHT. */
static uint32_t
qdwin_layer_surface_get_exclusive_edge(const struct qdwin_layer_surface *ls)
{
	/* Pure derivation lives in qdwin-logic.c; the original "edge unset"
	 * sentinel is exclusive_edge == 0. */
	return qdwin_layer_exclusive_edge(ls->current.anchor,
					  ls->current.exclusive_edge != 0,
					  ls->current.exclusive_edge);
}
_Static_assert(ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP == QDWIN_LOGIC_ANCHOR_TOP,
	       "layer anchor bit drift vs qdwin-logic.h");
_Static_assert(ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM == QDWIN_LOGIC_ANCHOR_BOTTOM,
	       "layer anchor bit drift vs qdwin-logic.h");
_Static_assert(ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT == QDWIN_LOGIC_ANCHOR_LEFT,
	       "layer anchor bit drift vs qdwin-logic.h");
_Static_assert(ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT == QDWIN_LOGIC_ANCHOR_RIGHT,
	       "layer anchor bit drift vs qdwin-logic.h");

/* Phase 1.3: forward-declared at top of file; called by
 * qdwin_output_work_area. Subtracts every mapped layer-shell surface's
 * exclusive zone from the working rect. exclusive_zone == -1 means
 * "occupies full output, doesn't reserve" — handled by the
 * exclusive_zone <= 0 skip. Margin on the anchored edge is added to
 * the zone (matches wlroots layer_surface_exclusive_zone semantics). */
static void
qdwin_layer_shell_subtract_zones(struct qdwin *qdwin,
				 struct weston_output *out,
				 int *x, int *y, int *w, int *h)
{
	struct qdwin_layer_surface *ls;
	wl_list_for_each(ls, &qdwin->layer_surfaces, link) {
		if (!ls->mapped) continue;
		if (ls->output && ls->output != out) continue;
		if (ls->current.exclusive_zone <= 0) continue;
		uint32_t edge = qdwin_layer_surface_get_exclusive_edge(ls);
		int t = ls->current.exclusive_zone;
		switch (edge) {
		case ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP:
			t += ls->current.margin.top;
			*y += t; *h -= t; break;
		case ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM:
			t += ls->current.margin.bottom;
			*h -= t; break;
		case ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT:
			t += ls->current.margin.left;
			*x += t; *w -= t; break;
		case ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT:
			t += ls->current.margin.right;
			*w -= t; break;
		default: break; /* ambiguous → no shrink */
		}
	}
}

/* Port of wlroots types/scene/layer_shell_v1.c
 * wlr_scene_layer_surface_v1_configure (commit a8bb88b cross-ref).
 * Computes the surface rect inside `bounds` (full output rect when
 * exclusive_zone == -1, otherwise the usable area shrunk by previously
 * placed exclusive zones). Writes result to ls->box_{x,y,w,h}. */
static void
qdwin_layer_surface_compute_box(struct qdwin_layer_surface *ls,
				int32_t bx, int32_t by,
				int32_t bw, int32_t bh)
{
	/* Pure layout math lives in qdwin-logic.c; we read the struct
	 * inputs, run the kernel, and store the placed rect back. */
	qdwin_layer_compute_box(ls->current.anchor,
				(int32_t)ls->current.desired_w,
				(int32_t)ls->current.desired_h,
				ls->current.margin.top,
				ls->current.margin.right,
				ls->current.margin.bottom,
				ls->current.margin.left,
				bx, by, bw, bh,
				&ls->box_x, &ls->box_y,
				&ls->box_w, &ls->box_h);
}

static void
qdwin_layer_surface_send_configure(struct qdwin_layer_surface *ls)
{
	struct weston_output *out = qdwin_layer_surface_resolve_output(ls);
	int32_t fx = 0, fy = 0, fw = 1920, fh = 1080;
	if (out) {
		fx = out->pos.c.x;
		fy = out->pos.c.y;
		fw = out->width;
		fh = out->height;
	}
	/* Phase 1.1: full_area = output rect. Per-output usable_area
	 * (Phase 1.3) will subtract previously placed exclusive zones. */
	qdwin_layer_surface_compute_box(ls, fx, fy, fw, fh);

	uint32_t serial = ++ls->qdwin->layer_configure_serial_next;
	ls->last_configure_serial = serial;
	zwlr_layer_surface_v1_send_configure(ls->resource, serial,
					     ls->box_w, ls->box_h);
	weston_log("qdwin: layer-shell configure ns=%s layer=%u "
		   "box=%d,%d %ux%u serial=%u\n",
		   ls->namespace ? ls->namespace : "(null)",
		   ls->layer, ls->box_x, ls->box_y,
		   ls->box_w, ls->box_h, serial);
}

/* Lazily create the weston_view + place it on the right layer + map.
 * Idempotent across commits; subsequent commits just re-position. */
static void
qdwin_layer_surface_apply(struct qdwin_layer_surface *ls)
{
	struct qdwin *qdwin = ls->qdwin;
	if (ls->layer > ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY)
		return;

	if (!ls->view) {
		ls->view = weston_view_create(ls->surface);
		if (!ls->view) {
			weston_log("qdwin: layer-shell view_create failed "
				   "ns=%s\n",
				   ls->namespace ? ls->namespace : "(null)");
			return;
		}
		weston_view_move_to_layer(ls->view,
			&qdwin->layer_shell_layer[ls->layer].view_list);
	}

	struct weston_coord_global pos = {
		.c = weston_coord(ls->box_x, ls->box_y)
	};
	weston_view_set_position(ls->view, pos);

	if (!ls->mapped) {
		if (!weston_surface_is_mapped(ls->surface))
			weston_surface_map(ls->surface);
		ls->mapped = 1;
		/* Log "mapped" only on the !mapped→mapped transition (i.e.
		 * an actual map event), not on every subsequent same-shape
		 * commit. Otherwise a client that repaints at 60 Hz (clock
		 * widget, animation) floods the journal with one "mapped"
		 * line per frame, drowning real qdwin signal — see
		 * todo/qdshell-bar-remap-storm.md. Reconfigures still log
		 * separately in qdwin_layer_surface_on_commit. */
		weston_log("qdwin: layer-shell mapped ns=%s layer=%u "
			   "at %d,%d %ux%u\n",
			   ls->namespace ? ls->namespace : "(null)",
			   ls->layer, ls->box_x, ls->box_y,
			   ls->box_w, ls->box_h);
		/* Phase 1.3: a newly mapped panel with exclusive_zone > 0
		 * shrinks the work area; reflow any maximised toplevels.
		 * Cheap to call unconditionally — qdwin_panels_on_output_change
		 * is a no-op when nothing is maximised. */
		qdwin_panels_on_output_change(qdwin);
	}

	/* Phase 1.5: if the client requested EXCLUSIVE keyboard
	 * interactivity (e.g. swaylock-style overlays), grant focus to
	 * this layer surface on every apply while still EXCLUSIVE. The
	 * shell's own focus orchestration via set_keyboard_focus_v2 keeps
	 * working for normal toplevels — EXCLUSIVE is a compositor-side
	 * override that mirrors wlroots's behaviour for OVERLAY/TOP locker
	 * surfaces.
	 *
	 * ON_DEMAND (focus-on-click) is wired in plan3 M4 via
	 * qdwin_layer_surface_handle_on_demand_button (called from
	 * qdwin_proxy_default_grab_button). EXCLUSIVE is granted here at
	 * map/apply time; ON_DEMAND is granted at left-button-press time
	 * and ON_DEMAND will not steal focus from a held EXCLUSIVE
	 * (see post-deep-review NEW-H2 guard). NONE leaves focus alone. */
	if (ls->current.kbd_interactivity ==
	    ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE) {
		struct weston_seat *seat;
		wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
			if (weston_seat_get_keyboard(seat))
				weston_seat_set_keyboard_focus(seat, ls->surface);
		}
	}
}

static void
qdwin_layer_surface_on_commit(struct wl_listener *l, void *data)
{
	struct qdwin_layer_surface *ls =
		wl_container_of(l, ls, commit_listener);
	(void)data;

	/* Promote pending → current. The state is "double buffered" per
	 * spec: setters store into pending, commit applies to current. */
	ls->current = ls->pending;

	if (!ls->initial_configure_sent) {
		/* Spec: on the initial commit (no buffer attached) the
		 * compositor sends its first configure. */
		ls->initial_commit_seen = 1;
		ls->initial_configure_sent = 1;
		qdwin_layer_surface_send_configure(ls);
		return;
	}

	/* Subsequent commit. Recompute layout (anchor/size/margin may have
	 * changed since the last configure) and either send a new
	 * configure if the rect changed, or accept the current rect and
	 * map the view. */
	int32_t prev_x = ls->box_x, prev_y = ls->box_y;
	uint32_t prev_w = ls->box_w, prev_h = ls->box_h;

	struct weston_output *out = qdwin_layer_surface_resolve_output(ls);
	int32_t fx = 0, fy = 0, fw = 1920, fh = 1080;
	if (out) {
		fx = out->pos.c.x; fy = out->pos.c.y;
		fw = out->width;   fh = out->height;
	}
	qdwin_layer_surface_compute_box(ls, fx, fy, fw, fh);

	if (ls->box_w != prev_w || ls->box_h != prev_h) {
		uint32_t serial = ++ls->qdwin->layer_configure_serial_next;
		ls->last_configure_serial = serial;
		zwlr_layer_surface_v1_send_configure(ls->resource, serial,
						     ls->box_w, ls->box_h);
		weston_log("qdwin: layer-shell reconfigure ns=%s "
			   "box=%d,%d %ux%u serial=%u (was %ux%u)\n",
			   ls->namespace ? ls->namespace : "(null)",
			   ls->box_x, ls->box_y,
			   ls->box_w, ls->box_h, serial, prev_w, prev_h);
		(void)prev_x; (void)prev_y;
		return;
	}

	qdwin_layer_surface_apply(ls);
	/* "mapped" is logged inside qdwin_layer_surface_apply on the
	 * unmapped→mapped transition only — not on every subsequent
	 * same-shape commit. See todo/qdshell-bar-remap-storm.md. */
}

static void
qdwin_layer_surface_on_surface_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_layer_surface *ls =
		wl_container_of(l, ls, surface_destroy_listener);
	(void)data;
	wl_list_remove(&ls->surface_destroy_listener.link);
	wl_list_init(&ls->surface_destroy_listener.link);
	wl_list_remove(&ls->commit_listener.link);
	wl_list_init(&ls->commit_listener.link);
	int was_reserving = ls->mapped &&
			    ls->current.exclusive_zone > 0;
	if (ls->view) {
		if (ls->mapped)
			weston_view_unmap(ls->view);
		weston_view_destroy(ls->view);
		ls->view = NULL;
		ls->mapped = 0;
	}
	struct qdwin_layer_popup *lp, *tmp;
	wl_list_for_each_safe(lp, tmp, &ls->popups, link)
		qdwin_layer_popup_destroy(lp);
	ls->surface = NULL;
	if (was_reserving)
		qdwin_panels_on_output_change(ls->qdwin);
	/* Per spec: closed event when surface is forcibly destroyed. */
	if (ls->resource)
		zwlr_layer_surface_v1_send_closed(ls->resource);
}

/* --- per-layer-surface request handlers --- */

static void
qdwin_layer_surface_set_size(struct wl_client *client,
			     struct wl_resource *resource,
			     uint32_t width, uint32_t height)
{
	(void)client;
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;
	ls->pending.desired_w = width;
	ls->pending.desired_h = height;
}

static void
qdwin_layer_surface_set_anchor(struct wl_client *client,
			       struct wl_resource *resource, uint32_t anchor)
{
	(void)client;
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;
	uint32_t valid = ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP
		       | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM
		       | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT
		       | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT;
	if (anchor & ~valid) {
		wl_resource_post_error(resource,
			ZWLR_LAYER_SURFACE_V1_ERROR_INVALID_ANCHOR,
			"invalid anchor bitfield 0x%x", anchor);
		return;
	}
	ls->pending.anchor = anchor;
}

static void
qdwin_layer_surface_set_exclusive_zone(struct wl_client *client,
				       struct wl_resource *resource,
				       int32_t zone)
{
	(void)client;
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;
	ls->pending.exclusive_zone = zone;
}

static void
qdwin_layer_surface_set_margin(struct wl_client *client,
			       struct wl_resource *resource,
			       int32_t top, int32_t right,
			       int32_t bottom, int32_t left)
{
	(void)client;
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;
	ls->pending.margin.top    = top;
	ls->pending.margin.right  = right;
	ls->pending.margin.bottom = bottom;
	ls->pending.margin.left   = left;
}

static void
qdwin_layer_surface_set_keyboard_interactivity(
	struct wl_client *client, struct wl_resource *resource,
	uint32_t interactivity)
{
	(void)client;
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;
	if (interactivity > ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_ON_DEMAND) {
		wl_resource_post_error(resource,
			ZWLR_LAYER_SURFACE_V1_ERROR_INVALID_KEYBOARD_INTERACTIVITY,
			"invalid kbd interactivity %u", interactivity);
		return;
	}
	/* Phase 1.5: NONE (default — waybar's default) leaves qdwin's
	 * shell-driven focus model alone. EXCLUSIVE is wired in
	 * qdwin_layer_surface_apply: every apply while EXCLUSIVE grants
	 * keyboard focus to this surface across all seats with a keyboard
	 * (matches wlroots semantics for swaylock-style OVERLAY surfaces).
	 * ON_DEMAND (focus-on-click) is wired in plan3 M4 via
	 * qdwin_proxy_default_grab_button → qdwin_layer_surface_handle_on_demand_button;
	 * NEW-H2 guards it against stealing focus from an EXCLUSIVE
	 * holder. The pending value is the source of truth for both paths. */
	ls->pending.kbd_interactivity = interactivity;
}

typedef bool (*qdwin_xdg_popup_attach_layer_parent_fn)(
	struct wl_resource *popup_resource,
	struct weston_surface *parent_surface);
typedef bool (*qdwin_xdg_popup_get_geometry_fn)(
	struct wl_resource *popup_resource,
	struct weston_geometry *geometry);

/* plan3 H1: dlsym typedefs for the new layer-popup grab helpers in
 * vendored libweston. Kept as soft references so qdwin still links and
 * runs against an unpatched libweston (the grab call will then
 * gracefully fall back to libweston's INVALID_GRAB error path). */
typedef bool (*qdwin_xdg_popup_layer_grab_handler_t)(
	struct wl_resource *popup_resource,
	struct weston_surface *popup_surface,
	struct weston_surface *layer_parent_surface,
	struct weston_seat *seat,
	uint32_t serial,
	void *user_data);

typedef void (*qdwin_xdg_popup_set_layer_grab_handler_fn)(
	qdwin_xdg_popup_layer_grab_handler_t handler,
	void *user_data);

typedef void (*qdwin_xdg_popup_dismiss_layer_grab_fn)(
	struct wl_resource *popup_resource);

static qdwin_xdg_popup_attach_layer_parent_fn
qdwin_xdg_popup_attach_layer_parent_sym(void)
{
	static qdwin_xdg_popup_attach_layer_parent_fn fn;
	static int looked_up;
	if (!looked_up) {
		fn = (qdwin_xdg_popup_attach_layer_parent_fn)dlsym(
			RTLD_DEFAULT,
			"weston_desktop_xdg_popup_attach_layer_parent");
		looked_up = 1;
	}
	return fn;
}

static qdwin_xdg_popup_get_geometry_fn
qdwin_xdg_popup_get_geometry_sym(void)
{
	static qdwin_xdg_popup_get_geometry_fn fn;
	static int looked_up;
	if (!looked_up) {
		fn = (qdwin_xdg_popup_get_geometry_fn)dlsym(
			RTLD_DEFAULT,
			"weston_desktop_xdg_popup_get_geometry");
		looked_up = 1;
	}
	return fn;
}

static qdwin_xdg_popup_set_layer_grab_handler_fn
qdwin_xdg_popup_set_layer_grab_handler_sym(void)
{
	static qdwin_xdg_popup_set_layer_grab_handler_fn fn;
	static int looked_up;
	if (!looked_up) {
		fn = (qdwin_xdg_popup_set_layer_grab_handler_fn)dlsym(
			RTLD_DEFAULT,
			"weston_desktop_xdg_popup_set_layer_grab_handler");
		looked_up = 1;
	}
	return fn;
}

static qdwin_xdg_popup_dismiss_layer_grab_fn
qdwin_xdg_popup_dismiss_layer_grab_sym(void)
{
	static qdwin_xdg_popup_dismiss_layer_grab_fn fn;
	static int looked_up;
	if (!looked_up) {
		fn = (qdwin_xdg_popup_dismiss_layer_grab_fn)dlsym(
			RTLD_DEFAULT,
			"weston_desktop_xdg_popup_dismiss_layer_grab");
		looked_up = 1;
	}
	return fn;
}

static void
qdwin_layer_popup_update_position(struct qdwin_layer_popup *lp)
{
	if (!lp || !lp->parent || !lp->view)
		return;

	struct weston_geometry g = { 0 };
	qdwin_xdg_popup_get_geometry_fn get_geometry =
		qdwin_xdg_popup_get_geometry_sym();
	if (!get_geometry || !get_geometry(lp->popup_resource, &g))
		return;

	struct weston_coord_global pos = {
		.c = weston_coord(lp->parent->box_x + g.x,
				  lp->parent->box_y + g.y)
	};
	weston_view_set_position(lp->view, pos);
	weston_view_update_transform(lp->view);
}

static void
qdwin_layer_popup_destroy(struct qdwin_layer_popup *lp)
{
	if (!lp)
		return;

	/* plan3 NEW-H1 + deep-review H3/H5: end any active pointer grab so
	 * libweston rebinds the default grab before we free the
	 * qdwin_layer_popup. Subtleties:
	 *
	 * - In vendored libweston, weston_pointer_end_grab (input.c:2113)
	 *   only swaps pointer->grab back to default_grab and calls
	 *   default_grab->focus(). It does NOT call our .cancel op, so
	 *   end_grab here will not synchronously fire any popup_done.
	 *   We still null popup_resource first (a) to make the intent
	 *   obvious — once destroy starts, no further events may be sent
	 *   on this resource — and (b) because our .cancel op IS invoked
	 *   from the explicit teardown paths (weston_pointer_cancel_grab
	 *   on seat keyboard release at input.c:2802); nulling first means
	 *   any racing cancel sees popup_resource == NULL and becomes a
	 *   no-op.
	 *
	 * - lp->grab.pointer is the pointer cached at start time. We must
	 *   not dereference it if its owning seat was destroyed since (the
	 *   seat_destroy_listener nulls lp->grab_seat for that case; we
	 *   skip end_grab and just release our state). */
	if (lp->grab_active && lp->grab_seat && lp->grab.pointer &&
	    lp->grab.pointer->grab == &lp->grab) {
		struct weston_pointer *p = lp->grab.pointer;
		lp->popup_resource = NULL;        /* suppresses cancel→dismiss */
		lp->grab_active = 0;
		weston_pointer_end_grab(p);
	} else {
		lp->grab_active = 0;
	}
	if (lp->seat_destroy_listener.link.next)
		wl_list_remove(&lp->seat_destroy_listener.link);
	lp->grab_seat = NULL;

	wl_list_remove(&lp->link);
	if (lp->surface_commit_listener.link.next)
		wl_list_remove(&lp->surface_commit_listener.link);
	if (lp->surface_destroy_listener.link.next)
		wl_list_remove(&lp->surface_destroy_listener.link);
	if (lp->popup_resource_destroy_listener.link.next)
		wl_list_remove(&lp->popup_resource_destroy_listener.link);

	if (lp->view) {
		weston_desktop_surface_unlink_view(lp->view);
		weston_view_destroy(lp->view);
	}
	free(lp);
}

/* plan3 H2 (deep-review): xdg_popup resource was destroyed — qdwin must
 * release its qdwin_layer_popup before any further commit/dismiss path
 * dereferences stale libweston data. lp->popup_resource is nulled first
 * to suppress any outgoing dismiss event aimed at the dying resource. */
static void
qdwin_layer_popup_on_popup_resource_destroyed(struct wl_listener *listener,
					      void *data)
{
	struct qdwin_layer_popup *lp =
		wl_container_of(listener, lp, popup_resource_destroy_listener);
	(void)data;
	lp->popup_resource = NULL;
	qdwin_layer_popup_destroy(lp);
}

/* plan3 H3 (deep-review): the grab pointer's owning seat was destroyed.
 * Null the cached lp->grab_seat so qdwin_layer_popup_destroy skips its
 * weston_pointer_end_grab call. The pointer struct itself may be freed
 * already — we never deref it from here.
 *
 * deep-review-2 DPF2-H1: also unlink the listener from the (now
 * destroyed) seat's destroy_signal list and re-init the link node so
 * later cleanup paths (qdwin_layer_popup_destroy, or a follow-up grab
 * on a different seat) can safely call wl_list_remove on a quiescent
 * listener. libwayland's destroy-signal dispatch does not detach
 * listeners after firing; the listener.link prev/next still reference
 * the dying signal's storage until we unlink. */
static void
qdwin_layer_popup_on_grab_seat_destroyed(struct wl_listener *listener,
					 void *data)
{
	struct qdwin_layer_popup *lp =
		wl_container_of(listener, lp, seat_destroy_listener);
	(void)data;
	weston_log("qdwin: layer-popup grab seat destroyed; clearing cached "
		   "pointer\n");
	wl_list_remove(&lp->seat_destroy_listener.link);
	wl_list_init(&lp->seat_destroy_listener.link);
	lp->grab_seat = NULL;
	lp->grab_active = 0;
	/* lp->grab.pointer is now stale; do not deref. weston_pointer was
	 * freed alongside the seat. */
	lp->grab.pointer = NULL;
}

/* plan3 H1: layer-popup grab interface. xdg_popup.grab on a layer-
 * parented popup cannot use libweston's weston_desktop_seat_popup_grab
 * machinery (it requires desktop_surface parents). We install our own
 * weston_pointer grab that dismisses the popup on outside-click via
 * weston_desktop_xdg_popup_dismiss_layer_grab (which sends
 * xdg_popup.popup_done). Inside-popup events go through normal pointer
 * delivery. */

static int
qdwin_layer_popup_bbox_contains(struct qdwin_layer_popup *lp,
				struct weston_coord_global pos)
{
	if (!lp || !lp->view || !lp->surface ||
	    lp->surface->width <= 0 || lp->surface->height <= 0)
		return 0;
	struct weston_coord_global vp =
		weston_view_get_pos_offset_global(lp->view);
	return pos.c.x >= vp.c.x && pos.c.x < vp.c.x + lp->surface->width &&
	       pos.c.y >= vp.c.y && pos.c.y < vp.c.y + lp->surface->height;
}

/* D9: is this surface an xdg_popup (the role libweston stamps on
 * weston_desktop popups)? Used to keep a same-client *nested* submenu popup
 * inside the grab while still excluding unrelated same-client surfaces
 * (hidden toplevels, other panels). Role is matched by name. */
static bool
qdwin_surface_is_xdg_popup(struct weston_surface *s)
{
	const char *role = s ? weston_surface_get_role(s) : NULL;
	return role && strcmp(role, "xdg_popup") == 0;
}

/* deep-review-2 H2 / D9: repick the topmost view at the pointer and clamp
 * pointer->focus to the popup surface, its layer parent, a subsurface of
 * either, or a same-client nested submenu popup; clear focus otherwise.
 * Called by every grab event hook before sending events so stale pre-grab
 * focus cannot receive button/axis/frame.
 *
 * Side effect: pointer->focus reflects the filter result on return.
 * Callers can compare against NULL to decide whether to forward the
 * current event to a downstream client. */
static void
qdwin_layer_popup_grab_refilter_focus(struct qdwin_layer_popup *lp,
				      struct weston_pointer *pointer)
{
	if (!pointer)
		return;
	struct weston_view *view = weston_compositor_pick_view(
		pointer->seat->compositor, pointer->pos);
	int deliver = 0;
	if (view && view->surface) {
		/* D9 (deeper-review): confine grabbed input to the popup family,
		 * NOT to every surface owned by the popup's wl_client. Legitimate
		 * targets are the popup surface, its layer parent (the anchor), a
		 * subsurface of either (weston_surface_get_main_surface folds
		 * subsurfaces onto their root), and a same-client *nested* submenu
		 * popup (a separate xdg_popup, not a subsurface). Everything else
		 * the old code reached via "any same-client surface" — a hidden
		 * sibling toplevel, an unrelated panel — is excluded. All derefs
		 * guarded (teardown/destroy paths null these). */
		struct weston_surface *parent_s =
			lp->parent ? lp->parent->surface : NULL;
		struct weston_surface *main_s =
			weston_surface_get_main_surface(view->surface);
		struct wl_client *grab_client = lp->popup_resource ?
			wl_resource_get_client(lp->popup_resource) : NULL;
		if (view->surface == lp->surface ||
		    view->surface == parent_s ||
		    main_s == lp->surface ||
		    main_s == parent_s) {
			deliver = 1;
		} else if (grab_client && view->surface->resource &&
			   wl_resource_get_client(view->surface->resource) ==
			       grab_client &&
			   qdwin_surface_is_xdg_popup(view->surface)) {
			deliver = 1;   /* same-client nested submenu popup */
		}
	}
	if (deliver) {
		if (view != pointer->focus)
			weston_pointer_set_focus(pointer, view);
	} else if (pointer->focus) {
		/* libweston accepts NULL view → clears focus + sends leave. */
		weston_pointer_set_focus(pointer, NULL);
	}
}

/* deep-review-2 H2: weston_pointer_start_grab invokes the grab's .focus
 * op synchronously. Without filtering here, the pre-grab focus survives
 * until the first motion event, so a button/axis/frame received in
 * between would be delivered to the prior focus client. */
static void
qdwin_layer_popup_grab_focus(struct weston_pointer_grab *grab)
{
	struct qdwin_layer_popup *lp = wl_container_of(grab, lp, grab);
	qdwin_layer_popup_grab_refilter_focus(lp, grab->pointer);
}

static void
qdwin_layer_popup_grab_motion(struct weston_pointer_grab *grab,
			      const struct weston_pointer_motion_event *event)
{
	/* J29: 14->16 dropped `time` param. */
	struct qdwin_layer_popup *lp = wl_container_of(grab, lp, grab);
	struct weston_pointer *pointer = grab->pointer;
	weston_pointer_move(pointer, event);
	qdwin_layer_popup_grab_refilter_focus(lp, pointer);
	if (pointer->focus)
		weston_pointer_send_motion(pointer, event);
}

static void
qdwin_layer_popup_grab_button(struct weston_pointer_grab *grab,
			      const struct weston_pointer_button_event *button_event)
{
	/* J29: 14->16 button/state now come from the event struct. */
	uint32_t state = button_event->button_state;
	struct qdwin_layer_popup *lp = wl_container_of(grab, lp, grab);
	struct weston_pointer *pointer = grab->pointer;

	if (state == WL_POINTER_BUTTON_STATE_PRESSED &&
	    !qdwin_layer_popup_bbox_contains(lp, pointer->pos)) {
		weston_log("qdwin: layer-popup dismissed by outside click "
			   "at (%.0f,%.0f)\n",
			   pointer->pos.c.x, pointer->pos.c.y);
		/* plan3 H5 (deep-review): dismiss exactly once.
		 * weston_pointer_end_grab itself does not call our .cancel op
		 * (it only restores default_grab and calls default_grab->focus,
		 * input.c:2113), so end_grab on its own will not double-fire
		 * popup_done. The hazard the ordering still defends against is
		 * a racing explicit weston_pointer_cancel_grab from seat
		 * teardown (input.c:2802) that would call our cancel handler
		 * while we are mid-dismiss. Null popup_resource first; the
		 * cancel handler treats NULL as "already dismissed" and stays
		 * a no-op. Then send the one dismiss, then end the grab. */
		struct wl_resource *r = lp->popup_resource;
		lp->popup_resource = NULL;
		if (r) {
			qdwin_xdg_popup_dismiss_layer_grab_fn dismiss =
				qdwin_xdg_popup_dismiss_layer_grab_sym();
			if (dismiss)
				dismiss(r);
		}
		if (lp->grab_active) {
			lp->grab_active = 0;
			weston_pointer_end_grab(pointer);
		}
		return;
	}
	/* deep-review-2 H2: clamp focus before delivering the button so a
	 * press that arrived right after start_grab (no motion yet) cannot
	 * use stale focus. send_button is a no-op when focus is NULL. */
	qdwin_layer_popup_grab_refilter_focus(lp, pointer);
	if (pointer->focus)
		weston_pointer_send_button(pointer, button_event);
}

static void
qdwin_layer_popup_grab_axis(struct weston_pointer_grab *grab,
			    const struct weston_pointer_axis_event *event)
{
	/* J29: 14->16 dropped `time` param. */
	struct qdwin_layer_popup *lp = wl_container_of(grab, lp, grab);
	struct weston_pointer *pointer = grab->pointer;
	qdwin_layer_popup_grab_refilter_focus(lp, pointer);
	if (pointer->focus)
		weston_pointer_send_axis(pointer, event);
}

static void
qdwin_layer_popup_grab_axis_source(struct weston_pointer_grab *grab,
				   uint32_t source)
{
	struct qdwin_layer_popup *lp = wl_container_of(grab, lp, grab);
	struct weston_pointer *pointer = grab->pointer;
	qdwin_layer_popup_grab_refilter_focus(lp, pointer);
	if (pointer->focus)
		weston_pointer_send_axis_source(pointer, source);
}

static void
qdwin_layer_popup_grab_frame(struct weston_pointer_grab *grab)
{
	struct qdwin_layer_popup *lp = wl_container_of(grab, lp, grab);
	struct weston_pointer *pointer = grab->pointer;
	qdwin_layer_popup_grab_refilter_focus(lp, pointer);
	if (pointer->focus)
		weston_pointer_send_frame(pointer);
}

static void
qdwin_layer_popup_grab_cancel(struct weston_pointer_grab *grab)
{
	struct qdwin_layer_popup *lp = wl_container_of(grab, lp, grab);
	lp->grab_active = 0;
	if (lp->popup_resource) {
		qdwin_xdg_popup_dismiss_layer_grab_fn dismiss =
			qdwin_xdg_popup_dismiss_layer_grab_sym();
		if (dismiss)
			dismiss(lp->popup_resource);
	}
}

static const struct weston_pointer_grab_interface
qdwin_layer_popup_grab_iface = {
	.focus       = qdwin_layer_popup_grab_focus,
	.motion      = qdwin_layer_popup_grab_motion,
	.button      = qdwin_layer_popup_grab_button,
	.axis        = qdwin_layer_popup_grab_axis,
	.axis_source = qdwin_layer_popup_grab_axis_source,
	.frame       = qdwin_layer_popup_grab_frame,
	.cancel      = qdwin_layer_popup_grab_cancel,
};

/* Forward declaration: defined later to find the qdwin_layer_popup that
 * owns a given xdg_popup resource. */
static struct qdwin_layer_popup *
qdwin_layer_popup_for_resource(struct qdwin *qdwin,
			       struct wl_resource *popup_resource);

/* plan3 H1: layer-popup grab handler called from vendored libweston's
 * weston_desktop_xdg_popup_protocol_grab when the popup is layer-
 * parented and qdwin has registered this handler. Returning false makes
 * libweston post XDG_POPUP_ERROR_INVALID_GRAB to the client. */
static bool
qdwin_layer_popup_layer_grab_handler(struct wl_resource *popup_resource,
				     struct weston_surface *popup_surface,
				     struct weston_surface *layer_parent_surface,
				     struct weston_seat *wseat,
				     uint32_t serial,
				     void *user_data)
{
	struct qdwin *qdwin = user_data;
	(void)popup_surface;
	(void)layer_parent_surface;
	if (!qdwin || !wseat || !popup_resource)
		return false;

	/* Validate that one of the input devices on this seat has a grab
	 * serial matching the request. Mirrors libweston's serial check
	 * in weston_desktop_seat_popup_grab_start. Shares the serial-match
	 * predicate with qdwin_handle_show_popup (D7). */
	struct weston_pointer *pointer = weston_seat_get_pointer(wseat);
	if (!qdwin_seat_grab_serial_matches(wseat, serial)) {
		struct weston_keyboard *keyboard = weston_seat_get_keyboard(wseat);
		struct weston_touch *touch = weston_seat_get_touch(wseat);
		weston_log("qdwin: layer-popup grab refused: stale serial=%u "
			   "(p=%u k=%u t=%u)\n", serial,
			   pointer ? pointer->grab_serial : 0,
			   keyboard ? keyboard->grab_serial : 0,
			   touch ? touch->grab_serial : 0);
		return false;
	}

	struct qdwin_layer_popup *lp =
		qdwin_layer_popup_for_resource(qdwin, popup_resource);
	if (!lp) {
		weston_log("qdwin: layer-popup grab refused: popup not "
			   "tracked\n");
		return false;
	}

	if (!pointer) {
		weston_log("qdwin: layer-popup grab refused: no pointer on "
			   "seat\n");
		return false;
	}
	if (lp->grab_active) {
		weston_log("qdwin: layer-popup grab already active for "
			   "popup=%p\n", (void *)popup_resource);
		return true;
	}
	lp->grab.interface = &qdwin_layer_popup_grab_iface;
	weston_pointer_start_grab(pointer, &lp->grab);
	lp->grab_active = 1;
	/* plan3 H3 (deep-review): subscribe to the seat's destroy signal so
	 * that if the seat goes away mid-grab, the cached lp->grab.pointer
	 * is recognised as stale and qdwin_layer_popup_destroy skips its
	 * end_grab. The listener.link was wl_list_init'd at get_popup time;
	 * re-init defensively in case a prior grab on a different seat left
	 * it attached, then add to the new seat's signal. */
	if (lp->seat_destroy_listener.link.next)
		wl_list_remove(&lp->seat_destroy_listener.link);
	wl_signal_add(&wseat->destroy_signal, &lp->seat_destroy_listener);
	lp->grab_seat = wseat;
	weston_log("qdwin: layer-popup grab started popup=%p seat=%s\n",
		   (void *)popup_resource,
		   wseat->seat_name ? wseat->seat_name : "");
	return true;
}

static void
qdwin_layer_popup_on_commit(struct wl_listener *listener, void *data)
{
	struct qdwin_layer_popup *lp =
		wl_container_of(listener, lp, surface_commit_listener);
	(void)data;
	qdwin_layer_popup_update_position(lp);
}

static void
qdwin_layer_popup_on_surface_destroyed(struct wl_listener *listener,
				       void *data)
{
	struct qdwin_layer_popup *lp =
		wl_container_of(listener, lp, surface_destroy_listener);
	(void)data;
	qdwin_layer_popup_destroy(lp);
}

static void
qdwin_layer_surface_get_popup(struct wl_client *client,
			      struct wl_resource *resource,
			      struct wl_resource *popup)
{
	(void)client;
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;

	qdwin_xdg_popup_attach_layer_parent_fn attach_layer_parent =
		qdwin_xdg_popup_attach_layer_parent_sym();
	if (!attach_layer_parent || !attach_layer_parent(popup, ls->surface)) {
		wl_resource_post_error(resource,
			ZWLR_LAYER_SURFACE_V1_ERROR_INVALID_SURFACE_STATE,
			"get_popup requires patched libweston xdg_popup support");
		return;
	}

	struct weston_desktop_surface *dsurf = wl_resource_get_user_data(popup);
	struct qdwin_layer_popup *lp = calloc(1, sizeof *lp);
	if (!lp) {
		wl_client_post_no_memory(client);
		return;
	}

	lp->parent = ls;
	lp->popup_resource = popup;
	lp->desktop_surface = dsurf;
	lp->surface = weston_desktop_surface_get_surface(dsurf);
	lp->view = weston_desktop_surface_create_view(dsurf);
	if (!lp->view) {
		free(lp);
		wl_client_post_no_memory(client);
		return;
	}

	weston_view_move_to_layer(lp->view,
		&ls->qdwin->layer_shell_layer[ls->layer].view_list);
	qdwin_layer_popup_update_position(lp);

	lp->surface_commit_listener.notify = qdwin_layer_popup_on_commit;
	wl_signal_add(&lp->surface->commit_signal, &lp->surface_commit_listener);
	lp->surface_destroy_listener.notify =
		qdwin_layer_popup_on_surface_destroyed;
	wl_signal_add(&lp->surface->destroy_signal, &lp->surface_destroy_listener);
	/* plan3 H2: also subscribe to xdg_popup resource destruction so a
	 * client that drops the popup role (without destroying the
	 * wl_surface) does not leave a dangling qdwin_layer_popup. */
	lp->popup_resource_destroy_listener.notify =
		qdwin_layer_popup_on_popup_resource_destroyed;
	wl_resource_add_destroy_listener(popup,
					 &lp->popup_resource_destroy_listener);
	wl_list_init(&lp->seat_destroy_listener.link);
	lp->seat_destroy_listener.notify =
		qdwin_layer_popup_on_grab_seat_destroyed;
	wl_list_insert(&ls->popups, &lp->link);

	weston_log("qdwin: layer-shell get_popup ns=%s popup=%p attached\n",
		   ls->namespace ? ls->namespace : "(null)", (void *)popup);
}

/* plan3 H1: walk every tracked layer-surface and its popups for the one
 * owning a given xdg_popup resource. Layer popups are rare and short-
 * lived; a linear scan is cheaper than threading a back-pointer through
 * the libweston-side struct. */
static struct qdwin_layer_popup *
qdwin_layer_popup_for_resource(struct qdwin *qdwin,
			       struct wl_resource *popup_resource)
{
	if (!qdwin || !popup_resource)
		return NULL;
	struct qdwin_layer_surface *ls;
	wl_list_for_each(ls, &qdwin->layer_surfaces, link) {
		struct qdwin_layer_popup *lp;
		wl_list_for_each(lp, &ls->popups, link) {
			if (lp->popup_resource == popup_resource)
				return lp;
		}
	}
	return NULL;
}

static void
qdwin_layer_surface_ack_configure(struct wl_client *client,
				  struct wl_resource *resource, uint32_t serial)
{
	(void)client;
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;
	ls->last_acked_serial = serial;
}

static void
qdwin_layer_surface_destroy_req(struct wl_client *client,
				struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_layer_surface_set_layer(struct wl_client *client,
			      struct wl_resource *resource, uint32_t layer)
{
	(void)client;
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;
	if (layer > ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY) {
		wl_resource_post_error(resource,
			ZWLR_LAYER_SHELL_V1_ERROR_INVALID_LAYER,
			"invalid layer %u", layer);
		return;
	}
	uint32_t old = ls->layer;
	ls->layer = layer;
	/* If the view exists, move it onto the new layer's view_list.
	 * weston_view_move_to_layer handles removal from the old layer. */
	if (ls->view && old != layer) {
		weston_view_move_to_layer(ls->view,
			&ls->qdwin->layer_shell_layer[layer].view_list);
	}
}

static void
qdwin_layer_surface_set_exclusive_edge(struct wl_client *client,
				       struct wl_resource *resource,
				       uint32_t edge)
{
	(void)client;
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;
	uint32_t valid = ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP
		       | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM
		       | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT
		       | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT;
	if (edge != 0 && (edge & ~valid)) {
		wl_resource_post_error(resource,
			ZWLR_LAYER_SURFACE_V1_ERROR_INVALID_EXCLUSIVE_EDGE,
			"invalid exclusive_edge 0x%x", edge);
		return;
	}
	ls->pending.exclusive_edge = edge;
}

static const struct zwlr_layer_surface_v1_interface
qdwin_layer_surface_impl = {
	.set_size                    = qdwin_layer_surface_set_size,
	.set_anchor                  = qdwin_layer_surface_set_anchor,
	.set_exclusive_zone          = qdwin_layer_surface_set_exclusive_zone,
	.set_margin                  = qdwin_layer_surface_set_margin,
	.set_keyboard_interactivity  = qdwin_layer_surface_set_keyboard_interactivity,
	.get_popup                   = qdwin_layer_surface_get_popup,
	.ack_configure               = qdwin_layer_surface_ack_configure,
	.destroy                     = qdwin_layer_surface_destroy_req,
	.set_layer                   = qdwin_layer_surface_set_layer,
	.set_exclusive_edge          = qdwin_layer_surface_set_exclusive_edge,
};

static void
qdwin_layer_surface_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_layer_surface *ls = wl_resource_get_user_data(resource);
	if (!ls) return;
	struct qdwin *qdwin = ls->qdwin;
	int was_reserving = ls->mapped &&
			    ls->current.exclusive_zone > 0;
	if (ls->surface) {
		wl_list_remove(&ls->commit_listener.link);
		wl_list_remove(&ls->surface_destroy_listener.link);
	}
	if (ls->view) {
		if (ls->mapped)
			weston_view_unmap(ls->view);
		weston_view_destroy(ls->view);
		ls->view = NULL;
	}
	struct qdwin_layer_popup *lp, *tmp;
	wl_list_for_each_safe(lp, tmp, &ls->popups, link)
		qdwin_layer_popup_destroy(lp);
	wl_list_remove(&ls->link);
	free(ls->namespace);
	free(ls);
	if (was_reserving)
		qdwin_panels_on_output_change(qdwin);
}

/* --- manager (zwlr_layer_shell_v1) --- */

static void
qdwin_layer_shell_get_layer_surface(struct wl_client *client,
				    struct wl_resource *resource,
				    uint32_t id,
				    struct wl_resource *surface_resource,
				    struct wl_resource *output_resource,
				    uint32_t layer,
				    const char *namespace_str)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct weston_surface *surface =
		wl_resource_get_user_data(surface_resource);

	if (layer > ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY) {
		wl_resource_post_error(resource,
			ZWLR_LAYER_SHELL_V1_ERROR_INVALID_LAYER,
			"invalid layer %u", layer);
		return;
	}
	if (weston_surface_set_role(surface, QDWIN_LAYER_SURFACE_ROLE,
				    resource,
				    ZWLR_LAYER_SHELL_V1_ERROR_ROLE) < 0) {
		/* set_role posts the protocol error itself. */
		return;
	}

	struct qdwin_layer_surface *ls = calloc(1, sizeof *ls);
	if (!ls) {
		wl_client_post_no_memory(client);
		return;
	}
	ls->qdwin     = qdwin;
	ls->surface   = surface;
	ls->layer     = layer;
	ls->namespace = qdwin_xstrdup_or_null(namespace_str);
	ls->pending.exclusive_zone = 0;
	ls->pending.kbd_interactivity =
		ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE;
	wl_list_init(&ls->popups);

	if (output_resource) {
		struct weston_head *head =
			weston_head_from_resource(output_resource);
		ls->output = head ? head->output : NULL;
	}

	ls->resource = wl_resource_create(client,
		&zwlr_layer_surface_v1_interface,
		wl_resource_get_version(resource), id);
	if (!ls->resource) {
		free(ls->namespace);
		free(ls);
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(ls->resource,
		&qdwin_layer_surface_impl, ls,
		qdwin_layer_surface_resource_destroy);

	ls->commit_listener.notify = qdwin_layer_surface_on_commit;
	wl_signal_add(&surface->commit_signal, &ls->commit_listener);
	ls->surface_destroy_listener.notify =
		qdwin_layer_surface_on_surface_destroyed;
	wl_signal_add(&surface->destroy_signal, &ls->surface_destroy_listener);

	wl_list_insert(&qdwin->layer_surfaces, &ls->link);
	weston_log("qdwin: layer-shell get_layer_surface ns=%s layer=%u "
		   "output=%p\n",
		   ls->namespace ? ls->namespace : "(null)",
		   layer, (void *)ls->output);
}

static void
qdwin_layer_shell_destroy(struct wl_client *client,
			  struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct zwlr_layer_shell_v1_interface qdwin_layer_shell_impl = {
	.get_layer_surface = qdwin_layer_shell_get_layer_surface,
	.destroy           = qdwin_layer_shell_destroy,
};

/* Defined later (identity-capture helpers); the optional layer-shell
 * peer allowlist below uses them for the exe/SELinux/starttime checks. */
static char *qdwin_proc_exe(pid_t pid);
static char *qdwin_proc_selinux_label(pid_t pid);
static uint64_t qdwin_proc_starttime(pid_t pid);

static void
bind_qdwin_layer_shell(struct wl_client *client, void *data,
		       uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	pid_t pid; uid_t uid; gid_t gid;
	struct wl_resource *resource;

	/* Gate: only the shell client (or same-uid fallback during early
	 * startup before bind_as_shell) may bind zwlr_layer_shell_v1.
	 * Layer-shell surfaces can take exclusive keyboard focus and occupy
	 * overlay layers — untrusted clients must not have this capability.
	 * See codex-review Finding 1 (HIGH). */
	/* findings F4 (defense in depth): reject sandboxed silo clients outright
	 * so the bind gate holds even if the global filter ever regresses or the
	 * global name leaks; layer-shell is never a silo capability. */
	if (qdwin_secctx_client_find(qdwin, client)) {
		wl_client_post_implementation_error(
			client,
			"zwlr_layer_shell_v1: sandboxed clients may not bind");
		return;
	}
	wl_client_get_credentials(client, &pid, &uid, &gid);

	/* Optional admin allowlist (security review Finding #4). When any of
	 * --qdwin-allowed-layershell-uid/-exe/-label is configured, the peer
	 * must match the configured constraints before we hand out the
	 * layer-shell resource — in addition to the shell-client/allowed_uid
	 * gate below. Default-unset => this whole block is skipped and the
	 * historical broad/test posture is unchanged (no regression).
	 *
	 * Fail-closed: the exe/label helpers map readlink/read failures to ""
	 * so an unreadable /proc entry can never satisfy a non-empty
	 * expectation; a NULL return means OOM and is also treated as
	 * unverifiable. The /proc reads are bracketed by /proc/<pid>/stat
	 * starttime samples and the bind is rejected if the starttime is
	 * unreadable (0) or *changes* across that window, so a pid recycled
	 * to a different process *during* the exe/label reads fails closed.
	 *
	 * This bracketing is NARROW; the verified exe/label are a read-time
	 * snapshot, NOT a stable post-bind identity. It does NOT cover:
	 *   - a recycle that completes *before* the first starttime sample
	 *     (every read, starttime included, sees the impostor consistently);
	 *   - a same-process execve() — starttime is the creation time and is
	 *     unchanged by exec, so a peer can pass the gate then exec another
	 *     binary under the same pid/starttime;
	 *   - a SELinux domain transition (e.g. on exec), which likewise leaves
	 *     starttime unchanged, so the verified label may differ afterwards.
	 * The pid is what the kernel pinned at connect time but /proc is read
	 * now, so these checks are best-effort defence-in-depth, not proof of
	 * peer identity (see doc/protocol.md for the full TOCTOU discussion). */
	if (qdwin->allowed_layershell_uid_set ||
	    qdwin->allowed_layershell_exe ||
	    qdwin->allowed_layershell_label) {
		uint64_t st_before = qdwin_proc_starttime(pid);

		if (qdwin->allowed_layershell_uid_set &&
		    uid != qdwin->allowed_layershell_uid) {
			weston_log("qdwin: layer-shell bind rejected pid=%d "
				   "uid=%u (allowlist uid=%u)\n",
				   (int)pid, (unsigned)uid,
				   (unsigned)qdwin->allowed_layershell_uid);
			wl_client_post_implementation_error(client,
				"zwlr_layer_shell_v1: peer uid not permitted");
			return;
		}

		if (qdwin->allowed_layershell_exe) {
			char *exe = qdwin_proc_exe(pid);
			int ok = (exe &&
				  strcmp(exe, qdwin->allowed_layershell_exe) == 0);
			if (!ok) {
				weston_log("qdwin: layer-shell bind rejected pid=%d "
					   "exe='%s' (expected '%s')\n",
					   (int)pid, exe ? exe : "(unreadable)",
					   qdwin->allowed_layershell_exe);
				wl_client_post_implementation_error(client,
					"zwlr_layer_shell_v1: peer executable not "
					"permitted");
				free(exe);
				return;
			}
			free(exe);
		}

		if (qdwin->allowed_layershell_label) {
			char *label = qdwin_proc_selinux_label(pid);
			int ok = (label &&
				  strcmp(label, qdwin->allowed_layershell_label) == 0);
			if (!ok) {
				weston_log("qdwin: layer-shell bind rejected pid=%d "
					   "label='%s' (expected '%s')\n",
					   (int)pid, label ? label : "(unreadable)",
					   qdwin->allowed_layershell_label);
				wl_client_post_implementation_error(client,
					"zwlr_layer_shell_v1: peer SELinux label "
					"not permitted");
				free(label);
				return;
			}
			free(label);
		}

		uint64_t st_after = qdwin_proc_starttime(pid);
		if (st_before == 0 || st_after == 0 || st_before != st_after) {
			weston_log("qdwin: layer-shell bind rejected pid=%d — "
				   "process identity unstable across /proc read "
				   "(starttime %llu -> %llu)\n",
				   (int)pid,
				   (unsigned long long)st_before,
				   (unsigned long long)st_after);
			wl_client_post_implementation_error(client,
				"zwlr_layer_shell_v1: peer process identity "
				"could not be verified");
			return;
		}
	}

	if (qdwin->shell_bound && qdwin->shell_resource) {
		struct wl_client *shell_client =
			wl_resource_get_client(qdwin->shell_resource);
		/* qdshell opens a second wl_client for its layer-shell
		 * surfaces, so pure client-pointer comparison rejects it.
		 * Fall back to (pid, uid) for same-process identity. */
		if (client != shell_client &&
		    !(pid > 0 && pid == qdwin->shell_pid &&
		      uid == qdwin->shell_uid)) {
			weston_log("qdwin: layer-shell bind REJECTED — "
				   "pid=%d uid=%u is not the shell client\n",
				   (int)pid, (unsigned)uid);
			wl_client_post_implementation_error(
				client,
				"zwlr_layer_shell_v1: only the shell client "
				"may bind this interface");
			return;
		}
	} else {
		/* No shell bound yet; fall back to allowed_uid check. */
		if (!qdwin_layershell_pre_shell_uid_allowed(uid,
							    qdwin->allowed_uid)) {
			weston_log("qdwin: layer-shell bind REJECTED — "
				   "uid=%u not permitted (allowed_uid=%u, "
				   "no shell bound)\n",
				   (unsigned)uid, (unsigned)qdwin->allowed_uid);
			wl_client_post_implementation_error(
				client,
				"zwlr_layer_shell_v1: uid %u not permitted",
				(unsigned)uid);
			return;
		}
	}

	resource = wl_resource_create(
		client, &zwlr_layer_shell_v1_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource, &qdwin_layer_shell_impl,
				       qdwin, NULL);
}

/* --- ext-idle-notify-v1 -------------------------------------------- */

static void
qdwin_idle_notification_destroy(struct wl_client *client,
				struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct ext_idle_notification_v1_interface
qdwin_idle_notification_impl = {
	.destroy = qdwin_idle_notification_destroy,
};

static void
qdwin_idle_notification_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_idle_notification *n = wl_resource_get_user_data(resource);
	if (!n)
		return;
	if (n->timer)
		wl_event_source_remove(n->timer);
	wl_list_remove(&n->link);
	free(n);
}

static int
qdwin_idle_notification_timer_fire(void *data)
{
	struct qdwin_idle_notification *n = data;
	uint64_t now_msec;
	if (n->is_idle)
		return 0;
	if (n->qdwin->idle_internal_mode && n->timeout_ms > 0) {
		now_msec = qdwin_now_msec();
		if (now_msec - n->last_activity_msec < n->timeout_ms) {
			if (n->timer)
				wl_event_source_timer_update(
					n->timer,
					qdwin_idle_internal_next_delay(
						now_msec, n));
			return 0;
		}
	}
	if (!n->ignore_inhibit && n->qdwin->compositor->idle_inhibit > 0) {
		if (n->qdwin->idle_internal_mode && n->timer &&
		    n->timeout_ms > 0)
			wl_event_source_timer_update(
				n->timer,
				(int)QDWIN_IDLE_INTERNAL_INHIBIT_POLL_MS);
		return 0;
	}
	n->is_idle = 1;
	/* In qdwin's internal-idle mode weston's built-in idle timer is
	 * disabled, so the first ordinary (inhibitor-aware) notification timer
	 * is the thing that makes the session idle. Mirror weston's idle_handler
	 * state transition and signal pairing; otherwise later input calls
	 * weston_compositor_wake() while the compositor is still ACTIVE,
	 * libweston suppresses wake_signal, and ext-idle clients never receive
	 * `resumed` (leaving qdshell's display-power-off path stuck black).
	 * input-idle notifications deliberately ignore inhibitors and must not
	 * move the compositor's global idle state. */
	if (n->qdwin->idle_internal_mode &&
	    !n->ignore_inhibit &&
	    n->qdwin->compositor->state == WESTON_COMPOSITOR_ACTIVE) {
		n->qdwin->compositor->state = WESTON_COMPOSITOR_IDLE;
		wl_signal_emit(&n->qdwin->compositor->idle_signal,
			       n->qdwin->compositor);
	}
	ext_idle_notification_v1_send_idled(n->resource);
	return 0;
}

static struct qdwin_idle_notification *
qdwin_idle_notification_create(struct qdwin *qdwin,
			       struct wl_client *client,
			       uint32_t id,
			       uint32_t timeout_ms,
			       int ignore_inhibit,
			       uint32_t version)
{
	struct qdwin_idle_notification *n = calloc(1, sizeof *n);
	struct wl_resource *resource;
	if (!n) {
		wl_client_post_no_memory(client);
		return NULL;
	}
	resource = wl_resource_create(
		client, &ext_idle_notification_v1_interface, version, id);
	if (!resource) {
		free(n);
		wl_client_post_no_memory(client);
		return NULL;
	}
	n->qdwin = qdwin;
	n->resource = resource;
	n->timeout_ms = timeout_ms;
	n->last_activity_msec = qdwin_now_msec();
	n->ignore_inhibit = ignore_inhibit;
	n->timer = wl_event_loop_add_timer(
		wl_display_get_event_loop(qdwin->compositor->wl_display),
		qdwin_idle_notification_timer_fire, n);
	wl_list_insert(&qdwin->idle_notifications, &n->link);
	wl_resource_set_implementation(resource,
				       &qdwin_idle_notification_impl,
				       n,
				       qdwin_idle_notification_resource_destroy);
	/* §6.7(a) follow-up: in internal-idle mode, arm the timer at
	 * creation for the full timeout. wake_signal will rearm it as
	 * activity arrives; timer fire sends `idled`. Outside internal
	 * mode, the timer stays disarmed until idle_signal fires. */
	if (qdwin->idle_internal_mode && n->timer && n->timeout_ms > 0)
		wl_event_source_timer_update(
			n->timer,
			qdwin_idle_internal_next_delay(n->last_activity_msec, n));
	return n;
}

static void
qdwin_idle_notifier_destroy(struct wl_client *client,
			    struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_idle_notifier_get_idle_notification(struct wl_client *client,
					  struct wl_resource *resource,
					  uint32_t id,
					  uint32_t timeout,
					  struct wl_resource *seat)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)seat;
	qdwin_idle_notification_create(qdwin, client, id, timeout,
				       0 /* honour inhibit */,
				       wl_resource_get_version(resource));
}

static void
qdwin_idle_notifier_get_input_idle_notification(struct wl_client *client,
						struct wl_resource *resource,
						uint32_t id,
						uint32_t timeout,
						struct wl_resource *seat)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)seat;
	qdwin_idle_notification_create(qdwin, client, id, timeout,
				       1 /* ignore inhibit */,
				       wl_resource_get_version(resource));
}

static const struct ext_idle_notifier_v1_interface
qdwin_idle_notifier_impl = {
	.destroy                       = qdwin_idle_notifier_destroy,
	.get_idle_notification         = qdwin_idle_notifier_get_idle_notification,
	.get_input_idle_notification   = qdwin_idle_notifier_get_input_idle_notification,
};

static void
bind_qdwin_idle_notifier(struct wl_client *client, void *data,
			 uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource = wl_resource_create(
		client, &ext_idle_notifier_v1_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource, &qdwin_idle_notifier_impl,
				       qdwin, NULL);
}

static void
qdwin_on_idle_signal(struct wl_listener *listener, void *data)
{
	struct qdwin *qdwin =
		wl_container_of(listener, qdwin, idle_signal_listener);
	struct qdwin_idle_notification *n;
	uint32_t weston_idle_ms;
	(void)data;
	/* In internal-idle mode qdwin emitted idle_signal only to pair the
	 * compositor state transition with a later wake_signal. The individual
	 * notification timers are already armed from create/wake and must not be
	 * re-armed from this signal; doing so would delay sibling notifications. */
	if (qdwin->idle_internal_mode)
		return;
	/* weston idle_time is configured in seconds. Outside internal-idle mode,
	 * weston's built-in idle timer fired this signal and notifications with
	 * longer client timeouts are armed for the remaining offset. */
	weston_idle_ms = (uint32_t)qdwin->compositor->idle_time * 1000u;
	wl_list_for_each(n, &qdwin->idle_notifications, link) {
		if (n->is_idle)
			continue;
		if (!n->ignore_inhibit && qdwin->compositor->idle_inhibit > 0)
			continue;
		if (n->timeout_ms <= weston_idle_ms) {
			/* Client asked for ≤ weston idle — fire now. */
			n->is_idle = 1;
			ext_idle_notification_v1_send_idled(n->resource);
		} else if (n->timer) {
			/* Client asked for longer than weston idle — arm a
			 * secondary timer so we fire at approx the right
			 * offset. Timer is disarmed on wake_signal below. */
			wl_event_source_timer_update(
				n->timer,
				(int)(n->timeout_ms - weston_idle_ms));
		}
	}
}

static void
qdwin_on_wake_signal(struct wl_listener *listener, void *data)
{
	struct qdwin *qdwin =
		wl_container_of(listener, qdwin, wake_signal_listener);
	struct qdwin_idle_notification *n;
	uint64_t now_msec = qdwin_now_msec();
	(void)data;
	wl_list_for_each(n, &qdwin->idle_notifications, link) {
		n->last_activity_msec = now_msec;
		/* §6.7(a) follow-up: in internal-idle mode, rearm the timer
		 * to the full timeout so the notification fires exactly
		 * timeout_ms after this activity. Outside internal mode,
		 * disarm — idle_signal will rearm as needed. */
		if (n->timer) {
			if (qdwin->idle_internal_mode && n->timeout_ms > 0)
				wl_event_source_timer_update(
					n->timer,
					qdwin_idle_internal_next_delay(
						now_msec, n));
			else
				wl_event_source_timer_update(n->timer, 0);
		}
		if (!n->is_idle)
			continue;
		n->is_idle = 0;
		ext_idle_notification_v1_send_resumed(n->resource);
	}
}

/* ------------------------------------------------------------------
 * §6.7 cursor-shape-v1.
 *
 * §6.7(b) lands libXcursor theme loading + per-shape image cache +
 * validation + informative logging. When a client calls set_shape,
 * the compositor looks up the preloaded XcursorImages, logs the
 * selected image dimensions + hotspot, and validates that a theme
 * image was actually available.
 *
 * What is still deferred: installing the loaded image as the
 * pointer's sprite (weston_pointer::sprite). Investigation notes
 * from §6.7(b) follow-up (2026-04-24):
 *
 *   - weston_buffer_create_solid_rgba() is the only public API that
 *     creates a server-side weston_buffer_reference; it takes a
 *     solid color, not an arbitrary pixel array. Good for a
 *     proof-of-plumbing but not for theme sprites.
 *   - weston_buffer_from_resource(ec, wl_resource) requires a valid
 *     wl_buffer resource, which only exists if a client has uploaded
 *     one. That's the crux: to feed the compositor pixel data, some
 *     wl_client has to hold a wl_shm_pool, create wl_buffer, and
 *     call wl_surface.attach + commit on a surface it owns. That
 *     client doesn't exist today.
 *   - The canonical fix is an internal client: wl_client_create(
 *     display, sv[0]) where sv[] is a socketpair, wait for it to
 *     roundtrip via a worker thread, then have the client side
 *     (running the wayland-client library in a thread) bind wl_shm,
 *     create pool from an anonymous shm fd pre-filled with the
 *     XcursorImage pixels, upload wl_buffer, commit, send a tiny
 *     "cursor ready" sentinel, which the server side picks up and
 *     assigns to pointer->sprite.
 *   - The per-pointer serial tracking in set_shape(serial, shape)
 *     also requires knowing which seat the cursor-shape device is
 *     bound to — today we ignore pointer_resource in get_pointer,
 *     which would need to change to resolve the weston_pointer.
 *
 * This is ~1 week of bounded work (socketpair plumbing, thread
 * lifecycle, shm buffer management, pointer resolution). It is not
 * on the §6.6 critical path — qdshell can set_cursor from its own
 * wl_surfaces directly via standard wl_pointer.set_cursor. Filed as
 * a separate follow-up; for now, set_shape logs the validated image
 * dimensions + hotspot so the data flow is observable.
 *
 * §6.6 follow-up research finding (2026-04-25):
 *
 *   Surveyed libweston-16 public headers for a server-side set-sprite
 *   API. weston_surface_create(compositor) exists, but does NOT
 *   create a weston_buffer — weston_buffer_create_solid_rgba only
 *   produces solid colors, and weston_buffer_from_resource requires
 *   an uploaded wl_resource. Neither supports raw pixel upload from
 *   server-owned XcursorImage data.
 *
 *   Pre-condition for the full impl:
 *     (a) libweston patch adding weston_buffer_create_from_shm(pixels,
 *         stride, format, w, h) OR
 *     (b) internal wl_client (socketpair + wayland-client worker thread
 *         binding wl_shm + wl_compositor, doing the upload, and
 *         forwarding the resulting wl_buffer resource back over a
 *         control socket), OR
 *     (c) weston_pointer::sprite is a private field inside
 *         libweston.c — there is no public setter. Even if we could
 *         create the buffer, we'd need to patch libweston to expose
 *         weston_pointer_set_sprite(pointer, view, hotspot_x, hotspot_y).
 *
 *   Conclusion: full sprite install requires either an upstream
 *   libweston patch or the internal-client plumbing. qdshell's
 *   decoration chrome already paints via internal wl_client threads
 *   (qdshell → qdwin over /run/user/1000/wayland-1), so (b) is
 *   precedented but lives on the client side, not inside qdwin. A
 *   libweston patch ((a)+(c)) is cleaner; scoped as post-Phase-6.
 *
 * Mapping of cursor-shape-v1 enum → Xcursor name follows CSS naming
 * (the protocol's enum names ARE the CSS cursor names); modern
 * themes (Adwaita, DMZ, etc.) expose them directly. XcursorLibrary-
 * LoadImages falls through the theme's inheritance chain so legacy
 * ICCCM-style names (xterm, fleur, etc.) are reachable by alias
 * without us mapping them explicitly.
 * ------------------------------------------------------------------ */

static const char *
qdwin_cursor_shape_name(uint32_t shape)
{
	switch (shape) {
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_DEFAULT:      return "default";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_CONTEXT_MENU: return "context-menu";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_HELP:         return "help";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_POINTER:      return "pointer";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_PROGRESS:     return "progress";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_WAIT:         return "wait";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_CELL:         return "cell";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_CROSSHAIR:    return "crosshair";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_TEXT:         return "text";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_VERTICAL_TEXT:return "vertical-text";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALIAS:        return "alias";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_COPY:         return "copy";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_MOVE:         return "move";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_NO_DROP:      return "no-drop";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_NOT_ALLOWED:  return "not-allowed";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_GRAB:         return "grab";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_GRABBING:     return "grabbing";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_E_RESIZE:     return "e-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_N_RESIZE:     return "n-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_NE_RESIZE:    return "ne-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_NW_RESIZE:    return "nw-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_S_RESIZE:     return "s-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_SE_RESIZE:    return "se-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_SW_RESIZE:    return "sw-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_W_RESIZE:     return "w-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_EW_RESIZE:    return "ew-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_NS_RESIZE:    return "ns-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_NESW_RESIZE:  return "nesw-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_NWSE_RESIZE:  return "nwse-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_COL_RESIZE:   return "col-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ROW_RESIZE:   return "row-resize";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALL_SCROLL:   return "all-scroll";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ZOOM_IN:      return "zoom-in";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ZOOM_OUT:     return "zoom-out";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_DND_ASK:      return "dnd-ask";
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALL_RESIZE:   return "all-resize";
	default:                                            return "other";
	}
}

/* Load one image from the theme for every known shape, keyed by enum.
 * Missing theme entries leave the slot NULL; set_shape logs a miss. */
static void
qdwin_cursor_theme_load(struct qdwin *qdwin)
{
	const char *env_theme = getenv("XCURSOR_THEME");
	const char *env_size  = getenv("XCURSOR_SIZE");
	int size = env_size ? atoi(env_size) : 0;
	if (size <= 0)
		size = 24;
	qdwin->cursor_size = size;
	qdwin->cursor_theme_name = qdwin_xstrdup_or_null(env_theme);

	unsigned loaded = 0;
	for (uint32_t s = 1; s <= WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALL_RESIZE; s++) {
		const char *name = qdwin_cursor_shape_name(s);
		if (!name || !strcmp(name, "other"))
			continue;
		qdwin->cursor_images[s] =
			XcursorLibraryLoadImages(name,
						 qdwin->cursor_theme_name,
						 size);
		if (qdwin->cursor_images[s])
			loaded++;
	}
	weston_log("qdwin: cursor-shape theme=%s size=%d loaded=%u/%d\n",
		   qdwin->cursor_theme_name ? qdwin->cursor_theme_name
					    : "(default)",
		   size, loaded,
		   WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALL_RESIZE);
}

static void
qdwin_cursor_theme_destroy(struct qdwin *qdwin)
{
	for (size_t i = 0;
	     i < sizeof qdwin->cursor_images / sizeof qdwin->cursor_images[0];
	     i++) {
		if (qdwin->cursor_images[i]) {
			XcursorImagesDestroy(qdwin->cursor_images[i]);
			qdwin->cursor_images[i] = NULL;
		}
	}
	for (size_t i = 0;
	     i < sizeof qdwin->cursor_sprites /
		 sizeof qdwin->cursor_sprites[0];
	     i++) {
		if (qdwin->cursor_sprites[i].surface) {
			wl_list_remove(
				&qdwin->cursor_sprites[i].destroy_listener.link);
			if (qdwin->cursor_sprites[i].commit_listener.link.next)
				wl_list_remove(
					&qdwin->cursor_sprites[i].commit_listener.link);
			qdwin->cursor_sprites[i].surface = NULL;
		}
	}
	free(qdwin->cursor_theme_name);
	qdwin->cursor_theme_name = NULL;
}

/* §6.6 follow-up 2026-04-25: sprite install via solid-color proxy.
 *
 * Gated on QDWIN_CURSOR_SPRITE_SOLID=1 and `qdwin_cursor_solid_enabled`
 * below. Uses public libweston-16 API (weston_buffer_create_solid_rgba
 * + weston_surface_attach_solid + weston_view_create + direct
 * assignment to weston_pointer::sprite, which IS a public field in the
 * version-14 header contrary to the earlier §6.7(b) finding). Per-
 * shape solid colour makes the visible cursor shape-distinguishable
 * without the theme-image upload path, which still requires an
 * internal wl_client worker thread (tracked as the "full theme"
 * follow-up in the parking notes above).
 *
 * Lifecycle: one solid sprite surface per cursor-shape device, torn
 * down on device destroy or re-created on each set_shape. The
 * sprite_destroy_listener in weston_pointer is wired so libweston
 * clears pointer->sprite when our view is destroyed (important: we
 * initialise wl_list on the listener's link slot before sending).
 */
struct qdwin_cursor_shape_device {
	struct qdwin *qdwin;
	struct weston_pointer *pointer; /* resolved at get_pointer */
	struct wl_listener pointer_destroy_listener;
	struct weston_surface *sprite_surface;
	struct weston_view *sprite_view;
	struct weston_buffer_reference *sprite_buffer_ref;
	uint32_t current_shape;
};

/* Forward decls — definitions live further down (theme path is below
 * the cursor_shape_device_impl table because it needs the device struct,
 * but set_shape needs to call into them). */
static void
qdwin_cursor_device_install_theme_sprite(struct qdwin_cursor_shape_device *dev,
					 uint32_t shape);
static void
qdwin_cursor_device_install_solid_sprite(struct qdwin_cursor_shape_device *dev,
					 uint32_t shape);

static bool
qdwin_cursor_solid_enabled(void)
{
	const char *v = getenv("QDWIN_CURSOR_SPRITE_SOLID");
	return v && v[0] && v[0] != '0';
}

static void
qdwin_pointer_cursor_surface_committed(struct weston_surface *surface,
				       struct weston_coord_surface new_origin)
{
	struct weston_pointer *pointer = surface->committed_private;
	struct weston_coord_surface hotspot_inv;

	if (surface->width == 0)
		return;
	if (!pointer || !pointer->sprite ||
	    pointer->sprite->surface != surface) {
		weston_log("qdwin: cursor-sprite commit ignored for stale "
			   "surface\n");
		return;
	}

	pointer->hotspot = weston_coord_surface_sub(pointer->hotspot,
						    new_origin);
	hotspot_inv = weston_coord_surface_invert(pointer->hotspot);
	weston_view_set_position_with_offset(pointer->sprite,
					     pointer->pos, hotspot_inv);

	pixman_region32_clear(&surface->pending.input);
	pixman_region32_clear(&surface->input);

	if (!weston_surface_is_mapped(surface)) {
		weston_surface_map(surface);
		weston_view_move_to_layer(pointer->sprite,
					  &surface->compositor->
					  cursor_layer.view_list);
	}
}

static void
qdwin_pointer_unmap_sprite(struct weston_pointer *pointer)
{
	struct weston_surface *surface;

	if (!pointer || !pointer->sprite)
		return;

	surface = pointer->sprite->surface;
	if (surface) {
		if (weston_surface_is_mapped(surface))
			weston_surface_unmap(surface);
		if (surface->committed_private == pointer) {
			surface->committed = NULL;
			surface->committed_private = NULL;
			/* J29: 14->16 set_label_func (callback form) removed;
			 * clear the label to the static default. */
			weston_surface_set_label_static(surface, NULL);
		}
	}
	if (pointer->sprite_destroy_listener.link.next)
		wl_list_remove(&pointer->sprite_destroy_listener.link);
	wl_list_init(&pointer->sprite_destroy_listener.link);
	weston_view_destroy(pointer->sprite);
	pointer->sprite = NULL;
}

static void
qdwin_cursor_shape_colour(uint32_t shape, float *r, float *g, float *b,
			  float *a)
{
	*a = 0.9f;
	/* Default: near-white so a "default" arrow stands out. */
	*r = 0.95f; *g = 0.95f; *b = 0.95f;
	switch (shape) {
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_TEXT:
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_VERTICAL_TEXT:
		*r = 0.15f; *g = 0.15f; *b = 0.15f; return;
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_POINTER:
		*r = 0.30f; *g = 0.85f; *b = 0.30f; return;
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_WAIT:
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_PROGRESS:
		*r = 0.95f; *g = 0.85f; *b = 0.20f; return;
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_NOT_ALLOWED:
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_NO_DROP:
		*r = 0.90f; *g = 0.25f; *b = 0.25f; return;
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_GRAB:
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_GRABBING:
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_MOVE:
		*r = 0.30f; *g = 0.50f; *b = 0.90f; return;
	case WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_HELP:
		*r = 0.70f; *g = 0.50f; *b = 0.95f; return;
	default:
		return;
	}
}

static void
qdwin_cursor_sprite_tear_down(struct qdwin_cursor_shape_device *dev)
{
	if (dev->sprite_view) {
		if (dev->pointer && dev->pointer->sprite == dev->sprite_view) {
			qdwin_pointer_unmap_sprite(dev->pointer);
		}
		/* If another cursor-shape device replaced pointer->sprite,
		 * qdwin_pointer_unmap_sprite() already destroyed this view.
		 * Do not dereference or destroy a non-current cached pointer. */
		dev->sprite_view = NULL;
	}
	if (dev->sprite_buffer_ref) {
		weston_buffer_destroy_solid(dev->sprite_buffer_ref);
		dev->sprite_buffer_ref = NULL;
	}
	if (dev->sprite_surface) {
		weston_surface_unref(dev->sprite_surface);
		dev->sprite_surface = NULL;
	}
}

static void
qdwin_cursor_device_install_solid_sprite(struct qdwin_cursor_shape_device *dev,
					 uint32_t shape)
{
	if (!dev->pointer) {
		weston_log("qdwin: cursor-shape solid install skipped "
			   "(no pointer resolved)\n");
		return;
	}
	qdwin_cursor_sprite_tear_down(dev);

	float r, g, b, a;
	qdwin_cursor_shape_colour(shape, &r, &g, &b, &a);

	const int size = 24;
	struct weston_surface *surface =
		/* J29: 14->16 added client arg; NULL = compositor-internal
		 * cursor sprite (no owning wl_client), matching shell-utils. */
		weston_surface_create(dev->qdwin->compositor, NULL);
	if (!surface)
		return;
	struct weston_buffer_reference *ref =
		weston_buffer_create_solid_rgba(dev->qdwin->compositor,
						r, g, b, a);
	if (!ref) {
		weston_surface_unref(surface);
		return;
	}
	weston_surface_attach_solid(surface, ref, size, size);
	struct weston_view *view = weston_view_create(surface);
	if (!view) {
		weston_buffer_destroy_solid(ref);
		weston_surface_unref(surface);
		return;
	}
	dev->sprite_surface = surface;
	dev->sprite_buffer_ref = ref;
	dev->sprite_view = view;
	dev->current_shape = shape;

	/* Hotspot at the top-left for a simple coloured cursor; most
	 * cursor shapes from CSS point into their upper-left quadrant. */
	qdwin_pointer_unmap_sprite(dev->pointer);
	dev->pointer->hotspot = weston_coord_surface(0, 0, surface);
	dev->pointer->sprite = view;
	wl_signal_add(&surface->destroy_signal,
		      &dev->pointer->sprite_destroy_listener);
	surface->committed = qdwin_pointer_cursor_surface_committed;
	surface->committed_private = dev->pointer;

	weston_compositor_schedule_repaint(dev->qdwin->compositor);
	weston_log("qdwin: cursor-shape sprite installed "
		   "(shape=%s, solid rgba=%.2f,%.2f,%.2f,%.2f, %dx%d)\n",
		   qdwin_cursor_shape_name(shape), r, g, b, a, size, size);
}

static void
qdwin_cursor_device_pointer_destroyed(struct wl_listener *listener,
				      void *data)
{
	struct qdwin_cursor_shape_device *dev =
		wl_container_of(listener, dev, pointer_destroy_listener);
	(void)data;
	qdwin_cursor_sprite_tear_down(dev);
	dev->pointer = NULL;
}

static void
qdwin_cursor_shape_device_destroy(struct wl_client *client,
				  struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_cursor_shape_device_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_cursor_shape_device *dev =
		wl_resource_get_user_data(resource);
	if (!dev)
		return;
	qdwin_cursor_sprite_tear_down(dev);
	if (dev->pointer) {
		wl_list_remove(&dev->pointer_destroy_listener.link);
		dev->pointer = NULL;
	}
	free(dev);
}

static void
qdwin_cursor_shape_device_set_shape(struct wl_client *client,
				    struct wl_resource *resource,
				    uint32_t serial,
				    uint32_t shape)
{
	struct qdwin_cursor_shape_device *dev =
		wl_resource_get_user_data(resource);
	struct qdwin *qdwin = dev ? dev->qdwin : NULL;
	(void)client;
	(void)serial;
	if (shape < 1 ||
	    shape > WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALL_RESIZE) {
		wl_resource_post_error(resource,
			WP_CURSOR_SHAPE_DEVICE_V1_ERROR_INVALID_SHAPE,
			"invalid cursor shape %u", shape);
		return;
	}
	XcursorImages *imgs = qdwin ? qdwin->cursor_images[shape] : NULL;
	if (imgs && imgs->nimage > 0) {
		XcursorImage *img = imgs->images[0];
		weston_log("qdwin: cursor-shape set_shape=%s dims=%ux%u "
			   "hotspot=%u,%u nframes=%d (sprite=%s)\n",
			   qdwin_cursor_shape_name(shape),
			   img->width, img->height,
			   img->xhot, img->yhot, imgs->nimage,
			   qdwin_cursor_solid_enabled()
				? "installing-solid"
				: "deferred");
	} else {
		weston_log("qdwin: cursor-shape set_shape=%s "
			   "(theme miss; sprite=%s)\n",
			   qdwin_cursor_shape_name(shape),
			   qdwin_cursor_solid_enabled()
				? "installing-solid"
				: "deferred");
	}

	if (!dev)
		return;
	/* §6.8 cursor-sprite full theme: prefer a shell-registered sprite
	 * if present; fall back to solid colour (gated by
	 * QDWIN_CURSOR_SPRITE_SOLID); else leave whatever the previous
	 * sprite was. */
	if (qdwin && shape < sizeof qdwin->cursor_sprites /
			      sizeof qdwin->cursor_sprites[0] &&
	    qdwin->cursor_sprites[shape].surface) {
		qdwin_cursor_device_install_theme_sprite(dev, shape);
	} else if (qdwin_cursor_solid_enabled()) {
		qdwin_cursor_device_install_solid_sprite(dev, shape);
	}
}

static const struct wp_cursor_shape_device_v1_interface
qdwin_cursor_shape_device_impl = {
	.destroy   = qdwin_cursor_shape_device_destroy,
	.set_shape = qdwin_cursor_shape_device_set_shape,
};

/* §6.8 cursor-sprite full theme: shell-provided per-shape wl_surface
 * applied on set_shape. The shell (or a helper it spawns) loads
 * libXcursor itself, paints each shape into a wl_shm-backed
 * wl_surface, and registers it via qdwin_shell_v1.set_cursor_sprite.
 * The compositor caches {shape -> weston_surface, hotspot}; on
 * set_shape we install that surface + hotspot on the active pointer.
 * Falls back to the solid-colour path (QDWIN_CURSOR_SPRITE_SOLID) or
 * a no-op if neither is present. */

/* plan3 M2: per-commit hook that re-asserts the cursor input-region
 * invariant. Weston's libweston/input.c:pointer_cursor_surface_committed
 * clears these on every commit; qdwin's install path only clears them
 * once. The sprite helper is allowed to re-commit (theme change, scale,
 * new frame) and any non-empty pending.input it ships would otherwise
 * promote the sprite back to a pickable surface above shell UI. The
 * post-commit log line is the discriminator for
 * tests/host/test_cursor_sprite_input_invariant.py. */
static void
qdwin_cursor_sprite_on_commit(struct wl_listener *listener, void *data)
{
	struct qdwin_cursor_sprite *slot =
		wl_container_of(listener, slot, commit_listener);
	struct weston_surface *s = slot->surface;
	(void)data;
	if (!s)
		return;
	int had_pending = pixman_region32_not_empty(&s->pending.input);
	int had_current = pixman_region32_not_empty(&s->input);
	if (had_pending)
		pixman_region32_clear(&s->pending.input);
	if (had_current)
		pixman_region32_clear(&s->input);
	if (had_pending || had_current) {
		weston_log("qdwin: cursor-sprite commit re-cleared input "
			   "shape=%s pending=%d current=%d\n",
			   qdwin_cursor_shape_name(slot->shape),
			   had_pending, had_current);
	}
}

static void
qdwin_cursor_sprite_clear_slot(struct qdwin *qdwin, uint32_t shape)
{
	if (shape >= sizeof qdwin->cursor_sprites /
		      sizeof qdwin->cursor_sprites[0])
		return;
	struct qdwin_cursor_sprite *slot = &qdwin->cursor_sprites[shape];
	if (slot->surface) {
		wl_list_remove(&slot->destroy_listener.link);
		if (slot->commit_listener.link.next)
			wl_list_remove(&slot->commit_listener.link);
		slot->surface = NULL;
	}
	slot->hotspot_x = 0;
	slot->hotspot_y = 0;
}

static void
qdwin_cursor_sprite_on_surface_destroyed(struct wl_listener *listener,
					 void *data)
{
	struct qdwin_cursor_sprite *slot =
		wl_container_of(listener, slot, destroy_listener);
	(void)data;
	weston_log("qdwin: cursor-sprite cleared shape=%s "
		   "(surface destroyed)\n",
		   qdwin_cursor_shape_name(slot->shape));
	/* Don't call qdwin_cursor_sprite_clear_slot — it does
	 * wl_list_remove on the destroy_listener.link, but libwayland
	 * already removed it before invoking us. Just reset state. */
	if (slot->commit_listener.link.next)
		wl_list_remove(&slot->commit_listener.link);
	slot->surface = NULL;
	slot->hotspot_x = 0;
	slot->hotspot_y = 0;
	wl_list_init(&slot->destroy_listener.link);
	wl_list_init(&slot->commit_listener.link);
}

/* B6 / task(134): install a cached cursor sprite as pointer->sprite,
 * mirroring weston's `pointer_set_cursor` (libweston/input.c). The
 * minimal-but-correct steps are:
 *
 *   1. Surface needs role "wl_pointer-cursor" — without it weston
 *      treats the surface as un-mapped-in-disguise.
 *   2. Surface needs to be mapped — or weston skips it on composition.
 *   3. View needs to live in compositor->cursor_layer.view_list — or
 *      no layer iteration ever picks it up; the renderer / DRM cursor
 *      plane both walk that specific layer.
 *
 * Used by both `qdwin_install_default_cursor_on_pointer` (no-client-
 * owns-cursor fallback) and `qdwin_cursor_device_install_theme_sprite`
 * (wp_cursor_shape_v1.set_shape from clients). Returns NULL on failure,
 * the freshly-installed view on success. The caller owns positioning
 * + scheduling repaint; this helper does both at the end so the cursor
 * is immediately visible. */
static struct weston_view *
qdwin_install_cursor_sprite_view(struct qdwin *qdwin,
				 struct weston_pointer *pointer,
				 struct weston_surface *surface,
				 int32_t hotspot_x, int32_t hotspot_y,
				 const char *log_prefix)
{
	const char *role = weston_surface_get_role(surface);
	if (!role) {
		if (weston_surface_set_role(surface, "wl_pointer-cursor",
					    NULL, 0) < 0) {
			weston_log("qdwin: %s: set_role failed\n", log_prefix);
			return NULL;
		}
	} else if (strcmp(role, "wl_pointer-cursor") != 0) {
		weston_log("qdwin: %s: surface has role=%s, skipping\n",
			   log_prefix, role);
		return NULL;
	}

	struct weston_view *view = weston_view_create(surface);
	if (!view) {
		weston_log("qdwin: %s: view create failed\n", log_prefix);
		return NULL;
	}
	qdwin_pointer_unmap_sprite(pointer);
	pointer->hotspot = weston_coord_surface(hotspot_x, hotspot_y, surface);
	pointer->sprite = view;
	wl_signal_add(&surface->destroy_signal,
		      &pointer->sprite_destroy_listener);
	surface->committed = qdwin_pointer_cursor_surface_committed;
	surface->committed_private = pointer;

	struct weston_coord_surface hotspot_inv =
		weston_coord_surface_invert(pointer->hotspot);
	weston_view_set_position_with_offset(view, pointer->pos, hotspot_inv);

	if (!weston_surface_is_mapped(surface))
		weston_surface_map(surface);
	pixman_region32_clear(&surface->pending.input);
	pixman_region32_clear(&surface->input);
	weston_view_move_to_layer(view,
				  &qdwin->compositor->cursor_layer.view_list);

	weston_compositor_schedule_repaint(qdwin->compositor);

	/* Blank-BO guard (verification aid): a sprite whose buffer is empty
	 * or fully transparent maps onto the cursor_layer and reaches the hw
	 * DRM plane exactly like a real one — the plane is active and the BO
	 * is "allocated", but the cursor is invisible. The DRM-plane checks
	 * and this "mapped on cursor_layer" log alone cannot tell the two
	 * apart. So, for the introspectable case (the live path: wl_shm
	 * ARGB8888 sprites from qdistro-cursor-sprites), count the
	 * non-transparent pixels and surface it in the log so the GUI smoke
	 * test can fail a blank/invisible cursor deterministically. Non-SHM
	 * buffers aren't readable here and fall back to the DRM-plane signal. */
	struct weston_buffer *cbuf = surface->buffer_ref.buffer;
	if (!cbuf) {
		weston_log("qdwin: %s: mapped on cursor_layer (hotspot=%d,%d) "
			   "payload=none\n", log_prefix, hotspot_x, hotspot_y);
	} else if (cbuf->type == WESTON_BUFFER_SHM && cbuf->shm_buffer) {
		struct wl_shm_buffer *shm = cbuf->shm_buffer;
		uint32_t fmt = wl_shm_buffer_get_format(shm);
		int32_t pw = wl_shm_buffer_get_width(shm);
		int32_t ph = wl_shm_buffer_get_height(shm);
		int32_t pstride = wl_shm_buffer_get_stride(shm);
		long opaque = -1;
		/* Hostile-dimensions guard: wl_shm only validates
		 * offset+stride*height <= pool_size, NOT stride >= width*4.
		 * A client can attach stride < width*4, which would make the
		 * per-row scan below read past the pool. Require a sane stride
		 * (and positive dims); otherwise leave opaque=-1
		 * (non-introspectable) and rely on the DRM-plane signal. */
		bool sane = pw > 0 && ph > 0 && pstride >= (int64_t)pw * 4;
		if (fmt == WL_SHM_FORMAT_XRGB8888 && sane) {
			/* no alpha channel — every pixel is opaque */
			opaque = (long)pw * ph;
		} else if (fmt == WL_SHM_FORMAT_ARGB8888 && sane) {
			wl_shm_buffer_begin_access(shm);
			const uint8_t *d = wl_shm_buffer_get_data(shm);
			long n = 0;
			if (d) {
				for (int32_t y = 0; y < ph; y++) {
					const uint8_t *row = d + (size_t)y * pstride;
					for (int32_t x = 0; x < pw; x++)
						if (row[(size_t)x * 4 + 3] != 0)
							n++;
				}
			}
			wl_shm_buffer_end_access(shm);
			opaque = n;
		}
		weston_log("qdwin: %s: mapped on cursor_layer (hotspot=%d,%d) "
			   "payload=%dx%d nonzero_alpha=%ld\n",
			   log_prefix, hotspot_x, hotspot_y, pw, ph, opaque);
	} else {
		weston_log("qdwin: %s: mapped on cursor_layer (hotspot=%d,%d) "
			   "payload=non-shm\n", log_prefix, hotspot_x, hotspot_y);
	}
	return view;
}

/* B6: install the cached "default" sprite as pointer->sprite when no
 * client owns the cursor (over desktop, panel, or any non-Wayland
 * surface; also during the early boot window before any client maps). */
static void
qdwin_install_default_cursor_on_pointer(struct qdwin *qdwin,
					struct weston_pointer *pointer)
{
	if (!qdwin || !pointer)
		return;
	struct qdwin_cursor_sprite *slot =
		&qdwin->cursor_sprites[WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_DEFAULT];
	if (!slot->surface) {
		/* Log once per qdwin lifetime — fires on every pointer
		 * focus change otherwise (1000+ identical lines per
		 * session under apps that move focus a lot). */
		if (!qdwin->cursor_default_warned) {
			qdwin->cursor_default_warned = 1;
			weston_log("qdwin: install_default_cursor: no surface "
				   "yet (helper not started?) — further "
				   "occurrences suppressed\n");
		}
		return;     /* helper hasn't registered the default yet */
	}
	qdwin_install_cursor_sprite_view(qdwin, pointer, slot->surface,
					 slot->hotspot_x, slot->hotspot_y,
					 "install_default_cursor");
}

/* Restore the default cursor when pointer focus leaves a client
 * surface. Without this, the last client's cursor sticks even after
 * the pointer is over the bare desktop. */
static void
qdwin_default_cursor_on_focus_changed(struct wl_listener *listener, void *data)
{
	struct weston_pointer *pointer = data;
	(void)listener;
	if (pointer->focus)
		return;     /* focus on a client → that client owns cursor */
	if (pointer->sprite)
		return;     /* something else already set a sprite */
	if (qdwin_singleton)
		qdwin_install_default_cursor_on_pointer(qdwin_singleton, pointer);
}

/* B6 fix: timer callback to retry cursor installation when pointer
 * wasn't ready when cursor-sprites helper ran. */
static struct wl_event_source *cursor_retry_timer;

static int
qdwin_cursor_retry_timer_cb(void *data)
{
	(void)data;
	/* Backup: try to install cursor if pointer is now available */
	struct qdwin *qdwin = qdwin_singleton;
	if (qdwin) {
		struct weston_seat *seat;
		wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
			struct weston_pointer *p = weston_seat_get_pointer(seat);
			if (p && !p->sprite)
				qdwin_install_default_cursor_on_pointer(qdwin, p);
		}
	}
	cursor_retry_timer = NULL;
	return 0;
}

static int
qdwin_cursor_retry_install(struct qdwin *qdwin)
{
	struct wl_event_loop *loop;

	if (cursor_retry_timer)
		return 0;  /* already scheduled */

	loop = wl_display_get_event_loop(qdwin->compositor->wl_display);
	cursor_retry_timer = wl_event_loop_add_timer(loop, qdwin_cursor_retry_timer_cb, qdwin);
	if (!cursor_retry_timer) {
		weston_log("qdwin: cursor_retry: failed to add timer\n");
		return -1;
	}
	weston_log("qdwin: cursor_retry_install: scheduling first retry in 1000ms\n");
	wl_event_source_timer_update(cursor_retry_timer, 1000);
	return 0;
}

static void
qdwin_handle_set_cursor_sprite(struct wl_client *client,
			       struct wl_resource *resource,
			       uint32_t shape,
			       struct wl_resource *surface_resource,
			       int32_t hotspot_x,
			       int32_t hotspot_y)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	pid_t pid;
	uid_t uid;
	gid_t gid;
	char *peer_exe;
	bool allowed;

	wl_client_get_credentials(client, &pid, &uid, &gid);
	peer_exe = qdwin_proc_exe(pid);
	allowed = qdwin_client_is_bound_shell(qdwin, client) ||
		  qdwin_cursor_sprite_peer_allowed(peer_exe);
	free(peer_exe);
	if (!allowed) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_NOT_BOUND,
				       "set_cursor_sprite: not the bound shell "
				       "or cursor-sprites helper");
		return;
	}

	if (shape < 1 ||
	    shape > WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_ALL_RESIZE) {
		wl_resource_post_error(resource,
			WP_CURSOR_SHAPE_DEVICE_V1_ERROR_INVALID_SHAPE,
			"set_cursor_sprite: invalid shape %u", shape);
		return;
	}

	qdwin_cursor_sprite_clear_slot(qdwin, shape);

	if (!surface_resource) {
		weston_log("qdwin: cursor-sprite cleared shape=%s\n",
			   qdwin_cursor_shape_name(shape));
		return;
	}

	struct weston_surface *ws =
		wl_resource_get_user_data(surface_resource);
	if (!ws) {
		weston_log("qdwin: cursor-sprite shape=%s — surface "
			   "resource invalid\n",
			   qdwin_cursor_shape_name(shape));
		return;
	}

	struct qdwin_cursor_sprite *slot = &qdwin->cursor_sprites[shape];
	slot->surface = ws;
	slot->hotspot_x = hotspot_x;
	slot->hotspot_y = hotspot_y;
	slot->qdwin = qdwin;
	slot->shape = shape;
	slot->destroy_listener.notify =
		qdwin_cursor_sprite_on_surface_destroyed;
	wl_signal_add(&ws->destroy_signal, &slot->destroy_listener);

	/* plan3 M2: stay aligned with weston cursor-surface invariant on
	 * every commit, not just at install. The first install also clears
	 * the regions via qdwin_install_cursor_sprite_view; this listener
	 * catches subsequent re-commits from the helper. */
	slot->commit_listener.notify = qdwin_cursor_sprite_on_commit;
	wl_signal_add(&ws->commit_signal, &slot->commit_listener);
	/* Also clear once on register so a sprite whose creating client
	 * already attached a non-empty input region cannot leak before
	 * its first commit. */
	pixman_region32_clear(&ws->pending.input);
	pixman_region32_clear(&ws->input);

	weston_log("qdwin: cursor-sprite registered shape=%s hotspot=%d,%d\n",
		   qdwin_cursor_shape_name(shape), hotspot_x, hotspot_y);

	/* B6 fix: when the "default" sprite registers, attach it to every
	 * existing pointer that has no client-set sprite. Without this,
	 * the cursor stays invisible until a client (foot, etc.) takes
	 * pointer focus and calls wp_cursor_shape_v1.set_shape — over an
	 * empty desktop or non-Wayland-aware area there's nothing to set
	 * the cursor and SPICE has no cursor data to send to the viewer.
	 *
	 * Also installs on pointer focus → NULL transitions via the
	 * focus_signal listener wired up in qdwin_install_default_cursor_*.
	 */
	if (shape == WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_DEFAULT) {
		struct weston_seat *seat;
		int any_pointer = 0;
		wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
			struct weston_pointer *p = weston_seat_get_pointer(seat);
			if (p) {
				any_pointer = 1;
				if (!p->sprite)
					qdwin_install_default_cursor_on_pointer(qdwin, p);
			}
		}
		/* B6: if no pointer existed yet, schedule a retry as backup */
		if (!any_pointer)
			qdwin_cursor_retry_install(qdwin);
	}
}

static void
qdwin_cursor_device_install_theme_sprite(struct qdwin_cursor_shape_device *dev,
					 uint32_t shape)
{
	struct qdwin *qdwin = dev->qdwin;
	if (!dev->pointer)
		return;
	if (shape >= sizeof qdwin->cursor_sprites /
		     sizeof qdwin->cursor_sprites[0])
		return;
	struct qdwin_cursor_sprite *slot = &qdwin->cursor_sprites[shape];
	if (!slot->surface)
		return;

	qdwin_cursor_sprite_tear_down(dev);

	char log_prefix[64];
	snprintf(log_prefix, sizeof log_prefix,
		 "cursor-shape install shape=%s",
		 qdwin_cursor_shape_name(shape));
	struct weston_view *view = qdwin_install_cursor_sprite_view(
		qdwin, dev->pointer, slot->surface,
		slot->hotspot_x, slot->hotspot_y, log_prefix);
	if (!view)
		return;
	dev->sprite_view = view;
	dev->current_shape = shape;
}

static void
qdwin_cursor_shape_manager_destroy(struct wl_client *client,
				   struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static struct weston_pointer *
qdwin_resolve_first_pointer(struct qdwin *qdwin)
{
	/* Single-seat single-pointer is the only shape qdwin supports
	 * today (RDP backend creates one seat per client; we pin to the
	 * first seat's pointer for cursor-shape sprite install). */
	struct weston_seat *seat;
	wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
		struct weston_pointer *p = weston_seat_get_pointer(seat);
		if (p)
			return p;
	}
	return NULL;
}

static void
qdwin_cursor_shape_manager_get_pointer(struct wl_client *client,
				       struct wl_resource *resource,
				       uint32_t id,
				       struct wl_resource *pointer_resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct wl_resource *dev_res;
	(void)pointer_resource;
	dev_res = wl_resource_create(client,
				     &wp_cursor_shape_device_v1_interface,
				     wl_resource_get_version(resource), id);
	if (!dev_res) {
		wl_client_post_no_memory(client);
		return;
	}
	struct qdwin_cursor_shape_device *dev = calloc(1, sizeof *dev);
	if (!dev) {
		wl_resource_destroy(dev_res);
		wl_client_post_no_memory(client);
		return;
	}
	dev->qdwin = qdwin;
	dev->pointer = qdwin_resolve_first_pointer(qdwin);
	if (dev->pointer) {
		dev->pointer_destroy_listener.notify =
			qdwin_cursor_device_pointer_destroyed;
		wl_signal_add(&dev->pointer->destroy_signal,
			      &dev->pointer_destroy_listener);
	} else {
		wl_list_init(&dev->pointer_destroy_listener.link);
	}
	wl_resource_set_implementation(dev_res,
				       &qdwin_cursor_shape_device_impl,
				       dev,
				       qdwin_cursor_shape_device_resource_destroy);
}

static void
qdwin_cursor_shape_manager_get_tablet_tool_v2(struct wl_client *client,
					      struct wl_resource *resource,
					      uint32_t id,
					      struct wl_resource *tool_resource)
{
	qdwin_cursor_shape_manager_get_pointer(client, resource, id,
					       tool_resource);
}

static const struct wp_cursor_shape_manager_v1_interface
qdwin_cursor_shape_manager_impl = {
	.destroy            = qdwin_cursor_shape_manager_destroy,
	.get_pointer        = qdwin_cursor_shape_manager_get_pointer,
	.get_tablet_tool_v2 = qdwin_cursor_shape_manager_get_tablet_tool_v2,
};

static void
bind_qdwin_cursor_shape_manager(struct wl_client *client, void *data,
				uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource = wl_resource_create(
		client, &wp_cursor_shape_manager_v1_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource,
				       &qdwin_cursor_shape_manager_impl,
				       qdwin, NULL);
}

/* ------------------------------------------------------------------
 * §6.7 wp_fractional_scale_v1.
 *
 * Each get_fractional_scale creates a wp_fractional_scale_v1 tied to
 * a specific wl_surface. The compositor sends `preferred_scale(N)`
 * with N/120 giving the recommended buffer scale for that surface's
 * current output.
 *
 * §6.7(c): we track the resource + backing surface in
 * qdwin::fractional_scales and recompute preferred_scale whenever
 * outputs change (output_created / output_resized) by looking at
 * the first available output's integer scale. When weston later
 * gains true fractional-scale output configuration, this function
 * is the single place to upgrade. A per-output inherited scale
 * (following the surface's primary output assignment) is the
 * planned follow-up — requires wiring weston_surface::output into
 * the compute path and a surface-output-changed listener.
 *
 * Protocol encoding: scale unit is 120. 120 = 1.0×, 180 = 1.5×,
 * 240 = 2.0×. If no output is present (headless, very early init)
 * we conservatively report 120.
 * ------------------------------------------------------------------ */

struct qdwin_fractional_scale {
	struct qdwin *qdwin;
	struct wl_resource *resource;      /* wp_fractional_scale_v1 */
	struct weston_surface *surface;    /* may go NULL if destroyed */
	struct wl_listener surface_destroy_listener;
	struct wl_listener surface_commit_listener;
	uint32_t last_sent_scale;          /* 0 = nothing sent yet */
	struct wl_list link;               /* qdwin::fractional_scales */
};

static uint32_t
qdwin_compute_preferred_scale_for_surface(struct qdwin *qdwin,
					  struct weston_surface *surface)
{
	/* §6.7(c) follow-up: per-surface output selection. If the surface
	 * has a primary output (weston_surface::output, set internally by
	 * weston_view_assign_output), prefer its current_scale. Otherwise
	 * fall back to the first output, or 120 when headless.
	 *
	 * Env override QDWIN_FRACTIONAL_SCALE=N forces a specific value
	 * for testing non-integer scales (150 = 1.25×). */
	const char *env = getenv("QDWIN_FRACTIONAL_SCALE");
	if (env && *env) {
		long n = strtol(env, NULL, 10);
		/* Accept-or-fall-through gate (30..960) lives in qdwin-logic.c. */
		if (qdwin_fractional_scale_env_valid(n))
			return (uint32_t)n;
	}
	if (surface && surface->output && surface->output->current_scale > 0)
		return (uint32_t)surface->output->current_scale * 120u;
	struct weston_output *out;
	wl_list_for_each(out, &qdwin->compositor->output_list, link) {
		if (out->current_scale > 0)
			return (uint32_t)out->current_scale * 120u;
		break;
	}
	return 120;
}

static void
qdwin_fractional_scale_push(struct qdwin_fractional_scale *fs)
{
	uint32_t scale =
		qdwin_compute_preferred_scale_for_surface(fs->qdwin, fs->surface);
	if (fs->last_sent_scale == scale)
		return;
	fs->last_sent_scale = scale;
	wp_fractional_scale_v1_send_preferred_scale(fs->resource, scale);
}

static void
qdwin_fractional_scale_broadcast(struct qdwin *qdwin)
{
	struct qdwin_fractional_scale *fs;
	wl_list_for_each(fs, &qdwin->fractional_scales, link)
		qdwin_fractional_scale_push(fs);
}

static void
qdwin_fractional_scale_surface_commit(struct wl_listener *l, void *data)
{
	struct qdwin_fractional_scale *fs =
		wl_container_of(l, fs, surface_commit_listener);
	(void)data;
	/* weston_view_assign_output runs on commit and may flip
	 * surface->output. Re-evaluate the preferred scale. */
	qdwin_fractional_scale_push(fs);
}

static void
qdwin_fractional_scale_surface_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_fractional_scale *fs =
		wl_container_of(l, fs, surface_destroy_listener);
	(void)data;
	wl_list_remove(&fs->surface_destroy_listener.link);
	wl_list_init(&fs->surface_destroy_listener.link);
	wl_list_remove(&fs->surface_commit_listener.link);
	wl_list_init(&fs->surface_commit_listener.link);
	fs->surface = NULL;
}

static void
qdwin_fractional_scale_destroy(struct wl_client *client,
			       struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_fractional_scale_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_fractional_scale *fs = wl_resource_get_user_data(resource);
	if (!fs)
		return;
	if (fs->surface) {
		wl_list_remove(&fs->surface_destroy_listener.link);
		wl_list_remove(&fs->surface_commit_listener.link);
	}
	wl_list_remove(&fs->link);
	free(fs);
}

static const struct wp_fractional_scale_v1_interface
qdwin_fractional_scale_impl = {
	.destroy = qdwin_fractional_scale_destroy,
};

static void
qdwin_fractional_scale_manager_destroy(struct wl_client *client,
				       struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_fractional_scale_manager_get(struct wl_client *client,
				   struct wl_resource *resource,
				   uint32_t id,
				   struct wl_resource *surface_resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct weston_surface *surface =
		surface_resource ? wl_resource_get_user_data(surface_resource)
				 : NULL;
	struct qdwin_fractional_scale *fs = calloc(1, sizeof *fs);
	struct wl_resource *fs_res;
	uint32_t scale;
	if (!fs) {
		wl_client_post_no_memory(client);
		return;
	}
	fs_res = wl_resource_create(client,
				    &wp_fractional_scale_v1_interface,
				    wl_resource_get_version(resource), id);
	if (!fs_res) {
		free(fs);
		wl_client_post_no_memory(client);
		return;
	}
	fs->qdwin = qdwin;
	fs->resource = fs_res;
	fs->surface = surface;
	wl_list_init(&fs->surface_destroy_listener.link);
	wl_list_init(&fs->surface_commit_listener.link);
	if (surface) {
		fs->surface_destroy_listener.notify =
			qdwin_fractional_scale_surface_destroyed;
		wl_signal_add(&surface->destroy_signal,
			      &fs->surface_destroy_listener);
		fs->surface_commit_listener.notify =
			qdwin_fractional_scale_surface_commit;
		wl_signal_add(&surface->commit_signal,
			      &fs->surface_commit_listener);
	}
	wl_list_insert(&qdwin->fractional_scales, &fs->link);
	wl_resource_set_implementation(fs_res, &qdwin_fractional_scale_impl,
				       fs,
				       qdwin_fractional_scale_resource_destroy);
	scale = qdwin_compute_preferred_scale_for_surface(qdwin, surface);
	fs->last_sent_scale = scale;
	wp_fractional_scale_v1_send_preferred_scale(fs_res, scale);
	(void)scale;
}

static const struct wp_fractional_scale_manager_v1_interface
qdwin_fractional_scale_manager_impl = {
	.destroy              = qdwin_fractional_scale_manager_destroy,
	.get_fractional_scale = qdwin_fractional_scale_manager_get,
};

static void
bind_qdwin_fractional_scale_manager(struct wl_client *client, void *data,
				    uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource = wl_resource_create(
		client, &wp_fractional_scale_manager_v1_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource,
				       &qdwin_fractional_scale_manager_impl,
				       qdwin, NULL);
}

/* ------------------------------------------------------------------
 * Bucket A / P1: text-input-unstable-v3 (zwp_text_input_manager_v3).
 *
 * App-facing IME text-input plane. Toolkits (GTK/Qt/Chromium) bind this
 * directly to learn when a text field can receive input-method commits.
 * qdwin advertises it OPEN (every app needs it) at interface version 1.
 *
 * Scope today = FOUNDATION ONLY: the compositor drives enter/leave off the
 * keyboard focus it already tracks and accepts the double-buffered
 * enable/disable + set_* state requests (stored, otherwise no-ops), but it
 * never emits preedit_string / commit_string / done — there is no
 * input-method-v2 IME wired in yet (the input-method side is gated on the
 * IME-identity decision, see todo/issues/qdwin/app-compat-protocol-gaps.md
 * P1). A bound client therefore correctly sees focus enter/leave but
 * receives no composed text until an IME exists — the standard
 * "text-input advertised, no IME running" state. Safe: per-seat,
 * focus-gated, no cross-client surface; qdwin's peer-uid plugin gate
 * already limits clients to a single uid.
 * ------------------------------------------------------------------ */

struct qdwin_text_input {
	struct qdwin *qdwin;
	struct wl_resource *resource;     /* zwp_text_input_v3 */
	struct weston_seat *seat;         /* seat passed to get_text_input */
	struct wl_client *client;
	/* Double-buffered enable state (enable/disable are pending until
	 * commit). We don't act on the content, but tracking it keeps a
	 * future input-method wiring a drop-in. */
	int pending_enabled;
	int current_enabled;
	uint32_t commit_count;            /* serial echoed in zwp_text_input_v3.done */
	/* App-set, double-buffered content the IME needs (applied on commit,
	 * forwarded to a bound input-method via activate/surrounding_text/
	 * content_type). The *_set flags distinguish "field never supplied"
	 * (don't forward — IME treats as unsupported) from "supplied as empty". */
	char *pending_surrounding, *current_surrounding;
	uint32_t pending_sur_cursor, pending_sur_anchor;
	uint32_t current_sur_cursor, current_sur_anchor;
	int pending_sur_set, current_sur_set;
	uint32_t pending_hint, pending_purpose, current_hint, current_purpose;
	int pending_ct_set, current_ct_set;
	uint32_t pending_change_cause, current_change_cause;
	int pending_cc_set, current_cc_set;
	/* Surface this text_input currently holds enter on (NULL = none).
	 * Tracked so we send exactly one leave per enter and never touch a
	 * destroyed surface: the destroy listener clears it WITHOUT a leave
	 * (the wl_surface resource is already gone by then). */
	struct weston_surface *entered;
	struct wl_listener entered_destroy_listener;
	struct wl_list link;              /* qdwin::text_inputs */
};

static void
qdwin_text_input_clear_entered(struct qdwin_text_input *ti)
{
	if (!ti->entered)
		return;
	wl_list_remove(&ti->entered_destroy_listener.link);
	wl_list_init(&ti->entered_destroy_listener.link);
	ti->entered = NULL;
}

static void
qdwin_text_input_entered_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_text_input *ti =
		wl_container_of(l, ti, entered_destroy_listener);
	(void)data;
	/* The focused surface is going away; its wl_surface resource is no
	 * longer usable, so we cannot (and per spec need not) send leave —
	 * just drop the tracking. The follow-up focus_signal settles the
	 * new focus. */
	qdwin_text_input_clear_entered(ti);
}

static void
qdwin_text_input_set_entered(struct qdwin_text_input *ti,
			     struct weston_surface *surface)
{
	ti->entered = surface;
	ti->entered_destroy_listener.notify =
		qdwin_text_input_entered_destroyed;
	wl_signal_add(&surface->destroy_signal,
		      &ti->entered_destroy_listener);
}

/* Recompute enter/leave for every text_input on `seat` after a keyboard
 * focus change. Defined here; forward-declared near the focus listener. */
static void
qdwin_text_input_update_focus(struct qdwin *qdwin, struct weston_seat *seat)
{
	struct weston_keyboard *kbd =
		seat ? weston_seat_get_keyboard(seat) : NULL;
	struct weston_surface *focus = kbd ? kbd->focus : NULL;
	struct qdwin_text_input *ti;

	wl_list_for_each(ti, &qdwin->text_inputs, link) {
		struct weston_surface *want = NULL;
		if (ti->seat != seat)
			continue;
		/* A text_input only enters a surface owned by its OWN client
		 * — the wl_surface resource we pass in enter must belong to
		 * that client. */
		if (focus && focus->resource &&
		    wl_resource_get_client(focus->resource) == ti->client)
			want = focus;
		if (want == ti->entered)
			continue;
		if (ti->entered) {
			/* Still alive (the destroy listener would have cleared
			 * it otherwise) → safe to send leave with its
			 * resource. */
			zwp_text_input_v3_send_leave(ti->resource,
						     ti->entered->resource);
			qdwin_text_input_clear_entered(ti);
		}
		if (want) {
			zwp_text_input_v3_send_enter(ti->resource,
						     want->resource);
			qdwin_text_input_set_entered(ti, want);
		}
	}

	/* Focus moved → the seat's IME may need to (de)activate for the newly
	 * focused, enabled text_input. */
	qdwin_im_sync_seat(qdwin, seat);
}

/* ---- zwp_text_input_v3 requests ---- */

static void
qdwin_text_input_destroy(struct wl_client *client, struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_text_input_enable(struct wl_client *client, struct wl_resource *resource)
{
	struct qdwin_text_input *ti = wl_resource_get_user_data(resource);
	(void)client;
	if (ti)
		ti->pending_enabled = 1;
}

static void
qdwin_text_input_disable(struct wl_client *client, struct wl_resource *resource)
{
	struct qdwin_text_input *ti = wl_resource_get_user_data(resource);
	(void)client;
	if (ti)
		ti->pending_enabled = 0;
}

static void
qdwin_text_input_set_surrounding_text(struct wl_client *client,
				      struct wl_resource *resource,
				      const char *text,
				      int32_t cursor, int32_t anchor)
{
	struct qdwin_text_input *ti = wl_resource_get_user_data(resource);
	(void)client;
	if (!ti)
		return;
	/* Double-buffered: pending until the next zwp_text_input_v3.commit,
	 * then forwarded to a bound IME as zwp_input_method_v2.surrounding_text.
	 * strdup failure leaves the field unset (forwarded as unsupported)
	 * rather than aborting the client. */
	free(ti->pending_surrounding);
	ti->pending_surrounding = qdwin_xstrdup_or_null(text);
	ti->pending_sur_cursor = (uint32_t)cursor;
	ti->pending_sur_anchor = (uint32_t)anchor;
	ti->pending_sur_set = 1;
}

static void
qdwin_text_input_set_text_change_cause(struct wl_client *client,
				       struct wl_resource *resource,
				       uint32_t cause)
{
	struct qdwin_text_input *ti = wl_resource_get_user_data(resource);
	(void)client;
	if (!ti)
		return;
	ti->pending_change_cause = cause;
	ti->pending_cc_set = 1;
}

static void
qdwin_text_input_set_content_type(struct wl_client *client,
				  struct wl_resource *resource,
				  uint32_t hint, uint32_t purpose)
{
	struct qdwin_text_input *ti = wl_resource_get_user_data(resource);
	(void)client;
	if (!ti)
		return;
	ti->pending_hint = hint;
	ti->pending_purpose = purpose;
	ti->pending_ct_set = 1;
}

static void
qdwin_text_input_set_cursor_rectangle(struct wl_client *client,
				      struct wl_resource *resource,
				      int32_t x, int32_t y,
				      int32_t width, int32_t height)
{
	(void)client; (void)resource; (void)x; (void)y;
	(void)width; (void)height;
	/* Stored for input-method popup placement once popup surfaces gain a
	 * real position; the cursor rectangle carries no IME state that needs
	 * forwarding for composition itself, so it is accepted as a no-op. */
}

static void
qdwin_text_input_commit(struct wl_client *client, struct wl_resource *resource)
{
	struct qdwin_text_input *ti = wl_resource_get_user_data(resource);
	int was_enabled;
	(void)client;
	if (!ti)
		return;
	/* Atomically apply the buffered enable + content state and bump the
	 * serial the spec requires us to echo in `done`. */
	was_enabled = ti->current_enabled;
	ti->current_enabled = ti->pending_enabled;
	ti->commit_count++;

	/* Apply the double-buffered content the IME consumes. surrounding_text
	 * ownership transfers from pending to current. */
	if (ti->pending_sur_set) {
		free(ti->current_surrounding);
		ti->current_surrounding = ti->pending_surrounding;
		ti->pending_surrounding = NULL;
		ti->current_sur_cursor = ti->pending_sur_cursor;
		ti->current_sur_anchor = ti->pending_sur_anchor;
		ti->current_sur_set = 1;
		ti->pending_sur_set = 0;
	}
	if (ti->pending_ct_set) {
		ti->current_hint = ti->pending_hint;
		ti->current_purpose = ti->pending_purpose;
		ti->current_ct_set = 1;
		ti->pending_ct_set = 0;
	}
	if (ti->pending_cc_set) {
		ti->current_change_cause = ti->pending_change_cause;
		ti->current_cc_set = 1;
		ti->pending_cc_set = 0;
	}

	/* Reconcile the seat's IME. An enable/disable edge changes which
	 * text_input (if any) the IME serves; a commit while already enabled is
	 * a content refresh. qdwin_im_sync_seat handles both: it (de)activates
	 * on the edge and re-pushes current state to the active text_input. */
	(void)was_enabled;
	if (ti->qdwin && ti->seat)
		qdwin_im_sync_seat(ti->qdwin, ti->seat);
}

static const struct zwp_text_input_v3_interface qdwin_text_input_impl = {
	.destroy               = qdwin_text_input_destroy,
	.enable                = qdwin_text_input_enable,
	.disable               = qdwin_text_input_disable,
	.set_surrounding_text  = qdwin_text_input_set_surrounding_text,
	.set_text_change_cause = qdwin_text_input_set_text_change_cause,
	.set_content_type      = qdwin_text_input_set_content_type,
	.set_cursor_rectangle  = qdwin_text_input_set_cursor_rectangle,
	.commit                = qdwin_text_input_commit,
};

static void
qdwin_text_input_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_text_input *ti = wl_resource_get_user_data(resource);
	if (!ti)
		return;
	/* If a bound IME was serving this text_input, deactivate it before the
	 * backing object disappears (clears the IME's active_ti back-pointer). */
	if (ti->qdwin)
		qdwin_im_text_input_gone(ti->qdwin, ti);
	qdwin_text_input_clear_entered(ti);
	wl_list_remove(&ti->link);
	free(ti->pending_surrounding);
	free(ti->current_surrounding);
	free(ti);
}

/* Compositor-teardown drain (called from qdwin_destroy before free(qdwin)).
 * A client's zwp_text_input_v3 resource can outlive the shell plugin; if it
 * does, its destroy callback would later run wl_list_remove against the freed
 * qdwin->text_inputs list head. Neutralize each resource's user_data (the
 * destroy callback no-ops on NULL) and unlink+free now, so teardown is safe
 * regardless of libwayland's client/resource destroy ordering. Mirrors
 * qdwin_secctx_destroy_all's "detach the resource too" handling. */
static void
qdwin_text_inputs_destroy_all(struct qdwin *qdwin)
{
	struct qdwin_text_input *ti, *tmp;
	wl_list_for_each_safe(ti, tmp, &qdwin->text_inputs, link) {
		wl_resource_set_user_data(ti->resource, NULL);
		qdwin_text_input_clear_entered(ti);
		wl_list_remove(&ti->link);
		free(ti->pending_surrounding);
		free(ti->current_surrounding);
		free(ti);
	}
}

/* ---- zwp_text_input_manager_v3 ---- */

/* One per live manager resource. The back-pointer to qdwin is neutralized to
 * NULL at compositor teardown (qdwin_text_input_managers_destroy_all) because
 * a manager resource can outlive the shell plugin — without this a late
 * get_text_input would dereference a freed qdwin. */
struct qdwin_text_input_manager {
	struct qdwin *qdwin;          /* NULL after teardown neutralization */
	struct wl_resource *resource;
	struct wl_list link;          /* qdwin::text_input_managers */
};

static void
qdwin_text_input_manager_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_text_input_manager *mgr =
		wl_resource_get_user_data(resource);
	if (!mgr)
		return;
	wl_list_remove(&mgr->link);
	free(mgr);
}

static void
qdwin_text_input_managers_destroy_all(struct qdwin *qdwin)
{
	struct qdwin_text_input_manager *mgr, *tmp;
	wl_list_for_each_safe(mgr, tmp, &qdwin->text_input_managers, link) {
		wl_resource_set_user_data(mgr->resource, NULL);
		wl_list_remove(&mgr->link);
		free(mgr);
	}
}

static void
qdwin_text_input_manager_destroy(struct wl_client *client,
				 struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_text_input_manager_get_text_input(struct wl_client *client,
					struct wl_resource *resource,
					uint32_t id,
					struct wl_resource *seat_resource)
{
	struct qdwin_text_input_manager *mgr =
		wl_resource_get_user_data(resource);
	struct qdwin *qdwin = mgr ? mgr->qdwin : NULL;
	struct weston_seat *seat =
		seat_resource ? wl_resource_get_user_data(seat_resource) : NULL;
	struct qdwin_text_input *ti;
	struct wl_resource *ti_res;

	if (!qdwin) {
		/* Manager outlived qdwin (teardown). Honour the new_id with an
		 * inert object (NULL user_data → every handler no-ops) rather
		 * than dereferencing freed state or desyncing the client's id
		 * allocation. */
		ti_res = wl_resource_create(client, &zwp_text_input_v3_interface,
					    wl_resource_get_version(resource), id);
		if (ti_res)
			wl_resource_set_implementation(ti_res,
				&qdwin_text_input_impl, NULL, NULL);
		return;
	}

	ti = calloc(1, sizeof *ti);
	if (!ti) {
		wl_client_post_no_memory(client);
		return;
	}
	ti_res = wl_resource_create(client, &zwp_text_input_v3_interface,
				    wl_resource_get_version(resource), id);
	if (!ti_res) {
		free(ti);
		wl_client_post_no_memory(client);
		return;
	}
	ti->qdwin = qdwin;
	ti->resource = ti_res;
	ti->seat = seat;
	ti->client = client;
	wl_list_init(&ti->entered_destroy_listener.link);
	wl_list_insert(&qdwin->text_inputs, &ti->link);
	wl_resource_set_implementation(ti_res, &qdwin_text_input_impl, ti,
				       qdwin_text_input_resource_destroy);
	/* If the seat's keyboard already focuses one of this client's
	 * surfaces, deliver the initial enter right away. */
	qdwin_text_input_update_focus(qdwin, seat);
}

static const struct zwp_text_input_manager_v3_interface
qdwin_text_input_manager_impl = {
	.destroy        = qdwin_text_input_manager_destroy,
	.get_text_input = qdwin_text_input_manager_get_text_input,
};

static void
bind_qdwin_text_input_manager(struct wl_client *client, void *data,
			      uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct qdwin_text_input_manager *mgr;
	struct wl_resource *resource;

	mgr = calloc(1, sizeof *mgr);
	if (!mgr) {
		wl_client_post_no_memory(client);
		return;
	}
	resource = wl_resource_create(
		client, &zwp_text_input_manager_v3_interface, version, id);
	if (!resource) {
		free(mgr);
		wl_client_post_no_memory(client);
		return;
	}
	mgr->qdwin = qdwin;
	mgr->resource = resource;
	wl_list_insert(&qdwin->text_input_managers, &mgr->link);
	wl_resource_set_implementation(
		resource, &qdwin_text_input_manager_impl, mgr,
		qdwin_text_input_manager_resource_destroy);
}

/* ==================================================================
 * Bucket A / P1: input-method-unstable-v2 (zwp_input_method_manager_v2).
 *
 * The privileged IME side of the text-input plane. A session-trusted input
 * method (fcitx5/ibus) binds the manager, calls get_input_method(seat) to get
 * one zwp_input_method_v2 per seat, optionally grab_keyboard()s the seat's
 * hardware keyboard to receive raw keys, and pushes preedit/commit/delete back
 * which the compositor forwards into the focused, enabled zwp_text_input_v3.
 *
 * Trust: unlike the OPEN text-input-v3 plane, this grants keystroke capture +
 * arbitrary text injection across the focused client, so it is identity-gated
 * on two layers: (1) hidden from sandboxed/secctx clients by the global filter
 * (qdwin_secctx_global_filter) so a silo client cannot even see it — this is
 * STRONGER than the locker global, which is bind-gated only (visible to all
 * clients, bind rejected unless authorized; it is NOT in the global filter);
 * and (2) bind-gated to allowed_ime_uid (default allowed_uid) with optional
 * exe/label pins, and limited to one ACTIVE input method per seat (a second
 * get_input_method gets only `unavailable`, per the protocol). A grabbing IME
 * also needs zwp_virtual_keyboard_manager_v1 to pass non-composed keys back to
 * apps; that companion protocol IS implemented (registered in wet_shell_init)
 * and gated IDENTICALLY — hidden by the same global filter and bind-gated via
 * the shared qdwin_ime_family_bind_allowed() helper. The default (no IME bound)
 * still keeps text-input-v3 inert and the keyboard untouched.
 * ================================================================== */

struct qdwin_input_method;

/* Per-seat keyboard grab held by an IME. weston_keyboard_grab MUST be the first
 * member so the grab interface callbacks can recover us by pointer identity. */
struct qdwin_im_keyboard_grab {
	struct weston_keyboard_grab base;   /* must be first */
	struct qdwin_input_method *im;      /* NULL once the IME detaches */
	struct wl_resource *resource;       /* zwp_input_method_keyboard_grab_v2 */
	struct weston_keyboard *keyboard;   /* NULL if grab was cancelled by wl */
};

struct qdwin_input_method {
	struct qdwin *qdwin;
	struct wl_resource *resource;       /* zwp_input_method_v2 */
	struct weston_seat *seat;           /* NULL after seat destroy */
	struct wl_client *client;
	int inert;                          /* second-per-seat: only `unavailable` */
	int active;                         /* currently activated for active_ti */
	struct qdwin_text_input *active_ti; /* text_input being served (NULL=none) */
	uint32_t done_count;                /* serial: # of zwp_input_method_v2.done */
	/* Pending IME->app state, applied on zwp_input_method_v2.commit. */
	char *pending_preedit;
	int32_t pending_preedit_cb, pending_preedit_ce;
	char *pending_commit;
	uint32_t pending_del_before, pending_del_after;
	struct qdwin_im_keyboard_grab *grab; /* active keyboard grab, or NULL */
	struct wl_listener seat_destroy_listener;
	struct wl_list link;                /* qdwin::input_methods */
};

/* Reset the per-commit buffered IME->app state to its protocol-initial values. */
static void
qdwin_im_reset_pending(struct qdwin_input_method *im)
{
	free(im->pending_preedit);
	im->pending_preedit = NULL;
	im->pending_preedit_cb = 0;
	im->pending_preedit_ce = 0;
	free(im->pending_commit);
	im->pending_commit = NULL;
	im->pending_del_before = 0;
	im->pending_del_after = 0;
}

/* Find the non-inert IME bound for `seat` (at most one). */
static struct qdwin_input_method *
qdwin_im_for_seat(struct qdwin *qdwin, struct weston_seat *seat)
{
	struct qdwin_input_method *im;
	if (!seat)
		return NULL;
	wl_list_for_each(im, &qdwin->input_methods, link) {
		if (!im->inert && im->seat == seat)
			return im;
	}
	return NULL;
}

/* The focused, enabled text_input on `seat` (the IME's activation target), or
 * NULL. enter-tracking already singles out the focused client's text_input. */
static struct qdwin_text_input *
qdwin_im_active_candidate(struct qdwin *qdwin, struct weston_seat *seat)
{
	struct qdwin_text_input *ti;
	wl_list_for_each(ti, &qdwin->text_inputs, link) {
		if (ti->seat == seat && ti->entered && ti->current_enabled)
			return ti;
	}
	return NULL;
}

/* Push the active text_input's current content to the IME, framed by a `done`.
 * Caller guarantees im->active && im->active_ti. Per spec, surrounding_text /
 * content_type are only sent if the text_input actually supplied them. */
static void
qdwin_im_send_state(struct qdwin_input_method *im)
{
	struct qdwin_text_input *ti = im->active_ti;
	if (ti->current_sur_set)
		zwp_input_method_v2_send_surrounding_text(
			im->resource,
			ti->current_surrounding ? ti->current_surrounding : "",
			ti->current_sur_cursor, ti->current_sur_anchor);
	if (ti->current_cc_set)
		zwp_input_method_v2_send_text_change_cause(
			im->resource, ti->current_change_cause);
	if (ti->current_ct_set)
		zwp_input_method_v2_send_content_type(
			im->resource, ti->current_hint, ti->current_purpose);
	zwp_input_method_v2_send_done(im->resource);
	im->done_count++;
}

static void
qdwin_im_deactivate(struct qdwin_input_method *im)
{
	if (!im->active)
		return;
	zwp_input_method_v2_send_deactivate(im->resource);
	zwp_input_method_v2_send_done(im->resource);
	im->done_count++;
	im->active = 0;
	im->active_ti = NULL;
	/* A fresh activation must start from clean buffered state. */
	qdwin_im_reset_pending(im);
}

static void
qdwin_im_activate(struct qdwin_input_method *im, struct qdwin_text_input *ti)
{
	im->active = 1;
	im->active_ti = ti;
	qdwin_im_reset_pending(im);
	zwp_input_method_v2_send_activate(im->resource);
	qdwin_im_send_state(im);
}

/* Reconcile the seat's IME with the current focus/enable state. Called after a
 * focus change or a text_input enable/disable/content commit. */
static void
qdwin_im_sync_seat(struct qdwin *qdwin, struct weston_seat *seat)
{
	struct qdwin_input_method *im = qdwin_im_for_seat(qdwin, seat);
	struct qdwin_text_input *ti;
	if (!im)
		return;
	ti = qdwin_im_active_candidate(qdwin, seat);
	if (ti != im->active_ti) {
		if (im->active)
			qdwin_im_deactivate(im);
		if (ti)
			qdwin_im_activate(im, ti);
	} else if (ti && im->active) {
		/* Same target, still active → a content refresh (the app
		 * committed new surrounding text / content type). */
		qdwin_im_send_state(im);
	}
}

/* A text_input is being destroyed; if it was an IME's active target, deactivate
 * so no stale back-pointer survives. */
static void
qdwin_im_text_input_gone(struct qdwin *qdwin, struct qdwin_text_input *ti)
{
	struct qdwin_input_method *im;
	wl_list_for_each(im, &qdwin->input_methods, link) {
		if (im->active_ti == ti)
			qdwin_im_deactivate(im);
	}
}

/* Generic destructor handler shared by the input-method child objects whose
 * only request is `destroy`/`release` (popup surface, keyboard grab). */
static void
qdwin_im_generic_destroy(struct wl_client *client, struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

/* ---- zwp_input_method_keyboard_grab_v2 ---- */

static void
qdwin_im_grab_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_im_keyboard_grab *g = wl_resource_get_user_data(resource);
	if (!g)
		return;
	if (g->im)
		g->im->grab = NULL;
	/* Only end the grab if it is still the keyboard's active grab — weston
	 * may already have swapped it out (cancel), in which case g->keyboard
	 * was nulled and end_grab would corrupt a different grab. */
	if (g->keyboard && g->keyboard->grab == &g->base)
		weston_keyboard_end_grab(g->keyboard);
	free(g);
}

static const struct zwp_input_method_keyboard_grab_v2_interface
qdwin_im_keyboard_grab_impl = {
	.release = qdwin_im_generic_destroy,
};

static const struct zwp_input_popup_surface_v2_interface
qdwin_im_popup_impl = {
	.destroy = qdwin_im_generic_destroy,
};

/* weston keyboard grab: forward raw keys/modifiers to the IME grab resource and
 * SUPPRESS normal app delivery (spec: the compositor must not further process
 * an event after forwarding it to the grab holder). */
static void
qdwin_im_grab_key(struct weston_keyboard_grab *grab,
		  const struct weston_key_event *ke)
{
	/* J29: 14->16 time/key/state now come from the event struct. */
	const struct timespec *time = &ke->base.ts;
	uint32_t key = ke->key;
	uint32_t state = ke->key_state;
	struct qdwin_im_keyboard_grab *g =
		wl_container_of(grab, g, base);
	struct qdwin *qdwin = qdwin_singleton;
	struct wl_display *display;
	uint32_t serial, msecs;
	qdwin_idle_note_activity(qdwin);
	/* Same-client virtual-keyboard passthrough: when the IME re-injects a key
	 * it did NOT compose through its OWN virtual keyboard, that key must reach
	 * the focused app — not loop back into this grab and re-enter the IME.
	 * qdwin_vk_req_key marks the injecting client for the duration of the
	 * notify_key; if it is this grab's IME client, hand the event to the
	 * default grab (normal app delivery) and stop. Mirrors sway's
	 * keyboard_get_im_grab same-client bypass. */
	if (qdwin && qdwin->vk_injecting_client && g->im &&
	    qdwin->vk_injecting_client == g->im->client) {
		struct weston_keyboard *kbd = grab->keyboard;
		if (kbd)
			/* J29: 14->16 grab->key op takes the event struct. */
			kbd->default_grab.interface->key(&kbd->default_grab, ke);
		return;
	}
	if (!g->resource)
		return;
	display = grab->keyboard->seat->compositor->wl_display;
	serial = wl_display_next_serial(display);
	msecs = (uint32_t)((time->tv_sec * 1000) + (time->tv_nsec / 1000000));
	zwp_input_method_keyboard_grab_v2_send_key(g->resource, serial, msecs,
						   key, state);
}

static void
qdwin_im_grab_modifiers(struct weston_keyboard_grab *grab, uint32_t serial,
			uint32_t mods_depressed, uint32_t mods_latched,
			uint32_t mods_locked, uint32_t group)
{
	struct qdwin_im_keyboard_grab *g =
		wl_container_of(grab, g, base);
	struct qdwin *qdwin = qdwin_singleton;
	/* Same-client passthrough, exactly as qdwin_im_grab_key: a modifier
	 * update injected by the IME's own virtual keyboard goes to the app, not
	 * back to the IME. */
	if (qdwin && qdwin->vk_injecting_client && g->im &&
	    qdwin->vk_injecting_client == g->im->client) {
		struct weston_keyboard *kbd = grab->keyboard;
		if (kbd)
			kbd->default_grab.interface->modifiers(
				&kbd->default_grab, serial, mods_depressed,
				mods_latched, mods_locked, group);
		return;
	}
	if (!g->resource)
		return;
	zwp_input_method_keyboard_grab_v2_send_modifiers(
		g->resource, serial, mods_depressed, mods_latched,
		mods_locked, group);
}

static void
qdwin_im_grab_cancel(struct weston_keyboard_grab *grab)
{
	struct qdwin_im_keyboard_grab *g =
		wl_container_of(grab, g, base);
	/* weston is tearing the grab down (e.g. seat/keyboard release). Detach
	 * from the keyboard so the resource-destroy path does not call
	 * weston_keyboard_end_grab() on an already-gone grab. */
	g->keyboard = NULL;
}

static const struct weston_keyboard_grab_interface qdwin_im_grab_iface = {
	.key       = qdwin_im_grab_key,
	.modifiers = qdwin_im_grab_modifiers,
	.cancel    = qdwin_im_grab_cancel,
};

/* ---- zwp_input_method_v2 requests ---- */

static void
qdwin_im_req_commit_string(struct wl_client *client,
			   struct wl_resource *resource, const char *text)
{
	struct qdwin_input_method *im = wl_resource_get_user_data(resource);
	(void)client;
	if (!im || im->inert)
		return;
	free(im->pending_commit);
	im->pending_commit = qdwin_xstrdup_or_null(text);
}

static void
qdwin_im_req_set_preedit_string(struct wl_client *client,
				struct wl_resource *resource, const char *text,
				int32_t cursor_begin, int32_t cursor_end)
{
	struct qdwin_input_method *im = wl_resource_get_user_data(resource);
	(void)client;
	if (!im || im->inert)
		return;
	free(im->pending_preedit);
	im->pending_preedit = qdwin_xstrdup_or_null(text);
	im->pending_preedit_cb = cursor_begin;
	im->pending_preedit_ce = cursor_end;
}

static void
qdwin_im_req_delete_surrounding_text(struct wl_client *client,
				     struct wl_resource *resource,
				     uint32_t before_length,
				     uint32_t after_length)
{
	struct qdwin_input_method *im = wl_resource_get_user_data(resource);
	(void)client;
	if (!im || im->inert)
		return;
	im->pending_del_before = before_length;
	im->pending_del_after = after_length;
}

static void
qdwin_im_req_commit(struct wl_client *client, struct wl_resource *resource,
		    uint32_t serial)
{
	struct qdwin_input_method *im = wl_resource_get_user_data(resource);
	struct qdwin_text_input *ti;
	(void)client;
	if (!im || im->inert)
		return;
	ti = im->active_ti;
	/* Drop the buffered changes unless they target the current activation:
	 * the IME must be active for a still-enabled text_input AND echo our
	 * latest done serial (a stale serial means it is acting on superseded
	 * state — spec says do not change current state). */
	if (!im->active || !ti || !ti->current_enabled ||
	    serial != im->done_count) {
		qdwin_im_reset_pending(im);
		return;
	}
	if (im->pending_del_before || im->pending_del_after)
		zwp_text_input_v3_send_delete_surrounding_text(
			ti->resource, im->pending_del_before,
			im->pending_del_after);
	if (im->pending_commit && im->pending_commit[0])
		zwp_text_input_v3_send_commit_string(ti->resource,
						     im->pending_commit);
	/* Always send preedit (an empty string clears any existing preedit). */
	zwp_text_input_v3_send_preedit_string(
		ti->resource, im->pending_preedit ? im->pending_preedit : "",
		im->pending_preedit_cb, im->pending_preedit_ce);
	/* text-input-v3 done echoes the text_input's own commit serial. */
	zwp_text_input_v3_send_done(ti->resource, ti->commit_count);
	qdwin_im_reset_pending(im);
}

static void
qdwin_im_req_get_input_popup_surface(struct wl_client *client,
				     struct wl_resource *resource,
				     uint32_t id,
				     struct wl_resource *surface_resource)
{
	struct weston_surface *surface = surface_resource ?
		wl_resource_get_user_data(surface_resource) : NULL;
	struct wl_resource *popup;
	/* Per the protocol the surface gets the input_popup role and a role
	 * conflict must post a protocol error (set_role posts it itself). */
	if (surface &&
	    weston_surface_set_role(surface, "zwp_input_popup_surface_v2",
				    resource, ZWP_INPUT_METHOD_V2_ERROR_ROLE) < 0)
		return;
	/* Minimal popup: the object is created so the IME's candidate window
	 * does not error, but qdwin does not yet position it as an overlay
	 * (text_input_rectangle is a hint only) — composition does not depend on
	 * it. user_data is NULL: the only request is destroy (generic) and the
	 * popup must never carry a back-pointer to an input_method that may be
	 * destroyed first (its lifetime is client-managed). */
	popup = wl_resource_create(client,
				   &zwp_input_popup_surface_v2_interface,
				   wl_resource_get_version(resource), id);
	if (!popup) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(popup, &qdwin_im_popup_impl, NULL, NULL);
}

static void
qdwin_im_req_grab_keyboard(struct wl_client *client,
			   struct wl_resource *resource, uint32_t keyboard_id)
{
	struct qdwin_input_method *im = wl_resource_get_user_data(resource);
	struct qdwin_im_keyboard_grab *g;
	struct wl_resource *grab_res;
	struct weston_keyboard *kbd;
	int32_t rate, delay;

	grab_res = wl_resource_create(
		client, &zwp_input_method_keyboard_grab_v2_interface,
		wl_resource_get_version(resource), keyboard_id);
	if (!grab_res) {
		wl_client_post_no_memory(client);
		return;
	}
	if (!im || im->inert || !im->seat) {
		/* Inert IME or no seat: hand back an object that holds no grab. */
		wl_resource_set_implementation(grab_res,
			&qdwin_im_keyboard_grab_impl, NULL, NULL);
		return;
	}
	/* One grab per IME: drop a stale one first (ends its weston grab). */
	if (im->grab && im->grab->resource)
		wl_resource_destroy(im->grab->resource);

	g = calloc(1, sizeof *g);
	if (!g) {
		wl_resource_set_implementation(grab_res,
			&qdwin_im_keyboard_grab_impl, NULL, NULL);
		wl_client_post_no_memory(client);
		return;
	}
	g->base.interface = &qdwin_im_grab_iface;
	g->im = im;
	g->resource = grab_res;
	/* Track on the IME BEFORE anything can fail/return: otherwise an im
	 * destroy would free `im` while the live grab resource still holds
	 * g->im, and releasing the grab would deref freed `im`. With im->grab
	 * set, qdwin_im_detach nulls g->im on teardown. */
	im->grab = g;
	wl_resource_set_implementation(grab_res, &qdwin_im_keyboard_grab_impl,
				       g, qdwin_im_grab_resource_destroy);

	kbd = weston_seat_get_keyboard(im->seat);
	if (!kbd) {
		/* Seat has no keyboard capability right now; the object is live
		 * and tracked (im->grab) but holds no weston grab. */
		return;
	}
	g->keyboard = kbd;
	/* The grab interface is wire-compatible with wl_keyboard (the protocol
	 * mirrors wl_keyboard v6), so weston's keymap sender targets it
	 * correctly; key/modifiers/repeat_info use the generated senders. */
	weston_keyboard_send_keymap(kbd, grab_res);
	rate = im->qdwin->compositor->kb_repeat_rate;
	delay = im->qdwin->compositor->kb_repeat_delay;
	zwp_input_method_keyboard_grab_v2_send_repeat_info(grab_res, rate, delay);
	weston_keyboard_start_grab(kbd, &g->base);
}

static void
qdwin_im_req_destroy(struct wl_client *client, struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct zwp_input_method_v2_interface qdwin_input_method_impl = {
	.commit_string          = qdwin_im_req_commit_string,
	.set_preedit_string     = qdwin_im_req_set_preedit_string,
	.delete_surrounding_text = qdwin_im_req_delete_surrounding_text,
	.commit                 = qdwin_im_req_commit,
	.get_input_popup_surface = qdwin_im_req_get_input_popup_surface,
	.grab_keyboard          = qdwin_im_req_grab_keyboard,
	.destroy                = qdwin_im_req_destroy,
};

/* Detach an IME from all live compositor state (grab, seat listener, active
 * text_input). Idempotent; used by resource-destroy, seat-destroy and teardown. */
static void
qdwin_im_detach(struct qdwin_input_method *im)
{
	if (im->grab) {
		struct qdwin_im_keyboard_grab *g = im->grab;
		im->grab = NULL;
		g->im = NULL;
		if (g->keyboard && g->keyboard->grab == &g->base)
			weston_keyboard_end_grab(g->keyboard);
		g->keyboard = NULL;
	}
	if (im->seat) {
		wl_list_remove(&im->seat_destroy_listener.link);
		wl_list_init(&im->seat_destroy_listener.link);
		im->seat = NULL;
	}
	im->active = 0;
	im->active_ti = NULL;
	qdwin_im_reset_pending(im);
}

static void
qdwin_input_method_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_input_method *im = wl_resource_get_user_data(resource);
	if (!im)
		return;
	/* Per the protocol, destroying the input_method destroys its child
	 * keyboard grab. Destroy the grab resource first (its destroy callback
	 * ends the weston grab, frees the grab struct, and nulls im->grab), so
	 * detach below has nothing left to do for it and no orphaned grab
	 * resource survives carrying a back-pointer to the freed im. */
	if (im->grab && im->grab->resource)
		wl_resource_destroy(im->grab->resource);
	qdwin_im_detach(im);
	wl_list_remove(&im->link);
	free(im);
}

static void
qdwin_im_seat_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_input_method *im =
		wl_container_of(l, im, seat_destroy_listener);
	(void)data;
	/* The seat (hence its keyboard) is going away: tell the IME it is no
	 * longer usable, then detach. The resource stays alive but inert. */
	im->inert = 1;
	zwp_input_method_v2_send_unavailable(im->resource);
	/* UAF guard: vendored weston_seat_release() destroys (frees) the seat's
	 * weston_keyboard BEFORE emitting seat->destroy_signal, and
	 * weston_keyboard_destroy() does NOT cancel an active grab. So by the time
	 * this listener runs, a cached g->keyboard points at freed memory; reading
	 * g->keyboard->grab or calling weston_keyboard_end_grab() on it would be a
	 * use-after-free. The keyboard is already gone, so there is nothing left to
	 * end — null the cached pointer up front so the shared detach path below
	 * (and any later grab-resource destroy) never dereferences it. */
	if (im->grab)
		im->grab->keyboard = NULL;
	/* The listener link is removed inside detach; clear active state too. */
	qdwin_im_detach(im);
}

/* ---- zwp_input_method_manager_v2 ---- */

/* One per live manager resource. The back-pointer to qdwin is neutralized to
 * NULL at compositor teardown so a get_input_method on a manager that outlives
 * the plugin returns an inert object instead of dereferencing freed qdwin.
 * Mirrors qdwin_text_input_manager. */
struct qdwin_input_method_manager {
	struct qdwin *qdwin;          /* NULL after teardown neutralization */
	struct wl_resource *resource;
	struct wl_list link;          /* qdwin::input_method_managers */
};

static void
qdwin_im_manager_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_input_method_manager *mgr =
		wl_resource_get_user_data(resource);
	if (!mgr)
		return;
	wl_list_remove(&mgr->link);
	free(mgr);
}

static void
qdwin_input_method_managers_destroy_all(struct qdwin *qdwin)
{
	struct qdwin_input_method_manager *mgr, *tmp;
	wl_list_for_each_safe(mgr, tmp, &qdwin->input_method_managers, link) {
		wl_resource_set_user_data(mgr->resource, NULL);
		wl_list_remove(&mgr->link);
		free(mgr);
	}
}

static void
qdwin_im_manager_get_input_method(struct wl_client *client,
				  struct wl_resource *resource,
				  struct wl_resource *seat_resource,
				  uint32_t id)
{
	struct qdwin_input_method_manager *mgr =
		wl_resource_get_user_data(resource);
	struct qdwin *qdwin = mgr ? mgr->qdwin : NULL;
	struct weston_seat *seat =
		seat_resource ? wl_resource_get_user_data(seat_resource) : NULL;
	struct qdwin_input_method *im;
	struct wl_resource *im_res;
	struct qdwin_input_method *existing;

	im_res = wl_resource_create(client, &zwp_input_method_v2_interface,
				    wl_resource_get_version(resource), id);
	if (!im_res) {
		wl_client_post_no_memory(client);
		return;
	}
	if (!qdwin) {
		/* Manager outlived qdwin (teardown). Honour the new_id with an
		 * inert object rather than dereferencing freed state. */
		wl_resource_set_implementation(im_res, &qdwin_input_method_impl,
					       NULL, NULL);
		zwp_input_method_v2_send_unavailable(im_res);
		return;
	}
	im = calloc(1, sizeof *im);
	if (!im) {
		wl_resource_set_implementation(im_res, &qdwin_input_method_impl,
					       NULL, NULL);
		wl_client_post_no_memory(client);
		return;
	}
	/* One input method per seat: probe for a pre-existing live IME BEFORE
	 * inserting this one into the list — otherwise qdwin_im_for_seat would
	 * just find `im` itself and the duplicate would never be made inert
	 * (two concurrently-active privileged IMEs on a seat). */
	existing = qdwin_im_for_seat(qdwin, seat);
	im->qdwin = qdwin;
	im->resource = im_res;
	im->seat = seat;
	im->client = client;
	wl_list_init(&im->seat_destroy_listener.link);
	wl_list_insert(&qdwin->input_methods, &im->link);
	wl_resource_set_implementation(im_res, &qdwin_input_method_impl, im,
				       qdwin_input_method_resource_destroy);

	/* If this seat already has a live IME (or there is no seat), the new
	 * object is inert and gets only `unavailable` (protocol-mandated),
	 * never an implementation error. */
	if (!seat || existing) {
		im->inert = 1;
		im->seat = NULL;
		zwp_input_method_v2_send_unavailable(im_res);
		return;
	}
	im->seat_destroy_listener.notify = qdwin_im_seat_destroyed;
	wl_signal_add(&seat->destroy_signal, &im->seat_destroy_listener);
	/* If a text_input is already focused+enabled on this seat, activate now. */
	qdwin_im_sync_seat(qdwin, seat);
}

static void
qdwin_im_manager_destroy(struct wl_client *client, struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct zwp_input_method_manager_v2_interface
qdwin_im_manager_impl = {
	.get_input_method = qdwin_im_manager_get_input_method,
	.destroy          = qdwin_im_manager_destroy,
};

/* Shared identity gate for the privileged IME-family protocols
 * (zwp_input_method_manager_v2 + its companion zwp_virtual_keyboard_manager_v1).
 * Both grant keystroke capture / arbitrary injection across the focused app, so
 * both must be admitted ONLY to the trusted IME, gated identically: never a
 * sandboxed/secctx silo client (else one silo could keylog/inject into
 * another), only allowed_ime_uid (default allowed_uid), and — if configured —
 * the IME's own resolved exe / SELinux label, bracketed by a starttime
 * double-read so a recycled pid fails closed. Returns true if `client` may
 * bind; otherwise posts an implementation error on `client` (so the caller's
 * early-return is fail-closed) and returns false. `proto` names the interface
 * for the error/log text. Keeping ONE gate for both protocols guarantees they
 * cannot drift apart. */
static bool
qdwin_ime_family_bind_allowed(struct qdwin *qdwin, struct wl_client *client,
			      const char *proto)
{
	pid_t pid; uid_t uid; gid_t gid;
	uid_t allowed = qdwin->allowed_ime_uid != (uid_t)-1 ?
		qdwin->allowed_ime_uid : qdwin->allowed_uid;

	wl_client_get_credentials(client, &pid, &uid, &gid);
	weston_log("qdwin: %s bind attempt pid=%d uid=%u "
		   "(allowed_ime_uid=%u)\n",
		   proto, (int)pid, (unsigned)uid, (unsigned)allowed);

	/* Sandboxed/secctx clients (silo apps) must never become the IME — that
	 * would let one silo keylog/inject into another. The global filter
	 * already hides these globals from them; reject here too as defence in
	 * depth in case a client obtained the global another way. */
	if (qdwin_secctx_client_find(qdwin, client) != NULL) {
		wl_client_post_implementation_error(client,
			"%s: sandboxed clients may not act as an input method",
			proto);
		return false;
	}
	if (uid != allowed) {
		wl_client_post_implementation_error(client,
			"%s: uid %u not permitted (allowed ime uid=%u)",
			proto, (unsigned)uid, (unsigned)allowed);
		return false;
	}
	/* Optional defence-in-depth peer pins (resolved exe / SELinux label),
	 * bracketed by a starttime double-read so a recycled pid fails closed —
	 * mirrors the locker bind. Unreadable /proc maps to NULL so a configured
	 * non-empty expectation always fails closed. */
	if (qdwin->allowed_ime_exe || qdwin->allowed_ime_label) {
		uint64_t st_before = qdwin_proc_starttime(pid);
		uint64_t st_after;
		if (qdwin->allowed_ime_exe) {
			char *exe = qdwin_proc_exe(pid);
			int ok = (exe &&
				  strcmp(exe, qdwin->allowed_ime_exe) == 0);
			free(exe);
			if (!ok) {
				weston_log("qdwin: %s bind rejected "
					   "pid=%d (exe mismatch)\n",
					   proto, (int)pid);
				wl_client_post_implementation_error(client,
					"%s: peer exe not permitted", proto);
				return false;
			}
		}
		if (qdwin->allowed_ime_label) {
			char *label = qdwin_proc_selinux_label(pid);
			int ok = (label &&
				  strcmp(label, qdwin->allowed_ime_label) == 0);
			free(label);
			if (!ok) {
				weston_log("qdwin: %s bind rejected "
					   "pid=%d (label mismatch)\n",
					   proto, (int)pid);
				wl_client_post_implementation_error(client,
					"%s: peer label not permitted", proto);
				return false;
			}
		}
		st_after = qdwin_proc_starttime(pid);
		if (st_before == 0 || st_after == 0 || st_before != st_after) {
			wl_client_post_implementation_error(client,
				"%s: peer identity unstable", proto);
			return false;
		}
	}
	return true;
}

static void
bind_qdwin_input_method_manager(struct wl_client *client, void *data,
				uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource;
	pid_t pid; uid_t uid; gid_t gid;

	/* Identity gate (secctx-deny + allowed_ime_uid + optional exe/label),
	 * shared with the virtual-keyboard companion. Fail-closed: on reject the
	 * helper posts the error and we return BEFORE creating the resource. */
	if (!qdwin_ime_family_bind_allowed(qdwin, client,
					   "zwp_input_method_manager_v2"))
		return;
	wl_client_get_credentials(client, &pid, &uid, &gid);

	resource = wl_resource_create(
		client, &zwp_input_method_manager_v2_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	{
		struct qdwin_input_method_manager *mgr = calloc(1, sizeof *mgr);
		if (!mgr) {
			wl_resource_set_implementation(resource,
				&qdwin_im_manager_impl, NULL, NULL);
			wl_client_post_no_memory(client);
			return;
		}
		mgr->qdwin = qdwin;
		mgr->resource = resource;
		wl_list_insert(&qdwin->input_method_managers, &mgr->link);
		wl_resource_set_implementation(resource, &qdwin_im_manager_impl,
					       mgr, qdwin_im_manager_resource_destroy);
	}
	weston_log("qdwin: input-method manager bound by uid=%u pid=%d\n",
		   (unsigned)uid, (int)pid);
}

/* Compositor-teardown drain: neutralize every live input-method resource so a
 * late destroy callback no-ops, and detach compositor state. Mirrors
 * qdwin_text_inputs_destroy_all. */
static void
qdwin_input_methods_destroy_all(struct qdwin *qdwin)
{
	struct qdwin_input_method *im, *tmp;
	wl_list_for_each_safe(im, tmp, &qdwin->input_methods, link) {
		/* Neutralize the grab resource (so a late libwayland-driven
		 * destroy no-ops) AND free the grab struct here: qdwin_im_detach
		 * only ends the weston grab and nulls im->grab, it does not free
		 * the struct, so without this the grab would leak at teardown. */
		struct qdwin_im_keyboard_grab *g = im->grab;
		if (g && g->resource)
			wl_resource_set_user_data(g->resource, NULL);
		qdwin_im_detach(im);
		free(g);
		wl_resource_set_user_data(im->resource, NULL);
		wl_list_remove(&im->link);
		free(im);
	}
}

/* ==================================================================
 * Bucket A / P1 companion: virtual-keyboard-unstable-v1
 * (zwp_virtual_keyboard_manager_v1).
 *
 * The other half of a grabbing IME. input-method-v2 lets fcitx5/ibus grab the
 * seat keyboard and compose; for the keys it does NOT compose, it passes them
 * back to the focused app by injecting them through a virtual keyboard. Without
 * this companion a grabbing IME would simply swallow passthrough keys (hence it
 * is a hard prerequisite before enabling a real IME in production — see
 * todo/open-followups.md).
 *
 * The compositor injects each key via notify_key() and each modifier/group
 * update via notify_modifiers() on the target seat — i.e. straight into the
 * seat's keyboard, exactly as a hardware key would arrive, so the focused
 * client sees ordinary wl_keyboard events.
 *
 * KEYMAP: the protocol has the client upload its own keymap. qdwin does NOT
 * swap the shared seat keymap to a per-virtual-keyboard one (that would mutate
 * the real keyboard's keymap for every client and race the hardware keyboard);
 * instead injected keycodes are interpreted with the seat keymap. For the one
 * supported use case — an IME re-injecting the very keys it received from the
 * seat keyboard grab — the keymaps are identical (the IME got the keymap from
 * the compositor via the grab's send_keymap), so this is correct. We still
 * enforce the protocol contract: the format must be XKB_V1 and a keymap must be
 * set before key/modifiers (else the protocol-mandated no_keymap /
 * invalid_keymap_format errors).
 *
 * Trust: a virtual keyboard injects arbitrary keystrokes into whatever app is
 * focused — equally privileged to input-method-v2's keystroke capture. It is
 * gated IDENTICALLY: hidden from secctx/sandboxed clients by the global filter
 * and bind-gated via the SAME qdwin_ime_family_bind_allowed() helper
 * (allowed_ime_uid + secctx deny + optional exe/label pins). No silo app may
 * ever obtain one.
 * ================================================================== */

struct qdwin_virtual_keyboard {
	struct qdwin *qdwin;
	struct wl_resource *resource;     /* zwp_virtual_keyboard_v1 */
	struct weston_seat *seat;         /* NULL after seat destroy (inert) */
	struct wl_client *client;
	int has_keymap;                   /* keymap set → key/modifiers allowed */
	struct wl_listener seat_destroy_listener;
	struct wl_list link;              /* qdwin::virtual_keyboards */
};

/* Detach from the seat (drop the destroy listener) and mark inert. Idempotent;
 * used by resource-destroy, seat-destroy and teardown. */
static void
qdwin_virtual_keyboard_detach(struct qdwin_virtual_keyboard *vk)
{
	if (vk->seat) {
		wl_list_remove(&vk->seat_destroy_listener.link);
		wl_list_init(&vk->seat_destroy_listener.link);
		vk->seat = NULL;
	}
}

/* ---- zwp_virtual_keyboard_v1 requests ---- */

static void
qdwin_vk_req_keymap(struct wl_client *client, struct wl_resource *resource,
		    uint32_t format, int32_t fd, uint32_t size)
{
	struct qdwin_virtual_keyboard *vk = wl_resource_get_user_data(resource);
	(void)client;
	(void)size;
	/* Only the standard xkb v1 text keymap is accepted. We do not apply the
	 * keymap to the seat (see the section comment), but we must still
	 * validate the format per the protocol and always consume the fd so it
	 * does not leak. */
	if (format != WL_KEYBOARD_KEYMAP_FORMAT_XKB_V1) {
		if (fd >= 0)
			close(fd);
		wl_resource_post_error(resource,
			ZWP_VIRTUAL_KEYBOARD_V1_ERROR_INVALID_KEYMAP_FORMAT,
			"unsupported keymap format %u (only XKB_V1)",
			(unsigned)format);
		return;
	}
	if (fd >= 0)
		close(fd);
	if (vk)
		vk->has_keymap = 1;
}

static void
qdwin_vk_req_key(struct wl_client *client, struct wl_resource *resource,
		 uint32_t time, uint32_t key, uint32_t state)
{
	struct qdwin_virtual_keyboard *vk = wl_resource_get_user_data(resource);
	struct timespec ts;
	(void)client;
	if (!vk)
		return;
	/* Protocol: a keymap must be set before any key event. */
	if (!vk->has_keymap) {
		wl_resource_post_error(resource,
			ZWP_VIRTUAL_KEYBOARD_V1_ERROR_NO_KEYMAP,
			"key event before keymap was set");
		return;
	}
	/* Inert once the seat is gone, or while the seat has no keyboard. */
	if (!vk->seat || !weston_seat_get_keyboard(vk->seat))
		return;
	/* Synthetic input still counts as activity (resets the idle timer),
	 * mirroring the IME keyboard-grab path. */
	qdwin_idle_note_activity(vk->qdwin);
	ts = qdwin_ts_from_msec(time);
	/* Mark the injecting client so an active same-client IME keyboard grab
	 * passes this key through to the app instead of looping it back to the
	 * IME (see qdwin_im_grab_key). Cleared immediately after — the marker is
	 * only meaningful for the synchronous notify_key dispatch. */
	vk->qdwin->vk_injecting_client = vk->client;
	/* STATE_UPDATE_AUTOMATIC so an injected modifier *keycode* updates the
	 * seat's xkb modifier state just like a hardware key — the focused app
	 * then sees correct modifiers on subsequent keys. The explicit
	 * modifiers request below complements this for latched/locked/group. */
	qdwin_notify_key(vk->seat, &ts, key,
		   state ? WL_KEYBOARD_KEY_STATE_PRESSED
			 : WL_KEYBOARD_KEY_STATE_RELEASED,
		   STATE_UPDATE_AUTOMATIC);
	vk->qdwin->vk_injecting_client = NULL;
}

static void
qdwin_vk_req_modifiers(struct wl_client *client, struct wl_resource *resource,
		       uint32_t mods_depressed, uint32_t mods_latched,
		       uint32_t mods_locked, uint32_t group)
{
	struct qdwin_virtual_keyboard *vk = wl_resource_get_user_data(resource);
	struct weston_keyboard *kbd;
	(void)client;
	if (!vk)
		return;
	if (!vk->has_keymap) {
		wl_resource_post_error(resource,
			ZWP_VIRTUAL_KEYBOARD_V1_ERROR_NO_KEYMAP,
			"modifiers before keymap was set");
		return;
	}
	if (!vk->seat)
		return;
	kbd = weston_seat_get_keyboard(vk->seat);
	if (!kbd || !kbd->xkb_state.state)
		return;
	/* Set the seat keyboard's xkb modifier/group state explicitly, then let
	 * notify_modifiers() re-serialize it and fan out to clients/grabs — the
	 * same path weston's own update_modifier_state() uses. (group maps to
	 * the locked layout index.) The injecting-client marker routes a
	 * same-client IME grab's modifiers to the app (see qdwin_im_grab_modifiers). */
	xkb_state_update_mask(kbd->xkb_state.state, mods_depressed, mods_latched,
			      mods_locked, 0, 0, group);
	vk->qdwin->vk_injecting_client = vk->client;
	notify_modifiers(vk->seat,
			 wl_display_next_serial(vk->qdwin->compositor->wl_display));
	vk->qdwin->vk_injecting_client = NULL;
}

static const struct zwp_virtual_keyboard_v1_interface
qdwin_virtual_keyboard_impl = {
	.keymap    = qdwin_vk_req_keymap,
	.key       = qdwin_vk_req_key,
	.modifiers = qdwin_vk_req_modifiers,
	.destroy   = qdwin_im_generic_destroy,  /* shared: only wl_resource_destroy */
};

static void
qdwin_virtual_keyboard_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_virtual_keyboard *vk = wl_resource_get_user_data(resource);
	if (!vk)
		return;
	qdwin_virtual_keyboard_detach(vk);
	wl_list_remove(&vk->link);
	free(vk);
}

static void
qdwin_vk_seat_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_virtual_keyboard *vk =
		wl_container_of(l, vk, seat_destroy_listener);
	(void)data;
	/* The seat (hence its keyboard) is going away; the vk can no longer
	 * inject. The resource stays alive but inert (key/modifiers drop on a
	 * NULL seat). */
	qdwin_virtual_keyboard_detach(vk);
}

/* ---- zwp_virtual_keyboard_manager_v1 ---- */

/* One per live manager resource. The back-pointer to qdwin is neutralized to
 * NULL at compositor teardown so a create_virtual_keyboard on a manager that
 * outlives the plugin returns an inert object instead of dereferencing freed
 * qdwin. Mirrors qdwin_input_method_manager. */
struct qdwin_virtual_keyboard_manager {
	struct qdwin *qdwin;          /* NULL after teardown neutralization */
	struct wl_resource *resource;
	struct wl_list link;          /* qdwin::virtual_keyboard_managers */
};

static void
qdwin_vk_manager_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_virtual_keyboard_manager *mgr =
		wl_resource_get_user_data(resource);
	if (!mgr)
		return;
	wl_list_remove(&mgr->link);
	free(mgr);
}

static void
qdwin_virtual_keyboard_managers_destroy_all(struct qdwin *qdwin)
{
	struct qdwin_virtual_keyboard_manager *mgr, *tmp;
	wl_list_for_each_safe(mgr, tmp, &qdwin->virtual_keyboard_managers, link) {
		wl_resource_set_user_data(mgr->resource, NULL);
		wl_list_remove(&mgr->link);
		free(mgr);
	}
}

static void
qdwin_vk_manager_create_virtual_keyboard(struct wl_client *client,
					 struct wl_resource *resource,
					 struct wl_resource *seat_resource,
					 uint32_t id)
{
	struct qdwin_virtual_keyboard_manager *mgr =
		wl_resource_get_user_data(resource);
	struct qdwin *qdwin = mgr ? mgr->qdwin : NULL;
	struct weston_seat *seat =
		seat_resource ? wl_resource_get_user_data(seat_resource) : NULL;
	struct qdwin_virtual_keyboard *vk;
	struct wl_resource *vk_res;

	vk_res = wl_resource_create(client, &zwp_virtual_keyboard_v1_interface,
				    wl_resource_get_version(resource), id);
	if (!vk_res) {
		wl_client_post_no_memory(client);
		return;
	}
	if (!qdwin) {
		/* Manager outlived qdwin (teardown). Honour the new_id with an
		 * inert object (NULL user_data → key/modifiers no-op) rather
		 * than dereferencing freed state. */
		wl_resource_set_implementation(vk_res,
			&qdwin_virtual_keyboard_impl, NULL, NULL);
		return;
	}
	vk = calloc(1, sizeof *vk);
	if (!vk) {
		wl_resource_set_implementation(vk_res,
			&qdwin_virtual_keyboard_impl, NULL, NULL);
		wl_client_post_no_memory(client);
		return;
	}
	vk->qdwin = qdwin;
	vk->resource = vk_res;
	vk->seat = seat;
	vk->client = client;
	wl_list_init(&vk->seat_destroy_listener.link);
	wl_list_insert(&qdwin->virtual_keyboards, &vk->link);
	wl_resource_set_implementation(vk_res, &qdwin_virtual_keyboard_impl, vk,
				       qdwin_virtual_keyboard_resource_destroy);
	/* No seat (invalid seat_resource): the object is live and tracked but
	 * inert (no injection target). The protocol allows several virtual
	 * keyboards per seat, so there is no single-claimant restriction. */
	if (seat) {
		vk->seat_destroy_listener.notify = qdwin_vk_seat_destroyed;
		wl_signal_add(&seat->destroy_signal, &vk->seat_destroy_listener);
	}
}

static const struct zwp_virtual_keyboard_manager_v1_interface
qdwin_vk_manager_impl = {
	.create_virtual_keyboard = qdwin_vk_manager_create_virtual_keyboard,
};

static void
bind_qdwin_virtual_keyboard_manager(struct wl_client *client, void *data,
				    uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource;
	pid_t pid; uid_t uid; gid_t gid;

	/* SAME identity gate as input-method-v2 (shared helper): a virtual
	 * keyboard injects arbitrary keystrokes into the focused app, so it is
	 * equally privileged. Fail-closed — on reject the helper posts the error
	 * and we return BEFORE creating the resource. */
	if (!qdwin_ime_family_bind_allowed(qdwin, client,
					   "zwp_virtual_keyboard_manager_v1"))
		return;
	wl_client_get_credentials(client, &pid, &uid, &gid);

	resource = wl_resource_create(
		client, &zwp_virtual_keyboard_manager_v1_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	{
		struct qdwin_virtual_keyboard_manager *mgr = calloc(1, sizeof *mgr);
		if (!mgr) {
			wl_resource_set_implementation(resource,
				&qdwin_vk_manager_impl, NULL, NULL);
			wl_client_post_no_memory(client);
			return;
		}
		mgr->qdwin = qdwin;
		mgr->resource = resource;
		wl_list_insert(&qdwin->virtual_keyboard_managers, &mgr->link);
		wl_resource_set_implementation(resource, &qdwin_vk_manager_impl,
					       mgr, qdwin_vk_manager_resource_destroy);
	}
	weston_log("qdwin: virtual-keyboard manager bound by uid=%u pid=%d\n",
		   (unsigned)uid, (int)pid);
}

/* Compositor-teardown drain: neutralize every live virtual-keyboard resource so
 * a late destroy callback no-ops, and detach seat state. Mirrors
 * qdwin_input_methods_destroy_all. */
static void
qdwin_virtual_keyboards_destroy_all(struct qdwin *qdwin)
{
	struct qdwin_virtual_keyboard *vk, *tmp;
	wl_list_for_each_safe(vk, tmp, &qdwin->virtual_keyboards, link) {
		qdwin_virtual_keyboard_detach(vk);
		wl_resource_set_user_data(vk->resource, NULL);
		wl_list_remove(&vk->link);
		free(vk);
	}
}

/* ------------------------------------------------------------------
 * §6.7 primary-selection-unstable-v1.
 *
 * Middle-click clipboard (per-seat). One qdwin_primary_seat per
 * weston_seat tracks the current source + attached devices. A client
 * calls create_source(), then source.offer(mime) for each advertised
 * type, then device.set_selection(source, serial). The compositor
 * fans out a fresh offer resource to every other device on the seat
 * and tells them about the new selection. A reading client calls
 * offer.receive(mime, fd) → compositor forwards source.send(mime, fd)
 * to the source's client, which writes the data and closes the fd.
 *
 * Scope today: broadcast delivery to all devices on the seat. The
 * spec permits focus-gated delivery (only the keyboard-focused
 * client gets selection events) as a privacy hardening; we leave
 * that as a follow-up once §6.6 S0 gives us reliable focus hooks.
 * Primary-selection content is generally low-sensitivity (transient
 * text), and qdwin's peer-uid plugin gate limits clients to the
 * same uid anyway.
 * ------------------------------------------------------------------ */

struct qdwin_primary_seat;
struct qdwin_primary_source;
struct qdwin_primary_device;

struct qdwin_primary_mime {
	char *type;
	struct wl_list link;  /* qdwin_primary_source::mime_types */
};

struct qdwin_primary_source {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct qdwin_primary_seat *pseat;  /* NULL until set_selection wins */
	struct wl_list mime_types;          /* qdwin_primary_mime::link */
	struct wl_list offers;              /* qdwin_primary_offer::link */
};

struct qdwin_primary_device {
	struct qdwin *qdwin;
	struct qdwin_primary_seat *pseat;
	struct wl_resource *resource;
	struct wl_list link;  /* qdwin_primary_seat::devices */
};

struct qdwin_primary_offer {
	struct qdwin_primary_source *source;  /* NULL after source destroyed */
	struct wl_resource *resource;
	struct wl_list link;                  /* qdwin_primary_source::offers */
};

struct qdwin_primary_seat {
	struct qdwin *qdwin;
	struct weston_seat *seat;
	struct qdwin_primary_source *current_source;
	uint32_t selection_serial;
	struct wl_list devices;  /* qdwin_primary_device::link */
	struct wl_listener seat_destroy_listener;
	struct wl_list link;  /* qdwin::primary_seats::link */
};

static struct qdwin_primary_seat *
qdwin_primary_seat_find(struct qdwin *qdwin, struct weston_seat *seat)
{
	struct qdwin_primary_seat *pseat;
	wl_list_for_each(pseat, &qdwin->primary_seats, link)
		if (pseat->seat == seat)
			return pseat;
	return NULL;
}

static void qdwin_primary_seat_seat_destroyed(struct wl_listener *l,
					      void *data);

static struct qdwin_primary_seat *
qdwin_primary_seat_ensure(struct qdwin *qdwin, struct weston_seat *seat)
{
	struct qdwin_primary_seat *pseat = qdwin_primary_seat_find(qdwin, seat);
	if (pseat)
		return pseat;
	pseat = calloc(1, sizeof *pseat);
	if (!pseat)
		return NULL;
	pseat->qdwin = qdwin;
	pseat->seat = seat;
	wl_list_init(&pseat->devices);
	pseat->seat_destroy_listener.notify = qdwin_primary_seat_seat_destroyed;
	wl_signal_add(&seat->destroy_signal, &pseat->seat_destroy_listener);
	wl_list_insert(&qdwin->primary_seats, &pseat->link);
	return pseat;
}

/* --- primary_selection_offer -------------------------------------- */

/* deeper-review D1: are two clients in the same silo, for primary-selection
 * receive gating? Same silo == matching security-context (engine, app_id);
 * two untagged clients (trusted admin-uid session tools) are also same-silo.
 * A tagged client paired with an untagged one, or differing (engine, app_id),
 * is CROSS-silo. instance_id is excluded on purpose — different launches of
 * one silo are still the same silo (mirrors qdwin_toplevel_for_secctx_app_id,
 * D3). Fails closed (returns false) on any incomplete identity. */
static bool
qdwin_primary_same_silo(struct qdwin *qdwin,
			struct wl_client *a, struct wl_client *b)
{
	if (a == b)
		return true;
	struct qdwin_secctx_client *sa = qdwin_secctx_client_lookup(qdwin, a);
	struct qdwin_secctx_client *sb = qdwin_secctx_client_lookup(qdwin, b);
	if (!sa && !sb)
		return true;   /* both untagged (admin / ordinary) */
	if (!sa || !sb)
		return false;  /* one tagged, one not → cross-boundary */
	const char *ea = qdwin_secctx_client_engine(sa);
	const char *eb = qdwin_secctx_client_engine(sb);
	const char *aa = qdwin_secctx_client_app_id(sa);
	const char *ab = qdwin_secctx_client_app_id(sb);
	if (!ea || !*ea || !eb || !*eb || !aa || !*aa || !ab || !*ab)
		return false;  /* incomplete identity → fail closed */
	return strcmp(ea, eb) == 0 && strcmp(aa, ab) == 0;
}

static void
qdwin_primary_offer_receive(struct wl_client *client,
			    struct wl_resource *resource,
			    const char *mime_type, int32_t fd)
{
	struct qdwin_primary_offer *offer = wl_resource_get_user_data(resource);
	struct qdwin *qdwin = qdwin_singleton;
	if (offer && offer->source && offer->source->resource && qdwin) {
		struct wl_client *src_client =
			wl_resource_get_client(offer->source->resource);
		/* deeper-review D1: the primary selection (middle-click clipboard)
		 * had NO cross-silo receive gate — unlike wl_data_device, whose
		 * v15 send-shim routes every receive through a per-MIME, fail-
		 * closed broker decision — so one silo could read another silo's
		 * primary selection by calling receive() on the broadcast offer.
		 * Gate it: forward a same-silo paste; default-DENY a cross-silo
		 * receive (fail closed — fd is closed below so the receiver sees
		 * EOF / empty paste). A broker-rule allow path for cross-silo
		 * primary paste (mirroring the clipboard ClipboardGate) can be
		 * layered on later if it becomes a product requirement; default-
		 * deny is the secure baseline and matches the clipboard posture. */
		if (qdwin_primary_same_silo(qdwin, src_client, client)) {
			zwp_primary_selection_source_v1_send_send(
				offer->source->resource, mime_type, fd);
		} else {
			weston_log("qdwin: primary-selection receive DENIED "
				   "(cross-silo) mime='%s'\n",
				   mime_type ? mime_type : "");
		}
	}
	close(fd);
}

static void
qdwin_primary_offer_destroy(struct wl_client *client,
			    struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct zwp_primary_selection_offer_v1_interface
qdwin_primary_offer_impl = {
	.receive = qdwin_primary_offer_receive,
	.destroy = qdwin_primary_offer_destroy,
};

static void
qdwin_primary_offer_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_primary_offer *offer = wl_resource_get_user_data(resource);
	if (offer && offer->source) {
		wl_list_remove(&offer->link);
		offer->source = NULL;
	}
	free(offer);
}

/* Build a new offer for `device`, hand it to the client, and announce
 * each mime type. Does not send selection(offer) itself — caller does. */
static struct wl_resource *
qdwin_primary_build_offer_for_device(struct qdwin_primary_device *device,
				     struct qdwin_primary_source *source)
{
	struct wl_client *client = wl_resource_get_client(device->resource);
	uint32_t version = wl_resource_get_version(device->resource);
	struct qdwin_primary_offer *offer = calloc(1, sizeof *offer);
	struct wl_resource *res;
	struct qdwin_primary_mime *m;
	if (!offer)
		return NULL;
	res = wl_resource_create(client,
				 &zwp_primary_selection_offer_v1_interface,
				 version, 0);
	if (!res) {
		free(offer);
		return NULL;
	}
	offer->source = source;
	offer->resource = res;
	wl_list_insert(&source->offers, &offer->link);
	wl_resource_set_implementation(res, &qdwin_primary_offer_impl, offer,
				       qdwin_primary_offer_resource_destroy);
	zwp_primary_selection_device_v1_send_data_offer(device->resource, res);
	wl_list_for_each(m, &source->mime_types, link)
		zwp_primary_selection_offer_v1_send_offer(res, m->type);
	return res;
}

/* --- primary_selection_source ------------------------------------ */

static void
qdwin_primary_source_clear_offers(struct qdwin_primary_source *source)
{
	/* Offers keep a pointer back to source; mark them stale so their
	 * receive() becomes a no-op after source goes away. */
	struct qdwin_primary_offer *offer, *tmp;
	wl_list_for_each_safe(offer, tmp, &source->offers, link) {
		wl_list_remove(&offer->link);
		wl_list_init(&offer->link);
		offer->source = NULL;
	}
}

static void
qdwin_primary_source_free_mimes(struct qdwin_primary_source *source)
{
	struct qdwin_primary_mime *m, *tmp;
	wl_list_for_each_safe(m, tmp, &source->mime_types, link) {
		wl_list_remove(&m->link);
		free(m->type);
		free(m);
	}
}

static void
qdwin_primary_source_offer(struct wl_client *client,
			   struct wl_resource *resource,
			   const char *mime_type)
{
	struct qdwin_primary_source *source = wl_resource_get_user_data(resource);
	struct qdwin_primary_mime *m;
	unsigned n = 0;
	(void)client;
	if (!mime_type)
		return;
	/* F7: bound the per-MIME-type length (DoS guard) — these are stored and
	 * re-broadcast to every primary-selection device. Silently drop an
	 * over-long type (same fail-closed-drop posture as the count cap below;
	 * a protocol error would kill an otherwise-legitimate noisy client). */
	if (strnlen(mime_type, QDWIN_MIME_TYPE_MAX + 1u) > QDWIN_MIME_TYPE_MAX) {
		weston_log("qdwin: primary-selection offer dropped: "
			   "mime too long (cap %u)\n", QDWIN_MIME_TYPE_MAX);
		return;
	}
	/* D5 (deeper-review): bound the per-source MIME list. A source client
	 * may call offer() unboundedly; each entry is a calloc+strdup that is
	 * re-broadcast to every primary-selection device, so an unbounded list
	 * is a heap + event-queue DoS (the same family as the F7 length caps).
	 * Real sources advertise a handful to a few dozen target names;
	 * QDWIN_PRIMARY_MIME_MAX is far above any legitimate set. Over the cap
	 * we SILENTLY drop further offers — posting a protocol error here would
	 * kill an otherwise-legitimate but noisy client, which is the wrong
	 * trade for a DoS guard. (The regular clipboard source is owned by
	 * vendored libweston and capped separately/upstream.) */
	wl_list_for_each(m, &source->mime_types, link) {
		if (++n >= QDWIN_PRIMARY_MIME_MAX) {
			weston_log("qdwin: primary-selection offer dropped: "
				   "MIME cap %u reached\n",
				   QDWIN_PRIMARY_MIME_MAX);
			return;
		}
	}
	m = calloc(1, sizeof *m);
	if (!m)
		return;
	m->type = strdup(mime_type);
	if (!m->type) {
		free(m);
		return;
	}
	wl_list_insert(source->mime_types.prev, &m->link);
}

static void
qdwin_primary_source_destroy_req(struct wl_client *client,
				 struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct zwp_primary_selection_source_v1_interface
qdwin_primary_source_impl = {
	.offer   = qdwin_primary_source_offer,
	.destroy = qdwin_primary_source_destroy_req,
};

static void
qdwin_primary_seat_clear_selection(struct qdwin_primary_seat *pseat,
				   int notify_source);

static void
qdwin_primary_source_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_primary_source *source = wl_resource_get_user_data(resource);
	if (!source)
		return;
	if (source->pseat && source->pseat->current_source == source) {
		/* Source died while owning the selection. Notify all
		 * devices with selection(NULL) but don't fire cancelled
		 * on ourselves — the resource is already being torn down. */
		qdwin_primary_seat_clear_selection(source->pseat, 0);
	}
	qdwin_primary_source_clear_offers(source);
	qdwin_primary_source_free_mimes(source);
	free(source);
}

/* --- primary_selection_device ------------------------------------ */

/* True when the primary seat currently owns a selection source. Lets the
 * clear_selection handler (far above, where the struct is opaque) test
 * emptiness for its idempotent fast-path. */
static bool
qdwin_primary_seat_has_source(struct qdwin_primary_seat *pseat)
{
	return pseat && pseat->current_source != NULL;
}

static void
qdwin_primary_seat_clear_selection(struct qdwin_primary_seat *pseat,
				   int notify_source)
{
	struct qdwin_primary_source *prev = pseat->current_source;
	struct qdwin_primary_device *device;
	pseat->current_source = NULL;
	if (prev) {
		if (notify_source && prev->resource)
			zwp_primary_selection_source_v1_send_cancelled(
				prev->resource);
		prev->pseat = NULL;
	}
	wl_list_for_each(device, &pseat->devices, link)
		zwp_primary_selection_device_v1_send_selection(
			device->resource, NULL);
}

static void
qdwin_primary_device_set_selection(struct wl_client *client,
				   struct wl_resource *resource,
				   struct wl_resource *source_resource,
				   uint32_t serial)
{
	struct qdwin_primary_device *device = wl_resource_get_user_data(resource);
	struct qdwin_primary_source *source =
		source_resource ? wl_resource_get_user_data(source_resource)
				: NULL;
	struct qdwin_primary_seat *pseat = device ? device->pseat : NULL;
	struct qdwin_primary_device *d;
	(void)client;
	if (!pseat)
		return;
	/* Same stale-serial guard as weston_seat_set_selection
	 * (libweston data-device.c): silently ignore an older serial
	 * when a source is already set. First set is never stale. */
	if (qdwin_selection_serial_is_stale(pseat->selection_serial, serial,
					    pseat->current_source != NULL))
		return;
	if (source && source->pseat && source->pseat != pseat) {
		/* Source already bound elsewhere — reject per spec intent. */
		return;
	}
	/* Replace previous selection. Notify old source via cancelled. */
	if (pseat->current_source && pseat->current_source != source)
		qdwin_primary_seat_clear_selection(pseat, 1);
	else if (!source)
		qdwin_primary_seat_clear_selection(pseat, 1);
	pseat->selection_serial = serial;
	if (!source)
		return;
	source->pseat = pseat;
	pseat->current_source = source;
	/* For every device on the seat, emit a fresh data_offer, then
	 * announce the selection. */
	wl_list_for_each(d, &pseat->devices, link) {
		struct wl_resource *offer_res =
			qdwin_primary_build_offer_for_device(d, source);
		zwp_primary_selection_device_v1_send_selection(
			d->resource, offer_res);
	}
	/* spec/10: surface to the shell so it can broker-gate. mime_types
	 * here are stored as struct qdwin_primary_mime entries on a
	 * wl_list, not a wl_array — pack them into a flat wl_array on
	 * the stack so we can reuse the regular pack helper. */
	if (qdwin_shell_can_receive_v11(pseat->qdwin)) {
		struct wl_array tmp;
		wl_array_init(&tmp);
		struct qdwin_primary_mime *m;
		wl_list_for_each(m, &source->mime_types, link) {
			char **slot = wl_array_add(&tmp, sizeof(char *));
			if (slot)
				*slot = m->type;
		}
		struct wl_client *src_client = source->resource ?
			wl_resource_get_client(source->resource) : NULL;
		qdwin_emit_selection_set(pseat->qdwin, pseat->seat, &tmp, 1,
					 src_client);
		wl_array_release(&tmp);
	}
}

static void
qdwin_primary_device_destroy_req(struct wl_client *client,
				 struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct zwp_primary_selection_device_v1_interface
qdwin_primary_device_impl = {
	.set_selection = qdwin_primary_device_set_selection,
	.destroy       = qdwin_primary_device_destroy_req,
};

static void
qdwin_primary_device_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_primary_device *device = wl_resource_get_user_data(resource);
	if (!device)
		return;
	wl_list_remove(&device->link);
	free(device);
}

/* --- primary_selection_device_manager ---------------------------- */

static void
qdwin_primary_manager_create_source(struct wl_client *client,
				    struct wl_resource *resource,
				    uint32_t id)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_primary_source *source = calloc(1, sizeof *source);
	struct wl_resource *res;
	if (!source) {
		wl_client_post_no_memory(client);
		return;
	}
	res = wl_resource_create(client,
				 &zwp_primary_selection_source_v1_interface,
				 wl_resource_get_version(resource), id);
	if (!res) {
		free(source);
		wl_client_post_no_memory(client);
		return;
	}
	source->qdwin = qdwin;
	source->resource = res;
	wl_list_init(&source->mime_types);
	wl_list_init(&source->offers);
	wl_resource_set_implementation(res, &qdwin_primary_source_impl,
				       source,
				       qdwin_primary_source_resource_destroy);
}

static void
qdwin_primary_manager_get_device(struct wl_client *client,
				 struct wl_resource *resource,
				 uint32_t id,
				 struct wl_resource *seat_resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct weston_seat *seat =
		seat_resource ? wl_resource_get_user_data(seat_resource)
			      : NULL;
	struct qdwin_primary_seat *pseat;
	struct qdwin_primary_device *device;
	struct wl_resource *res;
	if (!seat) {
		wl_client_post_no_memory(client);
		return;
	}
	pseat = qdwin_primary_seat_ensure(qdwin, seat);
	if (!pseat) {
		wl_client_post_no_memory(client);
		return;
	}
	device = calloc(1, sizeof *device);
	if (!device) {
		wl_client_post_no_memory(client);
		return;
	}
	res = wl_resource_create(client,
				 &zwp_primary_selection_device_v1_interface,
				 wl_resource_get_version(resource), id);
	if (!res) {
		free(device);
		wl_client_post_no_memory(client);
		return;
	}
	device->qdwin = qdwin;
	device->pseat = pseat;
	device->resource = res;
	wl_list_insert(&pseat->devices, &device->link);
	wl_resource_set_implementation(res, &qdwin_primary_device_impl,
				       device,
				       qdwin_primary_device_resource_destroy);
	/* If a selection is already active on this seat, announce it to
	 * the brand-new device. */
	if (pseat->current_source) {
		struct wl_resource *offer_res =
			qdwin_primary_build_offer_for_device(
				device, pseat->current_source);
		zwp_primary_selection_device_v1_send_selection(res, offer_res);
	}
}

static void
qdwin_primary_manager_destroy_req(struct wl_client *client,
				  struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static const struct zwp_primary_selection_device_manager_v1_interface
qdwin_primary_manager_impl = {
	.create_source = qdwin_primary_manager_create_source,
	.get_device    = qdwin_primary_manager_get_device,
	.destroy       = qdwin_primary_manager_destroy_req,
};

static void
bind_qdwin_primary_manager(struct wl_client *client, void *data,
			   uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource = wl_resource_create(
		client, &zwp_primary_selection_device_manager_v1_interface,
		version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource, &qdwin_primary_manager_impl,
				       qdwin, NULL);
}

static void
qdwin_primary_seat_seat_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_primary_seat *pseat =
		wl_container_of(l, pseat, seat_destroy_listener);
	struct qdwin_primary_device *device, *tmp;
	(void)data;
	wl_list_remove(&pseat->seat_destroy_listener.link);
	wl_list_init(&pseat->seat_destroy_listener.link);
	if (pseat->current_source)
		qdwin_primary_seat_clear_selection(pseat, 1);
	wl_list_for_each_safe(device, tmp, &pseat->devices, link)
		device->pseat = NULL;
	wl_list_remove(&pseat->link);
	free(pseat);
}

/* ------------------------------------------------------------------
 * R8/A4 output-loss / return-home boundary.
 *
 * Output destruction is a session boundary, not merely a geometry event:
 * the vanished display may have held an interactive move, popup grab, DND,
 * IME composition, pointer constraint, or clipboard content. Drain those
 * capabilities before relocating surfaces so neither a stuck input state nor
 * stale cross-machine data survives the return-home transition.
 * ------------------------------------------------------------------ */

static struct weston_output *
qdwin_output_boundary_survivor(struct qdwin *qdwin,
			       struct weston_output *removed)
{
	struct weston_output *out;
	wl_list_for_each(out, &qdwin->compositor->output_list, link) {
		if (out != removed && !out->destroying)
			return out;
	}
	return NULL;
}

static void
qdwin_output_boundary_cancel_state(struct qdwin *qdwin,
				   struct weston_output *removed)
{
	struct qdwin_toplevel *tl;
	struct qdwin_view_stream *stream;
	struct qdwin_layer_surface *ls;
	struct qdwin_input_method *im;
	struct weston_seat *seat;

	/* Popup objects own pointer grabs; destroy them before the generic DND
	 * cancellation below so no freed qdwin grab can remain installed. */
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		if (tl->popup)
			qdwin_popup_teardown(tl->popup);
	}
	wl_list_for_each(ls, &qdwin->layer_surfaces, link) {
		struct qdwin_layer_popup *lp, *tmp;
		wl_list_for_each_safe(lp, tmp, &ls->popups, link) {
			/* Send xdg_popup.popup_done through the vendored helper before
			 * dropping qdwin's wrapper; destroy alone only releases local
			 * state and would strand the client's popup role. Do NOT call the
			 * grab's cancel callback directly: that callback deliberately
			 * leaves pointer->grab installed for libweston's seat-teardown
			 * caller, so freeing lp afterwards would leave a dangling grab.
			 * qdwin_layer_popup_destroy sees grab_active and safely ends the
			 * pointer grab after this one protocol dismissal. */
			if (lp->popup_resource) {
				qdwin_xdg_popup_dismiss_layer_grab_fn dismiss =
					qdwin_xdg_popup_dismiss_layer_grab_sym();
				if (dismiss)
					dismiss(lp->popup_resource);
			}
			qdwin_layer_popup_destroy(lp);
		}
	}

	if (qdwin->move_grab_active)
		qdwin_move_grab_end_for(qdwin, qdwin->move_grab_handle);
	qdwin_switcher_grab_end(qdwin);
	qdwin_overlay_grab_end(qdwin);
	wl_list_for_each(stream, &qdwin->view_streams, link)
		qdwin_stream_confine_grab_end(stream);

	/* Deactivate composition and revoke its raw-keyboard grab. The IME may
	 * bind a fresh grab after focus is re-established on the survivor. */
	wl_list_for_each(im, &qdwin->input_methods, link) {
		if (im->active)
			qdwin_im_deactivate(im);
		if (im->grab && im->grab->resource)
			wl_resource_destroy(im->grab->resource);
	}

	wl_list_for_each(seat, &qdwin->compositor->seat_list, link) {
		struct weston_pointer *pointer = weston_seat_get_pointer(seat);
		struct weston_keyboard *keyboard = weston_seat_get_keyboard(seat);
		struct weston_touch *touch = weston_seat_get_touch(seat);

		/* A non-default grab left after qdwin's own grabs are gone is the
		 * libweston data-device drag (or another core grab). Its cancel
		 * callback performs the protocol cancellation and frees its state. */
		if (pointer && pointer->grab != &pointer->default_grab &&
		    pointer->grab->interface && pointer->grab->interface->cancel) {
			pointer->grab->interface->cancel(pointer->grab);
			if (pointer->grab != &pointer->default_grab)
				weston_pointer_end_grab(pointer);
		}
		if (touch && touch->grab != &touch->default_grab &&
		    touch->grab->interface && touch->grab->interface->cancel) {
			touch->grab->interface->cancel(touch->grab);
			if (touch->grab != &touch->default_grab)
				weston_touch_end_grab(touch);
		}
		if (keyboard && keyboard->grab != &keyboard->default_grab &&
		    keyboard->grab->interface && keyboard->grab->interface->cancel) {
			keyboard->grab->interface->cancel(keyboard->grab);
			if (keyboard->grab != &keyboard->default_grab)
				weston_keyboard_end_grab(keyboard);
		}

		/* leave/focus reset makes clients release locally-held input and
		 * cancels any focus-derived pointer constraint. */
		if (pointer)
			weston_pointer_set_focus(pointer, NULL);
		if (keyboard)
			weston_keyboard_set_focus(keyboard, NULL);
		if (touch)
			weston_touch_set_focus(touch, NULL);
		if (seat->output == removed)
			seat->output = NULL;

		/* Clipboard and primary selection are session scoped. Never carry
		 * their contents across an unmount/output-loss boundary. */
		if (seat->selection_data_source)
			weston_seat_set_selection(
				seat, NULL,
				wl_display_next_serial(qdwin->compositor->wl_display));
		struct qdwin_primary_seat *pseat =
			qdwin_primary_seat_find(qdwin, seat);
		if (pseat && qdwin_primary_seat_has_source(pseat))
			qdwin_primary_seat_clear_selection(pseat, 1);
	}
}

static void
qdwin_output_boundary_rehome_saved(struct qdwin_toplevel *tl,
				   struct weston_output *survivor,
				   double *content_x, double *content_y,
				   int outer_w, int outer_h)
{
	int wx, wy, ww, wh, x, y;
	qdwin_output_work_area(tl->qdwin, survivor, &wx, &wy, &ww, &wh);
	qdwin_rehome_outer_rect((int32_t)(*content_x - tl->inset_w),
				(int32_t)(*content_y - tl->inset_n),
				outer_w, outer_h, wx, wy, ww, wh, &x, &y);
	*content_x = x + tl->inset_w;
	*content_y = y + tl->inset_n;
}

static bool
qdwin_toplevel_belongs_to_output(struct qdwin_toplevel *tl,
				 struct weston_output *output)
{
	if (!tl || !tl->view || !output)
		return false;
	if (tl->view->output == output)
		return true;

	struct weston_coord_global pos =
		weston_view_get_pos_offset_global(tl->view);
	int w = tl->outer_width > 0 ? tl->outer_width : tl->last_width;
	int h = tl->outer_height > 0 ? tl->outer_height : tl->last_height;
	if (w <= 0) w = 1;
	if (h <= 0) h = 1;
	double cx = pos.c.x - tl->inset_w + (double)w / 2.0;
	double cy = pos.c.y - tl->inset_n + (double)h / 2.0;
	return cx >= output->pos.c.x &&
	       cx < output->pos.c.x + output->width &&
	       cy >= output->pos.c.y &&
	       cy < output->pos.c.y + output->height;
}

static void
qdwin_output_boundary_rehome_toplevel(struct qdwin *qdwin,
				      struct qdwin_toplevel *tl,
				      struct weston_output *survivor)
{
	struct weston_coord_global pos =
		weston_view_get_pos_offset_global(tl->view);
	int outer_w = tl->outer_width > 0 ? tl->outer_width : tl->last_width;
	int outer_h = tl->outer_height > 0 ? tl->outer_height : tl->last_height;
	if (outer_w <= 0) outer_w = 800;
	if (outer_h <= 0) outer_h = 600;

	/* Restore slots must move too, otherwise the next unmaximize/untile
	 * jumps straight back onto the vanished output. */
	if (tl->saved_outer_w > 0 && tl->saved_outer_h > 0)
		qdwin_output_boundary_rehome_saved(
			tl, survivor, &tl->saved_x, &tl->saved_y,
			tl->saved_outer_w, tl->saved_outer_h);
	if (tl->tile_saved_outer_w > 0 && tl->tile_saved_outer_h > 0)
		qdwin_output_boundary_rehome_saved(
			tl, survivor, &tl->tile_saved_x, &tl->tile_saved_y,
			tl->tile_saved_outer_w, tl->tile_saved_outer_h);

	weston_view_set_output(tl->view, survivor);
	if (tl->state & QDWIN_TS_FULLSCREEN) {
		qdwin_toplevel_apply_fullscreen_geometry(qdwin, tl, survivor);
	} else if (tl->state & QDWIN_TS_MAXIMIZED) {
		int wx, wy, ww, wh;
		qdwin_output_work_area(qdwin, survivor, &wx, &wy, &ww, &wh);
		tl->outer_width = ww;
		tl->outer_height = wh;
		if (tl->is_nested_proxy)
			qdwin_nested_proxy_set_geometry(tl, ww, wh);
		else
			weston_desktop_surface_set_maximized(tl->desktop_surface,
							true);
		struct weston_coord_global origin = {
			.c = weston_coord(wx + tl->inset_w, wy + tl->inset_n),
		};
		weston_view_set_position(tl->view, origin);
		qdwin_toplevel_apply_inset(tl);
		qdwin_toplevel_position_chrome(tl);
	} else if (tl->tiled == QDWIN_TILE_LEFT ||
		   tl->tiled == QDWIN_TILE_RIGHT) {
		int wx, wy, ww, wh;
		qdwin_output_work_area(qdwin, survivor, &wx, &wy, &ww, &wh);
		int left_w = ww / 2;
		int tile_w = tl->tiled == QDWIN_TILE_RIGHT ? ww - left_w : left_w;
		int tile_x = tl->tiled == QDWIN_TILE_RIGHT ? wx + left_w : wx;
		tl->outer_width = tile_w;
		tl->outer_height = wh;
		struct weston_coord_global origin = {
			.c = weston_coord(tile_x + tl->inset_w,
					  wy + tl->inset_n),
		};
		weston_view_set_position(tl->view, origin);
		if (tl->is_nested_proxy)
			qdwin_nested_proxy_set_geometry(tl, tile_w, wh);
		else
			qdwin_toplevel_apply_inset(tl);
		qdwin_toplevel_position_chrome(tl);
	} else {
		int wx, wy, ww, wh, x, y;
		qdwin_output_work_area(qdwin, survivor, &wx, &wy, &ww, &wh);
		qdwin_rehome_outer_rect((int32_t)(pos.c.x - tl->inset_w),
					(int32_t)(pos.c.y - tl->inset_n),
					outer_w, outer_h,
					wx, wy, ww, wh, &x, &y);
		struct weston_coord_global origin = {
			.c = weston_coord(x + tl->inset_w, y + tl->inset_n),
		};
		weston_view_set_position(tl->view, origin);
		qdwin_toplevel_apply_inset(tl);
		qdwin_toplevel_position_chrome(tl);
	}

	weston_view_update_transform(tl->view);
	if (qdwin->shell_bound && qdwin->shell_resource) {
		struct weston_coord_global now =
			weston_view_get_pos_offset_global(tl->view);
		qdwin_shell_v1_send_toplevel_geometry(
			qdwin->shell_resource, tl->handle,
			(int)(now.c.x - tl->inset_w),
			(int)(now.c.y - tl->inset_n),
			(uint32_t)(tl->outer_width > 0 ? tl->outer_width : outer_w),
			(uint32_t)(tl->outer_height > 0 ? tl->outer_height : outer_h));
	}
	weston_log("qdwin: output-boundary rehomed handle=%u to output=%s\n",
		   tl->handle, survivor->name ? survivor->name : "(unnamed)");
}

static void
qdwin_output_boundary_transition(struct qdwin *qdwin,
				 struct weston_output *removed)
{
	struct weston_output *survivor =
		qdwin_output_boundary_survivor(qdwin, removed);
	struct qdwin_panel *panel;
	struct qdwin_notification *notification;
	struct qdwin_launcher *launcher;
	struct qdwin_layer_surface *ls;
	struct qdwin_toplevel *tl;
	struct qdwin_view_stream *stream;

	qdwin_output_boundary_cancel_state(qdwin, removed);

	/* Eliminate every long-lived pointer to the dying output before any
	 * work-area calculation. Shell chrome and credential prompts move first
	 * so maximized/tiled applications see the survivor's real reserved area. */
	wl_list_for_each(panel, &qdwin->panels, link) {
		if (panel->output == removed) {
			panel->output = survivor;
			qdwin_panel_place(panel);
		}
	}
	wl_list_for_each(notification, &qdwin->notifications, link) {
		if (notification->output == removed) {
			notification->output = survivor;
			qdwin_notification_place(notification);
		}
	}
	wl_list_for_each(launcher, &qdwin->launchers, link) {
		if (launcher->output == removed) {
			launcher->output = survivor;
			qdwin_launcher_place(launcher);
		}
	}
	wl_list_for_each(ls, &qdwin->layer_surfaces, link) {
		if (!ls->output || ls->output == removed)
			ls->output = survivor;
		if (survivor && ls->output == survivor) {
			qdwin_layer_surface_send_configure(ls);
			if (ls->view)
				qdwin_layer_surface_apply(ls);
		}
	}

	/* A stream unpinned later must never restore its source view onto the
	 * removed output. The PipeWire target itself is handled by its normal
	 * backend teardown, but its restore destination is repaired here. */
	wl_list_for_each(stream, &qdwin->view_streams, link) {
		if (stream->prev_output == removed)
			stream->prev_output = survivor;
	}

	if (!survivor) {
		weston_log("qdwin: output-boundary output=%s: no survivor; "
			   "input/data state cleared, surface rescue deferred\n",
			   removed && removed->name ? removed->name : "(unnamed)");
		return;
	}

	wl_list_for_each(tl, &qdwin->toplevels, link) {
		if (qdwin_toplevel_belongs_to_output(tl, removed))
			qdwin_output_boundary_rehome_toplevel(qdwin, tl, survivor);
	}
	qdwin_refresh_background(qdwin);
	if (qdwin->shell_background) {
		qdwin_background_place(qdwin->shell_background);
		qdwin_background_send_geometry(qdwin->shell_background);
	}
	qdwin_lock_surface_place(qdwin);
	if (qdwin->locked)
		qdwin_install_lock_curtain(qdwin);
	weston_compositor_schedule_repaint(qdwin->compositor);
	weston_log("qdwin: output-boundary removed=%s survivor=%s complete\n",
		   removed && removed->name ? removed->name : "(unnamed)",
		   survivor->name ? survivor->name : "(unnamed)");
}

/* ------------------------------------------------------------------
 * §6.8 S0 qdwin_nested_v1 — stub.
 *
 * S0 ships bind + advertise_toplevel logging only. No proxy surface
 * creation yet. Rationale + staged plan in
 * . Shape-A (PipeWire per-
 * toplevel stream from nested → outer proxy surface) is locked in;
 * S1 wires the nested-side PipeWire publish, S2 wires the outer-side
 * proxy-surface creation + qdwin_shell_v1.toplevel_added integration,
 * S3 wires input injection via the companion PipeWire sink, S4 seals
 * lifecycle + authorization via admin-broker CheckPermission.
 * ------------------------------------------------------------------ */

/* Defined inside the §6.8 S0 nested block; stub closure of the forward
 * decl above. Lives here so the surface_added/_committed paths above
 * can reference it without needing the full nested_toplevel layout. */

struct qdwin_nested_toplevel {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	char *pw_node;       /* §6.8 S1 v2: string identifier */
	char *input_sink;    /* §6.8 S1 v2: string identifier */
	char *app_id;
	char *title;
	uint32_t origin_uid;
	uint32_t configured_w;
	uint32_t configured_h;
	/* §6.8 S2: the qdwin_toplevel proxy synthesised from this advertise.
	 * Owns the placeholder curtain + view in the outer's normal_layer.
	 * NULL until proxy creation succeeds; cleared on proxy teardown. */
	struct qdwin_toplevel *proxy_tl;
	struct wl_list link;  /* qdwin::nested_toplevels */
};

#define QDWIN_NESTED_TOPLEVEL_CAP_PER_UID 64u

static uint32_t
qdwin_nested_count_for_uid(struct qdwin *qdwin, uid_t uid)
{
	uint32_t n = 0;
	struct qdwin_nested_toplevel *t;

	wl_list_for_each(t, &qdwin->nested_toplevels, link) {
		if ((uid_t)t->origin_uid == uid)
			n++;
	}
	return n;
}

static void
qdwin_nested_toplevel_destroy_req(struct wl_client *client,
				  struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

/* §6.8 S2/S4: send close_requested on the nested-toplevel resource
 * owned by the given proxy. Defined here (after the full
 * qdwin_nested_toplevel struct) but called from request_close at the
 * top of the file via the forward decl. */
static void
qdwin_nested_proxy_send_close(struct qdwin_toplevel *tl)
{
	if (!tl || !tl->is_nested_proxy)
		return;
	if (tl->proxy_nested_owner && tl->proxy_nested_owner->resource)
		qdwin_nested_toplevel_v1_send_close_requested(
			tl->proxy_nested_owner->resource);
}

static void
qdwin_nested_toplevel_set_title(struct wl_client *client,
				struct wl_resource *resource,
				const char *title)
{
	struct qdwin_nested_toplevel *t =
		wl_resource_get_user_data(resource);
	(void)client;
	if (!t)
		return;
	free(t->title);
	t->title = qdwin_xstrdup_or_null(title);
	weston_log("qdwin: nested-toplevel set_title app_id=%s title=%s\n",
		   t->app_id ? t->app_id : "",
		   t->title ? t->title : "");
	if (t->proxy_tl)
		qdwin_nested_proxy_set_title(t->proxy_tl, t->title);
}

static void
qdwin_nested_toplevel_set_app_id(struct wl_client *client,
				 struct wl_resource *resource,
				 const char *app_id)
{
	struct qdwin_nested_toplevel *t =
		wl_resource_get_user_data(resource);
	(void)client;
	if (!t)
		return;
	free(t->app_id);
	t->app_id = qdwin_xstrdup_or_null(app_id);
	weston_log("qdwin: nested-toplevel set_app_id=%s\n",
		   t->app_id ? t->app_id : "");
	if (t->proxy_tl)
		qdwin_nested_proxy_set_app_id(t->proxy_tl, t->app_id);
}

static void
qdwin_nested_toplevel_set_geometry(struct wl_client *client,
				   struct wl_resource *resource,
				   int32_t w, int32_t h)
{
	struct qdwin_nested_toplevel *t =
		wl_resource_get_user_data(resource);
	(void)client;
	if (!t)
		return;
	t->configured_w = (w > 0) ? (uint32_t)w : 0;
	t->configured_h = (h > 0) ? (uint32_t)h : 0;
	weston_log("qdwin: nested-toplevel set_geometry=%dx%d\n", w, h);
	if (t->proxy_tl)
		qdwin_nested_proxy_set_geometry(t->proxy_tl, w, h);
}

static int
qdwin_remote_identity_text_valid(const char *text, int stream)
{
	if (!text)
		return 0;
	size_t length = strlen(text);
	if (!length || length > 128 || (stream && length < 16))
		return 0;
	for (size_t i = 0; i < length; i++) {
		unsigned char c = (unsigned char)text[i];
		int alpha_num = (c >= 'a' && c <= 'z') ||
			(c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
		if (!alpha_num && !(i > 0 && (c == '_' || c == '-' ||
		    (!stream && (c == '.' || c == ':')))))
			return 0;
	}
	return 1;
}

static void
qdwin_nested_toplevel_set_remote_identity(struct wl_client *client,
					   struct wl_resource *resource,
					   const char *source_machine,
					   const char *trust_domain_id,
					   const char *stream_id,
					   uint32_t generation_hi,
					   uint32_t generation_lo)
{
	struct qdwin_nested_toplevel *t = wl_resource_get_user_data(resource);
	if (!t || !t->proxy_tl)
		return;
	pid_t pid = 0;
	uid_t uid = 0;
	gid_t gid = 0;
	wl_client_get_credentials(client, &pid, &uid, &gid);
	(void)uid;
	(void)gid;
	char *peer_exe = qdwin_proc_exe(pid);
	int publisher_allowed =
		qdwin_remote_nested_publisher_allowed(peer_exe);
	free(peer_exe);
	if (!publisher_allowed) {
		wl_resource_post_error(resource,
			QDWIN_NESTED_TOPLEVEL_V1_ERROR_UNAUTHORIZED_REMOTE_IDENTITY,
			"remote identity publisher is not authorized");
		return;
	}
	uint64_t generation = ((uint64_t)generation_hi << 32) | generation_lo;
	if (!generation ||
	    !qdwin_remote_identity_text_valid(source_machine, 0) ||
	    !qdwin_remote_identity_text_valid(trust_domain_id, 0) ||
	    !qdwin_remote_identity_text_valid(stream_id, 1)) {
		wl_resource_post_error(resource,
			QDWIN_NESTED_TOPLEVEL_V1_ERROR_INVALID_REMOTE_IDENTITY,
			"remote identity fields are invalid");
		return;
	}
	struct qdwin_toplevel *tl = t->proxy_tl;
	if (tl->proxy_remote_source_machine) {
		if (tl->proxy_remote_generation == generation &&
		    strcmp(tl->proxy_remote_source_machine, source_machine) == 0 &&
		    strcmp(tl->proxy_remote_trust_domain_id, trust_domain_id) == 0 &&
		    strcmp(tl->proxy_remote_stream_id, stream_id) == 0)
			return;
		wl_resource_post_error(resource,
			QDWIN_NESTED_TOPLEVEL_V1_ERROR_REMOTE_IDENTITY_IMMUTABLE,
			"remote identity cannot change");
		return;
	}
	char *source_copy = qdwin_xstrdup_or_null(source_machine);
	char *trust_copy = qdwin_xstrdup_or_null(trust_domain_id);
	char *stream_copy = qdwin_xstrdup_or_null(stream_id);
	if (!source_copy || !trust_copy || !stream_copy) {
		free(source_copy);
		free(trust_copy);
		free(stream_copy);
		wl_client_post_no_memory(client);
		return;
	}
	tl->proxy_remote_source_machine = source_copy;
	tl->proxy_remote_trust_domain_id = trust_copy;
	tl->proxy_remote_stream_id = stream_copy;
	tl->proxy_remote_generation = generation;
	weston_log("qdwin/nested-proxy: verified remote identity handle=%u "
		   "source=%s trust_domain=%s stream=%s generation=%llu\n",
		   tl->handle, source_copy, trust_copy, stream_copy,
		   (unsigned long long)generation);
	qdwin_emit_nested_proxy_remote_identity(tl->qdwin, tl);
}

static const struct qdwin_nested_toplevel_v1_interface
qdwin_nested_toplevel_impl = {
	.destroy      = qdwin_nested_toplevel_destroy_req,
	.set_title    = qdwin_nested_toplevel_set_title,
	.set_app_id   = qdwin_nested_toplevel_set_app_id,
	.set_geometry = qdwin_nested_toplevel_set_geometry,
	.set_remote_identity = qdwin_nested_toplevel_set_remote_identity,
};

static void
qdwin_nested_toplevel_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_nested_toplevel *t =
		wl_resource_get_user_data(resource);
	if (!t)
		return;
	if (t->proxy_tl) {
		t->proxy_tl->proxy_nested_owner = NULL;
		qdwin_nested_proxy_destroy(t->proxy_tl);
		t->proxy_tl = NULL;
	}
	wl_list_remove(&t->link);
	free(t->app_id);
	free(t->title);
	free(t->pw_node);
	free(t->input_sink);
	free(t);
}

static void
qdwin_nested_manager_destroy_req(struct wl_client *client,
				 struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

/* §6.8 S3d route-test (env-gated QDWIN_NESTED_S3D_TEST) — DEFERRED.
 *
 * The deterministic, ydotool-independent F6#2 routing oracle. It must run
 * AFTER a repaint: weston_compositor_pick_view() walks compositor->view_list,
 * which is rebuilt lazily at repaint — running synchronously inside
 * advertise_toplevel would read a stale list that does not yet include the
 * just-created proxy curtain (observed: pick_matched=0). So advertise schedules
 * a repaint + a one-shot timer; this fires once the list is current.
 *
 * Unlike S3b/S3c (which set active_input_proxy DIRECTLY), this drives the REAL
 * path: it aims at the proxy view's centre, lets the actual picker resolve it
 * (pick_matched proves the phantom is truly gone AND the placeholder is
 * input-pickable), focuses the picker's result, runs the same
 * qdwin_proxy_pointer_track_focus the default grab uses, then injects a button
 * via notify_button so the default-grab button handler encodes a QDNI button to
 * the inner sink. Pre-test focus + active proxy are restored afterwards. */
struct qdwin_s3d_ctx {
	struct qdwin *qdwin;
	uint32_t handle;
	struct wl_event_source *timer;
	/* F6#2 post-allow lane: phase 0 = first fire (pending-check, and — if
	 * QDWIN_NESTED_S3D_ALLOW_AFTER_PENDING=1 — apply allow then re-arm a
	 * phase-1 timer); phase 1 = post-allow routing assertion on the SAME
	 * handle. 0 for the plain s3d/s3e single-fire lanes. */
	int phase;
	/* Phase-0 allow-after-pending: how many times we re-armed phase-0 waiting
	 * for the probe's bind_proxy_pixels to STASH a surface before applying
	 * allow. Bounded so a never-binding test can't spin forever. */
	int allow_poll_tries;
	/* J29 14->16: weston_compositor_pick_view() walks compositor->view_list,
	 * which is rebuilt ONLY inside an output repaint (build_view_list is
	 * static — not callable here). On 16-headless a single schedule_repaint
	 * may not land a repaint within one 500ms window, so the pick can read a
	 * stale list and return null on a view that IS present. Retry the fire a
	 * bounded number of times (re-dirtying + re-scheduling a repaint each
	 * round) before asserting, so a transient stale list is not a failure but
	 * a genuinely-absent view still fails. */
	int pick_retries;
};

static int qdwin_s3d_route_test_fire(void *data);

/* Arm a one-shot S3d test timer (~500ms) after forcing a repaint so the
 * deferred fire runs against a current view_list. Frees ctx + returns false on
 * failure so the caller never leaks. */
static bool
qdwin_s3d_arm_timer(struct qdwin_s3d_ctx *ctx)
{
	ctx->timer = wl_event_loop_add_timer(
		wl_display_get_event_loop(ctx->qdwin->compositor->wl_display),
		qdwin_s3d_route_test_fire, ctx);
	if (!ctx->timer) {
		free(ctx);
		return false;
	}
	weston_compositor_schedule_repaint(ctx->qdwin->compositor);
	wl_event_source_timer_update(ctx->timer, 500);
	return true;
}

static int
qdwin_s3d_route_test_fire(void *data)
{
	struct qdwin_s3d_ctx *ctx = data;
	struct qdwin *qdwin = ctx->qdwin;
	struct qdwin_toplevel *tl = qdwin_toplevel_from_handle(qdwin, ctx->handle);
	int routed = 0, pick_matched = 0, matched = 0;

	if (tl && tl->is_nested_proxy && tl->view) {
		/* Ask the REAL picker what is at the proxy view's centre. */
		weston_view_update_transform(tl->view);
		struct weston_coord_global vpos =
			weston_view_get_pos_offset_global(tl->view);
		struct weston_coord_global center = { .c = weston_coord(
			vpos.c.x + tl->last_width  / 2.0,
			vpos.c.y + tl->last_height / 2.0) };
		struct weston_view *picked =
			weston_compositor_pick_view(qdwin->compositor, center);
		/* Diagnostic: describe what pick_view returned (proxy / null /
		 * cursor sprite / other-role) so a failure is one-shot debuggable. */
		const char *picked_desc;
		if (!picked)
			picked_desc = "(null)";
		else if (picked == tl->view)
			picked_desc = "proxy";
		else {
			const char *r = picked->surface ?
				weston_surface_get_role(picked->surface) : NULL;
			picked_desc = r ? r : "(other,no-role)";
		}

		if (tl->nested_proxy_pending_decision) {
			/* Security invariant (mechanism A): a PENDING/unapproved
			 * proxy must NOT be input-pickable — even if a pixel feed
			 * was bound while pending (it is stashed; the active view
			 * stays the empty-input curtain). pick_view must NOT return
			 * the proxy view. */
			int pending_unpickable = (picked != tl->view);
			weston_log("qdwin/nested-proxy: S3d pending-check handle=%u "
				   "picked=%s center=(%.0f,%.0f) "
				   "pending_unpickable=%d\n",
				   ctx->handle, picked_desc,
				   center.c.x, center.c.y, pending_unpickable);

			/* F6#2 post-allow lane (env QDWIN_NESTED_S3D_ALLOW_AFTER_
			 * PENDING=1): on the FIRST fire only, after the pending
			 * pick-miss is recorded, drive the SAME proxy through the
			 * REAL allow mechanism (qdwin_nested_proxy_apply_allow —
			 * the identical code the shell decision=allow handler runs)
			 * and re-arm a phase-1 timer that re-runs the visible
			 * routing branch on this same handle. This proves the
			 * pending->allow->pickable+routes transition without a v9
			 * qdshell/broker on the wire. It does NOT alter the pending
			 * security invariant: the proxy was already verified
			 * unpickable above; allow is the production transition.
			 * Gated on phase==0 so a stuck/idempotent state can't loop. */
			if (ctx->phase == 0 &&
			    getenv("QDWIN_NESTED_S3D_ALLOW_AFTER_PENDING")) {
				/* Capture handle BEFORE any arm call — the arm
				 * helper frees ctx on failure, so reading
				 * ctx->handle afterwards would be a use-after-free.
				 */
				uint32_t h = ctx->handle;
				/* Determinism gate: only apply allow once the
				 * probe's bind_proxy_pixels has STASHED a surface
				 * (proxy_pixel_surface set, no view yet). Otherwise
				 * the allow could race ahead of the bind and the
				 * lane would never exercise the stash->allow->
				 * activate path. If not yet stashed, re-arm phase 0
				 * (bounded) to poll. */
				bool stashed = (tl->proxy_pixel_surface &&
						!tl->proxy_pixel_view);
				if (!stashed) {
					if (ctx->allow_poll_tries < 40) {
						ctx->allow_poll_tries++;
						wl_event_source_remove(ctx->timer);
						ctx->timer = NULL;
						if (!qdwin_s3d_arm_timer(ctx))
							weston_log("qdwin/nested-"
								   "proxy: S3d allow-"
								   "after-pending "
								   "handle=%u poll re-arm "
								   "FAILED\n", h);
						return 0;
					}
					/* Poll budget exhausted with no stash: the
					 * probe never bound a pixel surface in time.
					 * ABORT the test transition — do NOT apply
					 * allow (that would contradict the determinism
					 * gate and leave a stashed=0 transition the
					 * probe doesn't assert). The proxy stays
					 * PENDING/unpickable (fail-closed); the probe's
					 * stashed=1 assertion then fails loudly. */
					weston_log("qdwin/nested-proxy: S3d allow-"
						   "after-pending handle=%u — no stash "
						   "after %d polls; ABORTING test "
						   "transition (proxy stays pending)\n",
						   h, ctx->allow_poll_tries);
					goto s3d_fire_done;
				}
				weston_log("qdwin/nested-proxy: S3d allow-after-"
					   "pending handle=%u stashed=1 — "
					   "applying allow\n", h);
				qdwin_nested_proxy_apply_allow(
					tl, "s3d-test/allow-after-pending");
				ctx->phase = 1;
				/* Re-arm a second deferred fire (post-repaint)
				 * for the routing assertion on the now-approved
				 * proxy. On arm success the ctx is reused — skip
				 * the free/remove at the bottom by returning here
				 * after detaching the spent timer. */
				wl_event_source_remove(ctx->timer);
				ctx->timer = NULL;
				if (!qdwin_s3d_arm_timer(ctx)) {
					weston_log("qdwin/nested-proxy: S3d "
						   "allow-after-pending handle=%u "
						   "re-arm FAILED\n", h);
					/* ctx already freed by arm helper. */
				}
				return 0;
			}
		} else {
			/* Visible/approved: drive the real routing path. */
			/* J29 14->16: if the pick came back null, the view_list may
			 * simply be stale (rebuilt only at repaint; see pick_retries).
			 * Re-dirty the proxy view (sets view_list_needs_rebuild +
			 * schedules a repaint) and re-arm rather than failing on a
			 * transient stale list. Bounded so a truly-absent view fails. */
			if (!picked && ctx->pick_retries < 3) {
				ctx->pick_retries++;
				/* Distinct "route-retry" prefix (NOT "route-test") so the
				 * probe's result grep is never satisfied by a re-arm. Give
				 * a real (repaint-driven) session a few chances to rebuild
				 * the view_list before the geometric fallback below. */
				weston_log("qdwin/nested-proxy: S3d route-retry handle=%u "
					   "pick null — re-arming (retry %d/3)\n",
					   ctx->handle, ctx->pick_retries);
				weston_view_geometry_dirty(tl->view);
				weston_compositor_damage_all(qdwin->compositor);
				wl_event_source_timer_update(ctx->timer, 200);
				return 0;
			}
			if (!picked) {
				/* On 16-headless the compositor never runs an output
				 * repaint on its own (no frame clock / no client commits
				 * driving it), and weston_compositor_pick_view() walks
				 * compositor->view_list, which is rebuilt ONLY inside a
				 * repaint (build_view_list is static, uncallable here). So a
				 * correctly mapped/positioned/layered proxy is simply absent
				 * from the perpetually-stale list and the real picker returns
				 * null forever (verified: needs_rebuild stays 1 across forced
				 * damage_all + schedule_repaint). Fall back to pick_view's OWN
				 * containment + input test applied directly to the proxy view,
				 * so the substantive property — the placeholder is input-
				 * pickable at its centre — is still verified. */
				weston_view_update_transform(tl->view);
				if (pixman_region32_contains_point(
					    &tl->view->transform.boundingbox,
					    (int)center.c.x, (int)center.c.y, NULL)) {
					struct weston_coord_surface sp =
						weston_coord_global_to_surface(tl->view, center);
					/* Same test pick_view applies, but on the surface's
					 * public input region directly (the internal
					 * weston_view_takes_input_at_point is not exported). */
					if (pixman_region32_contains_point(
						    &tl->view->surface->input,
						    (int)sp.c.x, (int)sp.c.y, NULL)) {
						picked = tl->view;
						picked_desc = "proxy(geom)";
						weston_log("qdwin/nested-proxy: S3d handle=%u "
							   "pick_view list stale (headless "
							   "no-repaint) — proxy geometrically "
							   "pickable at centre; treating as "
							   "picked\n", ctx->handle);
					}
				}
			}
			pick_matched = (picked == tl->view);
			struct weston_seat *seat;
			struct timespec ts = {0, 0};
			wl_list_for_each(seat, &qdwin->compositor->seat_list,
					 link) {
				struct weston_pointer *ptr =
					weston_seat_get_pointer(seat);
				if (!ptr)
					continue;
				struct weston_view *saved_focus = ptr->focus;
				struct qdwin_toplevel *saved_proxy =
					qdwin->active_input_proxy;
				weston_pointer_set_focus(ptr, picked);
				qdwin_proxy_pointer_track_focus(qdwin, ptr);
				matched = (qdwin->active_input_proxy == tl);
				qdwin_notify_button(seat, &ts, 0x110 /*BTN_LEFT*/,
					      WL_POINTER_BUTTON_STATE_PRESSED);
				qdwin_notify_button(seat, &ts, 0x110,
					      WL_POINTER_BUTTON_STATE_RELEASED);
				qdwin->active_input_proxy = saved_proxy;
				weston_pointer_set_focus(ptr, saved_focus);
				routed = 1;
				break;
			}
			weston_log("qdwin/nested-proxy: S3d route-test handle=%u "
				   "routed=%d picked=%s center=(%.0f,%.0f) "
				   "pick_matched=%d "
				   "active_input_proxy_matched=%d\n",
				   ctx->handle, routed, picked_desc,
				   center.c.x, center.c.y,
				   pick_matched, matched);
		}
	} else {
		weston_log("qdwin/nested-proxy: S3d route-test handle=%u "
			   "proxy gone before fire\n", ctx->handle);
	}
s3d_fire_done:
	wl_event_source_remove(ctx->timer);
	free(ctx);
	return 0;
}

static void
qdwin_nested_manager_advertise_toplevel(struct wl_client *client,
					struct wl_resource *resource,
					uint32_t id,
					const char *pw_node,
					const char *input_sink,
					const char *app_id,
					const char *title,
					uint32_t origin_uid)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);

	/* Nested-proxy identity hardening: origin_uid is client-asserted and
	 * feeds the per-uid colour chip + the broker authz decision, so it
	 * must NOT be taken on the client's word. The advertising client is
	 * the nested compositor (bound peer-uid-filtered to allowed_uid); in
	 * the per-uid tier-2 model every inner client it can advertise runs
	 * as that same uid. Bind origin_uid to the advertising client's
	 * kernel-resolved peer uid: if the client asserts a different uid,
	 * override it (and log the spoof attempt). Fail closed if the peer
	 * credential is unreadable. */
	pid_t peer_pid = 0; uid_t peer_uid = 0; gid_t peer_gid = 0;
	wl_client_get_credentials(client, &peer_pid, &peer_uid, &peer_gid);
	(void)peer_pid; (void)peer_gid;
	if (origin_uid != (uint32_t)peer_uid) {
		weston_log("qdwin: nested-toplevel advertise origin_uid=%u "
			   "disagrees with advertising client peer uid=%u; "
			   "overriding to peer uid (client cannot assert a "
			   "foreign origin_uid)\n",
			   origin_uid, (unsigned)peer_uid);
		origin_uid = (uint32_t)peer_uid;
	}

	uint32_t advertised = qdwin_nested_count_for_uid(qdwin, peer_uid);
	if (advertised >= QDWIN_NESTED_TOPLEVEL_CAP_PER_UID) {
		weston_log("qdwin: nested-toplevel advertise refused uid=%u "
			   "count=%u cap=%u\n",
			   (unsigned)peer_uid, advertised,
			   QDWIN_NESTED_TOPLEVEL_CAP_PER_UID);
		wl_resource_post_error(
			resource,
			QDWIN_NESTED_MANAGER_V1_ERROR_POLICY_DENIED,
			"nested toplevel advertise cap reached for uid %u",
			(unsigned)peer_uid);
		return;
	}

	struct wl_resource *tl_res = wl_resource_create(
		client, &qdwin_nested_toplevel_v1_interface,
		wl_resource_get_version(resource), id);
	if (!tl_res) {
		wl_client_post_no_memory(client);
		return;
	}
	struct qdwin_nested_toplevel *t = calloc(1, sizeof *t);
	if (!t) {
		wl_resource_destroy(tl_res);
		wl_client_post_no_memory(client);
		return;
	}
	t->qdwin       = qdwin;
	t->resource    = tl_res;
	t->pw_node     = qdwin_xstrdup_or_null(pw_node);
	t->input_sink  = qdwin_xstrdup_or_null(input_sink);
	t->app_id      = qdwin_xstrdup_or_null(app_id);
	t->title       = qdwin_xstrdup_or_null(title);
	/* origin_uid here is the verified peer uid (see above), not the raw
	 * client assertion. */
	t->origin_uid  = origin_uid;
	wl_list_insert(&qdwin->nested_toplevels, &t->link);

	wl_resource_set_implementation(tl_res,
				       &qdwin_nested_toplevel_impl, t,
				       qdwin_nested_toplevel_resource_destroy);

	weston_log("qdwin: nested-toplevel advertise pw_node='%s' "
		   "input_sink='%s' app_id=%s title=%s origin_uid=%u "
		   "(peer uid=%u)\n",
		   t->pw_node    ? t->pw_node    : "",
		   t->input_sink ? t->input_sink : "",
		   t->app_id     ? t->app_id     : "",
		   t->title      ? t->title      : "",
		   origin_uid, (unsigned)peer_uid);

	/* §6.8 S2: synthesise the outer-side proxy toplevel + curtain. */
	t->proxy_tl = qdwin_nested_proxy_create(qdwin, t,
					       t->app_id, t->title,
					       origin_uid, 800, 600);
	/* §6.8 S3: connect to the nested-published input sink and send
	 * the wire-format-proving PING. Real motion/button/key encoders
	 * arrive in S3b. The fd persists on the proxy until destroy. */
	if (t->proxy_tl) {
		t->proxy_tl->proxy_input_sink_fd = -1;
		if (input_sink && *input_sink) {
			int fd = qdwin_nested_input_sink_connect(input_sink);
			/* Nested-proxy identity hardening: input_sink is a
			 * client-asserted socket path that the outer side
			 * *writes synthesised input into*. The path embeds a
			 * /run/user/<uid>/ segment the client controls, so a
			 * malicious advertiser could point it at another uid's
			 * listener. Bind the sink to the advertising client's
			 * verified identity: the connected peer's uid (via
			 * SO_PEERCRED) must equal the verified origin_uid
			 * (== advertising client peer uid). Reject on mismatch
			 * or unreadable creds (fail closed: treat the proxy as
			 * display-only rather than routing input to a sink we
			 * can't attribute to the advertiser). */
			if (fd >= 0) {
				uid_t sink_uid = 0;
				if (!qdwin_fd_peer_uid(fd, &sink_uid) ||
				    sink_uid != (uid_t)origin_uid) {
					weston_log("qdwin/nested-proxy: "
						   "input-sink peer uid=%u does "
						   "not match origin uid=%u "
						   "(handle=%u path=%s) — "
						   "refusing sink, proxy is "
						   "display-only\n",
						   (unsigned)sink_uid,
						   (unsigned)origin_uid,
						   t->proxy_tl->handle,
						   input_sink);
					close(fd);
					fd = -1;
				}
			}
			if (fd >= 0) {
				t->proxy_tl->proxy_input_sink_fd = fd;
				if (qdwin_nested_input_sink_send(fd, 1, NULL, 0) == 0)
					weston_log("qdwin/nested-proxy: "
						   "input-sink PING sent "
						   "handle=%u path=%s\n",
						   t->proxy_tl->handle,
						   input_sink);
				/* §6.8 S3b synthetic injection (env-gated):
				 * sends a motion/button/key/axis/focus burst
				 * for the bats decoder test. Production input
				 * forwarding goes through the pointer/keyboard
				 * grab paths, not this one. */
				if (getenv("QDWIN_NESTED_S3B_TEST")) {
					qdwin_nested_input_sink_send_focus(
						fd, 1);
					qdwin_nested_input_sink_send_motion(
						fd, 0,
						wl_fixed_from_int(120),
						wl_fixed_from_int(80));
					qdwin_nested_input_sink_send_button(
						fd, 0, 0x110 /*BTN_LEFT*/, 1);
					qdwin_nested_input_sink_send_button(
						fd, 0, 0x110, 0);
					qdwin_nested_input_sink_send_key(
						fd, 0, 30 /*KEY_A*/, 1);
					qdwin_nested_input_sink_send_key(
						fd, 0, 30, 0);
					qdwin_nested_input_sink_send_axis(
						fd, 0, 0 /*vert*/,
						wl_fixed_from_int(15));
					qdwin_nested_input_sink_send_focus(
						fd, 0);
					weston_log("qdwin/nested-proxy: "
						   "S3b synthetic burst sent "
						   "handle=%u\n",
						   t->proxy_tl->handle);
				}
				/* §6.8 S3c keyboard-grab synthetic test
				 * (env-gated): forces active_input_proxy to
				 * this proxy so the default keyboard grab
				 * encodes QDNI keys, then drives notify_key
				 * on the first seat with a keyboard. The
				 * grab callback's QDNI emission is the
				 * acceptance criterion — nested decodes a
				 * key whose wire entry was a real keyboard
				 * grab call rather than a direct send_key.
				 *
				 * Uses KEY_S (31) so it's distinct from the
				 * S3b KEY_A (30) burst, even when both env
				 * vars happen to be set in the same run. */
				if (getenv("QDWIN_NESTED_S3C_TEST")) {
					struct qdwin_toplevel *saved =
						qdwin->active_input_proxy;
					qdwin_nested_input_sink_send_focus(
						fd, 1);
					qdwin->active_input_proxy = t->proxy_tl;
					struct weston_seat *seat;
					struct timespec ts = {0, 0};
					int dispatched = 0;
					wl_list_for_each(seat,
						&qdwin->compositor->seat_list,
						link) {
						struct weston_keyboard *kb =
							weston_seat_get_keyboard(seat);
						if (!kb)
							continue;
						qdwin_notify_key(seat, &ts,
							31 /*KEY_S*/,
							WL_KEYBOARD_KEY_STATE_PRESSED,
							STATE_UPDATE_AUTOMATIC);
						qdwin_notify_key(seat, &ts,
							31,
							WL_KEYBOARD_KEY_STATE_RELEASED,
							STATE_UPDATE_AUTOMATIC);
						dispatched = 1;
						break;
					}
					qdwin_nested_input_sink_send_focus(
						fd, 0);
					qdwin->active_input_proxy = saved;
					weston_log("qdwin/nested-proxy: "
						   "S3c keyboard-grab burst "
						   "handle=%u dispatched=%d\n",
						   t->proxy_tl->handle,
						   dispatched);
				}
				/* §6.8 S3d route-test (env-gated) — DEFERRED to a
				 * one-shot timer (see qdwin_s3d_route_test_fire
				 * above). pick_view must run AFTER the view_list
				 * rebuilds at the next repaint; a synchronous pick
				 * here reads a stale list missing the just-created
				 * proxy curtain. schedule_repaint forces that repaint
				 * promptly so the timer fires against a current list. */
				if (getenv("QDWIN_NESTED_S3D_TEST") &&
				    t->proxy_tl && t->proxy_tl->view) {
					struct qdwin_s3d_ctx *s3d =
						calloc(1, sizeof *s3d);
					if (s3d) {
						s3d->qdwin = qdwin;
						s3d->handle = t->proxy_tl->handle;
						s3d->phase = 0;
						(void)qdwin_s3d_arm_timer(s3d);
					}
				}
			} else {
				weston_log("qdwin/nested-proxy: input-sink "
					   "connect failed handle=%u path=%s\n",
					   t->proxy_tl->handle, input_sink);
			}
		}
	}
	int cw = t->proxy_tl ? t->proxy_tl->last_width  : 800;
	int ch = t->proxy_tl ? t->proxy_tl->last_height : 600;

	qdwin_nested_toplevel_v1_send_configured(tl_res, cw, ch);
	t->configured_w = cw;
	t->configured_h = ch;
}

static const struct qdwin_nested_manager_v1_interface
qdwin_nested_manager_impl = {
	.destroy            = qdwin_nested_manager_destroy_req,
	.advertise_toplevel = qdwin_nested_manager_advertise_toplevel,
};

/* §P10: callback referenced only when role=host; see bind_qdwin_locker
 * note above. */
__attribute__((unused))
static void
bind_qdwin_nested_manager(struct wl_client *client, void *data,
			  uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct qdwin_secctx_client *sc =
		qdwin_secctx_client_find(qdwin, client);

	if (sc && !qdwin_nested_secctx_publisher_allowed(
			  qdwin_secctx_client_engine(sc),
			  qdwin_secctx_client_peer_exe(sc))) {
		weston_log("qdwin: nested bind refused for secctx client "
			   "engine=%s exe=%s\n",
			   qdwin_secctx_client_engine(sc),
			   qdwin_secctx_client_peer_exe(sc));
		wl_client_post_implementation_error(
			client,
			"qdwin_nested: secctx client is not an authorized "
			"nested publisher");
		return;
	}

	/* Peer-uid filter: same rule as qdwin_shell_v1 — only the
	 * allowed_uid may bind. Nested-compositor clients run as the
	 * user hosting the session. */
	if (qdwin->allowed_uid != (uid_t)-1) {
		uid_t uid;
		pid_t pid;
		gid_t gid;
		wl_client_get_credentials(client, &pid, &uid, &gid);
		if (uid != qdwin->allowed_uid) {
			weston_log("qdwin: nested bind refused uid=%u (allowed=%u)\n",
				   (unsigned)uid, (unsigned)qdwin->allowed_uid);
			wl_client_post_implementation_error(
				client,
				"qdwin_nested: peer-uid filter rejected bind");
			return;
		}
	}

	struct wl_resource *resource = wl_resource_create(
		client, &qdwin_nested_manager_v1_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource,
				       &qdwin_nested_manager_impl,
				       qdwin, NULL);
	weston_log("qdwin: nested_manager bound v%u\n", version);
}

/* ------------------------------------------------------------------
 * §6.7 xdg-activation-v1.
 *
 * Focus-stealing-prevention + launcher tokens. Two interfaces:
 *   xdg_activation_v1        — one global; issues tokens, consumes
 *                              them in activate(token, surface).
 *   xdg_activation_token_v1  — per-get_activation_token handle; the
 *                              client accumulates (serial, seat,
 *                              app_id, requesting_surface) via
 *                              set_*, then commits to receive `done`.
 *
 * Policy today: tokens are single-use and anonymous. Any valid
 * token grants activation; the compositor raises the target
 * toplevel to the normal layer's top. This matches weston's own
 * behaviour at this version. A future policy gate can reject
 * tokens by requester-uid, seat staleness, or app_id mismatch
 * without changing the wire protocol.
 * ------------------------------------------------------------------ */

struct qdwin_activation_token {
	struct qdwin *qdwin;
	struct wl_resource *token_resource;  /* xdg_activation_token_v1 */
	char *token;                         /* NULL until commit */
	char *app_id;
	struct weston_surface *requesting_surface;
	struct wl_listener requesting_surface_destroy;
	uint32_t serial;
	struct wl_list link;                 /* qdwin::activation_tokens */
	int committed;
	int used;
};

/* Defined below the qdwin_activation_pending struct — clears any
 * `ap->token == t` back-references so freeing `t` here can't leave
 * a dangling pointer on the pending list. */
static void qdwin_activation_pending_drop_token_refs(
	struct qdwin *qdwin, struct qdwin_activation_token *t);

static void
qdwin_activation_token_free(struct qdwin_activation_token *t)
{
	wl_list_remove(&t->link);
	wl_list_init(&t->link);
	if (t->requesting_surface) {
		wl_list_remove(&t->requesting_surface_destroy.link);
		wl_list_init(&t->requesting_surface_destroy.link);
		t->requesting_surface = NULL;
	}
	/* Sever the wl_resource → t back-reference. Many call paths
	 * (perform/use/timeout/cancel) free `t` while the client's
	 * xdg_activation_token_v1 resource is still alive; when the
	 * client later destroys it, qdwin_activation_token_resource_destroy
	 * fires and dereferences the user_data. Without this NULL-out,
	 * that destructor re-enters this function on a freed `t` and
	 * SEGVs in wl_list_remove. */
	if (t->token_resource)
		wl_resource_set_user_data(t->token_resource, NULL);
	/* The token may also be referenced from a pending activation
	 * (`ap->token`) that's still waiting on a shell decision or
	 * its 10s safety timeout. If the client destroys the token
	 * resource OR a perform/cancel path frees `t` while an `ap`
	 * still owns it, the next `qdwin_activation_pending_free`
	 * would call `qdwin_activation_token_free(ap->token)` on the
	 * already-freed `t` and SEGV in `wl_list_remove(&t->link)`.
	 * The pending-struct layout is defined further down; defer
	 * the back-reference scrub to a helper below. */
	qdwin_activation_pending_drop_token_refs(t->qdwin, t);
	free(t->token);
	free(t->app_id);
	free(t);
}

static struct qdwin_activation_token *
qdwin_activation_token_find(struct qdwin *qdwin, const char *token)
{
	struct qdwin_activation_token *t;
	if (!token)
		return NULL;
	wl_list_for_each(t, &qdwin->activation_tokens, link) {
		if (t->token && !t->used && strcmp(t->token, token) == 0)
			return t;
	}
	return NULL;
}

static int
qdwin_generate_token(char out[33])
{
	unsigned char raw[16];
	static const char hex[] = "0123456789abcdef";
	/* EINTR/short reads are retried inside qdwin_getrandom_full. On a
	 * hard failure there is NO fallback: a time-seeded token is
	 * guessable and the token gates focus stealing. Return -1 and let
	 * the caller deny the activation (iso2 `11` E4). */
	if (qdwin_getrandom_full(raw, sizeof raw) < 0) {
		out[0] = '\0';
		return -1;
	}
	for (int i = 0; i < 16; i++) {
		out[i * 2]     = hex[(raw[i] >> 4) & 0xf];
		out[i * 2 + 1] = hex[raw[i]        & 0xf];
	}
	out[32] = '\0';
	return 0;
}

static void
qdwin_activation_requesting_surface_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_activation_token *t =
		wl_container_of(l, t, requesting_surface_destroy);
	(void)data;
	wl_list_remove(&t->requesting_surface_destroy.link);
	wl_list_init(&t->requesting_surface_destroy.link);
	t->requesting_surface = NULL;
}

static void
qdwin_activation_token_destroy(struct wl_client *client,
			       struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_activation_token_set_serial(struct wl_client *client,
				  struct wl_resource *resource,
				  uint32_t serial,
				  struct wl_resource *seat_resource)
{
	struct qdwin_activation_token *t = wl_resource_get_user_data(resource);
	(void)client;
	(void)seat_resource;
	if (!t || t->committed)
		return;
	t->serial = serial;
}

static void
qdwin_activation_token_set_app_id(struct wl_client *client,
				  struct wl_resource *resource,
				  const char *app_id)
{
	struct qdwin_activation_token *t = wl_resource_get_user_data(resource);
	(void)client;
	if (!t || t->committed)
		return;
	free(t->app_id);
	t->app_id = qdwin_xstrdup_or_null(app_id);
}

static void
qdwin_activation_token_set_surface(struct wl_client *client,
				   struct wl_resource *resource,
				   struct wl_resource *surface_resource)
{
	struct qdwin_activation_token *t = wl_resource_get_user_data(resource);
	struct weston_surface *surface =
		surface_resource ? wl_resource_get_user_data(surface_resource)
				 : NULL;
	(void)client;
	if (!t || t->committed)
		return;
	if (t->requesting_surface) {
		wl_list_remove(&t->requesting_surface_destroy.link);
		wl_list_init(&t->requesting_surface_destroy.link);
		t->requesting_surface = NULL;
	}
	if (surface) {
		t->requesting_surface = surface;
		t->requesting_surface_destroy.notify =
			qdwin_activation_requesting_surface_destroyed;
		wl_signal_add(&surface->destroy_signal,
			      &t->requesting_surface_destroy);
	}
}

static void
qdwin_activation_token_commit(struct wl_client *client,
			      struct wl_resource *resource)
{
	struct qdwin_activation_token *t = wl_resource_get_user_data(resource);
	char token_buf[33];
	(void)client;
	if (!t || t->committed)
		return;
	free(t->token);
	if (qdwin_generate_token(token_buf) < 0) {
		weston_log("qdwin: xdg-activation getrandom failed → deny\n");
		t->token = NULL;
	} else {
		t->token = strdup(token_buf);
	}
	if (!t->token) {
		/* Alloc failure: deny rather than issue an empty/unfindable
		 * token that silently breaks activation.  Send done("") so the
		 * client doesn't hang, but the empty string will never match
		 * in qdwin_activation_token_find.  See codex-review Finding 3. */
		weston_log("qdwin: xdg-activation token strdup failed → "
			   "deny (empty token issued)\n");
		t->committed = 1;
		xdg_activation_token_v1_send_done(resource, "");
		return;
	}
	t->committed = 1;
	t->qdwin->activation_token_counter++;
	xdg_activation_token_v1_send_done(resource, t->token);
	weston_log("qdwin: xdg-activation token issued #%u app_id=%s\n",
		   (unsigned)t->qdwin->activation_token_counter,
		   t->app_id ? t->app_id : "(none)");
}

static const struct xdg_activation_token_v1_interface
qdwin_activation_token_impl = {
	.destroy    = qdwin_activation_token_destroy,
	.set_serial = qdwin_activation_token_set_serial,
	.set_app_id = qdwin_activation_token_set_app_id,
	.set_surface= qdwin_activation_token_set_surface,
	.commit     = qdwin_activation_token_commit,
};

static void
qdwin_activation_token_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_activation_token *t = wl_resource_get_user_data(resource);
	if (t)
		qdwin_activation_token_free(t);
}

static void
qdwin_activation_destroy(struct wl_client *client,
			 struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_activation_get_activation_token(struct wl_client *client,
				      struct wl_resource *resource,
				      uint32_t id)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_activation_token *t = calloc(1, sizeof *t);
	struct wl_resource *tok_resource;
	if (!t) {
		wl_client_post_no_memory(client);
		return;
	}
	tok_resource = wl_resource_create(
		client, &xdg_activation_token_v1_interface,
		wl_resource_get_version(resource), id);
	if (!tok_resource) {
		free(t);
		wl_client_post_no_memory(client);
		return;
	}
	t->qdwin = qdwin;
	t->token_resource = tok_resource;
	wl_list_insert(&qdwin->activation_tokens, &t->link);
	wl_resource_set_implementation(tok_resource,
				       &qdwin_activation_token_impl,
				       t,
				       qdwin_activation_token_resource_destroy);
}

static struct qdwin_toplevel *
qdwin_toplevel_from_wl_surface(struct qdwin *qdwin,
			       struct weston_surface *surface)
{
	struct qdwin_toplevel *tl;
	if (!surface)
		return NULL;
	wl_list_for_each(tl, &qdwin->toplevels, link) {
		if (!tl->desktop_surface)
			continue;
		if (weston_desktop_surface_get_surface(tl->desktop_surface)
		    == surface)
			return tl;
	}
	return NULL;
}

/* spec/09: pending xdg-activation, stalled until the shell calls
 * activation_decision. Owned by qdwin::activation_pending; freed on
 * either decision. Carries the original token and target so the
 * decision-handler can resume the activate body without re-walking
 * the protocol arguments. */
struct qdwin_activation_pending {
	struct qdwin *qdwin;
	uint32_t handle;
	struct qdwin_activation_token *token;
	struct qdwin_toplevel *target_tl;
	struct wl_listener target_destroy_listener;
	struct wl_event_source *timeout_source;
	struct wl_list link;  /* qdwin::activation_pending */
};

static void
qdwin_activation_pending_drop_token_refs(struct qdwin *qdwin,
					 struct qdwin_activation_token *t)
{
	struct qdwin_activation_pending *ap;
	wl_list_for_each(ap, &qdwin->activation_pending, link) {
		if (ap->token == t)
			ap->token = NULL;
	}
}

static void
qdwin_activation_pending_free(struct qdwin_activation_pending *ap)
{
	wl_list_remove(&ap->link);
	if (ap->timeout_source)
		wl_event_source_remove(ap->timeout_source);
	wl_list_remove(&ap->target_destroy_listener.link);
	if (ap->token)
		qdwin_activation_token_free(ap->token);
	free(ap);
}

static void
qdwin_activation_pending_free_all(struct qdwin *qdwin)
{
	struct qdwin_activation_pending *ap, *tmp;
	wl_list_for_each_safe(ap, tmp, &qdwin->activation_pending, link)
		qdwin_activation_pending_free(ap);
}

static void
qdwin_activation_pending_target_destroyed(struct wl_listener *l, void *data)
{
	struct qdwin_activation_pending *ap =
		wl_container_of(l, ap, target_destroy_listener);
	(void)data;
	weston_log("qdwin: activation_pending handle=%u target destroyed "
		   "before decision\n", ap->handle);
	qdwin_activation_pending_free(ap);
}

/* Run the original raise+focus body. Caller must NOT use `t` after
 * this — perform consumes it. */
static void
qdwin_activation_perform(struct qdwin *qdwin,
			 struct qdwin_toplevel *tl,
			 struct qdwin_activation_token *t)
{
	struct weston_seat *seat = NULL;
	/* A broker answer can race a lock transition. Never restack or focus a
	 * normal toplevel after lock entry: normal_layer is deliberately hidden,
	 * and moving the authorized locker out of lock_layer would leave only the
	 * black fail-secure curtain visible. Input during lock is owned by the
	 * locker overlay grab, not xdg-activation. */
	if (qdwin->locked) {
		weston_log("qdwin: xdg-activation blocked while locked handle=%u\n",
			   tl->handle);
		if (t)
			qdwin_activation_token_free(t);
		return;
	}
	if (tl->decorated) {
		weston_view_move_to_layer(tl->view,
					  &qdwin->normal_layer.view_list);
	}
	if (!wl_list_empty(&qdwin->compositor->seat_list)) {
		seat = wl_container_of(qdwin->compositor->seat_list.next,
				       seat, link);
	}
	if (seat) {
		weston_view_activate_input(tl->view, seat,
					   WESTON_ACTIVATE_FLAG_CONFIGURE);
	}
	weston_log("qdwin: xdg-activation activated handle=%u app_id=%s\n",
		   tl->handle,
		   t && t->app_id ? t->app_id : "(none)");
	if (t)
		qdwin_activation_token_free(t);
}

static int
qdwin_activation_pending_timeout_cb(void *data)
{
	struct qdwin_activation_pending *ap = data;
	weston_log("qdwin: activation_pending handle=%u timed out → deny\n",
		   ap->handle);
	ap->timeout_source = NULL;  /* libwayland frees on cb return for one-shot */
	qdwin_activation_pending_free(ap);
	return 0;
}

static struct qdwin_activation_pending *
qdwin_activation_pending_find(struct qdwin *qdwin, uint32_t handle)
{
	struct qdwin_activation_pending *ap;
	wl_list_for_each(ap, &qdwin->activation_pending, link)
		if (ap->handle == handle)
			return ap;
	return NULL;
}

static int
qdwin_shell_can_receive_v12(struct qdwin *qdwin)
{
	return qdwin->shell_bound && qdwin->shell_resource &&
	       wl_resource_get_version(qdwin->shell_resource) >= 12;
}

static void
qdwin_activation_activate(struct wl_client *client,
			  struct wl_resource *resource,
			  const char *token_str,
			  struct wl_resource *surface_resource)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct weston_surface *surface =
		surface_resource ? wl_resource_get_user_data(surface_resource)
				 : NULL;
	struct qdwin_activation_token *t;
	struct qdwin_toplevel *tl;

	(void)client;

	t = qdwin_activation_token_find(qdwin, token_str);
	if (!t) {
		weston_log("qdwin: xdg-activation activate with unknown token\n");
		return;
	}
	t->used = 1;

	tl = qdwin_toplevel_from_wl_surface(qdwin, surface);
	if (!tl) {
		weston_log("qdwin: xdg-activation target surface has no toplevel\n");
		qdwin_activation_token_free(t);
		return;
	}
	/* Reject requests made after lock entry before they reach the shell
	 * broker. qdwin_activation_perform repeats this check because a request
	 * already pending at the broker can race set_locked(1). */
	if (qdwin->locked) {
		weston_log("qdwin: xdg-activation request blocked while locked "
			   "handle=%u\n", tl->handle);
		qdwin_activation_token_free(t);
		return;
	}

	/* spec/09: gate via shell-broker if the shell speaks v12,
	 * otherwise auto-allow (legacy behaviour). The shell decides
	 * same-silo (trivial allow) vs cross-silo (broker-rule-or-deny). */
	if (!qdwin_shell_can_receive_v12(qdwin)) {
		qdwin_activation_perform(qdwin, tl, t);
		return;
	}

	struct qdwin_activation_pending *ap =
		calloc(1, sizeof *ap);
	if (!ap) {
		/* fail closed on alloc failure: gated activation must not auto-allow */
		weston_log("qdwin: activation_pending calloc failed → deny "
			   "(token consumed) app_id=%s\n",
			   t->app_id ? t->app_id : "(none)");
		qdwin_activation_token_free(t);
		return;
	}
	ap->qdwin = qdwin;
	ap->handle = ++qdwin->activation_pending_next_handle;
	ap->token = t;
	ap->target_tl = tl;
	ap->target_destroy_listener.notify =
		qdwin_activation_pending_target_destroyed;
	wl_signal_add(&surface->destroy_signal,
		      &ap->target_destroy_listener);
	wl_list_insert(&qdwin->activation_pending, &ap->link);

	/* 10s safety timeout — broker should answer in <100ms; if the
	 * shell is wedged we don't want pending state to accumulate. */
	struct wl_event_loop *loop =
		wl_display_get_event_loop(qdwin->compositor->wl_display);
	ap->timeout_source = wl_event_loop_add_timer(
		loop, qdwin_activation_pending_timeout_cb, ap);
	if (ap->timeout_source)
		wl_event_source_timer_update(ap->timeout_source, 10000);

	struct qdwin_toplevel *src_tl =
		qdwin_toplevel_from_wl_surface(qdwin, t->requesting_surface);
	uint32_t src_handle = src_tl ? src_tl->handle : UINT32_MAX;
	uint32_t tgt_handle = tl->handle;
	qdwin_shell_v1_send_activation_pending(qdwin->shell_resource,
					       ap->handle,
					       src_handle, tgt_handle,
					       t->app_id ? t->app_id : "");
	weston_log("qdwin: activation_pending handle=%u src=%u tgt=%u "
		   "app_id=%s\n",
		   ap->handle, src_handle, tgt_handle,
		   t->app_id ? t->app_id : "");
}

/* spec/09: shell answers an activation_pending. */
static void
qdwin_handle_activation_decision(struct wl_client *client,
				 struct wl_resource *resource,
				 uint32_t handle,
				 uint32_t decision,
				 const char *reason)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	(void)client;
	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	struct qdwin_activation_pending *ap =
		qdwin_activation_pending_find(qdwin, handle);
	if (!ap) {
		weston_log("qdwin: activation_decision unknown handle=%u\n",
			   handle);
		return;
	}
	struct qdwin_activation_token *t = ap->token;
	struct qdwin_toplevel *tl = ap->target_tl;
	ap->token = NULL;  /* perform consumes it */
	wl_list_remove(&ap->link);
	if (ap->timeout_source) {
		wl_event_source_remove(ap->timeout_source);
		ap->timeout_source = NULL;
	}
	wl_list_remove(&ap->target_destroy_listener.link);
	wl_list_init(&ap->target_destroy_listener.link);
	free(ap);
	if (decision == 0) {
		weston_log("qdwin: activation_decision handle=%u → allow\n",
			   handle);
		qdwin_activation_perform(qdwin, tl, t);
	} else {
		weston_log("qdwin: activation_decision handle=%u → "
			   "%s reason=%s\n", handle,
			   decision == 1 ? "deny" : "defer",
			   reason ? reason : "");
		if (t)
			qdwin_activation_token_free(t);
	}
}

static const struct xdg_activation_v1_interface qdwin_activation_impl = {
	.destroy              = qdwin_activation_destroy,
	.get_activation_token = qdwin_activation_get_activation_token,
	.activate             = qdwin_activation_activate,
};

static void
bind_xdg_activation(struct wl_client *client, void *data,
		    uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	struct wl_resource *resource = wl_resource_create(
		client, &xdg_activation_v1_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource, &qdwin_activation_impl,
				       qdwin, NULL);
}

/* ------------------------------------------------------------------
 * zxdg_decoration_manager_v1 — always-server_side stub.
 *
 * Background. qdwin's design has qdshell draw chrome via the private
 * qdwin_shell_v1 protocol, so the compositor never wants toolkit-drawn
 * CSDs (they would stack on top of qdshell's chrome — see
 * tests/gui/AGENTS.md pitfall #2 for the foot case). The natural way
 * to tell toolkits "skip CSD" is to advertise xdg-decoration-v1 and
 * always reply with mode=server_side; Qt-Wayland and GTK4 both honour
 * this. We don't actually draw any decoration in response — qdshell
 * does that via qdwin_shell_v1.attach_decoration.
 *
 * Without this global, PyQt6 (Qt-Wayland built-in bradient/adwaita
 * CSD plugin) aborts in QApplication during first-widget construction:
 *
 *     Fatal Python error: Aborted
 *       File "<string>", line N in <module>
 *     Extension modules: PyQt6.QtCore, PyQt6.QtGui, PyQt6.QtWidgets
 *
 * Set QT_WAYLAND_DISABLE_WINDOWDECORATION=1 and the abort disappears,
 * which is what isolated the crash to the CSD attach path. With this
 * stub global the abort also disappears because Qt opts out of CSD
 * on its own. See todo/qdwin-crash.md.
 * ------------------------------------------------------------------ */

static void
qdwin_toplevel_decoration_destroy(struct wl_client *client,
				  struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_toplevel_decoration_set_mode(struct wl_client *client,
				   struct wl_resource *resource,
				   uint32_t mode)
{
	(void)client; (void)mode;
	/* Whatever the client asks for, we mandate server_side. */
	zxdg_toplevel_decoration_v1_send_configure(
		resource, ZXDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE);
}

static void
qdwin_toplevel_decoration_unset_mode(struct wl_client *client,
				     struct wl_resource *resource)
{
	(void)client;
	zxdg_toplevel_decoration_v1_send_configure(
		resource, ZXDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE);
}

static const struct zxdg_toplevel_decoration_v1_interface
qdwin_toplevel_decoration_impl = {
	.destroy    = qdwin_toplevel_decoration_destroy,
	.set_mode   = qdwin_toplevel_decoration_set_mode,
	.unset_mode = qdwin_toplevel_decoration_unset_mode,
};

static void
qdwin_xdg_decoration_manager_destroy(struct wl_client *client,
				     struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_xdg_decoration_manager_get_toplevel_decoration(
	struct wl_client *client,
	struct wl_resource *manager_resource,
	uint32_t id,
	struct wl_resource *xdg_toplevel_resource)
{
	(void)manager_resource; (void)xdg_toplevel_resource;
	uint32_t version = wl_resource_get_version(manager_resource);
	struct wl_resource *dec = wl_resource_create(
		client, &zxdg_toplevel_decoration_v1_interface, version, id);
	if (!dec) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(dec, &qdwin_toplevel_decoration_impl,
				       NULL, NULL);
	/* Send the initial configure immediately. The client must ack
	 * via xdg_surface.ack_configure before attaching a buffer; Qt
	 * and GTK both handle a same-frame initial mode correctly. */
	zxdg_toplevel_decoration_v1_send_configure(
		dec, ZXDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE);
}

static const struct zxdg_decoration_manager_v1_interface
qdwin_xdg_decoration_manager_impl = {
	.destroy                 = qdwin_xdg_decoration_manager_destroy,
	.get_toplevel_decoration =
		qdwin_xdg_decoration_manager_get_toplevel_decoration,
};

static void
bind_qdwin_xdg_decoration_manager(struct wl_client *client, void *data,
				  uint32_t version, uint32_t id)
{
	struct wl_resource *resource = wl_resource_create(
		client, &zxdg_decoration_manager_v1_interface, version, id);
	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource,
				       &qdwin_xdg_decoration_manager_impl,
				       data, NULL);
}

/* ------------------------------------------------------------------
 * §6.8 S1 — nested-mode publisher.
 *
 * When QDWIN_NESTED_MODE=1 is set in the environment, qdwin-shell.so
 * is being loaded by a *nested* weston whose toplevels should be
 * hosted by the *outer* qdwin. Instead of acting as the admin shell
 * with chrome / panel / launcher, this shell acts as a publisher:
 *
 *   1. Opens a separate wl_display connection to the outer qdwin
 *      (env QDWIN_OUTER_DISPLAY, default "wayland-0"). Reuses the
 *      shell's allowed_uid for peer-uid filtering on the outer. This
 *      connection — NOT a wayland-backend — is how the publisher
 *      reaches the outer, so the nested weston needs no
 *      wayland-backend (and must not load one: its host-output window
 *      would shadow the proxy in the outer's pick_view; F6#2).
 *   2. Loads the pipewire-output API; expects backend-pipewire to
 *      be present in the nested compositor (configure via
 *      `[core] backend=pipewire-backend.so`).
 *   3. On each new desktop surface (= inner xdg_toplevel of an
 *      inner wayland client), allocates a dedicated pipewire output
 *      and pins the toplevel onto it. The pipewire output's frames
 *      become a PipeWire stream node visible on the host's
 *      PipeWire daemon — same uid as the outer qdwin, so the
 *      outer's libpipewire client can resolve it.
 *   4. Calls advertise_toplevel(pw_node, input_sink, app_id, title,
 *      origin_uid) on the outer's qdwin_nested_manager_v1 (bound
 *      via the separate connection). pw_node is a string of form
 *      "weston.pipewire:<nested-pid>:<weston-output-name>".
 *      input_sink is empty for S1 (deferred to S3).
 *
 * S1 scope: lifecycle plumbing + pixel publish. The outer side's
 * proxy surface creation is S2; input injection is S3.
 *
 * Failure modes (all log + degrade gracefully):
 *
 *   - QDWIN_OUTER_DISPLAY missing or wl_display_connect fails:
 *     nested mode disabled with a single-line warning. Inner
 *     toplevels render normally inside the nested compositor; the
 *     §6.6 S6 outer-toplevel-fallback shape continues to apply.
 *   - Manager not advertised on outer (peer-uid rejected): same.
 *   - pipewire-output API unavailable: log + run inner clients
 *     without per-toplevel publish.
 *
 * ------------------------------------------------------------------ */

static int
qdwin_nested_outer_fd_handler(int fd, uint32_t mask, void *data)
{
	struct qdwin *qdwin = data;
	(void)fd;
	if (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR)) {
		weston_log("qdwin: nested-mode outer connection closed\n");
		return 0;
	}
	int n = qdwin_nested_client_dispatch_pending(qdwin->nested_client);
	if (n < 0) {
		weston_log("qdwin: nested-mode outer dispatch error\n");
		return 0;
	}
	qdwin_nested_client_flush(qdwin->nested_client);
	return n;
}

static void
qdwin_nested_init(struct qdwin *qdwin)
{
	const char *enabled = getenv("QDWIN_NESTED_MODE");
	if (!enabled || strcmp(enabled, "1") != 0)
		return;

	const char *outer = getenv("QDWIN_OUTER_DISPLAY");
	if (!outer || !*outer)
		outer = "wayland-0";

	/* findings F6: QDWIN_OUTER_DISPLAY selects which Wayland server the
	 * nested qdwin connects out to. It is owned by the trusted launcher, but
	 * constrain it defensively so a compromised environment cannot redirect
	 * the outer connection to an attacker-controlled socket (MITM of
	 * advertise_toplevel etc.). Accept a runtime-dir-relative display name
	 * (no '/', libwayland resolves it under XDG_RUNTIME_DIR); accept an
	 * absolute path only if it lives under $XDG_RUNTIME_DIR. Reject anything
	 * else and refuse to enter nested mode (fail secure). */
	if (strchr(outer, '/')) {
		const char *xrd = getenv("XDG_RUNTIME_DIR");
		size_t xl = xrd ? strlen(xrd) : 0;
		/* Reject any ".." component so a runtime-dir-prefixed path cannot
		 * traverse out (e.g. $XDG_RUNTIME_DIR/../1001/wayland-0). */
		if (outer[0] != '/' || !xrd || !xl ||
		    strncmp(outer, xrd, xl) != 0 || outer[xl] != '/' ||
		    strstr(outer, "/../") != NULL ||
		    (strlen(outer) >= 3 &&
		     strcmp(outer + strlen(outer) - 3, "/..") == 0)) {
			weston_log("qdwin: NESTED_MODE refused — QDWIN_OUTER_DISPLAY="
				   "%s is neither a display name nor a path under "
				   "XDG_RUNTIME_DIR\n", outer);
			return;
		}
	}

	weston_log("qdwin: NESTED_MODE on; pid=%d outer=%s\n",
		   (int)getpid(), outer);

	/* Hard isolate the outer connection from the nested compositor's
	 * own server: drop any inherited WAYLAND_SOCKET (used by parent-
	 * launched children to reuse a pre-opened fd) so wl_display_connect
	 * actually opens a fresh socket to `outer`. Restore after. */
	const char *saved_wsock = getenv("WAYLAND_SOCKET");
	if (saved_wsock)
		unsetenv("WAYLAND_SOCKET");

	qdwin->nested_client = qdwin_nested_client_new(outer);

	if (saved_wsock)
		setenv("WAYLAND_SOCKET", saved_wsock, 1);
	if (qdwin_nested_client_failed(qdwin->nested_client)) {
		weston_log("qdwin: nested-mode disabled — outer "
			   "qdwin_nested_manager_v1 unavailable on display=%s\n",
			   outer);
		if (qdwin->nested_client) {
			qdwin_nested_client_destroy(qdwin->nested_client);
			qdwin->nested_client = NULL;
		}
		return;
	}

	int fd = qdwin_nested_client_get_fd(qdwin->nested_client);
	struct wl_event_loop *loop =
		wl_display_get_event_loop(qdwin->compositor->wl_display);
	qdwin->nested_outer_event_source = wl_event_loop_add_fd(
		loop, fd, WL_EVENT_READABLE,
		qdwin_nested_outer_fd_handler, qdwin);
	if (!qdwin->nested_outer_event_source) {
		weston_log("qdwin: nested-mode disabled — event loop "
			   "integration failed\n");
		qdwin_nested_client_destroy(qdwin->nested_client);
		qdwin->nested_client = NULL;
		return;
	}

	/* Count pre-allocated pipewire outputs in the nested compositor
	 * (from [pipewire] num-outputs=N in weston.ini). Each inner
	 * toplevel pins to one; if pool is empty we publish without
	 * pixels. */
	int pw_pool = 0;
	struct weston_output *o;
	wl_list_for_each(o, &qdwin->compositor->output_list, link) {
		if (strncmp(o->name, "pipewire", 8) == 0)
			pw_pool++;
	}

	qdwin->nested_mode = true;
	qdwin->nested_next_pw_id = 0;
	weston_log("qdwin: nested-mode publisher ready (outer manager v%u; "
		   "pipewire-output pool=%d)\n",
		   qdwin_nested_client_manager_version(qdwin->nested_client),
		   pw_pool);
}

/* §6.8 S3b: lazy init for the per-toplevel inner-seat. First non-PING
 * packet arrival triggers this; PING-only S3-mvp probes never pay it. */
static void
qdwin_nested_inner_seat_init(struct qdwin_toplevel *tl)
{
	if (tl->nested_inner_seat_inited)
		return;
	struct qdwin *qdwin = tl->qdwin;
	char seat_name[64];
	snprintf(seat_name, sizeof seat_name,
		 "qdwin-nested-T%u", tl->handle);
	weston_seat_init(&tl->nested_inner_seat, qdwin->compositor, seat_name);
	if (weston_seat_init_pointer(&tl->nested_inner_seat) < 0)
		weston_log("qdwin/nested: inner-seat init_pointer failed "
			   "handle=%u\n", tl->handle);
	struct xkb_keymap *keymap = NULL;
	if (qdwin->compositor->xkb_info)
		keymap = qdwin->compositor->xkb_info->keymap;
	if (weston_seat_init_keyboard(&tl->nested_inner_seat, keymap) < 0)
		weston_log("qdwin/nested: inner-seat init_keyboard failed "
			   "handle=%u\n", tl->handle);
	tl->nested_inner_seat_inited = true;
	weston_log("qdwin/nested: inner-seat '%s' ready\n", seat_name);
}

static void
qdwin_nested_inner_seat_release(struct qdwin_toplevel *tl)
{
	if (!tl->nested_inner_seat_inited)
		return;
	weston_seat_release_keyboard(&tl->nested_inner_seat);
	weston_seat_release_pointer(&tl->nested_inner_seat);
	weston_seat_release(&tl->nested_inner_seat);
	tl->nested_inner_seat_inited = false;
}

/* Pin pointer + keyboard focus to the inner toplevel's view/surface so
 * notify_motion_absolute / notify_button / notify_key route to the
 * inner client. Cheap when focus is already correct. */
static void
qdwin_nested_inner_seat_assert_focus(struct qdwin_toplevel *tl)
{
	if (!tl->nested_inner_seat_inited || !tl->view || !tl->view->surface)
		return;
	struct weston_pointer *ptr =
		weston_seat_get_pointer(&tl->nested_inner_seat);
	if (ptr && ptr->focus != tl->view)
		weston_pointer_set_focus(ptr, tl->view);
	struct weston_keyboard *kbd =
		weston_seat_get_keyboard(&tl->nested_inner_seat);
	if (kbd && kbd->focus != tl->view->surface)
		weston_keyboard_set_focus(kbd, tl->view->surface);
}

static struct timespec
qdwin_nested_ts_from_msec(uint32_t time_msec)
{
	struct timespec ts;
	ts.tv_sec  = time_msec / 1000u;
	ts.tv_nsec = (long)(time_msec % 1000u) * 1000000L;
	return ts;
}

static void
qdwin_nested_dispatch_motion(struct qdwin_toplevel *tl,
			     const struct qdni_motion_payload *p)
{
	qdwin_nested_inner_seat_init(tl);
	qdwin_nested_inner_seat_assert_focus(tl);
	if (!tl->view || !tl->view->surface)
		return;
	weston_view_update_transform(tl->view);
	struct weston_coord_surface cs = {
		.c = { .x = wl_fixed_to_double(p->x_fixed),
		       .y = wl_fixed_to_double(p->y_fixed) },
		.coordinate_space_id = tl->view->surface,
	};
	struct weston_coord_global gpos =
		weston_coord_surface_to_global(tl->view, cs);
	struct timespec ts = qdwin_nested_ts_from_msec(p->time_msec);
	qdwin_notify_motion_absolute(&tl->nested_inner_seat, &ts, gpos);
}

static void
qdwin_nested_dispatch_button(struct qdwin_toplevel *tl,
			     const struct qdni_button_payload *p)
{
	qdwin_nested_inner_seat_init(tl);
	qdwin_nested_inner_seat_assert_focus(tl);
	struct timespec ts = qdwin_nested_ts_from_msec(p->time_msec);
	qdwin_notify_button(&tl->nested_inner_seat, &ts, (int32_t)p->button,
		      p->state ? WL_POINTER_BUTTON_STATE_PRESSED
			       : WL_POINTER_BUTTON_STATE_RELEASED);
}

static void
qdwin_nested_dispatch_key(struct qdwin_toplevel *tl,
			  const struct qdni_key_payload *p)
{
	qdwin_nested_inner_seat_init(tl);
	qdwin_nested_inner_seat_assert_focus(tl);
	struct timespec ts = qdwin_nested_ts_from_msec(p->time_msec);
	qdwin_notify_key(&tl->nested_inner_seat, &ts, p->key,
		   p->state ? WL_KEYBOARD_KEY_STATE_PRESSED
			    : WL_KEYBOARD_KEY_STATE_RELEASED,
		   STATE_UPDATE_AUTOMATIC);
}

static void
qdwin_nested_dispatch_axis(struct qdwin_toplevel *tl,
			   const struct qdni_axis_payload *p)
{
	qdwin_nested_inner_seat_init(tl);
	struct weston_pointer_axis_event ev = {
		.axis = p->axis,
		.value = wl_fixed_to_double(p->value_fixed),
		.has_discrete = false,
		.discrete = 0,
	};
	struct timespec ts = qdwin_nested_ts_from_msec(p->time_msec);
	qdwin_notify_axis(&tl->nested_inner_seat, &ts, &ev);
}

/* §6.8 S3/S3b: peer-fd readable callback — drain one packet at a time.
 * S3-mvp PING is the wire-format proof; S3b decodes motion / button /
 * key / axis / focus via the per-toplevel inner-seat. */
static int
qdwin_nested_input_sink_peer_cb(int fd, uint32_t mask, void *data)
{
	struct qdwin_toplevel *tl = data;
	int current_fd = tl->nested_input_sink
		? tl->nested_input_sink->peer_fd : -1;
	if (!qdwin_nested_input_peer_event_current(fd, current_fd)) {
		weston_log("qdwin/nested: ignoring stale input-sink peer event "
			   "handle=%u event_fd=%d current_fd=%d\n",
			   tl->handle, fd, current_fd);
		return 0;
	}
	if (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR)) {
		weston_log("qdwin/nested: input-sink peer closed handle=%u\n",
			   tl->handle);
		if (tl->nested_input_peer_source) {
			wl_event_source_remove(tl->nested_input_peer_source);
			tl->nested_input_peer_source = NULL;
		}
		if (tl->nested_input_sink && tl->nested_input_sink->peer_fd >= 0) {
			close(tl->nested_input_sink->peer_fd);
			tl->nested_input_sink->peer_fd = -1;
		}
		return 0;
	}
	/* §6.8 Round-8 fix: drain the per-peer buffer until the parser
	 * reports QDWIN_NESTED_INPUT_SINK_AGAIN (not enough bytes for the
	 * next full packet). The outer sends the S3b burst as ~9 packets
	 * back-to-back; on a SOCK_STREAM they typically arrive in a single
	 * readable notification, so reading just one packet per callback
	 * used to drop the trailing packets unless the event loop happened
	 * to re-trigger (the ~50% Round-7 flake). */
	for (;;) {
		uint8_t version = 0, payload[256];
		uint16_t plen = 0;
		int et = qdwin_nested_input_sink_read_one(
			tl->nested_input_sink,
			&version, &plen, payload, sizeof payload);
		if (et == QDWIN_NESTED_INPUT_SINK_AGAIN)
			break;
		if (et < 0)
			return 0;
		switch (et) {
		case QDNI_EVENT_PING:
			weston_log("qdwin/nested: input-sink PING handle=%u "
				   "version=%u (S3 wire-format proven)\n",
				   tl->handle, (unsigned)version);
			break;
		case QDNI_EVENT_MOTION:
			if (plen < sizeof(struct qdni_motion_payload)) break;
			qdwin_nested_dispatch_motion(tl,
				(const struct qdni_motion_payload *)payload);
			if (getenv("QDWIN_NESTED_INPUT_DEBUG"))
				weston_log("qdwin/nested: motion handle=%u\n",
					   tl->handle);
			break;
		case QDNI_EVENT_BUTTON: {
			if (plen < sizeof(struct qdni_button_payload)) break;
			const struct qdni_button_payload *p =
				(const struct qdni_button_payload *)payload;
			qdwin_nested_dispatch_button(tl, p);
			weston_log("qdwin/nested: button handle=%u "
				   "btn=0x%x state=%u\n",
				   tl->handle, p->button, p->state);
			break;
		}
		case QDNI_EVENT_KEY: {
			if (plen < sizeof(struct qdni_key_payload)) break;
			const struct qdni_key_payload *p =
				(const struct qdni_key_payload *)payload;
			qdwin_nested_dispatch_key(tl, p);
			weston_log("qdwin/nested: key handle=%u "
				   "key=%u state=%u\n",
				   tl->handle, p->key, p->state);
			break;
		}
		case QDNI_EVENT_AXIS:
			if (plen < sizeof(struct qdni_axis_payload)) break;
			qdwin_nested_dispatch_axis(tl,
				(const struct qdni_axis_payload *)payload);
			break;
		case QDNI_EVENT_FOCUS:
			if (plen < sizeof(struct qdni_focus_payload)) break;
			weston_log("qdwin/nested: focus handle=%u "
				   "focused=%u\n",
				   tl->handle,
				   ((const struct qdni_focus_payload *)
				    payload)->focused);
			break;
		default:
			weston_log("qdwin/nested: input-sink unknown "
				   "event_type=%d plen=%u handle=%u\n",
				   et, (unsigned)plen, tl->handle);
			break;
		}
	}
	return 1;
}

/* §6.8 S3: listen-fd readable callback — accept the outer's connect. */
static int
qdwin_nested_input_sink_listen_cb(int fd, uint32_t mask, void *data)
{
	struct qdwin_toplevel *tl = data;
	struct qdwin *qdwin = tl->qdwin;
	(void)fd;
	if (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR))
		return 0;

	/* The sink helper closes the old peer only after accept4 succeeds.  Its
	 * wl_event_source must be removed first: otherwise a delayed HUP for the
	 * closed fd can run after peer_fd has been replaced and close the new
	 * peer.  If accept4 loses a readiness race, restore the old watch. */
	int old_peer_fd = tl->nested_input_sink
		? tl->nested_input_sink->peer_fd : -1;
	if (tl->nested_input_peer_source) {
		wl_event_source_remove(tl->nested_input_peer_source);
		tl->nested_input_peer_source = NULL;
	}
	if (qdwin_nested_input_sink_accept(tl->nested_input_sink) < 0) {
		if (old_peer_fd >= 0 && tl->nested_input_sink &&
		    tl->nested_input_sink->peer_fd == old_peer_fd) {
			struct wl_event_loop *loop = wl_display_get_event_loop(
				qdwin->compositor->wl_display);
			tl->nested_input_peer_source = wl_event_loop_add_fd(
				loop, old_peer_fd, WL_EVENT_READABLE,
				qdwin_nested_input_sink_peer_cb, tl);
			if (!tl->nested_input_peer_source) {
				close(old_peer_fd);
				tl->nested_input_sink->peer_fd = -1;
				weston_log("qdwin/nested: failed to restore "
					   "input-sink peer watch handle=%u\n",
					   tl->handle);
			}
		}
		return 0;
	}
	weston_log("qdwin/nested: input-sink connected handle=%u\n",
		   tl->handle);
	struct wl_event_loop *loop = wl_display_get_event_loop(
		qdwin->compositor->wl_display);
	tl->nested_input_peer_source = wl_event_loop_add_fd(
		loop, tl->nested_input_sink->peer_fd, WL_EVENT_READABLE,
		qdwin_nested_input_sink_peer_cb, tl);
	if (!tl->nested_input_peer_source) {
		close(tl->nested_input_sink->peer_fd);
		tl->nested_input_sink->peer_fd = -1;
		weston_log("qdwin/nested: failed to watch input-sink peer "
			   "handle=%u\n", tl->handle);
		return 0;
	}
	return 1;
}

static void
qdwin_nested_on_configured(void *userdata, int32_t w, int32_t h)
{
	struct qdwin_toplevel *tl = userdata;
	tl->nested_configured_w = w;
	tl->nested_configured_h = h;
	weston_log("qdwin/nested: outer configured handle=%u %dx%d\n",
		   tl->handle, w, h);
	if (tl->desktop_surface && w > 0 && h > 0)
		weston_desktop_surface_set_size(tl->desktop_surface, w, h);
}

static void
qdwin_nested_on_close_requested(void *userdata)
{
	struct qdwin_toplevel *tl = userdata;
	weston_log("qdwin/nested: outer close_requested handle=%u\n",
		   tl->handle);
	if (tl->desktop_surface)
		weston_desktop_surface_close(tl->desktop_surface);
}

static void
qdwin_nested_on_focus_changed(void *userdata, uint32_t focused)
{
	struct qdwin_toplevel *tl = userdata;
	weston_log("qdwin/nested: outer focus_changed handle=%u focused=%u\n",
		   tl->handle, focused);
	(void)focused;
	/* S1: log only. S3 ties this to inner-seat focus. */
}

/* Pick the next un-pinned pipewire output from the nested compositor's
 * pre-allocated pool ([pipewire] num-outputs=N in weston.ini). This is
 * the §6.5 view-stream shape applied to nested mode: backend-pipewire's
 * weston_pipewire_output_api_v2 doesn't expose dynamic create_output
 * via an external-friendly pointer (the head_create entry takes a
 * weston_backend* that isn't reachable from a shell plugin), so static
 * pre-allocation is the LLM-friendly path for libweston-16.
 *
 * Returns NULL if all pipewire outputs are already pinned to other
 * nested toplevels — caller advertises with pw_node="...:none" in
 * that case so the outer can still surface the toplevel without
 * pixels (admin shell decides whether to grow the pool or refuse).
 */
static struct weston_output *
qdwin_nested_pick_pw_output(struct qdwin *qdwin)
{
	struct weston_output *o;
	wl_list_for_each(o, &qdwin->compositor->output_list, link) {
		if (strncmp(o->name, "pipewire", 8) != 0)
			continue;
		/* Skip outputs already pinned to a nested-pub. */
		struct qdwin_toplevel *tl;
		int taken = 0;
		wl_list_for_each(tl, &qdwin->toplevels, link) {
			if (tl->nested_pw_output == o) { taken = 1; break; }
		}
		if (taken)
			continue;
		return o;
	}
	return NULL;
}

static void
qdwin_nested_publish_toplevel(struct qdwin_toplevel *tl)
{
	struct qdwin *qdwin = tl->qdwin;
	if (!qdwin->nested_mode || !qdwin->nested_client)
		return;
	if (tl->nested_pub) /* already advertised */
		return;
	if (!qdwin_nested_client_manager_ready(qdwin->nested_client)) {
		weston_log("qdwin/nested: skip publish — manager not ready\n");
		return;
	}

	tl->nested_pw_output = qdwin_nested_pick_pw_output(qdwin);

	/* Pin the inner toplevel's view onto the pipewire output if we
	 * got one. Otherwise leave on whatever the desktop layout chose;
	 * the outer side will see the advertise but no pixels. */
	if (tl->nested_pw_output) {
		struct weston_output *pw = tl->nested_pw_output;
		struct weston_coord_global pos = {
			.c = weston_coord(pw->pos.c.x, pw->pos.c.y),
		};
		weston_view_set_position(tl->view, pos);
		weston_view_set_output(tl->view, pw);
		weston_view_update_transform(tl->view);
		/* J29: 14->16 weston_surface_damage() removed; dirty the view to
		 * force the pipewire output to composite the pinned view's bytes. */
		if (tl->view->surface)
			weston_view_geometry_dirty(tl->view);
		weston_output_schedule_repaint(pw);
	}

	const char *app_id =
		weston_desktop_surface_get_app_id(tl->desktop_surface);
	const char *title =
		weston_desktop_surface_get_title(tl->desktop_surface);
	uid_t uid = qdwin_client_uid(tl->desktop_surface);

	char pw_node[128];
	if (tl->nested_pw_output && tl->nested_pw_output->name)
		snprintf(pw_node, sizeof pw_node,
			 "weston.pipewire:%d:%s",
			 (int)getpid(), tl->nested_pw_output->name);
	else
		snprintf(pw_node, sizeof pw_node,
			 "weston.pipewire:%d:none", (int)getpid());

	/* §6.8 S3: open the per-toplevel input sink + integrate listen
	 * fd into the weston event loop. The accept callback sets up a
	 * second event source for the connected peer fd. */
	tl->nested_input_sink = qdwin_nested_input_sink_open(tl->handle);
	const char *input_sink_path = "";
	if (tl->nested_input_sink) {
		input_sink_path = tl->nested_input_sink->socket_path;
		struct wl_event_loop *loop = wl_display_get_event_loop(
			qdwin->compositor->wl_display);
		tl->nested_input_sink_source = wl_event_loop_add_fd(
			loop, tl->nested_input_sink->listen_fd,
			WL_EVENT_READABLE,
			qdwin_nested_input_sink_listen_cb, tl);
	}

	tl->nested_pub = qdwin_nested_client_advertise(
		qdwin->nested_client,
		pw_node,
		input_sink_path,
		app_id ? app_id : "",
		title ? title : "",
		(uint32_t)uid,
		tl,
		qdwin_nested_on_configured,
		qdwin_nested_on_close_requested,
		qdwin_nested_on_focus_changed);
	if (!tl->nested_pub) {
		weston_log("qdwin/nested: advertise_toplevel failed for "
			   "handle=%u app_id=%s\n",
			   tl->handle, app_id ? app_id : "(null)");
		return;
	}
	weston_log("qdwin/nested: advertised handle=%u pw_node=%s "
		   "app_id=%s title=%s origin_uid=%u\n",
		   tl->handle, pw_node,
		   app_id ? app_id : "(null)", title ? title : "(null)",
		   (unsigned)uid);
}

static void
qdwin_nested_unpublish_toplevel(struct qdwin_toplevel *tl)
{
	if (!tl->nested_pub)
		return;
	weston_log("qdwin/nested: unpublish handle=%u\n", tl->handle);
	qdwin_nested_client_pub_destroy(tl->nested_pub);
	tl->nested_pub = NULL;
	/* The pipewire output stays for the lifetime of the compositor —
	 * weston doesn't currently expose a public output_destroy on
	 * pipewire heads, and reuse is fine since names are unique. */
	tl->nested_pw_output = NULL;
	/* §6.8 S3: tear down the input sink lifecycle. Closing the
	 * listener removes outer-side ability to reconnect; the peer fd
	 * (if connected) gets closed too. */
	if (tl->nested_input_peer_source) {
		wl_event_source_remove(tl->nested_input_peer_source);
		tl->nested_input_peer_source = NULL;
	}
	if (tl->nested_input_sink_source) {
		wl_event_source_remove(tl->nested_input_sink_source);
		tl->nested_input_sink_source = NULL;
	}
	if (tl->nested_input_sink) {
		qdwin_nested_input_sink_close(tl->nested_input_sink);
		tl->nested_input_sink = NULL;
	}
	/* §6.8 S3b: tear the per-toplevel inner-seat down too. The seat
	 * may have been lazily inited only if real (non-PING) events
	 * landed; release is a no-op when never inited. */
	qdwin_nested_inner_seat_release(tl);
}

/* ------------------------------------------------------------------
 * §6.8 S2 — outer proxy surface from advertise.
 *
 * On the *outer* qdwin side: when qdwin_nested_v1.advertise_toplevel
 * lands, synthesise a placeholder weston_curtain + wrap it in a
 * qdwin_toplevel marked is_nested_proxy=true, then fire
 * qdwin_shell_v1.toplevel_added so qdshell decorates the proxy
 * exactly like a real xdg_toplevel. Real PipeWire-fed pixels are
 * S2b — the curtain is a stable placeholder until the consumer side
 * lands. set_title forwards via toplevel_title; app_id changes are
 * lossy (qdwin_shell_v1 has no toplevel_app_id event); set_geometry
 * resizes the curtain.
 *
 * Lifecycle:
 *   advertise_toplevel(...) -> qdwin_nested_proxy_create()
 *      -> curtain at default 800x600, visible on normal_layer
 *      -> toplevel_added event on outer shell
 *   set_title -> proxy_title cache + toplevel_title event
 *   set_geometry -> resize curtain (destroy + recreate)
 *   resource destroy -> qdwin_nested_proxy_destroy()
 *      -> toplevel_removed + curtain teardown + free
 *
 * Chrome attach (qdwin_shell_v1.attach_decoration) finds the proxy
 * by handle in qdwin->toplevels (proxies live in the same list as
 * real desktop_surface-backed toplevels) and the existing chrome
 * pipeline works unchanged because chrome positions are derived from
 * the view's geometry, not desktop_surface state.
 *
 * Future work (S2b/S3/S4):
 *   - Replace placeholder curtain with PipeWire-fed buffers via a
 *     small consumer process (pattern from qdistro-forward §6.5).
 *   - Wire close_requested when shell asks to close the proxy.
 *   - Wire focus_changed onto outer seat focus events for the proxy.
 *   - admin-broker CheckPermission gate at advertise time (S4).
 * ------------------------------------------------------------------ */

static struct qdwin_toplevel *
qdwin_nested_proxy_create(struct qdwin *qdwin,
			  struct qdwin_nested_toplevel *owner,
			  const char *app_id, const char *title,
			  uint32_t origin_uid, int w, int h)
{
	if (w <= 0) w = 800;
	if (h <= 0) h = 600;
	struct qdwin_toplevel *tl = calloc(1, sizeof *tl);
	if (!tl)
		return NULL;
	tl->qdwin              = qdwin;
	tl->desktop_surface    = NULL;
	tl->handle             = ++qdwin->next_handle;
	tl->is_nested_proxy    = true;
	tl->proxy_app_id       = qdwin_xstrdup_or_null(app_id);
	tl->proxy_title        = qdwin_xstrdup_or_null(title);
	tl->proxy_origin_uid   = origin_uid;
	tl->proxy_nested_owner = owner;
	tl->mapped             = 1;
	tl->decorated          = 0;
	tl->last_width         = w;
	tl->last_height        = h;
	for (int s = 0; s < QDWIN_SIDES; s++) {
		tl->chrome[s].side = s;
		tl->chrome[s].tl   = tl;
		wl_list_init(&tl->chrome[s].surface_destroy.link);
		wl_list_init(&tl->chrome[s].surface_commit.link);
	}
	/* F6#2 mechanism A: the pixel-feed destroy listener may be added without
	 * a view (pending stash), and teardown paths key on the link being empty,
	 * so initialise it to a valid empty list (calloc zeroes are not a valid
	 * wl_list). */
	wl_list_init(&tl->proxy_pixel_destroy_listener.link);

	struct weston_output *out = qdwin_primary_output(qdwin);
	int cx = 0, cy = 0;
	if (out) {
		/* Pixel-coord math; cast pos.c.{x,y} (double) to int so the
		 * integer division isn't flagged as float-context loss. */
		cx = (int)out->pos.c.x + (out->width  - w) / 2;
		cy = (int)out->pos.c.y + (out->height - h) / 2;
		if (cx < 0) cx = 0;
		if (cy < 0) cy = 0;
	}
	/* §6.8 S4: gate visibility behind admin-broker decision. Production
	 * defaults fail-closed: pre-v8 shells (or no shell) leave the proxy held
	 * indefinitely instead of auto-showing. Dev/test can explicitly restore
	 * the old optional posture with QDWIN_NESTED_BROKER_OPTIONAL=1.
	 * The proxy starts on held layer (invisible) and releases on `allow`,
	 * destroys on `deny`.
	 *
	 * Decide pending BEFORE building the curtain so its input region
	 * matches the broker boundary (F6#2): a PENDING proxy keeps an
	 * EMPTY input region (capture_input=false) so weston_compositor_
	 * pick_view never routes pointer input to an unapproved proxy —
	 * the held_layer is at WESTON_LAYER_POSITION_HIDDEN (below the
	 * input-transparent background), so a non-empty input region there
	 * could still be picked. A VISIBLE proxy (normal_layer) captures
	 * input (capture_input=true) so it is pickable immediately, even
	 * before the pixel feed binds (otherwise early clicks fall through
	 * and active_input_proxy never arms). The pixel-bound surface, when
	 * it later replaces the curtain, carries its own (default-full)
	 * input region. Invariant: pending => empty input; visible => full;
	 * pixel-bound => the consumer surface's own input. */
	bool gate_required = true;
	const char *opt_env = getenv("QDWIN_NESTED_BROKER_OPTIONAL");
	if (opt_env && strcmp(opt_env, "1") == 0)
		gate_required = false;
	const char *s3d_env = getenv("QDWIN_NESTED_S3D_TEST");
	const char *req_env = getenv("QDWIN_NESTED_BROKER_REQUIRED");
	if (s3d_env && strcmp(s3d_env, "1") == 0 &&
	    (!req_env || strcmp(req_env, "1") != 0))
		gate_required = false;
	bool shell_can_gate = (qdwin->shell_bound && qdwin->shell_resource &&
		wl_resource_get_version(qdwin->shell_resource) >= 8);
	bool pending = (shell_can_gate || gate_required);

	struct weston_curtain_params params = {
		.r = 0.20f, .g = 0.22f, .b = 0.28f, .a = 1.0f,
		.pos = { .c = weston_coord(cx, cy) },
		.width = w,
		.height = h,
		.capture_input = !pending,
	};
	tl->proxy_curtain =
		weston_shell_utils_curtain_create(qdwin->compositor, &params);
	if (!tl->proxy_curtain) {
		free(tl->proxy_app_id);
		free(tl->proxy_title);
		free(tl);
		return NULL;
	}
	tl->view = tl->proxy_curtain->view;

	if (pending) {
		weston_view_move_to_layer(tl->view,
					  &qdwin->held_layer.view_list);
		tl->decorated = 0;
		tl->nested_proxy_pending_decision = true;
	} else {
		weston_view_move_to_layer(tl->view,
					  &qdwin->normal_layer.view_list);
		tl->decorated = 1;
		tl->nested_proxy_pending_decision = false;
	}

	wl_list_insert(&qdwin->toplevels, &tl->link);

	weston_log("qdwin/nested-proxy: created handle=%u uid=%u app_id=%s "
		   "title=%s size=%dx%d pos=(%d,%d) pending=%d\n",
		   tl->handle, origin_uid,
		   app_id ? app_id : "(null)",
		   title ? title : "(null)", w, h, cx, cy,
		   tl->nested_proxy_pending_decision ? 1 : 0);

	/* J12 Fix B: capture the advertising container's secctx tag BEFORE the
	 * first toplevel_added (which triggers qdwin_send_toplevel_security_
	 * context) so the shell gets toplevel_security_context for this proxy on
	 * the initial advertise, not only on a later shell rebind. owner->
	 * resource's client is the tier-2 nested compositor, tagged via
	 * wp_security_context_v1 when spawned through a root launcher; that tag
	 * is the silo/app identity. An untagged advertiser (dev / un-tagged
	 * spawn) leaves proxy_secctx_set false, so nothing is emitted. */
	if (owner && owner->resource) {
		struct wl_client *adv = wl_resource_get_client(owner->resource);
		struct qdwin_secctx_client *sc =
			qdwin_secctx_client_lookup(qdwin, adv);
		if (sc) {
			tl->proxy_secctx_engine =
				qdwin_xstrdup_or_null(qdwin_secctx_client_engine(sc));
			tl->proxy_secctx_app_id =
				qdwin_xstrdup_or_null(qdwin_secctx_client_app_id(sc));
			tl->proxy_secctx_instance =
				qdwin_xstrdup_or_null(qdwin_secctx_client_instance_id(sc));
			/* The accessors are guaranteed non-NULL (""), so a NULL here
			 * can only be a failed strdup. Do NOT arm the stash on a
			 * partial capture: the send path normalises NULL to "", so we
			 * would emit a tagged-looking event with an empty field —
			 * worst case an empty instance_id, which the shell cannot
			 * match, so the placeholder times out anyway while the event
			 * claims the window IS identified. Emitting nothing is the
			 * honest degradation (absence == not sandboxed), and it keeps
			 * "a secctx event was sent" meaning "all three fields are the
			 * advertiser's". */
			tl->proxy_secctx_set = tl->proxy_secctx_engine &&
					       tl->proxy_secctx_app_id &&
					       tl->proxy_secctx_instance;
			if (!tl->proxy_secctx_set)
				weston_log("qdwin: WARN: could not stash the advertiser "
					   "secctx for nested proxy handle=%u (OOM); "
					   "emitting no toplevel_security_context\n",
					   tl->handle);
		}
	}

	qdwin_send_toplevel_added(qdwin, tl);
	if (qdwin->shell_bound && qdwin->shell_resource) {
		qdwin_shell_v1_send_toplevel_geometry(
			qdwin->shell_resource, tl->handle, cx, cy,
			(uint32_t)w, (uint32_t)h);
	}
	if (shell_can_gate) {
		qdwin_shell_v1_send_nested_proxy_pending(
			qdwin->shell_resource, tl->handle,
			app_id ? app_id : "", origin_uid);
	}
	/* §6.8 S2b: a v9 shell can spawn a pixel consumer. Emit pw_node +
	 * input_sink the consumer needs to resolve via libpipewire. */
	if (qdwin->shell_bound && qdwin->shell_resource &&
	    wl_resource_get_version(qdwin->shell_resource) >= 9 &&
	    owner) {
		qdwin_shell_v1_send_nested_proxy_pixel_source(
			qdwin->shell_resource, tl->handle,
			owner->pw_node ? owner->pw_node : "",
			owner->input_sink ? owner->input_sink : "");
	}

	return tl;
}

/* F6#2 mechanism A: defined after bind_proxy_pixels; forward-declared for the
 * allow path (qdwin_handle_nested_proxy_decision) which performs the deferred
 * curtain->pixel swap for a surface stashed while the proxy was pending. */
static bool
qdwin_nested_proxy_activate_pixel_surface(struct qdwin_toplevel *tl);

/* F6#2: set the proxy placeholder curtain's input region to full
 * (capture=true) or empty (capture=false). A solid-colour curtain surface
 * has no client to recompute its input region on commit, so we replace
 * surface->input directly; weston_compositor_pick_view reads
 * view->surface->input live (via weston_view_takes_input_at_point), so no
 * damage / geometry-dirty step is needed. Used to make an approved proxy
 * pickable while keeping a pending one input-transparent. */
static void
qdwin_nested_proxy_curtain_set_capture(struct qdwin_toplevel *tl, bool capture)
{
	if (!tl || !tl->proxy_curtain || !tl->proxy_curtain->view)
		return;
	struct weston_surface *surface = tl->proxy_curtain->view->surface;
	if (!surface)
		return;
	int w = tl->last_width  > 0 ? tl->last_width  : surface->width;
	int h = tl->last_height > 0 ? tl->last_height : surface->height;
	pixman_region32_fini(&surface->input);
	if (capture)
		pixman_region32_init_rect(&surface->input, 0, 0, w, h);
	else
		pixman_region32_init(&surface->input);
}

/* §6.8 S4: shell's verdict on a pending nested proxy.
 *
 * decision values per qdwin-shell-v1.xml:
 *   0=allow (release proxy from held to normal — visible)
 *   1=deny  (post policy_denied error on the nested toplevel resource
 *            and destroy the proxy)
 *   2=defer (keep proxy held; pending stays true; shell may re-decide
 *            later when the broker policy changes)
 *
 * Idempotent: extra/stale decisions on a non-pending toplevel log + no-op.
 */
static void
qdwin_handle_nested_proxy_decision(struct wl_client *client,
				   struct wl_resource *resource,
				   uint32_t handle,
				   uint32_t decision,
				   const char *reason)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl = qdwin_toplevel_from_handle(qdwin, handle);
	(void)client;

	if (!qdwin_shell_require_bound(qdwin, resource))
		return;
	if (!tl || !tl->is_nested_proxy) {
		weston_log("qdwin: nested_proxy_decision handle=%u: "
			   "no nested-proxy with that handle (stale?)\n",
			   handle);
		return;
	}

	switch (decision) {
	case 0: /* allow */
		if (!tl->nested_proxy_pending_decision) {
			weston_log("qdwin: nested_proxy_decision handle=%u "
				   "allow: not pending (idempotent no-op)\n",
				   handle);
			return;
		}
		qdwin_nested_proxy_apply_allow(tl, "nested_proxy_decision/allow");
		weston_log("qdwin: nested_proxy_decision handle=%u ALLOW "
			   "reason='%s'\n", handle, reason ? reason : "");
		break;
	case 1: /* deny */
		if (!tl->nested_proxy_pending_decision) {
			weston_log("qdwin: nested_proxy_decision handle=%u "
				   "deny: not pending (idempotent no-op)\n",
				   handle);
			return;
		}
		weston_log("qdwin: nested_proxy_decision handle=%u DENY "
			   "reason='%s'\n", handle, reason ? reason : "");
		if (tl->proxy_nested_owner) {
			struct qdwin_nested_toplevel *nt =
				tl->proxy_nested_owner;
			if (nt->resource) {
				wl_resource_post_error(
					nt->resource,
					QDWIN_NESTED_MANAGER_V1_ERROR_POLICY_DENIED,
					"qdistro.nested.advertise denied "
					"by admin policy: %s",
					reason ? reason : "(no reason)");
			}
			/* Break the back-edge first so the resource_destroy
			 * cleanup that follows the protocol error doesn't
			 * call qdwin_nested_proxy_destroy on already-freed
			 * memory. */
			nt->proxy_tl = NULL;
			tl->proxy_nested_owner = NULL;
			/* The protocol error tears down the resource client-
			 * side, but we destroy the proxy locally now to free
			 * the curtain immediately. */
		}
		qdwin_nested_proxy_destroy(tl);
		break;
	case 2: /* defer */
		if (!tl->nested_proxy_pending_decision) {
			weston_log("qdwin: nested_proxy_decision handle=%u "
				   "defer: not pending (idempotent no-op)\n",
				   handle);
			return;
		}
		weston_log("qdwin: nested_proxy_decision handle=%u DEFER "
			   "reason='%s' (proxy stays held)\n",
			   handle, reason ? reason : "");
		/* Keep nested_proxy_pending_decision = true. */
		break;
	default:
		weston_log("qdwin: nested_proxy_decision handle=%u "
			   "unknown decision=%u (ignored)\n",
			   handle, decision);
		break;
	}
}

/* §6.8 S2b: when the consumer wl_client (or the bound surface) goes
 * away, restore the placeholder curtain so the proxy stays visible
 * (still in the admin shell) while the shell decides what to do
 * (e.g. respawn the consumer). */
static void
qdwin_proxy_pixel_surface_destroyed(struct wl_listener *listener, void *data)
{
	(void)data;
	struct qdwin_toplevel *tl =
		wl_container_of(listener, tl, proxy_pixel_destroy_listener);
	struct qdwin *qdwin = tl->qdwin;
	/* F6#2 mechanism A: was the pixel surface the ACTIVE view, or only a
	 * pending stash (curtain still tl->view)? */
	bool was_active_pixel = (tl->proxy_pixel_view != NULL);
	struct weston_coord_global pos = tl->view
		? weston_view_get_pos_offset_global(tl->view)
		: (struct weston_coord_global){ .c = weston_coord(0, 0) };

	if (tl->proxy_pixel_view) {
		if (tl->view == tl->proxy_pixel_view)
			tl->view = NULL;
		weston_view_destroy(tl->proxy_pixel_view);
		tl->proxy_pixel_view = NULL;
	}
	tl->proxy_pixel_surface = NULL;
	if (!wl_list_empty(&tl->proxy_pixel_destroy_listener.link)) {
		wl_list_remove(&tl->proxy_pixel_destroy_listener.link);
		wl_list_init(&tl->proxy_pixel_destroy_listener.link);
	}

	/* Pending stash died and the curtain was never swapped out — it is still
	 * the active view (empty-input, held). Nothing to revert; leave it. */
	if (!was_active_pixel) {
		weston_log("qdwin/nested-proxy: stashed pixel surface destroyed "
			   "handle=%u (proxy pending; curtain still active)\n",
			   tl->handle);
		return;
	}

	weston_log("qdwin/nested-proxy: pixel surface destroyed handle=%u "
	   "(reverting to placeholder curtain)\n", tl->handle);

	/* Re-create a placeholder curtain at the current pixel-feed position +
	 * size so the user still sees something. */
	int w = tl->last_width  > 0 ? tl->last_width  : 800;
	int h = tl->last_height > 0 ? tl->last_height : 600;
	struct weston_curtain_params params = {
		.r = 0.20f, .g = 0.22f, .b = 0.28f, .a = 1.0f,
		.pos = pos,
		.width = w,
		.height = h,
		/* F6#2: preserve the input policy on recreation — an approved
		 * (non-pending) proxy whose pixel feed died must stay pickable;
		 * a still-pending one stays input-transparent. */
		.capture_input = !tl->nested_proxy_pending_decision,
	};
	tl->proxy_curtain = weston_shell_utils_curtain_create(
		qdwin->compositor, &params);
	if (tl->proxy_curtain) {
		tl->view = tl->proxy_curtain->view;
		weston_view_move_to_layer(
			tl->view,
			tl->nested_proxy_pending_decision
				? &qdwin->held_layer.view_list
				: &qdwin->normal_layer.view_list);
	}
}

static void
qdwin_handle_bind_proxy_pixels(struct wl_client *client,
			       struct wl_resource *resource,
			       uint32_t handle,
			       struct wl_resource *surface_res)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	struct qdwin_toplevel *tl = qdwin_toplevel_from_handle(qdwin, handle);
	/* §6.8 S2b: consumer wl_clients (qdistro-nested-pixelfeed) bind
	 * qdwin_shell_v1 via the standard registry path (already
	 * peer-uid-filtered to allowed_uid in bind_qdwin_shell), so we
	 * intentionally do NOT require qdwin_shell_v1.bind_as_shell here
	 * — only the admin shell calls that, but the consumer is a
	 * separate process at the same uid. The handle lookup below
	 * still rejects bogus handles. */
	if (!tl || !tl->is_nested_proxy) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
				       "bind_proxy_pixels: handle %u is not a "
				       "nested-proxy", handle);
		return;
	}
	/* Nested-proxy identity hardening: tie the pixel feed to the proxy's
	 * verified owner. The binding client must run as the same uid that
	 * advertised the proxy (proxy_origin_uid is the kernel-resolved
	 * advertise-time peer uid, not a client assertion). Without this a
	 * client could call bind_proxy_pixels on a proxy advertised by a
	 * different uid and replace its pixels with attacker-controlled
	 * content. Fail closed if the peer credential is unreadable. */
	{
		pid_t cpid = 0; uid_t cuid = 0; gid_t cgid = 0;
		wl_client_get_credentials(client, &cpid, &cuid, &cgid);
		(void)cpid; (void)cgid;
		if (cuid != (uid_t)tl->proxy_origin_uid) {
			weston_log("qdwin/nested-proxy: bind_proxy_pixels "
				   "refused handle=%u: caller uid=%u != proxy "
				   "origin uid=%u\n",
				   handle, (unsigned)cuid,
				   (unsigned)tl->proxy_origin_uid);
			wl_resource_post_error(
				resource,
				QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
				"bind_proxy_pixels: caller does not own "
				"nested-proxy handle %u", handle);
			return;
		}
	}
	struct weston_surface *ws = wl_resource_get_user_data(surface_res);
	if (!ws) {
		wl_resource_post_error(resource,
				       QDWIN_SHELL_V1_ERROR_INVALID_HANDLE,
				       "bind_proxy_pixels: surface invalid");
		return;
	}

	/* Capture the current on-screen position up front — before dropping any
	 * old view — so the swap is seamless and we never read a freed view (the
	 * respawn case below destroys the previous pixel view). */
	struct weston_coord_global pos = tl->view
		? weston_view_get_pos_offset_global(tl->view)
		: (struct weston_coord_global){ .c = weston_coord(0, 0) };

	/* If we already had a bound pixel surface OR a pending stash, drop it
	 * (consumer respawn; new wins). Under mechanism A a PENDING proxy holds
	 * proxy_pixel_surface != NULL with proxy_pixel_view == NULL, so key the
	 * listener teardown on the listener link, not only the view. */
	if (tl->proxy_pixel_surface || tl->proxy_pixel_view) {
		if (!wl_list_empty(&tl->proxy_pixel_destroy_listener.link)) {
			wl_list_remove(&tl->proxy_pixel_destroy_listener.link);
			wl_list_init(&tl->proxy_pixel_destroy_listener.link);
		}
		if (tl->proxy_pixel_view) {
			if (tl->view == tl->proxy_pixel_view)
				tl->view = NULL;
			weston_view_destroy(tl->proxy_pixel_view);
			tl->proxy_pixel_view = NULL;
		}
		tl->proxy_pixel_surface = NULL;
	}

	/* F6#2 mechanism A: do NOT swap the curtain for the pixel surface while
	 * the proxy is still PENDING the admin broker decision. v9 shells get
	 * nested_proxy_pixel_source at create (before `allow`), so the pixelfeed
	 * can bind early; a client pixel surface has a default-FULL input region,
	 * so making it the active view (even on held_layer, which sits below the
	 * input-transparent background) would let weston_compositor_pick_view
	 * route pointer input to an UNAPPROVED proxy. Stash the surface (+ its
	 * destroy listener) and keep the empty-input curtain as tl->view; the
	 * deferred swap runs on allow (qdwin_nested_proxy_activate_pixel_surface).
	 * Clearing the client surface->input is not viable — the client recomputes
	 * it on every frame commit. */
	if (tl->nested_proxy_pending_decision) {
		tl->proxy_pixel_surface = ws;
		tl->proxy_pixel_destroy_listener.notify =
			qdwin_proxy_pixel_surface_destroyed;
		wl_signal_add(&ws->destroy_signal,
			      &tl->proxy_pixel_destroy_listener);
		weston_log("qdwin/nested-proxy: bind_proxy_pixels handle=%u "
			   "surface=%p (pixel feed STASHED — proxy pending; "
			   "swap deferred to allow)\n", handle, (void *)ws);
		return;
	}

	/* Approved/visible: swap the curtain for the live pixel feed. Create the
	 * pixel view BEFORE destroying the curtain so a view-create failure leaves
	 * the curtain intact (never a NULL tl->view) — same robustness as the
	 * deferred allow path. */
	struct weston_view *pv = weston_view_create(ws);
	if (!pv) {
		weston_log("qdwin/nested-proxy: bind_proxy_pixels handle=%u "
			   "weston_view_create failed — keeping curtain\n", handle);
		return;
	}
	if (tl->proxy_curtain) {
		weston_shell_utils_curtain_destroy(tl->proxy_curtain);
		tl->proxy_curtain = NULL;
		tl->view = NULL;
	}

	tl->proxy_pixel_surface = ws;
	tl->proxy_pixel_view = pv;
	tl->view = tl->proxy_pixel_view;

	tl->proxy_pixel_destroy_listener.notify =
		qdwin_proxy_pixel_surface_destroyed;
	wl_signal_add(&ws->destroy_signal, &tl->proxy_pixel_destroy_listener);

	weston_view_set_position(tl->view, pos);
	weston_view_move_to_layer(tl->view, &qdwin->normal_layer.view_list);
	if (!weston_surface_is_mapped(ws))
		weston_surface_map(ws);
	weston_view_update_transform(tl->view);

	weston_log("qdwin/nested-proxy: bind_proxy_pixels handle=%u "
		   "surface=%p (curtain swapped for live feed)\n",
		   handle, (void *)ws);
}

/* F6#2 mechanism A: on `allow`, perform the deferred curtain->pixel swap for a
 * surface that was stashed by bind_proxy_pixels while the proxy was pending.
 * Returns true if a stashed surface was activated. Caller has already cleared
 * nested_proxy_pending_decision. */
static bool
qdwin_nested_proxy_activate_pixel_surface(struct qdwin_toplevel *tl)
{
	if (!tl || !tl->proxy_pixel_surface || tl->proxy_pixel_view)
		return false;
	struct qdwin *qdwin = tl->qdwin;
	struct weston_surface *ws = tl->proxy_pixel_surface;
	struct weston_coord_global pos = tl->view
		? weston_view_get_pos_offset_global(tl->view)
		: (struct weston_coord_global){ .c = weston_coord(0, 0) };

	tl->proxy_pixel_view = weston_view_create(ws);
	if (!tl->proxy_pixel_view) {
		/* Keep the (now-approved) curtain as a capturing fallback so we
		 * never leave tl->view NULL; drop the stash + listener. */
		weston_log("qdwin/nested-proxy: activate pixel surface handle=%u "
			   "weston_view_create failed — keeping curtain\n",
			   tl->handle);
		if (!wl_list_empty(&tl->proxy_pixel_destroy_listener.link)) {
			wl_list_remove(&tl->proxy_pixel_destroy_listener.link);
			wl_list_init(&tl->proxy_pixel_destroy_listener.link);
		}
		tl->proxy_pixel_surface = NULL;
		qdwin_nested_proxy_curtain_set_capture(tl, true);
		return false;
	}
	/* Tear down the placeholder curtain — pixel feed takes over now. */
	if (tl->proxy_curtain) {
		weston_shell_utils_curtain_destroy(tl->proxy_curtain);
		tl->proxy_curtain = NULL;
	}
	tl->view = tl->proxy_pixel_view;
	weston_view_set_position(tl->view, pos);
	weston_view_move_to_layer(tl->view, &qdwin->normal_layer.view_list);
	if (!weston_surface_is_mapped(ws))
		weston_surface_map(ws);
	weston_view_update_transform(tl->view);
	weston_log("qdwin/nested-proxy: activate pixel surface handle=%u "
		   "(deferred swap on allow)\n", tl->handle);
	return true;
}

/* F6#2: the allow-transition core. A now-approved proxy becomes input-pickable:
 * if a pixel surface was STASHED by bind_proxy_pixels while pending (mechanism
 * A) perform the deferred curtain->pixel swap now; otherwise make the
 * placeholder curtain capturing. Either way tl->view ends non-NULL and pickable
 * before the layer move. Synchronous in the compositor thread — no input
 * interleaves. Shared verbatim by the real shell decision handler (decision=0)
 * and the env-gated S3d post-allow test hook; the test hook drives THIS exact
 * code so the lane proves the real allow mechanism, not a test-only shortcut. */
static void
qdwin_nested_proxy_apply_allow(struct qdwin_toplevel *tl, const char *cause)
{
	if (!tl || !tl->is_nested_proxy || !tl->nested_proxy_pending_decision)
		return;
	tl->nested_proxy_pending_decision = false;
	if (!qdwin_nested_proxy_activate_pixel_surface(tl))
		qdwin_nested_proxy_curtain_set_capture(tl, true);
	qdwin_toplevel_release_holding(tl, cause);
}

static void
qdwin_nested_proxy_destroy(struct qdwin_toplevel *tl)
{
	if (!tl || !tl->is_nested_proxy)
		return;
	struct qdwin *qdwin = tl->qdwin;
	tl->proxy_destroying = true;
	if (qdwin->active_input_proxy == tl)
		qdwin->active_input_proxy = NULL;
	weston_log("qdwin/nested-proxy: destroy handle=%u\n", tl->handle);
	qdwin_send_toplevel_removed(qdwin, tl);

	/* Before anything that invalidates tl->view: drop the popup, the
	 * move-grab and every view_stream sourced from this proxy. */
	qdwin_toplevel_release_dependents(qdwin, tl);

	for (int s = 0; s < QDWIN_SIDES; s++)
		qdwin_chrome_detach(&tl->chrome[s]);

	if (tl->proxy_curtain) {
		weston_shell_utils_curtain_destroy(tl->proxy_curtain);
		tl->proxy_curtain = NULL;
		tl->view = NULL;
	}
	/* §6.8 S2b: drop the pixel-feed view + listener (the underlying
	 * weston_surface is owned by the consumer wl_client; we only
	 * release our view + listener). F6#2 mechanism A: a PENDING proxy can
	 * hold a stashed surface + listener with NO view, so tear the listener
	 * down based on the link, not only on proxy_pixel_view. */
	if (!wl_list_empty(&tl->proxy_pixel_destroy_listener.link)) {
		wl_list_remove(&tl->proxy_pixel_destroy_listener.link);
		wl_list_init(&tl->proxy_pixel_destroy_listener.link);
	}
	if (tl->proxy_pixel_view) {
		weston_view_destroy(tl->proxy_pixel_view);
		tl->proxy_pixel_view = NULL;
	}
	tl->proxy_pixel_surface = NULL;
	if (tl->proxy_input_sink_fd >= 0) {
		close(tl->proxy_input_sink_fd);
		tl->proxy_input_sink_fd = -1;
	}
	/* Backstop for the synchronous grab-focus callbacks above: the
	 * proxy_destroying gate keeps the picker off this toplevel, and this
	 * final conditional clear keeps the invariant true even if some other
	 * path learns to cache a proxy. A different, live proxy selected
	 * during teardown is deliberately preserved. */
	if (qdwin->active_input_proxy == tl)
		qdwin->active_input_proxy = NULL;
	wl_list_remove(&tl->link);
	free(tl->proxy_app_id);
	free(tl->proxy_title);
	free(tl->proxy_secctx_engine);
	free(tl->proxy_secctx_app_id);
	free(tl->proxy_secctx_instance);
	free(tl->proxy_remote_source_machine);
	free(tl->proxy_remote_trust_domain_id);
	free(tl->proxy_remote_stream_id);
	free(tl);
}

static void
qdwin_nested_proxy_set_title(struct qdwin_toplevel *tl, const char *title)
{
	if (!tl || !tl->is_nested_proxy)
		return;
	free(tl->proxy_title);
	tl->proxy_title = qdwin_xstrdup_or_null(title);
	struct qdwin *qdwin = tl->qdwin;
	if (qdwin->shell_bound && qdwin->shell_resource) {
		qdwin_shell_v1_send_toplevel_title(
			qdwin->shell_resource, tl->handle,
			tl->proxy_title ? tl->proxy_title : "");
	}
}

static void
qdwin_nested_proxy_set_app_id(struct qdwin_toplevel *tl, const char *app_id)
{
	if (!tl || !tl->is_nested_proxy)
		return;
	free(tl->proxy_app_id);
	tl->proxy_app_id = qdwin_xstrdup_or_null(app_id);
	qdwin_emit_toplevel_app_id(tl->qdwin, tl,
				    tl->proxy_app_id ? tl->proxy_app_id : "");
	weston_log("qdwin/nested-proxy: handle=%u app_id updated to %s\n",
		   tl->handle, app_id ? app_id : "");
}

static void
qdwin_nested_proxy_set_geometry(struct qdwin_toplevel *tl, int w, int h)
{
	if (!tl || !tl->is_nested_proxy || !tl->proxy_curtain)
		return;
	if (w <= 0) w = 800;
	if (h <= 0) h = 600;
	if (tl->last_width == w && tl->last_height == h)
		return;
	struct qdwin *qdwin = tl->qdwin;

	/* Recreate curtain at new size. weston_curtain has no public
	 * resize, so destroy+recreate keeping position. */
	struct weston_coord_global pos =
		weston_view_get_pos_offset_global(tl->view);
	weston_shell_utils_curtain_destroy(tl->proxy_curtain);
	tl->proxy_curtain = NULL;
	tl->view = NULL;

	struct weston_curtain_params params = {
		.r = 0.20f, .g = 0.22f, .b = 0.28f, .a = 1.0f,
		.pos = pos,
		.width = w,
		.height = h,
		/* F6#2: preserve the input policy across resize (curtain is
		 * destroyed+recreated): approved => pickable, pending =>
		 * input-transparent. */
		.capture_input = !tl->nested_proxy_pending_decision,
	};
	tl->proxy_curtain =
		weston_shell_utils_curtain_create(qdwin->compositor, &params);
	if (!tl->proxy_curtain) {
		weston_log("qdwin/nested-proxy: handle=%u resize curtain_create "
			   "failed (toplevel pixels gone)\n", tl->handle);
		return;
	}
	tl->view = tl->proxy_curtain->view;
	weston_view_move_to_layer(
		tl->view,
		tl->nested_proxy_pending_decision
			? &qdwin->held_layer.view_list
			: &qdwin->normal_layer.view_list);
	tl->last_width  = w;
	tl->last_height = h;

	if (qdwin->shell_bound && qdwin->shell_resource) {
		qdwin_shell_v1_send_toplevel_geometry(
			qdwin->shell_resource, tl->handle,
			(int)pos.c.x, (int)pos.c.y,
			(uint32_t)w, (uint32_t)h);
	}
	weston_log("qdwin/nested-proxy: handle=%u resized to %dx%d\n",
		   tl->handle, w, h);
}

static uid_t
qdwin_parse_allowed_uid(int argc, char *argv[])
{
	const char *env = getenv("QDWIN_ALLOWED_UID");
	long v;

	for (int i = 1; i < argc; i++) {
		if (strncmp(argv[i], "--qdwin-allowed-uid=", 20) == 0) {
			env = argv[i] + 20;
			break;
		}
	}

	if (!env || !*env)
		return (uid_t)getuid();

	v = strtol(env, NULL, 10);
	if (v < 0)
		return (uid_t)getuid();
	return (uid_t)v;
}

/* Parse an optional string config value from `--<argprefix>=VALUE` or
 * the given environment variable (argv wins). Returns a heap-allocated
 * copy, or NULL when the option is unset/empty. *was_set is set to true
 * iff a non-empty value was present in argv/env, so the caller can tell
 * "unset" (NULL, was_set=false) from "configured but strdup() OOM'd"
 * (NULL, was_set=true) and fail closed on the latter rather than
 * silently disabling a security check. */
static char *
qdwin_parse_str_opt(int argc, char *argv[], const char *argprefix,
		    const char *envname, bool *was_set)
{
	const char *val = envname ? getenv(envname) : NULL;
	size_t prefixlen = strlen(argprefix);

	for (int i = 1; i < argc; i++) {
		if (strncmp(argv[i], argprefix, prefixlen) == 0) {
			val = argv[i] + prefixlen;
			break;
		}
	}

	if (!val || !*val) {
		*was_set = false;
		return NULL;
	}
	*was_set = true;
	/* F7: log on OOM. A NULL return with *was_set==true is the documented
	 * "configured but strdup() OOM'd" state the callers fail closed on. */
	return qdwin_xstrdup_or_null(val);
}

/* Parse a boolean opt-in flag from a bare `--<flag>` argv switch or a
 * truthy env var (argv presence OR env in {1,true,yes,on} => true).
 * Used for conscious dev/test opt-outs (e.g. weakening the locker bind
 * policy to uid-only). Returns true if the flag is set, false otherwise. */
static bool
qdwin_parse_flag(int argc, char *argv[], const char *argflag,
		 const char *envname)
{
	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], argflag) == 0)
			return true;
	}
	const char *v = envname ? getenv(envname) : NULL;
	if (v && (strcmp(v, "1") == 0 || strcmp(v, "true") == 0 ||
		  strcmp(v, "yes") == 0 || strcmp(v, "on") == 0))
		return true;
	return false;
}

/* Parse an optional uid config value from `--<argprefix>=VALUE` or env
 * (argv wins). Tri-state, fail-closed:
 *   - unset/empty           -> returns true, *was_set=false (check skipped)
 *   - present and valid     -> returns true, *was_set=true, *out set
 *   - present but malformed  -> returns false (caller must abort init
 *                              rather than silently skip/misapply the
 *                              configured uid check)
 * "valid" = the whole string is a non-negative decimal integer that fits
 * in uid_t and is not the all-ones (uid_t)-1 sentinel (no partial parses
 * like "123abc", no overflow, no "-1"). */
static bool
qdwin_parse_uid_opt(int argc, char *argv[], const char *argprefix,
		    const char *envname, uid_t *out, bool *was_set)
{
	const char *val = envname ? getenv(envname) : NULL;
	size_t prefixlen = strlen(argprefix);

	for (int i = 1; i < argc; i++) {
		if (strncmp(argv[i], argprefix, prefixlen) == 0) {
			val = argv[i] + prefixlen;
			break;
		}
	}

	*was_set = false;
	if (!val || !*val)
		return true;  /* unset => check skipped, not an error */

	errno = 0;
	char *end = NULL;
	long long v = strtoll(val, &end, 10);
	if (errno != 0 || end == val || *end != '\0' || v < 0 ||
	    (unsigned long long)v > (unsigned long long)((uid_t)-1) - 1)
		return false;  /* present but malformed => fail closed */
	*out = (uid_t)v;
	*was_set = true;
	return true;
}

/* ------------------------------------------------------------------
 * §6.10 wp_security_context_v1 — sandboxed-client identity.
 *
 * The protocol lets a sandbox engine (waypipe, flatpak, bubblewrap,
 * ...) register a listening socket and attach (sandbox_engine,
 * app_id, instance_id) metadata to all clients arriving on it. The
 * compositor owns the listening fd from `commit` onwards — accept()
 * on the fd, create a wl_client per accepted connection, tag it with
 * the secctx for later lookup.
 *
 * For qdistro the win is twofold:
 *
 *   1. waypipe 0.11's `--secctx` stops being a hard-fail (it required
 *      the global to be present), unblocking tier-3's signed
 *      identity path.
 *   2. qdwin can apply per-app policy (uid filter bypass, broker
 *      lookup keyed on app_id) for sandboxed clients without
 *      relying on PEERCRED.
 *
 * The protocol forbids nesting: a client that already has a secctx
 * attached isn't allowed to bind the manager. We enforce via a
 * global filter installed in wet_shell_init.
 * ------------------------------------------------------------------ */

struct qdwin_secctx {
	struct qdwin *qdwin;
	struct wl_resource *resource;       /* wp_security_context_v1 */
	int listen_fd;
	int close_fd;
	char *sandbox_engine;
	char *app_id;
	char *instance_id;
	int committed;
	struct wl_event_source *listen_source;
	struct wl_event_source *close_source;
	struct wl_list link;                /* qdwin::secctxs */
};

struct qdwin_secctx_client {
	struct wl_client *client;
	struct qdwin_secctx *secctx;        /* MAY become NULL when the
					     * listener is torn down before
					     * its accepted clients exit; the
					     * per-client metadata duplicated
					     * below survives the dangling
					     * pointer. */
	/* Per-client metadata copy. Filled at accept-time from secctx
	 * fields. Decoupled from `secctx` lifetime because per spec the
	 * listener outlives its creator AND the accepted clients outlive
	 * the listener — there's no symmetric guarantee on the tag struct,
	 * so we replicate to keep tag-introspection (qdwin_send_toplevel_
	 * security_context) safe. */
	char *sandbox_engine;
	char *app_id;
	char *instance_id;
	/* Option-B identity tuple, snapshotted at accept-time so the broker
	 * can re-verify the live process against /proc at decision time.
	 * (peer_pid, peer_starttime) is the anti-PID-reuse key; peer_exe
	 * and peer_selinux_label are empty if unreadable. See
	 * todo/decisions/secctx-identity-contract.md. */
	uint32_t peer_pid;
	uint64_t peer_starttime;
	uint32_t peer_uid;
	char *peer_exe;
	char *peer_selinux_label;
	struct wl_listener client_destroy_listener;
	struct wl_list link;                /* qdwin::secctx_clients */
};

static struct qdwin_secctx_client *
qdwin_secctx_client_find(struct qdwin *qdwin, struct wl_client *client)
{
	struct qdwin_secctx_client *sc;
	wl_list_for_each(sc, &qdwin->secctx_clients, link)
		if (sc->client == client)
			return sc;
	return NULL;
}

/* Public: returns the per-client secctx tag entry for `client`, or NULL
 * when unsandboxed. The entry's `secctx` pointer MAY be NULL if the
 * underlying listener was torn down before the client exited (per
 * spec the listener outlives its creator AND accepted clients outlive
 * the listener). The entry's snapshotted strings (sandbox_engine,
 * app_id, instance_id) survive that teardown. */
static struct qdwin_secctx_client *
qdwin_secctx_client_lookup(struct qdwin *qdwin, struct wl_client *client)
{
	return qdwin_secctx_client_find(qdwin, client);
}

static const char *qdwin_secctx_client_engine(struct qdwin_secctx_client *sc)
{ return sc && sc->sandbox_engine ? sc->sandbox_engine : ""; }
static const char *qdwin_secctx_client_app_id(struct qdwin_secctx_client *sc)
{ return sc && sc->app_id ? sc->app_id : ""; }
static const char *qdwin_secctx_client_instance_id(struct qdwin_secctx_client *sc)
{ return sc && sc->instance_id ? sc->instance_id : ""; }
static uint32_t qdwin_secctx_client_peer_pid(struct qdwin_secctx_client *sc)
{ return sc ? sc->peer_pid : 0; }
static uint64_t qdwin_secctx_client_peer_starttime(struct qdwin_secctx_client *sc)
{ return sc ? sc->peer_starttime : 0; }
static uint32_t qdwin_secctx_client_peer_uid(struct qdwin_secctx_client *sc)
{ return sc ? sc->peer_uid : 0; }
static const char *qdwin_secctx_client_peer_exe(struct qdwin_secctx_client *sc)
{ return sc && sc->peer_exe ? sc->peer_exe : ""; }
static const char *qdwin_secctx_client_peer_selinux_label(struct qdwin_secctx_client *sc)
{ return sc && sc->peer_selinux_label ? sc->peer_selinux_label : ""; }

static void
qdwin_secctx_client_on_destroy(struct wl_listener *l, void *data)
{
	struct qdwin_secctx_client *sc =
		wl_container_of(l, sc, client_destroy_listener);
	(void)data;
	wl_list_remove(&sc->link);
	wl_list_remove(&sc->client_destroy_listener.link);
	free(sc->sandbox_engine);
	free(sc->app_id);
	free(sc->instance_id);
	free(sc->peer_exe);
	free(sc->peer_selinux_label);
	free(sc);
}

/* Option-B identity capture helpers. Read /proc/<pid>/stat field 22
 * (starttime, the kernel's PID-reuse-safe pinning value), /proc/<pid>/exe
 * (resolved), and /proc/<pid>/attr/current (SELinux label). All return
 * a heap-allocated string the caller must free; readlink/read errors
 * map to an empty string so downstream consumers see "unverifiable" not
 * "spoofed". starttime is read into a uint64. */
static uint64_t
qdwin_proc_starttime(pid_t pid)
{
	if (pid <= 0)
		return 0;
	char path[64];
	snprintf(path, sizeof path, "/proc/%d/stat", (int)pid);
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return 0;
	char buf[2048];
	ssize_t n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return 0;
	buf[n] = '\0';
	/* The `comm` field is parenthesised and may contain spaces; skip
	 * past the *last* ')' so subsequent space-tokenisation is safe. */
	char *p = strrchr(buf, ')');
	if (!p)
		return 0;
	p++;
	/* After the ')' there's a leading space then field 3 (state).
	 * Field 22 (starttime) is the 20th whitespace-separated token after
	 * the ')'. */
	int field = 2;  /* the ')' itself is field 2 */
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
	unsigned long long v = 0;
	while (*p >= '0' && *p <= '9') {
		v = v * 10 + (unsigned)(*p - '0');
		p++;
	}
	return (uint64_t)v;
}

static char *
qdwin_proc_exe(pid_t pid)
{
	if (pid <= 0)
		return strdup("");
	char path[64];
	snprintf(path, sizeof path, "/proc/%d/exe", (int)pid);
	char buf[PATH_MAX];
	ssize_t n = readlink(path, buf, sizeof(buf) - 1);
	if (n <= 0)
		return strdup("");
	/* A full-buffer read may be a silently truncated path; treat it as
	 * unverifiable ("") rather than risk a prefix matching a configured
	 * expectation. */
	if ((size_t)n >= sizeof(buf) - 1)
		return strdup("");
	buf[n] = '\0';
	/* F7: log on OOM. Consumers treat a NULL exe as "OOM → unverifiable →
	 * reject (fail closed)" — see the locker/layer-shell/IME bind gates. */
	return qdwin_xstrdup_or_null(buf);
}

static char *
qdwin_proc_selinux_label(pid_t pid)
{
	if (pid <= 0)
		return strdup("");
	char path[64];
	snprintf(path, sizeof path, "/proc/%d/attr/current", (int)pid);
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return strdup("");
	char buf[512];
	ssize_t n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n <= 0)
		return strdup("");
	/* A full-buffer read may be a silently truncated label; treat it as
	 * unverifiable ("") rather than risk a prefix matching a configured
	 * expectation. SELinux labels are far shorter than this buffer. */
	if ((size_t)n >= sizeof(buf) - 1)
		return strdup("");
	buf[n] = '\0';
	/* Kernel appends a trailing NUL or newline; strip any trailing
	 * whitespace/NUL bytes so the broker's string compare is stable. */
	while (n > 0 && (buf[n - 1] == '\n' || buf[n - 1] == '\0' ||
			 buf[n - 1] == ' ' || buf[n - 1] == '\r')) {
		buf[--n] = '\0';
	}
	/* F7: log on OOM. A NULL label is treated as "unverifiable → fail
	 * closed" by every bind gate that compares it. */
	return qdwin_xstrdup_or_null(buf);
}

/* Read argv[1] (the launcher's first argument) from /proc/<pid>/cmdline.
 * For a Python console-script started as `ExecStart=/usr/local/bin/qdlocker`
 * the kernel hands the shebang interpreter the script path as argv[1], so
 * cmdline is "<interp>\0<script-path>\0...". This is how we recover the
 * locker's real launcher path when /proc/<pid>/exe is only the interpreter.
 * Returns a heap string the caller frees; "" on any read error or if there
 * is no argv[1] (fail-closed — an empty expectation can never be matched).
 *
 * Caveat (documented at the call site): a process can rewrite its own argv
 * memory, so cmdline is not as authoritative as exe. The default policy
 * pairs this with a stat() of the *named file* (must be the canonical,
 * root-owned entrypoint), which raises the bar well above "any same-uid
 * binary": a casual impostor (the shell, weston-terminal, a naive rogue)
 * has neither a python exe nor an argv[1] pointing at the entrypoint, so it
 * is rejected. */
static char *
qdwin_proc_argv1(pid_t pid)
{
	if (pid <= 0)
		return strdup("");
	char path[64];
	snprintf(path, sizeof path, "/proc/%d/cmdline", (int)pid);
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return strdup("");
	/* cmdline is NUL-separated argv. We only need the first two fields.
	 * Bound the read; a path arg far longer than PATH_MAX is bogus. */
	char buf[2 * PATH_MAX];
	size_t off = 0;
	ssize_t n;
	while (off < sizeof buf - 1 &&
	       (n = read(fd, buf + off, sizeof buf - 1 - off)) > 0)
		off += (size_t)n;
	close(fd);
	if (off == 0)
		return strdup("");
	buf[off] = '\0';
	/* argv[0] is the first NUL-terminated token; argv[1] starts after it. */
	size_t a0 = strnlen(buf, off);
	if (a0 >= off)            /* no NUL after argv[0] => no argv[1] */
		return strdup("");
	const char *a1 = buf + a0 + 1;
	if (*a1 == '\0')          /* empty argv[1] */
		return strdup("");
	/* Reject a silently-truncated argv[1] (no terminating NUL within the
	 * bytes we read) rather than risk matching a prefix of the entrypoint. */
	size_t a1max = off - (a0 + 1);
	if (strnlen(a1, a1max) >= a1max)
		return strdup("");
	/* F7: log on OOM. A NULL argv1 makes qdwin_path_is_trusted_entrypoint
	 * fail (entrypoint policy rejects), i.e. fail closed. */
	return qdwin_xstrdup_or_null(a1);
}

/* Is `exe` (a resolved /proc/<pid>/exe path) a system script interpreter
 * that legitimately launches a console-script? We accept a python3.x binary
 * living under a trusted system bindir. Basename must start with "python".
 * This is deliberately narrow: the genuine qdlocker is launched via
 * `#!/usr/bin/python3`, so its exe resolves to /usr/bin/python3.N. */
static bool
qdwin_exe_is_system_interpreter(const char *exe)
{
	if (!exe || !*exe)
		return false;
	/* Must be an absolute path under a trusted system bindir. */
	static const char *const dirs[] = {
		"/usr/bin/", "/bin/", "/usr/local/bin/",
	};
	const char *base = NULL;
	for (size_t i = 0; i < sizeof dirs / sizeof dirs[0]; i++) {
		size_t dl = strlen(dirs[i]);
		if (strncmp(exe, dirs[i], dl) == 0) {
			base = exe + dl;
			break;
		}
	}
	if (!base || !*base || strchr(base, '/'))
		return false;           /* nested path => not a bare bindir entry */
	return strncmp(base, "python", 6) == 0;
}

/* True iff `path` is the canonical, root-owned, non-group/other-writable
 * regular file we expect the locker entrypoint to be. realpath() both the
 * candidate and the configured entrypoint so symlinks/.. cannot disguise a
 * different file, then require the resolved paths to be identical AND the
 * file to be root-owned and not writable by group/other (so a same-uid
 * attacker cannot have planted or rewritten it). Fail-closed on any error.
 *
 * `expected` may be a colon-separated LIST of acceptable entrypoint paths
 * (the locker's install location is profile-dependent — /usr/local/bin vs
 * /usr/bin — and there is no single canonical path). The candidate matches
 * if it resolves to ANY listed entrypoint that also passes the ownership/
 * permission checks. An empty/whitespace list element is ignored. */
static bool
qdwin_path_is_trusted_entrypoint(const char *cand, const char *expected)
{
	if (!cand || !*cand || !expected || !*expected)
		return false;
	char rc[PATH_MAX];
	if (!realpath(cand, rc))
		return false;

	/* Iterate the colon-separated expected list. Copy onto the stack so
	 * we can NUL-split without mutating the caller's string. A single
	 * configured path (the common case) is just a one-element list. */
	char list[2 * PATH_MAX];
	if (strlen(expected) >= sizeof list)
		return false;           /* implausibly long => fail closed */
	memcpy(list, expected, strlen(expected) + 1);

	char *save = NULL;
	for (char *tok = strtok_r(list, ":", &save); tok;
	     tok = strtok_r(NULL, ":", &save)) {
		if (!*tok)
			continue;
		char re[PATH_MAX];
		if (!realpath(tok, re))
			continue;           /* this candidate path absent => try next */
		if (strcmp(rc, re) != 0)
			continue;
		struct stat st;
		if (stat(rc, &st) < 0)
			return false;
		if (!S_ISREG(st.st_mode))
			return false;
		if (st.st_uid != 0)
			return false;
		if (st.st_mode & (S_IWGRP | S_IWOTH))
			return false;
		return true;            /* matched a trusted entrypoint */
	}
	return false;
}

static int
qdwin_secctx_listen_cb(int fd, uint32_t mask, void *data)
{
	struct qdwin_secctx *sec = data;
	struct qdwin *qdwin = sec->qdwin;
	(void)mask;
	int client_fd = accept4(fd, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
	if (client_fd < 0) {
		if (errno != EAGAIN && errno != EWOULDBLOCK)
			weston_log("qdwin: secctx accept(%s/%s) failed: %s\n",
				   sec->sandbox_engine ? sec->sandbox_engine : "?",
				   sec->app_id ? sec->app_id : "?",
				   strerror(errno));
		return 0;
	}
	struct wl_client *new_client =
		wl_client_create(qdwin->compositor->wl_display, client_fd);
	if (!new_client) {
		close(client_fd);
		return 0;
	}
	struct qdwin_secctx_client *sc = calloc(1, sizeof *sc);
	if (!sc) {
		/* Connection accepted but tagging failed — close the
		 * client to keep state consistent (otherwise we'd treat
		 * a sandboxed client as unsandboxed, which is unsafe). */
		wl_client_destroy(new_client);
		return 0;
	}
	sc->client = new_client;
	sc->secctx = sec;
	/* Snapshot tag metadata into the client so introspection (the v13
	 * toplevel_security_context fanout) stays valid even after the
	 * listener (and its qdwin_secctx) is torn down. */
	/* F7: log on OOM. A NULL leaves the accessor falling back to "" — the
	 * client fails closed (an empty identity matches no silo gate) rather
	 * than crashing; visibility lets us spot the rare OOM truncation. */
	sc->sandbox_engine = qdwin_xstrdup_or_null(sec->sandbox_engine);
	sc->app_id         = qdwin_xstrdup_or_null(sec->app_id);
	sc->instance_id    = qdwin_xstrdup_or_null(sec->instance_id);
	/* Option-B identity capture: SO_PEERCRED on the accepted socket
	 * gives us the (pid, uid) the kernel pinned at connect-time, which
	 * the broker re-verifies against /proc at decision time. We snapshot
	 * starttime + exe + SELinux label here because the underlying
	 * process may exit before the broker call; the snapshot lets the
	 * broker still detect "the process is gone" vs "it changed identity". */
	pid_t peer_pid = 0;
	uid_t peer_uid = 0;
	gid_t peer_gid_unused = 0;
	wl_client_get_credentials(new_client, &peer_pid, &peer_uid,
				  &peer_gid_unused);
	sc->peer_pid = (uint32_t)peer_pid;
	sc->peer_uid = (uint32_t)peer_uid;
	sc->peer_starttime = qdwin_proc_starttime(peer_pid);
	sc->peer_exe = qdwin_proc_exe(peer_pid);
	sc->peer_selinux_label = qdwin_proc_selinux_label(peer_pid);
	sc->client_destroy_listener.notify = qdwin_secctx_client_on_destroy;
	wl_client_add_destroy_listener(new_client,
				       &sc->client_destroy_listener);
	wl_list_insert(&qdwin->secctx_clients, &sc->link);
	weston_log("qdwin/secctx: client accepted engine=%s app_id=%s "
		   "instance_id=%s (total tagged: %u)\n",
		   sec->sandbox_engine ? sec->sandbox_engine : "?",
		   sec->app_id ? sec->app_id : "?",
		   sec->instance_id ? sec->instance_id : "?",
		   (unsigned)wl_list_length(&qdwin->secctx_clients));
	return 0;
}

static void
qdwin_secctx_destroy(struct qdwin_secctx *sec);

static int
qdwin_secctx_close_cb(int fd, uint32_t mask, void *data)
{
	struct qdwin_secctx *sec = data;
	(void)fd; (void)mask;
	weston_log("qdwin/secctx: close_fd hangup engine=%s app_id=%s "
		   "→ tearing down listener\n",
		   sec->sandbox_engine ? sec->sandbox_engine : "?",
		   sec->app_id ? sec->app_id : "?");
	qdwin_secctx_destroy(sec);
	return 0;
}

static void qdwin_secctx_destroy(struct qdwin_secctx *sec);

static void
qdwin_secctx_destroy_all(struct qdwin *qdwin)
{
	struct qdwin_secctx *sec, *tmp;
	wl_list_for_each_safe(sec, tmp, &qdwin->secctxs, link)
		qdwin_secctx_destroy(sec);
}

static void
qdwin_secctx_destroy(struct qdwin_secctx *sec)
{
	if (sec->listen_source) {
		wl_event_source_remove(sec->listen_source);
		sec->listen_source = NULL;
	}
	if (sec->close_source) {
		wl_event_source_remove(sec->close_source);
		sec->close_source = NULL;
	}
	if (sec->listen_fd >= 0) { close(sec->listen_fd); sec->listen_fd = -1; }
	if (sec->close_fd >= 0) { close(sec->close_fd); sec->close_fd = -1; }
	wl_list_remove(&sec->link);
	/* Detach already-accepted clients so they don't dangle a freed
	 * pointer. Each client snapshots its own copy of the engine /
	 * app_id / instance_id at accept-time, so introspection (the v13
	 * fanout) keeps working post-tear-down. */
	struct qdwin_secctx_client *sc;
	wl_list_for_each(sc, &sec->qdwin->secctx_clients, link) {
		if (sc->secctx == sec)
			sc->secctx = NULL;
	}
	/* Detach the context resource too. After commit the listener can
	 * outlive the client's wp_security_context_v1 resource (e.g. this
	 * destroy fires from the close_fd HANGUP), so leaving the resource's
	 * user_data pointing at the about-to-be-freed `sec` is a
	 * use-after-free the moment the client later destroys the resource or
	 * disconnects. Clear it; qdwin_secctx_resource_destroy already no-ops
	 * on a NULL user_data. (When this is itself called from that
	 * destructor for the pre-commit case, clearing the in-flight
	 * resource's user_data is harmless.) */
	if (sec->resource) {
		wl_resource_set_user_data(sec->resource, NULL);
		sec->resource = NULL;
	}
	free(sec->sandbox_engine);
	free(sec->app_id);
	free(sec->instance_id);
	free(sec);
}

static void
qdwin_secctx_destroy_req(struct wl_client *client,
			 struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

/* findings F7: cap externally-settable secctx identity strings. These are
 * later strdup'd, logged, forwarded to the shell, and compared (incl. the D1/D3
 * (engine, app_id) silo matching), so an unbounded value is a needless DoS
 * amplifier. Legitimate engine/app_id/instance values are short (tens of
 * bytes); reject anything absurd with a protocol error. */
#define QDWIN_SECCTX_STR_MAX 4096

/* secctx tags are advisory routing metadata — qdwin forwards them to the
 * shell but broker verifies identity via starttime + uid (always) and
 * exe + SELinux label (when available).  See doc/protocol.md
 * "Security posture: wp_security_context_v1". */
static void
qdwin_secctx_set_sandbox_engine(struct wl_client *client,
				struct wl_resource *resource,
				const char *name)
{
	struct qdwin_secctx *sec = wl_resource_get_user_data(resource);
	(void)client;
	if (!sec)   /* listener already torn down (close_fd HUP); dead resource */
		return;
	if (sec->committed) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_USED,
				       "set_sandbox_engine after commit");
		return;
	}
	if (sec->sandbox_engine) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_SET,
				       "sandbox_engine already set");
		return;
	}
	if (name && strlen(name) > QDWIN_SECCTX_STR_MAX) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_INVALID_METADATA,
				       "sandbox_engine too long");
		return;
	}
	/* F7: on OOM after a non-NULL value, tell the client rather than
	 * silently storing NULL ("unset"), which would misrepresent a tagged
	 * client as untagged. */
	if (name) {
		sec->sandbox_engine = strdup(name);
		if (!sec->sandbox_engine) {
			wl_resource_post_no_memory(resource);
			return;
		}
	}
}

static void
qdwin_secctx_set_app_id(struct wl_client *client,
			struct wl_resource *resource, const char *app_id)
{
	struct qdwin_secctx *sec = wl_resource_get_user_data(resource);
	(void)client;
	if (!sec)   /* listener already torn down (close_fd HUP); dead resource */
		return;
	if (sec->committed) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_USED,
				       "set_app_id after commit");
		return;
	}
	if (sec->app_id) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_SET,
				       "app_id already set");
		return;
	}
	if (app_id && strlen(app_id) > QDWIN_SECCTX_STR_MAX) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_INVALID_METADATA,
				       "app_id too long");
		return;
	}
	/* F7: signal OOM instead of silently storing NULL — see set_sandbox_engine. */
	if (app_id) {
		sec->app_id = strdup(app_id);
		if (!sec->app_id) {
			wl_resource_post_no_memory(resource);
			return;
		}
	}
}

static void
qdwin_secctx_set_instance_id(struct wl_client *client,
			     struct wl_resource *resource,
			     const char *instance_id)
{
	struct qdwin_secctx *sec = wl_resource_get_user_data(resource);
	(void)client;
	if (!sec)   /* listener already torn down (close_fd HUP); dead resource */
		return;
	if (sec->committed) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_USED,
				       "set_instance_id after commit");
		return;
	}
	if (sec->instance_id) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_SET,
				       "instance_id already set");
		return;
	}
	if (instance_id && strlen(instance_id) > QDWIN_SECCTX_STR_MAX) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_INVALID_METADATA,
				       "instance_id too long");
		return;
	}
	/* F7: signal OOM instead of silently storing NULL — see set_sandbox_engine. */
	if (instance_id) {
		sec->instance_id = strdup(instance_id);
		if (!sec->instance_id) {
			wl_resource_post_no_memory(resource);
			return;
		}
	}
}

static void
qdwin_secctx_commit(struct wl_client *client, struct wl_resource *resource)
{
	struct qdwin_secctx *sec = wl_resource_get_user_data(resource);
	if (!sec)   /* listener already torn down (close_fd HUP); dead resource */
		return;
	struct qdwin *qdwin = sec->qdwin;
	(void)client;
	if (sec->committed) {
		wl_resource_post_error(resource,
				       WP_SECURITY_CONTEXT_V1_ERROR_ALREADY_USED,
				       "commit twice");
		return;
	}
	sec->committed = 1;
	struct wl_event_loop *loop =
		wl_display_get_event_loop(qdwin->compositor->wl_display);
	sec->listen_source = wl_event_loop_add_fd(
		loop, sec->listen_fd, WL_EVENT_READABLE,
		qdwin_secctx_listen_cb, sec);
	sec->close_source = wl_event_loop_add_fd(
		loop, sec->close_fd, WL_EVENT_HANGUP | WL_EVENT_ERROR,
		qdwin_secctx_close_cb, sec);
	if (!sec->listen_source || !sec->close_source) {
		weston_log("qdwin/secctx: event_loop_add_fd failed for "
			   "engine=%s app_id=%s\n",
			   sec->sandbox_engine ? sec->sandbox_engine : "?",
			   sec->app_id ? sec->app_id : "?");
		qdwin_secctx_destroy(sec);
		return;
	}
	weston_log("qdwin/secctx: committed engine=%s app_id=%s "
		   "instance_id=%s listen_fd=%d close_fd=%d\n",
		   sec->sandbox_engine ? sec->sandbox_engine : "?",
		   sec->app_id ? sec->app_id : "?",
		   sec->instance_id ? sec->instance_id : "?",
		   sec->listen_fd, sec->close_fd);
}

static const struct wp_security_context_v1_interface qdwin_secctx_impl = {
	.destroy = qdwin_secctx_destroy_req,
	.set_sandbox_engine = qdwin_secctx_set_sandbox_engine,
	.set_app_id = qdwin_secctx_set_app_id,
	.set_instance_id = qdwin_secctx_set_instance_id,
	.commit = qdwin_secctx_commit,
};

static void
qdwin_secctx_resource_destroy(struct wl_resource *resource)
{
	struct qdwin_secctx *sec = wl_resource_get_user_data(resource);
	if (!sec)
		return;
	/* If commit fired, the listener stays alive past the client's
	 * resource lifetime (per spec — "must continue to accept
	 * connections ... when the Wayland client which created the
	 * security context disconnects"). Otherwise tear down. */
	if (!sec->committed)
		qdwin_secctx_destroy(sec);
	else
		sec->resource = NULL;  /* listener detaches from client */
}

static void
qdwin_secctx_manager_destroy_req(struct wl_client *client,
				 struct wl_resource *resource)
{
	(void)client;
	wl_resource_destroy(resource);
}

static void
qdwin_secctx_manager_create_listener(struct wl_client *client,
				     struct wl_resource *resource,
				     uint32_t id,
				     int32_t listen_fd,
				     int32_t close_fd)
{
	struct qdwin *qdwin = wl_resource_get_user_data(resource);
	if (listen_fd < 0) {
		wl_resource_post_error(resource,
		    WP_SECURITY_CONTEXT_MANAGER_V1_ERROR_INVALID_LISTEN_FD,
		    "listen_fd is invalid");
		if (close_fd >= 0) close(close_fd);
		return;
	}
	struct qdwin_secctx *sec = calloc(1, sizeof *sec);
	if (!sec) {
		wl_client_post_no_memory(client);
		close(listen_fd);
		if (close_fd >= 0) close(close_fd);
		return;
	}
	sec->qdwin = qdwin;
	sec->listen_fd = listen_fd;
	sec->close_fd = close_fd;
	sec->resource = wl_resource_create(client,
		&wp_security_context_v1_interface,
		wl_resource_get_version(resource), id);
	if (!sec->resource) {
		close(listen_fd);
		if (close_fd >= 0) close(close_fd);
		free(sec);
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(sec->resource, &qdwin_secctx_impl,
				       sec, qdwin_secctx_resource_destroy);
	wl_list_insert(&qdwin->secctxs, &sec->link);
}

static const struct wp_security_context_manager_v1_interface
qdwin_secctx_manager_impl = {
	.destroy = qdwin_secctx_manager_destroy_req,
	.create_listener = qdwin_secctx_manager_create_listener,
};

static bool
qdwin_secctx_open_dev_mode(void)
{
	const char *open_env = getenv("QDWIN_SECCTX_OPEN");
	return open_env && strcmp(open_env, "1") == 0;
}

static bool
qdwin_secctx_exe_allowed(const char *exe)
{
	const char *env = getenv("QDWIN_ALLOWED_SECCTX_HELPER_EXE");

	if (!exe || !*exe)
		return false;
	if (env && *env)
		return strcmp(exe, env) == 0;
	return strcmp(exe, "/usr/bin/qdistro-secctx-exec") == 0 ||
	       strcmp(exe, "/usr/local/bin/qdistro-secctx-exec") == 0;
}

static int
qdwin_proc_environ_has(pid_t pid, const char *needle)
{
	char path[64], buf[4096], prev_tail[256];
	int fd;
	ssize_t n;
	size_t needle_len = strlen(needle);
	bool prefix = needle_len > 0 && needle[needle_len - 1] == '=';
	size_t tail_len = 0;
	size_t total = 0;
	bool tail_starts_entry = false;

	if (pid <= 0 || needle_len == 0 || needle_len >= sizeof prev_tail)
		return -1;
	snprintf(path, sizeof path, "/proc/%d/environ", (int)pid);
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	while ((n = read(fd, buf, sizeof buf)) > 0) {
		char window[sizeof prev_tail + sizeof buf];
		size_t window_len = tail_len + (size_t)n;
		bool window_starts_entry = total == 0 || tail_starts_entry;

		if (total + (size_t)n > 65536) {
			close(fd);
			return -1;
		}
		memcpy(window, prev_tail, tail_len);
		memcpy(window + tail_len, buf, (size_t)n);
		for (size_t i = 0; i + needle_len <= window_len; i++) {
			bool starts_entry = i == 0 ? window_starts_entry :
						      window[i - 1] == '\0';
			if (starts_entry &&
			    memcmp(window + i, needle, needle_len) == 0 &&
			    (prefix || i + needle_len == window_len ||
			     window[i + needle_len] == '\0')) {
				close(fd);
				return 1;
			}
		}
		tail_len = window_len < needle_len ?
			window_len : needle_len - 1;
		if (tail_len > 0) {
			size_t start = window_len - tail_len;
			tail_starts_entry = start == 0 ? window_starts_entry :
						   window[start - 1] == '\0';
		} else {
			tail_starts_entry = false;
		}
		memcpy(prev_tail, window + window_len - tail_len, tail_len);
		total += (size_t)n;
	}
	if (n < 0) {
		close(fd);
		return -1;
	}
	close(fd);
	return 0;
}

static bool
qdwin_proc_exe_is_root_trusted(pid_t pid)
{
	char path[64];
	struct stat st;

	if (pid <= 0)
		return false;
	snprintf(path, sizeof path, "/proc/%d/exe", (int)pid);
	if (stat(path, &st) < 0)
		return false;
	return st.st_uid == 0 && (st.st_mode & (S_IWGRP | S_IWOTH)) == 0;
}

static bool
qdwin_proc_status_uid_ppid(pid_t pid, uid_t *uid_out, pid_t *ppid_out)
{
	char path[64];
	char line[256];
	FILE *f;
	bool saw_uid = false, saw_ppid = false;

	if (pid <= 0)
		return false;
	snprintf(path, sizeof path, "/proc/%d/status", (int)pid);
	f = fopen(path, "re");
	if (!f)
		return false;
	while (fgets(line, sizeof line, f)) {
		unsigned int uid;
		int ppid;

		if (sscanf(line, "Uid:\t%u", &uid) == 1) {
			*uid_out = (uid_t)uid;
			saw_uid = true;
		} else if (sscanf(line, "PPid:\t%d", &ppid) == 1) {
			*ppid_out = (pid_t)ppid;
			saw_ppid = true;
		}
	}
	fclose(f);
	return saw_uid && saw_ppid;
}

static bool
qdwin_secctx_helper_has_root_launcher_parent(pid_t pid)
{
	uid_t client_uid = (uid_t)-1;
	uid_t parent_uid = (uid_t)-1;
	pid_t parent_pid = 0;
	pid_t parent_ppid = 0;
	uint64_t st_before, st_after;
	char *parent_exe, *base;
	bool ok = false;

	if (!qdwin_proc_status_uid_ppid(pid, &client_uid, &parent_pid) ||
	    parent_pid <= 1)
		return false;
	if (!qdwin_proc_status_uid_ppid(parent_pid, &parent_uid, &parent_ppid) ||
	    parent_uid != 0)
		return false;
	(void)parent_ppid;
	st_before = qdwin_proc_starttime(parent_pid);
	parent_exe = qdwin_proc_exe(parent_pid);
	if (!parent_exe)
		return false;
	base = strrchr(parent_exe, '/');
	base = base ? base + 1 : parent_exe;
	st_after = qdwin_proc_starttime(parent_pid);
	ok = qdwin_secctx_root_launcher_attested(parent_uid, st_before, st_after,
						 base);
	/* The launcher parent here is root (parent_uid==0, verified above),
	 * while this compositor runs unprivileged (admin uid, no
	 * CAP_SYS_PTRACE). The kernel therefore denies readlink(/proc/
	 * <parent_pid>/exe) for a more-privileged target, so qdwin_proc_exe()
	 * returns "" and the basename allowlist can never match the
	 * production tier-4 path (`runuser -u admin -- env ...
	 * qdistro-secctx-exec`). The load-bearing trust property — the helper
	 * has a *root* direct parent, unforgeable by an unprivileged attacker
	 * — is already established from /proc/<pid>/status (readable). Accept
	 * on the verified root parent + stable starttime when the launcher
	 * basename is structurally unreadable, rather than fail closed. */
	if (ok && (!*base)) {
		weston_log("qdwin/secctx: helper pid=%d launcher parent "
			   "pid=%d exe unreadable; falling back to verified "
			   "root-parent + stable-starttime attestation\n",
			   (int)pid, (int)parent_pid);
	}
	free(parent_exe);
	return ok;
}

static bool
qdwin_secctx_client_is_authorized(struct qdwin *qdwin,
				  struct wl_client *client,
				  pid_t pid, uid_t uid)
{
	uint64_t st_before, st_after;
	char *exe;
	bool ok;

	if (qdwin_client_is_bound_shell(qdwin, client))
		return true;
	if (qdwin_secctx_open_dev_mode())
		return true;
	if (uid != qdwin->allowed_uid && uid != 0)
		return false;

	st_before = qdwin_proc_starttime(pid);
	exe = qdwin_proc_exe(pid);
	ok = qdwin_secctx_exe_allowed(exe);
	free(exe);
	if (ok && !qdwin_proc_exe_is_root_trusted(pid)) {
		weston_log("qdwin/secctx: helper pid=%d executable is not "
			   "root-owned and non-writable by group/other; "
			   "refusing\n", (int)pid);
		ok = false;
	}
	if (ok && uid == qdwin->allowed_uid &&
	    !qdwin_secctx_helper_has_root_launcher_parent(pid)) {
		weston_log("qdwin/secctx: helper pid=%d lacks direct root "
			   "launcher parent; refusing\n", (int)pid);
		ok = false;
	}
	if (ok && uid != 0) {
		int env_has = qdwin_proc_environ_has(
			pid, "QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED=");
		if (env_has != 0) {
			weston_log("qdwin/secctx: helper pid=%d %s; refusing "
				   "because QDWIN_SECCTX_OPEN is not set\n",
				   (int)pid,
				   env_has > 0 ?
				   "carries dev-only "
				   "QDISTRO_SECCTX_EXEC_ALLOW_UNTRUSTED" :
				   "environment could not be fully checked");
			ok = false;
		}
	}
	st_after = qdwin_proc_starttime(pid);
	return ok && st_before != 0 && st_after != 0 && st_before == st_after;
}

/* Map an advertised wl_global to its policy kind (02/S1). Pointer identity
 * against the privileged globals we own a handle to; the screen-capture global
 * is created by libweston core and exposed via
 * compositor->output_capture.weston_capture_v1. Anything else is ORDINARY. */
static enum qdwin_global_kind
qdwin_classify_global(struct qdwin *qdwin, const struct wl_global *global)
{
	if (qdwin->input_method_manager_global &&
	    global == qdwin->input_method_manager_global)
		return QDWIN_GLOBAL_INPUT_METHOD;
	if (qdwin->virtual_keyboard_manager_global &&
	    global == qdwin->virtual_keyboard_manager_global)
		return QDWIN_GLOBAL_VIRTUAL_KEYBOARD;
	if (qdwin->compositor &&
	    qdwin->compositor->output_capture.weston_capture_v1 &&
	    global == qdwin->compositor->output_capture.weston_capture_v1)
		return QDWIN_GLOBAL_WESTON_CAPTURE;
	if (qdwin->compositor &&
	    qdwin->compositor->touch_calibration &&
	    global == qdwin->compositor->touch_calibration)
		return QDWIN_GLOBAL_TOUCH_CALIBRATION;
	if (qdwin->security_context_manager_global &&
	    global == qdwin->security_context_manager_global)
		return QDWIN_GLOBAL_SECCTX_MANAGER;
	if (qdwin->idle_notifier_global &&
	    global == qdwin->idle_notifier_global)
		return QDWIN_GLOBAL_IDLE_NOTIFIER;
	if (qdwin->shell_global &&
	    global == qdwin->shell_global)
		return QDWIN_GLOBAL_SHELL;
	if (qdwin->layer_shell_global &&
	    global == qdwin->layer_shell_global)
		return QDWIN_GLOBAL_LAYER_SHELL;
	if (qdwin->locker_global &&
	    global == qdwin->locker_global)
		return QDWIN_GLOBAL_LOCKER;
	if (qdwin->nested_manager_global &&
	    global == qdwin->nested_manager_global)
		return QDWIN_GLOBAL_NESTED_MANAGER;
	return QDWIN_GLOBAL_ORDINARY;
}

/* Hide privileged globals (keystroke capture/injection, screen-pixel theft,
 * secctx minting, and idle presence/activity) from the credential classes the
 * policy matrix denies, and block sandboxed clients from the secctx manager
 * (per the protocol's nesting prohibition). Installed as a wl_global_filter;
 * returns true to allow this client to see/bind the global. The per-class
 * visibility decision lives in the pure, unit-tested qdwin_global_visible()
 * (qdwin-logic.c, 02/S1); the bind handlers apply the further runtime
 * identity pins on top. */
static bool
qdwin_secctx_global_filter(const struct wl_client *client,
			   const struct wl_global *global, void *data)
{
	struct qdwin *qdwin = data;
	struct wl_client *cw = (struct wl_client *)client;
	enum qdwin_global_kind kind = qdwin_classify_global(qdwin, global);
	if (kind == QDWIN_GLOBAL_ORDINARY)
		return true;  /* fast path: not a gated global */

	/* Credential class. SECCTX (a silo client) is the cheap common case.
	 * The SHELL/ORDINARY split only matters for the shell-only globals
	 * (secctx manager + weston_capture) and needs the runtime identity check
	 * (bound shell or authorized secctx-exec helper) — resolve it ONLY for
	 * those, so we don't read pid/exe for every IME/VK filter call (whose row
	 * folds SHELL and ORDINARY together anyway). */
	enum qdwin_cred_class cred;
	struct qdwin_secctx_client *sc = qdwin_secctx_client_find(qdwin, cw);
	if (sc != NULL) {
		/* Generic silo clients stay on the matrix's fail-closed SECCTX row.
		 * The sole exception is the launcher-minted tier-2 inner Weston
		 * publisher; the bind handler repeats this exact identity gate. */
		cred = kind == QDWIN_GLOBAL_NESTED_MANAGER &&
		       qdwin_nested_secctx_publisher_allowed(
			       qdwin_secctx_client_engine(sc),
			       qdwin_secctx_client_peer_exe(sc))
			? QDWIN_CRED_ORDINARY : QDWIN_CRED_SECCTX;
	} else if (kind == QDWIN_GLOBAL_SECCTX_MANAGER ||
		   kind == QDWIN_GLOBAL_WESTON_CAPTURE) {
		pid_t pid; uid_t uid; gid_t gid;
		wl_client_get_credentials(cw, &pid, &uid, &gid);
		cred = qdwin_secctx_client_is_authorized(qdwin, cw, pid, uid)
			? QDWIN_CRED_SHELL : QDWIN_CRED_ORDINARY;
	} else {
		cred = QDWIN_CRED_ORDINARY;
	}
	return qdwin_global_visible(cred, kind);
}

static void
bind_qdwin_secctx_manager(struct wl_client *client, void *data,
			  uint32_t version, uint32_t id)
{
	struct qdwin *qdwin = data;
	pid_t pid; uid_t uid; gid_t gid;
	wl_client_get_credentials(client, &pid, &uid, &gid);

	/* Option A/B transition (secctx-identity-contract.md): the manager is
	 * no longer authorized by same uid. Production admits only the bound
	 * shell client and the installed qdistro-secctx-exec helper path used
	 * by root/broker launchers. The helper's strings are not sufficient
	 * policy identity by themselves; qdistro must resolve the resulting
	 * pid/starttime against launch records before trusting sandbox_engine /
	 * app_id / instance_id. QDWIN_SECCTX_OPEN=1 is the explicit dev/test
	 * escape hatch. */
	if (!qdwin_secctx_client_is_authorized(qdwin, client, pid, uid)) {
		weston_log("qdwin/secctx: REJECTED manager "
			   "bind from uid=%u pid=%d "
			   "(not shell or authorized helper)\n",
			   (unsigned)uid, (int)pid);
		wl_client_post_implementation_error(
			client,
			"wp_security_context_manager_v1: "
			"only the bound shell or authorized secctx helper "
			"may bind this global");
		return;
	}

	struct wl_resource *r = wl_resource_create(client,
		&wp_security_context_manager_v1_interface, version, id);
	if (!r) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(r, &qdwin_secctx_manager_impl,
				       qdwin, NULL);
	weston_log("qdwin/secctx: manager bound by uid=%u pid=%d%s\n",
		   (unsigned)uid, (int)pid,
		   qdwin_client_is_bound_shell(qdwin, client) ? " (shell)" :
		   qdwin_secctx_open_dev_mode() ? " (dev-open)" : " (helper)");
}

WL_EXPORT int
wet_shell_init(struct weston_compositor *ec, int *argc, char *argv[])
{
	struct qdwin *qdwin;

	qdwin = calloc(1, sizeof *qdwin);
	if (!qdwin)
		return -1;

	qdwin->compositor = ec;

	/* Parse the optional layer-shell bind allowlist (security review
	 * Finding #4) FIRST, *before* publishing qdwin_singleton or
	 * installing the default pointer grab. These parses can fail/abort
	 * (malformed uid, or a configured exe/label whose strdup() OOM'd),
	 * and on such failure we free(qdwin) and return. Doing this before
	 * the singleton/default-grab install guarantees no global hook can
	 * reference the freed qdwin if shell-init failure is ever treated as
	 * nonfatal by the caller. All three unset => the historical
	 * broad/test posture (shell-client or allowed_uid) is unchanged. If a
	 * string value was supplied but strdup() failed (was_set && NULL) we
	 * must not silently weaken the policy — abort init. */
	{
		bool exe_set = false, label_set = false;
		bool uid_ok = qdwin_parse_uid_opt(*argc, argv,
				    "--qdwin-allowed-layershell-uid=",
				    "QDWIN_ALLOWED_LAYERSHELL_UID",
				    &qdwin->allowed_layershell_uid,
				    &qdwin->allowed_layershell_uid_set);
		qdwin->allowed_layershell_exe =
			qdwin_parse_str_opt(*argc, argv,
					    "--qdwin-allowed-layershell-exe=",
					    "QDWIN_ALLOWED_LAYERSHELL_EXE",
					    &exe_set);
		qdwin->allowed_layershell_label =
			qdwin_parse_str_opt(*argc, argv,
					    "--qdwin-allowed-layershell-label=",
					    "QDWIN_ALLOWED_LAYERSHELL_LABEL",
					    &label_set);
		/* Fail closed: a present-but-malformed uid, or a configured
		 * exe/label whose strdup() OOM'd, must abort rather than leave a
		 * weaker policy than the admin requested. We have NOT yet
		 * published qdwin_singleton or installed the default pointer
		 * grab, so these returns leave no dangling global. */
		if (!uid_ok) {
			weston_log("qdwin: --qdwin-allowed-layershell-uid / "
				   "QDWIN_ALLOWED_LAYERSHELL_UID is malformed — "
				   "refusing to start\n");
			free(qdwin->allowed_layershell_exe);
			free(qdwin->allowed_layershell_label);
			free(qdwin);
			return -1;
		}
		if ((exe_set && !qdwin->allowed_layershell_exe) ||
		    (label_set && !qdwin->allowed_layershell_label)) {
			weston_log("qdwin: failed to allocate layer-shell bind "
				   "policy — refusing to start with a weaker "
				   "policy than configured\n");
			free(qdwin->allowed_layershell_exe);
			free(qdwin->allowed_layershell_label);
			free(qdwin);
			return -1;
		}
	}

	qdwin->allowed_uid = qdwin_parse_allowed_uid(*argc, argv);
	/* Default the locker uid to the shell uid (single-admin) until
	 * an explicit `--qdwin-allowed-locker-uid=N` is wired. Using
	 * (uid_t)-1 as the sentinel rather than 0 so a root-owned
	 * locker is a valid configuration. */
	qdwin->allowed_locker_uid = (uid_t)-1;
	/* Optional defence-in-depth peer checks for the locker bind: an
	 * expected resolved exe path and/or SELinux label. Unset => NULL =>
	 * that check is skipped (uid-only). Configured via argv or env.
	 * If a value was supplied but strdup() failed (was_set && NULL), we
	 * must not silently disable the check — abort init rather than fall
	 * back to a weaker policy than the admin requested. */
	{
		bool exe_set = false, label_set = false, entry_set = false;
		qdwin->allowed_locker_exe =
			qdwin_parse_str_opt(*argc, argv,
					    "--qdwin-allowed-locker-exe=",
					    "QDWIN_ALLOWED_LOCKER_EXE", &exe_set);
		qdwin->allowed_locker_label =
			qdwin_parse_str_opt(*argc, argv,
					    "--qdwin-allowed-locker-label=",
					    "QDWIN_ALLOWED_LOCKER_LABEL",
					    &label_set);
		qdwin->allowed_locker_entrypoint =
			qdwin_parse_str_opt(*argc, argv,
					    "--qdwin-allowed-locker-entrypoint=",
					    "QDWIN_ALLOWED_LOCKER_ENTRYPOINT",
					    &entry_set);
		if ((exe_set && !qdwin->allowed_locker_exe) ||
		    (label_set && !qdwin->allowed_locker_label) ||
		    (entry_set && !qdwin->allowed_locker_entrypoint)) {
			weston_log("qdwin: failed to allocate locker bind "
				   "policy — refusing to start with a weaker "
				   "policy than configured\n");
			free(qdwin->allowed_locker_exe);
			free(qdwin->allowed_locker_label);
			free(qdwin->allowed_locker_entrypoint);
			free(qdwin);
			return -1;
		}

		/* CRITICAL hardening: a uid-only locker policy is exploitable —
		 * any same-uid client (the shell and many desktop helpers run as
		 * the admin uid) could bind qdwin_locker_v1 and drive
		 * set_locked(0). Fail closed: when NO explicit identity policy
		 * (exe, label, OR entrypoint) is configured, default to the
		 * ENTRYPOINT policy so the peer's identity is verified.
		 *
		 * Why entrypoint, not exe: qdlocker is a Python setuptools
		 * console-script (qdlocker/pyproject.toml [project.scripts];
		 * systemd ExecStart=/usr/local/bin/qdlocker). Its /proc/<pid>/exe
		 * therefore resolves to the *interpreter* (/usr/bin/python3.N),
		 * never to the launcher path. The previous hardening defaulted
		 * allowed_locker_exe=/usr/bin/qdlocker and compared it against
		 * /proc/<pid>/exe — which can NEVER match for a script launcher,
		 * so the genuine locker was rejected at bind and the session
		 * could not lock at all. The entrypoint policy matches how the
		 * locker is actually packaged: exe is a system interpreter AND
		 * argv[1] resolves to the canonical, root-owned entrypoint file.
		 *
		 * An admin who genuinely wants uid-only (dev/test) must opt out
		 * CONSCIOUSLY via --qdwin-allowed-locker-any /
		 * QDWIN_ALLOWED_LOCKER_ANY=1, which we log loudly. A native
		 * locker (or a dedicated locker uid) can set --qdwin-allowed-
		 * locker-exe / -label explicitly to override. */
		qdwin->allowed_locker_any =
			qdwin_parse_flag(*argc, argv,
					 "--qdwin-allowed-locker-any",
					 "QDWIN_ALLOWED_LOCKER_ANY");
		if (!exe_set && !label_set && !entry_set) {
			if (qdwin->allowed_locker_any) {
				weston_log("qdwin: WARNING locker bind policy is "
					   "UID-ONLY (--qdwin-allowed-locker-any) — "
					   "any same-uid client can bind the locker "
					   "and unlock the session. Do NOT use this in "
					   "production.\n");
			} else {
				qdwin->allowed_locker_entrypoint =
					strdup(QDWIN_DEFAULT_LOCKER_ENTRYPOINT);
				if (!qdwin->allowed_locker_entrypoint) {
					weston_log("qdwin: failed to allocate "
						   "default locker entrypoint "
						   "policy — refusing to start "
						   "uid-only\n");
					free(qdwin->allowed_locker_exe);
					free(qdwin->allowed_locker_label);
					free(qdwin);
					return -1;
				}
				weston_log("qdwin: locker bind policy defaulting to "
					   "entrypoint=%s (system interpreter + "
					   "root-owned entrypoint; no explicit policy "
					   "configured); set --qdwin-allowed-locker-any "
					   "to override to uid-only\n",
					   qdwin->allowed_locker_entrypoint);
			}
		}
	}
	/* P1: input-method-v2 bind policy. Default uid sentinel (-1) => the IME
	 * is admitted at allowed_uid (the session uid); set
	 * --qdwin-allowed-ime-uid=N for a dedicated IME uid. The real silo
	 * boundary is the secctx deny in the global filter + bind handler
	 * (sandboxed clients can never act as the IME regardless of uid); the
	 * optional exe/label pins below are defence-in-depth, mirroring the
	 * locker. Fail closed on a present-but-unallocatable string policy. */
	qdwin->allowed_ime_uid = (uid_t)-1;
	{
		bool uid_set = false, exe_set = false, label_set = false;
		if (!qdwin_parse_uid_opt(*argc, argv,
					 "--qdwin-allowed-ime-uid=",
					 "QDWIN_ALLOWED_IME_UID",
					 &qdwin->allowed_ime_uid, &uid_set)) {
			weston_log("qdwin: --qdwin-allowed-ime-uid / "
				   "QDWIN_ALLOWED_IME_UID is malformed — "
				   "refusing to start\n");
			free(qdwin->allowed_locker_entrypoint);
			free(qdwin->allowed_locker_exe);
			free(qdwin->allowed_locker_label);
			free(qdwin->allowed_layershell_exe);
			free(qdwin->allowed_layershell_label);
			free(qdwin);
			return -1;
		}
		qdwin->allowed_ime_exe =
			qdwin_parse_str_opt(*argc, argv,
					    "--qdwin-allowed-ime-exe=",
					    "QDWIN_ALLOWED_IME_EXE", &exe_set);
		qdwin->allowed_ime_label =
			qdwin_parse_str_opt(*argc, argv,
					    "--qdwin-allowed-ime-label=",
					    "QDWIN_ALLOWED_IME_LABEL", &label_set);
		if ((exe_set && !qdwin->allowed_ime_exe) ||
		    (label_set && !qdwin->allowed_ime_label)) {
			weston_log("qdwin: failed to allocate input-method bind "
				   "policy — refusing to start with a weaker "
				   "policy than configured\n");
			free(qdwin->allowed_ime_exe);
			free(qdwin->allowed_ime_label);
			free(qdwin->allowed_locker_entrypoint);
			free(qdwin->allowed_locker_exe);
			free(qdwin->allowed_locker_label);
			free(qdwin->allowed_layershell_exe);
			free(qdwin->allowed_layershell_label);
			free(qdwin);
			return -1;
		}
	}
	/* §6.8 S3b: install our default-pointer-grab so nested-proxy
	 * input forwarding lives even when no other grab is active.
	 * Singleton hookup so the grab callbacks (which receive only a
	 * weston_pointer_grab*) can find this qdwin instance. Installed
	 * only after the fallible locker-policy parse above, so an init
	 * failure there never leaves these globals referencing freed qdwin. */
	qdwin_singleton = qdwin;
	weston_compositor_set_default_pointer_grab(
		ec, &qdwin_proxy_default_pointer_grab_iface);
	wl_list_init(&qdwin->hotkeys);
	qdwin_wm_policy_set_defaults(&qdwin->wm_policy);
	/* v28: only the DRM/libinput backend's seats embed a real udev_seat
	 * with a devices_list we can walk for set_pointer_config. */
	qdwin->libinput_backend =
		(ec->primary_backend &&
		 weston_get_backend_type(ec->primary_backend) ==
			 WESTON_BACKEND_DRM) ? 1 : 0;
	wl_list_init(&qdwin->toplevels);
	wl_list_init(&qdwin->view_streams);
	wl_list_init(&qdwin->seat_trackers);
	wl_list_init(&qdwin->activation_tokens);
	wl_list_init(&qdwin->activation_pending);
	wl_list_init(&qdwin->data_source_wraps);
	wl_list_init(&qdwin->data_offer_pending);
	wl_list_init(&qdwin->idle_notifications);
	wl_list_init(&qdwin->idle_inhibitors);
	wl_list_init(&qdwin->fractional_scales);
	wl_list_init(&qdwin->text_inputs);
	wl_list_init(&qdwin->text_input_managers);
	wl_list_init(&qdwin->input_methods);
	wl_list_init(&qdwin->input_method_managers);
	wl_list_init(&qdwin->virtual_keyboards);
	wl_list_init(&qdwin->virtual_keyboard_managers);
	wl_list_init(&qdwin->primary_seats);
	wl_list_init(&qdwin->nested_toplevels);
	qdwin->next_stream_port = 3401;  /* pool start for per-stream ports */

	/* v24 workspaces. Default count is overridable via env for tests /
	 * headless probes; the shell reconciles it to qdshell settings at
	 * session start (create_workspace / workspace.remove). */
	wl_list_init(&qdwin->ext_ws_managers);
	wl_list_init(&qdwin->om_managers);
	qdwin->om_serial = 1;  /* configs created against serial 0 are stale */
	qdwin->workspace_count = QDWIN_DEFAULT_WORKSPACES;
	qdwin->active_workspace = 0;
	{
		const char *env = getenv("QDWIN_WORKSPACE_COUNT");
		if (env && *env) {
			long n = strtol(env, NULL, 10);
			if (n >= 1 && n <= QDWIN_MAX_WORKSPACES)
				qdwin->workspace_count = (uint32_t)n;
		}
	}

	/* Layers: background (BACKGROUND) < held (HIDDEN) < normal (NORMAL).
	 * minimized_layer is intentionally *not* passed through
	 * weston_layer_set_position — that leaves it detached from the
	 * compositor's layer_list so layer_is_visible() returns false,
	 * and weston_view_move_to_layer() unmaps views moving into it
	 * (matching desktop-shell's minimised pattern). Setting position
	 * HIDDEN on the held layer is harmless because views placed
	 * there carry no buffer yet; they become visible only after the
	 * move to normal_layer in release_holding. */
	weston_layer_init(&qdwin->background_layer, ec);
	weston_layer_init(&qdwin->held_layer, ec);
	weston_layer_init(&qdwin->normal_layer, ec);
	weston_layer_init(&qdwin->minimized_layer, ec);
	/* v24: off-workspace views park here. Like minimized_layer, it is
	 * intentionally never weston_layer_set_position'd, so it stays
	 * detached from the layer_list (invisible) and weston_view_move_to_layer
	 * unmaps views moving into it. */
	weston_layer_init(&qdwin->workspace_hidden_layer, ec);
	weston_layer_init(&qdwin->panel_layer, ec);
	weston_layer_init(&qdwin->notification_layer, ec);
	weston_layer_init(&qdwin->launcher_layer, ec);
	weston_layer_init(&qdwin->lock_layer, ec);
	weston_layer_init(&qdwin->popup_layer, ec);
	for (int i = 0; i < 4; i++)
		weston_layer_init(&qdwin->layer_shell_layer[i], ec);
	weston_layer_set_position(&qdwin->background_layer,
				  WESTON_LAYER_POSITION_BACKGROUND);
	weston_layer_set_position(&qdwin->held_layer,
				  WESTON_LAYER_POSITION_HIDDEN);
	weston_layer_set_position(&qdwin->normal_layer,
				  WESTON_LAYER_POSITION_NORMAL);
	/* Panel sits between NORMAL (0x50000000) and UI/popup (0x80000000).
	 * 0x70000000 keeps popups on top so right-click chrome menus still
	 * overlay the panel. */
	weston_layer_set_position(&qdwin->panel_layer, QDWIN_LAYER_POS_PANEL);
	/* Notifications live above panel but below popups (popup_layer is
	 * WESTON_LAYER_POSITION_UI = 0x80000000). 0x78000000 fits between.
	 * Launcher between notifications and popup (0x7C000000). */
	weston_layer_set_position(&qdwin->notification_layer, QDWIN_LAYER_POS_NOTIFICATION);
	weston_layer_set_position(&qdwin->launcher_layer,    QDWIN_LAYER_POS_LAUNCHER);
	weston_layer_set_position(&qdwin->popup_layer,
				  WESTON_LAYER_POSITION_UI);
	/* Lock layer lives at WESTON_LAYER_POSITION_LOCK (above
	 * everything except cursor + fade). Set after popup so the
	 * position order is clear in the source. */
	weston_layer_set_position(&qdwin->lock_layer,
				  WESTON_LAYER_POSITION_LOCK);
	/* zwlr_layer_shell_v1 layer ladder. Position values picked to
	 * cohabit with qdwin's native panel layers:
	 *   BACKGROUND (0): just above qdwin->background_layer (0x02).
	 *   BOTTOM     (1): WESTON_LAYER_POSITION_BOTTOM_UI (0x30000000).
	 *   TOP        (2): 0x90000000 — above qdwin->popup_layer (UI =
	 *                   0x80000000), below FULLSCREEN (0xb0000000)
	 *                   so a fullscreen toplevel still covers TOP-
	 *                   layer panels per spec.
	 *   OVERLAY    (3): WESTON_LAYER_POSITION_TOP_UI (0xe0000000) —
	 *                   above FULLSCREEN per spec ("OVERLAY layer is
	 *                   above fullscreen surfaces").
	 */
	weston_layer_set_position(&qdwin->layer_shell_layer[0], QDWIN_LAYER_POS_LSHELL_BG);
	weston_layer_set_position(&qdwin->layer_shell_layer[1],
				  WESTON_LAYER_POSITION_BOTTOM_UI);
	weston_layer_set_position(&qdwin->layer_shell_layer[2], QDWIN_LAYER_POS_LSHELL_TOP);
	weston_layer_set_position(&qdwin->layer_shell_layer[3],
				  WESTON_LAYER_POSITION_TOP_UI);
	wl_list_init(&qdwin->panels);
	wl_list_init(&qdwin->notifications);
	wl_list_init(&qdwin->launchers);

	qdwin->desktop = weston_desktop_create(ec, &qdwin_desktop_api, qdwin);
	if (!qdwin->desktop) {
		weston_log("qdwin: weston_desktop_create failed\n");
		goto fail;
	}

	/* Advertise the full interface version so the shell can bind every
	 * request/event through v34 (remote identity/input/drain). */
	qdwin->shell_global = wl_global_create(ec->wl_display,
					       &qdwin_shell_v1_interface,
					       34, qdwin, bind_qdwin_shell);
	if (!qdwin->shell_global) {
		weston_log("qdwin: wl_global_create failed\n");
		goto fail;
	}

	/* v24: ext-workspace-v1 manager global. Unlike qdwin_shell_v1 this
	 * is a public taskbar protocol — advertised to all clients, no uid
	 * gate. See todo/decisions/qdwin-workspaces-ext-protocol.md. */
	qdwin->ext_ws_global = wl_global_create(ec->wl_display,
						&ext_workspace_manager_v1_interface,
						1, qdwin, bind_ext_workspace_manager);
	if (!qdwin->ext_ws_global)
		weston_log("qdwin: ext_workspace_manager_v1 global create failed "
			   "(workspaces unavailable)\n");

	/* Output (display) management: wlr-output-management-unstable-v1
	 * manager global. Public output-config protocol (no uid gate); the
	 * qdshell Display layout tab is the primary consumer. See
	 * todo/decisions/qdwin-output-management.md. */
	qdwin->output_mgmt_global = wl_global_create(ec->wl_display,
						     &zwlr_output_manager_v1_interface,
						     QDWIN_OM_VERSION, qdwin,
						     bind_output_manager);
	if (!qdwin->output_mgmt_global)
		weston_log("qdwin: zwlr_output_manager_v1 global create failed "
			   "(output management unavailable)\n");

	/* qdwin_locker_v1 — peer locker global. Defaults to the same uid
	 * as the shell unless overridden (single-admin assumption per
	 * sessions.md:4-14). The sentinel is (uid_t)-1 not 0 so a
	 * root-owned locker is a legitimate explicit configuration. See
	 * doc/locker.md.
	 *
	 * §P10: compiled out in role=guest builds. The in-VM compositor
	 * is locked at the host level (the outer host qdwin owns the
	 * locker; the guest is the locked thing, not a locker target).
	 * Spec: plan2/research/spice-retirement/00-overview.md §"What it
	 * drops vs the host qdwin". */
#ifndef QDWIN_ROLE_GUEST
	if (qdwin->allowed_locker_uid == (uid_t)-1)
		qdwin->allowed_locker_uid = qdwin->allowed_uid;
	qdwin->locker_global = wl_global_create(ec->wl_display,
						&qdwin_locker_v1_interface,
						1, qdwin, bind_qdwin_locker);
	if (!qdwin->locker_global) {
		weston_log("qdwin: locker wl_global_create failed\n");
		goto fail;
	}
	weston_log("qdwin: locker bind policy uid=%u entrypoint=%s exe=%s "
		   "label=%s%s\n",
		   (unsigned)qdwin->allowed_locker_uid,
		   qdwin->allowed_locker_entrypoint ?
			   qdwin->allowed_locker_entrypoint : "(any)",
		   qdwin->allowed_locker_exe ? qdwin->allowed_locker_exe : "(any)",
		   qdwin->allowed_locker_label ? qdwin->allowed_locker_label : "(any)",
		   (!qdwin->allowed_locker_entrypoint && !qdwin->allowed_locker_exe &&
		    !qdwin->allowed_locker_label)
			   ? " [UID-ONLY: INSECURE]" : "");
#else
	/* role=guest: explicitly leave qdwin->locker_global NULL so any
	 * code that later wants to broadcast through it short-circuits. */
	qdwin->locker_global = NULL;
#endif

	/* §6.5 S5: input-injection global. Visible to any client; the
	 * access_token in claim() is the gate. qdistro-forward gets the
	 * token via spawn argv from qdwin. */
	qdwin->stream_input_global = wl_global_create(
		ec->wl_display, &qdwin_stream_input_v1_interface,
		2, qdwin, bind_qdwin_stream_input);
	if (!qdwin->stream_input_global) {
		weston_log("qdwin: stream_input wl_global_create failed\n");
		goto fail;
	}

	/* §6.7: xdg-activation-v1 public global. Any client may bind. */
	qdwin->xdg_activation_global = wl_global_create(
		ec->wl_display, &xdg_activation_v1_interface,
		1, qdwin, bind_xdg_activation);
	if (!qdwin->xdg_activation_global) {
		weston_log("qdwin: xdg-activation wl_global_create failed\n");
		goto fail;
	}

	/* §6.7: ext-idle-notify-v1 + idle-inhibit-unstable-v1.
	 * ext-idle-notify is hidden from secctx/silo clients by the global
	 * filter because whole-session idle/resume is a cross-silo
	 * presence/activity signal. idle-inhibit remains public.
	 * idle_signal/wake_signal drive notification fan-out. */
	qdwin->idle_notifier_global = wl_global_create(
		ec->wl_display, &ext_idle_notifier_v1_interface,
		2, qdwin, bind_qdwin_idle_notifier);
	if (!qdwin->idle_notifier_global) {
		weston_log("qdwin: ext-idle-notify wl_global_create failed\n");
		goto fail;
	}
	qdwin->idle_inhibit_manager_global = wl_global_create(
		ec->wl_display, &zwp_idle_inhibit_manager_v1_interface,
		1, qdwin, bind_qdwin_idle_inhibit_manager);
	if (!qdwin->idle_inhibit_manager_global) {
		weston_log("qdwin: idle-inhibit wl_global_create failed\n");
		goto fail;
	}
	/* §6.6 S3/S4 keybindings: Ctrl+Space → launcher_requested;
	 * Alt+Tab → switcher_next(+1); Alt+Shift+Tab → switcher_next(-1).
	 * The compositor owns the key grab — events fan out to the
	 * shell via qdwin_shell_v1, keeping app clients unaffected.
	 * Alt release → switcher_commit via a modifier binding. */
	weston_compositor_add_key_binding(ec, KEY_SPACE, MODIFIER_CTRL,
					  qdwin_on_launcher_key, qdwin);
	weston_compositor_add_key_binding(ec, KEY_TAB, MODIFIER_ALT,
					  qdwin_on_switcher_key, qdwin);
	weston_compositor_add_key_binding(
		ec, KEY_TAB, (enum weston_keyboard_modifier)
		(MODIFIER_ALT | MODIFIER_SHIFT),
		qdwin_on_switcher_back_key, qdwin);
	weston_compositor_add_modifier_binding(ec, MODIFIER_ALT,
					       qdwin_on_alt_released, qdwin);
	/* §6.6 S5 full: Ctrl+Alt+L → lock_requested event.
	 * Shell decides whether to enter lock; compositor only relays. */
	weston_compositor_add_key_binding(
		ec, KEY_L, (enum weston_keyboard_modifier)
		(MODIFIER_CTRL | MODIFIER_ALT),
		qdwin_on_lock_key, qdwin);

	qdwin->idle_signal_listener.notify = qdwin_on_idle_signal;
	wl_signal_add(&ec->idle_signal, &qdwin->idle_signal_listener);
	qdwin->wake_signal_listener.notify = qdwin_on_wake_signal;
	wl_signal_add(&ec->wake_signal, &qdwin->wake_signal_listener);
	/* §6.7(a) follow-up: enable internal idle tracker if weston's own
	 * idle timer is disabled. Read once at init — ec->idle_time doesn't
	 * change post-init in weston-16. */
	qdwin->idle_internal_mode = (ec->idle_time == 0);
	weston_log("qdwin: ext-idle-notify idle_time=%d internal_mode=%d\n",
		   ec->idle_time, qdwin->idle_internal_mode);

	qdwin->cursor_shape_manager_global = wl_global_create(
		ec->wl_display, &wp_cursor_shape_manager_v1_interface,
		2, qdwin, bind_qdwin_cursor_shape_manager);
	if (!qdwin->cursor_shape_manager_global) {
		weston_log("qdwin: cursor-shape wl_global_create failed\n");
		goto fail;
	}
	qdwin_cursor_theme_load(qdwin);
	qdwin->fractional_scale_manager_global = wl_global_create(
		ec->wl_display, &wp_fractional_scale_manager_v1_interface,
		1, qdwin, bind_qdwin_fractional_scale_manager);
	if (!qdwin->fractional_scale_manager_global) {
		weston_log("qdwin: fractional-scale wl_global_create failed\n");
		goto fail;
	}
	qdwin->primary_selection_manager_global = wl_global_create(
		ec->wl_display,
		&zwp_primary_selection_device_manager_v1_interface,
		1, qdwin, bind_qdwin_primary_manager);
	if (!qdwin->primary_selection_manager_global) {
		weston_log("qdwin: primary-selection wl_global_create failed\n");
		goto fail;
	}

	/* Bucket A / P1: text-input-v3 (open, app-facing). Advertise v1 —
	 * the lowest common denominator every text-input-v3 toolkit speaks;
	 * v2 adds input-panel/actions we don't drive without an IME. */
	qdwin->text_input_manager_global = wl_global_create(
		ec->wl_display, &zwp_text_input_manager_v3_interface,
		1, qdwin, bind_qdwin_text_input_manager);
	if (!qdwin->text_input_manager_global) {
		weston_log("qdwin: text-input wl_global_create failed\n");
		goto fail;
	}

	/* P1: input-method-unstable-v2 (privileged IME side). v1 — the only
	 * interface version. Hidden from sandboxed clients by the global filter
	 * and bind-gated to allowed_ime_uid; see the section comment far above. */
	qdwin->input_method_manager_global = wl_global_create(
		ec->wl_display, &zwp_input_method_manager_v2_interface,
		1, qdwin, bind_qdwin_input_method_manager);
	if (!qdwin->input_method_manager_global) {
		weston_log("qdwin: input-method wl_global_create failed\n");
		goto fail;
	}

	/* P1 companion: virtual-keyboard-unstable-v1 (privileged key injection).
	 * v1 — the only interface version. Hidden from sandboxed clients by the
	 * global filter and bind-gated to allowed_ime_uid via the SAME helper as
	 * input-method-v2; see the section comment far above. */
	qdwin->virtual_keyboard_manager_global = wl_global_create(
		ec->wl_display, &zwp_virtual_keyboard_manager_v1_interface,
		1, qdwin, bind_qdwin_virtual_keyboard_manager);
	if (!qdwin->virtual_keyboard_manager_global) {
		weston_log("qdwin: virtual-keyboard wl_global_create failed\n");
		goto fail;
	}

	/* §6.10: wp_security_context_v1. Hides itself from sandboxed
	 * clients via the global filter so nesting is impossible. */
	wl_list_init(&qdwin->secctxs);
	wl_list_init(&qdwin->secctx_clients);
	qdwin->security_context_manager_global = wl_global_create(
		ec->wl_display,
		&wp_security_context_manager_v1_interface,
		1, qdwin, bind_qdwin_secctx_manager);
	if (!qdwin->security_context_manager_global) {
		weston_log("qdwin: security-context wl_global_create failed\n");
		goto fail;
	}
	wl_display_set_global_filter(ec->wl_display,
				     qdwin_secctx_global_filter, qdwin);

	/* §6.8 S1: qdwin_nested_v1 manager global at v3 (remote identity).
	 * Peer-uid-filtered.
	 *
	 * §P10: compiled out in role=guest builds. The in-VM compositor
	 * is the *inner* end of the nested-compositor topology — it never
	 * advertises a further nesting target to its own clients. Outer
	 * (host) qdwin keeps the global so it can publish proxy toplevels
	 * for guest-side apps via qdwin-bystander --forward-all-toplevels.
	 * Spec: plan2/research/spice-retirement/00-overview.md §"What it
	 * drops vs the host qdwin". */
#ifndef QDWIN_ROLE_GUEST
	qdwin->nested_manager_global = wl_global_create(
		ec->wl_display, &qdwin_nested_manager_v1_interface,
		3, qdwin, bind_qdwin_nested_manager);
	if (!qdwin->nested_manager_global) {
		weston_log("qdwin: nested-manager wl_global_create failed\n");
		goto fail;
	}
#else
	qdwin->nested_manager_global = NULL;
#endif

	/* zwlr_layer_shell_v1 v5: external panels/notifications/lockscreens
	 * (waybar, Quickshell/noctalia, eww, fuzzel, mako, swaylock).
	 * Stub stage — accepts the protocol and completes configure/ack
	 * but does not lay out or render yet. See impl block above. */
	/* bind_qdwin_layer_shell gates to shell-client or allowed_uid. See
	 * doc/protocol.md "Security posture: layer-shell". findings F4: the
	 * global is now ALSO classified QDWIN_GLOBAL_LAYER_SHELL and hidden from
	 * secctx/silo clients by qdwin_secctx_global_filter (the prior
	 * "advertised publicly" Production TODO), so the bind gate is a redundant
	 * second layer rather than the sole defense. */
	wl_list_init(&qdwin->layer_surfaces);
	qdwin->layer_shell_global = wl_global_create(
		ec->wl_display, &zwlr_layer_shell_v1_interface,
		5, qdwin, bind_qdwin_layer_shell);
	if (!qdwin->layer_shell_global) {
		weston_log("qdwin: layer-shell wl_global_create failed\n");
		goto fail;
	}
	if (qdwin->allowed_layershell_uid_set ||
	    qdwin->allowed_layershell_exe ||
	    qdwin->allowed_layershell_label) {
		char uidbuf[16];
		if (qdwin->allowed_layershell_uid_set)
			snprintf(uidbuf, sizeof uidbuf, "%u",
				 (unsigned)qdwin->allowed_layershell_uid);
		weston_log("qdwin: layer-shell bind allowlist uid=%s exe=%s "
			   "label=%s\n",
			   qdwin->allowed_layershell_uid_set ? uidbuf : "(any)",
			   qdwin->allowed_layershell_exe
				? qdwin->allowed_layershell_exe : "(any)",
			   qdwin->allowed_layershell_label
				? qdwin->allowed_layershell_label : "(any)");
	}

	/* plan3 H1: register a handler for xdg_popup.grab on layer-
	 * parented popups. Without this hook the running libweston (if
	 * patched) posts XDG_POPUP_ERROR_INVALID_GRAB; with it, qdwin
	 * installs its own pointer grab that dismisses on outside click.
	 * dlsym-soft so an unpatched libweston still links; the layer-
	 * popup grab path then falls back to libweston's error.
	 *
	 * plan3 L2: also probe the other two layer-popup helpers and log
	 * once at startup so the operator knows what degraded behaviour to
	 * expect on an unpatched libweston. Their failure modes are silent
	 * otherwise: get_geometry missing makes popup positioning a no-op
	 * (popup floats at 0,0); dismiss missing makes outside-click
	 * detection log but never tear the popup down. */
	{
		qdwin_xdg_popup_set_layer_grab_handler_fn set_handler =
			qdwin_xdg_popup_set_layer_grab_handler_sym();
		if (set_handler) {
			set_handler(qdwin_layer_popup_layer_grab_handler,
				    qdwin);
			weston_log("qdwin: layer-popup grab handler "
				   "registered\n");
		} else {
			weston_log("qdwin: layer-popup grab handler NOT "
				   "registered (libweston symbol "
				   "weston_desktop_xdg_popup_set_layer_grab_handler "
				   "missing) — xdg_popup.grab on layer-parented "
				   "popups will return INVALID_GRAB\n");
		}
		if (!qdwin_xdg_popup_get_geometry_sym()) {
			weston_log("qdwin: layer-popup positioning DEGRADED "
				   "(libweston symbol "
				   "weston_desktop_xdg_popup_get_geometry "
				   "missing) — layer-parented popups will "
				   "render at parent origin\n");
		}
		if (!qdwin_xdg_popup_dismiss_layer_grab_sym()) {
			weston_log("qdwin: layer-popup outside-click dismissal "
				   "DEGRADED (libweston symbol "
				   "weston_desktop_xdg_popup_dismiss_layer_grab "
				   "missing) — the grab will end on outside "
				   "click but xdg_popup.popup_done will not be "
				   "sent\n");
		}
	}

	/* zxdg_decoration_manager_v1 v1: always-server_side stub. Keeps
	 * toolkit CSDs (Qt-Wayland bradient, GTK4 fallback) off so the
	 * private qdwin_shell_v1 chrome is the only decoration. */
	qdwin->xdg_decoration_manager_global = wl_global_create(
		ec->wl_display, &zxdg_decoration_manager_v1_interface,
		1, qdwin, bind_qdwin_xdg_decoration_manager);
	if (!qdwin->xdg_decoration_manager_global) {
		weston_log("qdwin: xdg-decoration wl_global_create failed\n");
		goto fail;
	}

	qdwin->destroy_listener.notify = qdwin_destroy;
	wl_signal_add(&ec->destroy_signal, &qdwin->destroy_listener);

	qdwin->output_created_listener.notify = qdwin_on_output_changed;
	wl_signal_add(&ec->output_created_signal,
		      &qdwin->output_created_listener);
	qdwin->output_resized_listener.notify = qdwin_on_output_resized;
	wl_signal_add(&ec->output_resized_signal,
		      &qdwin->output_resized_listener);
	qdwin->output_destroyed_listener.notify = qdwin_on_output_destroyed;
	wl_signal_add(&ec->output_destroyed_signal,
		      &qdwin->output_destroyed_listener);

	qdwin->seat_created_listener.notify = qdwin_on_seat_created;
	wl_signal_add(&ec->seat_created_signal,
		      &qdwin->seat_created_listener);
	/* Pick up seats that may already exist at shell-plugin load time
	 * (e.g. the default libinput seat on some backends). */
	{
		struct weston_seat *seat;
		wl_list_for_each(seat, &ec->seat_list, link)
			qdwin_track_seat(qdwin, seat);
	}

	qdwin_refresh_background(qdwin);

	/* Enables the weston-screenshooter CLI for capture during
	 * development; safe to call even if the symbol is missing
	 * from some hypothetical alternative weston build (the library
	 * loader fails fast at module load, not here). Double-gated:
	 *   (1) QDWIN_ENABLE_SCREENSHOOTER env var must be set, AND
	 *   (2) the compositor's own euid must match allowed_uid.
	 * The screenshooter exposes whole-output capture outside qdwin's
	 * per-view stream authorization model and must not ship enabled by
	 * default. See wider-codex-review Finding 6 (MEDIUM). */
	{
		const char *ss_env = getenv("QDWIN_ENABLE_SCREENSHOOTER");
		int ss_enabled = ss_env && (strcmp(ss_env, "1") == 0 ||
					    strcasecmp(ss_env, "true") == 0 ||
					    strcasecmp(ss_env, "yes") == 0);
		if (ss_enabled && geteuid() != qdwin->allowed_uid) {
			weston_log("qdwin: screenshooter env var set but "
				   "euid=%u != allowed_uid=%u — REJECTED\n",
				   (unsigned)geteuid(),
				   (unsigned)qdwin->allowed_uid);
			ss_enabled = 0;
		}
		if (ss_enabled) {
			weston_log("qdwin: WARNING screenshooter enabled via "
				   "QDWIN_ENABLE_SCREENSHOOTER — dev/test only, "
				   "do not use in production\n");
			screenshooter_create(ec);
		}
	}

	/* Test/diagnostic capture for the bound shell only. Unlike the legacy
	 * screenshooter frontend above, this merely registers a libweston
	 * authority: weston_capture_v1 remains hidden from ordinary clients by
	 * qdwin's global filter, and the callback re-checks the exact shell
	 * wl_client plus the single designated output
	 * (QDWIN_SHELL_CAPTURE_OUTPUT) at task execution time. Mirror
	 * the screenshooter euid gate so an inherited environment cannot enable
	 * this capability under a differently configured compositor identity. */
	{
		const char *capture_env = getenv("QDWIN_ENABLE_SHELL_CAPTURE");
		int capture_enabled =
			capture_env && (strcmp(capture_env, "1") == 0 ||
					strcasecmp(capture_env, "true") == 0 ||
					strcasecmp(capture_env, "yes") == 0);
		if (capture_enabled && geteuid() != qdwin->allowed_uid) {
			weston_log("qdwin: shell-capture env var set but "
				   "euid=%u != allowed_uid=%u — REJECTED\n",
				   (unsigned)geteuid(),
				   (unsigned)qdwin->allowed_uid);
			capture_enabled = 0;
		}
		if (capture_enabled) {
			struct weston_output *out;

			weston_log("qdwin: WARNING shell-authorized output capture "
				   "enabled via QDWIN_ENABLE_SHELL_CAPTURE — "
				   "dev/test only, do not use in production\n");
			weston_compositor_add_screenshot_authority(
				ec, &qdwin->capture_auth_listener,
				qdwin_capture_auth_cb);
			qdwin->capture_auth_registered = true;

			/* v33 retained-frame stale-serve: watch for the
			 * designated output going away, and arm retention on
			 * it — now if it already exists, else from
			 * output_created. */
			wl_list_init(&qdwin->capture_task_filed_listener.link);
			qdwin->capture_retain_output_gone_listener.notify =
				qdwin_capture_retain_output_gone_cb;
			wl_signal_add(&ec->output_destroyed_signal,
				      &qdwin->capture_retain_output_gone_listener);
			wl_list_for_each(out, &ec->output_list, link)
				qdwin_capture_retention_maybe_arm(qdwin, out);
		}
	}

	/* §6.8 S1: nested-mode publisher init. No-op unless
	 * QDWIN_NESTED_MODE=1 in env. Must run after compositor + outputs
	 * are ready and after the pipewire-output API can be retrieved
	 * (backend-pipewire registers its API at backend init). */
	qdwin_nested_init(qdwin);

	weston_log("qdwin: shell loaded (allowed_uid=%u%s); "
		   "desktop + qdwin_shell_v1 ready\n",
		   (unsigned)qdwin->allowed_uid,
		   qdwin->nested_mode ? "; nested-mode publisher" : "");
	return 0;

fail:
	/* We are about to free(qdwin). qdwin_singleton was published and the
	 * compositor's default pointer grab points at our interface (whose
	 * callbacks reach qdwin via qdwin_singleton). If the caller ever
	 * treats a shell-init failure as nonfatal, a later pointer event
	 * would walk the freed qdwin. Neutralise both, before any free:
	 *   - reset the compositor default pointer grab to libweston's
	 *     built-in (passing NULL restores it per
	 *     weston_pointer_set_default_grab) — this is what actually stops
	 *     our grab callbacks from being invoked after the free;
	 *   - NULL the singleton — most consumers (data-source shim, the grab
	 *     callbacks, qdwin_proxy_pointer_track_focus) null-check it and
	 *     no-op, so it is a belt-and-braces guard for any other global
	 *     hook that survives below.
	 * This only addresses the dangling-singleton/default-grab hazard
	 * (the scope of the layershell init-reorder fix); a full teardown of
	 * every wl_global / listener / key binding created earlier in init
	 * remains a separate, pre-existing cleanup concern. */
	qdwin_singleton = NULL;
	weston_compositor_set_default_pointer_grab(ec, NULL);
	if (qdwin->desktop)
		weston_desktop_destroy(qdwin->desktop);
	weston_layer_fini(&qdwin->background_layer);
	weston_layer_fini(&qdwin->held_layer);
	weston_layer_fini(&qdwin->normal_layer);
	weston_layer_fini(&qdwin->minimized_layer);
	weston_layer_fini(&qdwin->workspace_hidden_layer);
	weston_layer_fini(&qdwin->panel_layer);
	weston_layer_fini(&qdwin->notification_layer);
	weston_layer_fini(&qdwin->launcher_layer);
	weston_layer_fini(&qdwin->lock_layer);
	weston_layer_fini(&qdwin->popup_layer);
	for (int i = 0; i < 4; i++)
		weston_layer_fini(&qdwin->layer_shell_layer[i]);
	free(qdwin->allowed_locker_entrypoint);
	free(qdwin->allowed_locker_exe);
	free(qdwin->allowed_locker_label);
	free(qdwin->allowed_ime_exe);
	free(qdwin->allowed_ime_label);
	free(qdwin->allowed_layershell_exe);
	free(qdwin->allowed_layershell_label);
	free(qdwin);
	return -1;
}
