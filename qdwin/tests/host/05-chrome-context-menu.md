# 05 — chrome context menu on right-click

Exercises §6.4: shell-owned context menu opened via
`qdwin_shell_v1.show_popup` and composited on qdwin's popup_layer
above normal windows.

Until the harness has a real pointer injection path, the
right-click is simulated via `qdshell --ctrl-socket`'s
`popup <h>` command, which invokes the same `show_context_menu`
path that the real wl_pointer BTN_RIGHT handler uses. We're
testing the menu's *appearance + teardown*, not the click→menu
wiring.

## Setup

```
ID=05-chrome-context-menu
HT=tests/host

$HT/start.sh $ID
```

## Steps

### S1 — Baseline

```
$HT/screenshot.sh $ID 01-baseline
```

Assert:
- Windowed weston-terminal with cyan qdshell chrome, same shape as
  scenario 01/02 baselines.
- No menu anywhere on the framebuffer.

### S2 — Open popup

```
$HT/ctrl.sh $ID popup 1 32 8
sleep 0.5
$HT/screenshot.sh $ID 02-menu-open
```

Assert:
- A **dark vertical menu rectangle ~200px wide** appears anchored at
  the top-left of the parent's titlebar (a few pixels in from the
  top-left corner of the outer chrome).
- Menu shows at least the labels **"Close"**, **"Minimise"**, and
  **"Toggle maximised"** stacked vertically, visible as light
  text on a dark background.
- The parent terminal window and its cyan chrome are still
  rendered (menu is on top of chrome, chrome on top of content).

### S3 — Close popup

```
$HT/ctrl.sh $ID popup-close 1
sleep 0.5
$HT/screenshot.sh $ID 03-menu-closed
```

Assert:
- The dark menu rectangle is gone.
- The parent terminal window and its chrome are unchanged, i.e.
  this screenshot is visually equivalent to `01-baseline.png`.

## Teardown

```
$HT/stop.sh $ID
```

## Why no dismiss-on-outside-click test

S5 of task 011 (compositor seat-level pointer snoop that fires
`qdwin_popup_v1.dismissed` on click outside the popup) requires
real pointer events. The headless backend doesn't provide them,
and weston-test is not compiled into Tumbleweed's weston.
`popup-close` above exercises the *teardown plumbing* (destroy
the qdwin_popup_v1 object → qdwin popup_teardown → view gone)
without needing a real click.
