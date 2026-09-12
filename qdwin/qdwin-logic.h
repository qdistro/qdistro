/* qdwin-logic.h — pure, side-effect-free logic kernels extracted from
 * qdwin.c so they can be unit-tested in isolation (tests/unit/
 * test-qdwin-logic.c).
 *
 * Everything here takes plain scalars / out-pointers — NO struct qdwin*,
 * NO weston/wayland types, NO global state. The only non-stdlib include
 * is <libinput.h>, used solely for the libinput config enum return types
 * of the two input-config mappers (qdwin.c already links libinput).
 *
 * qdwin.c keeps the original (struct-taking) wrappers; those wrappers
 * read their struct fields and call these kernels, so there is a single
 * source of truth for the arithmetic/enum logic.
 */
#ifndef QDWIN_LOGIC_H
#define QDWIN_LOGIC_H

#include <stdint.h>
#include <stdbool.h>
#include <sys/types.h>
#include <libinput.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------
 * Input-config enum mappers (v28 set_pointer_config).
 *
 * The qdwin-side enum values are mirrored here as plain constants so
 * the kernels can be compiled without qdwin.c's enum definitions; the
 * values MUST match enum qdwin_accel_profile / enum qdwin_scroll_method
 * in qdwin.c.
 * ------------------------------------------------------------------ */
#define QDWIN_LOGIC_ACCEL_ADAPTIVE       0u
#define QDWIN_LOGIC_ACCEL_FLAT           1u

#define QDWIN_LOGIC_SCROLL_NONE          0u
#define QDWIN_LOGIC_SCROLL_TWO_FINGER    1u
#define QDWIN_LOGIC_SCROLL_EDGE          2u
#define QDWIN_LOGIC_SCROLL_ON_BUTTON_DOWN 3u

/* FLAT → flat, anything else → adaptive. */
enum libinput_config_accel_profile
qdwin_accel_profile_to_libinput(uint32_t p);

/* Maps the qdwin scroll-method enum to libinput's; unknown → 2FG. */
enum libinput_config_scroll_method
qdwin_scroll_method_to_libinput(uint32_t m);

/* ------------------------------------------------------------------
 * zwlr_layer_shell anchor / box geometry.
 *
 * Anchor bits mirror the protocol's anchor enum:
 * ------------------------------------------------------------------ */
#define QDWIN_LOGIC_ANCHOR_TOP     1u
#define QDWIN_LOGIC_ANCHOR_BOTTOM  2u
#define QDWIN_LOGIC_ANCHOR_LEFT    4u
#define QDWIN_LOGIC_ANCHOR_RIGHT   8u

/* Derive the exclusive edge. If edge_set is true (edge_val != 0 in the
 * original), edge_val is returned verbatim. Otherwise it is derived from
 * the anchor bits: single-edge anchor → that edge; three-edge anchor →
 * opposite of the free edge; ambiguous → 0 (NONE). */
uint32_t qdwin_layer_exclusive_edge(uint32_t anchor, bool edge_set,
				    uint32_t edge_val);

/* Pure rectangle layout (port of wlroots
 * wlr_scene_layer_surface_v1_configure). Given the anchor bits, the
 * desired size (0 = stretch to fill that axis), the four margins, and a
 * bounding box (bx,by,bw,bh), writes the placed rect to *out_x/_y/_w/_h.
 * Negative widths/heights are clamped to 0. */
void qdwin_layer_compute_box(uint32_t anchor,
			     int32_t desired_w, int32_t desired_h,
			     int32_t margin_top, int32_t margin_right,
			     int32_t margin_bottom, int32_t margin_left,
			     int32_t bx, int32_t by, int32_t bw, int32_t bh,
			     int32_t *out_x, int32_t *out_y,
			     uint32_t *out_w, uint32_t *out_h);

/* ------------------------------------------------------------------
 * Decorated-toplevel inset geometry.
 *
 * A toplevel's inner content extent on one axis is the outer extent
 * minus the two server-side decoration insets on that axis (e.g.
 * width = outer_w - inset_w - inset_e). The result is clamped to a
 * minimum of 1: a <=0 inner size would ship a degenerate configure to
 * the client (locking it to an unusable 0/negative surface) when the
 * insets meet or exceed the available outer extent — the very thing
 * that happens to a maximized toplevel when panel-reflow shrinks the
 * work area below the chrome insets. Single source of truth for the
 * clamp applied at every weston_desktop_surface_set_size inset site in
 * qdwin.c (qdwin_toplevel_apply_inset and the maximized panel-reflow
 * in qdwin_panels_on_output_change).
 * ------------------------------------------------------------------ */
int32_t qdwin_inset_inner_extent(int32_t outer, int32_t inset_lead,
				 int32_t inset_trail);

/* Convert a desired visible desktop-window extent to the configure extent a
 * client needs. XWayland surfaces may have a larger wl_surface buffer than
 * their desktop geometry because of CSD shadows. Its configure path applies
 * that delta again, so subtract it here to make maximize/restore idempotent.
 * Some XWayland clients (notably Chromium) expose no surface/geometry delta;
 * `learned_delta` is the response delta measured from a prior configure. Use
 * whichever signal is larger. Native Wayland callers pass zero. The result is
 * always at least one pixel. */
int32_t qdwin_client_extent_for_geometry(int32_t desired_geometry,
					 int32_t surface_extent,
					 int32_t geometry_extent,
					 int32_t learned_delta);

/* Choose the committed extent used as a floating-window restore source.
 * Native Wayland uses desktop geometry. XWayland geometry may exclude the
 * client's own frame/shadow, while its wl_surface extent is the outer size
 * reported through qdwin_shell_v1 and must survive maximize/restore. */
int32_t qdwin_committed_restore_extent(bool xwayland,
				       int32_t surface_extent,
				       int32_t geometry_extent);

/* Relocate a chrome-inclusive toplevel rectangle into a surviving output's
 * work area after its previous output disappears. Windows which fit are
 * fully clamped inside the work area; oversize windows are anchored at the
 * work-area origin so their title/leading edge remains reachable. All
 * arithmetic is overflow-safe and degenerate extents are treated as 1. */
void qdwin_rehome_outer_rect(int32_t outer_x, int32_t outer_y,
			     int32_t outer_w, int32_t outer_h,
			     int32_t work_x, int32_t work_y,
			     int32_t work_w, int32_t work_h,
			     int32_t *out_x, int32_t *out_y);

/* ------------------------------------------------------------------
 * wp_fractional_scale: clamp / env-override arithmetic.
 *
 * Scale unit is 120 (120 = 1.0x, 180 = 1.5x, 240 = 2.0x). The valid
 * fractional range is 30..960 inclusive.
 * ------------------------------------------------------------------ */
#define QDWIN_LOGIC_FRACTIONAL_MIN 30u
#define QDWIN_LOGIC_FRACTIONAL_MAX 960u

/* Clamp a raw 120-unit fractional scale into [30, 960].
 *
 * NOTE: this clamp is intentionally NOT wired into qdwin.c's output scale
 * path today. qdwin.c computes its preferred scale as `current_scale * 120u`
 * and sends that UNCLAMPED (an integer output scale times 120 is always
 * in-range by construction). The only clamp-relevant input today is the
 * QDWIN_FRACTIONAL_SCALE env override, and that path is gated by
 * qdwin_fractional_scale_env_valid() (reject-out-of-range) rather than by
 * clamping. Wire this clamp in only when qdwin adopts true fractional
 * (non-integer-derived) output scales; until then do not assume qdwin
 * clamps output-derived scales. */
uint32_t qdwin_clamp_fractional_scale_120(uint32_t raw_120);

/* True when n is a valid in-range fractional scale (30..960), i.e. the
 * QDWIN_FRACTIONAL_SCALE env override should be honoured. Mirrors the
 * original `n >= 30 && n <= 960` accept-or-fall-through gate. */
bool qdwin_fractional_scale_env_valid(long n);

/* ------------------------------------------------------------------
 * Advertised-global visibility policy (02/S1, the §4b finding).
 *
 * The compositor advertises a few PRIVILEGED globals that must not be
 * reachable by sandboxed silo clients (each grants a cross-silo attack:
 * keystroke capture, keystroke injection, screen-pixel theft, or minting
 * security contexts). The wl_global_filter installed in qdwin.c classifies
 * the binding CLIENT into a credential class and the GLOBAL into a kind,
 * then consults this pure matrix. Keeping the matrix here (a) makes the
 * per-credential-class policy a single source of truth and (b) lets a
 * compiled test enumerate every (class × kind) cell so a regression — a
 * silo client gaining a privileged global — is a mechanical test failure,
 * not a silent compositor change. See tests/unit/test-qdwin-logic.c.
 *
 * Credential classes (computed in qdwin.c from the wl_client):
 * ------------------------------------------------------------------ */
enum qdwin_cred_class {
	/* The bound privileged shell, or the authorized secctx-exec helper —
	 * the only client trusted to mint security contexts. */
	QDWIN_CRED_SHELL = 0,
	/* A plain-uid client that is neither the shell nor a secctx (silo)
	 * client (e.g. a tier-0/1 session-level tool). */
	QDWIN_CRED_ORDINARY = 1,
	/* A sandboxed client running under a wp_security_context (a silo). */
	QDWIN_CRED_SECCTX = 2,
};

/* Global kinds the filter distinguishes. ORDINARY = any inherited libweston
 * global that is NOT one of the gated privileged ones (always visible). */
enum qdwin_global_kind {
	QDWIN_GLOBAL_ORDINARY = 0,
	QDWIN_GLOBAL_INPUT_METHOD = 1,      /* zwp_input_method_manager_v2 */
	QDWIN_GLOBAL_VIRTUAL_KEYBOARD = 2,  /* zwp_virtual_keyboard_manager_v1 */
	QDWIN_GLOBAL_WESTON_CAPTURE = 3,    /* weston_capture_v1 (screen capture) */
	QDWIN_GLOBAL_SECCTX_MANAGER = 4,    /* wp_security_context_manager_v1 */
	QDWIN_GLOBAL_IDLE_NOTIFIER = 5,     /* ext_idle_notifier_v1 */
	QDWIN_GLOBAL_SHELL = 6,             /* qdwin_shell_v1 (trusted shell role) */
	QDWIN_GLOBAL_LAYER_SHELL = 7,       /* zwlr_layer_shell_v1 (overlays/lock) */
	QDWIN_GLOBAL_TOUCH_CALIBRATION = 8, /* weston_touch_calibration (input remap) */
	QDWIN_GLOBAL_LOCKER = 9,            /* qdwin_locker_v1 (session lock/unlock) */
	QDWIN_GLOBAL_NESTED_MANAGER = 10,   /* qdwin_nested_manager_v1 */
};

/* Pure policy: may a client of credential class `cred` SEE/BIND a global of
 * kind `kind`? The filter still applies its runtime identity checks on top
 * (e.g. the secctx manager's bind handler, the IME's allowed_ime_uid pin);
 * this matrix is the per-class VISIBILITY gate that runs first. */
bool qdwin_global_visible(enum qdwin_cred_class cred,
			  enum qdwin_global_kind kind);

/* Narrow exception to the generic SECCTX denial for the nested-manager
 * global. Tier-2's launcher deliberately gives its inner Weston publisher a
 * security context; only that exact engine + executable pair may see/bind the
 * manager. All ordinary silo applications remain denied. */
bool qdwin_nested_secctx_publisher_allowed(const char *sandbox_engine,
					   const char *peer_exe);

/* Exact root-installed helper allowed to obtain a secondary shell-protocol
 * resource for bind_proxy_pixels without acquiring the shell role. */
bool qdwin_nested_pixelfeed_peer_allowed(const char *peer_exe);

/* Exact root-installed helper allowed to issue set_cursor_sprite without
 * acquiring the shell role. Production issuer is
 * /usr/bin/qdistro-cursor-sprites (qdistro-cursor-sprites.service). */
bool qdwin_cursor_sprite_peer_allowed(const char *peer_exe);

/* Weston wl_data_device set_selection stale-serial test: with a current
 * source, reject incoming serials that are not newer than current
 * (unsigned wrap-safe). First set (no current source) is never stale. */
bool qdwin_selection_serial_is_stale(uint32_t current, uint32_t incoming,
				     bool has_current);
/* Only this root-installed publisher may attach authority-bound identity to a
 * nested proxy. Ordinary weston publishers and path lookalikes fail closed. */
bool qdwin_remote_nested_publisher_allowed(const char *peer_exe);

/* A nested input peer callback may already be queued while a replacement
 * connection is accepted.  Only the event source for the currently-owned fd
 * may tear down or inject through the per-toplevel input sink. */
bool qdwin_nested_input_peer_event_current(int event_fd, int current_peer_fd);

/* ------------------------------------------------------------------
 * Deliberate fail-open / broad-trust pins (02/S13).
 *
 * These helpers model three explicit production compromises in qdwin.c. They
 * keep the current preconditions executable so future hardening is conscious
 * and tested:
 *
 * 1. Output-management mutation falls back to allowed_uid before qdshell binds,
 *    and preserves the open test posture when allowed_uid == (uid_t)-1.
 * 2. The secctx helper's direct root launcher parent is accepted when the
 *    launcher's /proc/<pid>/exe basename is structurally unreadable, provided
 *    the root-parent and stable-starttime checks already passed.
 * 3. Layer-shell bind falls back to allowed_uid before qdshell binds.
 * ------------------------------------------------------------------ */
bool qdwin_om_mutation_allowed(bool client_is_bound_shell,
			       bool shell_bound,
			       pid_t client_pid, uid_t client_uid,
			       pid_t shell_pid, uid_t shell_uid,
			       uid_t allowed_uid);

bool qdwin_secctx_root_launcher_attested(uid_t parent_uid,
					 uint64_t parent_start_before,
					 uint64_t parent_start_after,
					 const char *parent_exe_basename);

bool qdwin_layershell_pre_shell_uid_allowed(uid_t client_uid,
					    uid_t allowed_uid);

/* ------------------------------------------------------------------
 * idle-inhibit (zwp_idle_inhibitor_v1) hold policy — iso2/11 E2.
 *
 * The protocol permits a compositor to ignore an inhibitor whose
 * surface is not visible.  We take the *mapped* half of that
 * permission and deliberately decline the occlusion half:
 *
 *   - mapped + has-a-buffer + has-a-view is what closes the
 *     zero-effort hole.  A silo that creates a wl_surface, never
 *     attaches a buffer, and takes an inhibitor would otherwise
 *     suppress idle-lock and DPMS for the rest of the session with
 *     nothing on screen.  The buffer and view terms additionally
 *     reject two surfaces that weston leaves with a stale-true mapped
 *     bit: a bufferless child of an unmapped parent (reads as mapped
 *     again once the parent remaps), and a surface whose
 *     wl_subsurface role was destroyed while the wl_surface was kept
 *     (every view destroyed, mapped bit and buffer untouched, and the
 *     recursive query no longer walks to the unmapped parent).
 *     Minimisation is unaffected: qdwin minimises by moving the view
 *     to another layer, so the view survives.
 *   - occluded/minimised is left inhibiting on purpose.  "Video in a
 *     silo keeps the session awake" is a product feature, and a
 *     partly covered or minimised media window is exactly the case
 *     users expect to keep playing.
 *
 * This is a product policy, NOT a visibility security boundary, and
 * the difference is deliberate.  The terms prove a mapped surface
 * with a buffer and a live view; they do NOT prove that any view is
 * mapped, sits on a displayed layer, intersects an output, or draws a
 * non-transparent pixel.  Accepted residual: a 1x1 fully transparent
 * mapped surface with a buffer, a mapped surface moved off every
 * output, or one whose views are retained but not displayed, all
 * still inhibit.  No cheap predicate in this
 * weston proves visible content without also breaking the
 * covered/minimised-video behaviour we must keep.  What is closed is
 * the zero-effort version: never-mapped and effectively unmapped
 * surfaces hold nothing — released at the next successful backstop
 * sweep, normally within a second plus event-loop delay, when the
 * change is one no signal reports.
 *
 * Callers must re-evaluate; see qdwin_idle_inhibitor_sync() and the
 * §6.7 banner in qdwin.c for why the surface's own map/unmap signals
 * are necessary but not sufficient (weston's mapped query recurses up
 * the subsurface tree, so an ancestor change moves the answer with no
 * signal on this surface at all).
 * ------------------------------------------------------------------ */
bool qdwin_idle_inhibit_should_hold(bool have_surface, bool surface_mapped,
				    bool has_buffer, bool has_view);

#ifdef __cplusplus
}
#endif

#endif /* QDWIN_LOGIC_H */
