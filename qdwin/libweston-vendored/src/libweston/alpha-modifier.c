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

#include "config.h"

#include "alpha-modifier.h"
#include "shared/xalloc.h"
#include "shared/weston-assert.h"
#include "libweston-internal.h"
#include "alpha-modifier-v1-server-protocol.h"

struct weston_alpha_modifier_surface {
	struct wl_resource *owner;
	struct weston_surface *surface;
	struct wl_listener surface_destroy_listener;
};

static void
alpha_modifier_base_surface_destroyed(struct wl_listener *listener, void *data)
{
	struct weston_surface *surface = data;
	struct weston_compositor *wc = surface->compositor;
	struct weston_alpha_modifier_surface *ams =
		wl_container_of(listener, ams, surface_destroy_listener);

	weston_assert_ptr_eq(wc, surface, ams->surface);

	/* Surface destroyed before ams, so ams becomes inert. */
	ams->surface = NULL;
	wl_list_remove(&ams->surface_destroy_listener.link);
}

static void
alpha_modifier_surface_set_multiplier(struct wl_client *client,
				      struct wl_resource *res, uint32_t factor)
{
	struct weston_alpha_modifier_surface *ams = wl_resource_get_user_data(res);
	struct weston_surface *surface;
	float f;

	/**
	 * wp_alpha_modifier_surface_v1 states that if wl_surface gets destroyed
	 * before alpha modifier surface, ams should return error to all
	 * requests.
	 */
	if (!ams->surface) {
		wl_resource_post_error(res,
				       WP_ALPHA_MODIFIER_SURFACE_V1_ERROR_NO_SURFACE,
				       "surface already gone, set_multiplier request not valid");
		return;
	}

	surface = ams->surface;

	/**
	 * Normalize the factor to [0.0, 1.0] range. Converting UINT32_MAX to
	 * float brings precision issues, so we use double.
	 */
	f = factor / (double)UINT32_MAX;

	/* Schedule state changes (double-buffered request). */
	if (surface->pending.alpha_modifier != f) {
		surface->pending.alpha_modifier = f;
		surface->pending.status |= WESTON_SURFACE_DIRTY_BUFFER_PARAMS;
	}
}

static void
alpha_modifier_surface_destroy(struct wl_client *client,
			       struct wl_resource *res)
{
	struct weston_alpha_modifier_surface *ams = wl_resource_get_user_data(res);
	struct weston_surface *surface;
	const float f = 1.0f;

	/**
	 * wp_alpha_modifier_surface_v1 states that if wl_surface gets destroyed
	 * before alpha modifier surface, ams should return error to all
	 * requests.
	 */
	if (!ams->surface) {
		wl_resource_post_error(res,
				       WP_ALPHA_MODIFIER_SURFACE_V1_ERROR_NO_SURFACE,
				       "surface already gone, destroy request not valid");
		return;
	}

	surface = ams->surface;

	/**
	 * The resource gets destroyed with this request, but the state changes
	 * are double-buffered.
	 */
	if (surface->pending.alpha_modifier != f) {
		surface->pending.alpha_modifier = f;
		surface->pending.status |= WESTON_SURFACE_DIRTY_BUFFER_PARAMS;
	}

	wl_resource_destroy(res);
}

static const struct wp_alpha_modifier_surface_v1_interface alpha_modifier_surf_impl = {
	.destroy = alpha_modifier_surface_destroy,
	.set_multiplier = alpha_modifier_surface_set_multiplier,
};

static void
alpha_modifier_surface_destructor(struct wl_resource *res)
{
	struct weston_alpha_modifier_surface *ams = wl_resource_get_user_data(res);
	struct weston_surface *surface = ams->surface;

	if (surface) {
		surface->ams = NULL;
		wl_list_remove(&ams->surface_destroy_listener.link);
	}

	free(ams);
}

static void
alpha_modifier_get_surface(struct wl_client *client,
			   struct wl_resource *alpha_modifier_res,
			   uint32_t id, struct wl_resource *surface_res)
{
	struct weston_surface *surface = wl_resource_get_user_data(surface_res);
	int version = wl_resource_get_version(alpha_modifier_res);
	struct weston_alpha_modifier_surface *ams;
	struct wl_resource *res;

	if (surface->ams) {
		wl_resource_post_error(alpha_modifier_res,
				       WP_ALPHA_MODIFIER_V1_ERROR_ALREADY_CONSTRUCTED,
				       "surface already has an alpha modifier object");
		return;
	}

	res = wl_resource_create(client, &wp_alpha_modifier_surface_v1_interface,
				 version, id);
	if (!res) {
	      wl_resource_post_no_memory(alpha_modifier_res);
	      return;  
	}

	ams = xzalloc(sizeof(*ams));
	surface->ams = ams;

	ams->surface = surface;
	ams->owner = res;
	ams->surface_destroy_listener.notify = alpha_modifier_base_surface_destroyed;
	wl_signal_add(&surface->destroy_signal, &ams->surface_destroy_listener);

	wl_resource_set_implementation(res, &alpha_modifier_surf_impl,
				       ams, alpha_modifier_surface_destructor);
}

static void
alpha_modifier_destroy(struct wl_client *client, struct wl_resource *res)
{
	wl_resource_destroy(res);
}

static const struct wp_alpha_modifier_v1_interface alpha_modifier_impl = {
	.destroy = alpha_modifier_destroy,
	.get_surface = alpha_modifier_get_surface,
};

static void
bind_alpha_modifier(struct wl_client *client, void *data, uint32_t version,
		    uint32_t id)
{
	struct weston_compositor *compositor = data;
	struct wl_resource *res;

	res = wl_resource_create(client, &wp_alpha_modifier_v1_interface,
				 version, id);
	if (!res) {
		wl_client_post_no_memory(client);
		return;
	}

	wl_resource_set_implementation(res, &alpha_modifier_impl,
				       compositor, NULL);
}

/** Advertise wp_alpha_modifier_v1 support
 *
 * Calling this initializes the alpha modifier protocol support, so that
 * wp_alpha_modifier_v1 will be advertised to clients. Essentially it
 * creates a global. Do not call this function multiple times in the
 * compositor's lifetime. There is no way to deinit explicitly, globals will be
 * reaped when the wl_display gets destroyed.
 *
 * \param wc The compositor instance to init for.
 * \return True on success, false on failure.
 */
bool
weston_compositor_enable_alpha_modifier_protocol(struct weston_compositor *wc)
{
	int version = 1;

	if (!wl_global_create(wc->wl_display,
			      &wp_alpha_modifier_v1_interface,
			      version, wc, bind_alpha_modifier))
		return false;

	return true;
}
