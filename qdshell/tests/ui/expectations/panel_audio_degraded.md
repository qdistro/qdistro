# Panel: Audio — degraded (no PipeWire / no audio devices)

What must be visible when this panel is open with no working PipeWire server
or no audio sinks/sources. The panel must degrade gracefully:

- A panel header / title area for audio.
- A close affordance.
- The device/volume area renders a coherent "no devices" / disabled state
  rather than a populated device list. A volume slider may be shown but
  pinned/disabled, or an explanatory empty state is shown.
- No blank panel body and no crash.
