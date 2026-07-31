"""Generate the screen calibration targets in docs/calibration/.

Sizes come from the app itself, so the targets always match what it actually
uploads: IMAGE_DIMENSIONS for stills, GIF_DIMENSIONS for animation. Upload one
with "Stretch to fit" so nothing is re-fitted, then photograph the screen -- the
edge colours and corner blocks show exactly how the firmware places the frame.

    red = top, blue = bottom, green = left, yellow = right

Run:  .venv/bin/python tools/make_calibration.py
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.argv = ["make_calibration"]
import epomaker_rt100_gtk as app  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "calibration"


def draw(size: tuple[int, int], grid: int, corner: int, dot: tuple[int, int, int] | None):
    width, height = size
    image = Image.new("RGB", size, (0, 0, 0))
    d = ImageDraw.Draw(image)
    for x in range(0, width, grid):
        d.line([(x, 0), (x, height)], fill=(45, 45, 45))
    for y in range(0, height, grid):
        d.line([(0, y), (width, y)], fill=(45, 45, 45))
    edge = max(2, grid // 4)
    d.rectangle([0, 0, width - 1, edge - 1], fill=(255, 0, 0))
    d.rectangle([0, height - edge, width - 1, height - 1], fill=(0, 120, 255))
    d.rectangle([0, 0, edge - 1, height - 1], fill=(0, 255, 0))
    d.rectangle([width - edge, 0, width - 1, height - 1], fill=(255, 255, 0))
    for x, y, colour in [
        (0, 0, (255, 255, 255)), (width - corner, 0, (255, 0, 255)),
        (0, height - corner, (0, 255, 255)),
        (width - corner, height - corner, (255, 140, 0)),
    ]:
        d.rectangle([x, y, x + corner - 1, y + corner - 1], fill=colour)
    d.line([(width // 2, 0), (width // 2, height)], fill=(200, 200, 200))
    d.line([(0, height // 2), (width, height // 2)], fill=(200, 200, 200))
    if dot:
        r = min(width, height) // 8
        d.ellipse([width // 2 - r, height // 2 - r, width // 2 + r, height // 2 + r],
                  fill=dot)
    else:
        r = min(width, height) // 12
        d.ellipse([width // 2 - r, height // 2 - r, width // 2 + r, height // 2 + r],
                  outline=(255, 255, 255), width=2)
    return image


OUT.mkdir(parents=True, exist_ok=True)

still = OUT / "rt100-calibration.png"
draw(app.IMAGE_DIMENSIONS, 10, 20, None).save(still)
print(f"{still}  {app.IMAGE_DIMENSIONS[0]}x{app.IMAGE_DIMENSIONS[1]}  (still)")

# Two frames with a blinking centre, so a stuck still is obvious.
animated = OUT / "rt100-gif-calibration.gif"
frames = [draw(app.GIF_DIMENSIONS, 16, 24, c) for c in ((255, 255, 255), (255, 0, 0))]
frames[0].save(animated, save_all=True, append_images=frames[1:], duration=400, loop=0)
w, h = app.GIF_DIMENSIONS
print(f"{animated}  {w}x{h}  (animated, {100 * w * h / (162 * 173):.1f}% of panel)")
