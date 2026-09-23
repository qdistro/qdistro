# Panel: Media Player

What must be visible when this panel is open:

- Header section showing a "music" icon and the title "Media Player" (i18n `common.media-player`).
- A close button (✕) in the top-right of the header.
- A player source selector when multiple media players are active (otherwise just shows the active one).
- Album art (when `showAlbumArt` is enabled and the player provides art).
- Track info: artist + title text (may scroll if long).
- A progress / position slider with elapsed and total time labels.
- Playback control buttons: previous, play/pause, next.
- An audio spectrum visualizer area at the bottom (optional feature).
