# Settings → Region/Location

The harness opens this tab via `settings openTab location`
(`SettingsPanel.Tab.Location`), landing on the default ("Location") sub-tab.
What must be visible:

- A subtab bar with at least: "Location", "Date", "Calendar Panel", and
  "System Clock" (i18n keys `common.location`, `common.date`,
  `common.calendar-panel`, `common.system-clock`), with "Location" selected.
- On the active (Location) sub-tab:
  - A language selector (`panels.general.language-select-label`).
  - A location search / city input (`panels.location.location-search-label`).
  - Weather toggles, at least an enable-weather toggle
    (`panels.location.weather-enabled-label`).
- The standard left-side Settings tab strip is visible.

Notes:
- Only the default ("Location") sub-tab content renders after
  `openTab location`; the harness does not click sub-tabs. The other sub-tabs
  cover:
  - Date: date/time format controls (24-hour toggle, format dropdowns).
  - Calendar Panel: clock/calendar-panel display options.
  - System Clock: the OS date/time/timezone controls described below.

## System Clock sub-tab

Reached by selecting "System Clock" in the subtab bar. It drives the live OS
clock via systemd-timedated (`SystemClockService` → `timedatectl`). Contents:

- A timezone selector / searchable picker
  (`panels.location.system-clock-timezone-label`).
- An automatic time sync / NTP toggle
  (`panels.location.system-clock-ntp-label`), with an informational synced /
  not-synced status line when NTP is on.
- A manual date/time input (`panels.location.system-clock-manual-label`) with an
  apply button; it is disabled/read-only while NTP is on.
- An unavailable note (`panels.location.system-clock-unavailable`) shown when no
  `timedatectl` is present, and an error/permission-denied banner
  (`panels.location.system-clock-error-*`) when a change is rejected.

Notes:
- Timezone keys and the manual date/time string are UNTRUSTED: both are validated
  in the pure `SystemClockService` module (enumerated zone list / strict regex +
  range check) before any `timedatectl` command is built.
