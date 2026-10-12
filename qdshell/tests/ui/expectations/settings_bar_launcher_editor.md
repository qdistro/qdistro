# Settings → Bar → Widgets → Launcher widget settings

After opening the Launcher widget's per-instance settings from the Widgets
subtab, the editor must show:

- A Launcher widget settings dialog or panel with a close affordance.
- A "Select icon color" choice and a "Show launcher panel button" toggle.
- A "Custom launcher items" heading.
- An add-item form with "Name", "Icon" (including a Browse picker and an
  icon preview when an icon is selected), and "Command" fields.
- An "Add" button that is disabled until both Name and Command are non-empty.
- Existing configured items as editable rows with Name, Icon, Command,
  reorder up/down, and remove controls; when the list is empty, an explicit
  empty-state message is shown instead.
- No blank editor body or shell crash.
