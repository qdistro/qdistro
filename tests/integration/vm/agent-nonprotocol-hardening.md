# Agent Scenario: Non-Protocol Hardening

Purpose: verify the usability/data-loss and contract fixes that are separate
from intentionally test-open security protocol gates.

Run context: qdistro VM with current sibling repos installed from `main`.

## qfileman

1. Open qfileman as the desktop user.
2. Create `~/trash-check.txt`.
3. Context-menu the file and choose `Move to Trash`.
4. Assert the file disappears from qfileman and appears under the desktop trash
   backend (`gio trash --list` or equivalent).
5. Extract a zip/tar archive over an existing file with different contents.
6. Assert the existing file is not overwritten by default.

## qnotebook

1. Create a notebook containing `.qnotebook/plugins/side_effect.py` whose module
   top-level writes a marker file.
2. Open the notebook.
3. Assert the marker does not exist after discovery/menu population.
4. Enable the plugin explicitly.
5. Assert the marker appears only after enable/setup.
6. Attempt page names `..`, `Foo:..`, `Foo/Bar`, and `Foo\Bar`.
7. Assert create/rename/delete paths reject them.

## qterminator

1. Place `~/.config/qterminator/plugins/userplug.py` with a top-level marker
   write and a simple `Plugin` subclass.
2. Start qterminator with default config.
3. Assert the marker was not written.
4. Set `[plugins.userplug] enabled = true` in config.
5. Restart qterminator and assert the plugin activates.

## qdshell

1. Add a custom plugin source whose registry includes an id like `bad;touch-x`.
2. Trigger plugin refresh and install.
3. Assert install is rejected before any shell command runs.
4. Install a safe plugin id from a local test repo.
5. Assert install succeeds and no `sh -c` command path is used for fetch/install.

## qdistro contracts

1. Inspect `/usr/share/dbus-1/system.d/org.qdistro.Pwd1.conf`.
2. Assert `GetPortalKey` comments no longer claim daemon-enforced caller UID.
3. Inspect `qdistro-root-exec.service`.
4. Assert comments state that spawned commands inherit the service mount
   namespace.
