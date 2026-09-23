/*
 * Copyright (C) 2026 Amazon.com, Inc. or its affiliates
 *
 * Permission is hereby granted, free of charge, to any person obtaining
 * a copy of this software and associated documentation files (the
 * "Software"), to deal in the Software without restriction, including
 * without limitation the rights to use, copy, modify, merge, publish,
 * distribute, sublicense, and/or sell copies of the Software, and to
 * permit persons to whom the Software is furnished to do so, subject to
 * the following conditions:
 *
 * The above copyright notice and this permission notice (including the
 * next paragraph) shall be included in all copies or substantial
 * portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 * EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 * MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 * NONINFRINGEMENT.  IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
 * BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
 * ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
 * CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

#pragma once

#include "weston-trace.h"
#include <libweston/libweston.h>
#include <libweston/libweston-internal.h>

#include "perfetto/u_perfetto.h"

typedef struct { const struct timespec ts; } weston_trace_time_since;

void
perfetto_annotate_int(struct weston_debug_annotations *annots,
		      const char *key,
		      unsigned char key_size,
		      int value);

void
perfetto_annotate_bool(struct weston_debug_annotations *annots,
		      const char *key,
		      unsigned char key_size,
		      bool value);

void
perfetto_annotate_float(struct weston_debug_annotations *annots,
			const char *key,
			unsigned char key_size,
			float value);
void
perfetto_annotate_double(struct weston_debug_annotations *annots,
			const char *key,
			unsigned char key_size,
			double value);

void
perfetto_annotate_string(struct weston_debug_annotations *annots,
			 const char *key,
			 unsigned char key_size,
			 const char *value);

void
perfetto_annotate_buffer(struct weston_debug_annotations *annots,
			 const char *key,
			 unsigned char key_size,
			 const struct weston_buffer *buffer);

void
perfetto_annotate_solid_buffer_values(struct weston_debug_annotations *annots,
				      const char *key,
				      unsigned char key_size,
				      const struct weston_solid_buffer_values *values);

void
perfetto_annotate_flow(struct weston_debug_annotations *annots,
		       const char *key,
		       unsigned char key_size,
		       struct weston_trace_flow *flow);

void
perfetto_annotate_flow_const(struct weston_debug_annotations *annots,
			     const char *key,
			     unsigned char key_size,
			     const struct weston_trace_flow *flow);

void
perfetto_annotate_time_since(struct weston_debug_annotations *annots,
			     const char *key,
			     unsigned char key_size,
			     weston_trace_time_since *since);
