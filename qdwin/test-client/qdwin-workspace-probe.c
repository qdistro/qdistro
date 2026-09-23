/*
 * qdwin-workspace-probe — host test client for qdwin's ext-workspace-v1
 * server (the v24 workspace implementation, qdwin/qdwin.c).
 *
 * qdwin implements the standard ext_workspace_manager_v1: on bind it
 * streams one workspace_group + one ext_workspace_handle_v1 per workspace,
 * each carrying a 1-D coordinate (== index) and a state bitmask whose
 * ACTIVE bit (1) marks the active workspace, then a `done`. activate +
 * commit on a handle switches the active workspace; the compositor
 * re-broadcasts state and fires `done` again.
 *
 * This probe binds the manager, syncs the initial set, and (optionally)
 * activates a workspace and re-reads the active index — asserting the
 * compositor honoured the switch. Pure protocol, no pixels: it runs
 * against a headless qdwin in the host gate.
 *
 * Modes:
 *   (default)         sync and print "workspaces=N active=A".
 *   --expect-count=N  assert exactly N workspaces (else exit 1).
 *   --activate=K      activate workspace index K, then assert the active
 *                     index became K (else exit 1).
 *   --expect-output-enter[=N]
 *                     assert the freshly bound group carried at least one
 *                     output_enter (or exactly N, if N given) BEFORE the
 *                     first manager `done`. Covers the bind-time
 *                     group→output_enter association (fix #1): per-monitor
 *                     bars key off this to know the group spans their
 *                     output. The headless backend advertises a single
 *                     output, so `--expect-output-enter=1` is exact there.
 *   --batch-activate=K
 *                     drive the atomic-commit batching (fix #3): issue
 *                     create_workspace + activate(K) WITHOUT commit, prove
 *                     no state change is observed yet (count and active
 *                     index unchanged), then commit and prove BOTH landed
 *                     atomically (count grew by one and active became K).
 *                     Requires K to name an EXISTING workspace at bind time.
 *
 * Exit codes:
 *   0  success (and any assertion held)
 *   1  an assertion failed, or a protocol error was posted
 *   2  setup error (no display / manager global not advertised)
 *
 * SPDX-License-Identifier: MIT
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>

#include <wayland-client.h>
#include "ext-workspace-v1-client-protocol.h"

#define MAX_WS 64
#define MAX_OUTPUTS 16

struct ws_entry {
	struct ext_workspace_handle_v1 *proxy;
	uint32_t coord;
	int have_coord;
	uint32_t state;
	int removed;
};

struct probe {
	struct wl_display *display;
	struct wl_registry *registry;
	struct ext_workspace_manager_v1 *manager;
	struct ext_workspace_group_handle_v1 *group;
	struct ws_entry ws[MAX_WS];
	int ws_count;
	int done_seen;
	/* group→output association (fix #1). output_enter_total counts every
	 * output_enter on the group; output_enter_at_bind snapshots that
	 * count at the moment the FIRST manager `done` arrives, i.e. how many
	 * the bind burst carried before the compositor declared the initial
	 * state consistent. */
	int output_enter_total;
	int output_leave_total;
	int output_enter_at_bind;
	/* wl_output globals, bound BEFORE the manager so the compositor can
	 * reference them in the group's output_enter (fix #1): output_enter
	 * carries a wl_output object the client must already own. */
	uint32_t output_names[MAX_OUTPUTS];
	struct wl_output *outputs[MAX_OUTPUTS];
	int output_count;
};

/* ---- handle ---- */
static struct ws_entry *entry_for(struct probe *p,
				  struct ext_workspace_handle_v1 *h)
{
	for (int i = 0; i < p->ws_count; i++)
		if (p->ws[i].proxy == h)
			return &p->ws[i];
	return NULL;
}

static void h_id(void *d, struct ext_workspace_handle_v1 *h, const char *id)
{ (void)d; (void)h; (void)id; }
static void h_name(void *d, struct ext_workspace_handle_v1 *h, const char *n)
{ (void)d; (void)h; (void)n; }
static void h_coordinates(void *d, struct ext_workspace_handle_v1 *h,
			  struct wl_array *coords)
{
	struct ws_entry *e = entry_for(d, h);
	if (e && coords && coords->size >= sizeof(uint32_t)) {
		e->coord = *(uint32_t *)coords->data;
		e->have_coord = 1;
	}
}
static void h_state(void *d, struct ext_workspace_handle_v1 *h, uint32_t state)
{
	struct ws_entry *e = entry_for(d, h);
	if (e)
		e->state = state;
}
static void h_capabilities(void *d, struct ext_workspace_handle_v1 *h,
			   uint32_t caps)
{ (void)d; (void)h; (void)caps; }
static void h_removed(void *d, struct ext_workspace_handle_v1 *h)
{
	struct ws_entry *e = entry_for(d, h);
	if (e)
		e->removed = 1;
}
static const struct ext_workspace_handle_v1_listener handle_listener = {
	.id = h_id, .name = h_name, .coordinates = h_coordinates,
	.state = h_state, .capabilities = h_capabilities, .removed = h_removed,
};

/* ---- group (we ignore membership; single group) ---- */
static void g_caps(void *d, struct ext_workspace_group_handle_v1 *g, uint32_t c)
{ (void)d; (void)g; (void)c; }
static void g_oenter(void *d, struct ext_workspace_group_handle_v1 *g,
		     struct wl_output *o)
{ struct probe *p = d; (void)g; (void)o; p->output_enter_total++; }
static void g_oleave(void *d, struct ext_workspace_group_handle_v1 *g,
		     struct wl_output *o)
{ struct probe *p = d; (void)g; (void)o; p->output_leave_total++; }
static void g_wenter(void *d, struct ext_workspace_group_handle_v1 *g,
		     struct ext_workspace_handle_v1 *w)
{ (void)d; (void)g; (void)w; }
static void g_wleave(void *d, struct ext_workspace_group_handle_v1 *g,
		     struct ext_workspace_handle_v1 *w)
{ (void)d; (void)g; (void)w; }
static void g_removed(void *d, struct ext_workspace_group_handle_v1 *g)
{ (void)d; (void)g; }
static const struct ext_workspace_group_handle_v1_listener group_listener = {
	.capabilities = g_caps, .output_enter = g_oenter,
	.output_leave = g_oleave, .workspace_enter = g_wenter,
	.workspace_leave = g_wleave, .removed = g_removed,
};

/* ---- manager ---- */
static void m_workspace_group(void *d, struct ext_workspace_manager_v1 *m,
			      struct ext_workspace_group_handle_v1 *grp)
{
	struct probe *p = d;
	(void)m;
	p->group = grp;
	ext_workspace_group_handle_v1_add_listener(grp, &group_listener, p);
}
static void m_workspace(void *d, struct ext_workspace_manager_v1 *m,
			struct ext_workspace_handle_v1 *ws)
{
	struct probe *p = d;
	(void)m;
	if (p->ws_count < MAX_WS) {
		p->ws[p->ws_count].proxy = ws;
		p->ws_count++;
		ext_workspace_handle_v1_add_listener(ws, &handle_listener, p);
	}
}
static void m_done(void *d, struct ext_workspace_manager_v1 *m)
{
	struct probe *p = d;
	(void)m;
	/* The bind burst (group + handles + the group's output_enter) is
	 * terminated by the first `done`. Snapshot the output_enter count at
	 * that boundary so --expect-output-enter asserts on what bind sent,
	 * not on any later hotplug-driven enter/leave. */
	if (p->done_seen == 0)
		p->output_enter_at_bind = p->output_enter_total;
	p->done_seen++;
}
static void m_finished(void *d, struct ext_workspace_manager_v1 *m)
{ (void)d; (void)m; }
static const struct ext_workspace_manager_v1_listener manager_listener = {
	.workspace_group = m_workspace_group, .workspace = m_workspace,
	.done = m_done, .finished = m_finished,
};

/* The registry has two phases. First pass: bind every wl_output and record
 * the manager's global name (but do NOT bind it yet). Second pass binds the
 * manager — by then the wl_output binds have round-tripped, so the
 * compositor's bind handler can emit output_enter referencing them. */
static uint32_t g_manager_name;
static int g_manager_version;
static int g_bind_manager_now;

static void on_global(void *data, struct wl_registry *reg, uint32_t name,
		      const char *interface, uint32_t version)
{
	struct probe *p = data;
	if (strcmp(interface, ext_workspace_manager_v1_interface.name) == 0) {
		g_manager_name = name;
		g_manager_version = version < 1 ? (int)version : 1;
		if (g_bind_manager_now)
			p->manager = wl_registry_bind(
				reg, name, &ext_workspace_manager_v1_interface,
				g_manager_version);
	} else if (strcmp(interface, wl_output_interface.name) == 0 &&
		   p->output_count < MAX_OUTPUTS) {
		/* Bind at v1 — we only need the object identity for output_enter
		 * matching, not geometry events. */
		p->output_names[p->output_count] = name;
		p->outputs[p->output_count] =
			wl_registry_bind(reg, name, &wl_output_interface, 1);
		p->output_count++;
	}
}
static void on_global_remove(void *d, struct wl_registry *r, uint32_t n)
{ (void)d; (void)r; (void)n; }
static const struct wl_registry_listener registry_listener = {
	.global = on_global, .global_remove = on_global_remove,
};

/* Live (not removed) workspace count. */
static int live_count(struct probe *p)
{
	int n = 0;
	for (int i = 0; i < p->ws_count; i++)
		if (!p->ws[i].removed)
			n++;
	return n;
}

/* Active workspace index, by coordinate (matches qdwin's index). -1 if
 * none flagged active. */
static int active_index(struct probe *p)
{
	for (int i = 0; i < p->ws_count; i++)
		if (!p->ws[i].removed && (p->ws[i].state & 1u))
			return p->ws[i].have_coord ? (int)p->ws[i].coord : i;
	return -1;
}

static struct ws_entry *entry_by_coord(struct probe *p, uint32_t coord)
{
	for (int i = 0; i < p->ws_count; i++)
		if (!p->ws[i].removed && p->ws[i].have_coord &&
		    p->ws[i].coord == coord)
			return &p->ws[i];
	return NULL;
}

int main(int argc, char *argv[])
{
	int expect_count = -1;
	int activate = -1;
	int batch_activate = -1;
	int expect_output_enter = -1;   /* -1 off, 0 = "at least one", >0 exact */
	for (int i = 1; i < argc; i++) {
		if (strncmp(argv[i], "--expect-count=", 15) == 0)
			expect_count = atoi(argv[i] + 15);
		else if (strncmp(argv[i], "--activate=", 11) == 0)
			activate = atoi(argv[i] + 11);
		else if (strncmp(argv[i], "--batch-activate=", 17) == 0)
			batch_activate = atoi(argv[i] + 17);
		else if (strncmp(argv[i], "--expect-output-enter=", 22) == 0)
			expect_output_enter = atoi(argv[i] + 22);
		else if (strcmp(argv[i], "--expect-output-enter") == 0)
			expect_output_enter = 0;   /* at least one */
	}

	struct probe p = {0};
	p.display = wl_display_connect(NULL);
	if (!p.display) {
		fprintf(stderr, "qdwin-workspace-probe: wl_display_connect "
			"failed: %s\n", strerror(errno));
		return 2;
	}
	p.registry = wl_display_get_registry(p.display);
	wl_registry_add_listener(p.registry, &registry_listener, &p);
	/* Phase 1: discover globals, bind the wl_output(s), but NOT the
	 * manager yet — so the manager bind handler runs only after our
	 * output binds have round-tripped and the compositor can reference
	 * them in the group's output_enter. */
	wl_display_roundtrip(p.display);

	if (g_manager_name == 0) {
		fprintf(stderr, "qdwin-workspace-probe: ext_workspace_manager_v1 "
			"not advertised\n");
		return 2;
	}
	/* Make sure the wl_output binds have landed server-side before we ask
	 * for the manager. */
	wl_display_roundtrip(p.display);

	/* Phase 2: bind the manager. */
	g_bind_manager_now = 1;
	p.manager = wl_registry_bind(p.registry, g_manager_name,
				     &ext_workspace_manager_v1_interface,
				     g_manager_version);
	if (!p.manager) {
		fprintf(stderr, "qdwin-workspace-probe: manager bind failed\n");
		return 2;
	}
	ext_workspace_manager_v1_add_listener(p.manager, &manager_listener, &p);
	/* Drain the initial workspace burst + done. */
	wl_display_roundtrip(p.display);
	wl_display_roundtrip(p.display);

	int count = live_count(&p);
	int active = active_index(&p);
	printf("qdwin-workspace-probe: workspaces=%d active=%d done=%d "
	       "outputs_bound=%d output_enter@bind=%d\n",
	       count, active, p.done_seen, p.output_count,
	       p.output_enter_at_bind);

	/* fix #1: the freshly bound group must carry output_enter for the
	 * client's output(s) within the bind burst (before the first done). */
	if (expect_output_enter >= 0) {
		int got = p.output_enter_at_bind;
		int ok = (expect_output_enter == 0) ? (got >= 1)
						    : (got == expect_output_enter);
		if (!ok) {
			fprintf(stderr, "qdwin-workspace-probe: FAIL expected "
				"%s output_enter at bind, got %d\n",
				expect_output_enter == 0 ? ">=1"
				: "exactly N", got);
			return 1;
		}
	}

	if (expect_count >= 0 && count != expect_count) {
		fprintf(stderr, "qdwin-workspace-probe: FAIL expected %d "
			"workspaces, got %d\n", expect_count, count);
		return 1;
	}

	if (activate >= 0) {
		struct ws_entry *e = entry_by_coord(&p, (uint32_t)activate);
		if (!e) {
			fprintf(stderr, "qdwin-workspace-probe: FAIL no "
				"workspace with index %d\n", activate);
			return 1;
		}
		ext_workspace_handle_v1_activate(e->proxy);
		ext_workspace_manager_v1_commit(p.manager);
		wl_display_roundtrip(p.display);
		wl_display_roundtrip(p.display);
		int now = active_index(&p);
		printf("qdwin-workspace-probe: after activate(%d) active=%d\n",
		       activate, now);
		if (now != activate) {
			fprintf(stderr, "qdwin-workspace-probe: FAIL active is "
				"%d, expected %d\n", now, activate);
			return 1;
		}
	}

	/* fix #3: create_workspace + activate are STAGED on the manager and
	 * only take effect on `commit`, atomically. Prove the two-phase
	 * behaviour: stage both, observe NO change before commit, then commit
	 * and observe BOTH applied together. */
	if (batch_activate >= 0) {
		struct ws_entry *e = entry_by_coord(&p, (uint32_t)batch_activate);
		if (!e) {
			fprintf(stderr, "qdwin-workspace-probe: FAIL no "
				"workspace with index %d for batch\n",
				batch_activate);
			return 1;
		}
		if (batch_activate == active) {
			/* The activate half would be a no-op and could not be
			 * distinguished from "not applied". Refuse rather than
			 * false-pass. */
			fprintf(stderr, "qdwin-workspace-probe: FAIL batch index "
				"%d is already active; pick a different one\n",
				batch_activate);
			return 1;
		}
		if (!p.group) {
			fprintf(stderr, "qdwin-workspace-probe: FAIL no group "
				"to create_workspace on\n");
			return 1;
		}

		int count_before = count;
		int active_before = active;
		int done_before = p.done_seen;

		/* Stage both requests WITHOUT a commit. */
		ext_workspace_group_handle_v1_create_workspace(p.group,
							       "batch-probe");
		ext_workspace_handle_v1_activate(e->proxy);
		/* Two roundtrips give the server ample time to (incorrectly)
		 * act eagerly and broadcast, if it were going to. */
		wl_display_roundtrip(p.display);
		wl_display_roundtrip(p.display);

		int count_staged = live_count(&p);
		int active_staged = active_index(&p);
		printf("qdwin-workspace-probe: pre-commit count=%d active=%d "
		       "(was count=%d active=%d done %d->%d)\n",
		       count_staged, active_staged, count_before, active_before,
		       done_before, p.done_seen);

		if (count_staged != count_before ||
		    active_staged != active_before) {
			fprintf(stderr, "qdwin-workspace-probe: FAIL state "
				"changed BEFORE commit (count %d->%d, active "
				"%d->%d) — requests were applied eagerly, not "
				"staged\n", count_before, count_staged,
				active_before, active_staged);
			return 1;
		}

		/* Now commit: both staged ops must land atomically. */
		ext_workspace_manager_v1_commit(p.manager);
		wl_display_roundtrip(p.display);
		wl_display_roundtrip(p.display);

		int count_after = live_count(&p);
		int active_after = active_index(&p);
		printf("qdwin-workspace-probe: post-commit count=%d active=%d\n",
		       count_after, active_after);

		if (count_after != count_before + 1) {
			fprintf(stderr, "qdwin-workspace-probe: FAIL commit did "
				"not apply create_workspace (count %d, expected "
				"%d)\n", count_after, count_before + 1);
			return 1;
		}
		if (active_after != batch_activate) {
			fprintf(stderr, "qdwin-workspace-probe: FAIL commit did "
				"not apply activate (active %d, expected %d)\n",
				active_after, batch_activate);
			return 1;
		}
	}

	if (wl_display_get_error(p.display) != 0) {
		fprintf(stderr, "qdwin-workspace-probe: FAIL protocol error\n");
		return 1;
	}
	printf("qdwin-workspace-probe: OK\n");
	return 0;
}
