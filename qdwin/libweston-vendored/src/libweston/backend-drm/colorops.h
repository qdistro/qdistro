/*
 * Copyright © 2025-2026 Collabora, Ltd.
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

#include "drm-internal.h"
#include "drm-kms-enums.h"

struct drm_colorop_clut_blob {
	/* drm_device::drm_colorop_clut_blob_list */
	struct wl_list link;
	struct drm_device *device;

	/* Lifetime matches the xform. */
	struct weston_color_transform *xform;
	struct wl_listener destroy_listener;

	uint32_t shaper_len;
	uint32_t clut_len;

	uint32_t shaper_blob_id;
	uint32_t clut_blob_id;
};

struct drm_colorop_matrix_blob {
	/* drm_device::drm_colorop_matrix_blob_list */
	struct wl_list link;
	struct drm_device *device;

	/* Lifetime matches the xform. */
	struct weston_color_transform *xform;
	struct wl_listener destroy_listener;

	uint32_t blob_id;
};

struct drm_colorop {
	struct drm_color_pipeline *pipeline;
	struct wl_list link; /* drm_pipeline::colorop_list */

	enum wdrm_colorop_type type;

	uint32_t id;

	/* Some colorop's can be bypassed. */
	bool can_bypass;

	/* Only useful for 1D and 3D LUT colorop's. */
	uint32_t size;

	/* Holds the properties for the colorop. */
	struct drm_property_info props[WDRM_COLOROP__COUNT];
};

enum colorop_object_type {
	COLOROP_OBJECT_TYPE_CURVE = 0,
	COLOROP_OBJECT_TYPE_MATRIX,
	COLOROP_OBJECT_TYPE_3x1D_LUT,
	COLOROP_OBJECT_TYPE_3D_LUT,
	COLOROP_OBJECT_TYPE_MULTIPLIER,
};

struct drm_colorop_state_object {
	/* Defines which of the below is valid. The others are zero. */
	enum colorop_object_type type;

	enum wdrm_colorop_curve_1d curve;
	uint32_t matrix_blob_id;
	uint32_t lut_3x1d_blob_id;
	uint32_t lut_3d_blob_id;
	uint64_t multiplier;
};

struct drm_colorop_state {
	const struct drm_colorop *colorop;
	/* struct drm_color_pipeline_state::colorop_state_list */
	struct wl_list link;

	/* Object that should be programmed through the colorop. */
	struct drm_colorop_state_object object;
};

struct drm_color_pipeline {
	struct drm_plane *plane;
	struct wl_list colorop_list; /* drm_colorop::link */
	uint32_t id;
};

struct drm_color_pipeline_state {
	const struct drm_color_pipeline *pipeline;
	unsigned int ref_count;

	/* struct drm_colorop_state::link */
	struct wl_list colorop_state_list;
};

#if CAN_OFFLOAD_COLOR_PIPELINE

struct drm_color_pipeline_state *
drm_color_pipeline_state_ref(struct drm_color_pipeline_state *state);

void
drm_color_pipeline_state_unref(struct drm_color_pipeline_state *state);

struct drm_color_pipeline_state *
drm_color_pipeline_state_from_xform(struct drm_plane *plane,
				    struct weston_color_transform *xform,
				    const char *indent);

struct drm_colorop_3x1d_lut_blob *
drm_colorop_3x1d_lut_blob_create(struct drm_device *device,
				 struct weston_color_transform *xform,
				 enum weston_color_curve_step curve_step,
				 enum drm_colorop_3x1d_lut_blob_quantization quantization,
				 struct weston_vec3f *cm_lut, uint32_t lut_len);

const struct drm_colorop_3x1d_lut_blob *
drm_colorop_3x1d_lut_blob_search(const struct drm_device *device,
				 const struct weston_color_transform *xform,
				 enum weston_color_curve_step curve_step,
				 enum drm_colorop_3x1d_lut_blob_quantization quantization,
				 uint32_t lut_len);

const char *
drm_colorop_type_to_str(const struct drm_colorop *colorop);

void
drm_plane_populate_color_pipelines(struct drm_plane *plane,
				   drmModeObjectPropertiesPtr plane_props);

void
drm_plane_release_color_pipelines(struct drm_plane *plane);

#else /* CAN_OFFLOAD_COLOR_PIPELINE */

static inline struct drm_color_pipeline_state *
drm_color_pipeline_state_ref(struct drm_color_pipeline_state *state)
{
	return NULL;
}

static inline void
drm_color_pipeline_state_unref(struct drm_color_pipeline_state *state)
{
}

static inline struct drm_color_pipeline_state *
drm_color_pipeline_state_from_xform(struct drm_plane *plane,
				    struct weston_color_transform *xform,
				    const char *indent)
{
	return NULL;
}

static inline const struct drm_colorop_3x1d_lut_blob *
drm_colorop_3x1d_lut_blob_create(const struct drm_device *device,
				 const struct weston_color_transform *xform,
				 enum weston_color_curve_step curve_step,
				 enum drm_colorop_3x1d_lut_blob_quantization quantization,
				 struct weston_vec3f *cm_lut, uint32_t lut_len)
{
	return NULL;
}

static inline struct drm_colorop_3x1d_lut_blob *
drm_colorop_3x1d_lut_blob_search(struct drm_device *device,
				 struct weston_color_transform *xform,
				 enum weston_color_curve_step curve_step,
				 enum drm_colorop_3x1d_lut_blob_quantization quantization,
				 uint32_t lut_len)
{
	return NULL;
}

static inline const char *
drm_colorop_type_to_str(const struct drm_colorop *colorop)
{
	return "undefined";
}

static inline void
drm_plane_populate_color_pipelines(struct drm_plane *plane,
				   drmModeObjectPropertiesPtr plane_props)
{
}

static inline void
drm_plane_release_color_pipelines(struct drm_plane *plane)
{
}

#endif /* CAN_OFFLOAD_COLOR_PIPELINE */
