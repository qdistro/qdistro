/*
 * qdwin-bystander — long-lived stand-in shell for qdwin app testing.
 *
 * Binds qdwin_shell_v1 at v14 (set_keyboard_focus available), calls
 * bind_as_shell, and on every toplevel_added event:
 *   - calls set_border_color (releases the held-layer hold so pixels
 *     paint),
 *   - calls set_keyboard_focus on the default seat so the app
 *     receives keyboard input.
 *
 * CLI flags (optional):
 *   --subscribe <handle>   send subscribe_view_stream for HANDLE once
 *                          the bind roundtrip completes (or once a
 *                          toplevel with that handle is replayed/added).
 *   --subscribe last       subscribe to the first toplevel_added we
 *                          see after start (handy when the test spawns
 *                          the target after the bystander).
 *   --peer-label <s>       peer_label arg for the subscribe; default
 *                          "qdwin-bystander".
 *
 *   §P10 Shape A flags (tier-4-guest nested-qdwin session forwarding):
 *
 *   --inner-display <socket>
 *                          Connect to the named Wayland socket as the
 *                          *inner* compositor (guest qdwin's wayland-0).
 *                          This is a SEPARATE wl_display from the outer
 *                          one. The outer wl_display comes from the
 *                          ambient $WAYLAND_DISPLAY (which, when wrapped
 *                          by waypipe-server, is the intercept socket
 *                          that ferries protocol over vsock to the host
 *                          compositor). We deliberately do NOT setenv
 *                          WAYLAND_DISPLAY for the inner socket — that
 *                          would clobber waypipe-server's intercept and
 *                          defeat the entire transport.
 *                          (Backwards-compat: `--connect` is accepted as
 *                          an alias but, in --forward-session mode, is
 *                          treated the same as --inner-display.)
 *
 *   --forward-session      §P10 Shape A — one window per VM. On bind to
 *                          the inner compositor, create exactly ONE
 *                          outer xdg_toplevel on the outer wl_display
 *                          to represent the entire guest session. Inner
 *                          xdg_toplevel additions/removals are logged
 *                          as structured "FORWARD …" lines on stdout
 *                          (with newline / quote escaping so guest-side
 *                          attacker-controlled app_id / title strings
 *                          cannot spoof additional FORWARD lines), but
 *                          DO NOT each spawn a new outer toplevel.
 *                          Pixel-perfect mirroring of inner surfaces
 *                          onto the outer toplevel is P11's job; for
 *                          P10 the outer toplevel attaches a minimal
 *                          SHM placeholder buffer purely to satisfy the
 *                          Wayland protocol's "no buffer = no map"
 *                          requirement and prove the outer-toplevel
 *                          end-to-end path works.
 *
 *   --forward-all-toplevels
 *                          Deprecated spelling of --forward-session.
 *                          Kept so the in-tree publisher.sh and unit
 *                          tests don't all need to flip in lockstep.
 *
 *   --connect <socket>     §P10 legacy spelling of --inner-display.
 *                          Pre-fix-pass it stomped WAYLAND_DISPLAY,
 *                          which broke the waypipe transport (see the
 *                          P10 correctness/integration reviews H2/H3).
 *                          Now it's a pure alias for --inner-display
 *                          with no env mutation.
 *
 * Spec: plan2/tasks/P10-tier4-guest-image-nested-qdwin.md §Phase B.
 *
 * On `approved`, the credentials are printed to stdout as KEY=value
 * lines suitable for shell sourcing:
 *   HANDLE=<n>
 *   PIPEWIRE_NODE_NAME=<s>
 *   RDP_PORT=<n>
 *   RDP_CERT_PATH=<s>
 *   RDP_PASSWORD=<s>
 * The wayland connection is kept open so the stream stays live.
 *
 * Reads commands on a FIFO (default /tmp/qdwin-cmd.fifo), one per line:
 *   max <handle>      → request_maximize(handle, 1)
 *   restore <handle>  → request_maximize(handle, 0)
 *   min <handle>      → request_minimize(handle)
 *   close <handle>    → request_close(handle)
 *   focus <handle>    → set_keyboard_focus("default", handle)
 *   raise <handle>    → request_raise(handle)  (deterministic z-order)
 *   move <handle> <x> <y> → request_set_position(handle, x, y)  (v30)
 *   subscribe <handle> → subscribe_view_stream(handle)
 *   subscribelast     → subscribe to the most recently added toplevel
 *   list              → print last seen toplevels to stderr
 *   maxlast / restorelast → operate on most recently added toplevel
 *   tile <handle> <left|right|none>  → request_tile (v25)
 *   fullscreen <handle> [0|1]        → request_fullscreen (v25)
 *   wmpolicy <focus> <ffm_ms> <raise_click> <raise_hover> <placement>
 *            <snap_en> <snap_dist>   → set_wm_policy (v25)
 *   hotkey <id> <modifiers> <key>    → register_hotkey (v19/v25)
 *   displaypower <0|1>               → set_display_power (v26 DPMS)
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <poll.h>
#include <errno.h>

#include <wayland-client.h>
#include "qdwin-shell-v1-client-protocol.h"
#include "xdg-shell-client-protocol.h"

/* Bind the complete shell protocol: v25 window-manager controls, v30
 * request_set_position, and v31 live app-id updates. The bind remains clamped
 * to the version advertised by an older compositor. */
#define BIND_VERSION 31
#define MAX_TOPS 16
#define MAX_STREAMS 4
#define FIFO_PATH_DEFAULT "/tmp/qdwin-cmd.fifo"

struct top_info {
	uint32_t handle;
	int active;
};

struct stream_info {
	uint32_t handle;
	struct qdwin_view_stream_v1 *stream;
	struct app *app;
};

struct pending_subscribe {
	int armed;
	int wait_first;        /* 1 = subscribe to the next toplevel_added */
	uint32_t handle;
};

/* §P10 Shape A: outer wl_display state. Separate from the inner one
 * so the bystander can connect to *both* the in-guest compositor (via
 * the named --inner-display socket) AND the outer compositor (via the
 * ambient $WAYLAND_DISPLAY which, when waypipe-server wraps us, is
 * its own intercept socket that tunnels over vsock to the host).
 *
 * Exactly ONE outer xdg_toplevel is created when --forward-session
 * is active — the toplevel represents the whole VM session, not any
 * individual inner toplevel. */
struct outer_state {
	struct wl_display *display;
	struct wl_registry *registry;
	struct wl_compositor *compositor;
	struct wl_shm *shm;
	struct xdg_wm_base *wm_base;
	struct wl_surface *surface;
	struct xdg_surface *xdg_surface;
	struct xdg_toplevel *toplevel;
	int configured;
};

struct app {
	struct qdwin_shell_v1 *shell;
	struct top_info tops[MAX_TOPS];
	uint32_t last_handle;
	int got_last;
	struct stream_info streams[MAX_STREAMS];
	struct pending_subscribe pending;
	char peer_label[64];
	/* --allow-input: request an input-capable subscription (allow_input=1)
	 * instead of the read-only default. SERVER-ENFORCED (codex impl-14): with
	 * allow_input=0 the compositor DROPS every injected event for the stream, so
	 * a read-only export cannot be driven by the subscriber's qdistro-forward.
	 * Used by the step-8 input-confinement gate + its read-only negative control
	 * (codex impl-10/13): allow_input=1 lets the forward inject into the per-stream
	 * seat; allow_input=0 must yield zero injected presses. */
	int allow_input;
	/* Test-only lifecycle probe: retain the inert protocol tombstone after a
	 * server-side torn_down event. This proves qdwin releases server-owned
	 * stream state without relying on a cooperative destructor request. */
	int ignore_torn_down;
	/* Transient per-subscribe allow_input override (-1 = use `allow_input`).
	 * Set by the `subscribe`/`subscribelast` FIFO commands when they carry an
	 * explicit 0/1, consumed by do_subscribe, then reset to -1. */
	int sub_ai_override;
	/* §P10 --forward-session: when set, every toplevel_added emits a
	 * "FORWARD toplevel ..." stdout line so a wrapping waypipe-server
	 * (and any downstream consumer) sees the enumeration. Per-inner-
	 * toplevel events do NOT each create an outer toplevel — Shape A
	 * is one outer toplevel per VM session. */
	int forward_session;
	/* §P10 toplevel count seen since start. Used by the unit tests
	 * to assert dispatch fired without requiring a real compositor. */
	unsigned forward_toplevels_seen;
	unsigned forward_removes_seen;
	/* §P10 Shape A outer-display state (only populated when
	 * --forward-session is on and the outer wl_display_connect
	 * succeeded). NULL on plain --connect / non-forwarding modes. */
	struct outer_state *outer;
};

static struct app g_app;

static void do_subscribe(struct app *a, uint32_t handle);

/* §P10 SF1 / security-F1 / correctness-S1 — escape attacker-controlled
 * strings before printing them inside the structured "FORWARD …" line.
 * Inner wl_clients can call xdg_toplevel.set_title / set_app_id with
 * arbitrary bytes (newlines, double-quotes, backslashes); without
 * escaping a guest app could spoof additional FORWARD lines that a
 * host-side parser would treat as real events. We strip CR/LF entirely
 * (replaced with literal "\\n" / "\\r" sequences) and backslash-escape
 * `"` and `\` so downstream regex parsers keying on "..." substrings
 * survive untouched. Output is bounded; over-long titles get
 * truncated to keep the resulting line manageable.
 *
 * Returns dst for convenience. dst_sz must be >=1. */
static char *
escape_forward_field(char *dst, size_t dst_sz, const char *src)
{
	if (dst_sz == 0) return dst;
	if (!src) src = "";
	size_t i = 0;
	for (; *src && i + 2 < dst_sz; src++) {
		unsigned char c = (unsigned char)*src;
		if (c == '\n') {
			if (i + 2 >= dst_sz) break;
			dst[i++] = '\\'; dst[i++] = 'n';
		} else if (c == '\r') {
			if (i + 2 >= dst_sz) break;
			dst[i++] = '\\'; dst[i++] = 'r';
		} else if (c == '\\') {
			if (i + 2 >= dst_sz) break;
			dst[i++] = '\\'; dst[i++] = '\\';
		} else if (c == '"') {
			if (i + 2 >= dst_sz) break;
			dst[i++] = '\\'; dst[i++] = '"';
		} else if (c < 0x20 || c == 0x7f) {
			/* drop / replace other control bytes — they'd
			 * confuse parsers similarly to \n. */
			if (i + 1 >= dst_sz) break;
			dst[i++] = '?';
		} else {
			dst[i++] = (char)c;
		}
	}
	dst[i] = '\0';
	return dst;
}

static void
on_hello(void *d, struct qdwin_shell_v1 *s, uint32_t uid)
{
	(void)d; (void)s;
	fprintf(stderr, "qdwin-bystander: hello uid=%u\n", uid);
}

static void
record_toplevel(struct app *a, uint32_t handle)
{
	for (int i = 0; i < MAX_TOPS; i++) {
		if (!a->tops[i].active) {
			a->tops[i].handle = handle;
			a->tops[i].active = 1;
			break;
		}
	}
	a->last_handle = handle;
	a->got_last = 1;
}

static void
forget_toplevel(struct app *a, uint32_t handle)
{
	for (int i = 0; i < MAX_TOPS; i++) {
		if (a->tops[i].active && a->tops[i].handle == handle)
			a->tops[i].active = 0;
	}
	if (a->got_last && a->last_handle == handle)
		a->got_last = 0;
}

static void
on_toplevel_added(void *d, struct qdwin_shell_v1 *s,
		  uint32_t handle, uint32_t owner_uid,
		  const char *app_id, const char *title, uint32_t is_xwayland)
{
	struct app *a = d;
	/* owner_uid is advertised as a uint32_t; the compositor sends
	 * 0xFFFFFFFF ((uid_t)-1) for surfaces with no readable peer (XWayland
	 * client==NULL). Log it so scenarios can assert XWayland windows are
	 * reported as uid=4294967295 (unknown/untrusted), NOT the compositor's
	 * own admin uid (FINDING #6). */
	fprintf(stderr,
		"qdwin-bystander: toplevel_added handle=%u owner_uid=%u app_id=\"%s\" title=\"%s\" xwayland=%u\n",
		handle, owner_uid, app_id ? app_id : "", title ? title : "",
		is_xwayland);
	qdwin_shell_v1_set_border_color(s, handle, 0x0088aaffu);
	qdwin_shell_v1_set_keyboard_focus(s, "default", handle);
	record_toplevel(a, handle);

	/* §P10 --forward-session: structured stdout for waypipe-server
	 * (or unit tests) to pick up. Kept on a single line for trivial
	 * grep / regex matching. Attacker-controlled fields are escaped
	 * to prevent newline / quote injection (SF1). */
	if (a->forward_session) {
		char esc_app_id[256], esc_title[256];
		escape_forward_field(esc_app_id, sizeof esc_app_id, app_id);
		escape_forward_field(esc_title, sizeof esc_title, title);
		printf("FORWARD toplevel handle=%u app_id=\"%s\" title=\"%s\" xwayland=%u\n",
		       handle, esc_app_id, esc_title, is_xwayland);
		fflush(stdout);
		a->forward_toplevels_seen++;
	}

	if (a->pending.armed) {
		int match = a->pending.wait_first
			|| (a->pending.handle == handle);
		if (match) {
			a->pending.armed = 0;
			do_subscribe(a, handle);
		}
	}
}

/* ---- noop event listeners (signatures must match exactly) ---- */
static void l_geom(void *d, struct qdwin_shell_v1 *s, uint32_t h,
		   int32_t x, int32_t y, uint32_t w, uint32_t H)
{
	(void)d; (void)s;
	/* Logged so host tests (e.g. tests/host/13-wm-policy.md) can scrape
	 * the last geometry for a handle after a tile/fullscreen/placement. */
	fprintf(stderr, "qdwin-bystander: toplevel_geometry handle=%u "
		"x=%d y=%d w=%u h=%u\n", h, x, y, w, H);
}
static void l_state(void *d, struct qdwin_shell_v1 *s, uint32_t h, uint32_t st)
{
	(void)d; (void)s;
	fprintf(stderr, "qdwin-bystander: toplevel_state handle=%u state=0x%x\n",
		h, st);
}
static void l_title(void *d, struct qdwin_shell_v1 *s, uint32_t h, const char *t)
{ (void)d; (void)s; (void)h; (void)t; }
static void l_removed(void *d, struct qdwin_shell_v1 *s, uint32_t h)
{
	(void)s;
	struct app *a = d;
	fprintf(stderr, "qdwin-bystander: toplevel_removed handle=%u\n", h);
	forget_toplevel(a, h);
	/* §P10 --forward-session: paired removal so any downstream wrap
	 * can drop its inner-toplevel bookkeeping for this handle. The
	 * outer xdg_toplevel is NOT destroyed here — it represents the
	 * whole VM session and lives as long as the inner compositor
	 * connection does. */
	if (a->forward_session) {
		printf("FORWARD remove handle=%u\n", h);
		fflush(stdout);
		a->forward_removes_seen++;
	}
}
static void l_locked(void *d, struct qdwin_shell_v1 *s, uint32_t l)
{ (void)d; (void)s; (void)l; }
static void l_seat_created(void *d, struct qdwin_shell_v1 *s, const char *name)
{ (void)d; (void)s; (void)name; }
static void l_seat_removed(void *d, struct qdwin_shell_v1 *s, const char *name)
{ (void)d; (void)s; (void)name; }
static void l_output_created(void *d, struct qdwin_shell_v1 *s, const char *n)
{ (void)d; (void)s; (void)n; }
static void l_output_removed(void *d, struct qdwin_shell_v1 *s, const char *n)
{ (void)d; (void)s; (void)n; }
static void l_launcher(void *d, struct qdwin_shell_v1 *s)
{ (void)d; (void)s; }
static void l_switcher_next(void *d, struct qdwin_shell_v1 *s, int32_t dir)
{ (void)d; (void)s; (void)dir; }
static void l_switcher_commit(void *d, struct qdwin_shell_v1 *s)
{ (void)d; (void)s; }
static void l_lock_req(void *d, struct qdwin_shell_v1 *s)
{ (void)d; (void)s; }
static void l_idle_hint(void *d, struct qdwin_shell_v1 *s, uint32_t reason)
{ (void)d; (void)s; (void)reason; }
static void l_nested_pending(void *d, struct qdwin_shell_v1 *s,
			     uint32_t h, const char *app_id, uint32_t origin_uid)
{ (void)d; (void)s; (void)h; (void)app_id; (void)origin_uid; }
static void l_nested_pixsrc(void *d, struct qdwin_shell_v1 *s,
			    uint32_t h, const char *pw_node, const char *input_sink)
{ (void)d; (void)s; (void)h; (void)pw_node; (void)input_sink; }
static void l_selection_set(void *d, struct qdwin_shell_v1 *s,
			    const char *seat, uint32_t source_h,
			    const char *mimes, uint32_t serial)
{ (void)d; (void)s; (void)seat; (void)source_h; (void)mimes; (void)serial; }
static void l_activation_pending(void *d, struct qdwin_shell_v1 *s,
				 uint32_t h, uint32_t source_h,
				 uint32_t target_h, const char *token)
{ (void)d; (void)s; (void)h; (void)source_h; (void)target_h; (void)token; }
static void l_secctx(void *d, struct qdwin_shell_v1 *s,
		     uint32_t h, const char *engine, const char *app_id,
		     const char *instance_id)
{
	(void)d; (void)s;
	/* §P10 L5 / log-shape — emit on stderr so stdout stays a clean
	 * key=value / FORWARD … channel that downstream parsers can
	 * consume without filtering by prefix. */
	fprintf(stderr, "qdwin-bystander: toplevel_security_context handle=%u "
		"engine=\"%s\" app_id=\"%s\" instance_id=\"%s\"\n",
		h, engine ? engine : "(null)",
		app_id ? app_id : "(null)",
		instance_id ? instance_id : "(null)");
}
static void l_seat_focus_changed(void *d, struct qdwin_shell_v1 *s,
				 const char *seat, uint32_t handle)
{ (void)d; (void)s; (void)seat; (void)handle; }
/* v15-v25 events: the bystander binds at BIND_VERSION (25) so the
 * compositor will deliver every event up to v25. libwayland aborts the
 * client if any delivered event has a NULL listener slot, so provide a
 * no-op (or logging) handler for each. hotkey_pressed logs so the
 * register_hotkey host test can confirm delivery. */
static void l_overlay_key(void *d, struct qdwin_shell_v1 *s, uint32_t role,
			  uint32_t sym, const char *utf8, uint32_t state)
{ (void)d; (void)s; (void)role; (void)sym; (void)utf8; (void)state; }
static void l_selection_set_src_id(void *d, struct qdwin_shell_v1 *s,
				   const char *eng, const char *app,
				   const char *inst)
{ (void)d; (void)s; (void)eng; (void)app; (void)inst; }
static void l_peer_identity(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			    uint32_t pid, uint32_t st, uint32_t st_hi,
			    uint32_t uid, const char *exe, const char *label)
{ (void)d; (void)s; (void)h; (void)pid; (void)st; (void)st_hi; (void)uid;
  (void)exe; (void)label; }
static void l_data_offer_recv_pending(void *d, struct qdwin_shell_v1 *s,
				      uint32_t rh, const char *seat,
				      uint32_t src, uint32_t tgt,
				      const char *mime)
{ (void)d; (void)s; (void)rh; (void)seat; (void)src; (void)tgt; (void)mime; }
static void l_hotkey_pressed(void *d, struct qdwin_shell_v1 *s, uint32_t id)
{
	(void)d; (void)s;
	fprintf(stderr, "qdwin-bystander: hotkey_pressed id=%u\n", id);
}
static void l_chrome_button(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			    uint32_t side, wl_fixed_t sx, wl_fixed_t sy,
			    uint32_t button, uint32_t state, uint32_t serial)
{ (void)d; (void)s; (void)h; (void)side; (void)sx; (void)sy; (void)button;
  (void)state; (void)serial; }
static void l_popup_button(void *d, struct qdwin_shell_v1 *s, uint32_t h,
			   wl_fixed_t sx, wl_fixed_t sy, uint32_t button,
			   uint32_t state, uint32_t serial)
{ (void)d; (void)s; (void)h; (void)sx; (void)sy; (void)button; (void)state;
  (void)serial; }
static void l_toplevel_workspace(void *d, struct qdwin_shell_v1 *s,
				 uint32_t h, uint32_t index)
{ (void)d; (void)s; (void)h; (void)index; }
static void l_toplevel_app_id(void *d, struct qdwin_shell_v1 *s,
			      uint32_t h, const char *app_id)
{
	(void)d; (void)s;
	fprintf(stderr, "qdwin-bystander: toplevel_app_id handle=%u app_id=\"%s\"\n",
		h, app_id ? app_id : "");
}

static const struct qdwin_shell_v1_listener listener = {
	.hello                  = on_hello,
	.toplevel_added         = on_toplevel_added,
	.toplevel_geometry      = l_geom,
	.toplevel_state         = l_state,
	.toplevel_title         = l_title,
	.toplevel_removed       = l_removed,
	.locked_changed         = l_locked,
	.seat_created           = l_seat_created,
	.seat_removed           = l_seat_removed,
	.output_created         = l_output_created,
	.output_removed         = l_output_removed,
	.launcher_requested     = l_launcher,
	.switcher_next          = l_switcher_next,
	.switcher_commit        = l_switcher_commit,
	.lock_requested         = l_lock_req,
	.idle_lock_hint         = l_idle_hint,
	.nested_proxy_pending   = l_nested_pending,
	.nested_proxy_pixel_source = l_nested_pixsrc,
	.selection_set          = l_selection_set,
	.activation_pending     = l_activation_pending,
	.toplevel_security_context = l_secctx,
	.toplevel_peer_identity = l_peer_identity,
	.seat_focus_changed     = l_seat_focus_changed,
	.overlay_key            = l_overlay_key,
	.selection_set_source_identity = l_selection_set_src_id,
	.data_offer_receive_pending = l_data_offer_recv_pending,
	.hotkey_pressed         = l_hotkey_pressed,
	.chrome_button          = l_chrome_button,
	.popup_button           = l_popup_button,
	.toplevel_workspace     = l_toplevel_workspace,
	.toplevel_app_id        = l_toplevel_app_id,
};

/* ---- qdwin_view_stream_v1 listener ---- */
static void
on_stream_approved(void *d, struct qdwin_view_stream_v1 *stream,
		   const char *pipewire_node_name, uint32_t rdp_port,
		   const char *rdp_cert_path, const char *rdp_password)
{
	struct stream_info *si = d;
	(void)stream;
	fprintf(stderr,
		"qdwin-bystander: view_stream approved handle=%u "
		"pw=%s port=%u cert=%s\n",
		si->handle, pipewire_node_name ? pipewire_node_name : "",
		rdp_port, rdp_cert_path ? rdp_cert_path : "");
	printf("HANDLE=%u\n", si->handle);
	printf("PIPEWIRE_NODE_NAME=%s\n",
	       pipewire_node_name ? pipewire_node_name : "");
	printf("RDP_PORT=%u\n", rdp_port);
	printf("RDP_CERT_PATH=%s\n", rdp_cert_path ? rdp_cert_path : "");
	printf("RDP_PASSWORD=%s\n", rdp_password ? rdp_password : "");
	fflush(stdout);
}

static void
on_stream_denied(void *d, struct qdwin_view_stream_v1 *stream,
		 const char *reason)
{
	struct stream_info *si = d;
	fprintf(stderr,
		"qdwin-bystander: view_stream denied handle=%u reason=\"%s\"\n",
		si->handle, reason ? reason : "");
	qdwin_view_stream_v1_destroy(stream);
	si->stream = NULL;
}

static void
on_stream_torn_down(void *d, struct qdwin_view_stream_v1 *stream,
		    const char *reason)
{
	struct stream_info *si = d;
	fprintf(stderr,
		"qdwin-bystander: view_stream torn_down handle=%u reason=\"%s\"\n",
		si->handle, reason ? reason : "");
	if (si->app->ignore_torn_down) {
		fprintf(stderr,
			"qdwin-bystander: retaining inert view_stream handle=%u "
			"(--ignore-torn-down)\n", si->handle);
		return;
	}
	qdwin_view_stream_v1_destroy(stream);
	si->stream = NULL;
}

static const struct qdwin_view_stream_v1_listener stream_listener = {
	.approved  = on_stream_approved,
	.denied    = on_stream_denied,
	.torn_down = on_stream_torn_down,
};

static void
do_subscribe(struct app *a, uint32_t handle)
{
	if (!a->shell) {
		fprintf(stderr, "qdwin-bystander: subscribe handle=%u: shell not bound\n",
			handle);
		return;
	}
	int slot = -1;
	for (int i = 0; i < MAX_STREAMS; i++) {
		if (!a->streams[i].stream) { slot = i; break; }
	}
	if (slot < 0) {
		fprintf(stderr, "qdwin-bystander: subscribe handle=%u: stream table full\n",
			handle);
		return;
	}
	const char *label = a->peer_label[0] ? a->peer_label : "qdwin-bystander";
	/* ai_override <0 => use the process-global --allow-input flag; 0/1 => set
	 * this subscription's allow_input explicitly. Per-handle override lets ONE
	 * bystander (qdwin_shell_v1 is a singleton role) give two exported streams
	 * DIFFERENT allow_input — needed by the rung-1 per-window allow_input gate
	 * (A=0/B=1 then flipped). */
	uint32_t ai = (a->sub_ai_override >= 0)
		? (uint32_t)(a->sub_ai_override ? 1 : 0)
		: (a->allow_input ? 1u : 0u);
	struct qdwin_view_stream_v1 *s = qdwin_shell_v1_subscribe_view_stream(
		a->shell, handle, label, 0, 0, ai);
	fprintf(stderr, "qdwin-bystander: subscribe handle=%u allow_input=%u\n",
		handle, ai);
	if (!s) {
		fprintf(stderr, "qdwin-bystander: subscribe handle=%u: proxy create failed\n",
			handle);
		return;
	}
	a->streams[slot].handle = handle;
	a->streams[slot].stream = s;
	a->streams[slot].app = a;
	qdwin_view_stream_v1_add_listener(s, &stream_listener, &a->streams[slot]);
	fprintf(stderr, "qdwin-bystander: subscribe sent handle=%u peer_label=\"%s\"\n",
		handle, label);
}

static void
on_global(void *data, struct wl_registry *reg, uint32_t name,
	  const char *interface, uint32_t version)
{
	struct app *a = data;
	if (strcmp(interface, qdwin_shell_v1_interface.name) == 0) {
		uint32_t v = version < BIND_VERSION ? version : BIND_VERSION;
		a->shell = wl_registry_bind(reg, name,
					    &qdwin_shell_v1_interface, v);
		qdwin_shell_v1_add_listener(a->shell, &listener, a);
		fprintf(stderr, "qdwin-bystander: bound qdwin_shell_v1 v%u\n", v);
	}
}

static void on_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }

static const struct wl_registry_listener registry_listener = {
	.global = on_global,
	.global_remove = on_global_remove,
};

/* ====================================================================
 * §P10 Shape A — outer wl_display setup.
 *
 * One outer xdg_toplevel for the entire tier-4 VM session. The outer
 * compositor is whatever lives at $WAYLAND_DISPLAY in our environment.
 * Under the publisher, that's waypipe-server's intercept socket which
 * tunnels protocol over vsock to the host-side waypipe-client (and on
 * to the host qdwin). Under unit tests, $WAYLAND_DISPLAY points at a
 * non-existent socket and the outer connect fails fast — we just log
 * the failure and continue running the inner half (the unit tests
 * assert on stderr lines, not on a real outer toplevel).
 * ==================================================================== */

static void
outer_wm_base_ping(void *data, struct xdg_wm_base *base, uint32_t serial)
{
	(void)data;
	xdg_wm_base_pong(base, serial);
}
static const struct xdg_wm_base_listener outer_wm_base_listener = {
	.ping = outer_wm_base_ping,
};

static void
outer_registry_global(void *data, struct wl_registry *reg, uint32_t name,
		      const char *interface, uint32_t version)
{
	struct outer_state *o = data;
	if (!strcmp(interface, wl_compositor_interface.name)) {
		uint32_t v = version > 4 ? 4 : version;
		o->compositor = wl_registry_bind(reg, name,
						 &wl_compositor_interface, v);
	} else if (!strcmp(interface, wl_shm_interface.name)) {
		o->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
	} else if (!strcmp(interface, xdg_wm_base_interface.name)) {
		o->wm_base = wl_registry_bind(reg, name,
					      &xdg_wm_base_interface, 1);
		xdg_wm_base_add_listener(o->wm_base,
					 &outer_wm_base_listener, NULL);
	}
}
static void outer_registry_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener outer_registry_listener = {
	.global = outer_registry_global,
	.global_remove = outer_registry_global_remove,
};

static void
outer_xdg_surface_configure(void *data, struct xdg_surface *xs, uint32_t serial)
{
	struct outer_state *o = data;
	xdg_surface_ack_configure(xs, serial);
	o->configured = 1;
}
static const struct xdg_surface_listener outer_xdg_surface_listener = {
	.configure = outer_xdg_surface_configure,
};

static void
outer_xdg_toplevel_configure(void *d, struct xdg_toplevel *t,
			     int32_t w, int32_t h, struct wl_array *states)
{ (void)d; (void)t; (void)w; (void)h; (void)states; }
static void
outer_xdg_toplevel_close(void *d, struct xdg_toplevel *t)
{ (void)d; (void)t; /* host asked us to close; ignore for now — the
                       publisher's lifecycle is driven by inner
                       compositor death, not outer close. */ }
static void
outer_xdg_toplevel_configure_bounds(void *d, struct xdg_toplevel *t,
				    int32_t mw, int32_t mh)
{ (void)d; (void)t; (void)mw; (void)mh; }
static void
outer_xdg_toplevel_wm_capabilities(void *d, struct xdg_toplevel *t,
				   struct wl_array *caps)
{ (void)d; (void)t; (void)caps; }
static const struct xdg_toplevel_listener outer_xdg_toplevel_listener = {
	.configure = outer_xdg_toplevel_configure,
	.close = outer_xdg_toplevel_close,
	.configure_bounds = outer_xdg_toplevel_configure_bounds,
	.wm_capabilities = outer_xdg_toplevel_wm_capabilities,
};

/* §P10 Shape A — placeholder SHM buffer.
 * Pixel-perfect mirroring of inner surfaces onto this outer toplevel
 * is P11's job. For P10 we attach a tiny solid-colour buffer purely
 * to (a) satisfy the Wayland protocol's "the surface must have a
 * buffer attached before it can be mapped" rule, and (b) prove the
 * outer-toplevel end-to-end allocation path works through the
 * waypipe-server intercept. The buffer is intentionally minimal. */
static struct wl_buffer *
outer_make_placeholder_buffer(struct wl_shm *shm, int w, int h, uint32_t argb)
{
	int stride = w * 4;
	int size = stride * h;
	int fd = memfd_create("qdwin-bystander-outer", MFD_CLOEXEC);
	if (fd < 0) return NULL;
	if (ftruncate(fd, size) < 0) { close(fd); return NULL; }
	uint32_t *p = mmap(NULL, size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
	if (p == MAP_FAILED) { close(fd); return NULL; }
	for (int i = 0; i < w * h; i++) p[i] = argb;
	struct wl_shm_pool *pool = wl_shm_create_pool(shm, fd, size);
	struct wl_buffer *buf = wl_shm_pool_create_buffer(
		pool, 0, w, h, stride, WL_SHM_FORMAT_ARGB8888);
	wl_shm_pool_destroy(pool);
	munmap(p, size);
	close(fd);
	return buf;
}

/* Attempt to connect to the outer wl_display (whatever $WAYLAND_DISPLAY
 * points at in our env — typically waypipe-server's intercept socket
 * when this binary is the waypipe-server's child).
 *
 * Returns 0 on success (and stashes display + bindings + the one
 * outer xdg_toplevel into *out*), or -1 on any failure (with stderr
 * log). Failure is non-fatal: we still want to drive the inner
 * compositor connection and emit FORWARD lines, which the unit tests
 * exercise without a real outer socket. */
static int
outer_setup_one_toplevel(struct outer_state *out)
{
	memset(out, 0, sizeof *out);
	out->display = wl_display_connect(NULL);
	if (!out->display) {
		fprintf(stderr,
			"qdwin-bystander: outer wl_display_connect failed "
			"(WAYLAND_DISPLAY=%s); --forward-session will emit "
			"FORWARD lines only, no outer toplevel\n",
			getenv("WAYLAND_DISPLAY") ? getenv("WAYLAND_DISPLAY")
						 : "(unset)");
		return -1;
	}
	out->registry = wl_display_get_registry(out->display);
	wl_registry_add_listener(out->registry, &outer_registry_listener, out);
	wl_display_roundtrip(out->display);
	if (!out->compositor || !out->shm || !out->wm_base) {
		fprintf(stderr,
			"qdwin-bystander: outer missing globals "
			"(compositor=%p shm=%p xdg_wm_base=%p)\n",
			(void*)out->compositor, (void*)out->shm,
			(void*)out->wm_base);
		return -1;
	}
	out->surface = wl_compositor_create_surface(out->compositor);
	out->xdg_surface = xdg_wm_base_get_xdg_surface(out->wm_base, out->surface);
	xdg_surface_add_listener(out->xdg_surface, &outer_xdg_surface_listener, out);
	out->toplevel = xdg_surface_get_toplevel(out->xdg_surface);
	xdg_toplevel_add_listener(out->toplevel, &outer_xdg_toplevel_listener, out);
	xdg_toplevel_set_title(out->toplevel, "qdistro tier-4 guest");
	xdg_toplevel_set_app_id(out->toplevel, "qdistro.tier4.guest");
	wl_surface_commit(out->surface);
	wl_display_roundtrip(out->display);

	/* Attach a minimal placeholder buffer so the outer compositor
	 * actually maps the surface. (P11 will replace this with real
	 * pixel transport from the inner compositor.) */
	struct wl_buffer *buf = outer_make_placeholder_buffer(
		out->shm, 64, 64, 0xff202830u);  /* dark slate, distinctive */
	if (!buf) {
		fprintf(stderr,
			"qdwin-bystander: outer placeholder buffer alloc failed\n");
		return -1;
	}
	wl_surface_attach(out->surface, buf, 0, 0);
	wl_surface_damage_buffer(out->surface, 0, 0, 64, 64);
	wl_surface_commit(out->surface);
	wl_display_roundtrip(out->display);
	fprintf(stderr,
		"qdwin-bystander: outer xdg_toplevel created "
		"(one-per-session, P10 Shape A; pixel mirror is P11)\n");
	return 0;
}

static void
outer_teardown(struct outer_state *out)
{
	if (!out) return;
	if (out->toplevel) xdg_toplevel_destroy(out->toplevel);
	if (out->xdg_surface) xdg_surface_destroy(out->xdg_surface);
	if (out->surface) wl_surface_destroy(out->surface);
	if (out->wm_base) xdg_wm_base_destroy(out->wm_base);
	if (out->shm) wl_shm_destroy(out->shm);
	if (out->compositor) wl_compositor_destroy(out->compositor);
	if (out->registry) wl_registry_destroy(out->registry);
	if (out->display) wl_display_disconnect(out->display);
	memset(out, 0, sizeof *out);
}

static volatile sig_atomic_t stop = 0;
static void on_int(int s) { (void)s; stop = 1; }

/* Parse an optional allow_input override token: NULL => -1 (use the global
 * --allow-input flag); literal "0"/"1" => 0/1; anything else => rejected (-1 +
 * a diagnostic), so a malformed command never silently sets an input policy. */
static int
parse_ai_override(const char *s)
{
	if (!s)
		return -1;
	if (strcmp(s, "0") == 0)
		return 0;
	if (strcmp(s, "1") == 0)
		return 1;
	fprintf(stderr, "qdwin-bystander: allow_input override must be 0|1, "
		"got '%s' (using --allow-input default)\n", s);
	return -1;
}

static void
process_command(struct app *a, char *line)
{
	while (*line == ' ' || *line == '\t') line++;
	if (*line == '\0' || *line == '#') return;

	char *cmd = strtok(line, " \t\r\n");
	char *arg = strtok(NULL, " \t\r\n");
	if (!cmd) return;

	uint32_t handle = 0;
	if (arg) handle = (uint32_t)strtoul(arg, NULL, 10);
	int has_handle = (arg != NULL);
	if (!has_handle && a->got_last) {
		handle = a->last_handle;
		has_handle = 1;
	}

	if (strcmp(cmd, "max") == 0 && has_handle) {
		fprintf(stderr, "qdwin-bystander: cmd max handle=%u\n", handle);
		qdwin_shell_v1_request_maximize(a->shell, handle, 1);
	} else if (strcmp(cmd, "restore") == 0 && has_handle) {
		fprintf(stderr, "qdwin-bystander: cmd restore handle=%u\n", handle);
		qdwin_shell_v1_request_maximize(a->shell, handle, 0);
	} else if (strcmp(cmd, "min") == 0 && has_handle) {
		fprintf(stderr, "qdwin-bystander: cmd min handle=%u\n", handle);
		qdwin_shell_v1_request_minimize(a->shell, handle);
	} else if (strcmp(cmd, "close") == 0 && has_handle) {
		fprintf(stderr, "qdwin-bystander: cmd close handle=%u\n", handle);
		qdwin_shell_v1_request_close(a->shell, handle);
	} else if (strcmp(cmd, "tile") == 0 && has_handle) {
		/* tile <handle> <left|right|none> — v25 half-screen tiling. */
		char *edgearg = strtok(NULL, " \t\r\n");
		uint32_t edge = 0; /* none */
		if (edgearg && strcmp(edgearg, "left") == 0) edge = 1;
		else if (edgearg && strcmp(edgearg, "right") == 0) edge = 2;
		fprintf(stderr, "qdwin-bystander: cmd tile handle=%u edge=%u\n",
			handle, edge);
		qdwin_shell_v1_request_tile(a->shell, handle, edge);
	} else if (strcmp(cmd, "fullscreen") == 0 && has_handle) {
		/* fullscreen <handle> [0|1] — v25 shell-driven fullscreen. */
		char *fsarg = strtok(NULL, " \t\r\n");
		uint32_t fs = fsarg ? (uint32_t)strtoul(fsarg, NULL, 10) : 1;
		fprintf(stderr, "qdwin-bystander: cmd fullscreen handle=%u fs=%u\n",
			handle, fs);
		qdwin_shell_v1_request_fullscreen(a->shell, handle, fs);
	} else if (strcmp(cmd, "wmpolicy") == 0) {
		/* wmpolicy <focus> <ffm_ms> <raise_click> <raise_hover>
		 *          <placement> <snap_en> <snap_dist> — v25 policy.
		 * `arg` already consumed the first field (focus). */
		uint32_t fp = arg ? (uint32_t)strtoul(arg, NULL, 10) : 0;
		char *t;
		uint32_t ffm = (t = strtok(NULL, " \t\r\n")) ? (uint32_t)strtoul(t, NULL, 10) : 0;
		uint32_t rc  = (t = strtok(NULL, " \t\r\n")) ? (uint32_t)strtoul(t, NULL, 10) : 0;
		uint32_t rh  = (t = strtok(NULL, " \t\r\n")) ? (uint32_t)strtoul(t, NULL, 10) : 0;
		uint32_t pl  = (t = strtok(NULL, " \t\r\n")) ? (uint32_t)strtoul(t, NULL, 10) : 2;
		uint32_t se  = (t = strtok(NULL, " \t\r\n")) ? (uint32_t)strtoul(t, NULL, 10) : 0;
		uint32_t sd  = (t = strtok(NULL, " \t\r\n")) ? (uint32_t)strtoul(t, NULL, 10) : 16;
		fprintf(stderr, "qdwin-bystander: cmd wmpolicy focus=%u ffm=%u "
			"rc=%u rh=%u place=%u snap=%u dist=%u\n",
			fp, ffm, rc, rh, pl, se, sd);
		qdwin_shell_v1_set_wm_policy(a->shell, fp, ffm, rc, rh, pl, se, sd);
	} else if (strcmp(cmd, "displaypower") == 0) {
		/* displaypower <0|1> — v26 DPMS all outputs off/on. `arg` is the
		 * on flag. */
		uint32_t on = arg ? (uint32_t)strtoul(arg, NULL, 10) : 1;
		fprintf(stderr, "qdwin-bystander: cmd displaypower on=%u\n", on);
		qdwin_shell_v1_set_display_power(a->shell, on);
	} else if (strcmp(cmd, "hotkey") == 0) {
		/* hotkey <id> <modifiers> <key> — register a hotkey (v19/v25).
		 * `arg` already holds the id. */
		uint32_t hid = arg ? (uint32_t)strtoul(arg, NULL, 10) : 0;
		char *t;
		uint32_t mods = (t = strtok(NULL, " \t\r\n")) ? (uint32_t)strtoul(t, NULL, 10) : 0;
		uint32_t key  = (t = strtok(NULL, " \t\r\n")) ? (uint32_t)strtoul(t, NULL, 10) : 0;
		fprintf(stderr, "qdwin-bystander: cmd hotkey id=%u mods=%u key=%u\n",
			hid, mods, key);
		qdwin_shell_v1_register_hotkey(a->shell, hid, mods, key);
	} else if (strcmp(cmd, "focus") == 0 && has_handle) {
		fprintf(stderr, "qdwin-bystander: cmd focus handle=%u\n", handle);
		qdwin_shell_v1_set_keyboard_focus(a->shell, "default", handle);
	} else if (strcmp(cmd, "raise") == 0 && has_handle) {
		fprintf(stderr, "qdwin-bystander: cmd raise handle=%u\n", handle);
		qdwin_shell_v1_request_raise(a->shell, handle);
	} else if (strcmp(cmd, "move") == 0 && has_handle) {
		/* move <handle> <x> <y> — v30 shell-owned set-position. The two
		 * coordinates follow the handle; both required. */
		char *xs = strtok(NULL, " \t\r\n");
		char *ys = strtok(NULL, " \t\r\n");
		if (!xs || !ys) {
			fprintf(stderr, "qdwin-bystander: move needs <handle> <x> <y>\n");
		} else {
			int32_t mx = (int32_t)strtol(xs, NULL, 10);
			int32_t my = (int32_t)strtol(ys, NULL, 10);
			fprintf(stderr, "qdwin-bystander: cmd move handle=%u x=%d y=%d\n",
				handle, mx, my);
			qdwin_shell_v1_request_set_position(a->shell, handle, mx, my);
		}
	} else if (strcmp(cmd, "subscribe") == 0 && has_handle) {
		/* subscribe <handle> [allow_input 0|1] — optional per-handle override.
		 * Only literal 0/1 accepted; anything else is rejected (no silent
		 * malformed input policy — this feeds the rung-1 allow_input proof). */
		char *ais = strtok(NULL, " \t\r\n");
		a->sub_ai_override = parse_ai_override(ais);
		fprintf(stderr, "qdwin-bystander: cmd subscribe handle=%u ai=%s\n",
			handle, ais ? ais : "default");
		do_subscribe(a, handle);
		a->sub_ai_override = -1;
	} else if (strcmp(cmd, "subscribelast") == 0 && a->got_last) {
		/* subscribelast [allow_input 0|1] — `arg` already holds the optional ai. */
		a->sub_ai_override = parse_ai_override(arg);
		fprintf(stderr, "qdwin-bystander: cmd subscribelast handle=%u ai=%s\n",
			a->last_handle, arg ? arg : "default");
		do_subscribe(a, a->last_handle);
		a->sub_ai_override = -1;
	} else if (strcmp(cmd, "list") == 0) {
		fprintf(stderr, "qdwin-bystander: tracked toplevels:");
		for (int i = 0; i < MAX_TOPS; i++)
			if (a->tops[i].active)
				fprintf(stderr, " %u", a->tops[i].handle);
		fprintf(stderr, "\n");
	} else {
		fprintf(stderr, "qdwin-bystander: unknown cmd '%s'\n", cmd);
	}
}

static void
usage(const char *argv0)
{
	fprintf(stderr,
		"usage: %s [--subscribe <handle>|last] [--peer-label <s>]\n"
		"          [--allow-input] [--ignore-torn-down]\n"
		"          [--inner-display <wayland-socket>]\n"
		"          [--forward-session] [--connect <wayland-socket>]\n"
		"          [--forward-all-toplevels]\n",
		argv0);
}

int main(int argc, char **argv)
{
	const char *inner_socket = NULL;
	g_app.sub_ai_override = -1;        /* default: use the --allow-input flag */
	for (int i = 1; i < argc; i++) {
		if (strcmp(argv[i], "--subscribe") == 0 && i + 1 < argc) {
			const char *v = argv[++i];
			g_app.pending.armed = 1;
			if (strcmp(v, "last") == 0 || strcmp(v, "first") == 0) {
				g_app.pending.wait_first = 1;
				g_app.pending.handle = 0;
			} else {
				g_app.pending.wait_first = 0;
				g_app.pending.handle = (uint32_t)strtoul(v, NULL, 10);
			}
		} else if (strcmp(argv[i], "--peer-label") == 0 && i + 1 < argc) {
			snprintf(g_app.peer_label, sizeof g_app.peer_label,
				 "%s", argv[++i]);
		} else if (strcmp(argv[i], "--allow-input") == 0) {
			g_app.allow_input = 1;          /* input-confinement gate */
		} else if (strcmp(argv[i], "--ignore-torn-down") == 0) {
			g_app.ignore_torn_down = 1;     /* lifecycle tombstone probe */
		} else if ((strcmp(argv[i], "--inner-display") == 0
			    || strcmp(argv[i], "--connect") == 0)
			   && i + 1 < argc) {
			/* §P10 Shape A: select the *inner* (guest qdwin)
			 * compositor socket. This connection is SEPARATE
			 * from the ambient $WAYLAND_DISPLAY — the outer
			 * compositor (host, via waypipe-server's intercept)
			 * lives at $WAYLAND_DISPLAY and we leave it alone. */
			inner_socket = argv[++i];
		} else if (strcmp(argv[i], "--forward-session") == 0
			   || strcmp(argv[i], "--forward-all-toplevels") == 0) {
			/* §P10 Shape A: enable the one-outer-toplevel-per-VM
			 * forwarding mode. */
			g_app.forward_session = 1;
		} else if (strcmp(argv[i], "--help") == 0
			   || strcmp(argv[i], "-h") == 0) {
			usage(argv[0]);
			return 0;
		} else {
			fprintf(stderr, "qdwin-bystander: unknown arg '%s'\n",
				argv[i]);
			usage(argv[0]);
			return 2;
		}
	}

	/* §P10: log the resolved inner-socket target on startup so the
	 * unit tests can grep for it without spinning up a real
	 * compositor. */
	if (inner_socket) {
		fprintf(stderr, "qdwin-bystander: --inner-display socket=\"%s\"\n",
			inner_socket);
		/* Back-compat: emit the old log line shape so the existing
		 * unit-test grep for `socket="…"` continues to fire. */
		fprintf(stderr, "qdwin-bystander: --connect socket=\"%s\"\n",
			inner_socket);
	}
	if (g_app.forward_session) {
		fprintf(stderr, "qdwin-bystander: --forward-session enabled\n");
		/* Back-compat: keep the old log spelling too. */
		fprintf(stderr, "qdwin-bystander: --forward-all-toplevels enabled\n");
	}

	const char *fifo_path = getenv("QDWIN_BYSTANDER_FIFO");
	char fifo_buf[256];
	if (!fifo_path) {
		/* The harness and every consumer expect the command FIFO in the
		 * session runtime dir ($XDG_RUNTIME_DIR/qdwin-cmd.fifo, e.g.
		 * /run/user/1000/qdwin-cmd.fifo). Default there so a bystander
		 * started without QDWIN_BYSTANDER_FIFO still lands where
		 * qdwin_apps_session_up() polls, instead of /tmp. */
		const char *xdg = getenv("XDG_RUNTIME_DIR");
		if (xdg && xdg[0]) {
			snprintf(fifo_buf, sizeof fifo_buf,
				 "%s/qdwin-cmd.fifo", xdg);
			fifo_path = fifo_buf;
		} else {
			fifo_path = FIFO_PATH_DEFAULT;
		}
	}
	unlink(fifo_path);
	if (mkfifo(fifo_path, 0600) != 0) {
		fprintf(stderr, "qdwin-bystander: mkfifo %s failed: %s\n",
			fifo_path, strerror(errno));
		return 1;
	}
	int fifo_fd = open(fifo_path, O_RDWR | O_NONBLOCK);
	if (fifo_fd < 0) {
		fprintf(stderr, "qdwin-bystander: open fifo failed\n");
		return 1;
	}

	/* §P10 Shape A: bring up the outer compositor FIRST (if
	 * --forward-session is on). We deliberately attempt the outer
	 * connect with the *ambient* $WAYLAND_DISPLAY untouched — that's
	 * waypipe-server's intercept when this binary is wrapped, or
	 * whatever the test harness pointed at otherwise. Failure here
	 * is non-fatal: the unit tests run with bogus WAYLAND_DISPLAY
	 * values and we want them to still see the inner log lines. */
	struct outer_state outer_storage;
	int outer_ok = 0;
	if (g_app.forward_session) {
		if (outer_setup_one_toplevel(&outer_storage) == 0) {
			g_app.outer = &outer_storage;
			outer_ok = 1;
		} else {
			g_app.outer = NULL;
		}
	}

	/* Connect to the *inner* compositor by an explicit path. If the
	 * caller passed --inner-display, that's the socket name (relative
	 * to $XDG_RUNTIME_DIR per libwayland's lookup rules); otherwise
	 * fall back to $WAYLAND_DISPLAY (legacy behaviour for the host-
	 * side tests that have a single compositor). */
	struct wl_display *display = NULL;
	if (inner_socket) {
		display = wl_display_connect(inner_socket);
	} else {
		display = wl_display_connect(NULL);
	}
	if (!display) {
		fprintf(stderr, "qdwin-bystander: wl_display_connect failed "
			"(inner socket=%s)\n",
			inner_socket ? inner_socket : "(NULL → $WAYLAND_DISPLAY)");
		if (outer_ok) outer_teardown(&outer_storage);
		close(fifo_fd);
		unlink(fifo_path);
		return 1;
	}
	struct wl_registry *reg = wl_display_get_registry(display);
	wl_registry_add_listener(reg, &registry_listener, &g_app);
	wl_display_roundtrip(display);
	if (!g_app.shell) {
		fprintf(stderr, "qdwin-bystander: qdwin_shell_v1 not advertised\n");
		if (outer_ok) outer_teardown(&outer_storage);
		wl_display_disconnect(display);
		close(fifo_fd);
		unlink(fifo_path);
		return 1;
	}
	qdwin_shell_v1_bind_as_shell(g_app.shell);
	wl_display_roundtrip(display);

	signal(SIGINT, on_int);
	signal(SIGTERM, on_int);
	signal(SIGPIPE, SIG_IGN);

	int wl_fd = wl_display_get_fd(display);
	int outer_fd = (outer_ok && g_app.outer) ?
		wl_display_get_fd(g_app.outer->display) : -1;
	char buf[1024];
	size_t buf_len = 0;

	while (!stop) {
		while (wl_display_prepare_read(display) != 0) {
			if (wl_display_dispatch_pending(display) == -1)
				goto out;
		}
		if (wl_display_flush(display) == -1) {
			wl_display_cancel_read(display);
			goto out;
		}
		if (outer_ok && g_app.outer) {
			wl_display_dispatch_pending(g_app.outer->display);
			if (wl_display_flush(g_app.outer->display) == -1) {
				/* outer died — keep the inner half running */
				outer_fd = -1;
				outer_ok = 0;
			}
		}
		struct pollfd fds[3] = {
			{ .fd = wl_fd, .events = POLLIN },
			{ .fd = fifo_fd, .events = POLLIN },
			{ .fd = outer_fd, .events = POLLIN },
		};
		int nfds = (outer_fd >= 0) ? 3 : 2;
		int pr = poll(fds, nfds, -1);
		if (pr < 0) {
			wl_display_cancel_read(display);
			if (errno == EINTR) continue;
			break;
		}
		if (fds[0].revents & POLLIN) {
			if (wl_display_read_events(display) == -1) goto out;
		} else {
			wl_display_cancel_read(display);
		}
		if (wl_display_dispatch_pending(display) == -1) goto out;

		if (nfds >= 3 && (fds[2].revents & POLLIN)) {
			if (wl_display_dispatch(g_app.outer->display) == -1) {
				outer_fd = -1;
				outer_ok = 0;
			}
		}

		if (fds[1].revents & POLLIN) {
			ssize_t n = read(fifo_fd, buf + buf_len,
					 sizeof(buf) - 1 - buf_len);
			if (n > 0) {
				buf_len += (size_t)n;
				buf[buf_len] = '\0';
				char *nl;
				while ((nl = strchr(buf, '\n')) != NULL) {
					*nl = '\0';
					process_command(&g_app, buf);
					wl_display_flush(display);
					size_t consumed = (size_t)(nl - buf) + 1;
					memmove(buf, buf + consumed, buf_len - consumed);
					buf_len -= consumed;
				}
				if (buf_len >= sizeof(buf) - 1)
					buf_len = 0;
			}
		}
	}
out:
	if (outer_ok) outer_teardown(&outer_storage);
	wl_display_disconnect(display);
	close(fifo_fd);
	unlink(fifo_path);
	return 0;
}
