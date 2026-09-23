/* test-qdwin-logic.c — compiled unit test for the pure logic kernels in
 * qdwin/qdwin-logic.c. No weston, no wayland, no live compositor: just
 * the extracted arithmetic / enum maps exercised directly.
 *
 * Wired as meson test 'qdwin-logic-unit' in suite 'logic'. Prints a
 * PASS/FAIL line per check and exits non-zero on any failure.
 */
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <libinput.h>

#include "qdwin-logic.h"
#include "qdwin-xdg-constrain.h"

static int failures = 0;
static int checks = 0;

#define CHECK(cond, fmt, ...) do {                                       \
		checks++;                                                \
		if (cond) {                                              \
			printf("PASS: " fmt "\n", ##__VA_ARGS__);        \
		} else {                                                 \
			printf("FAIL: " fmt "\n", ##__VA_ARGS__);        \
			failures++;                                      \
		}                                                        \
	} while (0)

#define CHECK_U(actual, expected, label) \
	CHECK((actual) == (expected), "%s: got %u, want %u", \
	      (label), (unsigned)(actual), (unsigned)(expected))

/* ---- accel-profile enum map (incl. default) ---- */
static void test_accel_profile(void)
{
	CHECK(qdwin_accel_profile_to_libinput(QDWIN_LOGIC_ACCEL_FLAT)
	      == LIBINPUT_CONFIG_ACCEL_PROFILE_FLAT,
	      "accel FLAT -> libinput FLAT");
	CHECK(qdwin_accel_profile_to_libinput(QDWIN_LOGIC_ACCEL_ADAPTIVE)
	      == LIBINPUT_CONFIG_ACCEL_PROFILE_ADAPTIVE,
	      "accel ADAPTIVE -> libinput ADAPTIVE");
	/* any non-FLAT value falls back to adaptive */
	CHECK(qdwin_accel_profile_to_libinput(99u)
	      == LIBINPUT_CONFIG_ACCEL_PROFILE_ADAPTIVE,
	      "accel bogus(99) -> libinput ADAPTIVE (default)");
}

/* ---- scroll-method enum map (all cases incl. default) ---- */
static void test_scroll_method(void)
{
	CHECK(qdwin_scroll_method_to_libinput(QDWIN_LOGIC_SCROLL_NONE)
	      == LIBINPUT_CONFIG_SCROLL_NO_SCROLL,
	      "scroll NONE -> NO_SCROLL");
	CHECK(qdwin_scroll_method_to_libinput(QDWIN_LOGIC_SCROLL_EDGE)
	      == LIBINPUT_CONFIG_SCROLL_EDGE,
	      "scroll EDGE -> EDGE");
	CHECK(qdwin_scroll_method_to_libinput(QDWIN_LOGIC_SCROLL_ON_BUTTON_DOWN)
	      == LIBINPUT_CONFIG_SCROLL_ON_BUTTON_DOWN,
	      "scroll ON_BUTTON_DOWN -> ON_BUTTON_DOWN");
	CHECK(qdwin_scroll_method_to_libinput(QDWIN_LOGIC_SCROLL_TWO_FINGER)
	      == LIBINPUT_CONFIG_SCROLL_2FG,
	      "scroll TWO_FINGER -> 2FG");
	CHECK(qdwin_scroll_method_to_libinput(255u)
	      == LIBINPUT_CONFIG_SCROLL_2FG,
	      "scroll bogus(255) -> 2FG (default)");
}

/* ---- exclusive edge: 8 anchor combos + edge_set passthrough ---- */
static void test_exclusive_edge(void)
{
	const uint32_t T = QDWIN_LOGIC_ANCHOR_TOP;
	const uint32_t B = QDWIN_LOGIC_ANCHOR_BOTTOM;
	const uint32_t L = QDWIN_LOGIC_ANCHOR_LEFT;
	const uint32_t R = QDWIN_LOGIC_ANCHOR_RIGHT;

	/* edge_set passthrough: explicit edge wins regardless of anchor */
	CHECK_U(qdwin_layer_exclusive_edge(T | B | L | R, true, R), R,
		"edge_set passthrough returns edge_val verbatim");
	CHECK_U(qdwin_layer_exclusive_edge(0, true, L), L,
		"edge_set passthrough with zero anchor");

	/* single-edge anchors -> that edge */
	CHECK_U(qdwin_layer_exclusive_edge(T, false, 0), T, "anchor TOP -> T");
	CHECK_U(qdwin_layer_exclusive_edge(B, false, 0), B, "anchor BOTTOM -> B");
	CHECK_U(qdwin_layer_exclusive_edge(L, false, 0), L, "anchor LEFT -> L");
	CHECK_U(qdwin_layer_exclusive_edge(R, false, 0), R, "anchor RIGHT -> R");

	/* three-edge anchors -> opposite of the free edge */
	CHECK_U(qdwin_layer_exclusive_edge(T | L | R, false, 0), T,
		"anchor T|L|R (free=B) -> T");
	CHECK_U(qdwin_layer_exclusive_edge(B | L | R, false, 0), B,
		"anchor B|L|R (free=T) -> B");
	CHECK_U(qdwin_layer_exclusive_edge(L | T | B, false, 0), L,
		"anchor L|T|B (free=R) -> L");
	CHECK_U(qdwin_layer_exclusive_edge(R | T | B, false, 0), R,
		"anchor R|T|B (free=L) -> R");

	/* ambiguous combos -> 0 (NONE) */
	CHECK_U(qdwin_layer_exclusive_edge(T | B, false, 0), 0,
		"anchor T|B (two opposite) -> NONE");
	CHECK_U(qdwin_layer_exclusive_edge(T | L, false, 0), 0,
		"anchor T|L (corner) -> NONE");
	CHECK_U(qdwin_layer_exclusive_edge(0, false, 0), 0,
		"anchor 0 -> NONE");
	CHECK_U(qdwin_layer_exclusive_edge(T | B | L | R, false, 0), 0,
		"anchor all four -> NONE");
}

/* helper: run compute_box and assert the rect */
static void expect_box(const char *label, uint32_t anchor,
		       int32_t dw, int32_t dh,
		       int32_t mt, int32_t mr, int32_t mb, int32_t ml,
		       int32_t bx, int32_t by, int32_t bw, int32_t bh,
		       int32_t ex, int32_t ey, uint32_t ew, uint32_t eh)
{
	int32_t x = -12345, y = -12345;
	uint32_t w = 0xDEAD, h = 0xBEEF;
	qdwin_layer_compute_box(anchor, dw, dh, mt, mr, mb, ml,
				bx, by, bw, bh, &x, &y, &w, &h);
	CHECK(x == ex && y == ey && w == ew && h == eh,
	      "%s: got (x=%d y=%d w=%u h=%u) want (x=%d y=%d w=%u h=%u)",
	      label, x, y, (unsigned)w, (unsigned)h,
	      ex, ey, (unsigned)ew, (unsigned)eh);
}

/* ---- compute_box: representative cases ---- */
static void test_compute_box(void)
{
	const uint32_t T = QDWIN_LOGIC_ANCHOR_TOP;
	const uint32_t B = QDWIN_LOGIC_ANCHOR_BOTTOM;
	const uint32_t L = QDWIN_LOGIC_ANCHOR_LEFT;
	const uint32_t R = QDWIN_LOGIC_ANCHOR_RIGHT;
	/* bounding box: full 1920x1080 output at origin */
	const int32_t BX = 0, BY = 0, BW = 1920, BH = 1080;

	/* Full-width top bar: anchored T|L|R, width 0 (stretch), height 32.
	 * x = BX + ml(0) = 0; w = BW - 0 = 1920; anchored top: y = BY + mt(0)
	 * = 0; h = 32. */
	expect_box("full-width top bar (T|L|R, w=0,h=32)",
		   T | L | R, 0, 32, 0, 0, 0, 0,
		   BX, BY, BW, BH,
		   0, 0, 1920, 32);

	/* Full-width bottom bar with bottom margin 10, height 40.
	 * y = BY + BH - h - mb = 0 + 1080 - 40 - 10 = 1030. */
	expect_box("full-width bottom bar (B|L|R, h=40, mb=10)",
		   B | L | R, 0, 40, 0, 0, 10, 0,
		   BX, BY, BW, BH,
		   0, 1030, 1920, 40);

	/* Anchored top-right corner: 300x200, margins t=8 r=16.
	 * a&R (not L): x = BX + BW - w - mr = 1920 - 300 - 16 = 1604.
	 * a&T: y = BY + mt = 8. */
	expect_box("anchored top-right corner (T|R, 300x200, mt=8 mr=16)",
		   T | R, 300, 200, 8, 16, 0, 0,
		   BX, BY, BW, BH,
		   1604, 8, 300, 200);

	/* Anchored bottom-left corner: 100x100, margins b=5 l=7.
	 * a&L: x = BX + ml = 7. a&B: y = BY + BH - h - mb = 1080-100-5=975. */
	expect_box("anchored bottom-left corner (B|L, 100x100, mb=5 ml=7)",
		   B | L, 100, 100, 0, 0, 5, 7,
		   BX, BY, BW, BH,
		   7, 975, 100, 100);

	/* Centered (no anchors), 400x300.
	 * x = BX + BW/2 - w/2 = 960 - 200 = 760.
	 * y = BY + BH/2 - h/2 = 540 - 150 = 390. */
	expect_box("centered (anchor 0, 400x300)",
		   0, 400, 300, 0, 0, 0, 0,
		   BX, BY, BW, BH,
		   760, 390, 400, 300);

	/* Anchored L|R (both horizontal) with fixed width -> centered horiz.
	 * x = BX + BW/2 - w/2 = 960 - 250 = 710. Vertical: a&T only -> top. */
	expect_box("L|R|T fixed-width centered horiz, top vert (500x60)",
		   L | R | T, 500, 60, 4, 0, 0, 0,
		   BX, BY, BW, BH,
		   710, 4, 500, 60);

	/* Exclusive-margin style full-height left strip: anchored T|B|L,
	 * width 50, height 0 (stretch), margins t=12 b=20.
	 * a&L: x = BX + ml = 0; width fixed 50.
	 * h==0: y = BY + mt = 12; h = BH - (mt+mb) = 1080 - 32 = 1048. */
	expect_box("full-height left strip (T|B|L, w=50,h=0, mt=12 mb=20)",
		   T | B | L, 50, 0, 12, 0, 20, 0,
		   BX, BY, BW, BH,
		   0, 12, 50, 1048);

	/* Negative-clamp: width 0 stretch but margins exceed bounds.
	 * Horizontal: w = bw - (ml+mr) = 100 - 160 = -60 -> clamped to 0;
	 *   x = bx + ml = 80.
	 * Vertical: anchor L only (no T/B), h=10 -> else branch centers:
	 *   y = by + bh/2 - h/2 = 540 - 5 = 535. */
	expect_box("negative width clamps to 0",
		   L, 0, 10, 0, 80, 0, 80,
		   0, 0, 100, 1080,
		   80, 535, 0, 10);

	/* Non-zero bounding origin (usable area offset by exclusive zones).
	 * Top bar anchored T|L|R inside box at (0,40)-1920x1040.
	 * x = bx + ml = 0; w = bw = 1920; y = by + mt = 40; h = 30. */
	expect_box("top bar inside offset bounds (by=40)",
		   T | L | R, 0, 30, 0, 0, 0, 0,
		   0, 40, 1920, 1040,
		   0, 40, 1920, 30);

	/* ODD-dimension centered (no anchors), 301x101. Guards against a
	 * `bw/2 - w/2` vs `(bw-w)/2` off-by-one that EVEN dims hide:
	 * x = bx + bw/2 - w/2 = 0 + 960 - 150 = 810  (301/2 == 150, truncated).
	 * y = by + bh/2 - h/2 = 0 + 540 - 50  = 490  (101/2 == 50, truncated).
	 * Note (bw-w)/2 = (1920-301)/2 = 809 != 810, so the kernel's
	 * round-each-half-then-subtract is distinguishable here. */
	expect_box("ODD centered (anchor 0, 301x101)",
		   0, 301, 101, 0, 0, 0, 0,
		   BX, BY, BW, BH,
		   810, 490, 301, 101);

	/* T|B fixed-height -> vertical CENTERING (a&T && a&B branch), odd
	 * height to exercise the /2 truncation. Anchor also has L so the
	 * horizontal axis is left-anchored (not centered) — this isolates the
	 * vertical-centering path and catches an axis transposition.
	 * Horizontal: a&L (not R) -> x = bx + ml = 15.
	 * Vertical: a&T && a&B -> y = by + bh/2 - h/2 = 0 + 540 - 100 = 440
	 *           (201/2 == 100, truncated). h stays 201. */
	expect_box("T|B|L fixed-height vertical-centered (300x201, ml=15)",
		   T | B | L, 300, 201, 0, 0, 0, 15,
		   BX, BY, BW, BH,
		   15, 440, 300, 201);

	/* Horizontal STRETCH (w==0) with NONZERO left/right margins. Guards
	 * against margins being applied on the wrong axis: the vertical margins
	 * (mt/mb) must NOT influence width/x, and ml/mr must NOT influence y.
	 * Anchor T|L|R; w=0 -> stretch, h=50.
	 * Horizontal: w==0 -> x = bx + ml = 0 + 25 = 25;
	 *             w = bw - (ml + mr) = 1920 - (25 + 35) = 1860.
	 * Vertical: a&T -> y = by + mt = 0 + 12 = 12; h = 50. */
	expect_box("horizontal stretch with L/R margins (w=0,h=50, mt=12 mr=35 ml=25)",
		   T | L | R, 0, 50, 12, 35, 0, 25,
		   BX, BY, BW, BH,
		   25, 12, 1860, 50);
}

/* ---- fractional-scale clamp + env validity ---- */
static void test_fractional_scale(void)
{
	/* clamp: below min */
	CHECK_U(qdwin_clamp_fractional_scale_120(0), 30,
		"clamp 0 -> 30 (min)");
	CHECK_U(qdwin_clamp_fractional_scale_120(29), 30,
		"clamp 29 -> 30 (min)");
	/* clamp: boundaries */
	CHECK_U(qdwin_clamp_fractional_scale_120(30), 30, "clamp 30 -> 30");
	CHECK_U(qdwin_clamp_fractional_scale_120(960), 960, "clamp 960 -> 960");
	/* clamp: above max */
	CHECK_U(qdwin_clamp_fractional_scale_120(961), 960,
		"clamp 961 -> 960 (max)");
	CHECK_U(qdwin_clamp_fractional_scale_120(100000), 960,
		"clamp 100000 -> 960 (max)");
	/* clamp: typical in-range values pass through */
	CHECK_U(qdwin_clamp_fractional_scale_120(120), 120, "clamp 120 (1.0x)");
	CHECK_U(qdwin_clamp_fractional_scale_120(180), 180, "clamp 180 (1.5x)");
	CHECK_U(qdwin_clamp_fractional_scale_120(240), 240, "clamp 240 (2.0x)");

	/* env validity gate */
	CHECK(qdwin_fractional_scale_env_valid(30), "env 30 valid");
	CHECK(qdwin_fractional_scale_env_valid(150), "env 150 valid");
	CHECK(qdwin_fractional_scale_env_valid(960), "env 960 valid");
	CHECK(!qdwin_fractional_scale_env_valid(29), "env 29 invalid");
	CHECK(!qdwin_fractional_scale_env_valid(961), "env 961 invalid");
	CHECK(!qdwin_fractional_scale_env_valid(0), "env 0 invalid");
	CHECK(!qdwin_fractional_scale_env_valid(-5), "env -5 invalid");
}

/* ---- decorated-toplevel inset inner-extent clamp ---- */
static void test_inset_inner_extent(void)
{
	/* Typical case: insets fit comfortably inside the outer extent. */
	CHECK(qdwin_inset_inner_extent(800, 4, 4) == 792,
	      "800 - 4 - 4 -> 792: got %d",
	      qdwin_inset_inner_extent(800, 4, 4));
	CHECK(qdwin_inset_inner_extent(600, 28, 4) == 568,
	      "600 - 28 - 4 -> 568 (top chrome + thin border): got %d",
	      qdwin_inset_inner_extent(600, 28, 4));

	/* Zero insets pass the outer extent through unchanged. */
	CHECK(qdwin_inset_inner_extent(1024, 0, 0) == 1024,
	      "1024 - 0 - 0 -> 1024: got %d",
	      qdwin_inset_inner_extent(1024, 0, 0));

	/* Boundary: insets sum to exactly outer-1 -> inner is the minimum
	 * positive size of 1, not 0. */
	CHECK(qdwin_inset_inner_extent(32, 16, 15) == 1,
	      "32 - 16 - 15 -> 1 (boundary): got %d",
	      qdwin_inset_inner_extent(32, 16, 15));

	/* The maximized panel-reflow regression: insets EQUAL the work-area
	 * extent. Without the clamp this shipped a degenerate 0 to the
	 * client (qdwin.c qdwin_panels_on_output_change); it must floor to 1. */
	CHECK(qdwin_inset_inner_extent(32, 16, 16) == 1,
	      "32 - 16 - 16 -> 1 (insets == outer, was 0): got %d",
	      qdwin_inset_inner_extent(32, 16, 16));

	/* Insets EXCEED the available extent (panels reflowed below the
	 * chrome height) — would have been negative; clamps up to 1. */
	CHECK(qdwin_inset_inner_extent(20, 30, 10) == 1,
	      "20 - 30 - 10 -> 1 (insets exceed outer, was -20): got %d",
	      qdwin_inset_inner_extent(20, 30, 10));

	/* Degenerate outer extent of 0 (work area collapsed) still floors
	 * to 1 rather than going negative on any positive inset. */
	CHECK(qdwin_inset_inner_extent(0, 0, 0) == 1,
	      "0 - 0 - 0 -> 1 (collapsed outer): got %d",
	      qdwin_inset_inner_extent(0, 0, 0));
}

/* Ensures XWayland CSD/shadow deltas do not grow a window on every
 * maximize/restore configure round-trip. */
static void test_client_extent_for_geometry(void)
{
	CHECK(qdwin_client_extent_for_geometry(1009, 1073, 1009, 0) == 945,
	      "Chromium visible 1009 with 64px surface delta configures 945: got %d",
	      qdwin_client_extent_for_geometry(1009, 1073, 1009, 0));
	CHECK(qdwin_client_extent_for_geometry(1332, 1384, 1332, 0) == 1280,
	      "Firefox visible 1332 with 52px surface delta configures 1280: got %d",
	      qdwin_client_extent_for_geometry(1332, 1384, 1332, 0));
	CHECK(qdwin_client_extent_for_geometry(800, 800, 800, 0) == 800,
	      "native Wayland zero-delta extent passes through: got %d",
	      qdwin_client_extent_for_geometry(800, 800, 800, 0));
	CHECK(qdwin_client_extent_for_geometry(32, 96, 32, 0) == 1,
	      "oversize frame delta clamps configure to one pixel: got %d",
	      qdwin_client_extent_for_geometry(32, 96, 32, 0));
	CHECK(qdwin_client_extent_for_geometry(640, 600, 640, 0) == 640,
	      "negative/stale delta is ignored: got %d",
	      qdwin_client_extent_for_geometry(640, 600, 640, 0));
	CHECK(qdwin_client_extent_for_geometry(1280, 1114, 1114, 64) == 1216,
	      "learned Chromium frame delta corrects an otherwise invisible extent: got %d",
	      qdwin_client_extent_for_geometry(1280, 1114, 1114, 64));
	CHECK(qdwin_client_extent_for_geometry(1280, 1332, 1280, 64) == 1216,
	      "larger learned delta wins over a smaller live geometry delta: got %d",
	      qdwin_client_extent_for_geometry(1280, 1332, 1280, 64));
}

static void test_committed_restore_extent(void)
{
	CHECK(qdwin_committed_restore_extent(true, 676, 612) == 676,
	      "XWayland restore preserves the shell-visible outer extent: got %d",
	      qdwin_committed_restore_extent(true, 676, 612));
	CHECK(qdwin_committed_restore_extent(false, 676, 612) == 612,
	      "native Wayland restore uses desktop geometry: got %d",
	      qdwin_committed_restore_extent(false, 676, 612));
	CHECK(qdwin_committed_restore_extent(true, 0, 612) == 612,
	      "XWayland restore falls back to geometry before its first buffer: got %d",
	      qdwin_committed_restore_extent(true, 0, 612));
	CHECK(qdwin_committed_restore_extent(false, 676, 0) == 676,
	      "native restore falls back to surface without geometry: got %d",
	      qdwin_committed_restore_extent(false, 676, 0));
}

static void test_rehome_outer_rect(void)
{
	int32_t x = 0, y = 0;

	qdwin_rehome_outer_rect(2200, 300, 800, 600,
				100, 40, 1920, 1040, &x, &y);
	CHECK(x == 1220, "rehome clamps floating window at right edge: got %d", x);
	CHECK(y == 300, "rehome preserves an in-range vertical position: got %d", y);

	qdwin_rehome_outer_rect(-900, -700, 800, 600,
				100, 40, 1920, 1040, &x, &y);
	CHECK(x == 100, "rehome clamps floating window at left edge: got %d", x);
	CHECK(y == 40, "rehome clamps floating window at top edge: got %d", y);

	qdwin_rehome_outer_rect(INT32_MAX, INT32_MAX, 4000, 3000,
				1000, 2000, 800, 600, &x, &y);
	CHECK(x == 1000, "oversize rehome keeps leading edge reachable: got %d", x);
	CHECK(y == 2000, "oversize rehome keeps title edge reachable: got %d", y);

	qdwin_rehome_outer_rect(INT32_MIN, INT32_MIN, 0, -1,
				INT32_MAX, INT32_MAX, 0, 0, &x, &y);
	CHECK(x == INT32_MAX,
	      "degenerate rehome extent is overflow safe (x): got %d", x);
	CHECK(y == INT32_MAX,
	      "degenerate rehome extent is overflow safe (y): got %d", y);
}

/* ---- advertised-global visibility matrix (02/S1, the §4b finding) ----
 *
 * Enumerate every (credential class × global kind) cell so a regression —
 * most importantly a silo (SECCTX) client gaining a keystroke/capture global,
 * or an ordinary client gaining the secctx manager — is a mechanical failure.
 */
static void test_global_visibility(void)
{
	/* Ordinary (non-privileged) globals: visible to everyone. */
	CHECK(qdwin_global_visible(QDWIN_CRED_SHELL, QDWIN_GLOBAL_ORDINARY),
	      "ordinary global visible to shell");
	CHECK(qdwin_global_visible(QDWIN_CRED_ORDINARY, QDWIN_GLOBAL_ORDINARY),
	      "ordinary global visible to ordinary");
	CHECK(qdwin_global_visible(QDWIN_CRED_SECCTX, QDWIN_GLOBAL_ORDINARY),
	      "ordinary global visible to secctx/silo");

	/* The two keystroke-privileged globals (IME + virtual keyboard): visible
	 * to shell + ordinary, HIDDEN from a sandboxed silo client (the bind
	 * handlers apply the further allowed_ime_uid pin on top). */
	const enum qdwin_global_kind kbd[] = {
		QDWIN_GLOBAL_INPUT_METHOD,
		QDWIN_GLOBAL_VIRTUAL_KEYBOARD,
	};
	for (size_t i = 0; i < sizeof(kbd) / sizeof(kbd[0]); i++) {
		CHECK(qdwin_global_visible(QDWIN_CRED_SHELL, kbd[i]),
		      "kbd kind %u visible to shell", (unsigned)kbd[i]);
		CHECK(qdwin_global_visible(QDWIN_CRED_ORDINARY, kbd[i]),
		      "kbd kind %u visible to ordinary", (unsigned)kbd[i]);
		/* THE security invariant: a silo client must never see it. */
		CHECK(!qdwin_global_visible(QDWIN_CRED_SECCTX, kbd[i]),
		      "kbd kind %u HIDDEN from secctx/silo", (unsigned)kbd[i]);
	}

	/* weston_capture_v1 — the §4b screen-capture path. Gated TIGHTEST
	 * (shell-only): libweston binds it with no uid/exe pin, so it must be
	 * denied to ordinary AND silo clients, not just silos. */
	CHECK(qdwin_global_visible(QDWIN_CRED_SHELL,
				   QDWIN_GLOBAL_WESTON_CAPTURE),
	      "weston_capture_v1 visible to shell");
	CHECK(!qdwin_global_visible(QDWIN_CRED_ORDINARY,
				    QDWIN_GLOBAL_WESTON_CAPTURE),
	      "weston_capture_v1 denied to an ordinary uid client (no bind pin)");
	CHECK(!qdwin_global_visible(QDWIN_CRED_SECCTX,
				    QDWIN_GLOBAL_WESTON_CAPTURE),
	      "weston_capture_v1 denied to a silo client (the §4b gate)");

	/* secctx manager: only the shell/authorized helper may even see it. */
	CHECK(qdwin_global_visible(QDWIN_CRED_SHELL,
				   QDWIN_GLOBAL_SECCTX_MANAGER),
	      "secctx manager visible to shell");
	CHECK(!qdwin_global_visible(QDWIN_CRED_ORDINARY,
				    QDWIN_GLOBAL_SECCTX_MANAGER),
	      "secctx manager hidden from ordinary uid client");
	CHECK(!qdwin_global_visible(QDWIN_CRED_SECCTX,
				    QDWIN_GLOBAL_SECCTX_MANAGER),
	      "secctx manager hidden from secctx/silo client");

	/* ext_idle_notifier_v1 exposes whole-session idle/resume, which is a
	 * presence/activity side-channel across silo boundaries. It remains
	 * visible to trusted session components but hidden from secctx clients. */
	CHECK(qdwin_global_visible(QDWIN_CRED_SHELL,
				   QDWIN_GLOBAL_IDLE_NOTIFIER),
	      "ext_idle_notifier_v1 visible to shell");
	CHECK(qdwin_global_visible(QDWIN_CRED_ORDINARY,
				   QDWIN_GLOBAL_IDLE_NOTIFIER),
	      "ext_idle_notifier_v1 visible to ordinary session clients");
	CHECK(!qdwin_global_visible(QDWIN_CRED_SECCTX,
				    QDWIN_GLOBAL_IDLE_NOTIFIER),
	      "ext_idle_notifier_v1 hidden from secctx/silo clients");

	/* weston_touch_calibration drives global touch-input calibration / touch
	 * grab — a cross-silo input-integrity surface. Hidden from sandboxed silo
	 * clients; visible to the shell and ordinary admin-session tools (02/S1). */
	CHECK(qdwin_global_visible(QDWIN_CRED_SHELL,
				   QDWIN_GLOBAL_TOUCH_CALIBRATION),
	      "weston_touch_calibration visible to shell");
	CHECK(qdwin_global_visible(QDWIN_CRED_ORDINARY,
				   QDWIN_GLOBAL_TOUCH_CALIBRATION),
	      "weston_touch_calibration visible to ordinary session clients");
	CHECK(!qdwin_global_visible(QDWIN_CRED_SECCTX,
				    QDWIN_GLOBAL_TOUCH_CALIBRATION),
	      "weston_touch_calibration hidden from secctx/silo clients");

	/* Matrix-level fail-closed default: a kind enum value with no policy row
	 * must be DENIED to every credential class. NOTE this is the MATRIX
	 * default only — it does NOT mean a brand-new privileged libweston global
	 * fails closed at the live filter: qdwin_classify_global returns
	 * QDWIN_GLOBAL_ORDINARY (visible to all) for any global it does not
	 * recognise by pointer identity, so an unrecognised new global is visible
	 * until given a classify row. See todo/fable-release 02-security-gate.md
	 * S1 (classify-ORDINARY default) + the threat-model residual register. */
	CHECK(!qdwin_global_visible(QDWIN_CRED_SHELL,
				    (enum qdwin_global_kind)99),
	      "unknown global kind denied even to shell (fail closed)");
	CHECK(!qdwin_global_visible(QDWIN_CRED_ORDINARY,
				    (enum qdwin_global_kind)99),
	      "unknown global kind denied to ordinary (fail closed)");
	CHECK(!qdwin_global_visible(QDWIN_CRED_SECCTX,
				    (enum qdwin_global_kind)99),
	      "unknown global kind denied to secctx (fail closed)");
}

/* Ensures: the production tier-2 nested compositor can publish its window,
 * while another sandbox application cannot gain the nested-manager global by
 * merely carrying a security context or a weston-looking basename. */
static void test_nested_secctx_publisher_identity(void)
{
	CHECK(qdwin_nested_secctx_publisher_allowed(
		      "qdistro.tier2", "/usr/bin/weston"),
	      "tier2 inner /usr/bin/weston is an authorized nested publisher");
	CHECK(!qdwin_nested_secctx_publisher_allowed(
		       "qdistro.tier1", "/usr/bin/weston"),
	      "other sandbox engines cannot publish nested toplevels");
	CHECK(!qdwin_nested_secctx_publisher_allowed(
		       "qdistro.tier2", "/usr/bin/weston-terminal"),
	      "tier2 workload executable cannot bind the nested manager");
	CHECK(!qdwin_nested_secctx_publisher_allowed(
		       "qdistro.tier2", "weston"),
	      "basename-only weston identity is rejected");
	CHECK(!qdwin_nested_secctx_publisher_allowed("qdistro.tier2", ""),
	      "unreadable nested publisher executable is rejected");
	CHECK(!qdwin_nested_secctx_publisher_allowed(NULL, "/usr/bin/weston"),
	      "missing sandbox engine is rejected");
}

static void test_nested_pixelfeed_peer_identity(void)
{
	CHECK(qdwin_nested_pixelfeed_peer_allowed(
		      "/usr/bin/qdistro-nested-pixelfeed"),
	      "root-installed nested pixelfeed helper is recognized");
	CHECK(qdwin_nested_pixelfeed_peer_allowed(
		      "/usr/bin/qdistro-mm-remote-pixelfeed"),
	      "root-installed remote pixelfeed helper is recognized");
	CHECK(!qdwin_nested_pixelfeed_peer_allowed(NULL),
	      "missing pixelfeed executable is rejected");
	CHECK(!qdwin_nested_pixelfeed_peer_allowed(""),
	      "empty pixelfeed executable is rejected");
	CHECK(!qdwin_nested_pixelfeed_peer_allowed(
		       "qdistro-nested-pixelfeed"),
	      "basename-only pixelfeed identity is rejected");
	CHECK(!qdwin_nested_pixelfeed_peer_allowed(
		       "/tmp/qdistro-nested-pixelfeed"),
	      "lookalike pixelfeed path is rejected");
	CHECK(!qdwin_nested_pixelfeed_peer_allowed(
		       "/tmp/qdistro-mm-remote-pixelfeed"),
	      "lookalike remote pixelfeed path is rejected");
}

static void test_remote_nested_publisher_identity(void)
{
	CHECK(qdwin_remote_nested_publisher_allowed(
		      "/usr/bin/qdistro-mm-remote-viewer-helper"),
	      "root-installed remote viewer helper is recognized");
	CHECK(!qdwin_remote_nested_publisher_allowed(NULL),
	      "missing remote publisher executable is rejected");
	CHECK(!qdwin_remote_nested_publisher_allowed(
		       "qdistro-mm-remote-viewer-helper"),
	      "basename-only remote publisher is rejected");
	CHECK(!qdwin_remote_nested_publisher_allowed(
		       "/tmp/qdistro-mm-remote-viewer-helper"),
	      "remote publisher path lookalike is rejected");
	CHECK(!qdwin_remote_nested_publisher_allowed("/usr/bin/weston"),
	      "ordinary nested publisher cannot attach remote identity");
}

/* Ensures: a delayed HUP/readable callback for a replaced nested input peer
 * cannot close or inject through the currently-owned peer connection. */
static void test_nested_input_peer_event_identity(void)
{
	CHECK(qdwin_nested_input_peer_event_current(42, 42),
	      "current nested input peer event is accepted");
	CHECK(!qdwin_nested_input_peer_event_current(41, 42),
	      "replaced nested input peer event is rejected");
	CHECK(!qdwin_nested_input_peer_event_current(-1, 42),
	      "invalid nested input event fd is rejected");
	CHECK(!qdwin_nested_input_peer_event_current(42, -1),
	      "nested input event is rejected after current peer teardown");
}

static void test_cursor_sprite_peer_identity(void)
{
	CHECK(qdwin_cursor_sprite_peer_allowed(
		      "/usr/bin/qdistro-cursor-sprites"),
	      "root-installed cursor-sprites helper is recognized");
	CHECK(!qdwin_cursor_sprite_peer_allowed(NULL),
	      "missing cursor-sprites executable is rejected");
	CHECK(!qdwin_cursor_sprite_peer_allowed(""),
	      "empty cursor-sprites executable is rejected");
	CHECK(!qdwin_cursor_sprite_peer_allowed(
		       "qdistro-cursor-sprites"),
	      "basename-only cursor-sprites identity is rejected");
	CHECK(!qdwin_cursor_sprite_peer_allowed(
		       "/tmp/qdistro-cursor-sprites"),
	      "lookalike cursor-sprites path is rejected");
	CHECK(!qdwin_cursor_sprite_peer_allowed(
		       "/usr/bin/qdistro-nested-pixelfeed"),
	      "pixelfeed helper is not a cursor-sprites helper");
}

static void test_selection_serial_stale(void)
{
	CHECK(!qdwin_selection_serial_is_stale(0, 0, false),
	      "first set is never stale, even at serial 0");
	CHECK(!qdwin_selection_serial_is_stale(5, 0, false),
	      "first set ignores the stored serial");
	CHECK(qdwin_selection_serial_is_stale(5, 0, true),
	      "serial 0 is stale after a newer selection");
	CHECK(qdwin_selection_serial_is_stale(10, 9, true),
	      "older serial is stale");
	CHECK(qdwin_selection_serial_is_stale(10, 10, true),
	      "equal serial is stale (weston: current - incoming == 0)");
	CHECK(!qdwin_selection_serial_is_stale(10, 11, true),
	      "newer serial is accepted");
	CHECK(!qdwin_selection_serial_is_stale(0, 1, true),
	      "serial 1 is newer than 0");
}

/* ---- deliberate fail-open / broad-trust pins (02/S13) ----
 *
 * These tests pin explicit risk-register entries rather than asserting ideal
 * policy. If any row changes, the implementation must be deliberately closed
 * or the documented residual risk must be updated.
 */
static void test_s13_fail_open_pins(void)
{
	const uid_t admin = (uid_t)1000;
	const uid_t other = (uid_t)1001;
	const pid_t shell_pid = (pid_t)4242;
	const pid_t other_pid = (pid_t)5252;

	CHECK(qdwin_om_mutation_allowed(true, true, other_pid, other,
					shell_pid, admin, admin),
	      "S13 output-manager: bound shell may mutate");
	CHECK(qdwin_om_mutation_allowed(false, true, shell_pid, admin,
					shell_pid, admin, admin),
	      "S13 output-manager: same shell pid+uid second connection may mutate");
	CHECK(!qdwin_om_mutation_allowed(false, true, shell_pid, other,
					 shell_pid, admin, admin),
	      "S13 output-manager: shell-bound pid with wrong uid denied");
	/* The security-critical post-shell tightening: once a shell is bound,
	 * a client carrying the right uid but a DIFFERENT pid is denied. This is
	 * the only cell that discriminates the shell_bound branch from the
	 * pre-shell allowed_uid fallback (deleting that branch would let this
	 * client through via the uid==allowed_uid path). */
	CHECK(!qdwin_om_mutation_allowed(false, true, other_pid, admin,
					 shell_pid, admin, admin),
	      "S13 output-manager: shell-bound, allowed uid but wrong pid denied");
	/* And shell-bound must close the allowed_uid==-1 open posture, not
	 * inherit it: a non-shell client is denied even when allowed_uid is -1. */
	CHECK(!qdwin_om_mutation_allowed(false, true, other_pid, other,
					 shell_pid, admin, (uid_t)-1),
	      "S13 output-manager: shell-bound closes the -1 open posture");
	CHECK(qdwin_om_mutation_allowed(false, false, other_pid, admin,
					0, (uid_t)-1, admin),
	      "S13 output-manager: pre-shell allowed_uid may mutate");
	CHECK(!qdwin_om_mutation_allowed(false, false, other_pid, other,
					 0, (uid_t)-1, admin),
	      "S13 output-manager: pre-shell non-allowed uid denied");
	CHECK(qdwin_om_mutation_allowed(false, false, other_pid, other,
					0, (uid_t)-1, (uid_t)-1),
	      "S13 output-manager: allowed_uid=-1 preserves open test posture");

	CHECK(qdwin_secctx_root_launcher_attested(0, 111, 111, "runuser"),
	      "S13 secctx: known root launcher basename accepted");
	/* Pin the full accept-list, not just runuser — widening it (e.g. adding
	 * "bash") must be a deliberate, test-visible change. */
	CHECK(qdwin_secctx_root_launcher_attested(0, 111, 111, "su"),
	      "S13 secctx: su accepted");
	CHECK(qdwin_secctx_root_launcher_attested(0, 111, 111, "sudo"),
	      "S13 secctx: sudo accepted");
	CHECK(qdwin_secctx_root_launcher_attested(0, 111, 111, "pkexec"),
	      "S13 secctx: pkexec accepted");
	CHECK(qdwin_secctx_root_launcher_attested(0, 111, 111, ""),
	      "S13 secctx: unreadable root launcher basename accepted with stable root parent");
	CHECK(!qdwin_secctx_root_launcher_attested(other, 111, 111, ""),
	      "S13 secctx: unreadable non-root parent denied");
	CHECK(!qdwin_secctx_root_launcher_attested(0, 0, 111, ""),
	      "S13 secctx: unreadable parent with missing starttime denied");
	CHECK(!qdwin_secctx_root_launcher_attested(0, 111, 222, ""),
	      "S13 secctx: unreadable parent with changed starttime denied");
	/* Both starttime reads failing (0/0) must deny: before==after is true for
	 * 0/0, so this cell is what discriminates the `start_before != 0 &&
	 * start_after != 0` conjuncts from the stability check — without it,
	 * deleting both !=0 guards passes the suite (fable M3 mutation M5). */
	CHECK(!qdwin_secctx_root_launcher_attested(0, 0, 0, ""),
	      "S13 secctx: double starttime-read failure (0/0) denied");
	CHECK(!qdwin_secctx_root_launcher_attested(0, 111, 111, "sh"),
	      "S13 secctx: readable non-launcher basename denied");
	CHECK(!qdwin_secctx_root_launcher_attested(0, 111, 111, NULL),
	      "S13 secctx: NULL basename denied");

	CHECK(qdwin_layershell_pre_shell_uid_allowed(admin, admin),
	      "S13 layer-shell: pre-shell allowed_uid may bind");
	CHECK(!qdwin_layershell_pre_shell_uid_allowed(other, admin),
	      "S13 layer-shell: pre-shell non-allowed uid denied");
	CHECK(!qdwin_layershell_pre_shell_uid_allowed(admin, (uid_t)-1),
	      "S13 layer-shell: allowed_uid=-1 does not create an open bind");

	/* iso2/11 E2: idle-inhibit holds only while the surface is mapped.
	 * The unmapped-dummy case is the hole this closes; the mapped case
	 * is the product feature (video in a silo keeps the session awake)
	 * and must keep working. */
	CHECK(qdwin_idle_inhibit_should_hold(true, true, true, true),
	      "E2 idle-inhibit: mapped surface with a buffer and a view holds");
	CHECK(!qdwin_idle_inhibit_should_hold(true, false, true, true),
	      "E2 idle-inhibit: never-mapped surface holds nothing");
	CHECK(!qdwin_idle_inhibit_should_hold(true, true, false, true),
	      "E2 idle-inhibit: bufferless surface holds nothing even if the "
	      "map bit reads true (stale-bit case)");
	CHECK(!qdwin_idle_inhibit_should_hold(true, true, true, false),
	      "E2 idle-inhibit: viewless surface holds nothing even if mapped "
	      "and buffered (destroyed-subsurface-role case)");
	CHECK(!qdwin_idle_inhibit_should_hold(false, true, true, true),
	      "E2 idle-inhibit: destroyed surface holds nothing");
	CHECK(!qdwin_idle_inhibit_should_hold(false, false, false, false),
	      "E2 idle-inhibit: nothing present, nothing held");
}

/* ---- xdg_popup constraint kernel (M1 security fix) ----
 *
 * qdwin_xdg_constrain_geometry places + clamps an xdg_popup so it cannot
 * leave the parent's output. Each case exercises one property; CHECK_GEOM
 * asserts the full rect. */
#define CHECK_GEOM(gx, gy, gw, gh, ex, ey, ew, eh, label)                  \
	CHECK((gx) == (ex) && (gy) == (ey) && (gw) == (ew) && (gh) == (eh), \
	      "%s: got (%d,%d %dx%d), want (%d,%d %dx%d)",                  \
	      (label), (int)(gx), (int)(gy), (int)(gw), (int)(gh),         \
	      (int)(ex), (int)(ey), (int)(ew), (int)(eh))

static void test_popup_constrain(void)
{
	int32_t x, y, w, h;

	/* A. Base placement, bounds large + NONE: equals the unconstrained
	 *    anchor/gravity result (pins the kernel against get_geometry).
	 *    anchor BOTTOM_LEFT + gravity BOTTOM_RIGHT = grow down-right from
	 *    the anchor rect's bottom-left, the common menu case. */
	qdwin_xdg_constrain_geometry(100, 50, 20, 10, 40, 30, 0, 0,
				     QDWIN_PC_ANCHOR_BOTTOM_LEFT,
				     QDWIN_PC_GRAVITY_BOTTOM_RIGHT,
				     QDWIN_PC_ADJUST_NONE,
				     0, 0, 10000, 10000, &x, &y, &w, &h);
	CHECK_GEOM(x, y, w, h, 100, 60, 40, 30, "base placement, no constraint");

	/* B. Same popup well inside a real output: unchanged. */
	qdwin_xdg_constrain_geometry(100, 50, 20, 10, 40, 30, 0, 0,
				     QDWIN_PC_ANCHOR_BOTTOM_LEFT,
				     QDWIN_PC_GRAVITY_BOTTOM_RIGHT,
				     QDWIN_PC_ADJUST_NONE,
				     0, 0, 500, 500, &x, &y, &w, &h);
	CHECK_GEOM(x, y, w, h, 100, 60, 40, 30, "inside output, unchanged");

	/* C. Overflows the right edge, constraint_adjustment = NONE: the
	 *    MANDATORY clamp slides it back so the right edge meets bounds.
	 *    This is the core security property — NONE does not exempt. */
	qdwin_xdg_constrain_geometry(100, 50, 20, 10, 40, 30, 0, 0,
				     QDWIN_PC_ANCHOR_BOTTOM_LEFT,
				     QDWIN_PC_GRAVITY_BOTTOM_RIGHT,
				     QDWIN_PC_ADJUST_NONE,
				     0, 0, 120, 200, &x, &y, &w, &h);
	CHECK_GEOM(x, y, w, h, 80, 60, 40, 30, "NONE overflow slid inside");

	/* D. FLIP_X: a right-anchored menu overflowing the right edge flips
	 *    to the left of the anchor and fits — legitimate edge submenu. */
	qdwin_xdg_constrain_geometry(100, 50, 0, 0, 40, 30, 0, 0,
				     QDWIN_PC_ANCHOR_TOP_RIGHT,
				     QDWIN_PC_GRAVITY_BOTTOM_RIGHT,
				     QDWIN_PC_ADJUST_FLIP_X,
				     0, 0, 120, 200, &x, &y, &w, &h);
	CHECK_GEOM(x, y, w, h, 60, 50, 40, 30, "FLIP_X edge submenu flips left");

	/* E. Attacker parks the popup far away via a huge offset, NONE: the
	 *    clamp pulls it back inside the output. */
	qdwin_xdg_constrain_geometry(0, 0, 10, 10, 50, 50, 100000, 0,
				     QDWIN_PC_ANCHOR_TOP_LEFT,
				     QDWIN_PC_GRAVITY_BOTTOM_RIGHT,
				     QDWIN_PC_ADJUST_NONE,
				     0, 0, 800, 600, &x, &y, &w, &h);
	CHECK_GEOM(x, y, w, h, 750, 0, 50, 50, "far offset clamped into output");

	/* F. Extreme offset near INT32_MAX: int64 internals, no UB, still
	 *    clamped into the output. */
	qdwin_xdg_constrain_geometry(0, 0, 10, 10, 50, 50, INT32_MAX, 0,
				     QDWIN_PC_ANCHOR_TOP_LEFT,
				     QDWIN_PC_GRAVITY_BOTTOM_RIGHT,
				     QDWIN_PC_ADJUST_NONE,
				     0, 0, 800, 600, &x, &y, &w, &h);
	CHECK_GEOM(x, y, w, h, 750, 0, 50, 50, "INT32_MAX offset clamped, no overflow");

	/* G. Popup larger than the output is shrunk to fit (literal "cannot
	 *    escape"), even without RESIZE_*. */
	qdwin_xdg_constrain_geometry(0, 0, 0, 0, 2000, 30, 0, 0,
				     QDWIN_PC_ANCHOR_TOP_LEFT,
				     QDWIN_PC_GRAVITY_BOTTOM_RIGHT,
				     QDWIN_PC_ADJUST_NONE,
				     0, 0, 800, 600, &x, &y, &w, &h);
	CHECK_GEOM(x, y, w, h, 0, 0, 800, 30, "oversize popup shrunk to bounds");

	/* H. Non-zero bounds origin (output not at global 0,0): clamp uses
	 *    the bounds frame, not absolute 0. Popup at x=900 over an output
	 *    spanning [1000,1800) slides to its left edge. */
	qdwin_xdg_constrain_geometry(900, 1100, 0, 0, 50, 50, 0, 0,
				     QDWIN_PC_ANCHOR_TOP_LEFT,
				     QDWIN_PC_GRAVITY_BOTTOM_RIGHT,
				     QDWIN_PC_ADJUST_NONE,
				     1000, 1000, 800, 600, &x, &y, &w, &h);
	CHECK_GEOM(x, y, w, h, 1000, 1100, 50, 50, "clamp respects bounds origin");
}

int main(void)
{
	test_accel_profile();
	test_scroll_method();
	test_exclusive_edge();
	test_compute_box();
	test_fractional_scale();
	test_inset_inner_extent();
	test_client_extent_for_geometry();
	test_committed_restore_extent();
	test_rehome_outer_rect();
	test_global_visibility();
	test_nested_secctx_publisher_identity();
	test_nested_pixelfeed_peer_identity();
	test_cursor_sprite_peer_identity();
	test_selection_serial_stale();
	test_remote_nested_publisher_identity();
	test_nested_input_peer_event_identity();
	test_s13_fail_open_pins();
	test_popup_constrain();

	printf("\n%d checks, %d failures\n", checks, failures);
	if (failures) {
		printf("RESULT: FAIL\n");
		return 1;
	}
	printf("RESULT: PASS\n");
	return 0;
}
