---
name: Bug report
about: Create a report to help us improve
title: ''
labels: bug
assignees: ''

---

Before opening an issue, please try starting QTerminator with the
`--no-restore` flag and a fresh config:

```
QTERMINATOR_CONFIG=/dev/null qterminator --no-restore
```

If the bug only occurs with your config, please attach
`~/.config/qterminator/config.toml` to the issue.

**Describe the bug**
A clear and concise description of what the bug is.

**To Reproduce**
Steps to reproduce the behavior:
1.
2.
3.

**Expected behavior**
A clear and concise description of what you expected to happen.

**Screenshots**
If applicable, add screenshots to help explain your problem.

**Environment**
- OS / Linux distribution:
- Distribution version:
- Display server: Wayland / X11
- Python version (`python3 --version`):
- PyQt6 version (`python3 -c 'from PyQt6.QtCore import QT_VERSION_STR; print(QT_VERSION_STR)'`):
- QTermWidget version:
- QTerminator version (`qterminator --version`):

**Loaded plugins**
Output of `ls ~/.config/qterminator/plugins/` if you have custom plugins.

**Additional context**
Add any other context about the problem here.
