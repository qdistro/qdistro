/* Retained last-frame capture support — qdistro additions to the VENDORED
 * libweston (libweston-vendored/src/libweston/output-capture.{c,h}), not
 * present in stock libweston-16 or its installed headers.
 *
 * qdwin.c is routinely compiled against the stock system headers (the
 * fresh-vm-bootstrap builds qdwin before the vendored libweston is staged,
 * with -I/usr/include/weston/libweston-16), so these prototypes cannot live
 * in a libweston header qdwin includes. They are declared here WEAK
 * instead: against the vendored runtime the symbols resolve and the
 * feature works; against a stock libweston runtime they are NULL and
 * qdwin_capture_retention_maybe_arm() logs + disables the feature rather
 * than failing the module load. Callers MUST null-check before calling
 * (the arm gate does this once for all of them).
 *
 * weston_output_has_renderer_capture_tasks() is an upstream export, but its
 * declaration lives in the non-installed libweston/output-capture.h, so it
 * is declared here (weak, for uniformity with the availability gate).
 */

#pragma once

#include <libweston/libweston.h>

__attribute__((weak)) void
weston_output_capture_retention_enable(struct weston_output *output);

__attribute__((weak)) void
weston_output_capture_add_task_filed_listener(struct weston_output *output,
					      struct wl_listener *listener);

__attribute__((weak)) bool
weston_output_capture_retained_frame_info(struct weston_output *output,
					  uint32_t *age_ms_out,
					  uint64_t *msc_out);

__attribute__((weak)) int
weston_output_capture_serve_retained(struct weston_output *output);

__attribute__((weak)) bool
weston_output_has_renderer_capture_tasks(struct weston_output *output);
