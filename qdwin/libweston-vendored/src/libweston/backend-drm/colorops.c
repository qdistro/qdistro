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

#include "config.h"

#include <libweston/linalg-3.h>

#include "colorops.h"
#include "color-properties.h"
#include "drm-internal.h"
#include "shared/string-helpers.h"
#include "shared/weston-assert.h"
#include "shared/xalloc.h"

static void
drm_colorop_3x1d_lut_blob_destroy(struct drm_colorop_3x1d_lut_blob *lut)
{
	wl_list_remove(&lut->destroy_listener.link);
	wl_list_remove(&lut->link);
	drmModeDestroyPropertyBlob(lut->device->kms_device->fd, lut->blob_id);
	free(lut);
}

static void
drm_colorop_3x1d_lut_blob_destroy_handler(struct wl_listener *l, void *data)
{
	struct drm_colorop_3x1d_lut_blob *lut;

	lut = wl_container_of(l, lut, destroy_listener);
	assert(lut->xform == data);

	drm_colorop_3x1d_lut_blob_destroy(lut);
}

/**
 * Search for a 3x1D LUT colorop blob in a DRM device.
 *
 * \param device The DRM device in which we want to look for the blob.
 * \param xform The xform from which the LUT comes from.
 * \param curve_step What curve step from the xform originated the 3x1D LUT.
 * \param quantization The colorop 3x1D LUT quantization (U32 or U16).
 * \param lut_len How many taps each of the 1D LUT has.
 */
const struct drm_colorop_3x1d_lut_blob *
drm_colorop_3x1d_lut_blob_search(const struct drm_device *device,
				 const struct weston_color_transform *xform,
				 enum weston_color_curve_step curve_step,
				 enum drm_colorop_3x1d_lut_blob_quantization quantization,
				 uint32_t lut_len)
{
	const struct drm_colorop_3x1d_lut_blob *lut;

	wl_list_for_each(lut, &device->drm_colorop_3x1d_lut_blob_list, link)
		if (lut->xform == xform && lut->curve_step == curve_step &&
		    lut->lut_len == lut_len && lut->quantization == quantization)
			return lut;

	return NULL;
}

static struct drm_color_lut32
drm_vec3f_to_u32(struct weston_vec3f vec)
{
	struct drm_color_lut32 res;

	/* UINT32_MAX exceeds the 24-bit integer precision of floats and could
	 * be rounded incorrectly if multiplied in float. */

	res.red   = (double) vec.r * UINT32_MAX;
	res.green = (double) vec.g * UINT32_MAX;
	res.blue  = (double) vec.b * UINT32_MAX;

	return res;
}

static struct drm_color_lut
drm_vec3f_to_u16(struct weston_vec3f vec)
{
	struct drm_color_lut res;

	res.red   = vec.r * UINT16_MAX;
	res.green = vec.g * UINT16_MAX;
	res.blue  = vec.b * UINT16_MAX;

	return res;
}

/**
 * Create a 3x1D LUT colorop blob.
 *
 * A Weston colorop is an object associated with a step from a struct
 * weston_color_transform (or the xform itself, when it gets decomposed to
 * shaper + 3D LUT) and that can be used to program KMS color operations. This
 * function creates a blob for such kind of object and cache that in the given
 * DRM device, so we can avoid re-creating it.
 *
 * \param device The DRM device in which this colorop blob is stored.
 * \param xform The xform from which the LUT comes from. This object matches its
 * lifetime.
 * \param curve_step What xform curve step originated the 3x1D LUT.
 * \param quantization The colorop 3x1D LUT quantization (U32 or U16).
 * \param cm_lut The 3x1D LUT from which the colorop will be created.
 * \param lut_len The number of taps for each of the 1D LUT.
 * \return The 3x1D LUT colorop blob.
 */
struct drm_colorop_3x1d_lut_blob *
drm_colorop_3x1d_lut_blob_create(struct drm_device *device,
				 struct weston_color_transform *xform,
				 enum weston_color_curve_step curve_step,
				 enum drm_colorop_3x1d_lut_blob_quantization quantization,
				 struct weston_vec3f *cm_lut, uint32_t lut_len)
{
	struct drm_backend *b = device->backend;
	struct drm_colorop_3x1d_lut_blob *lut;
	uint32_t blob_id;
	unsigned int i;
	int ret = -1;

	switch (quantization) {
	case DRM_COLOROP_3X1D_LUT_BLOB_QUANTIZATION_U16: {
		struct drm_color_lut *drm_lut =
			xcalloc(lut_len, sizeof(*drm_lut));

		for (i = 0; i < lut_len; i++)
			drm_lut[i] = drm_vec3f_to_u16(cm_lut[i]);

		ret = drmModeCreatePropertyBlob(device->kms_device->fd, drm_lut, lut_len * sizeof(*drm_lut),
						&blob_id);
		free(drm_lut);
		break;
	}
	case DRM_COLOROP_3X1D_LUT_BLOB_QUANTIZATION_U32: {
		struct drm_color_lut32 *drm_lut =
			xcalloc(lut_len, sizeof(*drm_lut));

		for (i = 0; i < lut_len; i++)
			drm_lut[i] = drm_vec3f_to_u32(cm_lut[i]);

		ret = drmModeCreatePropertyBlob(device->kms_device->fd, drm_lut, lut_len * sizeof(*drm_lut),
						&blob_id);
		free(drm_lut);
		break;
	}}

	if (ret < 0) {
		drm_debug(b, "[colorop] failed to create blob for colorop 3x1D LUT;\n" \
			     "          lut_len %u, quantization %s",
			     lut_len,
			     quantization == DRM_COLOROP_3X1D_LUT_BLOB_QUANTIZATION_U16 ? "u16" : "u32");
		return NULL;
	}

	lut = xzalloc(sizeof(*lut));

	lut->device = device;
	lut->xform = xform;
	lut->curve_step = curve_step;
	lut->quantization = quantization;
	lut->lut_len = lut_len;
	lut->blob_id = blob_id;

	wl_list_insert(&device->drm_colorop_3x1d_lut_blob_list, &lut->link);

	lut->destroy_listener.notify = drm_colorop_3x1d_lut_blob_destroy_handler;
	wl_signal_add(&lut->xform->destroy_signal, &lut->destroy_listener);

	return lut;
}

enum lowering_curve_policy {
	LOWERING_CURVE_POLICY_ALLOW = true,
	LOWERING_CURVE_POLICY_DENY = false,
};

static const char *
lowering_curve_policy_str(enum lowering_curve_policy policy)
{
	switch (policy) {
	case LOWERING_CURVE_POLICY_DENY:
		return "deny lowering curve";
	case LOWERING_CURVE_POLICY_ALLOW:
		return "allow lowering curve";
	}
	return "???";
}

static const struct drm_colorop_3x1d_lut_blob *
drm_colorop_3x1d_lut_blob_from_curve(struct drm_device *device,
				     struct weston_color_transform *xform,
				     enum weston_color_curve_step curve_step,
				     uint32_t lut_len)
{
	struct weston_compositor *compositor = xform->cm->compositor;
	struct drm_backend *b = device->backend;
	const struct drm_colorop_3x1d_lut_blob *colorop_lut;
	char *err_msg;
	struct weston_vec3f *cm_lut;

	/* No need to create, 3x1D LUT colorop already exists. */
	colorop_lut = drm_colorop_3x1d_lut_blob_search(device, xform, curve_step,
						       DRM_COLOROP_3X1D_LUT_BLOB_QUANTIZATION_U32,
						       lut_len);
	if (colorop_lut)
		return colorop_lut;

	cm_lut = weston_color_curve_to_3x1D_LUT(compositor, xform, curve_step,
						WESTON_COLOR_PRECISION_CARELESS,
						lut_len, &err_msg);
	if (!cm_lut) {
		drm_debug(b, "[colorop] failed to create colorop 3x1D from curve: %s\n",
			     err_msg);
		free(err_msg);
		return NULL;
	}

	colorop_lut =
		drm_colorop_3x1d_lut_blob_create(device, xform, curve_step,
						 DRM_COLOROP_3X1D_LUT_BLOB_QUANTIZATION_U32,
						 cm_lut, lut_len);
	free(cm_lut);
	if (!colorop_lut) {
		drm_debug(b, "[colorop] failed to create colorop 3x1D from curve\n");
		return NULL;
	}

	return colorop_lut;
}

static void
drm_colorop_clut_blob_destroy(struct drm_colorop_clut_blob *clut)
{
	wl_list_remove(&clut->destroy_listener.link);
	wl_list_remove(&clut->link);
	drmModeDestroyPropertyBlob(clut->device->kms_device->fd, clut->clut_blob_id);
	drmModeDestroyPropertyBlob(clut->device->kms_device->fd, clut->shaper_blob_id);
	free(clut);
}

static void
drm_colorop_clut_blob_destroy_handler(struct wl_listener *l, void *data)
{
	struct drm_colorop_clut_blob *clut;

	clut = wl_container_of(l, clut, destroy_listener);
	assert(clut->xform == data);

	drm_colorop_clut_blob_destroy(clut);
}

static struct drm_colorop_clut_blob *
drm_colorop_clut_blob_create(struct drm_device *device,
			     struct weston_color_transform *xform,
			     uint32_t len_shaper, float *cm_shaper,
			     uint32_t len_clut, float *cm_clut)
{
	struct drm_backend *b = device->backend;
	struct drm_colorop_clut_blob *clut;
	struct drm_color_lut32 *drm_clut;
	struct drm_color_lut32 *drm_shaper;
	uint32_t clut_blob_id, shaper_blob_id;
	struct weston_vec3f v;
	unsigned int i;
	int ret;

	drm_clut = xcalloc(len_clut * len_clut * len_clut, sizeof(*drm_clut));
	drm_shaper = xcalloc(len_shaper, sizeof(*drm_shaper));

	for (i = 0; i < len_shaper; i++) {
		v = WESTON_VEC3F(cm_shaper[i],
				 cm_shaper[i + len_shaper],
				 cm_shaper[i + 2 * len_shaper]);
		drm_shaper[i] = drm_vec3f_to_u32(v);
	}

	ret = drmModeCreatePropertyBlob(device->kms_device->fd, drm_shaper,
					len_shaper * sizeof(*drm_shaper),
					&shaper_blob_id);
	if (ret < 0) {
		drm_debug(b, "[colorop] failed to create blob for colorop shaper\n");
		goto out;
	}

	/**
	 * Kernel uAPI doc states that the KMS 3D LUT indexes are traversed in
	 * BGR order (R index growing first, then G and lastly B). Our 3D cLUT
	 * is traversed in BGR order as well, so no index mapping is required.
	 */
	for (i = 0; i < len_clut * len_clut * len_clut; i++) {
		v = WESTON_VEC3F(cm_clut[3 * i],
				 cm_clut[3 * i + 1],
				 cm_clut[3 * i + 2]);
		drm_clut[i] = drm_vec3f_to_u32(v);
	}
	ret = drmModeCreatePropertyBlob(device->kms_device->fd, drm_clut,
					len_clut * len_clut * len_clut * sizeof(*drm_clut),
					&clut_blob_id);
	if (ret < 0) {
		drmModeDestroyPropertyBlob(device->kms_device->fd, shaper_blob_id);
		drm_debug(b, "[colorop] failed to create blob for colorop 3D cLUT\n");
		goto out;
	}

out:
	free(drm_clut);
	free(drm_shaper);

	if (ret < 0)
		return NULL;

	clut = xzalloc(sizeof(*clut));

	clut->device = device;
	clut->xform = xform;
	clut->shaper_len = len_shaper;
	clut->clut_len = len_clut;
	clut->shaper_blob_id = shaper_blob_id;
	clut->clut_blob_id = clut_blob_id;

	wl_list_insert(&device->drm_colorop_clut_blob_list, &clut->link);

	clut->destroy_listener.notify = drm_colorop_clut_blob_destroy_handler;
	wl_signal_add(&xform->destroy_signal, &clut->destroy_listener);

	return clut;
}

static const struct drm_colorop_clut_blob *
drm_colorop_clut_blob_search(const struct drm_device *device,
			     const struct weston_color_transform *xform,
			     uint32_t clut_len, uint32_t shaper_len)
{
	struct drm_colorop_clut_blob *clut;

	wl_list_for_each(clut, &device->drm_colorop_clut_blob_list, link)
		if (clut->xform == xform &&
		    clut->clut_len == clut_len && clut->shaper_len == shaper_len)
			return clut;

	return NULL;
}

static const struct drm_colorop_clut_blob *
drm_colorop_clut_blob_from_xform(struct drm_device *device,
				 struct weston_color_transform *xform,
				 uint32_t len_shaper, uint32_t len_clut)
{
	float *cm_shaper = NULL;
	float *cm_clut = NULL;
	const struct drm_colorop_clut_blob *colorop_clut = NULL;

	colorop_clut = drm_colorop_clut_blob_search(device, xform, len_shaper, len_clut);
	if (colorop_clut)
		return colorop_clut;

	/* Get shaper + 3D cLUT from xform. */
	cm_shaper = xcalloc(3 * len_shaper, sizeof(*cm_shaper));
	cm_clut = xcalloc(3 * len_clut * len_clut * len_clut, sizeof(*cm_clut));
	if (!xform->to_clut(xform, len_shaper, cm_shaper, len_clut, cm_clut))
		goto out;

	colorop_clut = drm_colorop_clut_blob_create(device, xform,
						    len_shaper, cm_shaper,
						    len_clut, cm_clut);

out:
	free(cm_shaper);
	free(cm_clut);

	return colorop_clut;
}

static void
drm_colorop_matrix_blob_destroy(struct drm_colorop_matrix_blob *mat)
{
	wl_list_remove(&mat->destroy_listener.link);
	wl_list_remove(&mat->link);
	drmModeDestroyPropertyBlob(mat->device->kms_device->fd, mat->blob_id);
	free(mat);
}

static void
drm_colorop_matrix_blob_destroy_handler(struct wl_listener *l, void *data)
{
	struct drm_colorop_matrix_blob *mat =
		wl_container_of(l, mat, destroy_listener);

	drm_colorop_matrix_blob_destroy(mat);
}

static const struct drm_colorop_matrix_blob *
drm_colorop_matrix_blob_search(const struct drm_device *device,
			       const struct weston_color_transform *xform)
{
	struct drm_colorop_matrix_blob *mat;

	wl_list_for_each(mat, &device->drm_colorop_matrix_blob_list, link)
		if (mat->xform == xform)
			return mat;

	return NULL;
}

/**
 * Float to S31.32 sign-magnitude representation.
 */
static uint64_t
float_to_s31_32_sign_magnitude(float val)
{
	uint64_t ret;

	if (val < 0) {
		ret = (uint64_t) (-val * (1ULL << 32));
		ret |= 1ULL << 63;
	} else {
		ret = (uint64_t) (val * (1ULL << 32));
	}

	return ret;
}

static struct drm_colorop_matrix_blob *
drm_colorop_matrix_blob_create(struct drm_device *device,
			       struct weston_color_transform *xform,
			       struct drm_color_ctm_3x4 *matrix)
{
	struct drm_backend *b = device->backend;
	struct drm_colorop_matrix_blob *colorop_mat;
	uint32_t blob_id;
	int ret;

	ret = drmModeCreatePropertyBlob(device->kms_device->fd, matrix,
					sizeof(*matrix), &blob_id);
	if (ret < 0) {
		drm_debug(b, "[colorop] failed to create blob for matrix\n");
		return NULL;
	}

	colorop_mat = xzalloc(sizeof(*colorop_mat));

	colorop_mat->blob_id = blob_id;
	colorop_mat->device = device;
	colorop_mat->xform = xform;
	wl_list_insert(&device->drm_colorop_matrix_blob_list, &colorop_mat->link);
	colorop_mat->destroy_listener.notify = drm_colorop_matrix_blob_destroy_handler;
	wl_signal_add(&xform->destroy_signal, &colorop_mat->destroy_listener);

	return colorop_mat;
}

static const struct drm_colorop_matrix_blob *
drm_colorop_matrix_blob_from_mapping(struct drm_device *device,
				     struct weston_color_transform *xform)
{
	struct drm_backend *b = device->backend;
	struct weston_color_mapping *mapping = &xform->mapping;
	const struct drm_colorop_matrix_blob *colorop_mat;
	struct drm_color_ctm_3x4 *mat_3x4;
	unsigned int row, col;
	float val;

	/* No need to create, colorop matrix already exists. */
	colorop_mat = drm_colorop_matrix_blob_search(device, xform);
	if (colorop_mat)
		return colorop_mat;

	mat_3x4 = xzalloc(sizeof(*mat_3x4));

	/**
	 * mapping->u.mat.matrix is in column-major order. We transpose it and
	 * also add a new column with the offset. Also, kernel requires the
	 * values in S31.32 sign-magnitude representation.
	 */
	for (row = 0; row < 3; row++) {
		for (col = 0; col < 3; col++) {
			val = mapping->u.mat.matrix.col[col].el[row];
			mat_3x4->matrix[row * 4 + col] = float_to_s31_32_sign_magnitude(val);
		}
		val = mapping->u.mat.offset.el[row];
		mat_3x4->matrix[row * 4 + 3] = float_to_s31_32_sign_magnitude(val);
	}

	colorop_mat = drm_colorop_matrix_blob_create(device, xform, mat_3x4);
	free(mat_3x4);
	if (!colorop_mat) {
		drm_debug(b, "[colorop] failed to create colorop matrix from mapping\n");
		return NULL;
	}

	return colorop_mat;
}

struct colorop_curve_scaler {
	float factor;
	/* placement wrt the curve colorop in the chain */
	enum {
		PLACEMENT_NONE = 0,
		PLACEMENT_BEFORE,
		PLACEMENT_AFTER,
	} placement;
};

static enum wdrm_colorop_curve_1d
weston_tf_to_colorop_curve(const struct weston_color_tf_info *tf_info,
			   enum weston_tf_direction tf_direction,
			   struct colorop_curve_scaler *scaler)
{
	/**
	 * wdrm_colorop_curve_1d only supports PQ EOTF (and its inverse) scaled
	 * by 125. We don't have a tf_info that corresponds to this specific
	 * scaled curve, but we handle it as a special case. A multiplier
	 * colorop is needed to scale values up or down, depending if we have
	 * the EOTF or its inverse. See curve_create_colorop_state().
	 */
	if (tf_info->tf == WESTON_TF_ST2084_PQ) {
		if (tf_direction == WESTON_INVERSE_TF) {
			scaler->factor = 125.0f;
			scaler->placement = PLACEMENT_BEFORE;
			return WDRM_COLOROP_CURVE_1D_PQ_125_INV_EOTF;
		} else {
			scaler->factor = 1.0f / 125.0f;
			scaler->placement = PLACEMENT_AFTER;
			return WDRM_COLOROP_CURVE_1D_PQ_125_EOTF;
		}
	}

	scaler->factor = 1.0f;
	scaler->placement = PLACEMENT_NONE;
	return (tf_direction == WESTON_INVERSE_TF) ?
		tf_info->kms_colorop_inverse : tf_info->kms_colorop;
}

static void
drm_colorop_destroy(struct drm_colorop *colorop)
{
	wl_list_remove(&colorop->link);
	drm_property_info_free(colorop->props, WDRM_COLOROP__COUNT);

	free(colorop);
}

static struct drm_colorop *
drm_colorop_create(struct drm_color_pipeline *pipeline, uint32_t colorop_id,
		   uint32_t *next_colorop_id)
{
	struct drm_device *device = pipeline->plane->device;
	drmModeObjectPropertiesPtr props_drm;
	struct drm_colorop *colorop;

	*next_colorop_id = 0;

	props_drm = drmModeObjectGetProperties(device->kms_device->fd, colorop_id,
					       DRM_MODE_OBJECT_COLOROP);
	if (!props_drm)
		return NULL;

	colorop = xzalloc(sizeof(*colorop));

	wl_list_insert(pipeline->colorop_list.prev, &colorop->link);

	colorop->id = colorop_id;
	colorop->pipeline = pipeline;

	drm_property_info_populate(device, colorop_props, colorop->props,
				   WDRM_COLOROP__COUNT, props_drm);

	colorop->type = drm_property_get_value(&colorop->props[WDRM_COLOROP_TYPE],
					       props_drm, WDRM_COLOROP_TYPE__COUNT);
	if (colorop->type == WDRM_COLOROP_TYPE__COUNT) {
		drm_colorop_destroy(colorop);
		drmModeFreeObjectProperties(props_drm);
		return NULL;
	}

	colorop->size = drm_property_get_value(&colorop->props[WDRM_COLOROP_SIZE],
					       props_drm, 0);
	if (colorop->size == 0 && (colorop->type == WDRM_COLOROP_TYPE_1D_LUT ||
				   colorop->type == WDRM_COLOROP_TYPE_3D_LUT)) {
		drm_colorop_destroy(colorop);
		drmModeFreeObjectProperties(props_drm);
		return NULL;
	}

	colorop->can_bypass = (colorop->props[WDRM_COLOROP_BYPASS].prop_id != 0);

	*next_colorop_id =
		drm_property_get_value(&colorop->props[WDRM_COLOROP_NEXT],
				       props_drm, 0);

	drmModeFreeObjectProperties(props_drm);

	return colorop;
}

/**
 * Given a colorop this returns its type as a string.
 *
 * \param colorop The colorop.
 * \return The colorop type as a string.
 */
const char *
drm_colorop_type_to_str(const struct drm_colorop *colorop)
{
	return colorop->props[WDRM_COLOROP_TYPE].enum_values[colorop->type].name;
}

static const struct drm_colorop *
drm_colorop_iterate(const struct drm_color_pipeline *pipeline,
		    const struct drm_colorop *iter)
{
	const struct wl_list *list = &pipeline->colorop_list;
	const struct wl_list *node;

	if (iter)
		node = iter->link.next;
	else
		node = list->next;

	if (node == list)
		return NULL;

	return container_of(node, const struct drm_colorop, link);
}

static bool
is_colorop_compatible_with_curve(struct weston_compositor *compositor,
				 const struct drm_colorop *colorop,
				 struct weston_color_curve *curve)
{
	struct weston_color_curve_parametric param;
	const struct drm_property_info *prop_info;
	enum wdrm_colorop_curve_1d curve_type;
	struct colorop_curve_scaler scaler;
	bool ret;

	if (colorop->type == WDRM_COLOROP_TYPE_1D_CURVE) {
		if (curve->type != WESTON_COLOR_CURVE_TYPE_ENUM)
			return false;

		curve_type = weston_tf_to_colorop_curve(curve->u.enumerated.tf.info,
							curve->u.enumerated.tf_direction,
							&scaler);
		if (curve_type == WDRM_COLOROP_CURVE_1D__COUNT)
			return false;

		prop_info = &colorop->props[WDRM_COLOROP_CURVE_1D];
		if (!prop_info->enum_values[curve_type].valid)
			return false;

		return true;
	} else if (colorop->type == WDRM_COLOROP_TYPE_1D_LUT) {
		switch (curve->type) {
		case WESTON_COLOR_CURVE_TYPE_LUT_3x1D:
			return true;
		case WESTON_COLOR_CURVE_TYPE_PARAMETRIC:
			/* Parametric can be lowered to LUT. */
			return true;
		case WESTON_COLOR_CURVE_TYPE_ENUM:
			switch (curve->u.enumerated.tf.info->tf) {
			case WESTON_TF_ST2084_PQ:
				/* This TF is implemented, so we can lower curve to LUT. */
				return true;
			default:
				/* If we can lower the TF to parametric, we can use it
				 * to create a LUT. */
				ret = weston_color_curve_enum_get_parametric(compositor,
									     &curve->u.enumerated,
									     &param);
				return ret;
			}
		case WESTON_COLOR_CURVE_TYPE_IDENTITY:
			/* Dead code, function never called for IDENTITY. */
			weston_assert_not_reached(compositor,
						  "no need to get colorop for identity curve");
		}

		return false;
	}

	return false;
}

static const struct drm_colorop *
search_colorop_compatible_curve(const struct drm_color_pipeline *pipeline,
				const struct drm_colorop *previous_colorop,
				struct weston_color_curve *curve,
				enum lowering_curve_policy policy)
{
	struct drm_backend *b = pipeline->plane->device->backend;
	const struct drm_colorop *colorop = previous_colorop;

	/**
	 * Identity curve should not need a colorop, so calling this func for
	 * IDENTITY is not allowed.
	 */
	weston_assert_u32_ne(b->compositor,
			     curve->type, WESTON_COLOR_CURVE_TYPE_IDENTITY);

	while ((colorop = drm_colorop_iterate(pipeline, colorop))) {
		switch (curve->type) {
		case WESTON_COLOR_CURVE_TYPE_ENUM:
			if (colorop->type == WDRM_COLOROP_TYPE_1D_CURVE &&
			    is_colorop_compatible_with_curve(b->compositor, colorop, curve))
				return colorop;
			else if (colorop->type == WDRM_COLOROP_TYPE_1D_LUT &&
				 policy == LOWERING_CURVE_POLICY_ALLOW &&
				 is_colorop_compatible_with_curve(b->compositor, colorop, curve))
				return colorop;
			break;
		case WESTON_COLOR_CURVE_TYPE_PARAMETRIC:
		case WESTON_COLOR_CURVE_TYPE_LUT_3x1D:
			if (colorop->type == WDRM_COLOROP_TYPE_1D_LUT)
				return colorop;
			break;
		case WESTON_COLOR_CURVE_TYPE_IDENTITY:
			/* Dead code. */
			weston_assert_not_reached(b->compositor,
						  "no need to get colorop for identity curve");
		}

		if (!colorop->can_bypass)
			break;
	}

	return NULL;
}

static const struct drm_colorop *
search_colorop_type(const struct drm_color_pipeline *pipeline,
		    const struct drm_colorop *previous_colorop,
		    enum wdrm_colorop_type type)
{
	const struct drm_colorop *colorop = previous_colorop;

	while ((colorop = drm_colorop_iterate(pipeline, colorop))) {
		if (colorop->type == type)
			return colorop;

		if (!colorop->can_bypass)
			break;
	}

	return NULL;
}

static struct drm_colorop_state *
drm_colorop_state_create(struct drm_color_pipeline_state *pipeline_state,
			 const struct drm_colorop *colorop,
			 struct drm_colorop_state_object so)
{
	struct drm_colorop_state *colorop_state;

	colorop_state = xzalloc(sizeof(*colorop_state));

	wl_list_insert(pipeline_state->colorop_state_list.prev, &colorop_state->link);

	colorop_state->colorop = colorop;
	colorop_state->object = so;

	return colorop_state;
}

static void
drm_colorop_state_destroy(struct drm_colorop_state *colorop_state)
{
	wl_list_remove(&colorop_state->link);
	free(colorop_state);
}

static struct drm_color_pipeline_state *
drm_color_pipeline_state_create(struct drm_color_pipeline *pipeline)
{
	struct drm_color_pipeline_state *state;

	state = xzalloc(sizeof(*state));

	state->pipeline = pipeline;
	state->ref_count = 1;

	wl_list_init(&state->colorop_state_list);

	return state;
}

/**
 * Take a reference on a color pipeline state.
 *
 * @param state The pipeline state to reference, NULL is valid.
 * @return Pointer to the referenced state, may be NULL as well.
 */
struct drm_color_pipeline_state *
drm_color_pipeline_state_ref(struct drm_color_pipeline_state *state)
{
	struct weston_compositor *wc;

	if (!state)
		return NULL;

	wc = state->pipeline->plane->base.compositor;

	weston_assert_uint_gt(wc, state->ref_count, 0);
	state->ref_count++;

	return state;
}

/**
 * Drop reference on a color pipeline state, freeing it when it is no longer
 * referenced.
 *
 * @param state The pipeline state to unreference, NULL is valid.
 */
void
drm_color_pipeline_state_unref(struct drm_color_pipeline_state *state)
{
	struct drm_colorop_state *colorop_state, *tmp_colorop_state;
	struct weston_compositor *wc;

	if (!state)
		return;

	wc = state->pipeline->plane->base.compositor;

	weston_assert_uint_gt(wc, state->ref_count, 0);
	if (--state->ref_count > 0)
		return;

	wl_list_for_each_safe(colorop_state, tmp_colorop_state,
			      &state->colorop_state_list, link)
		drm_colorop_state_destroy(colorop_state);

	free(state);
}

static struct drm_colorop_state *
multiplier_create_colorop_state(struct drm_color_pipeline_state *pipeline_state,
				const struct drm_colorop *first_colorop,
				const struct drm_colorop *last_colorop,
				float multiplier)
{
	const struct drm_color_pipeline *pipeline = pipeline_state->pipeline;
	struct drm_colorop_state_object so = { 0 };
	const struct drm_colorop *colorop;
	bool found = false;

	/**
	 * The multiplier colorop must be between first_colorop and
	 * last_colorop (excluding both).
	 */
	colorop = first_colorop;
	while ((colorop = drm_colorop_iterate(pipeline, colorop))) {
		if (colorop == last_colorop)
			break;

		if (colorop->type == WDRM_COLOROP_TYPE_MULTIPLIER) {
			found = true;
			break;
		}
	}
	if (!found)
		return NULL;

	so.type = COLOROP_OBJECT_TYPE_MULTIPLIER;
	so.multiplier = float_to_s31_32_sign_magnitude(multiplier);

	return drm_colorop_state_create(pipeline_state, colorop, so);
}

static struct drm_colorop_state *
curve_create_colorop_state(struct drm_color_pipeline_state *pipeline_state,
			   const struct drm_colorop *previous_colorop,
			   struct weston_color_transform *xform,
			   enum weston_color_curve_step curve_step,
			   enum lowering_curve_policy policy)
{
	const struct drm_color_pipeline *pipeline = pipeline_state->pipeline;
	struct weston_compositor *compositor = pipeline->plane->base.compositor;
	struct drm_device *device = pipeline->plane->device;
	const struct drm_colorop_3x1d_lut_blob *lut_blob;
	struct weston_color_curve *curve;
	struct drm_colorop_state_object so = { 0 };
	const struct drm_colorop *colorop_curve;
	struct drm_colorop_state *cs_curve;
	struct drm_colorop_state *cs_multiplier = NULL;
	uint32_t lut_len;
	struct colorop_curve_scaler scaler = (struct colorop_curve_scaler) {
		.factor = 1.0f,
		.placement = PLACEMENT_NONE,
	};

	curve = (curve_step == WESTON_COLOR_CURVE_STEP_PRE) ? &xform->pre_curve :
							      &xform->post_curve;

	colorop_curve = search_colorop_compatible_curve(pipeline, previous_colorop,
							curve, policy);
	if (!colorop_curve)
		return NULL;

	switch (colorop_curve->type) {
	case WDRM_COLOROP_TYPE_1D_CURVE:
		so.type = COLOROP_OBJECT_TYPE_CURVE;
		so.curve = weston_tf_to_colorop_curve(curve->u.enumerated.tf.info,
						      curve->u.enumerated.tf_direction,
						      &scaler);
		break;
	case WDRM_COLOROP_TYPE_1D_LUT:
		lut_len = colorop_curve->size;
		lut_blob = drm_colorop_3x1d_lut_blob_from_curve(device, xform,
								curve_step, lut_len);
		if (!lut_blob)
			return NULL;
		so.type = COLOROP_OBJECT_TYPE_3x1D_LUT;
		so.lut_3x1d_blob_id = lut_blob->blob_id;
		break;
	default:
		weston_assert_not_reached(compositor,
					  "curve colorop should be 1D curve or 1D LUT");
	}

	/**
	 * Curve may require a multiplier colorop before or after it.
	 */

	if (scaler.placement == PLACEMENT_BEFORE)
		cs_multiplier = multiplier_create_colorop_state(pipeline_state,
								previous_colorop, /* first colorop */
								colorop_curve, /* last colorop */
								scaler.factor);

	cs_curve = drm_colorop_state_create(pipeline_state, colorop_curve, so);

	if (scaler.placement == PLACEMENT_AFTER)
		cs_multiplier = multiplier_create_colorop_state(pipeline_state,
								colorop_curve, /* first colorop */
								NULL, /* last colorop */
								scaler.factor);

	if (scaler.placement != PLACEMENT_NONE && !cs_multiplier) {
		drm_colorop_state_destroy(cs_curve);
		return NULL;
	}

	/* Return the colorop state of the colorop that comes later in the chain. */
	return (scaler.placement == PLACEMENT_AFTER) ? cs_multiplier : cs_curve;
}

static struct drm_colorop_state *
mapping_create_colorop_state(struct drm_color_pipeline_state *pipeline_state,
			     const struct drm_colorop *previous_colorop,
			     struct weston_color_transform *xform)
{
	const struct drm_color_pipeline *pipeline = pipeline_state->pipeline;
	struct weston_compositor *compositor = pipeline->plane->base.compositor;
	struct drm_device *device = pipeline->plane->device;
	struct weston_color_mapping *mapping = &xform->mapping;
	const struct drm_colorop_matrix_blob *mat_blob;
	struct drm_colorop_state_object so = { 0 };
	const struct drm_colorop *colorop;

	/* For now Weston has only matrices color mapping. */
	weston_assert_u32_eq(compositor,
			     mapping->type, WESTON_COLOR_MAPPING_TYPE_MATRIX);

	colorop = search_colorop_type(pipeline, previous_colorop,
				      WDRM_COLOROP_TYPE_CTM_3X4);
	if (!colorop)
		return NULL;

	mat_blob = drm_colorop_matrix_blob_from_mapping(device, xform);
	if (!mat_blob)
		return NULL;

	so.type = COLOROP_OBJECT_TYPE_MATRIX;
	so.matrix_blob_id = mat_blob->blob_id;

	return drm_colorop_state_create(pipeline_state, colorop, so);
}

static struct drm_color_pipeline_state *
drm_color_pipeline_state_from_xform_steps(struct drm_color_pipeline *pipeline,
					  struct weston_color_transform *xform,
					  enum lowering_curve_policy policy,
					  const char *indent)
{
	struct drm_backend *b = pipeline->plane->device->backend;
	struct drm_color_pipeline_state *pipeline_state;
	struct drm_colorop_state *colorop_state;
	const struct drm_colorop *previous_colorop;
	uint32_t type;

	pipeline_state = drm_color_pipeline_state_create(pipeline);

	/* First previous_colorop: none. */
	previous_colorop = NULL;

	/* Find colorop for pre-curve. */
	type = xform->pre_curve.type;
	if (type != WESTON_COLOR_CURVE_TYPE_IDENTITY) {
		colorop_state = curve_create_colorop_state(pipeline_state,
							   previous_colorop, xform,
							   WESTON_COLOR_CURVE_STEP_PRE,
							   policy);
		if (!colorop_state)
			goto err;

		previous_colorop = colorop_state->colorop;
	}

	/* Find colorop for color mapping. */
	type = xform->mapping.type;
	if (type != WESTON_COLOR_MAPPING_TYPE_IDENTITY) {
		colorop_state = mapping_create_colorop_state(pipeline_state,
							     previous_colorop, xform);
		if (!colorop_state)
			goto err;

		previous_colorop = colorop_state->colorop;
	}

	/* Find colorop for post-curve. */
	type = xform->post_curve.type;
	if (type != WESTON_COLOR_CURVE_TYPE_IDENTITY) {
		colorop_state = curve_create_colorop_state(pipeline_state,
							   previous_colorop, xform,
							   WESTON_COLOR_CURVE_STEP_POST,
							   policy);
		if (!colorop_state)
			goto err;
	}

	drm_debug(b, "%s[colorop] color pipeline id %u IS compatible with xform t%u;\n" \
		     "%s          policy: %s\n",
		     indent, pipeline->id, xform->id, indent,
		     lowering_curve_policy_str(policy));
	return pipeline_state;

err:
	drm_color_pipeline_state_unref(pipeline_state);
	drm_debug(b, "%s[colorop] color pipeline id %u NOT compatible with xform t%u;\n" \
		     "%s          policy: %s\n",
		     indent, pipeline->id, xform->id, indent,
		     lowering_curve_policy_str(policy));
	return NULL;
}

static struct drm_color_pipeline_state *
drm_color_pipeline_state_from_xform_decomposed(struct drm_color_pipeline *pipeline,
					       struct weston_color_transform *xform,
					       const char *indent)
{
	struct drm_device *device = pipeline->plane->device;
	struct drm_backend *b = device->backend;
	struct drm_color_pipeline_state *pipeline_state = NULL;
	const struct drm_colorop *colorop_shaper, *colorop_clut;
	struct drm_colorop_state_object so_clut = { 0 };
	struct drm_colorop_state_object so_shaper = { 0 };
	const struct drm_colorop_clut_blob *clut;

	/* Find colorop for shaper (3x1D LUT). */
	colorop_shaper = search_colorop_type(pipeline,
					     NULL, /* previous colorop (none) */
					     WDRM_COLOROP_TYPE_1D_LUT);
	if (!colorop_shaper)
		goto out;

	/* Find colorop for 3D cLUT. */
	colorop_clut = search_colorop_type(pipeline,
					   colorop_shaper, /* previous colorop */
					   WDRM_COLOROP_TYPE_3D_LUT);
	if (!colorop_clut)
		goto out;

	clut = drm_colorop_clut_blob_from_xform(device, xform,
						colorop_shaper->size,
						colorop_clut->size);
	if (!clut)
		goto out;

	/* Create pipeline state and fill with the colorops. */
	pipeline_state = drm_color_pipeline_state_create(pipeline);

	so_shaper.type = COLOROP_OBJECT_TYPE_3x1D_LUT;
	so_shaper.lut_3x1d_blob_id = clut->shaper_blob_id;
	drm_colorop_state_create(pipeline_state, colorop_shaper, so_shaper);

	so_clut.type = COLOROP_OBJECT_TYPE_3D_LUT;
	so_clut.lut_3d_blob_id = clut->clut_blob_id;
	drm_colorop_state_create(pipeline_state, colorop_clut, so_clut);

out:
	drm_debug(b, "%s[colorop] color pipeline id %u %s compatible with xform id %u;\n" \
		     "%s          xform decomposed into shaper + 3D LUT\n",
		     indent, pipeline->id,
		     pipeline_state ? "IS" : "NOT",
		     xform->id, indent);
	return pipeline_state;
}

/**
 * Given a color transformation, returns a color pipeline state that can
 * be used to offload such xform to KMS.
 *
 * @param plane The DRM plane that we plan to use to offload the view.
 * @param xform The xform to offload.
 * @param indent To print debug error messages with proper indentation.
 * @return The color pipeline state, or NULL if no color pipelines are
 * compatible with the xform.
 */
struct drm_color_pipeline_state *
drm_color_pipeline_state_from_xform(struct drm_plane *plane,
				    struct weston_color_transform *xform,
				    const char *indent)
{
	struct drm_backend *b = plane->device->backend;
	struct drm_color_pipeline_state *pipeline_state;
	unsigned int i, mode_index;
	enum lowering_curve_policy policy;
	enum lowering_curve_policy policy_modes[2] = {
		LOWERING_CURVE_POLICY_DENY, LOWERING_CURVE_POLICY_ALLOW
	};

	drm_debug(b, "%s[colorop] searching color pipeline compatible with xform t%u\n",
		     indent, xform->id);

	/**
	 * Try to find a compatible pipeline.
	 *
	 * First, we try to find a compatible pipeline but not allowing Weston
	 * enumerated color curves to be lowered to parametric. If we can't find
	 * something, we start allowing that.
	 */
	if (xform->steps_valid) {
		for (mode_index = 0; mode_index < ARRAY_LENGTH(policy_modes); mode_index++) {
			policy = policy_modes[mode_index];
			for (i = 0; i < plane->num_color_pipelines; i++) {
				pipeline_state =
					drm_color_pipeline_state_from_xform_steps(&plane->pipelines[i],
										  xform, policy, indent);
				if (pipeline_state)
					return pipeline_state;
			}
		}
	}

	/**
	 * Either the pipelines are not compatible with our xform or we were
	 * unable to optimize the xform to steps. Our last resource would be
	 * crafting a shaper + 3D LUT from the xform. Let's check if any
	 * pipelines would be able to handle that.
	 */
	for (i = 0; i < plane->num_color_pipelines; i++) {
		pipeline_state =
			drm_color_pipeline_state_from_xform_decomposed(&plane->pipelines[i],
								       xform, indent);
		if (pipeline_state)
			return pipeline_state;
	}

	return NULL;
}

static void
drm_color_pipeline_print(struct drm_color_pipeline *pipeline, FILE *fp)
{
	struct drm_colorop *colorop;
	struct drm_property_info *curve_props;
	const char *type;
	const char *sep = "	";
	unsigned int i;

	if (!fp)
		return;

	fprintf(fp, "[colorop] color pipeline %u (owned by plane %u):\n",
		    pipeline->id, pipeline->plane->plane_id);

	wl_list_for_each(colorop, &pipeline->colorop_list, link) {
		type = drm_colorop_type_to_str(colorop);

		fprintf(fp, "%s[colorop] id %u, type %s, can bypass? %s",
			    sep, colorop->id, type, yesno(colorop->can_bypass));

		if (colorop->type == WDRM_COLOROP_TYPE_1D_CURVE) {
			curve_props = &colorop->props[WDRM_COLOROP_CURVE_1D];
			for (i = 0; i < curve_props->num_enum_values; i++) {
				if (curve_props->enum_values[i].valid)
					fprintf(fp, " [%s]",
						    curve_props->enum_values[i].name);
			}
		}

		fprintf(fp, "\n");
	}
}

/**
 * Populates the color pipelines of a DRM plane.
 *
 * This does nothing if the driver does not support color pipelines.
 *
 * @param plane The DRM plane whose pipelines this populates.
 * @param plane_props The DRM plane's props.
 */
void
drm_plane_populate_color_pipelines(struct drm_plane *plane,
				   drmModeObjectPropertiesPtr plane_props)
{
	struct weston_compositor *compositor = plane->base.compositor;
	struct drm_device *device = plane->device;
	struct drm_backend *b = device->backend;
	FILE *dbg = weston_log_scope_stream(b->debug);
	drmModePropertyRes *color_pipeline_props;
	uint32_t pipeline_i;
	unsigned int i;

	if (plane->props[WDRM_PLANE_COLOR_PIPELINE].prop_id == 0)
		return;

	color_pipeline_props =
		drmModeGetProperty(device->kms_device->fd,
				   plane->props[WDRM_PLANE_COLOR_PIPELINE].prop_id);
	if (!color_pipeline_props) {
		drm_debug(b, "failed to get color pipeline property for plane %u\n",
			     plane->plane_id);
		return;
	}

	plane->num_color_pipelines = 0;
	for (i = 0; (int)i < color_pipeline_props->count_enums; i++) {
		if (color_pipeline_props->enums[i].value != 0)
			plane->num_color_pipelines++;
	}
	plane->pipelines = xzalloc(plane->num_color_pipelines *
				   sizeof(*plane->pipelines));

	/* Populate pipelines. */
	pipeline_i = 0;
	for (i = 0; (int)i < color_pipeline_props->count_enums; i++) {
		struct drm_color_pipeline *pipeline;
		struct drm_colorop *colorop;
		uint32_t colorop_id, next_colorop_id;

		/* First colorop. */
		colorop_id = color_pipeline_props->enums[i].value;
		if (colorop_id == 0)
			continue;

		pipeline = &plane->pipelines[pipeline_i++];

		pipeline->plane = plane;
		wl_list_init(&pipeline->colorop_list);

		/* Id of the pipeline is the same of its first colorop. */
		pipeline->id = colorop_id;

		while (colorop_id != 0) {
			colorop = drm_colorop_create(pipeline, colorop_id, &next_colorop_id);
			if (!colorop) {
				drm_debug(b, "[colorop] failed to create colorop for id %u, destroying color pipelines for plane %u\n",
					     colorop_id, plane->plane_id);
				drm_plane_release_color_pipelines(plane);
				goto out;
			}
			colorop_id = next_colorop_id;
		}

		weston_assert_list_not_empty(compositor, &pipeline->colorop_list);
		if (dbg) {
			drm_color_pipeline_print(pipeline, dbg);
			fflush(dbg);
		}
	}
	weston_assert_u32_eq(b->compositor,
			     plane->num_color_pipelines, pipeline_i);

out:
	drmModeFreeProperty(color_pipeline_props);
}

/**
 * Release the color pipelines of a drm plane.
 *
 * @param plane The drm plane whose pipelines should be released.
 */
void
drm_plane_release_color_pipelines(struct drm_plane *plane)
{
	struct drm_color_pipeline *pipeline;
	struct drm_colorop *colorop, *tmp;
	unsigned int i;

	for (i = 0; i < plane->num_color_pipelines; i++) {
		pipeline = &plane->pipelines[i];
		wl_list_for_each_safe(colorop, tmp, &pipeline->colorop_list, link)
			drm_colorop_destroy(colorop);
	}

	plane->num_color_pipelines = 0;
	free(plane->pipelines);
	plane->pipelines = NULL;
}
