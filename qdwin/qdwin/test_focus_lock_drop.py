#!/usr/bin/env python3
"""Behavioural test: set_keyboard_focus / set_keyboard_focus_v2 while locked.

Since the D6 drop fix, both handlers refuse a locked request with a logged,
non-fatal DROP (weston_log + return), never wl_resource_post_error. The
request is reactive, best-effort intent — injectFocus is emitted by the
shell and by test drivers without checking lock state — so a fatal
protocol error used to unbind the shell mid-burst (qdlocker's 300 s idle
threshold inside a VM test window; phase7-tier3s-app.bats,
bats-20261009T192702Z-822639), after which even the legitimate post-unlock
focus restore could not arrive.

The splice-compile technique is the same as
test_idle_notify_inhibit_release.py: the real handler bodies are extracted
from qdwin.c, compiled against real libwayland lists, and asserted on — a
mutation to the production gate (e.g. a dropped `return`, or a restored
post_error) fails here, not just in review. Source shape is separately
pinned by test_popup_grab_hardening.py's D6_DROP_HANDLERS entry.

Cases:
  1. locked v1: no protocol error, one log line, ZERO side effects (no
     serial bump, selection clear, keyboard-focus call, activate, or
     seat_focus_changed emit) — the drop returns before the mutation path.
  2. locked v2: same, plus the seat tracker is never consulted or written
     (no last_target_silo update while locked).
  3. unlocked v2 same-silo: selection is NOT cleared (cross_silo stays 0)
     but the focus IS applied and emitted, and last_target_silo updates.
  4. unlocked v2 cross-silo: selection cleared, focus applied, tracker
     re-written to the new silo.
  5. unlocked v1 after a locked drop on the same fixture: the focus
     still lands — the drop leaves no wedged state and no deferred intent.
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


def _load_stripper():
    spec = importlib.util.spec_from_file_location(
        "inv", Path(__file__).with_name("test_idle_inhibit_visibility.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._strip_comments


_strip_comments = _load_stripper()


def extract(source, name):
    """Return the full definition of `name` with comments stripped."""
    code = _strip_comments(source)
    m = re.search(r"\b" + re.escape(name) + r"\s*\(", code)
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
#include <wayland-server-core.h>

#define WESTON_ACTIVATE_FLAG_CONFIGURE 1

/* Structs mirror the real members the spliced bodies touch. */
struct weston_compositor {
	struct wl_list seat_list;
	struct wl_display *wl_display;
};
struct weston_seat {
	char *seat_name;
	struct wl_list link;
};
struct weston_keyboard { int placeholder; };
struct weston_surface { int placeholder; };
struct weston_view { struct weston_surface *surface; };
struct qdwin_toplevel {
	uint32_t handle;
	int nested_proxy_pending_decision;
	struct weston_view *view;
	struct wl_list link;
};
struct qdwin_seat_tracker { char *silo; };
struct qdwin_primary_seat { int placeholder; };
struct qdwin {
	struct weston_compositor *compositor;
	int locked;
	struct wl_list toplevels;
};

/* Stub counters: every side effect the lock gate must precede. */
static int post_error_calls, log_calls, serial_calls, set_selection_calls;
static int pseat_find_calls, pseat_clear_calls, activate_calls;
static int kbd_focus_calls, seat_kbd_focus_calls, emit_focus_calls;
static int tracker_find_calls, set_silo_calls;
static void *fake_ud;
static struct qdwin_seat_tracker *fake_tracker;

#define weston_log(...) do { log_calls++; } while (0)
#define wl_resource_get_user_data(r) fake_ud
#define wl_resource_post_error stub_post_error
#define wl_display_next_serial stub_next_serial

static void
wl_resource_post_error(struct wl_resource *r, uint32_t code,
		       const char *msg, ...)
{
	(void)r; (void)code; (void)msg;
	post_error_calls++;
}
static int
qdwin_shell_require_bound(struct qdwin *q, struct wl_resource *r)
{
	(void)q; (void)r;
	return 1;
}
static uint32_t
wl_display_next_serial(struct wl_display *d)
{
	(void)d;
	serial_calls++;
	return (uint32_t)serial_calls;
}
static void
weston_seat_set_selection(struct weston_seat *s, void *src, uint32_t ser)
{
	(void)s; (void)src; (void)ser;
	set_selection_calls++;
}
static struct weston_keyboard *
weston_seat_get_keyboard(struct weston_seat *s)
{
	static struct weston_keyboard kbd;
	(void)s;
	return &kbd;
}
static struct qdwin_primary_seat *
qdwin_primary_seat_find(struct qdwin *q, struct weston_seat *s)
{
	(void)q; (void)s;
	pseat_find_calls++;
	return NULL;
}
static void
qdwin_primary_seat_clear_selection(struct qdwin_primary_seat *p, int n)
{
	(void)p; (void)n;
	pseat_clear_calls++;
}
static void
weston_view_activate_input(struct weston_view *v, struct weston_seat *s,
			   uint32_t flags)
{
	(void)v; (void)s; (void)flags;
	activate_calls++;
}
static void
weston_keyboard_set_focus(struct weston_keyboard *k,
			  struct weston_surface *surf)
{
	(void)k; (void)surf;
	kbd_focus_calls++;
}
static void
weston_seat_set_keyboard_focus(struct weston_seat *s,
			       struct weston_surface *surf)
{
	(void)s; (void)surf;
	seat_kbd_focus_calls++;
}
static void
qdwin_emit_seat_focus_changed(struct qdwin *q, struct weston_seat *s,
			      uint32_t handle)
{
	(void)q; (void)s; (void)handle;
	emit_focus_calls++;
}
static struct qdwin_seat_tracker *
qdwin_seat_tracker_for_seat(struct qdwin *q, struct weston_seat *s)
{
	(void)q; (void)s;
	tracker_find_calls++;
	return fake_tracker;
}
static const char *
qdwin_seat_tracker_silo(struct qdwin_seat_tracker *tr)
{
	/* Production returns "" (qdwin.c qdwin_seat_tracker_silo) — match it so
	 * a missing locked `return` fails via mutation counters, not a NULL
	 * dereference of prev_silo. */
	return (tr && tr->silo) ? tr->silo : "";
}
static void
qdwin_seat_tracker_set_silo(struct qdwin_seat_tracker *tr, const char *silo)
{
	set_silo_calls++;
	if (!tr)
		return;
	free(tr->silo);
	tr->silo = silo ? strdup(silo) : NULL;
}
"""

EPILOGUE = r"""
static struct weston_compositor comp;
static struct weston_seat seat0;
static struct weston_surface surf;
static struct weston_view view0 = { &surf };
static struct qdwin_toplevel tl7;
static struct qdwin qd;
static struct qdwin_seat_tracker tracker;
static struct wl_resource *res;

static void reset_counters(void)
{
	post_error_calls = log_calls = serial_calls = set_selection_calls = 0;
	pseat_find_calls = pseat_clear_calls = activate_calls = 0;
	kbd_focus_calls = seat_kbd_focus_calls = emit_focus_calls = 0;
	tracker_find_calls = set_silo_calls = 0;
}

static int zero_mutations(void)
{
	return serial_calls == 0 && set_selection_calls == 0 &&
	       pseat_find_calls == 0 && pseat_clear_calls == 0 &&
	       activate_calls == 0 && kbd_focus_calls == 0 &&
	       seat_kbd_focus_calls == 0 && emit_focus_calls == 0 &&
	       tracker_find_calls == 0 && set_silo_calls == 0;
}

static void reset_fixture(void)
{
	memset(&comp, 0, sizeof comp);
	wl_list_init(&comp.seat_list);
	comp.wl_display = (struct wl_display *)1;
	seat0.seat_name = "seat0";
	wl_list_insert(&comp.seat_list, &seat0.link);
	memset(&tl7, 0, sizeof tl7);
	tl7.handle = 7;
	tl7.view = &view0;
	memset(&qd, 0, sizeof qd);
	qd.compositor = &comp;
	wl_list_init(&qd.toplevels);
	wl_list_insert(&qd.toplevels, &tl7.link);
	fake_ud = &qd;
	fake_tracker = &tracker;
	free(tracker.silo);
	tracker.silo = NULL;
	reset_counters();
}

#define CHECK(cond, ...) do { \
	if (!(cond)) { printf("FAIL: " __VA_ARGS__); printf("\n"); return 1; } \
} while (0)

int
main(void)
{
	/* Case 1: locked v1 is a logged drop with zero side effects. */
	reset_fixture();
	qd.locked = 1;
	qdwin_handle_set_keyboard_focus(NULL, res, "seat0", 7);
	CHECK(post_error_calls == 0,
	      "locked v1 posted a protocol error (fatal refusal regressed)");
	CHECK(log_calls == 1,
	      "locked v1 did not log the refusal (got %d logs)", log_calls);
	CHECK(zero_mutations(),
	      "locked v1 reached the mutation path: serial=%d sel=%d "
	      "activate=%d kbd=%d seatkbd=%d emit=%d tracker=%d silo=%d",
	      serial_calls, set_selection_calls, activate_calls,
	      kbd_focus_calls, seat_kbd_focus_calls, emit_focus_calls,
	      tracker_find_calls, set_silo_calls);
	printf("case 1 ok: locked v1 logged drop, zero side effects\n");

	/* Case 2: locked v2 same; the seat tracker is never written. */
	reset_fixture();
	qd.locked = 1;
	qdwin_handle_set_keyboard_focus_v2(NULL, res, "seat0", 7, "work");
	CHECK(post_error_calls == 0,
	      "locked v2 posted a protocol error (fatal refusal regressed)");
	CHECK(log_calls == 1,
	      "locked v2 did not log the refusal (got %d logs)", log_calls);
	CHECK(zero_mutations(),
	      "locked v2 reached the mutation path");
	CHECK(tracker.silo == NULL,
	      "locked v2 wrote last_target_silo='%s'", tracker.silo);
	printf("case 2 ok: locked v2 logged drop, tracker untouched\n");

	/* Case 3: unlocked v2 same-silo keeps the selection but applies
	 * focus and rewrites the tracker. */
	reset_fixture();
	tracker.silo = strdup("work");
	qdwin_handle_set_keyboard_focus_v2(NULL, res, "seat0", 7, "work");
	CHECK(post_error_calls == 0, "unlocked v2 posted an error");
	CHECK(set_selection_calls == 0,
	      "same-silo v2 cleared the selection (cross_silo regressed)");
	CHECK(activate_calls == 1,
	      "unlocked v2 did not activate the target view");
	CHECK(emit_focus_calls == 1,
	      "unlocked v2 did not emit seat_focus_changed");
	CHECK(set_silo_calls == 1 && tracker.silo &&
	      strcmp(tracker.silo, "work") == 0,
	      "v2 did not update last_target_silo");
	printf("case 3 ok: same-silo v2 keeps selection, applies focus\n");

	/* Case 4: unlocked v2 cross-silo clears the selection. */
	reset_fixture();
	tracker.silo = strdup("work");
	qdwin_handle_set_keyboard_focus_v2(NULL, res, "seat0", 7, "game");
	CHECK(post_error_calls == 0, "unlocked v2 posted an error");
	CHECK(set_selection_calls == 1,
	      "cross-silo v2 did not clear the selection");
	CHECK(activate_calls == 1, "cross-silo v2 did not activate");
	CHECK(tracker.silo && strcmp(tracker.silo, "game") == 0,
	      "v2 cross-silo tracker = '%s'", tracker.silo);
	printf("case 4 ok: cross-silo v2 clears selection\n");

	/* Case 5: a locked drop leaves no wedged state — the next unlocked
	 * v1 applies focus normally (clears selection, activates, emits). */
	reset_fixture();
	qd.locked = 1;
	qdwin_handle_set_keyboard_focus(NULL, res, "seat0", 7);
	qd.locked = 0;
	reset_counters();
	qdwin_handle_set_keyboard_focus(NULL, res, "seat0", 7);
	CHECK(post_error_calls == 0, "post-drop v1 posted an error");
	CHECK(set_selection_calls == 1,
	      "post-drop v1 did not clear the selection");
	CHECK(activate_calls == 1, "post-drop v1 did not activate");
	CHECK(emit_focus_calls == 1, "post-drop v1 did not emit");
	printf("case 5 ok: focus still lands after a locked drop\n");

	free(tracker.silo);
	return 0;
}
"""

QDWIN_FNS = [("void", "qdwin_handle_set_keyboard_focus"),
             ("void", "qdwin_handle_set_keyboard_focus_v2")]


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
    for ret, name in QDWIN_FNS:
        parts.append(f"static {ret} " + extract(qdwin_c, name))
    parts.append(EPILOGUE)

    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "probe.c"
        exe = Path(td) / "probe"
        src.write_text("\n".join(parts), encoding="utf-8")
        cflags = subprocess.run(
            ["pkg-config", "--cflags", "--libs", "wayland-server"],
            capture_output=True, text=True, check=True).stdout.split()
        build = subprocess.run([cc, str(src), "-o", str(exe)] + cflags,
                               capture_output=True, text=True)
        if build.returncode:
            print(build.stderr)
            return fail("probe did not compile")
        run = subprocess.run([str(exe)], capture_output=True, text=True)
        sys.stdout.write(run.stdout)
        sys.stdout.write(run.stderr)
        if run.returncode in (0, 77):
            return run.returncode
        return 1


if __name__ == "__main__":
    sys.exit(main())
