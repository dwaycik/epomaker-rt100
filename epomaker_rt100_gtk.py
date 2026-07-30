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

APP_ID = "io.github.dano.EpomakerRT100"
SETTINGS_PATH = Path(GLib.get_user_config_dir()) / "epomaker-rt100-gtk" / "settings.json"

# --------------------------------------------------------------------------- #
# Library imports, deferred so a missing dependency becomes a UI message
# rather than a traceback on stderr.
# --------------------------------------------------------------------------- #

IMPORT_ERROR: str | None = None
try:
    import hid  # provided by the `hidapi` package, a dependency of the library

    from epomakercontroller.commands import (
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

# The one genuinely unknown mapping. On a UK ISO board the library's index 10
# ("BACKSLASH") is the key between Left Shift and Z, and index 75 ("HASH") is
# the key left of Enter. A US ANSI board has neither of those switches -- its
# backslash sits at the end of the QWERTY row. Which LED index the firmware
# assigns to it is NOT recorded anywhere in the library source, so it is not
# guessed here: the UI offers a one-click test of each candidate.
BACKSLASH_CANDIDATES = ["BACKSLASH", "HASH", "ENTER"]

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


class RT100(EpomakerController):
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

    def __init__(self, config, interface: int = 1) -> None:
        self._interface = interface
        self._pid: int | None = None
        super().__init__(config, dry_run=False)

    def _setup_signal_handling(self) -> None:  # noqa: D102 - deliberate no-op
        pass

    def _find_product_id(self) -> int | None:
        self._pid = super()._find_product_id()
        return self._pid

    def _find_device_path(self) -> bytes | None:
        for entry in hid.enumerate(self.vendor_id, self._pid):
            if entry.get("interface_number") == self._interface:
                return entry["path"]
        return None

    def _open_device(self, device_path: bytes) -> None:
        # The base implementation swallows the IOError, prints a sudo
        # suggestion and then trips a bare `assert`, so a permission problem
        # arrives as an AssertionError. Replaced with typed errors.
        self.device = hid.device()
        try:
            self.device.open_path(device_path)
        except (IOError, OSError) as exc:
            self.device = None
            raise DevicePermission(str(exc)) from exc

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
        return f"Stopped {self.unit} ({where}) for the upload."

    def restart(self) -> str | None:
        if not self.was_active:
            return None
        result = self._run("start", self.unit)
        self.was_active = False
        if result.returncode != 0:
            return f"Could not restart {self.unit}: {result.stderr.strip()}"
        return f"Restarted {self.unit}."


# --------------------------------------------------------------------------- #
# Image fitting. GdkPixbuf is already present via GTK, so no extra dependency
# is pulled in for this.
# --------------------------------------------------------------------------- #

FIT_MODES = [
    ("letterbox", "Show the whole image", "Adds bars so nothing is cut off"),
    ("crop", "Fill the screen", "Crops the edges to fill it completely"),
    ("stretch", "Stretch to fit", "Uses every pixel, distorts the shape"),
]


def fit_image(path: str, mode: str, bg: Gdk.RGBA) -> GdkPixbuf.Pixbuf:
    """Render `path` into an IMAGE_DIMENSIONS canvas.

    IMAGE_DIMENSIONS comes straight from the library
    (commands/data/constants.py) and is a cv2.resize dsize, so it reads
    (width, height). The library's own encode_image does a bare resize with no
    aspect handling, which is why the fitting happens here instead.
    """
    width, height = IMAGE_DIMENSIONS
    source = GdkPixbuf.Pixbuf.new_from_file(path)
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
        self.interface = int(self.settings.get("interface", 1))
        self.backslash_index_name = self.settings.get("backslash_index_name", "BACKSLASH")
        self.key_colours: dict[str, str] = dict(self.settings.get("key_colours", {}))
        self.picked: set[str] = set()
        self.busy = False
        self.keyboard_keys = None
        self.image_path: str | None = None
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
        self.refresh_device()

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
                "Not recorded in the library, which only ships a UK ISO map. Light each "
                "candidate and keep whichever matches your backslash key."
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
        keys = KeyboardKeys(dev.config_keymap)
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
                self.show_banner("No RT100 found on USB. Is it plugged in?", "error")
            self.set_controls_enabled(False)
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
        self.set_controls_enabled(not missing)

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

    def set_controls_enabled(self, enabled: bool) -> None:
        self.device_ready = enabled
        if hasattr(self, "upload_button"):
            self.upload_button.set_sensitive(enabled and self.image_path is not None)

    def on_interface_changed(self, drop: Gtk.DropDown, *_args) -> None:
        self.interface = drop.get_selected()
        self.persist()
        self.refresh_device()

    def persist(self) -> None:
        save_settings({
            "interface": self.interface,
            "backslash_index_name": self.backslash_index_name,
            "key_colours": self.key_colours,
            "fit_mode": self.fit_mode,
        })

    # ------------------------------------------------------------ operations --

    def run_on_device(self, label: str, work: Callable[[RT100], str]) -> None:
        """Open the device on a worker thread, run `work`, always close it."""
        if IMPORT_ERROR or not getattr(self, "device_ready", False):
            self.toast("Keyboard is not available.")
            return
        if self.busy:
            self.toast("Already talking to the keyboard.")
            return
        self.busy = True

        def thread_body() -> None:
            device: RT100 | None = None
            try:
                config = load_main_config()
                device = RT100(config, interface=self.interface)
                device.open_device()
                message = work(device)
                GLib.idle_add(self._finish, message, None)
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

        threading.Thread(target=thread_body, daemon=True, name="rt100-io").start()

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

        self.run_on_device("Solid colour", work)

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
            keys = KeyboardKeys(dev.config_keymap)
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

        self.run_on_device("Per-key colours", work)

    # ---------------------------------------------------------------- screen --

    def on_browse(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Choose an image")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        image_filter = Gtk.FileFilter()
        image_filter.set_name("Images the keyboard accepts")
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tif", "*.tiff", "*.webp"):
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
        self.file_row.set_subtitle(self.image_path or "")
        self.update_preview()
        self.upload_button.set_sensitive(getattr(self, "device_ready", False))

    def on_fit_changed(self, row: Adw.ComboRow, *_args) -> None:
        self.fit_mode = FIT_MODES[row.get_selected()][0]
        row.set_subtitle(FIT_MODES[row.get_selected()][2])
        self.persist()
        self.update_preview()

    def update_preview(self) -> None:
        if not self.image_path:
            return
        try:
            pixbuf = fit_image(self.image_path, self.fit_mode, self.bar_colour.get_rgba())
        except Exception as exc:
            self.show_error(f"Could not read that image.\n\n{type(exc).__name__}: {exc}")
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

        def work(dev: RT100) -> str:
            notes: list[str] = []
            guard = DaemonGuard()
            stopped = guard.stop()
            if stopped:
                notes.append(stopped)
            try:
                handle, temp = tempfile.mkstemp(prefix="rt100-", suffix=".png")
                os.close(handle)
                try:
                    pixbuf.savev(temp, "png", [], [])
                    command = EpomakerImageCommand.EpomakerImageCommand()
                    command.encode_image(temp)

                    def progress(done: int, total: int) -> None:
                        GLib.idle_add(self._set_progress, done, total)

                    dev.send_paced(command, ERASE_DELAY_S, IMAGE_PACKET_DELAY_S, progress)
                finally:
                    try:
                        os.unlink(temp)
                    except OSError:
                        pass
            finally:
                restarted = guard.restart()
                if restarted:
                    notes.append(restarted)
            return " ".join(["Image uploaded.", *notes])

        self.run_on_device("Image upload", work)

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


def main() -> int:
    return Application().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
