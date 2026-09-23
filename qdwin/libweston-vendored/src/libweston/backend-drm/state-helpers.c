/*
 * Copyright © 2008-2011 Kristian Høgsberg
 * Copyright © 2011 Intel Corporation
 * Copyright © 2017, 2018 Collabora, Ltd.
 * Copyright © 2017, 2018 General Electric Company
 * Copyright (c) 2018 DisplayLink (UK) Ltd.
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

#include "config.h"

#include <xf86drm.h>
#include <xf86drmMode.h>

#include "colorops.h"
#include "drm-internal.h"
#include "shared/weston-assert.h"
#include "shared/weston-drm-fourcc.h"
#include "shared/xalloc.h"

/**
 * Allocate a new, empty, plane state.
 */
struct drm_plane_state *
drm_plane_state_alloc(struct drm_output_state *state_output,
		      struct drm_plane *plane)
{
	struct drm_plane_state *state = zalloc(sizeof(*state));

	assert(state);
	state->output_state = state_output;
	state->plane = plane;
	state->handle = NULL;
	state->in_fence_fd = -1;
	state->rotation = drm_rotation_from_output_transform(plane,
							     WL_OUTPUT_TRANSFORM_NORMAL);
	assert(state->rotation);
	state->zpos = DRM_PLANE_ZPOS_INVALID_PLANE;
	state->alpha = (plane->alpha_max < DRM_PLANE_ALPHA_OPAQUE) ?
		       plane->alpha_max : DRM_PLANE_ALPHA_OPAQUE;

	state->blend_mode = WDRM_PLANE_BLEND_DEFAULT;

	state->color_encoding = WDRM_PLANE_COLOR_ENCODING_DEFAULT;
	state->color_range = WDRM_PLANE_COLOR_RANGE_DEFAULT;

	/* Here we only add the plane state to the desired link, and not
	 * set the member. Having an output pointer set means that the
	 * plane will be displayed on the output; this won't be the case
	 * when we go to disable a plane. In this case, it must be part of
	 * the commit (and thus the output state), but the member must be
	 * NULL, as it will not be on any output when the state takes
	 * effect.
	 */
	if (state_output)
		wl_list_insert(&state_output->plane_list, &state->link);
	else
		wl_list_init(&state->link);

	return state;
}

/**
 * Free an existing plane state. As a special case, the state will not
 * normally be freed if it is the current state; see drm_plane_set_state.
 */
void
drm_plane_state_free(struct drm_plane_state *state, bool force)
{
	struct drm_device *device;

	if (!state)
		return;

	wl_list_remove(&state->link);
	wl_list_init(&state->link);
	state->output_state = NULL;
	state->in_fence_fd = -1;
	state->zpos = DRM_PLANE_ZPOS_INVALID_PLANE;
	state->alpha = DRM_PLANE_ALPHA_OPAQUE;
	drm_color_pipeline_state_unref(state->pipeline_state);
	state->pipeline_state = NULL;

	/* Once the damage blob has been submitted, it is refcounted internally
	 * by the kernel, which means we can safely discard it.
	 */
	if (state->damage_blob_id != 0) {
		device = state->plane->device;

		drmModeDestroyPropertyBlob(device->kms_device->fd,
					   state->damage_blob_id);
		state->damage_blob_id = 0;
	}

	if (force || state != state->plane->state_cur) {
		drm_fb_unref(state->fb);
		weston_buffer_reference(&state->fb_ref.buffer, NULL,
					BUFFER_WILL_NOT_BE_ACCESSED);
		weston_buffer_release_reference(&state->fb_ref.release, NULL);
		free(state);
	}
}

/**
 * Duplicate an existing plane state into a new plane state, storing it within
 * the given output state. If the output state already contains a plane state
 * for the drm_plane referenced by 'src', that plane state is freed first.
 */
struct drm_plane_state *
drm_plane_state_duplicate(struct drm_output_state *state_output,
			  struct drm_plane_state *src)
{
	struct drm_plane_state *dst = zalloc(sizeof(*dst));
	struct drm_plane_state *old, *tmp;

	assert(src);
	assert(dst);
	*dst = *src;
	/* We don't want to copy this, because damage is transient, and only
	 * lasts for the duration of a single repaint.
	 */
	dst->damage_blob_id = 0;
	wl_list_init(&dst->link);
	/* Don't copy the fence, it may no longer be valid and waiting for it
	 * again is not necessary
	 */
	dst->in_fence_fd = -1;

	/**
	 * Take a reference on the pipeline state, as we don't want it to be
	 * destroyed with src plane_state.
	 */
	dst->pipeline_state = drm_color_pipeline_state_ref(src->pipeline_state);

	wl_list_for_each_safe(old, tmp, &state_output->plane_list, link) {
		/* Duplicating a plane state into the same output state, so
		 * it can replace itself with an identical copy of itself,
		 * makes no sense. */
		assert(old != src);
		if (old->plane == dst->plane)
			drm_plane_state_free(old, false);
	}

	wl_list_insert(&state_output->plane_list, &dst->link);

	/* Take a reference on the src framebuffer; if it wraps a client
	 * buffer, then we must also transfer the reference on the client
	 * buffer. */
	if (src->fb) {
		struct weston_buffer *buffer;

		dst->fb = drm_fb_ref(src->fb);
		memset(&dst->fb_ref, 0, sizeof(dst->fb_ref));

		if (src->fb->type == BUFFER_CLIENT ||
		    src->fb->type == BUFFER_DMABUF) {
			buffer = src->fb_ref.buffer.buffer;
		} else {
			buffer = NULL;
		}
		weston_buffer_reference(&dst->fb_ref.buffer, buffer,
					buffer ? BUFFER_MAY_BE_ACCESSED :
					         BUFFER_WILL_NOT_BE_ACCESSED);
		weston_buffer_release_reference(&dst->fb_ref.release,
						src->fb_ref.release.buffer_release);
	} else {
		assert(!src->fb_ref.buffer.buffer);
		assert(!src->fb_ref.release.buffer_release);
	}
	dst->output_state = state_output;
	dst->complete = false;

	return dst;
}

/**
 * Remove a plane state from an output state; if the plane was previously
 * enabled, then replace it with a disabling state. This ensures that the
 * output state was untouched from it was before the plane state was
 * modified by the caller of this function.
 *
 * This is required as drm_output_state_get_plane may either allocate a
 * new plane state, in which case this function will just perform a matching
 * drm_plane_state_free, or it may instead repurpose an existing disabling
 * state (if the plane was previously active), in which case this function
 * will reset it.
 */
void
drm_plane_state_put_back(struct drm_plane_state *state)
{
	struct drm_output_state *state_output;
	struct drm_plane *plane;

	if (!state)
		return;

	state_output = state->output_state;
	plane = state->plane;
	drm_plane_state_free(state, false);

	/* Plane was previously disabled; no need to keep this temporary
	 * state around. */
	if (!plane->state_cur->fb)
		return;

	(void) drm_plane_state_alloc(state_output, plane);
}

/**
 * Given a weston_paint_node, fill the drm_plane_state's co-ordinates to
 * display on a given plane.
 */
void
drm_plane_state_coords_for_paint_node(struct drm_plane_state *state,
				      struct weston_paint_node *pnode,
				      uint64_t zpos)
{
	uint16_t min_alpha = state->plane->alpha_min;
	uint16_t max_alpha = state->plane->alpha_max;

	/* The caller has already checked supported transforms when creating
	 * a list of candidate planes, so we should only ever get here with
	 * a supported transform.
	 */
	assert(drm_paint_node_transform_supported(pnode, state->plane));

	assert(pnode->simple_transform);
	state->rotation = drm_rotation_from_output_transform(state->plane, pnode->transform);

	state->dest_x = pnode->output_dest.x;
	state->dest_y = pnode->output_dest.y;
	state->dest_w = pnode->output_dest.width;
	state->dest_h = pnode->output_dest.height;

	/* Convert to U16.16 KMS fixed-point encoding. */
	state->src_x = wl_fixed_from_double(pnode->buffer_source_x) << 8;
	state->src_y = wl_fixed_from_double(pnode->buffer_source_y) << 8;
	state->src_w = wl_fixed_from_double(pnode->buffer_source_width) << 8;
	state->src_h = wl_fixed_from_double(pnode->buffer_source_height) << 8;

	/* apply zpos if available */
	state->zpos = zpos;

	/* The pnode->alpha is normalized to alpha value range [min_alpha,
	 * max_alpha] that got from drm. The alpha value would never exceed
	 * max_alpha if pnode->alpha <= 1.0.
	 */
	state->alpha = min_alpha +
		       (uint16_t)round((max_alpha - min_alpha) * pnode->alpha);
}

/**
 * Reset the current state of a DRM plane
 *
 * The current state will be freed and replaced by a pristine state.
 *
 * @param plane The plane to reset the current state of
 */
void
drm_plane_reset_state(struct drm_plane *plane)
{
	drm_plane_state_free(plane->state_cur, true);
	plane->state_cur = drm_plane_state_alloc(NULL, plane);
	plane->state_cur->complete = true;
}

/**
 * Return a plane state from a drm_output_state.
 */
struct drm_plane_state *
drm_output_state_get_existing_plane(struct drm_output_state *state_output,
				    struct drm_plane *plane)
{
	struct drm_plane_state *ps;

	wl_list_for_each(ps, &state_output->plane_list, link) {
		if (ps->plane == plane)
			return ps;
	}

	return NULL;
}

/**
 * Return a plane state from a drm_output_state, either existing or
 * freshly allocated.
 */
struct drm_plane_state *
drm_output_state_get_plane(struct drm_output_state *state_output,
			   struct drm_plane *plane)
{
	struct drm_plane_state *ps;

	ps = drm_output_state_get_existing_plane(state_output, plane);
	if (ps)
		return ps;

	return drm_plane_state_alloc(state_output, plane);
}

/**
 * Allocate a new, empty drm_output_state. This should not generally be used
 * in the repaint cycle; see drm_output_state_duplicate.
 */
struct drm_output_state *
drm_output_state_alloc(struct drm_output *output)
{
	struct drm_output_state *state = zalloc(sizeof(*state));

	assert(state);
	state->output = output;
	state->dpms = WESTON_DPMS_OFF;
	state->protection = WESTON_HDCP_DISABLE;
	wl_list_init(&state->link);

	wl_list_init(&state->plane_list);

	return state;
}

/**
 * Duplicate an existing drm_output_state into a new one. This is generally
 * used during the repaint cycle, to capture the existing state of an output
 * and modify it to create a new state to be used.
 *
 * The mode determines whether the output will be reset to an a blank state,
 * or an exact mirror of the current state.
 */
struct drm_output_state *
drm_output_state_duplicate(struct drm_output_state *src,
			   struct drm_pending_state *pending_state,
			   enum drm_output_state_duplicate_mode plane_mode)
{
	struct weston_compositor *compositor = src->output->base.compositor;
	struct drm_output_state *dst = xmalloc(sizeof(*dst));
	struct drm_plane_state *ps;

	/* The reuse bit isn't stored in the state */
	weston_assert_false(compositor, src->mode & DRM_OUTPUT_PROPOSE_STATE_REUSE);

	/* Copy the whole structure, then individually modify the
	 * pending_state, as well as the list link into our pending
	 * state. */
	*dst = *src;

	dst->pending_state = pending_state;
	if (pending_state)
		wl_list_insert(&pending_state->output_list, &dst->link);
	else
		wl_list_init(&dst->link);

	wl_list_init(&dst->plane_list);

	wl_list_for_each(ps, &src->plane_list, link) {
		/* Don't carry planes which are now disabled; these should be
		 * free for other outputs to reuse. */
		if (!ps->handle)
			continue;

		if (plane_mode == DRM_OUTPUT_STATE_CLEAR_PLANES)
			(void) drm_plane_state_alloc(dst, ps->plane);
		else
			(void) drm_plane_state_duplicate(dst, ps);
	}

	return dst;
}

/**
 * Free an unused drm_output_state.
 */
void
drm_output_state_free(struct drm_output_state *state)
{
	struct drm_plane_state *ps, *next;

	if (!state)
		return;

	wl_list_for_each_safe(ps, next, &state->plane_list, link)
		drm_plane_state_free(ps, false);

	wl_list_remove(&state->link);

	free(state);
}

/**
 * Allocate a new drm_pending_state
 *
 * Allocate a new, empty, 'pending state' structure to be used across a
 * repaint cycle or similar.
 *
 * @param device DRM device
 * @returns Newly-allocated pending state structure
 */
struct drm_pending_state *
drm_pending_state_alloc(struct drm_device *device)
{
	struct drm_pending_state *ret;

	ret = calloc(1, sizeof(*ret));
	if (!ret)
		return NULL;

	ret->device = device;
	wl_list_init(&ret->output_list);

	return ret;
}

/**
 * Free a drm_pending_state structure
 *
 * Frees a pending_state structure, as well as any output_states connected
 * to this pending state.
 *
 * @param pending_state Pending state structure to free
 */
void
drm_pending_state_free(struct drm_pending_state *pending_state)
{
	struct drm_output_state *output_state, *tmp;

	if (!pending_state)
		return;

	wl_list_for_each_safe(output_state, tmp, &pending_state->output_list,
			      link) {
		drm_output_state_free(output_state);
	}

	free(pending_state);
}

/**
 * Find an output state in a pending state
 *
 * Given a pending_state structure, find the output_state for a particular
 * output.
 *
 * @param pending_state Pending state structure to search
 * @param output Output to find state for
 * @returns Output state if present, or NULL if not
 */
struct drm_output_state *
drm_pending_state_get_output(struct drm_pending_state *pending_state,
			     struct drm_output *output)
{
	struct drm_output_state *output_state;

	wl_list_for_each(output_state, &pending_state->output_list, link) {
		if (output_state->output == output)
			return output_state;
	}

	return NULL;
}
