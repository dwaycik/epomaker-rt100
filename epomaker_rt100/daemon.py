"""Headless screen updater — clock, CPU and temperature.

Run as a systemd user unit rather than upstream's `epomakercontroller
start-daemon`, which opens interface 0 (the one carrying key input, so holding
it interferes with typing) and inherits working-directory-relative config paths.

    python -m epomaker_rt100.daemon [SENSOR_KEY]
    python -m epomaker_rt100.daemon --sync-theme [--force]
    python -m epomaker_rt100.daemon --watch-theme

--sync-theme paints the keys from the desktop colours once and exits; it does
nothing unless "Follow desktop colours" is on (or --force is given).
--watch-theme keeps running and repaints when the desktop accent changes.

The interface comes from the saved settings so the GUI and the service always
agree about which one to hold.
"""

from __future__ import annotations

import sys

from . import core


def main() -> int:
    if "--sync-theme" in sys.argv[1:]:
        code, message = core.sync_theme(force="--force" in sys.argv[1:])
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    if "--watch-theme" in sys.argv[1:]:
        return core.watch_theme()
    sensor = None
    for arg in sys.argv[1:]:
        if not arg.startswith("-"):
            sensor = arg
            break
    settings = core.load_settings()
    interface = int(settings.get("interface", core.DEFAULT_INTERFACE))
    return core.run_daemon(sensor, interface)


if __name__ == "__main__":
    raise SystemExit(main())
