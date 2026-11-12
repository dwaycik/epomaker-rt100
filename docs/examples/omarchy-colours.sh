#!/bin/sh
# Colour command for "Follow desktop colours" on Omarchy, which has no portal
# accent colour. Prints the accent (the bar colour if one is set, otherwise
# the theme accent), then the theme accent colour, which "All other keys" uses
# when set to "Desktop colour".
#
# To repaint on every theme change, add an Omarchy hook that runs:
#   epomaker-rt100-daemon --sync-theme
# e.g. ~/.config/omarchy/hooks/theme-set.d/20-keyboard-rgb
theme="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/current/theme/colors.toml"
shell="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.toml"

value() { # value FILE SECTION KEY: first "#RRGGBB" for KEY inside [SECTION] ("" = top level)
  awk -v want="$2" -v key="$3" '
    /^\[/ { section = substr($0, 2, length($0) - 2); next }
    section == want && $1 == key { if (match($0, /#[0-9A-Fa-f]{6}/)) { print substr($0, RSTART, 7); exit } }
  ' "$1" 2>/dev/null
}

accent=$(value "$shell" bar background)
[ -n "$accent" ] || accent=$(value "$theme" "" accent)
echo "$accent $(value "$theme" "" accent)"
