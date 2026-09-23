# Panel: Control Center

What must be visible when this panel is open:

- The panel does NOT use a single title-bar header. Instead it stacks a series of cards according to the user's `controlCenter.cards` config.
- Default-enabled cards, any of which may appear: Profile (user avatar + name), Shortcuts (quick toggles like DarkMode/NightLight/AirplaneMode/Bluetooth), Audio (volume slider), Brightness (brightness slider), Weather (forecast), Media (current track + controls), System Monitor (mini stats).
- A close button is NOT present in the header (panel is dismissed by click-outside or bar button).
- Layout is a column of cards; Media and System Monitor may render side-by-side in one card row.
