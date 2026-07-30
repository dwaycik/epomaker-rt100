#!/usr/bin/env bash
# Install a desktop entry so the app shows up in your application launcher.
#
# Deliberately user-level only: writes to ~/.local/share/applications and needs
# no root. Any launcher that reads the XDG desktop-entry directories picks it up
# with no further configuration -- wofi, rofi, fuzzel, GNOME, Plasma. There is
# no need to add a keybind: whatever key already opens your launcher will find
# it by name.
#
# Usage:  ./install-desktop.sh            # uses ./.venv/bin/python
#         ./install-desktop.sh /path/to/python
#         ./install-desktop.sh --uninstall
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
dest_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
dest="$dest_dir/epomaker-rt100-gtk.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f "$dest"
  command -v update-desktop-database >/dev/null && \
    update-desktop-database "$dest_dir" 2>/dev/null || true
  echo "Removed $dest"
  exit 0
fi

python_bin="${1:-$here/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  echo "No usable Python at: $python_bin" >&2
  echo "Create the venv first (see README.md), or pass an interpreter path." >&2
  exit 1
fi

script="$here/epomaker_rt100_gtk.py"
[[ -f "$script" ]] || { echo "Cannot find $script" >&2; exit 1; }

mkdir -p "$dest_dir"
# Quote both paths in Exec so directories containing spaces still work.
sed "s|@EXEC@|\"$python_bin\" \"$script\"|" \
  "$here/desktop/epomaker-rt100-gtk.desktop.in" > "$dest"
chmod 644 "$dest"

command -v update-desktop-database >/dev/null && \
  update-desktop-database "$dest_dir" 2>/dev/null || true

echo "Installed $dest"
if command -v desktop-file-validate >/dev/null; then
  desktop-file-validate "$dest" && echo "Desktop entry validates cleanly."
fi
echo "Open your launcher and type \"Epomaker\" or \"RT100\"."
