# epomaker-rt100-gtk

A small GTK4 / libadwaita desktop app to control an **Epomaker RT100** keyboard
on Linux: backlight colours, the firmware's built-in light effects, per-key
colours from a clickable layout, and a still image on the little screen.

Single window, single process, one Python file. No telemetry, no network
access, and it never invokes `sudo`.

It is a **wrapper** around [`EpomakerController`](https://github.com/strodgers/epomaker-controller)
by Sam Rodgers, which does all the actual USB HID work. This project adds a
native desktop UI, a US ANSI layout, and the operational glue (interface
selection, daemon handling, image fitting) that the library leaves to callers.

![US ANSI key map](docs/ansi-layout.png)

## Features

**Backlight**
- One solid colour across the whole board.
- All 19 built-in firmware effects, with speed, brightness, dazzle and
  direction, plus a "next effect" button to step through them.
- Per-key colours from a clickable layout. Select any number of keys, pick a
  colour, then send. Selections and colours persist between runs.

**Screen**
- File picker with a live, actual-size preview.
- Three fitting choices — show the whole image (letterboxed), fill and crop, or
  stretch — with a configurable bar colour.
- GIFs are accepted and sent as a **single chosen frame**, with a frame picker.
  See [GIFs and animation](#gifs-and-animation) — the screen cannot be animated
  from Linux.
- Progress by packet during upload.
- Automatically re-sends the picture after a backlight change, because writing
  key colours clears the screen. Switchable.

## Launcher entry

```bash
./install-desktop.sh          # writes ~/.local/share/applications, no root
./install-desktop.sh --uninstall
```

Then type "Epomaker" or "RT100" into whatever already opens your launcher. This
is a user-level XDG desktop entry, so **no keybind or compositor configuration
is needed** — any launcher that reads the standard directories finds it (wofi,
rofi, fuzzel, GNOME, Plasma).

## Requirements

- Linux, Wayland or X11.
- Python 3.10+, PyGObject, GTK 4.10+ and libadwaita 1.4+.
- An RT100 connected **over USB with its cable**. Bluetooth and the 2.4 GHz
  dongle are not supported — that is a limitation of the underlying library,
  not of this app. If only the dongle is plugged in, the app says so.

## Install

### Arch / CachyOS

The library pins `hidapi==0.14.0`, which **cannot build on Python 3.13+**
(`error: unknown file type '.pxd' (from 'chid.pxd')` under modern setuptools).
Use the distro binding instead of building it:

```bash
sudo pacman -S --needed python-gobject gtk4 libadwaita python-hidapi

git clone <this repo> epomaker-rt100-gtk && cd epomaker-rt100-gtk
python -m venv --system-site-packages .venv
.venv/bin/pip install --no-deps EpomakerController
.venv/bin/pip install appdirs click gpustat numpy opencv-python-headless \
    psutil python-dateutil
```

`--system-site-packages` is what lets the venv see PyGObject and
`python-hidapi`. `--no-deps` skips the unbuildable pin. If you would rather not
use the distro package, `pip install hidapi==0.15.0` also works — it builds
cleanly and behaves identically here.

### Debian / Ubuntu

```bash
sudo apt install -y python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
    gir1.2-adw-1 libhidapi-libusb0 libusb-1.0-0-dev
python3 -m venv --system-site-packages .venv
.venv/bin/pip install --no-deps EpomakerController
.venv/bin/pip install appdirs click gpustat numpy opencv-python-headless \
    psutil python-dateutil hidapi
```

### Permissions (required)

`import hid` resolves to hidapi's **libusb** backend, which talks to
`/dev/bus/usb/...`. That node is root-only by default, and the ACLs
systemd-logind puts on `/dev/hidraw*` do **not** help because the libusb
backend never opens those.

```bash
sudo install -m644 udev/70-epomaker-rt100.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
# then unplug and replug the keyboard
```

Two deliberate choices in that rule:

- **The filename must sort before `73-seat-late.rules`.** That is the rule which
  turns `TAG=="uaccess"` into an actual ACL. A file named `99-…` sets the tag
  too late and is silently ignored. Hence `70-`.
- **`TAG+="uaccess"`, not `MODE="0666" GROUP="plugdev"`.** uaccess grants only
  the user at the active seat. `0666` would make the keyboard world-writable,
  and `plugdev` does not exist as a group on Arch. The library's own
  `epomakercontroller dev --udev` writes the `0666`/`plugdev` form; this rule is
  the systemd-standard equivalent.

The app never escalates privileges. If a permission error comes back it shows
these commands rather than trying to run them.

## Run

```bash
.venv/bin/python epomaker_rt100_gtk.py
```

## Things worth knowing

**Three HID interfaces.** The RT100 exposes interfaces 0, 1 and 2. Interface 0
carries key input and using it interferes with normal typing. The app defaults
to **interface 1** and the choice is in the header bar.

The library has no interface argument — v0.0.8 finds a path by matching
`DEVICE_DESCRIPTION_REGEX` (`"ROYUAN .* System Control"`) against
`/sys/class/input/*/device/name`, filtering on Wired/Wireless and taking the
first hit. That lands on interface 1 in practice, but it is implicit and
unselectable, so `RT100._find_device_path` here replaces it with a direct
`hid.enumerate()` filter on `interface_number`.

**Screen size is 162 × 173.** Read from the library's
`commands/data/constants.py` (`IMAGE_DIMENSIONS`), not guessed. That value is a
`cv2.resize` dsize, so it reads *(width, height)*. The library's `encode_image`
resizes with no aspect handling at all, which is why the fitting happens in this
app before the file is handed over.

**The CPU/temp daemon holds the device.** It holds it for *any* operation, not
just uploads, so the app stops it around every one of them: it checks
`systemctl --user is-active` then `systemctl is-active` for
`epomaker-controller.service`, stops it, and restarts it in a `finally` block —
the same approach as the library repo's `service/epomaker-upload-image` helper.
Override the unit name with `EPOMAKER_SERVICE_NAME`. User units are tried first
because stopping those needs no authorisation.

**Packet pacing.** The firmware erases SRAM on the init report and needs a pause
before data arrives; the endpoint buffer can also overflow if reports arrive too
fast, historically corrupting the high-index right-hand keys. This app waits
250 ms after the init report, and paces key-frame packets by 10 ms (only 8
packets, so it costs nothing). The 1002-packet image upload keeps upstream's
back-to-back timing, since that path is known to work as-is.

**Signal handling is disabled.** `EpomakerController.__init__` installs
SIGINT/SIGTERM handlers that call `os._exit(0)`, which fights GTK and restricts
construction to the main thread. `RT100._setup_signal_handling` is a no-op and
the device is closed in a `finally` block instead.

## GIFs and animation

**The screen cannot be animated from Linux.** The RT100 firmware supports it —
Epomaker's own Windows/Mac software does it via Screen → Select Picture — but the
protocol for it has not been publicly reverse-engineered:

- `EpomakerController` lists "Upload GIFs" under **TODO**, and its
  `SUPPORTED_FORMATS` excludes `.gif` entirely.
- `tejmar/epomaker-controller` does have GIF import, but gated behind its
  `dynatab_screen` capability — the DynaTab's 60×9 dot-matrix. The RT100 gets
  `rt100_screen`, documented as "162×173 status-screen **image** upload".

So this app accepts a `.gif`, extracts its frames, and lets you pick **one** to
send as a still. The file never reaches the library — only the PNG rendered from
your chosen frame does. Frame extraction uses Pillow when it is installed;
without it, GdkPixbuf still returns the first frame, so GIFs stay usable.

Uploading frames in sequence is not a workaround: each upload is 1002 packets and
makes the keyboard unresponsive while it runs. Real animation would mean
capturing USB traffic from the Windows software during a GIF upload and decoding
the command it uses.

## Observed hardware behaviour

Measured on an RT100, 2026-07-30. Neither of these is documented upstream:

- **Backlight writes clear the screen.** Setting key colours issues the `0x18`
  "erase key SRAM" init report, and the screen's content buffer goes with it —
  the screen blanks until something redraws it. The app therefore remembers your
  last uploaded picture and re-sends it after any backlight change ("Keep this
  picture after backlight changes", on by default). Turn it off if you would
  rather backlight changes stay instant.
- **Brightness below 3 leaves the LEDs off.** The firmware range is 0–4, but only
  3 and 4 actually light the keys. The full range is still exposed because 0 is
  the only way to switch the backlight off outright; the UI says as much so the
  low steps do not look broken.

## Known unknowns

Two mappings are genuinely not recorded in the library, which ships a **UK ISO**
layout only. They are not guessed here:

- **The backslash key.** On UK ISO, index 10 (`BACKSLASH`) is the key between
  Left Shift and Z, and index 75 (`HASH`) is the key left of Enter. A US ANSI
  board has neither switch — its backslash sits at the end of the QWERTY row.
  Which LED index the firmware gives it is unknown, so the app has a **"Test
  next candidate"** button that lights one candidate at a time (10, 75, 80).
  Keep whichever matches your key; the choice is saved. It is drawn highlighted
  with a `?` until you confirm it.
- **Which Ctrl is which.** The library's ISO layout lists `RIGHT_CTRL` at the
  far left of the bottom row and `LEFT_CTRL` at the right. That looks like an
  upstream mix-up, but it is the only record, and LED indices are not geometric
  (index 11 is the DIAL, which sits top-right), so it cannot be derived. Both
  caps read "Ctrl", so it only matters if the wrong one lights — swap the two
  names in `ANSI_LAYOUT` if so.

Per-key colours can be overridden by an active firmware effect. If a colour does
not appear, set the effect to **Always On** first.

## Adding another layout

`ANSI_LAYOUT` maps a key name to `(x, y, width, height)` in keycap units. Key
*names* and LED indices are read from the library's keymap at runtime, so a new
layout only needs geometry. `tools/render_layout.py` draws the table to a PNG so
you can check it:

```bash
.venv/bin/python tools/render_layout.py
```

## Prior art

- [`strodgers/epomaker-controller`](https://github.com/strodgers/epomaker-controller)
  — the library this wraps. Ships a Tkinter per-key GUI, UK ISO only.
- [`tejmar/epomaker-controller`](https://github.com/tejmar/epomaker-controller)
  — a separate re-upload of that package with a much larger Tkinter GUI,
  including a DynaTab 60×9 screen designer and GIF import. Covers a lot of the
  same ground if you do not care about a native desktop look.
- [`zuev-stepan/rt100-wireless-display`](https://github.com/zuev-stepan/rt100-wireless-display)
  — drives the RT100 screen over the 2.4 GHz dongle, which the library cannot do.

## Licence

MIT, matching the library it wraps. See `LICENSE`.
