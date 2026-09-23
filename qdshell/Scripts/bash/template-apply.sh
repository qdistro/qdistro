#!/usr/bin/env -S bash

# Ensure at least one argument is provided.
if [ "$#" -lt 1 ]; then
    # Print usage information to standard error.
    echo "Error: No application specified." >&2
    echo "Usage: $0 {kitty|ghostty|foot|alacritty|wezterm|fuzzel|walker|pywalfox|cava|yazi|btop|zathura} [dark|light]" >&2
    exit 1
fi

APP_NAME="$1"
MODE="${2:-}" # Optional second argument for dark/light mode

# --- Apply theme based on the application name ---
case "$APP_NAME" in
kitty)
    KITTY_CONF="$HOME/.config/kitty/kitty.conf"
    if [ -w "$KITTY_CONF" ]; then
        kitty +kitten themes --reload-in=all qdshell
    else
        kitty +runpy "from kitty.utils import *; reload_conf_in_all_kitties()"
    fi
    ;;

ghostty)
    CONFIG_FILE="$HOME/.config/ghostty/config"
    # Check if the config file exists before trying to modify it.
    if [ -f "$CONFIG_FILE" ]; then
        # Check if theme is already set to qdshell (flexible spacing)
        if grep -qE "^theme\s*=\s*qdshell$" "$CONFIG_FILE"; then
            : # Already correct
        elif grep -qE "^theme\s*=" "$CONFIG_FILE"; then
            # Replace existing theme line in-place
            sed -i -E 's/^theme\s*=.*/theme = qdshell/' "$CONFIG_FILE"
        else
            # Add the new theme line to the end of the file
            echo "theme = qdshell" >>"$CONFIG_FILE"
        fi
        # Only signal if ghostty is running
        pgrep -f ghostty >/dev/null && pkill -SIGUSR2 ghostty || true
    else
        echo "Error: ghostty config file not found at $CONFIG_FILE" >&2
        exit 1
    fi
    ;;

foot)
    CONFIG_FILE="$HOME/.config/foot/foot.ini"

    # Check if the config file exists, create it if it doesn't.
    if [ ! -f "$CONFIG_FILE" ]; then
        # Create the config directory if it doesn't exist
        mkdir -p "$(dirname "$CONFIG_FILE")"
        # Create the config file with the qdshell theme
        cat >"$CONFIG_FILE" <<'EOF'
[main]
include=~/.config/foot/themes/qdshell
EOF
    else
        # Check if theme is already set to qdshell
        if ! grep -q "include.*qdshell" "$CONFIG_FILE"; then
            # Remove any existing theme include line to prevent duplicates.
            sed -i '/include=.*themes/d' "$CONFIG_FILE"
            if grep -q '^\[main\]' "$CONFIG_FILE"; then
                # Insert the include line after the existing [main] section header
                sed -i '/^\[main\]/a include=~/.config/foot/themes/qdshell' "$CONFIG_FILE"
            else
                # If [main] doesn't exist, create it at the beginning with the include
                sed -i '1i [main]\ninclude=~/.config/foot/themes/qdshell\n' "$CONFIG_FILE"
            fi
        fi
    fi
    ;;

alacritty)
    CONFIG_FILE="$HOME/.config/alacritty/alacritty.toml"
    NEW_THEME_PATH='~/.config/alacritty/themes/qdshell.toml'

    # Check if the config file exists, create it if it doesn't.
    if [ ! -f "$CONFIG_FILE" ]; then
        # Create the config directory if it doesn't exist
        mkdir -p "$(dirname "$CONFIG_FILE")"
        # Create the config file with the qdshell theme import
        cat >"$CONFIG_FILE" <<'EOF'
[general]
import = [
    "~/.config/alacritty/themes/qdshell.toml"
]
EOF
    else
        # Check if qdshell theme is already imported (any path variant)
        if grep -q 'qdshell\.toml' "$CONFIG_FILE"; then
            # Update old relative path to new absolute path if needed
            if grep -q '"themes/qdshell.toml"' "$CONFIG_FILE"; then
                sed -i 's|"themes/qdshell.toml"|"'"$NEW_THEME_PATH"'"|g' "$CONFIG_FILE"
            fi
            # Already has qdshell import with correct path, nothing to do
        else
            # No qdshell import found, add it
            if grep -q '^\[general\]' "$CONFIG_FILE"; then
                # Check if import line already exists under [general]
                if grep -q '^import\s*=' "$CONFIG_FILE"; then
                    # Append to existing import array (before the closing bracket)
                    sed -i '/^import\s*=\s*\[/,/\]/{/\]/s|]|    "'"$NEW_THEME_PATH"'",\n]|}' "$CONFIG_FILE"
                else
                    # Add import line after [general] section header
                    sed -i '/^\[general\]/a import = ["'"$NEW_THEME_PATH"'"]' "$CONFIG_FILE"
                fi
            else
                # Create [general] section with import at the beginning of the file
                sed -i '1i [general]\nimport = ["'"$NEW_THEME_PATH"'"]\n' "$CONFIG_FILE"
            fi
        fi
    fi
    ;;

wezterm)
    CONFIG_FILE="$HOME/.config/wezterm/wezterm.lua"
    WEZTERM_SCHEME_LINE='config.color_scheme = "Qdshell"'

    # Check if the config file exists.
    if [ -f "$CONFIG_FILE" ]; then

        # Check if theme is already set to Qdshell (matches 'Qdshell' or "Qdshell")
        if ! grep -q "^\s*config\.color_scheme\s*=\s*['\"]Qdshell['\"]\s*" "$CONFIG_FILE"; then
            # Not set to Qdshell. Check if *any* color_scheme line exists.
            if grep -q '^\s*config\.color_scheme\s*=' "$CONFIG_FILE"; then
                # It exists, so we replace it with our desired line.
                sed -i "s|^\(\s*config\.color_scheme\s*=\s*\).*$|\1\"Qdshell\"|" "$CONFIG_FILE"
            else
                # It doesn't exist, so we add it before the 'return config' line.
                if grep -q '^\s*return\s*config' "$CONFIG_FILE"; then
                    # 'return config' exists. Insert the line before it.
                    sed -i '/^\s*return\s*config/i\'"$WEZTERM_SCHEME_LINE" "$CONFIG_FILE"
                else
                    # This is a problem. We can't find the insertion point.
                    echo "Warning: 'config.color_scheme' not set and 'return config' line not found." >&2
                    echo "         Make sure $CONFIG_FILE is correct: https://wezterm.org/config/files.html" >&2
                fi
            fi
        fi
        # touching the config file fools wezterm into reloading it
        touch "$CONFIG_FILE"
    else
        echo "Error: wezterm.lua not found at $CONFIG_FILE" >&2
        echo "Instructions to create it: https://wezterm.org/config/files.html" >&2
        exit 1
    fi
    ;;

fuzzel)
    CONFIG_FILE="$HOME/.config/fuzzel/fuzzel.ini"

    # Check if the config file exists, create it if it doesn't.
    if [ ! -f "$CONFIG_FILE" ]; then
        # Create the config directory if it doesn't exist
        mkdir -p "$(dirname "$CONFIG_FILE")"
        # Create the config file with the qdshell theme
        cat >"$CONFIG_FILE" <<'EOF'
include=~/.config/fuzzel/themes/qdshell
EOF
    else
        # Check if theme is already set to qdshell
        if grep -q "^include=~/.config/fuzzel/themes/qdshell$" "$CONFIG_FILE"; then
            : # Already correct
        elif grep -q "^include=.*themes" "$CONFIG_FILE"; then
            # Replace existing theme include line in-place
            sed -i 's|^include=.*themes.*|include=~/.config/fuzzel/themes/qdshell|' "$CONFIG_FILE"
        else
            # Add the new theme include line
            echo "include=~/.config/fuzzel/themes/qdshell" >>"$CONFIG_FILE"
        fi
    fi
    ;;

walker)
    CONFIG_FILE="$HOME/.config/walker/config.toml"

    # Check if the config file exists.
    if [ -f "$CONFIG_FILE" ]; then
        # Check if theme is already set to qdshell (flexible spacing)
        if grep -qE '^theme\s*=\s*"qdshell"' "$CONFIG_FILE"; then
            : # Already correct
        elif grep -qE '^theme\s*=' "$CONFIG_FILE"; then
            # Replace existing theme line in-place
            sed -i -E 's/^theme\s*=.*/theme = "qdshell"/' "$CONFIG_FILE"
        else
            echo 'theme = "qdshell"' >>"$CONFIG_FILE"
        fi
    else
        echo "Error: walker config file not found at $CONFIG_FILE" >&2
        exit 1
    fi
    ;;

vicinae)
    # Apply the theme
    vicinae theme set qdshell
    ;;

pywalfox)
    # Set dark/light mode first if MODE is specified
    if [ -n "$MODE" ]; then
        if [ "$MODE" = "dark" ] || [ "$MODE" = "light" ]; then
            pywalfox "$MODE"
        else
            echo "Warning: Invalid mode '$MODE'. Expected 'dark' or 'light'. Skipping mode switch." >&2
        fi
    fi
    # Update the theme
    pywalfox update
    ;;

cava)
    CONFIG_FILE="$HOME/.config/cava/config"
    THEME_MODIFIED=false

    # Check if the config file exists.
    if [ -f "$CONFIG_FILE" ]; then
        # Check if [color] section exists
        if grep -q '^\[color\]' "$CONFIG_FILE"; then
            # Check if theme is already set to qdshell under [color] (flexible spacing)
            if sed -n '/^\[color\]/,/^\[/p' "$CONFIG_FILE" | grep -qE '^theme\s*=\s*"qdshell"'; then
                : # Already correct
            elif sed -n '/^\[color\]/,/^\[/p' "$CONFIG_FILE" | grep -qE '^theme\s*='; then
                # Replace existing theme line under [color]
                sed -i -E '/^\[color\]/,/^\[/{s/^theme\s*=.*/theme = "qdshell"/}' "$CONFIG_FILE"
                THEME_MODIFIED=true
            else
                # Add theme line after [color]
                sed -i '/^\[color\]/a theme = "qdshell"' "$CONFIG_FILE"
                THEME_MODIFIED=true
            fi
        else
            # Add [color] section with theme at the end of file
            echo "" >>"$CONFIG_FILE"
            echo "[color]" >>"$CONFIG_FILE"
            echo 'theme = "qdshell"' >>"$CONFIG_FILE"
            THEME_MODIFIED=true
        fi

        # Reload cava if it's running, but only if it's not using stdin config
        if pgrep -f cava >/dev/null; then
            # Check if Cava is running with -p /dev/stdin (managed by CavaService)
            if ! pgrep -af cava | grep -q -- "-p.*stdin"; then
                pkill -USR1 cava
            fi
        fi
    else
        echo "Error: cava config file not found at $CONFIG_FILE" >&2
        exit 1
    fi
    ;;

yazi)
    CONFIG_FILE="$HOME/.config/yazi/theme.toml"

    # Create config directory if it doesn't exist
    mkdir -p "$(dirname "$CONFIG_FILE")"

    if [ ! -f "$CONFIG_FILE" ]; then
        cat >"$CONFIG_FILE" <<'EOF'
[flavor]
dark  = "qdshell"
light = "qdshell"
EOF
    else
        # Check if [flavor] section exists
        if grep -q '^\[flavor\]' "$CONFIG_FILE"; then
            # Update or add dark/light lines under [flavor]
            if sed -n '/^\[flavor\]/,/^\[/p' "$CONFIG_FILE" | grep -q '^dark\s*='; then
                sed -i '/^\[flavor\]/,/^\[/{s/^dark\s*=.*/dark  = "qdshell"/}' "$CONFIG_FILE"
            else
                sed -i '/^\[flavor\]/a dark  = "qdshell"' "$CONFIG_FILE"
            fi
            if sed -n '/^\[flavor\]/,/^\[/p' "$CONFIG_FILE" | grep -q '^light\s*='; then
                sed -i '/^\[flavor\]/,/^\[/{s/^light\s*=.*/light = "qdshell"/}' "$CONFIG_FILE"
            else
                sed -i '/^\[flavor\]/,/^dark/a light = "qdshell"' "$CONFIG_FILE"
            fi
        else
            # Add [flavor] section at the end
            echo "" >>"$CONFIG_FILE"
            echo "[flavor]" >>"$CONFIG_FILE"
            echo 'dark  = "qdshell"' >>"$CONFIG_FILE"
            echo 'light = "qdshell"' >>"$CONFIG_FILE"
        fi
    fi
    ;;

btop)
    CONFIG_FILE="$HOME/.config/btop/btop.conf"

    if [ -f "$CONFIG_FILE" ]; then
        # Check if theme is already set to qdshell (flexible spacing)
        if grep -qE '^color_theme\s*=\s*"qdshell"' "$CONFIG_FILE"; then
            : # Already correct
        elif grep -qE '^color_theme\s*=' "$CONFIG_FILE"; then
            # Replace existing color_theme line in-place
            sed -i -E 's/^color_theme\s*=.*/color_theme = "qdshell"/' "$CONFIG_FILE"
        else
            echo 'color_theme = "qdshell"' >>"$CONFIG_FILE"
        fi

        if pgrep -x btop >/dev/null; then
            pkill -SIGUSR2 -x btop
        fi
    else
        echo "Warning: btop config file not found at $CONFIG_FILE" >&2
    fi
    ;;

zathura)
    ZATHURA_INSTANCES=$(dbus-send --session \
        --dest=org.freedesktop.DBus \
        --type=method_call \
        --print-reply \
        /org/freedesktop/DBus \
        org.freedesktop.DBus.ListNames |
        grep -o 'org.pwmt.zathura.PID-[0-9]*')

    for id in $ZATHURA_INSTANCES; do
        dbus-send --session \
            --dest="$id" \
            --type=method_call \
            /org/pwmt/zathura \
            org.pwmt.zathura.ExecuteCommand \
            string:"source"
    done
    ;;

*)
    # Handle unknown application names.
    echo "Error: Unknown application '$APP_NAME'." >&2
    exit 1
    ;;
esac
