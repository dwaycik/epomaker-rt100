#!/usr/bin/env bash
# Install the background screen updater as a systemd *user* service.
#
# User scope on purpose: starting and stopping it needs no authorisation, so the
# GUI can pause it around other keyboard operations without a polkit prompt and
# without ever touching sudo.
#
# Usage:  ./install-service.sh [SENSOR_KEY]     # default: coretemp-0
#         ./install-service.sh --uninstall
#         ./install-service.sh --list-sensors
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
unit_name="epomaker-controller.service"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
unit="$unit_dir/$unit_name"
python_bin="$here/.venv/bin/python"
script="$here/epomaker_rt100_gtk.py"
controller="$here/.venv/bin/epomakercontroller"

case "${1:-}" in
  --uninstall)
    systemctl --user disable --now "$unit_name" 2>/dev/null || true
    rm -f "$unit"
    systemctl --user daemon-reload
    echo "Removed $unit"
    exit 0
    ;;
  --list-sensors)
    [[ -x "$controller" ]] || { echo "Create the venv first (see README.md)." >&2; exit 1; }
    exec "$controller" list-temp-devices
    ;;
esac

sensor="${1:-coretemp-0}"
[[ -x "$python_bin" ]] || {
  echo "No python at: $python_bin" >&2
  echo "Create the venv first (see README.md)." >&2
  exit 1
}

mkdir -p "$unit_dir"
# The executable path is emitted in double quotes: systemd splits ExecStart on
# whitespace, so an unquoted path containing spaces fails with 203/EXEC.
sed -e "s|@PYTHON@ @SCRIPT@|\"$here/.venv/bin/epomaker-rt100-daemon\"|" \
    -e "s|@SENSOR@|$sensor|" \
  "$here/systemd/$unit_name.in" > "$unit"
chmod 644 "$unit"
systemctl --user daemon-reload

echo "Installed $unit  (sensor: $sensor)"
echo
echo "Start it now:        systemctl --user start $unit_name"
echo "Start at login:      systemctl --user enable $unit_name"
echo "Or use the System info tab in the app, which does both."
echo
echo "Pick a different sensor with:  ./install-service.sh --list-sensors"
