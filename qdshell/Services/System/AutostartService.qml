pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Services.UI

Singleton {
  id: root

  // Public list of autostart entries for UI binding
  property list<var> entries: []

  // Whether to include system (/etc/xdg/autostart) entries
  property bool showSystemEntries: true

  readonly property string userDir: (Quickshell.env("XDG_CONFIG_HOME") || (Quickshell.env("HOME") + "/.config")) + "/autostart"
  readonly property string systemDir: "/etc/xdg/autostart"

  // Read the showSystemAutostart setting, defaulting to true unless it is
  // explicitly set to false (robust against a missing session/key).
  function readShowSystem() {
    const s = Settings.data.session;
    if (s && s.showSystemAutostart !== undefined)
      return s.showSystemAutostart !== false;
    return true;
  }

  // Shell-safe quoting: wraps a string in single quotes, escaping embedded single quotes
  function _q(s) {
    return "'" + String(s).replace(/'/g, "'\\''") + "'";
  }

  // Build an awk program that sets a single key=value strictly within the
  // [Desktop Entry] group: replaces the first occurrence if present, otherwise
  // appends it at the end of the group (never inside later groups). The value
  // is read from the QD_VAL environment variable so awk never reinterprets it.
  function _awkSetKeyInGroup(key) {
    const k = key.replace(/"/g, "\\\"");
    return "BEGIN{ v=ENVIRON[\"QD_VAL\"] } " +
           "function flush(){ if(ingroup && !done){ print \"" + k + "=\" v; done=1 } } " +
           "/^\\[/{ flush(); ingroup=($0==\"[Desktop Entry]\"); print; next } " +
           "{ if(ingroup && index($0, \"" + k + "=\")==1){ if(!done){ print \"" + k + "=\" v; done=1 } next } print } " +
           "END{ flush() }";
  }

  // Build an awk program that deletes a key strictly within [Desktop Entry].
  function _awkDelKeyInGroup(key) {
    const k = key.replace(/"/g, "\\\"");
    return "/^\\[/{ ingroup=($0==\"[Desktop Entry]\"); print; next } " +
           "{ if(ingroup && index($0, \"" + k + "=\")==1){ next } print }";
  }

  // Shell command (string) that sets key=value within [Desktop Entry].
  function _setKeyCmd(filePath, key, value) {
    const fp = _q(filePath);
    return "tmp=\"$(mktemp)\"; QD_VAL=" + _q(value) + " awk " + _q(_awkSetKeyInGroup(key)) +
           " " + fp + " > \"$tmp\" && mv \"$tmp\" " + fp;
  }

  // Refresh the full list by scanning both directories
  function refresh() {
    _pendingEntries = [];
    scanProcess.command = ["sh", "-c", "ls -1 " + _q(userDir) + "/*.desktop 2>/dev/null; echo '---SEPARATOR---'; ls -1 " + _q(systemDir) + "/*.desktop 2>/dev/null"];
    scanProcess.running = true;
  }

  // Enable or disable an entry by toggling the appropriate desktop file key.
  // 'entry' is one of the objects from the entries list.
  function setEnabled(entry, enabled) {
    if (entry.isSystem) {
      // System entry (possibly with an existing user override). The override
      // lives at userDir/<fileName> with Hidden=true.
      const userPath = userDir + "/" + entry.fileName;
      if (!enabled) {
        // Create a minimal user override that hides the system entry.
        // We do not copy the full system file (that would shadow future
        // upstream changes); a small Hidden=true stub is sufficient per spec.
        var stub = "[Desktop Entry]\nType=Application\nHidden=true\n";
        writeProcess.command = ["sh", "-c",
          "mkdir -p " + _q(userDir) + " && cat > " + _q(userPath) + " << 'QDSHELL_EOF'\n" + stub + "QDSHELL_EOF"];
      } else {
        // Re-enable by removing the user override so the system entry applies.
        writeProcess.command = ["sh", "-c", "rm -f " + _q(userPath)];
      }
    } else {
      // Plain user entry: set X-GNOME-Autostart-enabled within [Desktop Entry].
      // When enabling, also drop any Hidden=true that would override it. Both
      // operations stay scoped to the [Desktop Entry] group via awk.
      const filePath = entry.filePath;
      if (enabled) {
        // Remove Hidden within the group, then set the enabled key true.
        const fp = _q(filePath);
        const delHidden = "tmp=\"$(mktemp)\"; awk " + _q(_awkDelKeyInGroup("Hidden")) +
                          " " + fp + " > \"$tmp\" && mv \"$tmp\" " + fp;
        writeProcess.command = ["sh", "-c",
          delHidden + " && " + _setKeyCmd(filePath, "X-GNOME-Autostart-enabled", "true")];
      } else {
        writeProcess.command = ["sh", "-c",
          _setKeyCmd(filePath, "X-GNOME-Autostart-enabled", "false")];
      }
    }
    writeProcess.running = true;
  }

  // Add a new autostart entry. Picks a non-colliding filename.
  function addEntry(name, comment, exec, workingDir) {
    const base = (name.replace(/[^a-zA-Z0-9_-]/g, "_") || "autostart");
    var content = "[Desktop Entry]\n";
    content += "Type=Application\n";
    content += "Name=" + name + "\n";
    if (comment)
      content += "Comment=" + comment + "\n";
    content += "Exec=" + exec + "\n";
    if (workingDir)
      content += "Path=" + workingDir + "\n";
    content += "X-GNOME-Autostart-enabled=true\n";

    // Use a shell loop to find a free filename. We avoid both existing user
    // files and any matching name in the system dir, so a new entry never
    // clobbers a user file nor silently shadows a system autostart entry.
    // The heredoc redirection must be on the cat command itself.
    const dir = _q(userDir);
    const sysDir = _q(systemDir);
    writeProcess.command = ["sh", "-c",
      "mkdir -p " + dir + "; " +
      "base=" + _q(base) + "; n=\"$base\"; i=1; " +
      "while [ -e " + dir + "/\"$n.desktop\" ] || [ -e " + sysDir + "/\"$n.desktop\" ]; do n=\"$base-$i\"; i=$((i+1)); done; " +
      "f=" + dir + "/\"$n.desktop\"; " +
      "cat > \"$f\" << 'QDSHELL_EOF'\n" + content + "QDSHELL_EOF"];
    writeProcess.running = true;
  }

  // Edit an existing user entry. Updates only Name/Comment/Exec/Path within the
  // [Desktop Entry] group, preserving all other keys (Icon, Terminal,
  // OnlyShowIn, enabled state, ...) and not touching later groups. Missing
  // keys are appended at the end of the [Desktop Entry] group. An empty value
  // removes the key (only within [Desktop Entry]).
  function editEntry(filePath, name, comment, exec, workingDir) {
    if (filePath.startsWith(systemDir))
      return; // Cannot edit system entries

    // Values are passed through the environment so awk never reinterprets
    // backslashes or shell metacharacters. Empty/undefined => remove the key.
    const env = {
      "QD_NAME": (name === undefined || name === null) ? "" : String(name),
      "QD_COMMENT": (comment === undefined || comment === null) ? "" : String(comment),
      "QD_EXEC": (exec === undefined || exec === null) ? "" : String(exec),
      "QD_PATH": (workingDir === undefined || workingDir === null) ? "" : String(workingDir)
    };

    // Single awk pass: track whether we are inside [Desktop Entry]. Update or
    // drop the four managed keys inside the group; on leaving the group (or at
    // EOF) emit any managed keys that were not already present and have a value.
    const awkProg =
      "BEGIN{ split(\"Name:QD_NAME Comment:QD_COMMENT Exec:QD_EXEC Path:QD_PATH\", a, \" \"); " +
      "for(j in a){ split(a[j], p, \":\"); keys[j]=p[1]; vals[p[1]]=ENVIRON[p[2]] } } " +
      "function flush(){ for(j=1;j<=4;j++){ kk=keys[j]; if(!seen[kk] && length(vals[kk])>0){ print kk\"=\"vals[kk] } seen[kk]=1 } } " +
      "/^\\[/{ if(ingroup){ flush() } ingroup=($0==\"[Desktop Entry]\"); print; next } " +
      "{ if(ingroup){ for(j=1;j<=4;j++){ kk=keys[j]; if(index($0, kk\"=\")==1){ if(length(vals[kk])>0 && !seen[kk]){ print kk\"=\"vals[kk] } seen[kk]=1; next } } } print } " +
      "END{ if(ingroup){ flush() } }";

    let assigns = "";
    for (var k in env) {
      assigns += k + "=" + _q(env[k]) + " ";
    }

    const fp = _q(filePath);
    const cmd = "tmp=\"$(mktemp)\"; " + assigns + "awk " + _q(awkProg) + " " + fp +
                " > \"$tmp\" && mv \"$tmp\" " + fp;
    writeProcess.command = ["sh", "-c", cmd];
    writeProcess.running = true;
  }

  // Remove a user autostart entry
  function removeEntry(filePath) {
    if (filePath.startsWith(systemDir))
      return; // Cannot remove system entries
    writeProcess.command = ["sh", "-c", "rm -f " + _q(filePath)];
    writeProcess.running = true;
  }

  // --- Internal ---
  property var _pendingEntries: []

  Component.onCompleted: {
    refresh();
  }

  Connections {
    target: Settings
    function onDataChanged() {
      root.showSystemEntries = root.readShowSystem();
    }
  }

  Process {
    id: scanProcess
    property string _stdout: ""

    onStarted: {
      _stdout = "";
    }

    stdout: SplitParser {
      onRead: data => scanProcess._stdout += data + "\n"
    }

    onExited: (exitCode, exitStatus) => {
      root._parseScanOutput(scanProcess._stdout);
    }
  }

  Process {
    id: readProcess
    property string _stdout: ""
    property string _filePath: ""
    property bool _isSystem: false

    onStarted: {
      _stdout = "";
    }

    stdout: SplitParser {
      onRead: data => readProcess._stdout += data + "\n"
    }

    onExited: (exitCode, exitStatus) => {
      root._parseDesktopFile(readProcess._filePath, readProcess._stdout, readProcess._isSystem);
      root._readNextFile();
    }
  }

  Process {
    id: writeProcess

    onExited: (exitCode, exitStatus) => {
      // Refresh entries after any write operation
      root.refresh();
    }
  }

  property var _filesToRead: []

  function _parseScanOutput(output) {
    _filesToRead = [];
    _pendingEntries = [];

    const lines = output.trim().split("\n");
    let inSystem = false;
    let userFiles = [];
    let systemFiles = [];

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (line === "---SEPARATOR---") {
        inSystem = true;
        continue;
      }
      if (line === "" || !line.endsWith(".desktop"))
        continue;

      if (inSystem) {
        systemFiles.push({ "path": line, "isSystem": true });
      } else {
        userFiles.push({ "path": line, "isSystem": false });
      }
    }

    // Read user files first, then system files. This ordering lets system
    // entries detect a pre-existing user override.
    _filesToRead = userFiles.concat(systemFiles);
    _readNextFile();
  }

  function _readNextFile() {
    if (_filesToRead.length === 0) {
      _finalize();
      return;
    }

    const next = _filesToRead.shift();
    readProcess._filePath = next.path;
    readProcess._isSystem = next.isSystem;
    readProcess.command = ["cat", next.path];
    readProcess.running = true;
  }

  function _parseDesktopFile(filePath, content, isSystem) {
    const lines = content.split("\n");
    let inDesktopEntry = false;
    let name = "";
    let comment = "";
    let exec = "";
    let icon = "";
    let hidden = false;
    let autostartEnabled = true;
    let type = "";
    let workingDir = "";

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (line.startsWith("[")) {
        // Only parse keys inside the [Desktop Entry] group.
        inDesktopEntry = (line === "[Desktop Entry]");
        continue;
      }
      if (!inDesktopEntry)
        continue;

      if (line.startsWith("Name=") && !line.startsWith("Name["))
        name = line.substring(5);
      else if (line.startsWith("Comment=") && !line.startsWith("Comment["))
        comment = line.substring(8);
      else if (line.startsWith("Exec="))
        exec = line.substring(5);
      else if (line.startsWith("Icon="))
        icon = line.substring(5);
      else if (line.startsWith("Hidden="))
        hidden = line.substring(7).toLowerCase() === "true";
      else if (line.startsWith("X-GNOME-Autostart-enabled="))
        autostartEnabled = line.substring(26).toLowerCase() !== "false";
      else if (line.startsWith("Type="))
        type = line.substring(5);
      else if (line.startsWith("Path="))
        workingDir = line.substring(5);
    }

    const fileName = filePath.substring(filePath.lastIndexOf("/") + 1);

    if (isSystem) {
      // If a user file with this filename was already read, it shadows this
      // system entry. We only treat it as a managed system override (read-only,
      // toggled by creating/deleting the stub) when it is a minimal hide-stub:
      // Hidden=true with no own Exec. A full user .desktop that merely shares
      // the name stays a normal, editable user entry.
      for (let i = 0; i < _pendingEntries.length; i++) {
        const e = _pendingEntries[i];
        if (e.fileName !== fileName)
          continue;

        if (e._hidden && !e._rawExec) {
          // Managed hide-stub override of this system entry.
          e.isSystem = true;
          e.name = name || fileName.replace(".desktop", "");
          e.comment = comment;
          e.exec = exec;
          // Effective enabled state is whatever the stub declared (disabled).
        }
        // Either way, the system entry itself is shadowed by the user file;
        // do not add a separate system row.
        return;
      }
    }

    // Skip non-application entries (only relevant for fresh entries).
    if (type !== "" && type !== "Application")
      return;

    // Determine effective enabled state
    const enabled = !hidden && autostartEnabled;

    _pendingEntries.push({
      "filePath": filePath,
      "fileName": fileName,
      "isSystem": isSystem,
      "name": name || fileName.replace(".desktop", ""),
      "comment": comment,
      "exec": exec,
      "icon": icon,
      "enabled": enabled,
      "workingDir": workingDir,
      // Internal flags used during the later system pass.
      "_hidden": hidden,
      "_rawExec": exec
    });
  }

  function _finalize() {
    // Sort: user entries first, then system, alphabetical within each group
    let sorted = _pendingEntries.slice();
    sorted.sort(function(a, b) {
      if (a.isSystem !== b.isSystem)
        return a.isSystem ? 1 : -1;
      return a.name.localeCompare(b.name);
    });

    // Filter out system entries if showSystemEntries is false
    if (!showSystemEntries) {
      sorted = sorted.filter(function(e) { return !e.isSystem; });
    }

    entries = sorted;
  }
}
