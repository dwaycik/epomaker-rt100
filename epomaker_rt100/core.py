"""Device, imaging and system layer for the Epomaker RT100 — no UI toolkit.

Everything here is shared by both front ends. Nothing in this module imports
GTK, Textual or any other toolkit, so a headless daemon or a terminal UI can use
it without pulling in a desktop stack.

Hardware facts come from the EpomakerController library at runtime rather than
being restated here. The exceptions are documented inline: the US ANSI geometry
(the library ships UK ISO only) and several workarounds for upstream defects.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

APP_ID = "io.github.dwaycik.EpomakerRT100"

# EPOMAKER_RT100_CONFIG_DIR redirects the settings file. Its reason for existing
# is test isolation: without it, a test run scribbles over real preferences.
_CONFIG_DIR = Path(
    os.environ.get("EPOMAKER_RT100_CONFIG_DIR")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "epomaker-rt100-gtk"
)
SETTINGS_PATH = _CONFIG_DIR / "settings.json"


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
# Upstream 0.0.9 writes to the working directory. Move somewhere of our own
# before importing it, then repoint its constants at the installed files.
# --------------------------------------------------------------------------- #


def _stabilise_library_paths() -> Path:
    """Work around upstream 0.0.9's working-directory-relative paths.

    ``epomakercontroller/configs/constants.py`` hard-codes three relative paths:

        PATH_TO_DEFAULT_CONFIG = "src/epomakercontroller/configs/default.json"
        CONFIG_DIRECTORY       = ".epomaker-controller"
        TMP_FOLDER             = os.path.abspath("./.epomaker_controller")

    The first only resolves inside an upstream source checkout, so
    ``load_main_config()`` raises FileNotFoundError anywhere else — including a
    systemd service and any app-menu launch. The second makes the config
    per-working-directory. The third runs ``os.mkdir`` at *import time*,
    littering whichever directory the process started in. The library also
    writes ``.logs/`` beside it.

    Reported upstream as PR #93.
    """
    runtime = (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "epomaker-rt100-gtk"
        / "runtime"
    )
    runtime.mkdir(parents=True, exist_ok=True)
    os.chdir(runtime)
    return runtime


RUNTIME_DIR = _stabilise_library_paths()

IMPORT_ERROR: str | None = None
try:
    import hid  # from the `hidapi` package, a dependency of the library

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

    import epomakercontroller.configs.configs as _epo_configs
    import epomakercontroller.configs.constants as _epo_constants

    _installed_default = Path(_epo_configs.__file__).parent / "default.json"
    if _installed_default.exists():
        # configs.py binds the name with `from .constants import ...`, so both
        # modules need patching.
        for _module in (_epo_constants, _epo_configs):
            if hasattr(_module, "PATH_TO_DEFAULT_CONFIG"):
                _module.PATH_TO_DEFAULT_CONFIG = str(_installed_default)
    for _module in (_epo_constants, _epo_configs):
        if hasattr(_module, "CONFIG_DIRECTORY"):
            _module.CONFIG_DIRECTORY = str(Path.home() / ".epomaker-controller")
except Exception as exc:  # pragma: no cover - environment problem, not logic
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Interfaces
# --------------------------------------------------------------------------- #

# Interface 2 is the only one safe to hold.
#
# The libusb backend detaches the kernel HID driver from whatever interface it
# opens, and the RT100's input collections are not spread evenly:
#
#   0 -- main keyboard. Holding it interferes with typing.
#   1 -- Consumer Control (the volume knob), System Control, a second keyboard
#        collection and a mouse collection. Holding it drops the keyboard from
#        six input nodes to one, killing the volume knob and media keys until
#        the handle closes. Measured on hardware 2026-07-31.
#   2 -- no input collections at all, and it accepts every command this app
#        sends: lighting, profiles, screen images, clock, CPU and temperature.
#
# OpenRGB reaches the same conclusion independently — its Epomaker detector
# registers this VID/PID on interface 2.
DEFAULT_INTERFACE = 2

VENDOR_ID = 0x3151
WIRED_PRODUCT_IDS = (0x4010, 0x4015)
WIRELESS_PRODUCT_IDS = (0x4011, 0x4016)

# Pacing. The firmware erases SRAM on the init report and needs a pause before
# data arrives; the endpoint buffer can also overflow if reports arrive too
# fast, historically corrupting the high-index right-hand keys. A key frame is
# 8 packets, so 10 ms costs nothing. The 1002-packet image upload keeps
# upstream's back-to-back timing, since that path is known to work as-is.
ERASE_DELAY_S = 0.25
KEY_PACKET_DELAY_S = 0.010
IMAGE_PACKET_DELAY_S = 0.0

UDEV_FIX = """A permission error came back from the keyboard.

The hidapi libusb backend needs write access to the USB device node, which the
/dev/hidraw* ACLs do not cover. Install the udev rule (the filename must sort
before 73-seat-late.rules, or the uaccess tag is ignored):

  sudo install -m644 udev/70-epomaker-rt100.rules /etc/udev/rules.d/
  sudo udevadm control --reload-rules && sudo udevadm trigger

Then unplug and replug the keyboard. This app will not escalate privileges."""


class DeviceMissing(Exception):
    """No wired RT100 on the USB bus."""


class DevicePermission(Exception):
    """Found the keyboard but could not open it."""


class DeviceBusy(Exception):
    """Something else is holding the interface."""


# --------------------------------------------------------------------------- #
# US ANSI geometry — name -> (x, y, width, height) in keycap units.
#
# Key *names* and their LED indices are read from the library's RT100 keymap at
# runtime; only the physical arrangement lives here, because the library ships a
# UK ISO layout only.
# --------------------------------------------------------------------------- #

ANSI_LAYOUT: dict[str, tuple[float, float, float, float]] = {
    "ESC": (0, 0, 1, 1),
    "F1": (2, 0, 1, 1), "F2": (3, 0, 1, 1), "F3": (4, 0, 1, 1), "F4": (5, 0, 1, 1),
    "F5": (6.5, 0, 1, 1), "F6": (7.5, 0, 1, 1), "F7": (8.5, 0, 1, 1),
    "F8": (9.5, 0, 1, 1),
    "F9": (11, 0, 1, 1), "F10": (12, 0, 1, 1), "F11": (13, 0, 1, 1),
    "F12": (14, 0, 1, 1),
    "DEL": (15.5, 0, 1, 1), "PGUP": (16.5, 0, 1, 1), "PGDOWN": (17.5, 0, 1, 1),
    "DIAL": (18.5, 0, 1, 1),

    "BACKQUOTE": (0, 1.5, 1, 1),
    "NUMROW_1": (1, 1.5, 1, 1), "NUMROW_2": (2, 1.5, 1, 1),
    "NUMROW_3": (3, 1.5, 1, 1), "NUMROW_4": (4, 1.5, 1, 1),
    "NUMROW_5": (5, 1.5, 1, 1), "NUMROW_6": (6, 1.5, 1, 1),
    "NUMROW_7": (7, 1.5, 1, 1), "NUMROW_8": (8, 1.5, 1, 1),
    "NUMROW_9": (9, 1.5, 1, 1), "NUMROW_0": (10, 1.5, 1, 1),
    "NUMROW_MINUS": (11, 1.5, 1, 1), "NUMROW_EQUAL": (12, 1.5, 1, 1),
    "BACKSPACE": (13, 1.5, 2, 1),
    "NUMLOCK": (15.5, 1.5, 1, 1), "NUMPAD_SLASH": (16.5, 1.5, 1, 1),
    "NUMPAD_ASTERISK": (17.5, 1.5, 1, 1), "NUMPAD_MINUS": (18.5, 1.5, 1, 1),

    "TAB": (0, 2.5, 1.5, 1),
    "Q": (1.5, 2.5, 1, 1), "W": (2.5, 2.5, 1, 1), "E": (3.5, 2.5, 1, 1),
    "R": (4.5, 2.5, 1, 1), "T": (5.5, 2.5, 1, 1), "Y": (6.5, 2.5, 1, 1),
    "U": (7.5, 2.5, 1, 1), "I": (8.5, 2.5, 1, 1), "O": (9.5, 2.5, 1, 1),
    "P": (10.5, 2.5, 1, 1),
    "OPEN_SQBR": (11.5, 2.5, 1, 1), "CLOSE_SQBR": (12.5, 2.5, 1, 1),
    "BACKSLASH": (13.5, 2.5, 1.5, 1),
    "NUMPAD_7": (15.5, 2.5, 1, 1), "NUMPAD_8": (16.5, 2.5, 1, 1),
    "NUMPAD_9": (17.5, 2.5, 1, 1), "NUMPAD_PLUS": (18.5, 2.5, 1, 2),

    "CAPS": (0, 3.5, 1.75, 1),
    "A": (1.75, 3.5, 1, 1), "S": (2.75, 3.5, 1, 1), "D": (3.75, 3.5, 1, 1),
    "F": (4.75, 3.5, 1, 1), "G": (5.75, 3.5, 1, 1), "H": (6.75, 3.5, 1, 1),
    "J": (7.75, 3.5, 1, 1), "K": (8.75, 3.5, 1, 1), "L": (9.75, 3.5, 1, 1),
    "SEMICOLON": (10.75, 3.5, 1, 1), "QUOTE": (11.75, 3.5, 1, 1),
    "ENTER": (12.75, 3.5, 2.25, 1),
    "NUMPAD_4": (15.5, 3.5, 1, 1), "NUMPAD_5": (16.5, 3.5, 1, 1),
    "NUMPAD_6": (17.5, 3.5, 1, 1),

    "LEFT_SHIFT": (0, 4.5, 2.25, 1),
    "Z": (2.25, 4.5, 1, 1), "X": (3.25, 4.5, 1, 1), "C": (4.25, 4.5, 1, 1),
    "V": (5.25, 4.5, 1, 1), "B": (6.25, 4.5, 1, 1), "N": (7.25, 4.5, 1, 1),
    "M": (8.25, 4.5, 1, 1),
    "COMMA": (9.25, 4.5, 1, 1), "DOT": (10.25, 4.5, 1, 1),
    "SLASH": (11.25, 4.5, 1, 1),
    "RIGHT_SHIFT": (12.25, 4.5, 1.75, 1),
    "UP": (14, 4.5, 1, 1),
    "NUMPAD_1": (15.5, 4.5, 1, 1), "NUMPAD_2": (16.5, 4.5, 1, 1),
    "NUMPAD_3": (17.5, 4.5, 1, 1), "NUMPAD_ENTER": (18.5, 4.5, 1, 2),

    # The leftmost/rightmost Ctrl names follow the library's own ISO layout
    # file, which lists RIGHT_CTRL first and LEFT_CTRL last. That looks like an
    # upstream mix-up, but LED indices are not geometric (index 11 is the DIAL,
    # top-right) so it cannot be derived. Both caps read "Ctrl"; if the wrong
    # one lights, swap these two names.
    "RIGHT_CTRL": (0, 5.5, 1.25, 1),
    "LEFT_WIN": (1.25, 5.5, 1.25, 1),
    "LEFT_ALT": (2.5, 5.5, 1.25, 1),
    "SPACE": (3.75, 5.5, 5.25, 1),
    "RIGHT_ALT": (9, 5.5, 1, 1), "FN": (10, 5.5, 1, 1),
    "LEFT_CTRL": (11, 5.5, 1, 1),
    "LEFT": (12, 5.5, 1, 1), "DOWN": (13, 5.5, 1, 1), "RIGHT": (14, 5.5, 1, 1),
    "NUMPAD_0": (15.5, 5.5, 2, 1), "NUMPAD_DOT": (17.5, 5.5, 1, 1),
}

LAYOUT_UNITS_W = 19.5
LAYOUT_UNITS_H = 6.5

# Plain words rather than symbols like U+232B/U+21B5, which live in fallback
# fonts. Arrow keys keep the library's ← ↑ ↓ →.
LABEL_OVERRIDES = {
    "SPACE": "Space", "BACKSPACE": "Bksp", "CAPS": "Caps", "DIAL": "Dial",
    "LEFT_WIN": "Super", "NUMLOCK": "Num", "PGDOWN": "PgDn",
    "NUMPAD_ENTER": "Enter", "ENTER": "Enter",
}

# Confirmed on hardware 2026-07-31: the US ANSI backslash is LED index 75, which
# the library's UK ISO keymap names "HASH". On ISO, index 10 ("BACKSLASH") is
# the key between Left Shift and Z and 75 is the key left of Enter; an ANSI
# board has neither switch and puts backslash at the end of the QWERTY row.
# Index 75 is the matrix position those two share. The list stays so the same
# test resolves it on a board this does not match; the first entry is default.
BACKSLASH_CANDIDATES = ["HASH", "ENTER", "BACKSLASH"]


def keycap_label(name: str, keys: "KeyboardKeys | None") -> str:
    """Caption for a key, preferring the library's display_str."""
    if name == "BACKSLASH":
        # Never inherit the display_str of whichever candidate index is aliased
        # — HASH would relabel it "#", an ISO key absent from an ANSI board.
        return "\\"
    label = LABEL_OVERRIDES.get(name)
    if label is not None:
        return label
    key = keys.get_key_by_name(name) if keys else None
    return ((key.display_str if key else name) or name).strip() or name


# --------------------------------------------------------------------------- #
# Device
# --------------------------------------------------------------------------- #


class RT100(EpomakerController if not IMPORT_ERROR else object):  # type: ignore[misc]
    """EpomakerController with an explicit HID interface choice.

    0.0.9 opens with ``hid.device().open(vendor_id, product_id)``, which takes
    whatever hidapi enumerates first — interface 0, the one carrying key input.
    There is no way to ask for another, so the path-based open lives here.
    Reported upstream as issue #94.

    Signal handling is also disabled. The base class installs SIGINT/SIGTERM
    handlers that call ``os._exit(0)``, which fights any main loop and restricts
    construction to the main thread. Callers close the device in a finally block
    instead.
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
        """Open a specific interface, raising instead of swallowing failures.

        Upstream logs the IOError and sets ``self.device = None`` rather than
        raising, so a permission problem surfaces later as a confusing
        AssertionError.
        """
        path = None
        for entry in hid.enumerate(self.config.vendor_id, product_id):
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

        0.0.8 put config_layout/config_keymap directly on the controller; 0.0.9
        moved them behind an EpomakerConfig wrapper at ``self.config``.
        """
        keymap = getattr(getattr(self, "config", None), "config_keymap", None)
        if keymap is None:
            keymap = getattr(self, "config_keymap", None)
        if keymap is None:
            raise RuntimeError(
                "This build of EpomakerController exposes neither "
                "controller.config.config_keymap nor controller.config_keymap."
            )
        return keymap

    def send_paced(
        self,
        command,
        erase_delay: float,
        packet_delay: float,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Send a prepared command, pacing packets and honouring the SRAM erase.

        Mirrors ``_send_command`` but adds delays and a progress callback, which
        the library's monolithic senders do not offer.
        """
        if self.device is None:
            raise DeviceBusy("Device is not open")
        packets = list(command)
        total = len(packets)
        for index, packet in enumerate(packets):
            if len(packet) != BUFF_LENGTH:
                raise RuntimeError(
                    f"Packet {index} is {len(packet)}, expected {BUFF_LENGTH}"
                )
            self.device.send_feature_report(packet.get_all_bytes())
            if index == 0 and erase_delay:
                time.sleep(erase_delay)
            elif packet_delay:
                time.sleep(packet_delay)
            if progress is not None:
                progress(index + 1, total)

    def send_gif_paced(self, gif_path: str) -> None:
        """Upload an animated GIF using the library's native GIF command.

        New in upstream 0.0.9 (not on PyPI): EpomakerGifCommand implements the
        multi-frame protocol sniffed from the vendor software.
        """
        command = EpomakerGifCommand.EpomakerGifCommand(gif_path)
        if not command.encode_gif():
            raise RuntimeError("The library could not encode that GIF.")
        self._send_command(command)

    # -- convenience wrappers so front ends never build commands themselves --

    def apply_solid(self, rgb: tuple[int, int, int]) -> None:
        self.set_rgb_all_keys(*rgb)

    def apply_key_colours(self, index_to_rgb: dict[int, tuple[int, int, int]]) -> None:
        keys = KeyboardKeys(self.keymap_config)
        mapping = EpomakerKeyRGBCommand.KeyMap(keys)
        for key in keys:
            mapping[key] = index_to_rgb.get(key.value, (0, 0, 0))
        frame = EpomakerKeyRGBCommand.KeyboardRGBFrame(key_map=mapping)
        command = EpomakerKeyRGBCommand.EpomakerKeyRGBCommand([frame])
        self.send_paced(command, ERASE_DELAY_S, KEY_PACKET_DELAY_S)

    def light_one_index(self, index: int | None) -> None:
        """Light a single LED index and blank the rest — the calibration probe."""
        self.apply_key_colours({index: (255, 255, 255)} if index is not None else {})

    def upload_still(
        self, image, progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Encode and send one already-fitted PIL image."""
        handle, temp = tempfile.mkstemp(prefix="rt100-", suffix=".png")
        os.close(handle)
        try:
            image.save(temp, "PNG")
            command = EpomakerImageCommand.EpomakerImageCommand()
            command.encode_image(temp)
            self.send_paced(command, ERASE_DELAY_S, IMAGE_PACKET_DELAY_S, progress)
        finally:
            try:
                os.unlink(temp)
            except OSError:
                pass


def keymap_keys():
    """Load the RT100 keymap without opening the device."""
    from epomakercontroller.configs.configs import Config, ConfigType

    config = load_main_config()
    return KeyboardKeys(Config(ConfigType.CONF_KEYMAP, config["CONF_KEYMAP_PATH"]))


def scan_bus() -> dict:
    """Report what RT100 hardware is on the bus, without opening anything."""
    wired: list[dict] = []
    wireless: list[dict] = []
    for pid in WIRED_PRODUCT_IDS:
        wired.extend(hid.enumerate(VENDOR_ID, pid))
    for pid in WIRELESS_PRODUCT_IDS:
        wireless.extend(hid.enumerate(VENDOR_ID, pid))
    interfaces = sorted(
        {e["interface_number"] for e in wired if e["interface_number"] >= 0}
    )
    return {"wired": wired, "wireless": wireless, "interfaces": interfaces}


# --------------------------------------------------------------------------- #
# Imaging — Pillow, so neither front end needs a toolkit for this
# --------------------------------------------------------------------------- #

FIT_MODES = [
    ("letterbox", "Show the whole image", "Adds bars so nothing is cut off"),
    ("crop", "Fill the screen", "Crops the edges to fill it completely"),
    ("stretch", "Stretch to fit", "Uses every pixel, distorts the shape"),
]

PICKER_PATTERNS = ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tif", "*.tiff",
                   "*.webp", "*.gif")

GIF_MAX_FRAMES = 56  # the library subsamples above this
GIF_FRAMERATE = 15

try:
    from PIL import Image
    HAVE_PILLOW = True
except ImportError:  # pragma: no cover
    HAVE_PILLOW = False


def _gif_size() -> tuple[int, int]:
    """Frame size for animated uploads.

    The firmware places an animation frame at roughly 1:1 rather than scaling it
    to the panel, so a small frame occupies less of the screen. The real
    constraint is only 4K alignment — ``w * h * 2 % 4096 == 0`` — because the
    animation framebuffer is page-aligned and unaligned sizes produce vertical
    line artifacts.

    Upstream satisfies that by flooring both axes to multiples of 64, which is
    stricter than needed and caps a square source at 128x128: 58.5% of the
    162x173 panel. 128x160 is the largest legal size that fits (40960 bytes =
    10 pages exactly), covers 73.1%, and is closer to the panel's shape.

    Override with EPOMAKER_GIF_SIZE=WxH; validated before use.
    """
    default = (128, 160)
    raw = os.environ.get("EPOMAKER_GIF_SIZE")
    if not raw:
        return default
    try:
        width, height = (int(part) for part in raw.lower().split("x"))
    except ValueError:
        return default
    panel_w, panel_h = IMAGE_DIMENSIONS if not IMPORT_ERROR else (162, 173)
    if (width * height * 2) % 4096 or not (
        0 < width <= panel_w and 0 < height <= panel_h
    ):
        print(
            f"Ignoring EPOMAKER_GIF_SIZE={raw}: must fit {panel_w}x{panel_h} "
            "and satisfy w*h*2 % 4096 == 0.",
            file=sys.stderr,
        )
        return default
    return (width, height)


GIF_DIMENSIONS = _gif_size()


def patch_gif_dimensions() -> None:
    """Stop the library re-flooring an already-legal frame size.

    ``prepare_gif()`` calls ``EpomakerGifCommand.best_gif_dimensions`` by
    explicit class name, so a subclass override never runs. Upstream's version
    also floors the short axis of a wide source to zero — 800x200 gives
    (128, 0), which passes its own ``% 4096`` check because 0 % 4096 == 0 and
    uploads an empty frame. Reported upstream as PR #92.
    """
    if IMPORT_ERROR:
        return
    panel_w, panel_h = IMAGE_DIMENSIONS

    def best(source_width: int, source_height: int) -> tuple[int, int]:
        if (
            0 < source_width <= panel_w
            and 0 < source_height <= panel_h
            and (source_width * source_height * 2) % 4096 == 0
        ):
            return source_width, source_height
        ratio = min(panel_w / source_width, panel_h / source_height)
        width = math.ceil(source_width * ratio)
        height = math.ceil(source_height * ratio)
        return (max(64, math.floor(width / 64) * 64),
                max(64, math.floor(height / 64) * 64))

    EpomakerGifCommand.EpomakerGifCommand.best_gif_dimensions = staticmethod(best)


patch_gif_dimensions()


def load_frames(path: str) -> list["Image.Image"]:
    """Every frame of `path`, or a single-item list for a still.

    GIFs are accepted even though the library's SUPPORTED_FORMATS excludes them:
    a GIF frame is a fine still once extracted, and for animation the file goes
    through the separate GIF command instead.
    """
    image = Image.open(path)
    count = getattr(image, "n_frames", 1)
    if count <= 1:
        return [image.convert("RGB")]

    # Optimised GIFs store partial frames with transparency for unchanged areas,
    # so each must be composited onto a canvas to get a complete picture.
    frames: list[Image.Image] = []
    canvas = Image.new("RGBA", image.size, (0, 0, 0, 255))
    for number in range(min(count, 512)):
        image.seek(number)
        frame = image.convert("RGBA")
        canvas.paste(frame, (0, 0), frame)
        frames.append(canvas.copy().convert("RGB"))
        if getattr(image, "disposal_method", 0) == 2:
            canvas = Image.new("RGBA", image.size, (0, 0, 0, 255))
    return frames


def fit_image(
    source: "Image.Image",
    mode: str,
    background: tuple[int, int, int],
    size: tuple[int, int] | None = None,
) -> "Image.Image":
    """Render `source` into an IMAGE_DIMENSIONS canvas, or `size` if given.

    IMAGE_DIMENSIONS comes from the library and is a cv2.resize dsize, so it
    reads (width, height). The library's own encode_image resizes with no aspect
    handling at all, which is why fitting happens here.
    """
    width, height = size or IMAGE_DIMENSIONS
    source = source.convert("RGB")
    if mode == "stretch":
        return source.resize((width, height), Image.LANCZOS)

    scale = (
        max(width / source.width, height / source.height)
        if mode == "crop"
        else min(width / source.width, height / source.height)
    )
    new_size = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    scaled = source.resize(new_size, Image.LANCZOS)

    canvas = Image.new("RGB", (width, height), background)
    if mode == "crop":
        left = max(0, (scaled.width - width) // 2)
        top = max(0, (scaled.height - height) // 2)
        canvas.paste(scaled.crop((left, top, left + width, top + height)), (0, 0))
    else:
        canvas.paste(scaled, ((width - scaled.width) // 2,
                              (height - scaled.height) // 2))
    return canvas


def render_gif(
    frames: list["Image.Image"],
    mode: str,
    background: tuple[int, int, int],
    out_path: str,
) -> int:
    """Write `frames` out as a GIF the library can upload. Returns frame count."""
    step = max(1, len(frames) / GIF_MAX_FRAMES)
    picked = [
        frames[min(int(i * step), len(frames) - 1)]
        for i in range(min(len(frames), GIF_MAX_FRAMES))
    ]
    images = [fit_image(f, mode, background, size=GIF_DIMENSIONS) for f in picked]
    images[0].save(
        out_path,
        save_all=True,
        append_images=images[1:],
        duration=int(1000 / GIF_FRAMERATE),
        loop=0,
        optimize=False,
    )
    return len(images)


# --------------------------------------------------------------------------- #
# System integration
# --------------------------------------------------------------------------- #


class UserUnit:
    """Talk to a systemd --user unit.

    User scope deliberately: starting and stopping needs no authorisation, so
    the app never escalates. A system unit would put a polkit prompt in the
    middle of an upload.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True, timeout=30
        )

    def exists(self) -> bool:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return (base / "systemd" / "user" / self.name).exists()

    def _quiet(self, *args: str) -> bool:
        try:
            return self._run(*args).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def is_active(self) -> bool:
        return self._quiet("is-active", "--quiet", self.name)

    def is_enabled(self) -> bool:
        return self._quiet("is-enabled", "--quiet", self.name)

    def start(self) -> str | None:
        result = self._run("start", self.name)
        return None if result.returncode == 0 else result.stderr.strip()

    def stop(self) -> str | None:
        result = self._run("stop", self.name)
        return None if result.returncode == 0 else result.stderr.strip()

    def set_enabled(self, enabled: bool) -> str | None:
        result = self._run("enable" if enabled else "disable", self.name)
        return None if result.returncode == 0 else result.stderr.strip()


SERVICE_NAME = os.environ.get("EPOMAKER_SERVICE_NAME", "epomaker-controller.service")


@dataclass
class DaemonGuard:
    """Stop the screen updater around any device operation, then restart it.

    It holds the HID interface for *every* operation, not just uploads. Same
    approach as the library repo's service/epomaker-upload-image helper.
    """

    unit: str = field(default_factory=lambda: SERVICE_NAME)
    scope: list[str] | None = None
    was_active: bool = False

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", *(self.scope or []), *args],
            capture_output=True, text=True, timeout=30,
        )

    def stop(self) -> str | None:
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
                f"{self.unit} is running and holding the keyboard, but stopping "
                f"it failed:\n\n{result.stderr.strip() or 'unknown error'}\n\n"
                "Stop it yourself and try again."
            )
        return f"Paused {self.unit}."

    def restart(self) -> str | None:
        if not self.was_active:
            return None
        result = self._run("start", self.unit)
        self.was_active = False
        if result.returncode != 0:
            return f"Could not restart {self.unit}: {result.stderr.strip()}"
        return f"Resumed {self.unit}."


def list_temp_sensors() -> list[tuple[str, str]]:
    """(key, human label) per sensor. Keys match the library's get_device_temp."""
    try:
        import psutil
    except ImportError:
        return []
    try:
        readings = psutil.sensors_temperatures()
    except Exception:
        return []
    out: list[tuple[str, str]] = []
    for chip, entries in readings.items():
        for index, entry in enumerate(entries):
            key = f"{chip}-{index}"
            out.append((key, f"{entry.label or chip} ({key}) — {entry.current:.0f}°C"))
    return out


def read_sensor(key: str) -> float | None:
    try:
        import psutil

        chip, _, index = key.rpartition("-")
        return psutil.sensors_temperatures().get(chip, [])[int(index)].current
    except Exception:
        return None


def cpu_percent() -> int:
    try:
        import psutil

        return int(psutil.cpu_percent())
    except Exception:
        return 0


# --------------------------------------------------------------------------- #
# Headless daemon
# --------------------------------------------------------------------------- #


def run_daemon(sensor: str | None, interface: int = DEFAULT_INTERFACE) -> int:
    """Clock once, then CPU and temperature forever.

    Used instead of upstream's ``epomakercontroller start-daemon``, which opens
    interface 0 — the one carrying key input — and inherits the
    working-directory-relative config paths worked around above.
    """
    if IMPORT_ERROR:
        print(f"Cannot start: {IMPORT_ERROR}", file=sys.stderr)
        return 1

    import signal as signal_module

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
        print(
            f"Screen updater running (interface {interface}, sensor {sensor}).",
            flush=True,
        )
        while not stopping:
            try:
                device.send_cpu(cpu_percent())
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
                time.sleep(0.1)
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
