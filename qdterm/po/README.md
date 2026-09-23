# Translations

QTerminator uses standard Python `gettext` for translations.

## For translators

Want to translate QTerminator into your language? Welcome!

1. Generate the latest template:

   ```
   just pot
   ```

   This produces `po/qterminator.pot`.

2. Create or update your language `.po` file:

   ```
   # New language (e.g. German)
   msginit -l de -i po/qterminator.pot -o po/de.po

   # Update existing one
   msgmerge --update po/de.po po/qterminator.pot
   ```

3. Edit `po/de.po` with your translations using any PO editor
   (Poedit, Lokalize, gtranslator, or a text editor).

4. Submit a pull request.

## For developers

To make a string translatable, import `_` from the translation module:

```python
from qterminator.translation import _

label = _("Save Screenshot...")
```

The `_()` function is a no-op if no translations are installed for
the user's locale, so untranslated strings just appear in English.

## Building

```
just pot      # extract strings into qterminator.pot
just mo       # compile all *.po into .mo files
```

The compiled `.mo` files end up at `po/<lang>/LC_MESSAGES/qterminator.mo`
and are picked up automatically by the translation module.

To install system-wide, the `.mo` files belong at
`/usr/share/locale/<lang>/LC_MESSAGES/qterminator.mo`.
