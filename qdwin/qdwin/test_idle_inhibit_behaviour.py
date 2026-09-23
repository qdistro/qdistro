#!/usr/bin/env python3
"""Behavioural test for the idle-inhibit hold policy (iso2/11 E2).

The source invariant next door pins the *shape* of the fix. This pins what
actually matters: the value of weston's ec->idle_inhibit counter across real
surface lifecycles.

There is no headless harness that can run qdwin's compositor, so this splices
the real function bodies — weston's recursive weston_surface_is_mapped() /
weston_surface_map() / weston_surface_unmap() and qdwin's inhibitor
activate/deactivate/sync/recheck — out of the actual sources, compiles them
against real libwayland-server signals with reduced structs, and asserts on
the counter. Technique credited to the codex round-1 review, which used it to
find the parent-unmap hole this file now regression-tests.

Cases:
  1. An inhibitor on a never-mapped surface holds nothing.
  2. An inhibitor on a mapped subsurface holds +1.
  3. Unmapping the *parent* makes the child effectively unmapped and emits no
     signal on the child, so the eager listeners alone leave a stale +1 —
     this is asserted explicitly, because it is the reason the backstop exists.
  4. The backstop sweep releases that stale hold.
  5. Remapping the parent re-acquires it.
  6. A mapped surface with no buffer holds nothing (weston stale-map-bit case).
  7. A destroyed wl_subsurface role leaves a mapped, buffered, viewless
     orphan that must not hold — and must not re-acquire later.
  8. Creation churn cannot starve the backstop timer.
  9. Multiple inhibitors are independent and the counter ends clean.
 9b. Dropping the last hold re-arms weston's one-shot idle timer.
 10. A failed backstop re-arm disables idle-inhibit terminally: holds go,
     a remap does not re-acquire, and new inhibitors are refused.
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
#include <stdio.h>
#include <time.h>
#include <wayland-server-core.h>

#define weston_log(...) ((void)0)
#define WESTON_COMPOSITOR_ACTIVE 1

struct weston_compositor {
	unsigned idle_inhibit;
	int idle_time;
	int state;
	struct wl_event_source *idle_source;
	struct wl_display *wl_display;
	bool view_list_needs_rebuild;
};
#define QDWIN_IDLE_INHIBIT_RECHECK_MS 1000

struct weston_subsurface;
struct weston_buffer { int placeholder; };
struct weston_buffer_ref { struct weston_buffer *buffer; };
struct weston_surface {
	bool is_mapped, is_mapping;
	struct weston_compositor *compositor;
	struct wl_signal map_signal, unmap_signal;
	struct wl_list views;
	void *output;
	struct weston_buffer_ref buffer_ref;
	struct weston_subsurface *sub;
};
struct weston_subsurface { struct weston_surface *parent; };
struct weston_view { struct wl_list surface_link; };
struct qdwin {
	struct weston_compositor *compositor;
	struct wl_list idle_inhibitors;
	struct wl_event_source *idle_inhibit_recheck_timer;
	bool idle_inhibit_disabled;
};
struct qdwin_idle_inhibitor;
static void qdwin_idle_inhibitor_sync(struct qdwin_idle_inhibitor *inh);
static void qdwin_idle_inhibitor_deactivate(struct qdwin_idle_inhibitor *inh);
struct qdwin_idle_inhibitor {
	struct qdwin *qdwin;
	struct wl_resource *resource;
	struct weston_surface *surface;
	struct wl_listener surface_destroy_listener;
	struct wl_listener surface_map_listener;
	struct wl_listener surface_unmap_listener;
	int active;
	struct wl_list link;
};

static struct weston_subsurface *
weston_surface_to_subsurface(struct weston_surface *s) { return s->sub; }
static void weston_view_unmap(struct weston_view *v) { (void)v; }
/* Real libwayland timers are used for the backstop. Only the compositor's
 * *idle* source is fake: it is not a wl_event_source at all, so intercept
 * updates aimed at it and count them separately from backstop re-arms
 * (codex r2: the old probe conflated the two). */
static struct wl_event_source *fake_idle_source = (struct wl_event_source *)0x1;
static int idle_rearm_calls;
/* Failure injection: make the next N backstop timer updates fail without
 * scheduling anything, the way a real wl_event_source_timer_update() error
 * would (codex r3 #1 asked for exactly this regression). */
static int fail_next_backstop_updates;
static int
qd_timer_update(struct wl_event_source *s, int ms)
{
	if (s == fake_idle_source) { idle_rearm_calls++; return 0; }
	if (fail_next_backstop_updates > 0) {
		fail_next_backstop_updates--;
		return -1;
	}
	return wl_event_source_timer_update(s, ms);
}
#define wl_event_source_timer_update qd_timer_update
"""

EPILOGUE = r"""
static void init_surface(struct weston_surface *s, struct weston_compositor *c)
{
	s->compositor = c;
	wl_signal_init(&s->map_signal);
	wl_signal_init(&s->unmap_signal);
	wl_list_init(&s->views);
}

/* A live weston_view, so the has-a-view term is exercised for real. */
static void add_view(struct weston_surface *s, struct weston_view *v)
{
	wl_list_insert(&s->views, &v->surface_link);
}

/* Model wl_subsurface.destroy: weston destroys every view but leaves the
 * mapped bit and the buffer alone, and clears ->committed so the recursive
 * mapped query stops walking to the parent. */
static void destroy_subsurface_role(struct weston_surface *s)
{
	struct weston_view *v, *next;

	wl_list_for_each_safe(v, next, &s->views, surface_link)
		wl_list_remove(&v->surface_link);
	wl_list_init(&s->views);
	s->sub = NULL;
}

/* The real create path, minus resource allocation: same ordering, same
 * empty->non-empty arming rule. */
static bool
create_inhibitor(struct qdwin_idle_inhibitor *i, struct qdwin *q,
		 struct weston_surface *s)
{
	bool was_empty;

	if (!qdwin_idle_inhibit_recheck_ensure(q))
		return false;   /* production posts no_memory and returns */
	i->qdwin = q;
	i->surface = s;
	i->active = 0;
	wl_list_init(&i->surface_destroy_listener.link);
	i->surface_map_listener.notify = qdwin_idle_inhibitor_surface_mapped;
	i->surface_unmap_listener.notify = qdwin_idle_inhibitor_surface_unmapped;
	wl_signal_add(&s->map_signal, &i->surface_map_listener);
	wl_signal_add(&s->unmap_signal, &i->surface_unmap_listener);
	was_empty = wl_list_empty(&q->idle_inhibitors);
	wl_list_insert(&q->idle_inhibitors, &i->link);
	qdwin_idle_inhibitor_sync(i);
	if (was_empty && !qdwin_idle_inhibit_recheck_schedule(q))
		return false;
	return true;
}

/* The real resource-destroy path. */
static void destroy_inhibitor(struct qdwin_idle_inhibitor *i)
{
	qdwin_idle_inhibitor_deactivate(i);
	if (i->surface) {
		wl_list_remove(&i->surface_map_listener.link);
		wl_list_remove(&i->surface_unmap_listener.link);
		i->surface = NULL;
	}
	wl_list_remove(&i->link);
}

/* Drive the real event loop for a real `ms` of wall time. Dispatch can
 * return early (a ready source, an interruption), so count actual elapsed
 * time against a monotonic deadline rather than assuming each call consumed
 * its slice (codex r3 #5). Returns the elapsed milliseconds. */
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

int main(void)
{
	struct weston_buffer dummy_buffer;
	struct wl_display *display = wl_display_create();
	struct wl_event_loop *loop;
	struct weston_compositor c = { 0 };
	struct qdwin q = { .compositor = &c };
	struct weston_surface parent = { 0 }, child = { 0 }, lone = { 0 };
	struct weston_subsurface sub = { .parent = &parent };
	struct weston_view pv = { 0 }, cv = { 0 }, lv = { 0 };
	struct qdwin_idle_inhibitor ci = { 0 }, li = { 0 }, churn = { 0 };
	const unsigned baseline = 2;
	int inconclusive = 0;

	assert(display);
	loop = wl_display_get_event_loop(display);
	c.wl_display = display;
	c.state = WESTON_COMPOSITOR_ACTIVE;
	c.idle_time = 300;
	c.idle_source = fake_idle_source;
	wl_list_init(&q.idle_inhibitors);
	/* An unrelated owner already holds the counter; none of our
	 * arithmetic may disturb it (codex r3 #5). */
	c.idle_inhibit = baseline;
	init_surface(&parent, &c);
	init_surface(&child, &c);
	init_surface(&lone, &c);
	child.sub = &sub;
	parent.buffer_ref.buffer = &dummy_buffer;
	child.buffer_ref.buffer = &dummy_buffer;
	lone.buffer_ref.buffer = &dummy_buffer;
	add_view(&parent, &pv);
	add_view(&child, &cv);
	add_view(&lone, &lv);

	/* 1. never-mapped surface holds nothing. */
	CHECK(create_inhibitor(&li, &q, &lone), "arming the backstop failed");
	CHECK(c.idle_inhibit == baseline,
	      "never-mapped surface took a hold (counter=%u)", c.idle_inhibit);

	/* 2. mapped subsurface holds +1. */
	weston_surface_map(&parent);
	weston_surface_start_mapping(&child);
	CHECK(create_inhibitor(&ci, &q, &child), "create failed");
	CHECK(c.idle_inhibit == baseline + 1,
	      "mapped subsurface did not hold (counter=%u)", c.idle_inhibit);

	/* 3. parent unmap flips the child's effective state with NO signal on
	 *    the child, so the eager listeners alone leave the hold stale.
	 *    This is the hole codex r1 #1 found; asserting it keeps the
	 *    backstop honest. */
	weston_surface_unmap(&parent);
	CHECK(!weston_surface_is_mapped(&child),
	      "child should be effectively unmapped after parent unmap");
	CHECK(c.idle_inhibit == baseline + 1,
	      "expected the eager path to miss this (counter=%u)",
	      c.idle_inhibit);

	/* 4. codex r2 #1: a client churning throwaway inhibitors must not be
	 *    able to postpone reconciliation. Create+destroy an inhibitor on
	 *    an unrelated never-mapped surface faster than the recheck
	 *    interval, for longer than the interval, and the stale hold must
	 *    still be released. */
	{
		long total = 0, worst_gap = 0;

		for (int i = 0; i < 6; i++) {
			struct weston_surface dummy = { 0 };
			/* Measure the WHOLE interval between consecutive
			 * creates, not just the time inside pump(): a
			 * deschedule between create and pump's first clock
			 * read would otherwise be invisible, and could let
			 * the deadline expire while the reported gap still
			 * looked sub-interval (codex r4 #4). */
			long gap_start = now_ms(), gap;
			init_surface(&dummy, &c);
			create_inhibitor(&churn, &q, &dummy);
			pump(loop, 400);
			destroy_inhibitor(&churn);
			gap = now_ms() - gap_start;
			total += gap;
			if (gap > worst_gap)
				worst_gap = gap;
		}
		/* The attack needs each create to land inside the recheck
		 * interval. If the scheduler stalled past it, this run proves
		 * nothing either way. Record that and keep going — bailing
		 * out here would also skip the latch regression below. */
		if (worst_gap >= QDWIN_IDLE_INHIBIT_RECHECK_MS ||
		    pump_errors > 0) {
			printf("INCONCLUSIVE: churn timing precondition not "
			       "met (worst gap %ld ms vs recheck interval "
			       "%d ms, dispatch errors %d); skipping the "
			       "starvation assertion, later cases still run\n",
			       worst_gap, QDWIN_IDLE_INHIBIT_RECHECK_MS,
			       pump_errors);
			inconclusive = 1;
			/* Reconcile by hand so the later cases start from a
			 * known state. */
			qdwin_idle_inhibit_recheck(&q);
		} else {
			CHECK(c.idle_inhibit == baseline,
			      "creation churn starved the backstop — stale "
			      "hold survived %ld ms of sub-interval "
			      "create/destroy cycles (worst gap %ld ms, "
			      "counter=%u)", total, worst_gap, c.idle_inhibit);
		}
	}

	/* 5. parent remap re-acquires, via the backstop alone. */
	weston_surface_map(&parent);
	CHECK(weston_surface_is_mapped(&child),
	      "child should be effectively mapped again");
	pump(loop, 1400);
	CHECK(c.idle_inhibit == baseline + 1,
	      "backstop did not re-acquire on remap (counter=%u)",
	      c.idle_inhibit);

	/* 6. mapped but bufferless holds nothing (weston stale map bit). */
	child.buffer_ref.buffer = NULL;
	pump(loop, 1400);
	CHECK(c.idle_inhibit == baseline,
	      "bufferless surface kept a hold (counter=%u)", c.idle_inhibit);
	child.buffer_ref.buffer = &dummy_buffer;
	pump(loop, 1400);
	CHECK(c.idle_inhibit == baseline + 1, "did not recover (counter=%u)",
	      c.idle_inhibit);

	/* 7. codex r2 #2: destroying the wl_subsurface role while keeping the
	 *    wl_surface leaves a buffered surface whose mapped bit still
	 *    reads true and which no longer walks to its parent — but with no
	 *    views. It must not hold, and must not re-acquire later. */
	destroy_subsurface_role(&child);
	CHECK(weston_surface_is_mapped(&child),
	      "precondition: the orphan still reads as mapped");
	CHECK(child.buffer_ref.buffer != NULL,
	      "precondition: the orphan still has its buffer");
	pump(loop, 1400);
	CHECK(c.idle_inhibit == baseline,
	      "destroyed-subsurface-role orphan kept a hold (counter=%u)",
	      c.idle_inhibit);
	pump(loop, 1400);
	CHECK(c.idle_inhibit == baseline,
	      "orphan re-acquired on a later sweep (counter=%u)",
	      c.idle_inhibit);

	/* 8. the second inhibitor stayed independent and sweeps are
	 *    idempotent; it acquires on its own map. */
	weston_surface_map(&lone);
	CHECK(c.idle_inhibit == baseline + 1,
	      "second inhibitor did not acquire on its own map (counter=%u)",
	      c.idle_inhibit);
	pump(loop, 2400);
	CHECK(c.idle_inhibit == baseline + 1,
	      "repeated sweeps are not idempotent (counter=%u)",
	      c.idle_inhibit);

	/* 9. releasing the last inhibitor leaves the counter clean. */
	destroy_inhibitor(&li);
	destroy_inhibitor(&ci);
	CHECK(wl_list_empty(&q.idle_inhibitors), "list not drained");
	CHECK(c.idle_inhibit == baseline,
	      "counter not clean after the last inhibitor left (counter=%u)",
	      c.idle_inhibit);
	pump(loop, 1400);
	CHECK(c.idle_inhibit == baseline, "a sweep on an empty list changed the "
	      "counter (counter=%u)", c.idle_inhibit);

	/* 9b. weston's idle_source is one-shot and its handler returns early
	 *     while inhibited, so dropping the LAST hold — the counter
	 *     actually reaching zero, not just our contribution going —
	 *     must re-arm it. Needs the unrelated owner gone. */
	{
		struct weston_surface s1 = { 0 };
		struct weston_view v1 = { 0 };
		struct qdwin_idle_inhibitor i1 = { 0 };
		struct weston_buffer b1;
		int before;

		c.idle_inhibit = 0;
		init_surface(&s1, &c);
		add_view(&s1, &v1);
		s1.buffer_ref.buffer = &b1;
		weston_surface_map(&s1);
		CHECK(create_inhibitor(&i1, &q, &s1), "case 9b create failed");
		CHECK(c.idle_inhibit == 1, "case 9b precondition (counter=%u)",
		      c.idle_inhibit);
		before = idle_rearm_calls;
		weston_surface_unmap(&s1);
		CHECK(c.idle_inhibit == 0, "case 9b hold not released "
		      "(counter=%u)", c.idle_inhibit);
		CHECK(idle_rearm_calls > before,
		      "dropping the last hold did not re-arm weston's "
		      "one-shot idle timer");
		destroy_inhibitor(&i1);
		c.idle_inhibit = baseline;
	}

	/* 10. codex r3 #1: if the backstop's self-rearm ever fails, every
	 *     hold must go and NOTHING may take one again — not this
	 *     inhibitor on a later map, not a freshly created one. Merely
	 *     releasing would be momentary: a remap would re-acquire a hold
	 *     that nothing would ever re-evaluate. */
	{
		struct weston_surface s2 = { 0 };
		struct weston_view v2 = { 0 };
		struct qdwin_idle_inhibitor i2 = { 0 }, i3 = { 0 };
		struct weston_buffer b2;

		init_surface(&s2, &c);
		add_view(&s2, &v2);
		s2.buffer_ref.buffer = &b2;
		weston_surface_map(&s2);
		CHECK(create_inhibitor(&i2, &q, &s2), "case 10 create failed");
		CHECK(c.idle_inhibit == baseline + 1,
		      "case 10 precondition: hold not taken (counter=%u)",
		      c.idle_inhibit);

		fail_next_backstop_updates = 1;
		pump(loop, 1400);
		CHECK(q.idle_inhibit_disabled,
		      "a failed backstop re-arm did not disable idle-inhibit");
		CHECK(c.idle_inhibit == baseline,
		      "a failed backstop re-arm did not release the holds "
		      "(counter=%u)", c.idle_inhibit);

		/* remap must NOT re-acquire */
		weston_surface_unmap(&s2);
		weston_surface_map(&s2);
		CHECK(c.idle_inhibit == baseline,
		      "a hold was re-acquired after the backstop failed "
		      "(counter=%u)", c.idle_inhibit);
		/* No sweep is scheduled any more — that is the point — so
		 * this dispatches the loop to show nothing re-acquires, and
		 * then forces a sweep by hand to prove a hypothetical later
		 * one would not either (codex r4 #5). */
		pump(loop, 400);
		qdwin_idle_inhibit_recheck(&q);
		CHECK(c.idle_inhibit == baseline,
		      "a hold reappeared on a forced later sweep after the "
		      "backstop failed (counter=%u)", c.idle_inhibit);

		/* a brand-new inhibitor must be refused */
		CHECK(!create_inhibitor(&i3, &q, &s2),
		      "a new inhibitor was accepted after the backstop failed");
		CHECK(c.idle_inhibit == baseline,
		      "the refused inhibitor still moved the counter "
		      "(counter=%u)", c.idle_inhibit);
	}

	if (inconclusive) {
		printf("SKIP: idle-inhibit behaviour — every case passed but "
		       "the churn/starvation timing precondition was not "
		       "met on this run\n");
		return 77;
	}
	printf("ok: idle-inhibit behaviour (10 cases)\n");
	return 0;
}
"""

WESTON_FNS = ["weston_surface_is_mapped", "weston_surface_start_mapping",
              "weston_surface_map", "weston_surface_unmap"]
QDWIN_FNS = [("void", "qdwin_idle_inhibitor_activate"),
             ("void", "qdwin_idle_inhibitor_deactivate"),
             ("void", "qdwin_idle_inhibitors_release_all"),
             ("bool", "qdwin_idle_inhibit_recheck_schedule"),
             ("void", "qdwin_idle_inhibitor_sync"),
             ("void", "qdwin_idle_inhibitor_surface_mapped"),
             ("void", "qdwin_idle_inhibitor_surface_unmapped"),
             ("int", "qdwin_idle_inhibit_recheck"),
             ("bool", "qdwin_idle_inhibit_recheck_ensure")]


def main():
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        print("no C compiler; skipping")
        return 77
    if subprocess.run(["pkg-config", "--exists", "wayland-server"]).returncode:
        print("wayland-server not available; skipping")
        return 77

    qdwin_c = (ROOT / "qdwin" / "qdwin.c").read_text(encoding="utf-8")
    weston_c = (ROOT / "libweston-vendored" / "src" / "libweston" /
                "compositor.c").read_text(encoding="utf-8")

    parts = [PROLOGUE]
    parts.append("static bool " + extract(weston_c, WESTON_FNS[0]))
    for name in WESTON_FNS[1:]:
        parts.append("static void " + extract(weston_c, name))
    logic_c = (ROOT / "qdwin" / "qdwin-logic.c").read_text(encoding="utf-8")
    parts.append("static bool " +
                 extract(logic_c, "qdwin_idle_inhibit_should_hold"))
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
        # 77 is meson's skip code and the probe uses it for an inconclusive
        # timing precondition. Collapsing it to 1 would report a scheduler
        # stall as a regression (codex r4 #4).
        if run.returncode in (0, 77):
            return run.returncode
        return 1


if __name__ == "__main__":
    sys.exit(main())
