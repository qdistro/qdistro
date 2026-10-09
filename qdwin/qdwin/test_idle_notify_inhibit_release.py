#!/usr/bin/env python3
"""Behavioural test: inhibitor-suppressed ext-idle-notify timers.

Non-internal mode (weston's built-in idle timer drives idle_signal) arms a
per-notification wl_event_source for notifications whose timeout exceeds
weston's idle_time (qdwin.c §6.7(a)). Those timers are one-shot: if an
inhibitor hold is taken after idle_signal and the secondary deadline expires
under the hold, the callback returns without rearming, and idle_signal will
not fire again until the compositor wakes and re-idles. Releasing the last
hold while the compositor is already IDLE restarted neither the built-in
timer nor the notification's own — the notification stayed silent until
unrelated activity.

The fix marks such notifications expired_while_inhibited and delivers the
overdue `idled` when the last hold drops while the compositor is not ACTIVE.

Same splice-compile technique as test_idle_inhibit_behaviour.py: the real
qdwin function bodies are extracted from qdwin.c, compiled against real
libwayland-server signals and event-loop timers, and asserted on — so a
mutation to the production code fails here, not just in review.

Cases:
  1. The bug: idle → inhibit → secondary deadline passes → release with no
     input delivers `idled`, sets is_idle, and does NOT re-arm weston's
     idle_source while IDLE.
  2. A wake pairs that delivered `idled` with a `resumed`.
  3. input-idle (ignore_inhibit) notifications are never suppressed.
  4. A hold released before the secondary deadline fires nothing early;
     the armed timer still fires on schedule.
  5. A wake clears the expired marker before release: no spurious `idled`,
     no `resumed`, and the next idle cycle works normally.
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
#include <time.h>
#include <wayland-server-core.h>

#define weston_log(...) ((void)0)
#define WESTON_COMPOSITOR_ACTIVE 1
#define WESTON_COMPOSITOR_IDLE 2
#define WESTON_COMPOSITOR_SLEEPING 3

#define QDWIN_IDLE_INTERNAL_INHIBIT_POLL_MS 1000u

struct weston_compositor {
	unsigned idle_inhibit;
	int idle_time;
	int state;
	struct wl_event_source *idle_source;
	struct wl_display *wl_display;
	struct wl_signal idle_signal;
	struct wl_signal wake_signal;
};

static void
weston_compositor_get_time(struct timespec *t)
{
	clock_gettime(CLOCK_MONOTONIC, t);
}

/* Notification/inhibitor structs mirror qdwin.c's fields one-for-one in
 * the members the spliced bodies touch. */
struct qdwin_idle_notification {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	uint32_t timeout_ms;
	uint64_t last_activity_msec;
	int is_idle;
	int ignore_inhibit;
	int expired_while_inhibited;
	struct wl_event_source *timer;
	struct wl_list link;
};

struct qdwin {
	struct weston_compositor *compositor;
	struct wl_list idle_notifications;
	int idle_internal_mode;
	struct wl_listener idle_signal_listener;
	struct wl_listener wake_signal_listener;
};

struct qdwin_idle_inhibitor {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	int active;
	struct wl_list link;
};
struct weston_surface { int placeholder; };

/* Protocol sends are generated code; count them per fake resource so the
 * test sees exactly which notification produced which event. */
#define MAX_RES 16
static int stub_idled[MAX_RES], stub_resumed[MAX_RES];
static void
ext_idle_notification_v1_send_idled(struct wl_resource *r)
{
	stub_idled[(uintptr_t)r]++;
}
static void
ext_idle_notification_v1_send_resumed(struct wl_resource *r)
{
	stub_resumed[(uintptr_t)r]++;
}

/* Real libwayland timers drive the notification timers. Only the
 * compositor's built-in idle source is fake: intercept updates aimed at
 * it so a release-while-idle cannot hide behind a spurious re-arm. */
static struct wl_event_source *fake_idle_source = (struct wl_event_source *)0x1;
static int idle_rearm_calls;
static int
qd_timer_update(struct wl_event_source *s, int ms)
{
	if (s == fake_idle_source) { idle_rearm_calls++; return 0; }
	return wl_event_source_timer_update(s, ms);
}
#define wl_event_source_timer_update qd_timer_update
"""

EPILOGUE = r"""
/* Model qdwin_idle_notification_create minus the wire resource: same
 * field init, same real event-loop timer, same list insertion. */
static void
make_notification(struct qdwin_idle_notification *n, struct qdwin *q,
		  int res_idx, uint32_t timeout_ms, int ignore_inhibit)
{
	memset(n, 0, sizeof *n);
	n->qdwin = q;
	n->resource = (struct wl_resource *)(uintptr_t)res_idx;
	n->timeout_ms = timeout_ms;
	n->last_activity_msec = qdwin_now_msec();
	n->ignore_inhibit = ignore_inhibit;
	n->timer = wl_event_loop_add_timer(
		wl_display_get_event_loop(q->compositor->wl_display),
		qdwin_idle_notification_timer_fire, n);
	assert(n->timer);
	wl_list_insert(&q->idle_notifications, &n->link);
}

/* Drive the real event loop for a real `ms` of wall time and report the
 * elapsed time so callers can judge "not yet" assertions honestly under
 * scheduler stalls. */
static long now_ms(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static int pump_errors;

static long pump(struct wl_event_loop *loop, int ms)
{
	long start = now_ms(), end = start + ms, left;

	while ((left = end - now_ms()) > 0) {
		if (wl_event_loop_dispatch(loop, left > 40 ? 40 : (int)left) < 0) {
			pump_errors++;
			break;
		}
	}
	return now_ms() - start;
}

#define CHECK(cond, ...) do { \
	if (!(cond)) { printf("FAIL: " __VA_ARGS__); printf("\n"); return 1; } \
} while (0)

#define IDLE(c) do { \
	(c)->state = WESTON_COMPOSITOR_IDLE; \
	wl_signal_emit(&(c)->idle_signal, (c)); \
} while (0)

#define WAKE(c) do { \
	(c)->state = WESTON_COMPOSITOR_ACTIVE; \
	wl_signal_emit(&(c)->wake_signal, (c)); \
} while (0)

/* idle_time = 1s, so a timeout_ms of 1500 arms a 500 ms secondary. */
#define SECONDARY(n) ((n).timeout_ms - 1000)

int main(void)
{
	struct wl_display *display = wl_display_create();
	struct wl_event_loop *loop;
	struct weston_compositor c = { 0 };
	struct qdwin q = { .compositor = &c };
	struct qdwin_idle_notification n1, n2, n3, n4;
	struct qdwin_idle_inhibitor inh = { .qdwin = &q };
	int before;
	long elapsed;
	int inconclusive = 0;

	assert(display);
	loop = wl_display_get_event_loop(display);
	c.wl_display = display;
	c.idle_time = 1;
	c.state = WESTON_COMPOSITOR_ACTIVE;
	c.idle_source = fake_idle_source;
	wl_signal_init(&c.idle_signal);
	wl_signal_init(&c.wake_signal);
	wl_list_init(&q.idle_notifications);
	q.idle_internal_mode = 0;
	q.idle_signal_listener.notify = qdwin_on_idle_signal;
	wl_signal_add(&c.idle_signal, &q.idle_signal_listener);
	q.wake_signal_listener.notify = qdwin_on_wake_signal;
	wl_signal_add(&c.wake_signal, &q.wake_signal_listener);

	/* 1. idle_signal arms the secondary; a hold taken after it lets the
	 *    deadline lapse; release while IDLE must deliver `idled`. */
	make_notification(&n1, &q, 1, 1500, 0);
	IDLE(&c);
	qdwin_idle_inhibitor_activate(&inh);
	CHECK(c.idle_inhibit == 1, "case 1: hold not taken");
	pump(loop, 900);   /* secondary deadline is ~500 ms */
	CHECK(stub_idled[1] == 0,
	      "case 1: idled sent while the hold was still up");
	CHECK(n1.expired_while_inhibited,
	      "case 1: expiry under the hold was not recorded");
	before = idle_rearm_calls;
	qdwin_idle_inhibitor_deactivate(&inh);
	CHECK(c.idle_inhibit == 0, "case 1: hold not released");
	CHECK(stub_idled[1] == 1,
	      "case 1: overdue idled not delivered on release");
	CHECK(n1.is_idle && !n1.expired_while_inhibited,
	      "case 1: notification left in a half-delivered state");
	CHECK(idle_rearm_calls == before,
	      "case 1: idle_source re-armed while the compositor is IDLE");

	/* 2. the delivered idled pairs with resumed on the next wake. */
	WAKE(&c);
	CHECK(stub_resumed[1] == 1,
	      "case 2: delivered idled never got its resumed");

	/* 3. input-idle notifications ignore inhibitors entirely. */
	make_notification(&n2, &q, 2, 1500, 1);
	IDLE(&c);
	qdwin_idle_inhibitor_activate(&inh);
	pump(loop, 900);
	CHECK(stub_idled[2] == 1,
	      "case 3: an ignore-inhibit notification was suppressed");
	CHECK(!n2.expired_while_inhibited,
	      "case 3: ignore-inhibit notification took the inhibit path");
	qdwin_idle_inhibitor_deactivate(&inh);
	CHECK(stub_idled[2] == 1,
	      "case 3: ignore-inhibit notification delivered twice");
	WAKE(&c);

	/* 4. release before the secondary deadline must not fire early. */
	make_notification(&n3, &q, 3, 2200, 0);   /* secondary ~1200 ms */
	IDLE(&c);
	qdwin_idle_inhibitor_activate(&inh);
	elapsed = pump(loop, 500);
	qdwin_idle_inhibitor_deactivate(&inh);
	if (elapsed >= 1100 || pump_errors > 0) {
		printf("INCONCLUSIVE: case 4's 'not yet' window was consumed "
		       "by a scheduler stall (%ld ms elapsed)\n", elapsed);
		inconclusive = 1;
	} else {
		CHECK(stub_idled[3] == 0,
		      "case 4: release delivered a notification early");
	}
	pump(loop, 900);   /* total ~1400 ms, past the 1200 ms deadline */
	CHECK(stub_idled[3] == 1,
	      "case 4: armed secondary timer lost after release");
	WAKE(&c);

	/* 5. a wake before release clears the expired marker: nothing is
	 *    delivered late, and the re-idle cycle behaves normally. */
	make_notification(&n4, &q, 4, 1500, 0);
	IDLE(&c);
	qdwin_idle_inhibitor_activate(&inh);
	pump(loop, 900);
	CHECK(n4.expired_while_inhibited,
	      "case 5: precondition — deadline did not lapse under the hold");
	WAKE(&c);
	CHECK(!n4.expired_while_inhibited,
	      "case 5: wake did not clear the expired marker");
	qdwin_idle_inhibitor_deactivate(&inh);
	CHECK(stub_idled[4] == 0 && stub_resumed[4] == 0,
	      "case 5: spurious event after wake-before-release");
	IDLE(&c);
	pump(loop, 900);
	CHECK(stub_idled[4] == 1,
	      "case 5: notification lost across a wake/re-idle cycle");

	if (inconclusive) {
		printf("SKIP: idle-notify inhibit release — every ran case "
		       "passed but a timing precondition was not met\n");
		return 77;
	}
	printf("ok: idle-notify inhibit release (5 cases)\n");
	return 0;
}
"""

QDWIN_FNS = [("uint64_t", "qdwin_now_msec"),
             ("int", "qdwin_idle_internal_next_delay"),
             ("void", "qdwin_idle_notifications_deliver_expired"),
             ("void", "qdwin_idle_inhibitor_activate"),
             ("void", "qdwin_idle_inhibitor_deactivate"),
             ("int", "qdwin_idle_notification_timer_fire"),
             ("void", "qdwin_on_idle_signal"),
             ("void", "qdwin_on_wake_signal")]


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
