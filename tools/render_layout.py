"""Render the ANSI layout table to a PNG so the mapping can be eyeballed."""
import sys
from pathlib import Path

import cairo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import epomaker_rt100_gtk as app
from epomakercontroller.configs.configs import Config, ConfigType, load_main_config
from epomakercontroller.utils.keyboard_keys import KeyboardKeys

cfg = load_main_config()
keys = KeyboardKeys(Config(ConfigType.CONF_KEYMAP, cfg["CONF_KEYMAP_PATH"]))
by_name = {k.name: k for k in keys}

U = 52
PAD = 22
W = int(app.LAYOUT_UNITS_W * U) + PAD * 2
H = int(app.LAYOUT_UNITS_H * U) + PAD * 2 + 46

surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
cr = cairo.Context(surface)

cr.set_source_rgb(0.051, 0.047, 0.102)  # cyberpunk $base
cr.paint()

cr.select_font_face("Noto Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
cr.set_font_size(15)
cr.set_source_rgb(0.88, 0.87, 1.0)
cr.move_to(PAD, 26)
cr.show_text("Epomaker RT100 — US ANSI map (label = keycap, small number = firmware LED index)")

TOP = PAD + 40


def rounded(x, y, w, h, r=5):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    cr.arc(x + r, y + r, r, 3.1416, 4.7124)
    cr.close_path()


for name, (x, y, w, h) in app.ANSI_LAYOUT.items():
    px = PAD + x * U
    py = TOP + y * U
    pw = w * U - 3
    ph = h * U - 3

    lookup = app.BACKSLASH_CANDIDATES[0] if name == "BACKSLASH" else name
    key = by_name.get(lookup)
    ambiguous = False  # index 75 confirmed on hardware 2026-07-31

    rounded(px, py, pw, ph)
    if ambiguous:
        cr.set_source_rgb(0.35, 0.10, 0.30)
    else:
        cr.set_source_rgb(0.145, 0.137, 0.243)  # $surface2-ish
    cr.fill_preserve()
    cr.set_source_rgb(0.36, 0.24, 0.62) if not ambiguous else cr.set_source_rgb(0.83, 0.24, 1.0)
    cr.set_line_width(1.4)
    cr.stroke()

    label = app.LABEL_OVERRIDES.get(name)
    if label is None:
        label = (key.display_str if key else name).strip() or name
    if name == "SPACE":
        label = "Space"
    if name == "BACKSLASH":
        # The cap is what is printed on the keyboard, not the display_str of the
        # ISO key sharing its LED index (75, which the ISO map calls "#").
        label = "\\"

    cr.select_font_face("Noto Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    size = 13 if len(label) <= 5 else 10
    cr.set_font_size(size)
    cr.set_source_rgb(0.88, 0.87, 1.0)
    ext = cr.text_extents(label)
    cr.move_to(px + (pw - ext.width) / 2 - ext.x_bearing, py + ph / 2 + 1)
    cr.show_text(label)

    idx = str(key.value) if key else "-"
    cr.set_font_size(8.5)
    cr.set_source_rgb(0.54, 0.53, 0.65)
    ext = cr.text_extents(idx)
    cr.move_to(px + pw - ext.width - 4, py + ph - 4)
    cr.show_text(idx)

out = "/tmp/claude-1000/-mnt-shared-Documents-Claude-Tinker-Place/153b3f18-886b-4a39-b5af-3f6eaeb1472e/scratchpad/rt100-ansi-layout.png"
surface.write_to_png(out)
print(out)
