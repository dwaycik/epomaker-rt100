"""Headless screen updater — clock, CPU and temperature.

Run as a systemd user unit rather than upstream's `epomakercontroller
start-daemon`, which opens interface 0 (the one carrying key input, so holding
it interferes with typing) and inherits working-directory-relative config paths.

    python -m epomaker_rt100.daemon [SENSOR_KEY]

The interface comes from the saved settings so the GUI and the service always
agree about which one to hold.
"""

from __future__ import annotations

import sys

from . import core


def main() -> int:
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
