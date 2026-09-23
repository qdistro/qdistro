# Settings → Power

The harness opens this tab via `settings openTab power`
(`SettingsPanel.Tab.Power`). The tab is a single scrolling column of sections;
the topmost sections are what render by default. What must be visible:

- The standard left-side Settings tab strip is visible.
- A "Buttons" section header (`panels.power.section-buttons`) with:
  - A power-button action chooser (`panels.power.power-button-action-label`).
  - A sleep-button action chooser (`panels.power.sleep-button-action-label`).

The remaining sections live further down the same scrolling column. They are part
of the contract for this surface (describe what is actually present, in order):

- An "Inactivity" section (`panels.power.section-inactivity`):
  - On-battery and on-AC inactivity timeout spin boxes in minutes
    (`panels.power.inactivity-battery-label`, `inactivity-ac-label`).
  - An inactivity action chooser (`panels.power.inactivity-action-label`).
  - When the compositor cannot apply idle/DPMS policy, a persist-only info banner
    (`panels.power.idle-persist-only`).
- A "Presentation & Inhibition" section (`panels.power.section-presentation`):
  - A presentation-mode toggle (`panels.power.presentation-mode-label`).
  - A presentation auto-disable timeout spin box in minutes
    (`panels.power.presentation-auto-disable-label`).
  - An inhibit-when-fullscreen toggle (`panels.power.inhibit-fullscreen-label`);
    when enabled it shows a persist-only / unsupported info banner indicating the
    compositor does not yet support it (`panels.power.inhibit-fullscreen-persist-only`).
  - A disable-notifications-while-inhibited toggle
    (`panels.power.disable-notifications-inhibited-label`).
  - A read-only active-inhibitor list/viewer
    (`panels.power.active-inhibitors-label`); when empty it shows a
    "none" placeholder (`panels.power.active-inhibitors-none`), otherwise one row
    per active inhibitor id.
- A "Critical battery" section (`panels.power.section-critical-battery`):
  - A critical battery level spin box in percent (`panels.power.critical-level-label`).
  - A critical battery action chooser (`panels.power.critical-action-label`).
- A "Display" section (`panels.power.section-display`):
  - Display-off-on-battery and display-off-on-AC timeout spin boxes in minutes
    (`panels.power.display-off-battery-label`, `display-off-ac-label`).
  - An auto-reduce-brightness-on-battery toggle
    (`panels.power.auto-reduce-brightness-label`).
  - When auto-reduce is enabled: an AC brightness level and a battery brightness
    level spin box in percent (`panels.power.ac-brightness-level-label`,
    `battery-brightness-level-label`); plus a persist-only info banner when no
    controllable backlight is detected (`panels.power.brightness-persist-only`).

Notes:
- A "Lid" section (`panels.power.section-lid`) with lid-close actions is only
  present on laptops (`PowerService.hasLid`); on a headless/desktop test host it
  is hidden, so the judge must NOT require it.
- The inhibit-when-fullscreen and per-source brightness banners are only visible
  when their respective toggle is enabled and the capability is missing; they are
  not guaranteed in the default state, so describe-but-not-require.
- Inhibitor ids/reasons are untrusted opaque text rendered verbatim as plain
  text; never interpolated into a command.
