#!/usr/bin/env python3
"""Compile production clipboard dispatch/lifecycle functions against real Wayland.

Ensures: only an explicit decision delivers bytes to the actual offer client;
shell loss, OOM, missing endpoints and timer failures yield EOF. No display or
host input is used: real wl_client/resources and pipes run on a private display.
"""
from pathlib import Path
import subprocess
import sys
import tempfile

source = Path(sys.argv[1]).read_text()
weston = Path(sys.argv[2]).read_text()
start = source.index('struct qdwin_data_source_wrap {')
end = source.index('\nstatic struct qdwin_data_offer_pending *\nqdwin_data_offer_pending_find', start)
block = source[start:end]
start = weston.index('static void\ndata_offer_receive(')
end = weston.index('\nstatic void\ndata_offer_destroy(', start)
dispatch = weston[start:end]
header = r'''
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/socket.h>
#include <wayland-server.h>
#define QDWIN_MIME_TYPE_MAX 4096
struct weston_data_source;
struct weston_data_offer { struct wl_resource *resource; struct weston_data_source *source; };
struct weston_data_source { struct wl_resource *resource; struct weston_data_offer *offer;
 struct wl_signal destroy_signal; void (*send)(struct weston_data_source *, const char *, int32_t); };
struct weston_seat { const char *seat_name; };
struct weston_compositor { struct wl_display *wl_display; };
struct qdwin { struct wl_resource *shell_resource; struct weston_compositor *compositor;
 struct wl_list data_source_wraps, data_offer_pending; uint32_t data_offer_receive_next_handle; };
struct qdwin_toplevel { uint32_t handle; };
static struct qdwin *qdwin_singleton;
static struct wl_client *source_client, *target_client;
static struct qdwin_toplevel source_tl = {11}, target_tl = {22}, focus_tl = {33};
static int ready = 1, fail_alloc, fail_timer, fail_update, events, sends;
static uint32_t reported_source, reported_target;
static void *checked_calloc(size_t n, size_t size) { if (fail_alloc) {fail_alloc=0; return NULL;} return calloc(n,size); }
#define calloc checked_calloc
static struct wl_event_source *checked_timer(struct wl_event_loop *l, wl_event_loop_timer_func_t cb, void *d) {
 if (fail_timer) return NULL; return wl_event_loop_add_timer(l,cb,d); }
#define wl_event_loop_add_timer checked_timer
static int checked_update(struct wl_event_source *s, int ms) { if(fail_update) return -1; return wl_event_source_timer_update(s,ms); }
#define wl_event_source_timer_update checked_update
static void weston_log(const char *fmt, ...) { }
static int qdwin_shell_can_receive_v15(struct qdwin *q) {return ready;}
static struct qdwin_toplevel *qdwin_toplevel_for_client(struct qdwin *q, struct wl_client *c) {
 return c == source_client ? &source_tl : c == target_client ? &target_tl : NULL; }
static struct qdwin_toplevel *qdwin_toplevel_for_keyboard_focus(struct qdwin *q,struct weston_seat *s) {return &focus_tl;}
static void qdwin_shell_v1_send_data_offer_receive_pending(struct wl_resource *r,uint32_t h,const char *seat,uint32_t src,uint32_t dst,const char *mime) {
 events++; reported_source=src; reported_target=dst; }
static void original_send(struct weston_data_source *s, const char *mime, int32_t fd) { sends++; assert(write(fd,"secret",6)==6); close(fd); }
'''
main = r'''
static struct wl_client *client_new(struct wl_display *d, int *peer) {
 int pair[2]; assert(socketpair(AF_UNIX,SOCK_STREAM,0,pair)==0); *peer=pair[1];
 struct wl_client *c=wl_client_create(d,pair[0]); assert(c); return c;
}
static void check_eof(int fd) { char b; assert(read(fd,&b,1)==0); close(fd); }
static void receive(struct weston_data_offer *offer,int *reader) {
 int p[2]; assert(pipe(p)==0); *reader=p[0];
 data_offer_receive(wl_resource_get_client(offer->resource),offer->resource,"text/plain",p[1]);
}
int main(void) {
 struct wl_display *d=wl_display_create(); assert(d);
 int sp,tp,bp; source_client=client_new(d,&sp); target_client=client_new(d,&tp);
 struct wl_client *background=client_new(d,&bp);
 struct weston_compositor wc={d}; struct qdwin q={.compositor=&wc};
 qdwin_singleton=&q; wl_list_init(&q.data_source_wraps); wl_list_init(&q.data_offer_pending);
 struct weston_seat seat={"test"};
 struct weston_data_source src={.resource=wl_resource_create(source_client,&wl_data_source_interface,3,0),.send=original_send};
 struct weston_data_offer offer={.resource=wl_resource_create(target_client,&wl_data_offer_interface,3,0),.source=&src};
 src.offer=&offer; wl_signal_init(&src.destroy_signal); wl_resource_set_user_data(offer.resource,&offer);
 qdwin_install_data_source_wrap(&q,&seat,&src);
 int fd; receive(&offer,&fd); assert(events==1 && sends==0);
 assert(reported_source==11 && reported_target==22); // focus is a third client
 struct qdwin_data_offer_pending *p=wl_container_of(q.data_offer_pending.next,p,link);
 qdwin_data_offer_pending_close(p,0); qdwin_data_offer_pending_free(p); check_eof(fd);
 receive(&offer,&fd); p=wl_container_of(q.data_offer_pending.next,p,link);
 qdwin_data_offer_pending_close(p,1); qdwin_data_offer_pending_free(p);
 char payload[8]={0}; assert(read(fd,payload,sizeof(payload))==6 && !strcmp(payload,"secret")); check_eof(fd);
 // Shell teardown closes pending FDs and grants cannot survive a restart.
 receive(&offer,&fd); qdwin_data_offer_pending_free_all(&q); check_eof(fd);
 ready=0; receive(&offer,&fd); check_eof(fd); assert(sends==1);
 // A selection first created during shell absence is still wrapped.
 struct weston_data_source offline=src; wl_signal_init(&offline.destroy_signal); offline.send=original_send;
 offer.source=&offline; offline.offer=&offer; qdwin_install_data_source_wrap(&q,&seat,&offline);
 receive(&offer,&fd); check_eof(fd); ready=1;
 receive(&offer,&fd); assert(!wl_list_empty(&q.data_offer_pending)); qdwin_data_offer_pending_free_all(&q); check_eof(fd);
 // Both pending allocation and timer creation/arming failures must deny.
 fail_alloc=1; receive(&offer,&fd); check_eof(fd);
 fail_timer=1; receive(&offer,&fd); check_eof(fd); fail_timer=0;
 fail_update=1; receive(&offer,&fd); check_eof(fd); fail_update=0;
 assert(wl_list_empty(&q.data_offer_pending));
 // Wrapper OOM disables the source permanently, instead of exposing send.
 struct weston_data_source oom=src; wl_signal_init(&oom.destroy_signal); oom.send=original_send;
 fail_alloc=1; qdwin_install_data_source_wrap(&q,&seat,&oom); offer.source=&oom; oom.offer=&offer;
 receive(&offer,&fd); check_eof(fd); assert(sends==1);
 qdwin_install_data_source_wrap(&q,&seat,&oom); // no recursive shim on retry
 receive(&offer,&fd); check_eof(fd);
 // A real one-shot timer callback removes its timer and pending request.
 offer.source=&src; receive(&offer,&fd); p=wl_container_of(q.data_offer_pending.next,p,link);
 assert(wl_event_source_timer_update(p->timeout_source,1)==0);
 wl_event_loop_dispatch(wl_display_get_event_loop(d),20); check_eof(fd);
 assert(wl_list_empty(&q.data_offer_pending));
 // Source destruction and recipient disconnection both revoke pending FDs.
 receive(&offer,&fd); wl_signal_emit(&src.destroy_signal,&src); check_eof(fd);
 assert(wl_list_empty(&q.data_offer_pending));
 offer.source=&offline; receive(&offer,&fd); wl_client_destroy(target_client); target_client=NULL; check_eof(fd);
 assert(wl_list_empty(&q.data_offer_pending));
 // An unmapped recipient cannot borrow focus or a same-app connection.
 offer.source=&offline;
 offer.resource=wl_resource_create(background,&wl_data_offer_interface,3,0); wl_resource_set_user_data(offer.resource,&offer);
 receive(&offer,&fd); check_eof(fd); assert(sends==1);
 qdwin_data_offer_pending_free_all(&q); qdwin_data_source_wraps_free_all(&q);
 wl_display_destroy(d); close(sp); close(tp); close(bp);
 puts("PASS: real Wayland offer clients remain distinct; deny/outage/OOM/timer paths deliver zero bytes; explicit allow delivers secret");
}
'''
# Also pin the lifecycle call-site: the tested cancellation must run on loss.
shell_destroy = source[source.index('static void\nqdwin_shell_resource_destroy('):source.index('static char *qdwin_proc_exe', source.index('static void\nqdwin_shell_resource_destroy('))]
assert 'qdwin_data_offer_pending_free_all(qdwin);' in shell_destroy
with tempfile.TemporaryDirectory(prefix='qdwin-clipboard-') as temp:
    c = Path(temp)/'receive.c'; exe=Path(temp)/'receive'
    c.write_text(header+block+dispatch+main)
    flags=subprocess.check_output(['pkg-config','--cflags','--libs','wayland-server'],text=True).split()
    subprocess.run(['cc','-std=gnu11','-g',str(c),'-o',str(exe),*flags],check=True)
    subprocess.run([str(exe)],check=True)
