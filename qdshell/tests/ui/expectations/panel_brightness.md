# Panel: Brightness

What must be visible when this panel is open:

- Header section showing a "settings-display" icon and the title "Display" (i18n `panels.display.title`).
- A close button (✕) in the top-right of the header.
- One row per detected monitor showing:
  - Monitor name / identifier.
  - A brightness slider with a percentage readout, led by a single
    brightness icon (the product renders one dynamic icon per row —
    `getIcon(brightness)` picks low/high by value — not two fixed icons at
    the slider ends).
  - On a VM without a controllable backlight, the sliders render disabled
    (shown at 0%/dimmed); no separate explanatory banner is part of the
    design.
