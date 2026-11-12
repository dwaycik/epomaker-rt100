"""Theme sync logic, no keyboard needed. Run: python tests/test_theme_sync.py"""
import os
import tempfile
from pathlib import Path

# Isolate before importing core: settings, desktop files and runtime state.
TMP = Path(tempfile.mkdtemp(prefix="rt100-test-"))
os.environ.update(
    EPOMAKER_RT100_CONFIG_DIR=str(TMP / "app"),
    XDG_CONFIG_HOME=str(TMP / "config"),
    XDG_STATE_HOME=str(TMP / "state"),
    XDG_RUNTIME_DIR=str(TMP / "run"),
)
(TMP / "run").mkdir()

from epomaker_rt100 import core  # noqa: E402

assert str(core.SETTINGS_PATH).startswith(str(TMP)), core.SETTINGS_PATH


# vivid: hue kept, stretched to full saturation; greys become white.
assert core.vivid((0x8B, 0xE9, 0xFD)) == (0, 0xD2, 0xFF)
assert core.vivid((0xD9, 0xA8, 0x62)) == (0xFF, 0x96, 0)
assert core.vivid((0x80, 0x80, 0x80)) == (255, 255, 255)

# led_gamma: mid channels drop, ends stay put.
assert core.led_gamma((0xFF, 0x9C, 0x00)) == (0xFF, 87, 0)
assert core.led_gamma((255, 255, 255)) == (255, 255, 255)

# Portal accent: gdbus output from ReadOne and the older Read; unset = None.
assert core.parse_portal_accent("(<(0.2, 0.5, 1.0)>,)") == (51, 128, 255)
assert core.parse_portal_accent("(<<(0.0, 1.0, 0.0)>>,)") == (0, 255, 0)
assert core.parse_portal_accent("(<(-1.0, -1.0, -1.0)>,)") is None
assert core.parse_portal_accent("Error: NotFound") is None

# Colour command output: hex words in order, anything else ignored.
assert core.parse_command_colours("#7f3f0a\nF5F5F7 junk #12") == [
    (0x7F, 0x3F, 0x0A), (0xF5, 0xF5, 0xF7)]

# desktop_colours: each group from the desktop or the user's own colour.
G = core.led_gamma
cmd = {"theme_sync_source": "command"}
assert core.desktop_colours(cmd) == "No colour command is set."
assert core.desktop_colours({**cmd, "theme_sync_command": "true"}) == \
    "The colour command printed no colour."
assert core.desktop_colours({**cmd, "theme_sync_command": "exit 3"}) == \
    "The colour command printed no colour."
two = {**cmd, "theme_sync_command": "echo '#ff0000' 00ff00"}
# Defaults: chosen keys follow the desktop, other keys use the own colour (white).
assert core.desktop_colours(two) == ((255, 0, 0), (255, 255, 255))
# Other keys from the desktop take the command's second colour.
assert core.desktop_colours({**two, "theme_sync_other_mode": "desktop"}) == \
    ((255, 0, 0), (0, 255, 0))
# Flipped: other keys follow a one-colour desktop, chosen keys use own colour.
one = {**cmd, "theme_sync_command": "echo 8d6019"}
assert core.desktop_colours({**one, "theme_sync_other_mode": "desktop",
                             "theme_sync_chosen_mode": "custom",
                             "theme_sync_chosen": "#0000ff"}) == \
    ((0, 0, 255), G(core.vivid((0x8D, 0x60, 0x19))))
# Neither group follows the desktop: the command is never needed.
assert core.desktop_colours({"theme_sync_chosen_mode": "custom",
                             "theme_sync_chosen": "#ff0000",
                             "theme_sync_other": "#00ff00"}) == ((255, 0, 0), (0, 255, 0))
# Pastel accent is saturated, then gamma-corrected.
assert core.desktop_colours({**cmd, "theme_sync_command": "echo 8d6019"})[0] == (255, 87, 0)

# Portal source with no portal: point gdbus at a session bus that does not exist.
saved = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={TMP}/no-bus"
assert isinstance(core.desktop_colours({}), str)
if saved is None:
    del os.environ["DBUS_SESSION_BUS_ADDRESS"]
else:
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = saved


# theme_key_colours: accent group vs rest, BACKSLASH via calibrated name.
class Key:
    def __init__(self, name, value):
        self.name, self.value = name, value


keys = [Key("LEFT_SHIFT", 4), Key("Q", 8), Key("HASH", 75)]
out = core.theme_key_colours(keys, ["LEFT_SHIFT", "BACKSLASH"], "A", "T", "HASH")
assert out == {4: "A", 8: "T", 75: "A"}, out

# Real keymap: every default key name exists, so none silently drop out.
if not core.IMPORT_ERROR:
    names = {k.name for k in core.keymap_keys()}
    missing = set(core.THEME_SYNC_DEFAULT_KEYS) - names
    assert not missing, missing

# sync_theme does nothing (and never opens the device) while switched off.
assert core.sync_theme() == (0, "Follow desktop colours is off.")

print("ok")
