/* Test-only weston module for the layer-shell-protocol suite
 * (tests/protocol/run-layer-shell-protocol-test.sh). Never installed.
 *
 * The headless suite had no input at all: --fake-seat gives a wl_seat with
 * a pointer and keyboard but nothing ever drives them, so every grab serial
 * a client could name was stale and no popup grab could be taken. The D9
 * layer-chain tests need REAL grabs — a press serial, an outside press that
 * dismisses, a touch down — so this module adds a seat with a pointer, a
 * keyboard and a touchscreen (the fake seat has no touch) and drives it from
 * the client over weston's own test protocol (weston_test, the XML vendored
 * at libweston-vendored/src/protocol/weston-test.xml). Requests arrive on
 * the client's connection, so an input event is ordered with the client's
 * other requests; one roundtrip later its events have been delivered.
 *
 * Only move_pointer, send_button and send_touch are implemented (all to
 * the module's seat, named "qdwin-test-input"); the other requests are an
 * implementation error. */
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
#include <wayland-server.h>
#include <libweston/libweston.h>
#include "weston-test-server-protocol.h"

/* Backend-facing libweston exports, undeclared in the installed plugin
 * header (qdwin/qdwin.c declares them the same way). */
void weston_seat_init(struct weston_seat *seat,
		      struct weston_compositor *ec,
		      const char *seat_name);
int  weston_seat_init_pointer(struct weston_seat *seat);
int  weston_seat_init_keyboard(struct weston_seat *seat,
			       struct xkb_keymap *keymap);
int  weston_seat_init_touch(struct weston_seat *seat);
struct weston_touch_device *
weston_touch_create_touch_device(struct weston_touch *touch,
				 const char *syspath,
				 void *backend_data,
				 const struct weston_touch_device_ops *ops,
				 weston_touch_device_set_output_func_t set_output);
void notify_motion(const struct weston_pointer_motion_event *event);
void notify_button(const struct weston_pointer_button_event *event);
void notify_pointer_frame(struct weston_seat *seat);
void notify_touch_normalized(const struct weston_touch_event *event,
			     const struct weston_point2d_device_normalized *norm);
void notify_touch_frame(struct weston_touch_device *device);

struct test_input {
	struct weston_compositor *compositor;
	struct weston_seat seat;
	struct weston_touch_device *touch_device;
};

static void
timespec_from_proto(struct timespec *ts, uint32_t sec_hi, uint32_t sec_lo,
		    uint32_t nsec)
{
	ts->tv_sec = ((uint64_t)sec_hi << 32) + sec_lo;
	ts->tv_nsec = nsec;
}

static void
not_implemented(struct wl_client *client, const char *what)
{
	wl_client_post_implementation_error(client,
		"weston_test.%s is not implemented by qdwin's test module",
		what);
}

static void
move_surface(struct wl_client *client, struct wl_resource *resource,
	     struct wl_resource *surface, int32_t x, int32_t y)
{
	(void)resource; (void)surface; (void)x; (void)y;
	not_implemented(client, "move_surface");
}

static void
move_pointer(struct wl_client *client, struct wl_resource *resource,
	     uint32_t tv_sec_hi, uint32_t tv_sec_lo, uint32_t tv_nsec,
	     int32_t x, int32_t y)
{
	struct test_input *t = wl_resource_get_user_data(resource);
	struct weston_pointer *pointer = weston_seat_get_pointer(&t->seat);
	struct weston_pointer_motion_event event;
	struct weston_coord_global pos;
	struct timespec ts;

	(void)client;
	timespec_from_proto(&ts, tv_sec_hi, tv_sec_lo, tv_nsec);
	pos.c = weston_coord(x, y);
	weston_pointer_motion_event_init(&event, &ts, &t->seat,
					 WESTON_POINTER_MOTION_ABS,
					 &pos, NULL, NULL);
	notify_motion(&event);
	notify_pointer_frame(&t->seat);
	weston_test_send_pointer_position(resource,
					  wl_fixed_from_double(pointer->pos.c.x),
					  wl_fixed_from_double(pointer->pos.c.y));
}

static void
send_button(struct wl_client *client, struct wl_resource *resource,
	    uint32_t tv_sec_hi, uint32_t tv_sec_lo, uint32_t tv_nsec,
	    int32_t button, uint32_t state)
{
	struct test_input *t = wl_resource_get_user_data(resource);
	struct weston_pointer_button_event event;
	struct timespec ts;

	(void)client;
	timespec_from_proto(&ts, tv_sec_hi, tv_sec_lo, tv_nsec);
	weston_pointer_button_event_init(&event, &ts, &t->seat, button, state);
	notify_button(&event);
	notify_pointer_frame(&t->seat);
}

static void
send_axis(struct wl_client *client, struct wl_resource *resource,
	  uint32_t tv_sec_hi, uint32_t tv_sec_lo, uint32_t tv_nsec,
	  uint32_t axis, wl_fixed_t value)
{
	(void)resource; (void)tv_sec_hi; (void)tv_sec_lo; (void)tv_nsec;
	(void)axis; (void)value;
	not_implemented(client, "send_axis");
}

static void
activate_surface(struct wl_client *client, struct wl_resource *resource,
		 struct wl_resource *surface)
{
	(void)resource; (void)surface;
	not_implemented(client, "activate_surface");
}

static void
send_key(struct wl_client *client, struct wl_resource *resource,
	 uint32_t tv_sec_hi, uint32_t tv_sec_lo, uint32_t tv_nsec,
	 uint32_t key, uint32_t state)
{
	(void)resource; (void)tv_sec_hi; (void)tv_sec_lo; (void)tv_nsec;
	(void)key; (void)state;
	not_implemented(client, "send_key");
}

static void
device_release(struct wl_client *client, struct wl_resource *resource,
	       const char *device)
{
	(void)resource; (void)device;
	not_implemented(client, "device_release");
}

static void
device_add(struct wl_client *client, struct wl_resource *resource,
	   const char *device)
{
	(void)resource; (void)device;
	not_implemented(client, "device_add");
}

static void
send_touch(struct wl_client *client, struct wl_resource *resource,
	   uint32_t tv_sec_hi, uint32_t tv_sec_lo, uint32_t tv_nsec,
	   int32_t touch_id, wl_fixed_t x, wl_fixed_t y, uint32_t touch_type)
{
	struct test_input *t = wl_resource_get_user_data(resource);
	struct weston_touch_event event;
	struct weston_coord_global pos;
	struct timespec ts;

	(void)client;
	if (touch_type == WL_TOUCH_UP && (x != 0 || y != 0)) {
		wl_resource_post_error(resource,
			WESTON_TEST_ERROR_TOUCH_UP_WITH_COORDINATE,
			"touch up must have 0,0 coordinates");
		return;
	}
	timespec_from_proto(&ts, tv_sec_hi, tv_sec_lo, tv_nsec);
	pos.c = weston_coord(wl_fixed_to_double(x), wl_fixed_to_double(y));
	weston_touch_event_init(&event, &ts, &t->seat, t->touch_device,
				touch_type, touch_id, &pos);
	notify_touch_normalized(&event, NULL);
	notify_touch_frame(t->touch_device);
}

static void
client_break(struct wl_client *client, struct wl_resource *resource,
	     uint32_t breakpoint, uint32_t resource_id)
{
	(void)resource; (void)breakpoint; (void)resource_id;
	not_implemented(client, "client_break");
}

static const struct weston_test_interface test_input_implementation = {
	.move_surface     = move_surface,
	.move_pointer     = move_pointer,
	.send_button      = send_button,
	.send_axis        = send_axis,
	.activate_surface = activate_surface,
	.send_key         = send_key,
	.device_release   = device_release,
	.device_add       = device_add,
	.send_touch       = send_touch,
	.client_break     = client_break,
};

static void
bind_test(struct wl_client *client, void *data, uint32_t version,
	  uint32_t id)
{
	struct wl_resource *resource =
		wl_resource_create(client, &weston_test_interface, version, id);

	if (!resource) {
		wl_client_post_no_memory(client);
		return;
	}
	wl_resource_set_implementation(resource, &test_input_implementation,
				       data, NULL);
}

WL_EXPORT int
wet_module_init(struct weston_compositor *compositor,
		int *argc, char *argv[])
{
	struct test_input *t = calloc(1, sizeof *t);

	(void)argc; (void)argv;
	if (!t)
		return -1;
	t->compositor = compositor;
	weston_seat_init(&t->seat, compositor, "qdwin-test-input");
	if (weston_seat_init_pointer(&t->seat) < 0 ||
	    weston_seat_init_keyboard(&t->seat, NULL) < 0 ||
	    weston_seat_init_touch(&t->seat) < 0)
		return -1;
	t->touch_device = weston_touch_create_touch_device(
		t->seat.touch_state, "qdwin-test-input-touch", NULL,
		NULL /* not calibratable: coordinates are global */, NULL);
	if (!t->touch_device)
		return -1;
	if (!wl_global_create(compositor->wl_display, &weston_test_interface,
			      1, t, bind_test))
		return -1;
	weston_log("qdwin-test-input: seat ready (pointer, keyboard, touch)\n");
	return 0;
}
