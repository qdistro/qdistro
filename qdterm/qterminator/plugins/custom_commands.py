"""Custom commands plugin for QTerminator.

Adds user-defined commands to the context menu.
Commands are stored in config under [plugins.custom_commands].
"""

from qterminator.config import Config
from qterminator.plugin import MenuProvider


class CustomCommandsPlugin(MenuProvider):
    name = "custom_commands"
    description = "Add custom commands to the context menu"
    version = "1.0"
    category = "Plugins"

    def get_menu_items(self, terminal):
        config = Config()
        commands = config.get("plugins", "custom_commands", "commands", default={})
        items = []
        for label, cmd in commands.items():
            items.append((label, lambda t=terminal, c=cmd: t.send_text(c + "\n")))
        return items
