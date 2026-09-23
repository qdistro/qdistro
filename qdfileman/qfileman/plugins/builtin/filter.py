"""Filter plugin for QFileMan.

Provides file filtering capabilities.
"""

from qfileman.plugin import FileFilter


class FilterPlugin(FileFilter):
    name = "filter"
    description = "Filter files by extension or pattern"
    version = "1.0"

    def __init__(self):
        super().__init__()
        self._extensions = set()
        self._exclude_extensions = set()

    def filter_files(self, files):
        """Filter files based on extension settings.

        Behaviour:

        - With no include or exclude list configured, all files pass.
        - With an include list, files whose extension is in the list are
          kept. Extension-less files (``README``, ``Makefile``) are also
          kept: an include list expresses "show these types as well as
          unclassified entries", not "hide everything without a dot".
        - With an exclude list, files whose extension is listed are
          dropped; everything else passes.
        """
        if not self._extensions and not self._exclude_extensions:
            return files

        filtered = []
        for f in files:
            ext = f.rsplit('.', 1)[-1].lower() if '.' in f else ''

            if self._extensions:
                if not ext or ext in self._extensions:
                    filtered.append(f)
            elif self._exclude_extensions:
                if ext not in self._exclude_extensions:
                    filtered.append(f)

        return filtered

    def filter_name(self, filename):
        """Filter files by name pattern."""
        return True

    def set_include_extensions(self, extensions):
        """Set extensions to include."""
        self._extensions = set(ext.lower().lstrip('.') for ext in extensions)

    def set_exclude_extensions(self, extensions):
        """Set extensions to exclude."""
        self._exclude_extensions = set(ext.lower().lstrip('.') for ext in extensions)
