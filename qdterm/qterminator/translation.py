"""Translation support via gettext.

Usage in modules:
    from qterminator.translation import _
    label = _("Save Screenshot...")

When no .mo files are installed, _() acts as the identity function.

To extract translatable strings into a .pot file:
    just pot

To compile .po files into .mo (after translators contribute):
    just mo

Translations are loaded from /usr/share/locale/<lang>/LC_MESSAGES/qterminator.mo
or ~/.local/share/locale/... per the standard gettext lookup.
"""

import gettext as _gettext
import os

DOMAIN = "qterminator"

# Try to install translations from standard locations
try:
    # Look for locale dir relative to the package, then fall back to system
    _here = os.path.dirname(os.path.abspath(__file__))
    _candidates = [
        os.path.join(_here, "..", "po"),
        os.path.join(_here, "..", "locale"),
        "/usr/share/locale",
        "/usr/local/share/locale",
        os.path.expanduser("~/.local/share/locale"),
    ]
    _localedir = None
    for d in _candidates:
        if os.path.isdir(d):
            _localedir = d
            break

    _translation = _gettext.translation(
        DOMAIN, localedir=_localedir, fallback=True
    )
    _ = _translation.gettext
    ngettext = _translation.ngettext
except Exception:
    # Last-resort fallback: identity function
    def _(text):
        return text

    def ngettext(singular, plural, n):
        return singular if n == 1 else plural
