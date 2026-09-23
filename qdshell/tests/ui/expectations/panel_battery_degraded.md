# Panel: Battery — degraded (no battery, e.g. desktop / VM)

What must be visible when this panel is open on a host with NO battery
(desktop or VM). Per the battery panel contract, when no batteries are
detected the charge card is hidden and only the header remains:

- Header section with a battery icon and the title "Battery".
- A close button (✕) in the top-right.
- NO per-battery charge rows / percentage / progress bar (there is no
  battery to report).
- The panel body is otherwise empty or shows power-profile/performance
  controls only if those settings are enabled — never a fake 0%/100% battery
  row and never a crash.
