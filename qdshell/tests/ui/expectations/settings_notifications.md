# Settings → Notifications

What must be visible when this tab is open:

- Tab header text: "Notifications" (i18n key `common.notifications`).
- Subtab bar with at least: "Appearance", "Duration", "History", "Sound", "Toast".
- A position selector for notification toasts.
- A "Detail mode" selector (combo box) showing its current value (default
  "Normal (title and body)"; the label may be elided), controlling how much of
  each toast is shown. Its option list (Compact / Normal / Detailed) is pinned
  by the source-level control contract test: the capture only opens the Toast
  theme chooser, so a screenshot cannot reveal this closed combo's options.
- A "Minimum width" spin box (px, with " px" suffix) setting the minimum
  toast width. The 200–1200 range is pinned by the source-level control
  contract test because a screenshot cannot reveal a closed spin box's limits.
- A "Toast theme" selector (combo box) with options Default / Compact / Rounded / Minimal / Accent bar, choosing the visual style (corner shape, padding, icon, border, accent strip) applied to notification toasts.
- Per-monitor enable toggles.
- The standard left-side Settings tab strip is visible.

Notes:
- The Detail mode, Toast theme, and Minimum width controls live on the "Appearance" subtab alongside the existing Density selector.
