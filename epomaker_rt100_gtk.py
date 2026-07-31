#!/usr/bin/env python3
"""GTK4 / libadwaita control panel for the Epomaker RT100.

A thin GUI wrapper around the EpomakerController library
(https://github.com/strodgers/epomaker-controller). USB-wired only.

Everything this app knows about the hardware comes from that library's source:
key indices from ``configs/keymaps/EpomakerRT100.json``, light modes from
``Profile.Mode``, and the screen size from ``IMAGE_DIMENSIONS``. The only thing
defined locally is the *geometry* of a US ANSI board, because the library ships
a UK ISO layout only.

No telemetry, no network access, and never invokes sudo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.github.dwaycik.EpomakerRT100"

# EPOMAKER_RT100_CONFIG_DIR redirects the settings file. Its reason for existing
# is test isolation: without it, running the validation harness scribbles over
# the real user's saved preferences.
SETTINGS_PATH = (
    Path(os.environ.get("EPOMAKER_RT100_CONFIG_DIR")
         or Path(GLib.get_user_config_dir()) / "epomaker-rt100-gtk")
    / "settings.json"
)

# How often to re-check whether the keyboard is present. Cheap: a libusb
# enumeration, no device is opened. Skipped entirely while a transfer is running.
DEVICE_POLL_SECONDS = 4

# Interface 2 is the only one safe to hold.
#
# The libusb backend detaches the kernel HID driver from whatever interface it
# opens, and on the RT100 the input collections are not spread evenly:
#
#   0 -- main keyboard. Holding it interferes with typing.
#   1 -- Consumer Control (the volume knob), System Control, a second keyboard
#        collection and a mouse collection. Holding it kills the volume knob
#        and media keys for as long as the handle is open. Measured 2026-07-31:
#        6 input nodes drop to 1, and return the moment the handle closes.
#   2 -- no input collections at all, and it accepts every command this app
#        sends: lighting, profiles, screen images, clock, CPU and temperature.
#
# OpenRGB reaches the same conclusion independently -- its Epomaker detector
# registers this VID/PID on interface 2.
DEFAULT_INTERFACE = 2

# --------------------------------------------------------------------------- #
# Library imports, deferred so a missing dependency becomes a UI message
# rather than a traceback on stderr.
# --------------------------------------------------------------------------- #

def _stabilise_library_paths() -> Path:
    """Work around upstream 0.0.9's working-directory-relative paths.

    epomakercontroller/configs/constants.py hard-codes three relative paths:

      PATH_TO_DEFAULT_CONFIG = "src/epomakercontroller/configs/default.json"
      CONFIG_DIRECTORY       = ".epomaker-controller"
      TMP_FOLDER             = os.path.abspath("./.epomaker_controller")

    The first only resolves inside an upstream source checkout, so
    load_main_config() raises FileNotFoundError anywhere else -- including a
    systemd service and any app-menu launch, where the working directory is the
    home directory. The second makes the config per-working-directory. The third
    is worse: constants.py runs os.mkdir on it at *import time*, littering
    whichever directory the process happened to start in.

    So: move to a runtime directory of our own before importing, then repoint
    the constants at the files as actually installed.
    """
    runtime = Path(GLib.get_user_data_dir()) / "epomaker-rt100-gtk" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    os.chdir(runtime)
    return runtime


RUNTIME_DIR = _stabilise_library_paths()

IMPORT_ERROR: str | None = None
try:
    import hid  # provided by the `hidapi` package, a dependency of the library

    from epomakercontroller.commands import (
        EpomakerGifCommand,
        EpomakerImageCommand,
        EpomakerKeyRGBCommand,
    )
    from epomakercontroller.commands.data.constants import (
        BUFF_LENGTH,
        IMAGE_DIMENSIONS,
        Profile,
    )
    from epomakercontroller.configs.configs import load_main_config
    from epomakercontroller.epomakercontroller import EpomakerController
    from epomakercontroller.utils.keyboard_keys import KeyboardKey, KeyboardKeys

    # Repoint the relative constants at the real installed locations. Both the
    # constants module and configs.py need patching, because configs.py does
    # `from .constants import PATH_TO_DEFAULT_CONFIG`, binding its own name.
    import epomakercontroller.configs.configs as _epo_configs
    import epomakercontroller.configs.constants as _epo_constants

    _installed_default = (
        Path(_epo_configs.__file__).parent / "default.json"
    )
    if _installed_default.exists():
        for _module in (_epo_constants, _epo_configs):
            if hasattr(_module, "PATH_TO_DEFAULT_CONFIG"):
                _module.PATH_TO_DEFAULT_CONFIG = str(_installed_default)
    _config_home = str(Path.home() / ".epomaker-controller")
    for _module in (_epo_constants, _epo_configs):
        if hasattr(_module, "CONFIG_DIRECTORY"):
            _module.CONFIG_DIRECTORY = _config_home

    def _best_gif_dimensions(source_width: int, source_height: int) -> tuple[int, int]:
        """Accept any already-legal frame size instead of re-flooring it.

        Upstream floors both axes to multiples of 64. That is stricter than the
        firmware's actual requirement -- the frame buffer is 4K page-aligned, so
        the rule is `w * h * 2 % 4096 == 0` -- and it caps a square source at
        128x128, 58.5% of the 162x173 panel. It also floors the short axis of a
        wide source to zero, which passes its own `% 4096` check because
        0 % 4096 == 0, and uploads an empty frame.

        Frames are pre-rendered to GIF_DIMENSIONS before they get here, so the
        common path is a pass-through. Anything else falls back to upstream's
        algorithm with the zero case clamped.
        """
        panel_w, panel_h = IMAGE_DIMENSIONS
        if (source_width <= panel_w and source_height <= panel_h
                and (source_width * source_height * 2) % 4096 == 0
                and source_width > 0 and source_height > 0):
            return source_width, source_height
        import math as _math

        ratio = min(panel_w / source_width, panel_h / source_height)
        width = _math.ceil(source_width * ratio)
        height = _math.ceil(source_height * ratio)
        return (max(64, _math.floor(width / 64) * 64),
                max(64, _math.floor(height / 64) * 64))

    EpomakerGifCommand.EpomakerGifCommand.best_gif_dimensions = staticmethod(
        _best_gif_dimensions
    )
except Exception as exc:  # pragma: no cover - environment problem, not logic
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

UDEV_FIX = """A permission error came back from the keyboard.

The hidapi libusb backend needs write access to the USB device node, which the
/dev/hidraw* ACLs do not cover. Install the udev rule (note: the filename must
sort before 73-seat-late.rules, or the uaccess tag is ignored):

  sudo install -m644 udev/70-epomaker-rt100.rules /etc/udev/rules.d/
  sudo udevadm control --reload-rules && sudo udevadm trigger

Then unplug and replug the keyboard. This app will not escalate privileges
itself."""


# --------------------------------------------------------------------------- #
# US ANSI geometry
#
# name -> (x, y, width, height) in keycap units, origin top-left.
#
# Key *names* and their LED indices are read from the library's RT100 keymap at
# runtime -- only the physical arrangement lives here. Two ISO-only keys are
# absent from ANSI hardware: HASH (the key left of an ISO Enter) and the ISO
# key between Left Shift and Z. See BACKSLASH_CANDIDATES below.
# --------------------------------------------------------------------------- #

ANSI_LAYOUT: dict[str, tuple[float, float, float, float]] = {
    # Function row
    "ESC": (0, 0, 1, 1),
    "F1": (2, 0, 1, 1), "F2": (3, 0, 1, 1), "F3": (4, 0, 1, 1), "F4": (5, 0, 1, 1),
    "F5": (6.5, 0, 1, 1), "F6": (7.5, 0, 1, 1), "F7": (8.5, 0, 1, 1), "F8": (9.5, 0, 1, 1),
    "F9": (11, 0, 1, 1), "F10": (12, 0, 1, 1), "F11": (13, 0, 1, 1), "F12": (14, 0, 1, 1),
    "DEL": (15.5, 0, 1, 1), "PGUP": (16.5, 0, 1, 1), "PGDOWN": (17.5, 0, 1, 1),
    "DIAL": (18.5, 0, 1, 1),
    # Number row
    "BACKQUOTE": (0, 1.5, 1, 1),
    "NUMROW_1": (1, 1.5, 1, 1), "NUMROW_2": (2, 1.5, 1, 1), "NUMROW_3": (3, 1.5, 1, 1),
    "NUMROW_4": (4, 1.5, 1, 1), "NUMROW_5": (5, 1.5, 1, 1), "NUMROW_6": (6, 1.5, 1, 1),
    "NUMROW_7": (7, 1.5, 1, 1), "NUMROW_8": (8, 1.5, 1, 1), "NUMROW_9": (9, 1.5, 1, 1),
    "NUMROW_0": (10, 1.5, 1, 1),
    "NUMROW_MINUS": (11, 1.5, 1, 1), "NUMROW_EQUAL": (12, 1.5, 1, 1),
    "BACKSPACE": (13, 1.5, 2, 1),
    "NUMLOCK": (15.5, 1.5, 1, 1), "NUMPAD_SLASH": (16.5, 1.5, 1, 1),
    "NUMPAD_ASTERISK": (17.5, 1.5, 1, 1), "NUMPAD_MINUS": (18.5, 1.5, 1, 1),
    # QWERTY row -- ANSI puts backslash here, at the end
    "TAB": (0, 2.5, 1.5, 1),
    "Q": (1.5, 2.5, 1, 1), "W": (2.5, 2.5, 1, 1), "E": (3.5, 2.5, 1, 1),
    "R": (4.5, 2.5, 1, 1), "T": (5.5, 2.5, 1, 1), "Y": (6.5, 2.5, 1, 1),
    "U": (7.5, 2.5, 1, 1), "I": (8.5, 2.5, 1, 1), "O": (9.5, 2.5, 1, 1),
    "P": (10.5, 2.5, 1, 1),
    "OPEN_SQBR": (11.5, 2.5, 1, 1), "CLOSE_SQBR": (12.5, 2.5, 1, 1),
    "BACKSLASH": (13.5, 2.5, 1.5, 1),
    "NUMPAD_7": (15.5, 2.5, 1, 1), "NUMPAD_8": (16.5, 2.5, 1, 1),
    "NUMPAD_9": (17.5, 2.5, 1, 1), "NUMPAD_PLUS": (18.5, 2.5, 1, 2),
    # Home row -- ANSI Enter is a single wide bar
    "CAPS": (0, 3.5, 1.75, 1),
    "A": (1.75, 3.5, 1, 1), "S": (2.75, 3.5, 1, 1), "D": (3.75, 3.5, 1, 1),
    "F": (4.75, 3.5, 1, 1), "G": (5.75, 3.5, 1, 1), "H": (6.75, 3.5, 1, 1),
    "J": (7.75, 3.5, 1, 1), "K": (8.75, 3.5, 1, 1), "L": (9.75, 3.5, 1, 1),
    "SEMICOLON": (10.75, 3.5, 1, 1), "QUOTE": (11.75, 3.5, 1, 1),
    "ENTER": (12.75, 3.5, 2.25, 1),
    "NUMPAD_4": (15.5, 3.5, 1, 1), "NUMPAD_5": (16.5, 3.5, 1, 1),
    "NUMPAD_6": (17.5, 3.5, 1, 1),
    # Shift row -- ANSI Left Shift runs straight into Z
    "LEFT_SHIFT": (0, 4.5, 2.25, 1),
    "Z": (2.25, 4.5, 1, 1), "X": (3.25, 4.5, 1, 1), "C": (4.25, 4.5, 1, 1),
    "V": (5.25, 4.5, 1, 1), "B": (6.25, 4.5, 1, 1), "N": (7.25, 4.5, 1, 1),
    "M": (8.25, 4.5, 1, 1),
    "COMMA": (9.25, 4.5, 1, 1), "DOT": (10.25, 4.5, 1, 1), "SLASH": (11.25, 4.5, 1, 1),
    "RIGHT_SHIFT": (12.25, 4.5, 1.75, 1),
    "UP": (14, 4.5, 1, 1),
    "NUMPAD_1": (15.5, 4.5, 1, 1), "NUMPAD_2": (16.5, 4.5, 1, 1),
    "NUMPAD_3": (17.5, 4.5, 1, 1), "NUMPAD_ENTER": (18.5, 4.5, 1, 2),
    # Bottom row.
    # The leftmost/rightmost Ctrl names follow the library's own ISO layout
    # file, which lists RIGHT_CTRL first and LEFT_CTRL last. That looks like an
    # upstream mix-up, but it is the only record of the mapping and the LED
    # indices are not geometric (index 11 is the DIAL, which sits top-right),
    # so it cannot be derived. Both caps read "Ctrl"; if the wrong one lights,
    # swap these two names.
    "RIGHT_CTRL": (0, 5.5, 1.25, 1),
    "LEFT_WIN": (1.25, 5.5, 1.25, 1),
    "LEFT_ALT": (2.5, 5.5, 1.25, 1),
    "SPACE": (3.75, 5.5, 5.25, 1),
    "RIGHT_ALT": (9, 5.5, 1, 1), "FN": (10, 5.5, 1, 1), "LEFT_CTRL": (11, 5.5, 1, 1),
    "LEFT": (12, 5.5, 1, 1), "DOWN": (13, 5.5, 1, 1), "RIGHT": (14, 5.5, 1, 1),
    "NUMPAD_0": (15.5, 5.5, 2, 1), "NUMPAD_DOT": (17.5, 5.5, 1, 1),
}

LAYOUT_UNITS_W = 19.5
LAYOUT_UNITS_H = 6.5
UNIT_PX = 34

# Nicer captions than the library's display_str for a few keys. Plain words
# rather than symbols like U+232B/U+21B5, which live in fallback fonts and are
# not guaranteed to be installed. The arrow keys keep the library's ← ↑ ↓ →.
LABEL_OVERRIDES = {
    "SPACE": "Space", "BACKSPACE": "Bksp", "CAPS": "Caps", "DIAL": "Dial",
    "LEFT_WIN": "Super", "NUMLOCK": "Num", "PGDOWN": "PgDn",
    "NUMPAD_ENTER": "Enter", "ENTER": "Enter",
}

# Confirmed on hardware 2026-07-31: the US ANSI backslash is LED index 75,
# which the library's UK ISO keymap names "HASH".
#
# That is not recorded anywhere in the library, which ships an ISO map only. On
# ISO, index 10 ("BACKSLASH") is the key between Left Shift and Z and index 75
# is the key left of Enter; an ANSI board has neither switch, and puts backslash
# at the end of the QWERTY row. Index 75 turns out to be the matrix position
# those two share.
#
# The candidate list stays so the same test works on another board -- the first
# entry is the default, and the UI lights each in turn.
BACKSLASH_CANDIDATES = ["HASH", "ENTER", "BACKSLASH"]

# Pacing. The upstream library sends packets back-to-back. The RT100 firmware
# erases SRAM on the init report and needs a pause before data arrives, and the
# endpoint buffer can overflow when reports arrive too quickly -- historically
# corrupting the high-index (right-hand) keys. 8 packets for a key frame makes
# 10 ms pacing free; the 1002-packet image upload keeps upstream's timing so as
# not to change a path that is known to work.
ERASE_DELAY_S = 0.25
KEY_PACKET_DELAY_S = 0.010
IMAGE_PACKET_DELAY_S = 0.0


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class DeviceMissing(Exception):
    """No wired RT100 on the USB bus."""


class DevicePermission(Exception):
    """Found the keyboard but could not open it."""


class DeviceBusy(Exception):
    """Something else is holding the interface."""


# --------------------------------------------------------------------------- #
# Device layer
# --------------------------------------------------------------------------- #


class RT100(EpomakerController if not IMPORT_ERROR else object):  # type: ignore[misc]
    """EpomakerController with an explicit HID interface choice.

    v0.0.8 has no interface argument: ``_find_device_path`` matches
    DEVICE_DESCRIPTION_REGEX ("ROYUAN .* System Control") against
    /sys/class/input/*/device/name, filters on Wired/Wireless and takes the
    first hit. That happens to land on interface 1 on the boards tested, but it
    is implicit and unselectable, so the lookup is replaced here with a direct
    ``hid.enumerate()`` filter on ``interface_number``.

    Signal handling is also disabled. The base class installs SIGINT/SIGTERM
    handlers that call ``os._exit(0)``, which both fights GTK and restricts
    construction to the main thread. This class closes the device in a finally
    block instead.
    """

    def __init__(self, config, interface: int = DEFAULT_INTERFACE) -> None:
        self._interface = interface
        self._pid: int | None = None
        super().__init__(config, dry_run=False)

    def _setup_signal_handling(self) -> None:  # noqa: D102 - deliberate no-op
        pass

    def _find_product_id(self) -> int | None:
        self._pid = super()._find_product_id()
        return self._pid

    def _open_device(self, product_id: int) -> None:
        """Open a specific interface, and raise instead of swallowing failures.

        0.0.9 opens with ``hid.device().open(vendor_id, product_id)``, which
        takes whatever interface hidapi enumerates first -- interface 0, the one
        that carries key input and interferes with typing. There is no way to
        ask for another. So the path-based open lives here instead, filtering
        hid.enumerate() on interface_number.

        Upstream also logs the IOError and sets self.device = None rather than
        raising, so a permission problem would surface later as a confusing
        AssertionError. Typed errors are raised here instead.
        """
        vendor_id = self.config.vendor_id
        path = None
        for entry in hid.enumerate(vendor_id, product_id):
            if entry.get("interface_number") == self._interface:
                path = entry["path"]
                break
        if path is None:
            self.device = None
            raise DeviceMissing(
                f"Interface {self._interface} is not present on the keyboard."
            )

        self.device = hid.device()
        try:
            self.device.open_path(path)
        except (IOError, OSError) as exc:
            self.device = None
            raise DevicePermission(str(exc)) from exc

    @property
    def keymap_config(self):
        """The keymap Config, across the 0.0.8 / 0.0.9 split.

        0.0.8 put config_layout/config_keymap directly on the controller. 0.0.9
        moved them behind an EpomakerConfig wrapper at ``self.config``.
        """
        wrapper = getattr(self, "config", None)
        keymap = getattr(wrapper, "config_keymap", None)
        if keymap is None:
            keymap = getattr(self, "config_keymap", None)
        if keymap is None:
            raise RuntimeError(
                "This build of EpomakerController exposes neither "
                "controller.config.config_keymap nor controller.config_keymap."
            )
        return keymap

    def send_gif_paced(self, gif_path: str) -> None:
        """Upload an animated GIF using the library's native GIF command.

        New in upstream 0.0.9 (not on PyPI): EpomakerGifCommand implements the
        multi-frame protocol, sniffed from the vendor software -- init report
        0xa5 carrying frame count, frame delay and per-frame size, then 1001
        reports per frame. Up to 56 frames at 15 fps.
        """
        command = EpomakerGifCommand.EpomakerGifCommand(gif_path)
        if not command.encode_gif():
            raise RuntimeError("The library could not encode that GIF.")
        self._send_command(command)

    def send_paced(
        self,
        command,
        erase_delay: float,
        packet_delay: float,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Send a prepared command, pacing packets and honouring the SRAM erase.

        Mirrors ``EpomakerController._send_command`` but adds delays and a
        progress callback, which the library's monolithic senders do not offer.
        """
        if self.device is None:
            raise DeviceBusy("Device is not open")
        packets = list(command)
        total = len(packets)
        for index, packet in enumerate(packets):
            if len(packet) != BUFF_LENGTH:
                raise RuntimeError(f"Packet {index} is {len(packet)}, expected {BUFF_LENGTH}")
            self.device.send_feature_report(packet.get_all_bytes())
            if index == 0 and erase_delay:
                time.sleep(erase_delay)
            elif packet_delay:
                time.sleep(packet_delay)
            if progress is not None:
                progress(index + 1, total)


def scan_bus() -> dict[str, object]:
    """Report what RT100 hardware is on the bus, without opening anything."""
    wired_ids = [0x4010, 0x4015]
    wireless_ids = [0x4011, 0x4016]
    if not hasattr(hid, "enumerate"):  # pragma: no cover
        return {"wired": [], "wireless": [], "interfaces": []}
    wired: list[dict] = []
    wireless: list[dict] = []
    for pid in wired_ids:
        wired.extend(hid.enumerate(0x3151, pid))
    for pid in wireless_ids:
        wireless.extend(hid.enumerate(0x3151, pid))
    interfaces = sorted({e["interface_number"] for e in wired if e["interface_number"] >= 0})
    return {"wired": wired, "wireless": wireless, "interfaces": interfaces}


# --------------------------------------------------------------------------- #
# The CPU/temp daemon holds the device, so an upload fails while it runs.
# Same approach as the repo's service/epomaker-upload-image helper: check
# is-active, stop, run, start again in a finally block. User units are tried
# first because stopping those needs no authorisation at all.
# --------------------------------------------------------------------------- #


class UserUnit:
    """Talk to a systemd --user unit.

    User scope only, deliberately: stopping and starting a user unit needs no
    authorisation, so the app never has to escalate. A system unit would put a
    polkit prompt in the middle of an upload.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["systemctl", "--user", *args],
                              capture_output=True, text=True, timeout=30)

    def exists(self) -> bool:
        """Ask systemd, rather than guessing where the unit file lives.

        A unit installed by the package lands in /usr/lib/systemd/user/, while
        install-service.sh writes ~/.config/systemd/user/. Checking only the
        latter made the service controls grey out on every packaged install.
        """
        try:
            result = self._run("list-unit-files", "--no-legend", self.name)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and self.name in result.stdout

    def is_active(self) -> bool:
        try:
            return self._run("is-active", "--quiet", self.name).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def is_enabled(self) -> bool:
        try:
            return self._run("is-enabled", "--quiet", self.name).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def start(self) -> str | None:
        result = self._run("start", self.name)
        return None if result.returncode == 0 else result.stderr.strip()

    def stop(self) -> str | None:
        result = self._run("stop", self.name)
        return None if result.returncode == 0 else result.stderr.strip()

    def set_enabled(self, enabled: bool) -> str | None:
        result = self._run("enable" if enabled else "disable", self.name)
        return None if result.returncode == 0 else result.stderr.strip()


def list_temp_sensors() -> list[tuple[str, str]]:
    """Return (key, human label) for each temperature sensor.

    Keys match what the library's get_device_temp() expects -- "<chip>-<index>",
    the same scheme as its own _get_temp_devices().
    """
    try:
        import psutil
    except ImportError:
        return []
    out: list[tuple[str, str]] = []
    try:
        readings = psutil.sensors_temperatures()
    except Exception:
        return []
    for chip, entries in readings.items():
        for index, entry in enumerate(entries):
            key = f"{chip}-{index}"
            label = entry.label or chip
            out.append((key, f"{label} ({key}) — {entry.current:.0f}°C"))
    return out


def read_sensor(key: str) -> float | None:
    try:
        import psutil
        chip, _, index = key.rpartition("-")
        entries = psutil.sensors_temperatures().get(chip, [])
        return entries[int(index)].current
    except Exception:
        return None


@dataclass
class DaemonGuard:
    unit: str = field(
        default_factory=lambda: os.environ.get(
            "EPOMAKER_SERVICE_NAME", "epomaker-controller.service"
        )
    )
    scope: list[str] | None = None
    was_active: bool = False

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", *(self.scope or []), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def stop(self) -> str | None:
        """Stop the daemon if running. Returns a note for the UI, or None."""
        for scope in (["--user"], []):
            self.scope = scope
            try:
                if self._run("is-active", "--quiet", self.unit).returncode == 0:
                    break
            except (OSError, subprocess.SubprocessError):
                self.scope = None
                return None
        else:
            self.scope = None
            return None

        self.was_active = True
        result = self._run("stop", self.unit)
        if result.returncode != 0:
            self.was_active = False
            raise DeviceBusy(
                f"{self.unit} is running and holding the keyboard, but stopping it "
                f"failed:\n\n{result.stderr.strip() or 'unknown error'}\n\n"
                "Stop it yourself and try again."
            )
        where = "user" if self.scope == ["--user"] else "system"
        return f"Paused {self.unit} ({where})."

    def restart(self) -> str | None:
        if not self.was_active:
            return None
        result = self._run("start", self.unit)
        self.was_active = False
        if result.returncode != 0:
            return f"Could not restart {self.unit}: {result.stderr.strip()}"
        return f"Resumed {self.unit}."


# --------------------------------------------------------------------------- #
# Image fitting. GdkPixbuf is already present via GTK, so no extra dependency
# is pulled in for this.
# --------------------------------------------------------------------------- #

FIT_MODES = [
    ("letterbox", "Show the whole image", "Adds bars so nothing is cut off"),
    ("crop", "Fill the screen", "Crops the edges to fill it completely"),
    ("stretch", "Stretch to fit", "Uses every pixel, distorts the shape"),
]


PICKER_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tif", "*.tiff",
                   "*.webp", "*.gif")


def load_frames(path: str) -> list[GdkPixbuf.Pixbuf]:
    """Return every frame of `path`, or a single-item list for a still.

    GIFs are accepted here even though the library's SUPPORTED_FORMATS excludes
    them, because a GIF frame is a perfectly good still once extracted -- the
    file never reaches the library, only the PNG rendered from the chosen frame.
    This does NOT make the screen animate: no public implementation of the
    RT100's animation protocol exists, and uploading frames in sequence is not a
    substitute (each upload is 1002 packets and freezes the keyboard).

    Pillow gives exact frame access and is used when present. It is not a hard
    requirement -- without it, GdkPixbuf still yields the representative first
    frame, so a GIF remains usable.
    """
    try:
        from PIL import Image
    except ImportError:
        return [GdkPixbuf.Pixbuf.new_from_file(path)]

    try:
        with Image.open(path) as image:
            count = getattr(image, "n_frames", 1)
            if count <= 1:
                return [GdkPixbuf.Pixbuf.new_from_file(path)]
            frames = []
            for number in range(min(count, 512)):
                image.seek(number)
                rgb = image.convert("RGB")
                data = GLib.Bytes.new(rgb.tobytes())
                frames.append(GdkPixbuf.Pixbuf.new_from_bytes(
                    data, GdkPixbuf.Colorspace.RGB, False, 8,
                    rgb.width, rgb.height, rgb.width * 3,
                ))
            return frames
    except Exception:
        # A frame walk that fails should not make the file unusable.
        return [GdkPixbuf.Pixbuf.new_from_file(path)]


# Animated uploads go at 128x128, not the still image's 162x173.
#
# The library derives GIF dimensions with best_gif_dimensions(): it scales the
# source to fit 162x173, then floors each axis to a multiple of 64, because the
# firmware's animation framebuffer is 4K page-aligned and a non-aligned frame
# size produces vertical line artifacts. Within 162x173 the only multiple of 64
# available on each axis is 128, so 128x128 is the largest legal frame.
#
# It also has a bug: a wide source floors the short axis to zero
# (800x200 -> (128, 0)), which passes its own `% 4096` check and uploads
# nothing usable. Pre-rendering every frame to exactly 128x128 here avoids that
# entirely -- best_gif_dimensions(128, 128) returns (128, 128) -- and applies
# the user's chosen fitting instead of an unconditional squash.
def _gif_size() -> tuple[int, int]:
    """Frame size for animated uploads.

    The firmware places an animation frame at roughly 1:1 rather than scaling it
    to the panel, so a small frame simply occupies less of the screen. The real
    constraint is only that the frame be 4K-aligned -- `w * h * 2 % 4096 == 0`
    -- because the animation framebuffer is page-aligned and unaligned sizes
    produce vertical line artifacts.

    Upstream satisfies that by flooring both axes to multiples of 64, which is
    stricter than the rule requires and caps a square source at 128x128: only
    58.5% of the 162x173 panel. 128x160 is the largest legal size that fits
    (40960 bytes = 10 pages exactly) and covers 73.1%, and its portrait shape is
    closer to the panel's than a square.

    Override with EPOMAKER_GIF_SIZE=WxH to try another; the value is validated
    against the alignment rule and the panel size before use.
    """
    default = (128, 160)
    raw = os.environ.get("EPOMAKER_GIF_SIZE")
    if not raw:
        return default
    try:
        width, height = (int(part) for part in raw.lower().split("x"))
    except ValueError:
        return default
    if (width * height * 2) % 4096 or not (0 < width <= 162 and 0 < height <= 173):
        print(f"Ignoring EPOMAKER_GIF_SIZE={raw}: must fit 162x173 and satisfy "
              "w*h*2 % 4096 == 0.", file=sys.stderr)
        return default
    return (width, height)


GIF_DIMENSIONS = _gif_size()
GIF_MAX_FRAMES = 56  # the library subsamples above this
GIF_FRAMERATE = 15

try:
    import PIL  # noqa: F401
    HAVE_PILLOW = True
except ImportError:
    # The library's own EpomakerGifCommand imports PIL, so without it animation
    # is unavailable either way -- but stills must keep working.
    HAVE_PILLOW = False


def render_gif(
    frames: list[GdkPixbuf.Pixbuf], mode: str, bg: Gdk.RGBA, out_path: str
) -> int:
    """Write `frames` out as a GIF the library can upload. Returns frame count."""
    from PIL import Image

    width, height = GIF_DIMENSIONS
    step = max(1, len(frames) / GIF_MAX_FRAMES)
    picked = [frames[min(int(i * step), len(frames) - 1)]
              for i in range(min(len(frames), GIF_MAX_FRAMES))]

    images = []
    for frame in picked:
        fitted = fit_pixbuf(frame, mode, bg, size=GIF_DIMENSIONS)
        images.append(Image.frombytes(
            "RGB", (width, height), bytes(fitted.get_pixels()), "raw", "RGB",
            fitted.get_rowstride(), 1,
        ))

    images[0].save(
        out_path, save_all=True, append_images=images[1:],
        duration=int(1000 / GIF_FRAMERATE), loop=0, optimize=False,
    )
    return len(images)


def fit_pixbuf(
    source: GdkPixbuf.Pixbuf,
    mode: str,
    bg: Gdk.RGBA,
    size: tuple[int, int] | None = None,
) -> GdkPixbuf.Pixbuf:
    """Render `source` into an IMAGE_DIMENSIONS canvas, or `size` if given.

    IMAGE_DIMENSIONS comes straight from the library
    (commands/data/constants.py) and is a cv2.resize dsize, so it reads
    (width, height). The library's own encode_image does a bare resize with no
    aspect handling, which is why the fitting happens here instead.
    """
    width, height = size or IMAGE_DIMENSIONS
    if source.get_has_alpha():
        flat = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8,
                                    source.get_width(), source.get_height())
        flat.fill(_rgba_to_pixel(bg))
        source.composite(flat, 0, 0, source.get_width(), source.get_height(),
                         0, 0, 1, 1, GdkPixbuf.InterpType.NEAREST, 255)
        source = flat

    canvas = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, False, 8, width, height)
    canvas.fill(_rgba_to_pixel(bg))

    src_w, src_h = source.get_width(), source.get_height()
    if mode == "stretch":
        scaled = source.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
        scaled.copy_area(0, 0, width, height, canvas, 0, 0)
        return canvas

    scale = max(width / src_w, height / src_h) if mode == "crop" \
        else min(width / src_w, height / src_h)
    new_w = max(1, round(src_w * scale))
    new_h = max(1, round(src_h * scale))
    scaled = source.scale_simple(new_w, new_h, GdkPixbuf.InterpType.BILINEAR)

    if mode == "crop":
        src_x = max(0, (new_w - width) // 2)
        src_y = max(0, (new_h - height) // 2)
        scaled.copy_area(src_x, src_y, min(width, new_w), min(height, new_h),
                         canvas, 0, 0)
    else:
        scaled.copy_area(0, 0, new_w, new_h, canvas,
                         (width - new_w) // 2, (height - new_h) // 2)
    return canvas


def _rgba_to_pixel(rgba: Gdk.RGBA) -> int:
    return ((int(rgba.red * 255) << 24) | (int(rgba.green * 255) << 16)
            | (int(rgba.blue * 255) << 8) | 0xFF)


# --------------------------------------------------------------------------- #
# Desktop integration: pick up the live Hyprland accent so the window does not
# look like a stray GNOME app. libadwaita deliberately ignores gtk-theme-name,
# so the accent has to be injected as CSS.
# --------------------------------------------------------------------------- #

FALLBACK_ACCENT = ("#9b30ff", "#c63dff")  # cyberpunk.conf $purple


def read_desktop_accent() -> tuple[str, str]:
    """Read accent + bright from ~/.config/hypr/accent.lua, if present."""
    import re

    path = Path(GLib.get_user_config_dir()) / "hypr" / "accent.lua"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_ACCENT

    def grab(field: str) -> str | None:
        match = re.search(rf'{field}\s*=\s*"rgba\(([0-9a-fA-F]{{6}})[0-9a-fA-F]{{2}}\)"', text)
        return f"#{match.group(1)}" if match else None

    return grab("accent") or FALLBACK_ACCENT[0], grab("bright") or FALLBACK_ACCENT[1]


BASE_CSS = """
:root {{
  --accent-bg-color: {accent};
  --accent-color: {bright};
}}
@define-color accent_bg_color {accent};
@define-color accent_color {bright};
@define-color accent_fg_color #ffffff;

.keycap {{
  font-size: 0.72rem;
  padding: 0;
  min-width: 0;
  min-height: 0;
  border-radius: 5px;
}}
.keycap.picked {{
  outline: 2px solid {bright};
  outline-offset: -2px;
}}
.screen-preview {{
  border: 1px solid alpha(currentColor, 0.25);
  border-radius: 6px;
  background-color: #000;
}}
"""


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # a settings file we cannot write is not worth interrupting for


# --------------------------------------------------------------------------- #
# Window
# --------------------------------------------------------------------------- #


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Epomaker RT100")
        self.set_default_size(760, 720)

        self.settings = load_settings()
        self.interface = int(self.settings.get("interface", DEFAULT_INTERFACE))
        if self.interface == 1 and not self.settings.get("iface1_migrated"):
            # 1 was this app's default before the volume-knob problem was
            # found, so a stored 1 is almost certainly ours, not a choice.
            self.interface = DEFAULT_INTERFACE
            self.settings["iface1_migrated"] = True
        self.backslash_index_name = self.settings.get(
            "backslash_index_name", BACKSLASH_CANDIDATES[0]
        )
        self.key_colours: dict[str, str] = dict(self.settings.get("key_colours", {}))
        self.picked: set[str] = set()
        self.busy = False
        self.device_ready = False
        self.unavailable_reason = ""
        self.unit = UserUnit(
            os.environ.get("EPOMAKER_SERVICE_NAME", "epomaker-controller.service")
        )
        self.keyboard_keys = None
        self.image_path: str | None = None
        self.frames: list[GdkPixbuf.Pixbuf] = []
        self.frame_index = 0
        self._last_sent: tuple[str, object] | None = None
        self.fit_mode = self.settings.get("fit_mode", "letterbox")

        self.keycap_css = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), self.keycap_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
        )

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        self.iface_drop = Gtk.DropDown.new_from_strings(
            ["Interface 0", "Interface 1", "Interface 2"]
        )
        self.iface_drop.set_selected(self.interface)
        self.iface_drop.set_tooltip_text(
            "The RT100 exposes three HID interfaces. Interface 0 carries key input and "
            "using it interferes with typing; 1 is lighting and 2 is the screen. "
            "Interface 1 is the default and works for both jobs here."
        )
        self.iface_drop.connect("notify::selected", self.on_interface_changed)
        header.pack_start(self.iface_drop)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Re-scan USB")
        refresh.connect("clicked", lambda *_: self.refresh_device())
        header.pack_end(refresh)

        self.banner = Adw.Banner(revealed=False)
        toolbar.add_top_bar(self.banner)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(self.stack)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        if IMPORT_ERROR:
            self.stack.add_titled(self._dependency_page(), "deps", "Setup")
            return

        self.stack.add_titled_with_icon(
            self._backlight_page(), "backlight", "Backlight", "display-brightness-symbolic"
        )
        self.stack.add_titled_with_icon(
            self._screen_page(), "screen", "Screen", "image-x-generic-symbolic"
        )
        self.stack.add_titled_with_icon(
            self._system_page(), "system", "System info", "utilities-system-monitor-symbolic"
        )
        self.refresh_device()
        GLib.timeout_add_seconds(DEVICE_POLL_SECONDS, self.poll_device)

    # ---------------------------------------------------------------- pages --

    def _dependency_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            icon_name="dialog-warning-symbolic",
            title="EpomakerController is not importable",
            description=(
                f"{IMPORT_ERROR}\n\n"
                "On Arch/CachyOS the reliable route is the distro hidapi binding, "
                "because the version the library pins (hidapi==0.14.0) does not build "
                "on current Python:\n\n"
                "  sudo pacman -S --needed python-hidapi\n"
                "  python -m venv --system-site-packages .venv\n"
                "  .venv/bin/pip install --no-deps EpomakerController\n"
                "  .venv/bin/pip install appdirs click gpustat 'numpy<2.0' \\\n"
                "      opencv-python-headless psutil python-dateutil"
            ),
        )
        return page

    def _backlight_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        # --- solid colour ---
        group = Adw.PreferencesGroup(
            title="One colour for every key",
            description="Sets a static colour across the whole board.",
        )
        row = Adw.ActionRow(title="Colour")
        self.solid_colour = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
        self.solid_colour.set_rgba(_rgba("#9b30ff"))
        self.solid_colour.set_valign(Gtk.Align.CENTER)
        row.add_suffix(self.solid_colour)
        apply_solid = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        apply_solid.add_css_class("suggested-action")
        apply_solid.connect("clicked", self.on_apply_solid)
        row.add_suffix(apply_solid)
        group.add(row)
        page.add(group)

        # --- built-in modes ---
        group = Adw.PreferencesGroup(
            title="Built-in light modes",
            description=(
                "The 19 effects in the keyboard's own firmware. Some modes ignore the "
                "colour and use their own palette."
            ),
        )
        self.mode_names = [m.name.replace("_", " ").title() for m in Profile.Mode]
        self.modes = list(Profile.Mode)
        self.mode_row = Adw.ComboRow(
            title="Effect", model=Gtk.StringList.new(self.mode_names)
        )
        group.add(self.mode_row)

        self.speed_row = Adw.SpinRow.new_with_range(
            Profile.Speed.MIN.value, Profile.Speed.MAX.value, 1
        )
        self.speed_row.set_title("Speed")
        self.speed_row.set_value(Profile.Speed.DEFAULT.value)
        group.add(self.speed_row)

        self.bright_row = Adw.SpinRow.new_with_range(
            Profile.Brightness.MIN.value, Profile.Brightness.MAX.value, 1
        )
        self.bright_row.set_title("Brightness")
        # Measured on an RT100, 2026-07-30: anything below 3 leaves the LEDs
        # dark. The firmware range is 0-4 and the full range is kept, because 0
        # is the only way to switch the backlight off outright -- but the low
        # steps are not a gradient, so say so rather than let them look broken.
        self.bright_row.set_subtitle(
            "Only 3 and 4 actually light the keys — 0, 1 and 2 leave them off "
            "(use 0 to turn the backlight off)"
        )
        self.bright_row.set_value(Profile.Brightness.DEFAULT.value)
        group.add(self.bright_row)

        self.dazzle_row = Adw.SwitchRow(
            title="Dazzle", subtitle="The firmware's extra-colour variant of the effect"
        )
        group.add(self.dazzle_row)

        self.direction_names = [
            ("Default", Profile.Option.DEFAULT), ("Reverse / inward", Profile.Option.ON),
            ("Left", Profile.Option.DRIFT_LEFT), ("Down", Profile.Option.DRIFT_DOWN),
            ("Up", Profile.Option.DRIFT_UP),
        ]
        self.direction_row = Adw.ComboRow(
            title="Direction",
            subtitle="Only some effects react to this",
            model=Gtk.StringList.new([n for n, _ in self.direction_names]),
        )
        group.add(self.direction_row)

        row = Adw.ActionRow(title="Effect colour")
        self.mode_colour = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
        self.mode_colour.set_rgba(_rgba("#b4b4b4"))
        self.mode_colour.set_valign(Gtk.Align.CENTER)
        row.add_suffix(self.mode_colour)
        apply_mode = Gtk.Button(label="Apply", valign=Gtk.Align.CENTER)
        apply_mode.add_css_class("suggested-action")
        apply_mode.connect("clicked", self.on_apply_mode)
        row.add_suffix(apply_mode)
        group.add(row)

        cycle_row = Adw.ActionRow(
            title="Step through the effects",
            subtitle="Moves to the next effect in the list so you can see each one",
        )
        step = Gtk.Button(label="Next effect", valign=Gtk.Align.CENTER)
        step.connect("clicked", self.on_step_mode)
        cycle_row.add_suffix(step)
        group.add(cycle_row)
        page.add(group)

        # --- per-key ---
        group = Adw.PreferencesGroup(
            title="Individual keys",
            description=(
                "Click keys to select them, then pick a colour. Nothing reaches the "
                "keyboard until you press Send. Layout shown is US ANSI."
            ),
        )
        self.key_fixed = Gtk.Fixed(
            halign=Gtk.Align.CENTER,
            margin_top=6, margin_bottom=6, margin_start=6, margin_end=6,
        )
        self.key_fixed.set_size_request(
            int(LAYOUT_UNITS_W * UNIT_PX), int(LAYOUT_UNITS_H * UNIT_PX)
        )
        scroller = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        scroller.set_child(self.key_fixed)
        group.add(scroller)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                           margin_top=6, halign=Gtk.Align.CENTER)
        self.key_colour = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
        self.key_colour.set_rgba(_rgba("#00fff9"))
        controls.append(self.key_colour)
        paint = Gtk.Button(label="Colour selected")
        paint.connect("clicked", self.on_paint_selected)
        controls.append(paint)
        clear = Gtk.Button(label="Unselect all")
        clear.connect("clicked", lambda *_: self.set_picked(set()))
        controls.append(clear)
        wipe = Gtk.Button(label="Reset to black")
        wipe.connect("clicked", self.on_wipe_keys)
        controls.append(wipe)
        send = Gtk.Button(label="Send to keyboard")
        send.add_css_class("suggested-action")
        send.connect("clicked", self.on_send_keys)
        controls.append(send)
        group.add(controls)

        hint = Gtk.Label(
            label=(
                "If the colours do not show up, set the effect above to "
                "“Always On” first — some firmware effects override per-key colour."
            ),
            wrap=True, xalign=0, margin_top=6,
        )
        hint.add_css_class("dim-label")
        hint.add_css_class("caption")
        group.add(hint)

        # backslash calibration
        self.backslash_row = Adw.ActionRow(
            title="Backslash key position",
            subtitle=(
                "Confirmed as LED index 75 on a US ANSI board. If yours differs, "
                "light each candidate and keep the one that matches."
            ),
        )
        test = Gtk.Button(label="Test next candidate", valign=Gtk.Align.CENTER)
        test.connect("clicked", self.on_test_backslash)
        self.backslash_row.add_suffix(test)
        group.add(self.backslash_row)
        page.add(group)

        self.build_keycaps()
        return page

    def _screen_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        width, height = IMAGE_DIMENSIONS
        group = Adw.PreferencesGroup(
            title="Still image on the screen",
            description=(
                f"The screen is {width}×{height} pixels, read from the library's "
                "IMAGE_DIMENSIONS. Your picture is fitted to that before upload."
            ),
        )

        self.file_row = Adw.ActionRow(title="Image", subtitle="Nothing chosen yet")
        browse = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
        browse.connect("clicked", self.on_browse)
        self.file_row.add_suffix(browse)
        group.add(self.file_row)

        self.animate_row = Adw.SwitchRow(
            title="Play the animation",
            subtitle=(
                f"Sends every frame at {GIF_FRAMERATE} fps. Turn off to send one "
                "still frame instead."
            ),
            active=True,
            visible=False,
        )
        self.animate_row.connect("notify::active", self.on_animate_changed)
        group.add(self.animate_row)

        self.frame_row = Adw.SpinRow.new_with_range(1, 1, 1)
        self.frame_row.set_title("Frame")
        self.frame_row.set_subtitle("Which single frame to send")
        self.frame_row.set_visible(False)
        self.frame_row.connect("notify::value", self.on_frame_changed)
        group.add(self.frame_row)

        self.fit_row = Adw.ComboRow(
            title="Fitting",
            model=Gtk.StringList.new([label for _, label, _ in FIT_MODES]),
        )
        keys = [key for key, _, _ in FIT_MODES]
        if self.fit_mode in keys:
            self.fit_row.set_selected(keys.index(self.fit_mode))
        self.fit_row.set_subtitle(FIT_MODES[self.fit_row.get_selected()][2])
        self.fit_row.connect("notify::selected", self.on_fit_changed)
        group.add(self.fit_row)

        row = Adw.ActionRow(title="Bar colour", subtitle="Fills any space around the image")
        self.bar_colour = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
        self.bar_colour.set_rgba(_rgba("#000000"))
        self.bar_colour.set_valign(Gtk.Align.CENTER)
        self.bar_colour.connect("notify::rgba", lambda *_: self.update_preview())
        row.add_suffix(self.bar_colour)
        group.add(row)

        self.restore_row = Adw.SwitchRow(
            title="Keep this picture after backlight changes",
            subtitle=(
                "Writing key colours clears the screen on this hardware, so the "
                "picture is sent again automatically. Adds a few seconds to each "
                "backlight change."
            ),
            active=bool(self.settings.get("restore_screen", True)),
        )
        self.restore_row.connect("notify::active", lambda *_: self.persist())
        group.add(self.restore_row)
        page.add(group)

        group = Adw.PreferencesGroup(title="Preview", description="Actual size.")
        self.preview = Gtk.Picture(
            content_fit=Gtk.ContentFit.CONTAIN, halign=Gtk.Align.CENTER,
            margin_top=6, margin_bottom=6,
        )
        self.preview.set_size_request(width, height)
        self.preview.add_css_class("screen-preview")
        group.add(self.preview)

        self.upload_progress = Gtk.ProgressBar(
            show_text=True, text="Idle", margin_top=6, visible=False
        )
        group.add(self.upload_progress)

        self.upload_button = Gtk.Button(
            label="Upload to keyboard", halign=Gtk.Align.CENTER, margin_top=6,
            sensitive=False,
        )
        self.upload_button.add_css_class("suggested-action")
        self.upload_button.add_css_class("pill")
        self.upload_button.connect("clicked", self.on_upload)
        group.add(self.upload_button)

        # Sits directly under the button, so a disabled button always explains
        # itself where the user is actually looking.
        self.upload_reason = Gtk.Label(
            wrap=True, justify=Gtk.Justification.CENTER, halign=Gtk.Align.CENTER,
            margin_top=4, visible=False,
        )
        self.upload_reason.add_css_class("warning")
        self.upload_reason.add_css_class("caption")
        group.add(self.upload_reason)

        note = Gtk.Label(
            label=(
                "Upload sends 1002 packets and takes a few seconds. The keyboard is "
                "unresponsive while it runs — do not unplug it."
            ),
            wrap=True, xalign=0, margin_top=6,
        )
        note.add_css_class("dim-label")
        note.add_css_class("caption")
        group.add(note)
        page.add(group)
        return page

    def _system_page(self) -> Gtk.Widget:
        """Clock, CPU and temperature on the keyboard's screen."""
        page = Adw.PreferencesPage()

        group = Adw.PreferencesGroup(
            title="Clock",
            description=(
                "The keyboard has no battery-backed clock — it shows whatever the "
                "host last sent, so it drifts and resets when unplugged."
            ),
        )
        row = Adw.ActionRow(
            title="Set the keyboard's time and date", subtitle="Sends your system clock"
        )
        sync = Gtk.Button(label="Sync now", valign=Gtk.Align.CENTER)
        sync.add_css_class("suggested-action")
        sync.connect("clicked", self.on_sync_time)
        row.add_suffix(sync)
        group.add(row)
        page.add(group)

        group = Adw.PreferencesGroup(
            title="CPU and temperature",
            description=(
                "The screen's temperature field is meant for weather, but the "
                "library repurposes it for a sensor on this machine."
            ),
        )

        self.sensors = list_temp_sensors()
        keys = [key for key, _ in self.sensors]
        self.sensor_row = Adw.ComboRow(
            title="Temperature sensor",
            model=Gtk.StringList.new([label for _, label in self.sensors]
                                     or ["No sensors found"]),
        )
        # coretemp-0 is the CPU package sensor -- the one worth watching.
        preferred = self.settings.get("temp_sensor", "coretemp-0")
        if preferred in keys:
            self.sensor_row.set_selected(keys.index(preferred))
        self.sensor_row.connect("notify::selected", self.on_sensor_changed)
        group.add(self.sensor_row)

        self.live_row = Adw.ActionRow(
            title="Right now", subtitle="—",
            icon_name="utilities-system-monitor-symbolic",
        )
        push = Gtk.Button(label="Send once", valign=Gtk.Align.CENTER)
        push.connect("clicked", self.on_send_stats)
        self.live_row.add_suffix(push)
        group.add(self.live_row)
        page.add(group)

        group = Adw.PreferencesGroup(
            title="Keep it updating",
            description=(
                "Runs a small background service that refreshes the screen "
                "continuously. It holds the keyboard, so this app stops it "
                "automatically while making any other change, then starts it again."
            ),
        )
        self.service_row = Adw.SwitchRow(
            title="Update the screen continuously",
            subtitle="Checking…",
        )
        self.service_row.connect("notify::active", self.on_service_toggled)
        group.add(self.service_row)

        self.autostart_row = Adw.SwitchRow(
            title="Start automatically when I log in", subtitle="",
        )
        self.autostart_row.connect("notify::active", self.on_autostart_toggled)
        group.add(self.autostart_row)
        page.add(group)

        self.refresh_service_state()
        GLib.timeout_add_seconds(2, self._tick_live)
        return page

    # ------------------------------------------------------- system info --

    def _tick_live(self) -> bool:
        if not hasattr(self, "live_row"):
            return True
        try:
            import psutil
            cpu = int(psutil.cpu_percent())
        except Exception:
            cpu = 0
        key = self.current_sensor()
        temp = read_sensor(key) if key else None
        self.live_row.set_subtitle(
            f"CPU {cpu}%   ·   {temp:.0f}°C" if temp is not None else f"CPU {cpu}%"
        )
        return True

    def current_sensor(self) -> str | None:
        if not self.sensors:
            return None
        index = min(self.sensor_row.get_selected(), len(self.sensors) - 1)
        return self.sensors[index][0]

    def on_sensor_changed(self, *_args) -> None:
        self.settings["temp_sensor"] = self.current_sensor()
        self.persist()
        self._tick_live()
        if self.unit.is_active():
            self.toast("Restart the background updater to use the new sensor.")

    def on_sync_time(self, *_args) -> None:
        self.run_on_device("Clock sync", lambda dev: (dev.send_time(),
                                                      "Keyboard clock set.")[1])

    def on_send_stats(self, *_args) -> None:
        key = self.current_sensor()

        def work(dev: RT100) -> str:
            import psutil
            cpu = int(psutil.cpu_percent())
            dev.send_cpu(cpu)
            temp = read_sensor(key) if key else None
            if temp is not None:
                dev.send_temperature(int(temp))
                return f"Sent CPU {cpu}% and {temp:.0f}°C."
            return f"Sent CPU {cpu}%."

        self.run_on_device("System stats", work)

    def refresh_service_state(self) -> None:
        installed = self.unit.exists()
        active = installed and self.unit.is_active()
        self.service_row.set_sensitive(installed)
        self.autostart_row.set_sensitive(installed)
        self._suppress_service_signal = True
        self.service_row.set_active(active)
        self.autostart_row.set_active(installed and self.unit.is_enabled())
        self._suppress_service_signal = False
        self.service_row.set_subtitle(
            ("Running — the screen is being refreshed." if active
             else "Stopped.") if installed else
            "Not installed yet. Run ./install-service.sh in the repo."
        )

    def on_service_toggled(self, *_args) -> None:
        if getattr(self, "_suppress_service_signal", False):
            return
        want = self.service_row.get_active()
        error = self.unit.start() if want else self.unit.stop()
        if error:
            self.show_error(f"Could not {'start' if want else 'stop'} the "
                            f"background updater.\n\n{error}")
        GLib.timeout_add(600, lambda: (self.refresh_service_state(), False)[1])

    def on_autostart_toggled(self, *_args) -> None:
        if getattr(self, "_suppress_service_signal", False):
            return
        error = self.unit.set_enabled(self.autostart_row.get_active())
        if error:
            self.show_error(f"Could not change autostart.\n\n{error}")

    # ------------------------------------------------------------- keycaps --

    def build_keycaps(self) -> None:
        self.keycap_buttons: dict[str, Gtk.ToggleButton] = {}
        for name, (x, y, w, h) in ANSI_LAYOUT.items():
            button = Gtk.ToggleButton()
            button.set_size_request(int(w * UNIT_PX) - 3, int(h * UNIT_PX) - 3)
            button.add_css_class("keycap")
            button.set_name(f"cap-{name}")
            button.connect("toggled", self.on_keycap_toggled, name)
            self.keycap_buttons[name] = button
            self.key_fixed.put(button, x * UNIT_PX, y * UNIT_PX)
        self.refresh_keycap_labels()
        self.refresh_keycap_css()

    def refresh_keycap_labels(self) -> None:
        """Label each cap, using the library's display_str where available."""
        for name, button in self.keycap_buttons.items():
            label = LABEL_OVERRIDES.get(name)
            if label is None:
                key = self._key_by_name(name)
                label = (key.display_str if key else name).strip() or name
            if name == "BACKSLASH":
                # The cap is whatever is printed on your keyboard. Don't inherit
                # the display_str of whichever candidate index is being tested --
                # aliasing to HASH would relabel it "#", which is an ISO key that
                # does not exist on this board.
                label = "\\"
            button.set_label(label)
            index = self._index_for(name)
            if index is None:
                button.set_tooltip_text(
                    f"{name}: no LED index in the library's RT100 keymap"
                )
                button.set_sensitive(False)
            else:
                button.set_tooltip_text(f"{name} — LED index {index}")
                button.set_sensitive(True)

    def refresh_keycap_css(self) -> None:
        rules = []
        for name, colour in self.key_colours.items():
            if name not in self.keycap_buttons:
                continue
            fg = "#000000" if _is_light(colour) else "#ffffff"
            rules.append(
                f"#cap-{name} {{ background-image: none; background-color: {colour}; "
                f"color: {fg}; }}"
            )
        self.keycap_css.load_from_string("\n".join(rules))

    def on_keycap_toggled(self, button: Gtk.ToggleButton, name: str) -> None:
        if button.get_active():
            self.picked.add(name)
            button.add_css_class("picked")
        else:
            self.picked.discard(name)
            button.remove_css_class("picked")

    def set_picked(self, names: set[str]) -> None:
        self.picked = set(names)
        for name, button in self.keycap_buttons.items():
            want = name in self.picked
            if button.get_active() != want:
                button.set_active(want)

    def on_paint_selected(self, *_args) -> None:
        if not self.picked:
            self.toast("Select some keys first.")
            return
        colour = _hex(self.key_colour.get_rgba())
        for name in self.picked:
            self.key_colours[name] = colour
        self.refresh_keycap_css()
        self.persist()

    def on_wipe_keys(self, *_args) -> None:
        self.key_colours.clear()
        self.refresh_keycap_css()
        self.persist()

    # -------------------------------------------------------- key indices --

    def _key_by_name(self, name: str):
        if self.keyboard_keys is None:
            return None
        if name == "BACKSLASH":
            return self.keyboard_keys.get_key_by_name(self.backslash_index_name)
        return self.keyboard_keys.get_key_by_name(name)

    def _index_for(self, name: str) -> int | None:
        key = self._key_by_name(name)
        return key.value if key else None

    def on_test_backslash(self, *_args) -> None:
        order = BACKSLASH_CANDIDATES
        current = order.index(self.backslash_index_name) if \
            self.backslash_index_name in order else 0
        self.backslash_index_name = order[(current + 1) % len(order)]
        self.persist()
        self.refresh_keycap_labels()
        index = self._index_for("BACKSLASH")
        self.backslash_row.set_subtitle(
            f"Now trying LED index {index} (the library calls it "
            f"{self.backslash_index_name}). Only that key should light up."
        )
        self.run_on_device(
            f"Lighting index {index}",
            lambda dev: self._send_single_key(dev, index),
        )

    def _send_single_key(self, dev: RT100, index: int | None) -> str:
        keys = KeyboardKeys(dev.keymap_config)
        mapping = EpomakerKeyRGBCommand.KeyMap(keys)
        for key in keys:
            mapping[key] = (0, 0, 0) if key.value != index else (255, 255, 255)
        frame = EpomakerKeyRGBCommand.KeyboardRGBFrame(key_map=mapping)
        command = EpomakerKeyRGBCommand.EpomakerKeyRGBCommand([frame])
        dev.send_paced(command, ERASE_DELAY_S, KEY_PACKET_DELAY_S)
        return f"Lit LED index {index}. If that is your backslash key, keep this setting."

    # --------------------------------------------------------- device state --

    def refresh_device(self) -> None:
        if IMPORT_ERROR:
            return
        try:
            state = scan_bus()
        except Exception as exc:
            self.show_banner(f"Could not scan USB: {exc}", "error")
            return

        if not state["wired"]:
            if state["wireless"]:
                self.show_banner(
                    "Only the 2.4 GHz dongle is connected. This app is USB-wired "
                    "only — plug the keyboard in with its cable.", "warning",
                )
            else:
                self.show_banner(
                    "Keyboard not found on USB. Plug in its USB-C cable — "
                    "this is checked again every few seconds.", "error",
                )
            self.set_controls_enabled(
                False,
                "Only the 2.4 GHz dongle is connected — this app needs the USB "
                "cable." if state["wireless"] else
                "Keyboard not connected. Plug in its USB-C cable.",
            )
            return

        interfaces = state["interfaces"]
        missing = self.interface not in interfaces
        note = (
            f"RT100 connected — interfaces {', '.join(str(i) for i in interfaces)}. "
            f"Using interface {self.interface}."
        )
        if missing:
            note = (
                f"RT100 connected, but interface {self.interface} is not present "
                f"(found {', '.join(str(i) for i in interfaces)})."
            )
        self.show_banner(note, "warning" if missing else "ok")
        self.set_controls_enabled(
            not missing,
            "" if not missing else
            f"Interface {self.interface} is not present — pick another in the "
            "title bar.",
        )

        if self.keyboard_keys is None:
            try:
                config = load_main_config()
                from epomakercontroller.configs.configs import Config, ConfigType

                self.keyboard_keys = KeyboardKeys(
                    Config(ConfigType.CONF_KEYMAP, config["CONF_KEYMAP_PATH"])
                )
                self.refresh_keycap_labels()
            except Exception as exc:
                self.show_banner(f"Could not load the RT100 keymap: {exc}", "error")

    def show_banner(self, text: str, kind: str) -> None:
        self.banner.set_title(text)
        self.banner.set_revealed(True)
        for css in ("error", "warning", "success"):
            self.banner.remove_css_class(css)
        self.banner.add_css_class({"error": "error", "warning": "warning"}.get(kind, "success"))

    def set_controls_enabled(self, enabled: bool, reason: str = "") -> None:
        self.device_ready = enabled
        self.unavailable_reason = reason
        self._sync_upload_button()

    def _sync_upload_button(self) -> None:
        """Enable the upload button, and say why not when it is disabled.

        A greyed-out button with the explanation only in a banner at the top of a
        scrollable page tells the user nothing, because by the time they reach the
        button the banner is off screen.
        """
        if not hasattr(self, "upload_button"):
            return
        ready = self.device_ready and self.image_path is not None
        self.upload_button.set_sensitive(ready)

        if ready:
            note = ""
        elif not self.device_ready:
            note = self.unavailable_reason or "Keyboard not available."
        else:
            note = "Choose an image first."
        self.upload_button.set_tooltip_text(note or None)
        self.upload_reason.set_label(note)
        self.upload_reason.set_visible(bool(note))

    def poll_device(self) -> bool:
        """Re-check presence on a timer so unplug/replug recovers by itself."""
        if not self.busy:
            try:
                state = scan_bus()
            except Exception:
                return True
            signature = (len(state["wired"]), tuple(state["interfaces"]),
                         len(state["wireless"]))
            if signature != getattr(self, "_bus_signature", None):
                self._bus_signature = signature
                self.refresh_device()
        return True  # keep the timer alive

    def on_interface_changed(self, drop: Gtk.DropDown, *_args) -> None:
        self.interface = drop.get_selected()
        self.persist()
        self.refresh_device()

    def persist(self) -> None:
        # Merge rather than replace, so last_image survives unrelated saves.
        self.settings.update({
            "interface": self.interface,
            "backslash_index_name": self.backslash_index_name,
            "key_colours": self.key_colours,
            "fit_mode": self.fit_mode,
            "restore_screen": (
                self.restore_row.get_active() if hasattr(self, "restore_row") else True
            ),
        })
        save_settings(self.settings)

    # ------------------------------------------------------------ operations --

    def run_on_device(
        self, label: str, work: Callable[[RT100], str], restore_screen: bool = False
    ) -> None:
        """Open the device on a worker thread, run `work`, always close it.

        The CPU/temp daemon holds the device for *any* operation, not only
        uploads, so it is stopped around the whole thing and restarted in a
        finally block -- the same shape as the library repo's
        service/epomaker-upload-image helper.

        With restore_screen, the last uploaded picture is re-sent afterwards.
        Writing key colours issues the 0x18 "erase key SRAM" report, which on
        this hardware also clears the screen's content buffer, so a backlight
        change blanks the screen until something redraws it.
        """
        if IMPORT_ERROR or not getattr(self, "device_ready", False):
            self.toast("Keyboard is not available.")
            return
        if self.busy:
            self.toast("Already talking to the keyboard.")
            return
        self.busy = True

        # Snapshot anything widget-derived here, on the main thread.
        payload = self._screen_payload() if restore_screen else None

        def thread_body() -> None:
            device: RT100 | None = None
            guard = DaemonGuard()
            notes: list[str] = []
            try:
                stopped = guard.stop()
                if stopped:
                    notes.append(stopped)
                config = load_main_config()
                device = RT100(config, interface=self.interface)
                device.open_device()
                message = work(device)
                if payload is not None:
                    GLib.idle_add(self._begin_restore)
                    kind, data = payload
                    if kind == "gif":
                        device.send_gif_paced(data)
                        message += " Animation restored."
                    else:
                        self._upload_pixbuf(device, data)
                        message += " Screen picture restored."
                GLib.idle_add(self._finish, " ".join([message, *notes]), None)
            except DeviceMissing as exc:
                GLib.idle_add(self._finish, None, str(exc))
            except DevicePermission as exc:
                GLib.idle_add(self._finish, None, f"{UDEV_FIX}\n\n({exc})")
            except DeviceBusy as exc:
                GLib.idle_add(self._finish, None, str(exc))
            except ValueError as exc:
                # open_device raises this when nothing matches
                GLib.idle_add(self._finish, None, f"Keyboard not found: {exc}")
            except Exception as exc:
                GLib.idle_add(
                    self._finish, None,
                    f"{label} failed.\n\n{type(exc).__name__}: {exc}\n\n"
                    f"{traceback.format_exc(limit=3)}",
                )
            finally:
                if device is not None:
                    try:
                        device.close_device()
                    except Exception:
                        pass
                try:
                    guard.restart()
                except Exception:
                    pass

        threading.Thread(target=thread_body, daemon=True, name="rt100-io").start()

    def _screen_payload(self) -> tuple[str, object] | None:
        """What to put back on the screen, as ("still"|"gif", data), or None."""
        if not getattr(self, "restore_row", None) or not self.restore_row.get_active():
            return None
        if getattr(self, "_last_sent", None) is not None:
            return self._last_sent
        # Nothing sent this run -- rebuild from what was remembered last time.
        remembered = self.settings.get("last_image")
        if not remembered:
            return None
        try:
            frames = load_frames(remembered["path"])
            bg = _rgba(remembered.get("bar", "#000000"))
            mode = remembered.get("fit", "letterbox")
            if remembered.get("animated") and len(frames) > 1 and HAVE_PILLOW:
                handle, temp = tempfile.mkstemp(prefix="rt100-anim-", suffix=".gif")
                os.close(handle)
                render_gif(frames, mode, bg, temp)
                self._last_sent = ("gif", temp)
                return self._last_sent
            index = min(remembered.get("frame", 0), len(frames) - 1)
            return ("still", fit_pixbuf(frames[index], mode, bg))
        except Exception:
            return None

    def _upload_pixbuf(
        self, dev: RT100, pixbuf: GdkPixbuf.Pixbuf, report: bool = False
    ) -> None:
        """Encode and send one already-fitted pixbuf. Runs on the worker thread."""
        handle, temp = tempfile.mkstemp(prefix="rt100-", suffix=".png")
        os.close(handle)
        try:
            pixbuf.savev(temp, "png", [], [])
            command = EpomakerImageCommand.EpomakerImageCommand()
            command.encode_image(temp)
            progress = None
            if report:
                def progress(done: int, total: int) -> None:
                    GLib.idle_add(self._set_progress, done, total)
            dev.send_paced(command, ERASE_DELAY_S, IMAGE_PACKET_DELAY_S, progress)
        finally:
            try:
                os.unlink(temp)
            except OSError:
                pass

    def _begin_restore(self) -> bool:
        self.upload_progress.set_visible(True)
        self.upload_progress.set_fraction(0.0)
        self.upload_progress.set_text("Restoring the screen picture…")
        return False

    def _finish(self, message: str | None, error: str | None) -> bool:
        self.busy = False
        self.upload_progress.set_visible(False)
        if error:
            self.show_error(error)
        elif message:
            self.toast(message)
        return False

    def on_apply_solid(self, *_args) -> None:
        r, g, b = _rgb(self.solid_colour.get_rgba())

        def work(dev: RT100) -> str:
            dev.set_rgb_all_keys(r, g, b)
            return f"All keys set to rgb({r}, {g}, {b})."

        self.run_on_device("Solid colour", work, restore_screen=True)

    def on_apply_mode(self, *_args) -> None:
        mode = self.modes[self.mode_row.get_selected()]
        speed = _closest(Profile.Speed, int(self.speed_row.get_value()))
        brightness = _closest(Profile.Brightness, int(self.bright_row.get_value()))
        dazzle = Profile.Dazzle.ON if self.dazzle_row.get_active() else Profile.Dazzle.OFF
        option = self.direction_names[self.direction_row.get_selected()][1]
        rgb = _rgb(self.mode_colour.get_rgba())

        def work(dev: RT100) -> str:
            dev.set_profile(Profile(mode=mode, speed=speed, brightness=brightness,
                                    dazzle=dazzle, option=option, rgb=rgb))
            return f"Effect set to {mode.name.replace('_', ' ').title()}."

        self.run_on_device("Light mode", work)

    def on_step_mode(self, *_args) -> None:
        nxt = (self.mode_row.get_selected() + 1) % len(self.modes)
        self.mode_row.set_selected(nxt)
        self.on_apply_mode()

    def on_send_keys(self, *_args) -> None:
        wanted = dict(self.key_colours)

        def work(dev: RT100) -> str:
            keys = KeyboardKeys(dev.keymap_config)
            mapping = EpomakerKeyRGBCommand.KeyMap(keys)
            index_to_colour: dict[int, tuple[int, int, int]] = {}
            for name, colour in wanted.items():
                index = self._index_for(name)
                if index is not None:
                    index_to_colour[index] = _hex_to_rgb(colour)
            for key in keys:
                mapping[key] = index_to_colour.get(key.value, (0, 0, 0))
            frame = EpomakerKeyRGBCommand.KeyboardRGBFrame(key_map=mapping)
            command = EpomakerKeyRGBCommand.EpomakerKeyRGBCommand([frame])
            dev.send_paced(command, ERASE_DELAY_S, KEY_PACKET_DELAY_S)
            return f"Sent {len(index_to_colour)} coloured keys."

        self.run_on_device("Per-key colours", work, restore_screen=True)

    # ---------------------------------------------------------------- screen --

    def on_browse(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Choose an image")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images and GIFs (GIFs send one frame)")
        for pattern in PICKER_PATTERNS:
            image_filter.add_pattern(pattern)
        filters.append(image_filter)
        dialog.set_filters(filters)
        dialog.open(self, None, self._browse_done)

    def _browse_done(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile is None:
            return
        self.image_path = gfile.get_path()
        try:
            self.frames = load_frames(self.image_path)
        except Exception as exc:
            self.show_error(f"Could not read that image.\n\n{type(exc).__name__}: {exc}")
            return

        count = len(self.frames)
        self.frame_index = 0
        animated = count > 1 and HAVE_PILLOW
        self.animate_row.set_visible(count > 1)
        self.animate_row.set_sensitive(HAVE_PILLOW)
        if count > 1 and not HAVE_PILLOW:
            self.animate_row.set_active(False)
            self.animate_row.set_subtitle(
                "Needs Pillow installed — sending a single frame instead."
            )
        if count > 1:
            self.frame_row.set_range(1, count)
            self.frame_row.set_value(1)
            sent = min(count, GIF_MAX_FRAMES)
            self.animate_row.set_subtitle(
                f"Sends {sent} of {count} frames at {GIF_FRAMERATE} fps"
                if count > GIF_MAX_FRAMES else
                f"Sends all {count} frames at {GIF_FRAMERATE} fps"
            )
        self._sync_frame_rows()
        name = Path(self.image_path).name
        self.file_row.set_subtitle(f"{name} ({count} frames)" if count > 1 else name)
        self.update_preview()
        self._sync_upload_button()

    def on_frame_changed(self, row: Adw.SpinRow, *_args) -> None:
        self.frame_index = max(0, int(row.get_value()) - 1)
        self.update_preview()

    def on_animate_changed(self, *_args) -> None:
        self._sync_frame_rows()
        self.update_preview()

    def _sync_frame_rows(self) -> None:
        """Only offer a frame choice when a single frame is what gets sent."""
        multi = len(self.frames) > 1
        self.frame_row.set_visible(multi and not self.animate_row.get_active())

    @property
    def sending_animation(self) -> bool:
        return (len(self.frames) > 1 and HAVE_PILLOW
                and self.animate_row.get_active())

    def on_fit_changed(self, row: Adw.ComboRow, *_args) -> None:
        self.fit_mode = FIT_MODES[row.get_selected()][0]
        row.set_subtitle(FIT_MODES[row.get_selected()][2])
        self.persist()
        self.update_preview()

    def update_preview(self) -> None:
        if not self.image_path or not getattr(self, "frames", None):
            return
        index = 0 if self.sending_animation else min(self.frame_index,
                                                     len(self.frames) - 1)
        # Preview at the real upload resolution: animations are 128x128, not the
        # 162x173 a still gets, and that difference is visible on the screen.
        size = GIF_DIMENSIONS if self.sending_animation else None
        try:
            pixbuf = fit_pixbuf(
                self.frames[index], self.fit_mode, self.bar_colour.get_rgba(),
                size=size,
            )
        except Exception as exc:
            self.show_error(f"Could not render that image.\n\n{type(exc).__name__}: {exc}")
            return
        self.preview.set_pixbuf(pixbuf)
        self._fitted = pixbuf

    def on_upload(self, *_args) -> None:
        if not self.image_path or not getattr(self, "_fitted", None):
            return
        pixbuf = self._fitted
        self.upload_progress.set_visible(True)
        self.upload_progress.set_fraction(0.0)
        self.upload_progress.set_text("Preparing…")

        if self.sending_animation:
            frames = list(self.frames)
            mode, bg = self.fit_mode, self.bar_colour.get_rgba()

            def work(dev: RT100) -> str:
                handle, temp = tempfile.mkstemp(prefix="rt100-anim-", suffix=".gif")
                os.close(handle)
                count = render_gif(frames, mode, bg, temp)
                GLib.idle_add(self._pulse, f"Sending {count} frames…")
                dev.send_gif_paced(temp)
                # Kept, not deleted: a later backlight change re-sends it.
                self._replace_last_sent(("gif", temp))
                GLib.idle_add(self._remember_image)
                return f"Animation uploaded — {count} frames."

            self.run_on_device("Animation upload", work)
            return

        def work(dev: RT100) -> str:
            self._upload_pixbuf(dev, pixbuf, report=True)
            self._replace_last_sent(("still", pixbuf))
            GLib.idle_add(self._remember_image)
            return "Image uploaded."

        self.run_on_device("Image upload", work)

    def _replace_last_sent(self, payload: tuple[str, object]) -> None:
        """Swap in the newest payload, cleaning up any temp GIF it replaces."""
        previous = getattr(self, "_last_sent", None)
        if previous and previous[0] == "gif" and previous[1] != payload[1]:
            try:
                os.unlink(previous[1])
            except OSError:
                pass
        self._last_sent = payload

    def _pulse(self, text: str) -> bool:
        self.upload_progress.set_visible(True)
        self.upload_progress.set_text(text)
        self.upload_progress.pulse()
        return False

    def _remember_image(self) -> bool:
        self.settings["last_image"] = {
            "path": self.image_path,
            "fit": self.fit_mode,
            "frame": self.frame_index,
            "bar": _hex(self.bar_colour.get_rgba()),
            "animated": self.sending_animation,
        }
        self.persist()
        return False

    def _set_progress(self, done: int, total: int) -> bool:
        self.upload_progress.set_fraction(done / total)
        self.upload_progress.set_text(f"Sending packet {done} of {total}")
        return False

    # ------------------------------------------------------------- feedback --

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def show_error(self, text: str) -> None:
        dialog = Adw.AlertDialog(heading="That did not work", body=text)
        dialog.add_response("ok", "Close")
        dialog.present(self)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _rgba(hex_colour: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.parse(hex_colour)
    return rgba


def _rgb(rgba: Gdk.RGBA) -> tuple[int, int, int]:
    return (int(round(rgba.red * 255)), int(round(rgba.green * 255)),
            int(round(rgba.blue * 255)))


def _hex(rgba: Gdk.RGBA) -> str:
    return "#%02x%02x%02x" % _rgb(rgba)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _is_light(hex_colour: str) -> bool:
    r, g, b = _hex_to_rgb(hex_colour)
    return (0.299 * r + 0.587 * g + 0.114 * b) > 150


def _closest(enum_cls, value: int):
    """Map a spin value onto the nearest member of a Profile enum."""
    return min(enum_cls, key=lambda member: abs(member.value - value))


class Application(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        accent, bright = read_desktop_accent()
        provider = Gtk.CssProvider()
        provider.load_from_string(BASE_CSS.format(accent=accent, bright=bright))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def do_activate(self) -> None:
        window = self.props.active_window or Window(self)
        window.present()


def run_daemon(sensor: str | None, interface: int) -> int:
    """Headless loop: clock once, then CPU and temperature forever.

    This exists instead of upstream's `epomakercontroller start-daemon` for two
    reasons. Its CLI opens the device with hid.device().open(vid, pid), which
    takes interface 0 -- the one carrying key input, so it interferes with
    typing. And it inherits the working-directory-relative config paths that
    _stabilise_library_paths() works around here.
    """
    if IMPORT_ERROR:
        print(f"Cannot start: {IMPORT_ERROR}", file=sys.stderr)
        return 1

    import signal as signal_module
    import time as time_module

    stopping = False

    def handle_stop(*_args) -> None:
        nonlocal stopping
        stopping = True

    signal_module.signal(signal_module.SIGTERM, handle_stop)
    signal_module.signal(signal_module.SIGINT, handle_stop)

    device: RT100 | None = None
    try:
        device = RT100(load_main_config(), interface=interface)
        device.open_device()
        device.send_time()
        print(f"Screen updater running (interface {interface}, sensor {sensor}).",
              flush=True)
        while not stopping:
            try:
                import psutil
                device.send_cpu(int(psutil.cpu_percent()))
                if stopping:
                    break
                if sensor:
                    temperature = read_sensor(sensor)
                    if temperature is not None:
                        device.send_temperature(int(temperature))
            except Exception as exc:
                print(f"Update failed: {exc}", file=sys.stderr, flush=True)
                return 1
            for _ in range(16):  # ~1.6s, responsive to SIGTERM
                if stopping:
                    break
                time_module.sleep(0.1)
    except DevicePermission as exc:
        print(f"{UDEV_FIX}\n\n({exc})", file=sys.stderr)
        return 1
    except (DeviceMissing, ValueError) as exc:
        print(f"Keyboard not available: {exc}", file=sys.stderr)
        return 1
    finally:
        if device is not None:
            try:
                device.close_device()
            except Exception:
                pass
    print("Screen updater stopped.", flush=True)
    return 0


def main() -> int:
    if "--daemon" in sys.argv:
        args = [a for a in sys.argv[1:] if a != "--daemon"]
        sensor = args[0] if args else None
        settings = load_settings()
        return run_daemon(sensor, int(settings.get("interface", DEFAULT_INTERFACE)))
    return Application().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
