#!/usr/bin/env python3
"""Behavioural test for the compositor-teardown drain.

The source invariant (test_qdwin_destroy_drain.py) pins the staging; this
pins what actually matters: after qdwin_destroy has run its drain and
free(qdwin), NOTHING that can still fire reaches into the freed struct.

Same technique as test_proxy_destroy_behaviour.py — splice the REAL
function bodies out of qdwin.c, compile them against reduced structs and
real libwayland lists/signals, heap-allocate the qdwin fixture, free it,
then fire the late paths under ASan:

  - a dsurface destroy reaching api.surface_removed (tl link severed →
    early return before qdwin is dereferenced),
  - a client disconnect reaching the shell/locker resource destructors
    (user_data NULLed → early return),
  - an inert stream resource destroy (s==NULL → early return),
  - wl_signal emits on seats/clients/surfaces whose listeners were
    registered by now-freed objects (listener removed at drain → emit
    touches no freed memory),
  - the synchronous default-grab focus callback an end_grab fires (the
    singleton was NULLed first → qdwin_proxy_pointer_track_focus(NULL)
    returns without touching the freed struct).

Cases:
  1. Toplevel drain, no dlsym resolution (unpatched libweston): the real
     qdwin_surface_removed teardown runs — dependents released, view
     destroyed, dsurface user_data severed, toplevel freed.
  2. Toplevel drain, dlsym resolves: the resolved destroy runs the
     api.surface_removed callback synchronously, same end state.
  3. Proxy + owner: the toplevel drain breaks BOTH ownership edges before
     freeing the proxy; the later nested-toplevel drain frees the owner
     without calling into freed memory.
  4. Owner destructor with the edge still live (the client-destroy shape):
     destroys the proxy and clears the back-edge.
  5. Listed stream: drain frees it and neutralizes the resource; a late
     resource destroy sees s==NULL and no-ops.
  6. Panel / notification / launcher: drains run the real destructors
     while qdwin is alive (view destroyed, listeners removed, launcher
     ends a role-0 overlay grab).
  7. Idle notification: drain removes its event-source timer.
  8. Fractional scale + primary seat + secctx client + layer surface:
     every listener registered on a longer-lived wl_signal is removed at
     drain, so a post-free emit cannot reach freed memory.
  9. ext-workspace / output-management: drain NULLs group/handle/head/
     mode back-pointers the client-owned resources still carry.
 10. Activation token: drain scrubs the pending-activation reference and
     severs the token resource's back-reference.
 11. Shell/locker resource destructors: NULL user_data → guarded return;
     live qdwin → the real unbind body still runs.
 12. Grab callbacks during teardown: end_grab fires the default focus
     handler SYNCHRONOUSLY mid-drain; with qdwin_singleton already NULL
     the handler still runs (pick_view) but caches nothing.
 13. Full pass: populate every family, run every drain, free(qdwin),
     fire every late path — zero touches of the freed struct under ASan.
"""

from pathlib import Path
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent


def fail(message):
    print(f"FAIL: {message}")
    return 1


def _load(name, mod_name):
    spec = importlib.util.spec_from_file_location(
        mod_name, Path(__file__).with_name(name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_strip_comments = _load(
    "test_idle_inhibit_visibility.py", "inv")._strip_comments


def extract(source, name):
    """Return the DEFINITION `name(args) {body}`, comments stripped.

    qdwin.c puts the return type on its own line, so a definition is the
    only place the name starts a line — a word-boundary match would grab
    a forward declaration or a call site instead.
    """
    code = _strip_comments(source)
    m = re.search(r"^" + re.escape(name) + r"\s*\(", code, re.MULTILINE)
    if not m:
        raise KeyError(name)
    begin = code.index("{", m.start())
    depth, end = 1, begin + 1
    while depth:
        depth += (code[end] == "{") - (code[end] == "}")
        end += 1
    return code[m.start():end]


PROLOGUE = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wayland-server-core.h>

#define weston_log(...) ((void)0)
#define QDWIN_SIDES 4
#define QDWIN_MAX_WORKSPACES 4

/* ---- reduced weston / wayland types -------------------------------- */

struct weston_coord_global { int placeholder; };
struct wl_display { int placeholder; };
struct wl_event_source { int removed; };
struct weston_desktop_surface { void *user_data; int destroyed; };
struct weston_surface { struct wl_signal destroy_signal; };
struct weston_view { int destroyed; struct weston_surface *surface; };
struct weston_curtain { int destroyed; };
struct weston_output { const char *name; struct wl_list link; };
struct weston_mode { int placeholder; };
struct weston_head { int placeholder; };
struct wl_client { struct wl_signal destroy_signal; };
struct wl_resource {
	void *user_data;
	void (*destructor)(struct wl_resource *);
	int destroyed;
};

struct weston_compositor {
	struct wl_list output_list;
	struct wl_display *wl_display;
	int kb_repeat_rate, kb_repeat_delay;
};

struct weston_pointer_grab;
struct weston_keyboard_grab;
struct weston_pointer_grab_interface {
	void (*focus)(struct weston_pointer_grab *grab);
};
struct weston_pointer_grab {
	const struct weston_pointer_grab_interface *interface;
	struct weston_pointer *pointer;
};
struct weston_keyboard_grab {
	void *interface;
	struct weston_keyboard *keyboard;
};
struct weston_pointer {
	struct weston_pointer_grab *grab;
	struct weston_pointer_grab default_grab;
	struct weston_view *focus;
	struct weston_seat *seat;
	struct weston_coord_global pos;
};
struct weston_keyboard_modifiers {
	uint32_t mods_depressed, mods_latched, mods_locked, group;
};
struct weston_keyboard {
	struct weston_keyboard_grab *grab;
	struct weston_keyboard_grab default_grab;
	struct weston_seat *seat;
	struct weston_keyboard_modifiers modifiers;
};
struct weston_seat {
	struct weston_compositor *compositor;
	struct wl_signal destroy_signal;
	struct wl_list link;
};

/* ---- reduced qdwin types -------------------------------------------- */

struct qdwin;
struct qdwin_toplevel;
struct qdwin_wm_policy { int placeholder; };
struct qdwin_pointer_config { int valid; };
struct qdwin_chrome { struct weston_view *view; };
struct qdwin_popup {
	struct wl_resource *resource;
	struct qdwin_toplevel *parent;
	struct weston_view *view;
	struct weston_surface *surface;
	struct wl_listener surface_destroy;
	struct weston_pointer_grab grab;
	int grab_active;
};
struct qdwin_nested_toplevel;
struct qdwin_toplevel {
	struct qdwin *qdwin;
	struct weston_desktop_surface *desktop_surface;
	struct weston_view *view;
	uint32_t handle;
	int is_nested_proxy;
	int proxy_destroying;
	struct qdwin_nested_toplevel *proxy_nested_owner;
	struct weston_curtain *proxy_curtain;
	char *proxy_app_id;
	char *proxy_title;
	char *proxy_secctx_engine, *proxy_secctx_app_id, *proxy_secctx_instance;
	char *proxy_remote_source_machine, *proxy_remote_trust_domain_id;
	char *proxy_remote_stream_id;
	struct wl_listener proxy_pixel_destroy_listener;
	struct weston_view *proxy_pixel_view;
	struct weston_surface *proxy_pixel_surface;
	int proxy_input_sink_fd;
	struct qdwin_popup *popup;
	struct qdwin_chrome chrome[QDWIN_SIDES];
	char *cached_title, *cached_app_id;
	struct wl_list link;
};
struct qdwin_nested_toplevel {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	char *pw_node, *input_sink, *app_id, *title;
	struct qdwin_toplevel *proxy_tl;
	struct wl_list link;
};
struct qdwin_view_stream {
	struct wl_resource *resource;
	struct qdwin *qdwin;
	struct qdwin_toplevel *tl;
	uint32_t toplevel_handle;
	int listed, server_state_released, torn_down_sent;
	struct weston_output *pw_output, *prev_output;
	struct weston_coord_global prev_pos;
	int pinned;
	pid_t forward_pid;
	int forward_pidfd;
	struct wl_event_source *forward_pidfd_source;
	uint32_t rdp_port;
	char access_token[33];
	char rdp_password[17];
	int input_claimed, allow_input;
	struct wl_resource *input_handle;
	struct wl_list link;
};
struct qdwin_panel {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct weston_view *view;
	struct wl_listener surface_destroy, surface_commit;
	struct wl_list link;
};
struct qdwin_notification {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct weston_view *view;
	struct wl_listener surface_destroy, surface_commit;
	struct wl_list link;
};
struct qdwin_launcher {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct weston_view *view;
	uint32_t kind;
	struct wl_listener surface_destroy, surface_commit;
	struct wl_list link;
};
struct qdwin_idle_notification {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct wl_event_source *timer;
	struct wl_list link;
};
struct qdwin_fractional_scale {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct wl_listener surface_destroy_listener, surface_commit_listener;
	struct wl_list link;
};
struct qdwin_activation_token {
	struct qdwin *qdwin;
	struct wl_resource *token_resource;
	char *token, *app_id;
	struct weston_surface *requesting_surface;
	struct wl_listener requesting_surface_destroy;
	struct wl_list link;
};
struct qdwin_activation_pending {
	struct qdwin *qdwin;
	struct qdwin_activation_token *token;
	struct wl_list link;
};
struct qdwin_primary_source { int placeholder; };
struct qdwin_primary_device {
	struct qdwin_primary_seat *pseat;
	struct wl_list link;
};
struct qdwin_primary_seat {
	struct qdwin *qdwin;
	struct weston_seat *seat;
	struct qdwin_primary_source *current_source;
	struct wl_list devices;
	struct wl_listener seat_destroy_listener;
	struct wl_list link;
};
struct qdwin_layer_surface {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	char *namespace;
	struct { int32_t exclusive_zone; } current;
	struct weston_view *view;
	int mapped;
	struct wl_listener commit_listener, surface_destroy_listener;
	struct wl_list popups;
	struct wl_list link;
};
struct qdwin_layer_popup {
	struct qdwin_layer_surface *parent;
	struct wl_resource *popup_resource;
	struct weston_surface *surface;
	struct weston_view *view;
	struct wl_listener surface_commit_listener, surface_destroy_listener;
	struct wl_listener popup_resource_destroy_listener;
	struct wl_list link;
	struct weston_pointer_grab grab;
	int grab_active;
	struct wl_listener seat_destroy_listener;
	struct weston_seat *grab_seat;
};
enum qdwin_ext_ws_pending_kind { QDWIN_EXT_WS_PENDING_ACTIVATE };
struct qdwin_ext_ws_pending_op {
	struct wl_list link;
	enum qdwin_ext_ws_pending_kind kind;
	uint32_t index;
};
struct qdwin_ext_ws_manager {
	struct wl_list link;
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct wl_resource *group;
	struct wl_resource *handles[QDWIN_MAX_WORKSPACES];
	struct wl_list pending;
	bool stopped;
};
struct qdwin_ext_ws_handle_ref {
	struct qdwin_ext_ws_manager *mgr;
	uint32_t index;
};
struct qdwin_om_mode {
	struct wl_list link;
	struct qdwin_om_head *head;
	struct wl_resource *resource;
	struct weston_mode *mode;
};
struct qdwin_om_head {
	struct wl_list link;
	struct qdwin_om_manager *mgr;
	struct wl_resource *resource;
	struct weston_head *head;
	struct weston_output *output;
	struct wl_list modes;
};
struct qdwin_om_manager {
	struct wl_list link;
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct wl_list heads;
	bool stopped, may_mutate;
};
struct qdwin_secctx_client {
	struct wl_client *client;
	void *secctx;
	char *sandbox_engine, *app_id, *instance_id;
	char *peer_exe, *peer_selinux_label;
	struct wl_listener client_destroy_listener;
	struct wl_list link;
};
struct qdwin {
	struct weston_compositor *compositor;
	struct wl_list toplevels;
	struct wl_list nested_toplevels;
	struct wl_list view_streams;
	struct wl_list panels;
	struct wl_list notifications;
	struct wl_list launchers;
	struct wl_list hotkeys;
	struct wl_list idle_notifications;
	struct wl_list fractional_scales;
	struct wl_list activation_tokens;
	struct wl_list activation_pending;
	struct wl_list primary_seats;
	struct wl_list layer_surfaces;
	struct wl_list ext_ws_managers;
	struct wl_list om_managers;
	struct wl_list secctx_clients;
	struct qdwin_toplevel *lock_toplevel;
	struct weston_view *lock_view;
	struct weston_surface *lock_surface;
	int lock_view_is_toplevel;
	int nested_mode, locked;
	struct wl_resource *shell_resource, *locker_resource, *lock_resource;
	int shell_bound;
	pid_t shell_pid;
	uint32_t shell_uid;
	uint64_t shell_starttime;
	pid_t locker_pid;
	uint32_t locker_uid;
	uint64_t locker_starttime;
	int move_grab_active;
	uint32_t move_grab_handle;
	struct weston_pointer_grab move_grab;
	int switcher_grab_active;
	struct weston_keyboard_grab switcher_grab;
	int overlay_grab_active;
	uint32_t overlay_grab_role;
	struct weston_keyboard_grab overlay_grab;
	int display_forced_off;
	struct qdwin_pointer_config pointer_config;
	struct qdwin_wm_policy wm_policy;
	int kb_repeat_overridden;
	int default_kb_repeat_rate, default_kb_repeat_delay;
	struct qdwin_toplevel *active_input_proxy;
};

static struct qdwin *qdwin_singleton;

/* ---- instrumented stubs --------------------------------------------- */

static int end_grab_calls, kbd_end_grab_calls, pick_calls, set_focus_calls;
static int view_destroy_calls, view_unmap_calls, unlink_view_calls;
static int curtain_destroy_calls, dismissed_calls, torn_down_calls;
static int toplevel_removed_calls, unpublish_calls, sink_focus_calls;
static int reap_calls, unpin_calls, seat_release_calls, disarm_calls;
static int panels_on_change_calls, hotkeys_purge_calls;
static int event_source_remove_calls;
static int clear_selection_calls, pending_clear_calls;
static int remote_valid_calls, remote_set_calls, ffm_cancel_calls;
static int wm_defaults_calls, power_calls, ptr_reset_calls;
static int resend_repeat_calls, offer_free_calls, demote_calls;
static int repaint_calls, serial_calls, send_modifiers_calls;
static int proxy_for_view_calls;
static struct weston_view *pick_result;
static struct qdwin_toplevel *proxy_for_view_result;
static int seat_destroyed_notify_calls, client_destroyed_notify_calls;

static struct wl_resource *res_new(void *ud,
				   void (*destructor)(struct wl_resource *))
{
	struct wl_resource *r = calloc(1, sizeof *r);
	assert(r);
	r->user_data = ud;
	r->destructor = destructor;
	return r;
}
static void qd_res_set_user_data(struct wl_resource *r, void *d)
{ if (r) r->user_data = d; }
static void *qd_res_get_user_data(struct wl_resource *r)
{ return r ? r->user_data : NULL; }
static void qd_res_destroy(struct wl_resource *r)
{
	if (!r || r->destroyed)
		return;
	r->destroyed = 1;
	if (r->destructor)
		r->destructor(r);
}
#define wl_resource_set_user_data qd_res_set_user_data
#define wl_resource_get_user_data qd_res_get_user_data
#define wl_resource_destroy qd_res_destroy

static int probe_event_source_remove(struct wl_event_source *s)
{ event_source_remove_calls++; s->removed = 1; return 0; }
#define wl_event_source_remove probe_event_source_remove

static uint32_t probe_next_serial(struct wl_display *d)
{ (void)d; serial_calls++; return 1; }
#define wl_display_next_serial probe_next_serial

static void *weston_desktop_surface_get_user_data(
	struct weston_desktop_surface *s)
{ return s->user_data; }
static void weston_desktop_surface_set_user_data(
	struct weston_desktop_surface *s, void *d)
{ s->user_data = d; }
static void weston_desktop_surface_unlink_view(struct weston_view *v)
{ (void)v; unlink_view_calls++; }
static void weston_view_destroy(struct weston_view *v)
{ if (v) v->destroyed = 1; view_destroy_calls++; }
static void weston_view_unmap(struct weston_view *v)
{ (void)v; view_unmap_calls++; }
static void weston_shell_utils_curtain_destroy(struct weston_curtain *c)
{ if (c) c->destroyed = 1; curtain_destroy_calls++; }
static void weston_pointer_set_focus(struct weston_pointer *p,
				     struct weston_view *v)
{ set_focus_calls++; p->focus = v; }
static struct weston_view *
weston_compositor_pick_view(struct weston_compositor *c,
			    struct weston_coord_global pos)
{ (void)c; (void)pos; pick_calls++; return pick_result; }
/* end_grab swaps to the default grab and runs its focus() SYNCHRONOUSLY —
 * this is what makes the stage-1 singleton NULL-out load-bearing. */
static void weston_pointer_end_grab(struct weston_pointer *p)
{
	end_grab_calls++;
	p->grab = &p->default_grab;
	if (p->default_grab.interface && p->default_grab.interface->focus)
		p->default_grab.interface->focus(&p->default_grab);
}
static void weston_keyboard_end_grab(struct weston_keyboard *k)
{
	kbd_end_grab_calls++;
	k->grab = &k->default_grab;
}
static void weston_keyboard_send_modifiers(struct weston_keyboard *k,
					   uint32_t s, uint32_t a,
					   uint32_t b, uint32_t c,
					   uint32_t d)
{ (void)k; (void)s; (void)a; (void)b; (void)c; (void)d;
  send_modifiers_calls++; }
static void weston_compositor_schedule_repaint(struct weston_compositor *c)
{ (void)c; repaint_calls++; }

static void qdwin_send_toplevel_removed(struct qdwin *q,
					struct qdwin_toplevel *tl)
{ (void)q; (void)tl; toplevel_removed_calls++; }
static void qdwin_nested_unpublish_toplevel(struct qdwin_toplevel *tl)
{ (void)tl; unpublish_calls++; }
static void qdwin_chrome_detach(struct qdwin_chrome *c) { c->view = NULL; }
static void qdwin_popup_v1_send_dismissed(struct wl_resource *r)
{ (void)r; dismissed_calls++; }
static void qdwin_panel_v1_send_dismissed(struct wl_resource *r)
{ (void)r; dismissed_calls++; }
static void qdwin_view_stream_v1_send_torn_down(struct wl_resource *r,
						const char *reason)
{ (void)r; (void)reason; torn_down_calls++; }
static void qdwin_view_stream_reap_forward(struct qdwin_view_stream *s)
{ (void)s; reap_calls++; }
static void qdwin_view_stream_unpin(struct qdwin_view_stream *s)
{ s->pinned = 0; unpin_calls++; }
static void qdwin_stream_seat_release(struct qdwin_view_stream *s)
{ (void)s; seat_release_calls++; }
static void qdwin_nested_input_sink_send_focus(int fd, int on)
{ (void)fd; (void)on; sink_focus_calls++; }
static struct qdwin_toplevel *
qdwin_proxy_for_view(struct qdwin *q, struct weston_view *v)
{ (void)q; (void)v; proxy_for_view_calls++; return proxy_for_view_result; }
static void qdwin_panels_on_output_change(struct qdwin *q)
{ (void)q; panels_on_change_calls++; }
static void qdwin_hotkeys_purge(struct qdwin *q)
{ (void)q; hotkeys_purge_calls++; }
static bool qdwin_remote_output_name_valid(const char *name)
{ (void)name; remote_valid_calls++; return false; }
static void qdwin_set_remote_output_input(struct qdwin *q, const char *n,
					  bool on)
{ (void)q; (void)n; (void)on; remote_set_calls++; }
static void qdwin_ffm_cancel(struct qdwin *q) { (void)q; ffm_cancel_calls++; }
static void qdwin_wm_policy_set_defaults(struct qdwin_wm_policy *p)
{ (void)p; wm_defaults_calls++; }
static void qdwin_set_all_outputs_power(struct qdwin *q, int on)
{ (void)q; (void)on; power_calls++; }
static void qdwin_pointer_config_reset_all(struct qdwin *q)
{ (void)q; ptr_reset_calls++; }
static void qdwin_resend_repeat_info(struct qdwin *q)
{ (void)q; resend_repeat_calls++; }
static void qdwin_data_offer_pending_free_all(struct qdwin *q)
{ (void)q; offer_free_calls++; }
static void qdwin_demote_lock_toplevel(struct qdwin *q, const char *cause)
{ (void)q; (void)cause; demote_calls++; }
static void qdwin_primary_seat_clear_selection(
	struct qdwin_primary_seat *ps, int cancel)
{ (void)cancel; ps->current_source = NULL; clear_selection_calls++; }
static void qdwin_ext_ws_pending_clear(struct qdwin_ext_ws_manager *m)
{
	struct qdwin_ext_ws_pending_op *op, *tmp;
	pending_clear_calls++;
	wl_list_for_each_safe(op, tmp, &m->pending, link) {
		wl_list_remove(&op->link);
		free(op);
	}
}

/* The dlsym-resolved dsurface destroy is internal API; the probe decides
 * per case whether resolution "succeeded". */
typedef void (*qdwin_desktop_surface_destroy_fn)(
	struct weston_desktop_surface *surface);
static qdwin_desktop_surface_destroy_fn probe_dsurf_destroy;
static qdwin_desktop_surface_destroy_fn
qdwin_desktop_surface_destroy_sym(void)
{ return probe_dsurf_destroy; }
"""

SPLICED = [
    "qdwin_view_stream_release_server_state",
    "qdwin_view_stream_terminate",
    "qdwin_toplevel_release_dependents",
    "qdwin_popup_teardown",
    "qdwin_move_grab_end_for",
    "qdwin_overlay_grab_end",
    "qdwin_surface_removed",
    "qdwin_nested_proxy_destroy",
    "qdwin_toplevels_destroy_all",
    "qdwin_nested_toplevel_resource_destroy",
    "qdwin_nested_toplevels_destroy_all",
    "qdwin_stream_resource_destroyed",
    "qdwin_view_streams_destroy_all",
    "qdwin_panel_drop",
    "qdwin_panel_resource_destroyed",
    "qdwin_panels_destroy_all",
    "qdwin_notification_resource_destroyed",
    "qdwin_notifications_destroy_all",
    "qdwin_launcher_resource_destroyed",
    "qdwin_launchers_destroy_all",
    "qdwin_idle_notification_resource_destroy",
    "qdwin_idle_notifications_destroy_all",
    "qdwin_fractional_scale_resource_destroy",
    "qdwin_fractional_scales_destroy_all",
    "qdwin_activation_pending_drop_token_refs",
    "qdwin_activation_token_free",
    "qdwin_activation_tokens_destroy_all",
    "qdwin_primary_seat_seat_destroyed",
    "qdwin_primary_seats_destroy_all",
    "qdwin_layer_popup_destroy",
    "qdwin_layer_surface_resource_destroy",
    "qdwin_layer_surfaces_destroy_all",
    "qdwin_ext_ws_manager_resource_destroy",
    "qdwin_ext_ws_managers_destroy_all",
    "qdwin_om_manager_resource_destroy",
    "qdwin_om_managers_destroy_all",
    "qdwin_secctx_client_on_destroy",
    "qdwin_secctx_clients_destroy_all",
    "qdwin_shell_resource_destroy",
    "qdwin_locker_resource_destroy",
    "qdwin_proxy_pointer_track_focus",
    "qdwin_proxy_default_grab_focus",
]

EPILOGUE = r"""
#define CHECK(cond, ...) do { \
	if (!(cond)) { printf("FAIL: " __VA_ARGS__); printf("\n"); return 1; } \
} while (0)

static int focus_handler_calls;
static void harness_default_grab_focus(struct weston_pointer_grab *grab)
{
	focus_handler_calls++;
	qdwin_proxy_default_grab_focus(grab);
}
static const struct weston_pointer_grab_interface default_grab_iface = {
	.focus = harness_default_grab_focus,
};
/* Wrappers let the fixture prove a listener DIDN'T fire: a stale link
 * post-drain would run the real body on freed memory (ASan) AND bump a
 * counter, so the assertion holds with or without ASan. */
static void harness_seat_destroyed(struct wl_listener *l, void *d)
{
	seat_destroyed_notify_calls++;
	qdwin_primary_seat_seat_destroyed(l, d);
}
static void harness_client_destroyed(struct wl_listener *l, void *d)
{
	client_destroyed_notify_calls++;
	qdwin_secctx_client_on_destroy(l, d);
}

static struct weston_compositor comp;
static struct weston_seat main_seat;
static struct weston_pointer main_pointer;
static struct weston_keyboard main_keyboard;

/* The dsurface destroy a resolved symbol would run: implementation
 * teardown fires api.surface_removed synchronously, then frees the
 * dsurface. */
static void fake_dsurf_destroy(struct weston_desktop_surface *ds);

static struct qdwin *q_new(void)
{
	struct qdwin *q = calloc(1, sizeof *q);
	assert(q);
	q->compositor = &comp;
	q->locker_uid = (uint32_t)-1;
	wl_list_init(&q->toplevels);
	wl_list_init(&q->nested_toplevels);
	wl_list_init(&q->view_streams);
	wl_list_init(&q->panels);
	wl_list_init(&q->notifications);
	wl_list_init(&q->launchers);
	wl_list_init(&q->hotkeys);
	wl_list_init(&q->idle_notifications);
	wl_list_init(&q->fractional_scales);
	wl_list_init(&q->activation_tokens);
	wl_list_init(&q->activation_pending);
	wl_list_init(&q->primary_seats);
	wl_list_init(&q->layer_surfaces);
	wl_list_init(&q->ext_ws_managers);
	wl_list_init(&q->om_managers);
	wl_list_init(&q->secctx_clients);
	return q;
}

static struct weston_desktop_surface *dsurf_new(void *ud)
{
	struct weston_desktop_surface *d = calloc(1, sizeof *d);
	assert(d);
	d->user_data = ud;
	return d;
}

static struct qdwin_toplevel *tl_new(struct qdwin *q, uint32_t handle)
{
	struct qdwin_toplevel *tl = calloc(1, sizeof *tl);
	assert(tl);
	tl->qdwin = q;
	tl->handle = handle;
	tl->view = calloc(1, sizeof *tl->view);
	tl->proxy_input_sink_fd = -1;
	wl_list_init(&tl->proxy_pixel_destroy_listener.link);
	wl_list_insert(&q->toplevels, &tl->link);
	return tl;
}

static int dsurf_destroy_calls;
static void fake_dsurf_destroy(struct weston_desktop_surface *ds)
{
	dsurf_destroy_calls++;
	qdwin_surface_removed(ds, ds->user_data ?
		((struct qdwin_toplevel *)ds->user_data)->qdwin : NULL);
	free(ds);
}

static void init_input(void)
{
	memset(&main_seat, 0, sizeof main_seat);
	memset(&main_pointer, 0, sizeof main_pointer);
	memset(&main_keyboard, 0, sizeof main_keyboard);
	wl_signal_init(&main_seat.destroy_signal);
	main_seat.compositor = &comp;
	main_pointer.seat = &main_seat;
	main_pointer.default_grab.interface = &default_grab_iface;
	main_pointer.default_grab.pointer = &main_pointer;
	main_pointer.grab = &main_pointer.default_grab;
	main_keyboard.seat = &main_seat;
	main_keyboard.grab = &main_keyboard.default_grab;
}

static void reset_counters(void)
{
	end_grab_calls = kbd_end_grab_calls = pick_calls = set_focus_calls = 0;
	view_destroy_calls = view_unmap_calls = unlink_view_calls = 0;
	curtain_destroy_calls = dismissed_calls = torn_down_calls = 0;
	toplevel_removed_calls = unpublish_calls = sink_focus_calls = 0;
	reap_calls = unpin_calls = seat_release_calls = 0;
	panels_on_change_calls = hotkeys_purge_calls = 0;
	event_source_remove_calls = 0;
	clear_selection_calls = pending_clear_calls = 0;
	remote_valid_calls = remote_set_calls = ffm_cancel_calls = 0;
	wm_defaults_calls = power_calls = ptr_reset_calls = 0;
	resend_repeat_calls = offer_free_calls = demote_calls = 0;
	repaint_calls = serial_calls = send_modifiers_calls = 0;
	proxy_for_view_calls = focus_handler_calls = dsurf_destroy_calls = 0;
	seat_destroyed_notify_calls = client_destroyed_notify_calls = 0;
	pick_result = NULL;
	proxy_for_view_result = NULL;
}

/* 1: fallback path — no dlsym symbol → real surface_removed teardown. */
static int case_toplevel_fallback(void)
{
	reset_counters();
	struct qdwin *q = q_new();
	struct qdwin_toplevel *tl = tl_new(q, 7);
	struct weston_desktop_surface *ds = dsurf_new(tl);
	struct weston_view *view = tl->view;
	tl->desktop_surface = ds;

	probe_dsurf_destroy = NULL;
	qdwin_toplevels_destroy_all(q);

	CHECK(wl_list_empty(&q->toplevels), "toplevel still listed");
	CHECK(ds->user_data == NULL, "dsurface->tl link not severed");
	CHECK(view->destroyed, "view not destroyed");
	CHECK(toplevel_removed_calls == 1, "no toplevel_removed sent");
	free(ds);   /* the client-owned dsurface outlives tl in fallback */
	free(view);
	free(q);
	return 0;
}

/* 2: resolved path — the internal destroy fires api.surface_removed. */
static int case_toplevel_resolved(void)
{
	reset_counters();
	struct qdwin *q = q_new();
	struct qdwin_toplevel *tl = tl_new(q, 8);
	struct weston_desktop_surface *ds = dsurf_new(tl);
	struct weston_view *view = tl->view;
	tl->desktop_surface = ds;
	tl->view->surface = NULL;

	probe_dsurf_destroy = fake_dsurf_destroy;
	qdwin_toplevels_destroy_all(q);
	probe_dsurf_destroy = NULL;

	CHECK(dsurf_destroy_calls == 1, "dsurface destroy never ran");
	CHECK(wl_list_empty(&q->toplevels), "toplevel still listed");
	CHECK(toplevel_removed_calls == 1, "no toplevel_removed sent");
	free(view);
	free(q);
	return 0;
}

/* 3+4: proxy/owner edges both directions. */
static int case_proxy_owner(void)
{
	reset_counters();
	struct qdwin *q = q_new();
	struct qdwin_nested_toplevel *nt = calloc(1, sizeof *nt);
	struct qdwin_toplevel *tl = tl_new(q, 9);
	struct weston_view *tlv = tl->view;
	assert(nt);
	nt->qdwin = q;
	nt->resource = res_new(nt, qdwin_nested_toplevel_resource_destroy);
	nt->proxy_tl = tl;
	tl->is_nested_proxy = 1;
	tl->proxy_nested_owner = nt;
	wl_list_insert(&q->nested_toplevels, &nt->link);

	/* Stage-3 order: toplevels first — the drain must break the edge. */
	struct wl_resource *nt_res = nt->resource;
	struct wl_resource *nt2_res;
	qdwin_toplevels_destroy_all(q);
	CHECK(nt->proxy_tl == NULL,
	      "owner still points at freed proxy");
	qdwin_nested_toplevels_destroy_all(q);
	CHECK(wl_list_empty(&q->nested_toplevels), "owner still listed");
	CHECK(nt_res->destroyed, "owner resource never destroyed");

	/* The client-destroy shape: owner dies first, destructor destroys
	 * the proxy itself. */
	reset_counters();
	struct qdwin *q2 = q_new();
	struct qdwin_nested_toplevel *nt2 = calloc(1, sizeof *nt2);
	struct qdwin_toplevel *tl2 = tl_new(q2, 10);
	struct weston_view *tl2v = tl2->view;
	nt2->qdwin = q2;
	nt2->resource = res_new(nt2, qdwin_nested_toplevel_resource_destroy);
	nt2->proxy_tl = tl2;
	tl2->is_nested_proxy = 1;
	tl2->proxy_nested_owner = nt2;
	wl_list_insert(&q2->nested_toplevels, &nt2->link);
	nt2_res = nt2->resource;
	qd_res_destroy(nt2_res);
	CHECK(wl_list_empty(&q2->nested_toplevels), "owner still listed");
	CHECK(wl_list_empty(&q2->toplevels),
	      "owner destructor did not destroy its proxy");
	free(nt2_res);
	free(nt_res);
	free(tlv);
	free(tl2v);
	free(q);
	free(q2);
	return 0;
}

/* 5: stream drain + late resource destroy. */
static int case_stream_late_destroy(void)
{
	reset_counters();
	struct qdwin *q = q_new();
	struct qdwin_view_stream *s = calloc(1, sizeof *s);
	assert(s);
	s->qdwin = q;
	s->listed = 1;
	s->pinned = 1;
	s->forward_pidfd = -1;
	memset(s->access_token, 'a', sizeof s->access_token - 1);
	s->resource = res_new(s, qdwin_stream_resource_destroyed);
	s->input_handle = res_new(s, NULL);
	struct wl_resource *s_res = s->resource;
	struct wl_resource *s_ih = s->input_handle;
	wl_list_insert(&q->view_streams, &s->link);

	qdwin_view_streams_destroy_all(q);
	CHECK(wl_list_empty(&q->view_streams), "stream still listed");
	CHECK(s_res->user_data == NULL,
	      "stream resource still armed");
	CHECK(s_ih->destroyed, "input handle not destroyed");

	/* Client disconnects after teardown: s==NULL → inert no-op. */
	qd_res_destroy(s_res);
	free(s_res);
	free(s_ih);
	free(q);
	return 0;
}

/* 6: panel / notification / launcher drains. */
static int case_shell_objects(void)
{
	reset_counters();
	struct qdwin *q = q_new();

	struct weston_surface *psurf = calloc(1, sizeof *psurf);
	struct qdwin_panel *p = calloc(1, sizeof *p);
	wl_signal_init(&psurf->destroy_signal);
	p->qdwin = q;
	p->resource = res_new(p, qdwin_panel_resource_destroyed);
	p->surface = psurf;
	p->view = calloc(1, sizeof *p->view);
	wl_list_init(&p->surface_destroy.link);
	wl_list_init(&p->surface_commit.link);
	wl_list_insert(&psurf->destroy_signal.listener_list,
		       &p->surface_destroy.link);
	p->surface_destroy.notify = (void *)0xdead;
	wl_list_insert(&q->panels, &p->link);

	struct qdwin_notification *n = calloc(1, sizeof *n);
	n->qdwin = q;
	n->resource = res_new(n, qdwin_notification_resource_destroyed);
	n->view = calloc(1, sizeof *n->view);
	wl_list_init(&n->surface_destroy.link);
	wl_list_init(&n->surface_commit.link);
	wl_list_insert(&q->notifications, &n->link);

	struct qdwin_launcher *ln = calloc(1, sizeof *ln);
	ln->qdwin = q;
	ln->kind = 0;
	ln->resource = res_new(ln, qdwin_launcher_resource_destroyed);
	ln->view = calloc(1, sizeof *ln->view);
	wl_list_init(&ln->surface_destroy.link);
	wl_list_init(&ln->surface_commit.link);
	wl_list_insert(&q->launchers, &ln->link);
	q->overlay_grab_active = 1;
	q->overlay_grab_role = 0;
	q->overlay_grab.keyboard = &main_keyboard;

	struct weston_view *pv = p->view, *nv = n->view, *lv = ln->view;
	struct wl_resource *p_res = p->resource, *n_res = n->resource;
	struct wl_resource *ln_res = ln->resource;
	qdwin_panels_destroy_all(q);
	qdwin_notifications_destroy_all(q);
	qdwin_launchers_destroy_all(q);

	CHECK(wl_list_empty(&q->panels), "panel still listed");
	CHECK(wl_list_empty(&q->notifications), "notification still listed");
	CHECK(wl_list_empty(&q->launchers), "launcher still listed");
	CHECK(pv->destroyed, "panel view not destroyed");
	CHECK(nv->destroyed, "notification view not destroyed");
	CHECK(lv->destroyed, "launcher view not destroyed");
	CHECK(kbd_end_grab_calls == 1 && !q->overlay_grab_active,
	      "launcher drain did not end the role-0 overlay grab");
	CHECK(panels_on_change_calls >= 1,
	      "panel drain did not recompute the work area");

	/* The surface the panel listened on outlives the drained panel. */
	wl_signal_emit(&psurf->destroy_signal, NULL);
	free(p_res); free(n_res); free(ln_res);
	free(pv); free(nv); free(lv);
	free(psurf);
	free(q);
	return 0;
}

/* 7: idle notifications own event-source timers. */
static int case_idle_notifications(void)
{
	reset_counters();
	struct qdwin *q = q_new();
	struct qdwin_idle_notification *n = calloc(1, sizeof *n);
	n->qdwin = q;
	n->timer = calloc(1, sizeof *n->timer);
	n->resource = res_new(n, qdwin_idle_notification_resource_destroy);
	struct wl_event_source *n_timer = n->timer;
	struct wl_resource *n_res = n->resource;
	wl_list_insert(&q->idle_notifications, &n->link);

	qdwin_idle_notifications_destroy_all(q);
	CHECK(wl_list_empty(&q->idle_notifications),
	      "idle notification still listed");
	CHECK(n_timer->removed && event_source_remove_calls == 1,
	      "notification timer never removed");
	free(n_res);
	free(n_timer);
	free(q);
	return 0;
}

/* 8: listener-armed families — every wl_signal listener must be gone
 * before free, so a later emit touches no freed memory. */
static int case_listener_families(void)
{
	reset_counters();
	struct qdwin *q = q_new();

	struct weston_surface *fsurf = calloc(1, sizeof *fsurf);
	wl_signal_init(&fsurf->destroy_signal);
	struct qdwin_fractional_scale *fs = calloc(1, sizeof *fs);
	fs->qdwin = q;
	fs->surface = fsurf;
	fs->resource = res_new(fs, qdwin_fractional_scale_resource_destroy);
	wl_list_init(&fs->surface_commit_listener.link);
	wl_list_insert(&fsurf->destroy_signal.listener_list,
		       &fs->surface_destroy_listener.link);
	fs->surface_destroy_listener.notify = (void *)0xdead;
	wl_list_insert(&q->fractional_scales, &fs->link);

	struct qdwin_primary_seat *ps = calloc(1, sizeof *ps);
	struct qdwin_primary_device *dev = calloc(1, sizeof *dev);
	ps->qdwin = q;
	ps->seat = &main_seat;
	ps->current_source = calloc(1, sizeof *ps->current_source);
	dev->pseat = ps;
	wl_list_init(&ps->devices);
	wl_list_insert(&ps->devices, &dev->link);
	wl_list_insert(&main_seat.destroy_signal.listener_list,
		       &ps->seat_destroy_listener.link);
	ps->seat_destroy_listener.notify = harness_seat_destroyed;
	wl_list_insert(&q->primary_seats, &ps->link);

	struct wl_client *client = calloc(1, sizeof *client);
	wl_signal_init(&client->destroy_signal);
	struct qdwin_secctx_client *sc = calloc(1, sizeof *sc);
	sc->client = client;
	sc->app_id = strdup("app");
	wl_list_insert(&client->destroy_signal.listener_list,
		       &sc->client_destroy_listener.link);
	sc->client_destroy_listener.notify = harness_client_destroyed;
	wl_list_insert(&q->secctx_clients, &sc->link);

	struct weston_surface *lsurf = calloc(1, sizeof *lsurf);
	wl_signal_init(&lsurf->destroy_signal);
	struct qdwin_layer_surface *ls = calloc(1, sizeof *ls);
	ls->qdwin = q;
	ls->namespace = strdup("panel");
	ls->mapped = 1;
	ls->current.exclusive_zone = 32;
	ls->surface = lsurf;
	ls->view = calloc(1, sizeof *ls->view);
	ls->resource = res_new(ls, qdwin_layer_surface_resource_destroy);
	wl_list_init(&ls->commit_listener.link);
	wl_list_insert(&lsurf->destroy_signal.listener_list,
		       &ls->surface_destroy_listener.link);
	ls->surface_destroy_listener.notify = (void *)0xdead;
	wl_list_init(&ls->popups);
	struct qdwin_layer_popup *lp = calloc(1, sizeof *lp);
	lp->parent = ls;
	lp->view = calloc(1, sizeof *lp->view);
	lp->grab_active = 1;
	lp->grab_seat = &main_seat;
	lp->grab.pointer = &main_pointer;
	main_pointer.grab = &lp->grab;
	wl_list_init(&lp->surface_commit_listener.link);
	wl_list_init(&lp->surface_destroy_listener.link);
	wl_list_init(&lp->popup_resource_destroy_listener.link);
	wl_list_init(&lp->seat_destroy_listener.link);
	wl_list_insert(&ls->popups, &lp->link);
	wl_list_insert(&q->layer_surfaces, &ls->link);

	struct wl_resource *fs_res = fs->resource;
	struct wl_resource *ls_res = ls->resource;
	struct weston_view *lpv = lp->view;
	struct weston_view *lsv = ls->view;
	struct qdwin_primary_source *psrc = ps->current_source;
	qdwin_fractional_scales_destroy_all(q);
	qdwin_primary_seats_destroy_all(q);
	qdwin_secctx_clients_destroy_all(q);
	qdwin_layer_surfaces_destroy_all(q);

	CHECK(wl_list_empty(&q->fractional_scales), "fs still listed");
	CHECK(wl_list_empty(&q->primary_seats), "pseat still listed");
	CHECK(wl_list_empty(&q->secctx_clients), "secctx still listed");
	CHECK(wl_list_empty(&q->layer_surfaces), "layer surface still listed");
	CHECK(dev->pseat == NULL, "primary device still armed");
	CHECK(clear_selection_calls == 1, "held selection not cancelled");
	CHECK(end_grab_calls >= 1,
	      "layer popup grab never ended");
	CHECK(panels_on_change_calls >= 1,
	      "exclusive-zone release did not recompute work area");

	/* Post-drain emits must touch no freed listener memory — under ASan
	 * any stale link aborts; the counters prove listeners are quiet. */
	wl_signal_emit(&fsurf->destroy_signal, NULL);
	wl_signal_emit(&main_seat.destroy_signal, NULL);
	wl_signal_emit(&client->destroy_signal, NULL);
	wl_signal_emit(&lsurf->destroy_signal, NULL);
	CHECK(seat_destroyed_notify_calls == 0 &&
	      client_destroyed_notify_calls == 0,
	      "a removed listener still fired");

	free(psrc);
	free(fs_res); free(fsurf);
	free(ls_res); free(lpv); free(lsv); free(lsurf);
	free(client); free(dev); free(q);
	return 0;
}

/* 9: manager back-pointer families. */
static int case_managers(void)
{
	reset_counters();
	struct qdwin *q = q_new();

	struct qdwin_ext_ws_manager *m = calloc(1, sizeof *m);
	m->qdwin = q;
	m->resource = res_new(m, qdwin_ext_ws_manager_resource_destroy);
	m->group = res_new(m, NULL);
	wl_list_init(&m->pending);
	struct qdwin_ext_ws_handle_ref *ref =
		calloc(1, sizeof *ref);
	ref->mgr = m;
	m->handles[0] = res_new(ref, NULL);
	wl_list_insert(&q->ext_ws_managers, &m->link);

	struct qdwin_om_manager *om = calloc(1, sizeof *om);
	om->qdwin = q;
	om->resource = res_new(om, qdwin_om_manager_resource_destroy);
	wl_list_init(&om->heads);
	struct qdwin_om_head *omh = calloc(1, sizeof *omh);
	omh->mgr = om;
	omh->head = calloc(1, sizeof *omh->head);
	omh->output = calloc(1, sizeof *omh->output);
	wl_list_init(&omh->modes);
	struct qdwin_om_mode *omm = calloc(1, sizeof *omm);
	omm->head = omh;
	wl_list_insert(&omh->modes, &omm->link);
	wl_list_insert(&om->heads, &omh->link);
	wl_list_insert(&q->om_managers, &om->link);

	struct wl_resource *m_res = m->resource;
	struct wl_resource *m_group = m->group;
	struct wl_resource *m_h0 = m->handles[0];
	struct wl_resource *om_res = om->resource;
	struct weston_head *omh_head = omh->head;
	struct weston_output *omh_output = omh->output;
	qdwin_ext_ws_managers_destroy_all(q);
	qdwin_om_managers_destroy_all(q);

	CHECK(wl_list_empty(&q->ext_ws_managers), "ext_ws mgr still listed");
	CHECK(wl_list_empty(&q->om_managers), "om mgr still listed");
	CHECK(m_group->user_data == NULL, "group back-ref still armed");
	CHECK(ref->mgr == NULL, "handle ref still armed");
	CHECK(omh->mgr == NULL && omh->head == NULL && omh->output == NULL,
	      "om head back-refs still armed");
	CHECK(omm->head == NULL, "om mode back-ref still armed");

	free(m_res); free(m_group); free(m_h0); free(ref);
	free(om_res); free(omm); free(omh); free(omh_head); free(omh_output);
	free(q);
	return 0;
}

/* 10: activation tokens scrub pending refs + sever resource back-ref. */
static int case_activation_tokens(void)
{
	reset_counters();
	struct qdwin *q = q_new();
	struct qdwin_activation_token *t = calloc(1, sizeof *t);
	t->qdwin = q;
	t->token = strdup("tok");
	t->app_id = strdup("app");
	t->token_resource = res_new(t, NULL);
	struct weston_surface *req = calloc(1, sizeof *req);
	wl_signal_init(&req->destroy_signal);
	t->requesting_surface = req;
	wl_list_insert(&req->destroy_signal.listener_list,
		       &t->requesting_surface_destroy.link);
	t->requesting_surface_destroy.notify = (void *)0xdead;
	struct qdwin_activation_pending *ap = calloc(1, sizeof *ap);
	ap->qdwin = q;
	ap->token = t;
	wl_list_insert(&q->activation_pending, &ap->link);
	wl_list_insert(&q->activation_tokens, &t->link);

	struct wl_resource *tres = t->token_resource;
	qdwin_activation_tokens_destroy_all(q);

	CHECK(wl_list_empty(&q->activation_tokens), "token still listed");
	CHECK(ap->token == NULL, "pending ref still armed");
	CHECK(tres->user_data == NULL,
	      "token resource back-ref still armed");
	wl_signal_emit(&req->destroy_signal, NULL);
	free(tres); free(ap); free(req); free(q);
	return 0;
}

/* 11: shell/locker destructors — NULL-guarded and normal paths. */
static int case_binding_destructors(void)
{
	reset_counters();
	struct qdwin *q = q_new();
	struct wl_resource *sr = res_new(NULL, qdwin_shell_resource_destroy);
	struct wl_resource *lr = res_new(NULL, qdwin_locker_resource_destroy);
	int before = hotkeys_purge_calls + demote_calls + repaint_calls;
	qdwin_shell_resource_destroy(sr);
	qdwin_locker_resource_destroy(lr);
	CHECK(hotkeys_purge_calls + demote_calls + repaint_calls == before,
	      "NULL user_data destructor ran the unbind body");

	/* Normal paths still work with a live qdwin. */
	struct wl_resource *shr = res_new(q, qdwin_shell_resource_destroy);
	q->shell_resource = shr;
	q->shell_bound = 1;
	q->move_grab_active = 1;
	q->move_grab.pointer = &main_pointer;
	q->pointer_config.valid = 1;
	q->kb_repeat_overridden = 1;
	q->default_kb_repeat_rate = 40;
	q->default_kb_repeat_delay = 600;
	qd_res_destroy(shr);
	CHECK(q->shell_resource == NULL && q->shell_bound == 0,
	      "shell unbind did not clear the binding");
	CHECK(hotkeys_purge_calls == 1 && ffm_cancel_calls == 1 &&
	      wm_defaults_calls == 1 && ptr_reset_calls == 1 &&
	      resend_repeat_calls == 1 && offer_free_calls == 1,
	      "shell unbind skipped teardown steps");
	CHECK(comp.kb_repeat_rate == 40 && !q->kb_repeat_overridden,
	      "key repeat not restored");

	struct wl_resource *lkr = res_new(q, qdwin_locker_resource_destroy);
	q->locker_resource = lkr;
	q->locked = 1;
	q->overlay_grab_active = 1;
	q->overlay_grab_role = 2;
	q->overlay_grab.keyboard = &main_keyboard;
	qd_res_destroy(lkr);
	CHECK(q->locker_resource == NULL && q->locker_pid == 0,
	      "locker unbind did not clear identity");
	CHECK(demote_calls == 1 && repaint_calls == 1,
	      "locker unbind skipped demote / fail-secure repaint");
	CHECK(kbd_end_grab_calls >= 1 && !q->overlay_grab_active,
	      "locker unbind left the overlay grab installed");
	/* The role-2 end_grab resyncs modifiers only when NOT locked. */
	CHECK(send_modifiers_calls == 0,
	      "modifier resync leaked while still locked");

	free(sr); free(lr); free(shr); free(lkr);
	free(q);
	return 0;
}

/* 12: grab-end focus callbacks during a drain are suppressed by the
 * stage-1 singleton NULL-out — but the handler itself still runs. */
static int case_grab_suppression(void)
{
	reset_counters();
	struct qdwin *q = q_new();
	qdwin_singleton = q;

	struct weston_pointer_grab foreign = { .pointer = &main_pointer };
	main_pointer.grab = &foreign;

	/* Stage 1 severs reach-back before anything ends a grab. */
	qdwin_singleton = NULL;
	struct qdwin_toplevel *tl = tl_new(q, 11);
	struct weston_view *tlv = tl->view;
	tl->popup = calloc(1, sizeof *tl->popup);
	tl->popup->parent = tl;
	tl->popup->grab_active = 1;
	tl->popup->grab.pointer = &main_pointer;
	tl->popup->resource = res_new(tl->popup, NULL);
	struct wl_resource *popup_res = tl->popup->resource;
	probe_dsurf_destroy = NULL;
	struct weston_desktop_surface *ds = dsurf_new(tl);
	tl->desktop_surface = ds;

	qdwin_toplevels_destroy_all(q);

	CHECK(end_grab_calls == 1, "popup grab never ended");
	CHECK(focus_handler_calls == 1,
	      "end_grab did not synchronously fire the focus handler");
	CHECK(pick_calls == 1, "suppressed focus handler did not pick");
	CHECK(q->active_input_proxy == NULL,
	      "focus handler cached a proxy through the NULL singleton");

	free(ds);
	free(tlv);
	free(popup_res);
	free(q);
	return 0;
}

/* 13: full pass — populate every family, drain, free, fire late paths. */
static int case_full_pass(void)
{
	reset_counters();
	struct qdwin *q = q_new();
	qdwin_singleton = q;

	struct qdwin_toplevel *tl = tl_new(q, 12);
	struct weston_desktop_surface *ds = dsurf_new(tl);
	tl->desktop_surface = ds;
	struct qdwin_nested_toplevel *nt = calloc(1, sizeof *nt);
	struct qdwin_toplevel *pxy = tl_new(q, 13);
	struct weston_view *tlv = tl->view, *pxyv = pxy->view;
	nt->qdwin = q;
	nt->resource = res_new(nt, qdwin_nested_toplevel_resource_destroy);
	nt->proxy_tl = pxy;
	pxy->is_nested_proxy = 1;
	pxy->proxy_nested_owner = nt;
	wl_list_insert(&q->nested_toplevels, &nt->link);

	struct qdwin_view_stream *s = calloc(1, sizeof *s);
	s->qdwin = q;
	s->listed = 1;
	s->tl = tl;
	s->forward_pidfd = -1;
	s->resource = res_new(s, qdwin_stream_resource_destroyed);
	wl_list_insert(&q->view_streams, &s->link);

	struct qdwin_idle_notification *n = calloc(1, sizeof *n);
	n->qdwin = q;
	n->resource = res_new(n, qdwin_idle_notification_resource_destroy);
	wl_list_insert(&q->idle_notifications, &n->link);

	struct wl_client *client = calloc(1, sizeof *client);
	wl_signal_init(&client->destroy_signal);
	struct qdwin_secctx_client *sc = calloc(1, sizeof *sc);
	sc->client = client;
	wl_list_insert(&client->destroy_signal.listener_list,
		       &sc->client_destroy_listener.link);
	sc->client_destroy_listener.notify = harness_client_destroyed;
	wl_list_insert(&q->secctx_clients, &sc->link);

	struct qdwin_primary_seat *ps = calloc(1, sizeof *ps);
	ps->qdwin = q;
	ps->seat = &main_seat;
	wl_list_init(&ps->devices);
	wl_list_insert(&main_seat.destroy_signal.listener_list,
		       &ps->seat_destroy_listener.link);
	ps->seat_destroy_listener.notify = harness_seat_destroyed;
	wl_list_insert(&q->primary_seats, &ps->link);

	struct wl_resource *shell_res = res_new(q, qdwin_shell_resource_destroy);
	struct wl_resource *locker_res = res_new(q, qdwin_locker_resource_destroy);
	q->shell_resource = shell_res;
	q->locker_resource = locker_res;

	struct wl_resource *s_res = s->resource;
	struct wl_resource *n_res = n->resource;
	struct wl_resource *nt_res = nt->resource;

	/* Stage 1 + stage 3 + stage 4, in production order. */
	qdwin_singleton = NULL;
	qdwin_toplevels_destroy_all(q);
	qdwin_nested_toplevels_destroy_all(q);
	qdwin_view_streams_destroy_all(q);
	qdwin_panels_destroy_all(q);
	qdwin_notifications_destroy_all(q);
	qdwin_launchers_destroy_all(q);
	qdwin_idle_notifications_destroy_all(q);
	qdwin_fractional_scales_destroy_all(q);
	qdwin_activation_tokens_destroy_all(q);
	qdwin_primary_seats_destroy_all(q);
	qdwin_layer_surfaces_destroy_all(q);
	qdwin_ext_ws_managers_destroy_all(q);
	qdwin_om_managers_destroy_all(q);
	qdwin_secctx_clients_destroy_all(q);
	wl_resource_set_user_data(shell_res, NULL);
	wl_resource_set_user_data(locker_res, NULL);
	free(q);

	/* Everything that can still fire after free(qdwin): under ASan
	 * any deref of the freed struct aborts the probe. */
	struct weston_desktop_surface *late = dsurf_new(NULL);
	qdwin_surface_removed(late, q);          /* !tl → return */
	free(late);
	qdwin_shell_resource_destroy(shell_res); /* !qdwin → return */
	qdwin_locker_resource_destroy(locker_res);
	qd_res_destroy(s_res);                   /* !s → return */
	wl_signal_emit(&main_seat.destroy_signal, NULL);
	wl_signal_emit(&client->destroy_signal, NULL);
	main_pointer.grab = &main_pointer.default_grab;
	harness_default_grab_focus(&main_pointer.default_grab);
	CHECK(focus_handler_calls >= 1,
	      "post-free focus handler did not run");

	free(shell_res); free(locker_res); free(s_res);
	free(n_res); free(nt_res); free(client);
	free(ds); free(tlv); free(pxyv);
	return 0;
}

int main(void)
{
	memset(&comp, 0, sizeof comp);
	wl_list_init(&comp.output_list);
	init_input();

	int r;
	if ((r = case_toplevel_fallback())) return r;
	if ((r = case_toplevel_resolved())) return r;
	if ((r = case_proxy_owner())) return r;
	if ((r = case_stream_late_destroy())) return r;
	if ((r = case_shell_objects())) return r;
	if ((r = case_idle_notifications())) return r;
	if ((r = case_listener_families())) return r;
	if ((r = case_managers())) return r;
	if ((r = case_activation_tokens())) return r;
	if ((r = case_binding_destructors())) return r;
	if ((r = case_grab_suppression())) return r;
	if ((r = case_full_pass())) return r;
	printf("destroy-drain behaviour: 12 cases OK\n");
	return 0;
}
"""


def main():
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        print("no C compiler; skipping")
        return 77
    if subprocess.run(["pkg-config", "--exists", "wayland-server"]).returncode:
        print("wayland-server not available; skipping")
        return 77

    qdwin_c = (ROOT / "qdwin" / "qdwin.c").read_text(encoding="utf-8")

    parts = [PROLOGUE]
    # Forward declarations first — the spliced order doesn't match call
    # order (e.g. toplevels_destroy_all calls nested_proxy_destroy).
    for name in SPLICED:
        body = extract(qdwin_c, name)
        sig = body[:body.index("{")].strip()
        parts.append(f"static void {sig};")
    for name in SPLICED:
        parts.append("static void " + extract(qdwin_c, name))
    parts.append(EPILOGUE)

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "probe.c"
        exe = Path(td) / "probe"
        src.write_text("\n".join(parts), encoding="utf-8")
        cflags = subprocess.run(
            ["pkg-config", "--cflags", "--libs", "wayland-server"],
            capture_output=True, text=True, check=True).stdout.split()
        cmd = [cc, str(src), "-o", str(exe),
               "-fsanitize=address", "-g"] + cflags
        build = subprocess.run(cmd, capture_output=True, text=True)
        if build.returncode:
            # ASan may not be available — retry without it; the explicit
            # counter assertions still pin the contract.
            build = subprocess.run(
                [cc, str(src), "-o", str(exe)] + cflags,
                capture_output=True, text=True)
        if build.returncode:
            print(build.stderr)
            return fail("probe did not compile")
        run = subprocess.run([str(exe)], capture_output=True, text=True)
        sys.stdout.write(run.stdout)
        sys.stdout.write(run.stderr)
        return 0 if run.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
