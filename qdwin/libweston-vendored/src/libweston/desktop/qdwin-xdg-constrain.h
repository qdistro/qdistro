/* qdwin-xdg-constrain.h — pure xdg_positioner constraint kernel.
 *
 * qdistro security patch. Vendored libweston 14.0.2 leaves the
 * xdg_positioner constraint_adjustment TODO unimplemented
 * (desktop/xdg-shell.c: weston_desktop_xdg_positioner_get_geometry),
 * so a direct Wayland client (tier 0/1) can position an xdg_popup
 * arbitrarily far from its parent — e.g. parking a spoofed dialog over
 * another silo's window to phish a click. This kernel constrains the
 * popup geometry so it cannot leave the parent toplevel's output.
 *
 * It is a *pure* function: plain ints in, geometry out, no weston/wayland
 * types, no global state. xdg-shell.c includes it directly (same dir) and
 * computes the bounds rectangle from the parent's output; the unit test
 * (tests/unit/test-qdwin-logic.c) includes it too via one include-dir line
 * in meson.build — so the exact code the compositor runs is unit-tested.
 * Single source of truth, no separate link target.
 *
 * Coordinate space: the caller supplies the anchor rect, offset and the
 * bounds rectangle in a SINGLE shared coordinate space. xdg-shell.c uses
 * the parent WINDOW-GEOMETRY frame (popup->geometry is defined relative to
 * the parent window-geometry origin, per the xdg-shell spec and the
 * use_geometry path in surface.c), with the output rect translated into
 * that frame. The unit test uses an arbitrary frame. The result is in the
 * same frame and is written straight to popup->geometry.
 *
 * All internal arithmetic is int64_t: the anchor rect and offset are
 * attacker-controlled int32_t, so int32 placement math could overflow
 * (UB, and a wrong clamp could let a coordinate escape). Inputs/outputs
 * stay int32_t; the final result is saturated to the int32 range.
 *
 * Behaviour, per axis:
 *   1. Base placement = anchor point (per anchor edge) + offset, shifted
 *      by gravity. Mirrors the formulas in get_geometry exactly.
 *   2. If the FLIP flag is set and the base overflows the axis, the
 *      anchor+gravity edges are flipped and re-placed; the flip is adopted
 *      if it reduces (not necessarily eliminates) the overflow.
 *   3. If the RESIZE flag is set and overflow remains, the size is shrunk.
 *   4. MANDATORY slide-and-fit clamp (security backstop, runs regardless of
 *      the adjustment flags): the rect is pulled inside bounds, and if it
 *      is still larger than bounds it is shrunk to fit. The returned
 *      rectangle is therefore ALWAYS fully contained in bounds (for a
 *      bounds of positive size) — a popup can never escape the parent's
 *      output, even with constraint_adjustment = NONE. The SLIDE_* flags
 *      are subsumed by this step.
 */
#ifndef QDWIN_XDG_CONSTRAIN_H
#define QDWIN_XDG_CONSTRAIN_H

#include <stdint.h>
#include <stdbool.h>

/* Mirror of the xdg_shell protocol enum values. xdg-shell.c passes the
 * real enum constants (identical numeric values); the unit test uses
 * these. Kept here so the header has no protocol-header dependency. */
#define QDWIN_PC_ANCHOR_NONE          0u
#define QDWIN_PC_ANCHOR_TOP           1u
#define QDWIN_PC_ANCHOR_BOTTOM        2u
#define QDWIN_PC_ANCHOR_LEFT          3u
#define QDWIN_PC_ANCHOR_RIGHT         4u
#define QDWIN_PC_ANCHOR_TOP_LEFT      5u
#define QDWIN_PC_ANCHOR_BOTTOM_LEFT   6u
#define QDWIN_PC_ANCHOR_TOP_RIGHT     7u
#define QDWIN_PC_ANCHOR_BOTTOM_RIGHT  8u

#define QDWIN_PC_GRAVITY_NONE         0u
#define QDWIN_PC_GRAVITY_TOP          1u
#define QDWIN_PC_GRAVITY_BOTTOM       2u
#define QDWIN_PC_GRAVITY_LEFT         3u
#define QDWIN_PC_GRAVITY_RIGHT        4u
#define QDWIN_PC_GRAVITY_TOP_LEFT     5u
#define QDWIN_PC_GRAVITY_BOTTOM_LEFT  6u
#define QDWIN_PC_GRAVITY_TOP_RIGHT    7u
#define QDWIN_PC_GRAVITY_BOTTOM_RIGHT 8u

#define QDWIN_PC_ADJUST_NONE          0u
#define QDWIN_PC_ADJUST_SLIDE_X       1u
#define QDWIN_PC_ADJUST_SLIDE_Y       2u
#define QDWIN_PC_ADJUST_FLIP_X        4u
#define QDWIN_PC_ADJUST_FLIP_Y        8u
#define QDWIN_PC_ADJUST_RESIZE_X      16u
#define QDWIN_PC_ADJUST_RESIZE_Y      32u

/* --- internal helpers (axis = 0 for X, 1 for Y) --- */

/* True when the anchor/gravity enum has a LEFT/TOP edge on the axis. */
static inline bool
qdwin__edge_low(uint32_t e, int axis)
{
	if (axis == 0)
		return e == QDWIN_PC_ANCHOR_LEFT ||
		       e == QDWIN_PC_ANCHOR_TOP_LEFT ||
		       e == QDWIN_PC_ANCHOR_BOTTOM_LEFT;
	return e == QDWIN_PC_ANCHOR_TOP ||
	       e == QDWIN_PC_ANCHOR_TOP_LEFT ||
	       e == QDWIN_PC_ANCHOR_TOP_RIGHT;
}

/* True when the anchor/gravity enum has a RIGHT/BOTTOM edge on the axis. */
static inline bool
qdwin__edge_high(uint32_t e, int axis)
{
	if (axis == 0)
		return e == QDWIN_PC_ANCHOR_RIGHT ||
		       e == QDWIN_PC_ANCHOR_TOP_RIGHT ||
		       e == QDWIN_PC_ANCHOR_BOTTOM_RIGHT;
	return e == QDWIN_PC_ANCHOR_BOTTOM ||
	       e == QDWIN_PC_ANCHOR_BOTTOM_LEFT ||
	       e == QDWIN_PC_ANCHOR_BOTTOM_RIGHT;
}

/* Reflect an anchor/gravity enum across the given axis (LEFT<->RIGHT for
 * axis 0, TOP<->BOTTOM for axis 1); the orthogonal edge is preserved.
 * NONE values (no edge on the axis) are returned unchanged. The anchor
 * and gravity enums share the same numeric layout, so one mapper serves
 * both. */
static inline uint32_t
qdwin__flip_edge(uint32_t e, int axis)
{
	if (axis == 0) {
		switch (e) {
		case QDWIN_PC_ANCHOR_LEFT:         return QDWIN_PC_ANCHOR_RIGHT;
		case QDWIN_PC_ANCHOR_RIGHT:        return QDWIN_PC_ANCHOR_LEFT;
		case QDWIN_PC_ANCHOR_TOP_LEFT:     return QDWIN_PC_ANCHOR_TOP_RIGHT;
		case QDWIN_PC_ANCHOR_TOP_RIGHT:    return QDWIN_PC_ANCHOR_TOP_LEFT;
		case QDWIN_PC_ANCHOR_BOTTOM_LEFT:  return QDWIN_PC_ANCHOR_BOTTOM_RIGHT;
		case QDWIN_PC_ANCHOR_BOTTOM_RIGHT: return QDWIN_PC_ANCHOR_BOTTOM_LEFT;
		default:                           return e;
		}
	}
	switch (e) {
	case QDWIN_PC_ANCHOR_TOP:          return QDWIN_PC_ANCHOR_BOTTOM;
	case QDWIN_PC_ANCHOR_BOTTOM:       return QDWIN_PC_ANCHOR_TOP;
	case QDWIN_PC_ANCHOR_TOP_LEFT:     return QDWIN_PC_ANCHOR_BOTTOM_LEFT;
	case QDWIN_PC_ANCHOR_BOTTOM_LEFT:  return QDWIN_PC_ANCHOR_TOP_LEFT;
	case QDWIN_PC_ANCHOR_TOP_RIGHT:    return QDWIN_PC_ANCHOR_BOTTOM_RIGHT;
	case QDWIN_PC_ANCHOR_BOTTOM_RIGHT: return QDWIN_PC_ANCHOR_TOP_RIGHT;
	default:                           return e;
	}
}

/* Base placement origin on one axis: anchor point + offset, shifted by
 * gravity. anchor_lo/anchor_size are the anchor rect's low coord and
 * extent on the axis; popup_size is the popup extent. Mirrors get_geometry.
 * int64_t throughout — inputs are attacker-controlled int32_t. */
static inline int64_t
qdwin__place_axis(int axis, uint32_t anchor, uint32_t gravity,
		  int64_t anchor_lo, int64_t anchor_size,
		  int64_t popup_size, int64_t offset)
{
	int64_t p;

	if (qdwin__edge_low(anchor, axis))
		p = anchor_lo;
	else if (qdwin__edge_high(anchor, axis))
		p = anchor_lo + anchor_size;
	else
		p = anchor_lo + anchor_size / 2;

	p += offset;

	if (qdwin__edge_low(gravity, axis))
		p -= popup_size;
	else if (qdwin__edge_high(gravity, axis))
		p -= 0;
	else
		p -= popup_size / 2;

	return p;
}

/* Overflow magnitude of [p, p+s) against [lo, lo+size): how far the rect
 * pokes outside the bounds on either end (0 if fully inside). */
static inline int64_t
qdwin__overflow(int64_t p, int64_t s, int64_t lo, int64_t size)
{
	int64_t hi = lo + size;
	int64_t over = 0;

	if (p < lo)
		over += lo - p;
	if (p + s > hi)
		over += (p + s) - hi;
	return over;
}

/* Constrain a single axis. pos/size are in/out pointers; bounds_lo and
 * bounds_size describe the available span; flip/resize say whether those
 * adjustments are permitted on this axis. */
static inline void
qdwin__constrain_axis(int axis, uint32_t anchor, uint32_t gravity,
		      int64_t anchor_lo, int64_t anchor_size,
		      int64_t offset, bool flip, bool resize,
		      int64_t bounds_lo, int64_t bounds_size,
		      int64_t *pos, int64_t *size)
{
	int64_t bounds_hi = bounds_lo + bounds_size;
	int64_t p = *pos;
	int64_t s = *size;

	/* 1. flip, if permitted and the base overflows this axis. Adopt the
	 *    flipped placement when it pokes outside bounds LESS than the
	 *    original (protocol intent — better edge submenu placement). */
	if (flip && qdwin__overflow(p, s, bounds_lo, bounds_size) > 0) {
		int64_t fp = qdwin__place_axis(axis,
					       qdwin__flip_edge(anchor, axis),
					       qdwin__flip_edge(gravity, axis),
					       anchor_lo, anchor_size, s, offset);
		if (qdwin__overflow(fp, s, bounds_lo, bounds_size) <
		    qdwin__overflow(p, s, bounds_lo, bounds_size))
			p = fp;
	}

	/* 2. resize, if permitted and overflow remains. */
	if (resize) {
		if (p < bounds_lo) {
			s -= (bounds_lo - p);
			p = bounds_lo;
		}
		if (p + s > bounds_hi)
			s = bounds_hi - p;
		if (s < 1)
			s = 1;
	}

	/* 3. mandatory slide-and-fit clamp (security backstop, runs always).
	 *    First shrink anything still larger than bounds so it cannot
	 *    extend past the far edge, then slide inside. The low edge wins
	 *    so the anchor stays visible. Result is fully within bounds. */
	if (bounds_size < 1) {
		/* Degenerate bounds: pin to the low edge with unit size. The
		 * glue never passes this (output extents are positive, and the
		 * fail-closed fallback forces >= 1), but keep the kernel
		 * self-contained-safe if reused with untrusted bounds. */
		p = bounds_lo;
		s = 1;
		*pos = p;
		*size = s;
		return;
	}
	if (s > bounds_size)
		s = bounds_size;
	if (p + s > bounds_hi)
		p = bounds_hi - s;
	if (p < bounds_lo)
		p = bounds_lo;
	if (s < 1)
		s = 1;

	*pos = p;
	*size = s;
}

/* Saturate an int64_t into the int32_t range. */
static inline int32_t
qdwin__sat32(int64_t v)
{
	if (v > INT32_MAX)
		return INT32_MAX;
	if (v < INT32_MIN)
		return INT32_MIN;
	return (int32_t)v;
}

/* Place + constrain an xdg_popup. anchor_rect (ax,ay,aw,ah), popup size
 * (sw,sh), offset (ox,oy), anchor/gravity/adjustment from the positioner,
 * and the bounds rect (bx,by,bw,bh) are all in one shared coordinate
 * space. Writes the final geometry to *out_x/_y/_w/_h.
 *
 * Invariant (bounds of positive size): the output rect lies entirely
 * within bounds — the property the unit test pins. With bounds large
 * enough to contain the base placement and adjustment = NONE, the output
 * equals the unconstrained base placement (pinned against get_geometry). */
static inline void
qdwin_xdg_constrain_geometry(int32_t ax, int32_t ay, int32_t aw, int32_t ah,
			     int32_t sw, int32_t sh,
			     int32_t ox, int32_t oy,
			     uint32_t anchor, uint32_t gravity,
			     uint32_t adjustment,
			     int32_t bx, int32_t by, int32_t bw, int32_t bh,
			     int32_t *out_x, int32_t *out_y,
			     int32_t *out_w, int32_t *out_h)
{
	int64_t x = qdwin__place_axis(0, anchor, gravity, ax, aw, sw, ox);
	int64_t y = qdwin__place_axis(1, anchor, gravity, ay, ah, sh, oy);
	int64_t w = sw < 1 ? 1 : sw;
	int64_t h = sh < 1 ? 1 : sh;

	qdwin__constrain_axis(0, anchor, gravity, ax, aw, ox,
			      adjustment & QDWIN_PC_ADJUST_FLIP_X,
			      adjustment & QDWIN_PC_ADJUST_RESIZE_X,
			      bx, bw, &x, &w);
	qdwin__constrain_axis(1, anchor, gravity, ay, ah, oy,
			      adjustment & QDWIN_PC_ADJUST_FLIP_Y,
			      adjustment & QDWIN_PC_ADJUST_RESIZE_Y,
			      by, bh, &y, &h);

	*out_x = qdwin__sat32(x);
	*out_y = qdwin__sat32(y);
	*out_w = qdwin__sat32(w);
	*out_h = qdwin__sat32(h);
}

#endif /* QDWIN_XDG_CONSTRAIN_H */
