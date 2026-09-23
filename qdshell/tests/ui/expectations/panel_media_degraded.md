# Panel: Media — degraded (no MPRIS media player running)

What must be visible when this panel is open with no media player publishing
MPRIS metadata. The panel must degrade gracefully:

- A media panel surface that opens.
- A coherent "nothing playing" / "no media player" empty state — no track
  title, artist, artwork or transport controls bound to a real player.
- No blank panel body and no crash.
