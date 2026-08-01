# CLAUDE.md — epomaker-rt100

Guidance for Claude Code working in this repository.

This is a **portable, publishable project**. Write and reason about it as if it
runs on any Linux desktop — do not assume anything about a particular machine's
configuration. Launch Claude from inside this directory when you want it
isolated from surrounding context.

## What this is

Control for the Epomaker RT100 keyboard over USB, wrapping the
`EpomakerController` library.

Layout:

| File | Role |
|---|---|
| `epomaker_rt100/core.py` | device, imaging, sensors, systemd, headless daemon. **Imports no UI toolkit.** |
| `epomaker_rt100/tui.py` | Textual front end |
| `epomaker_rt100/daemon.py` | `epomaker-rt100-daemon`, the screen updater service |
| `epomaker_rt100_gtk.py` | GTK4/libadwaita front end. Predates `core` and still carries its own copies of `RT100`, `UserUnit` and `DaemonGuard` — porting it onto `core` is outstanding work. |
| `packaging/` | PKGBUILDs for the two AUR packages |

Front ends must stay interchangeable: anything about the hardware belongs in
`core`, not in a front end. `ANSI_LAYOUT` in particular is shared, so the two
can never disagree about which key is where.

## Hard rules

1. **Never invoke `sudo`, `pkexec`, or any privilege escalation from the app.**
   Permission problems get surfaced to the user with the fix printed. The
   library's own `generate_udev_rule()` shells out to sudo — do not call it.
2. **No network access and no telemetry.** Ever.
3. **USB wired only.** 0.0.9 added a 2.4 GHz path, but its handshake does not
   complete on this hardware: the dongle answers `00015d0000010101…` where
   `send_wireless_init()` looks for `01010168`, so it returns False and the
   device never opens. Tested on hardware, not assumed. Bluetooth is not
   implemented at all. Do not add UI implying either works.
4. **Never guess hardware facts.** Every constant about the keyboard — LED
   indices, screen dimensions, light modes, packet sizes — must be read from the
   library source at runtime or cited to a specific file and line. If a fact is
   not recorded anywhere, say so and give the user a way to determine it
   empirically, the way the backslash-index test button does. Do not silently
   pick a plausible value.
5. **All HID I/O happens off the UI thread** — `Window.run_on_device` in GTK
   (with `GLib.idle_add` for widgets), `@work(thread=True)` in the TUI. The
   device is always closed in a `finally` block.

## Where facts come from

Read these from the installed package rather than from its README or CLI help,
which are both out of date relative to the code:

| Fact | Source |
|---|---|
| LED index per key | `configs/keymaps/EpomakerRT100.json` (99 keys, sparse values 0–101) |
| Screen size | `commands/data/constants.py` → `IMAGE_DIMENSIONS = (162, 173)`, a cv2 dsize so *(w, h)* |
| Light effects | `commands/data/constants.py` → `Profile.Mode` (19 members) |
| Packet size | `commands/data/constants.py` → `BUFF_LENGTH = 64` |
| Device selection | `epomakercontroller.py` → `_open_device`. 0.0.9 removed `_find_device_path`. |
| Daemon stop/start pattern | the library repo's `service/epomaker-upload-image` |

## Library quirks this code works around

Do not "fix" these by removing the workarounds:

- **No interface argument exists, and 0.0.9 made this worse.** It opens with
  `hid.device().open(vendor_id, product_id)`, taking whatever enumerates first —
  interface 0, the one carrying key input. `RT100._open_device` replaces it with
  a path-based open filtered on `interface_number`. Reported as upstream #94.
- **`_open_device` swallows IOError, prints a sudo hint, then trips a bare
  `assert`** — so a permission failure arrives as `AssertionError`.
  `RT100._open_device` overrides it to raise `DevicePermission`.
- **`__init__` installs SIGINT/SIGTERM handlers calling `os._exit(0)`.**
  `RT100._setup_signal_handling` is a deliberate no-op.
- **`open_device()` raises `ValueError` when no device is found**, rather than
  returning `False` as its docstring claims.
- **`cycle_light_modes()` blocks for 19 × 5 seconds** in `time.sleep`. Never call
  it from the GUI; use `set_profile()` per effect.
- **`send_image` / `send_keys` give no progress and no pacing.** `send_paced`
  reimplements `_send_command` with an erase delay, packet pacing and a callback.
- **Working-directory-relative paths.** `PATH_TO_DEFAULT_CONFIG`,
  `CONFIG_DIRECTORY` and `TMP_FOLDER` are all relative, and `constants.py` runs
  `os.mkdir` at import time. `_stabilise_library_paths()` handles it. Upstream #93.
- **`best_gif_dimensions()` floors a wide source's short axis to zero**
  (`800x200 -> (128, 0)`), which passes its own `% 4096` check. Patched in
  `core.patch_gif_dimensions()`. Upstream #92.
- **`import gpustat` and `import cv2` at module scope.** Both only serve GPU
  temperatures and five image calls, but their absence takes down the whole
  library. `_stub_gpustat()` and `_stub_cv2()` supply them from Pillow and numpy,
  verified byte-identical against real OpenCV. This is what keeps the package at
  1.3 MiB instead of 436 MiB — do not "simplify" it away.
- **0.0.8 pinned `hidapi==0.14.0`**, which will not build on Python 3.13+. 0.0.9
  relaxed it, which is one reason the pinned commit is used rather than PyPI.

## Hardware behaviour established by testing

Measured on a real RT100, 2026-07-30. Do not "simplify" the code that handles
these — they are not documented anywhere upstream:

- **A key-RGB write clears the screen.** The `0x18` erase-key-SRAM init report
  takes the screen's content buffer with it. Hence `run_on_device(...,
  restore_screen=True)` on every backlight path and the remembered `last_image`.
- **Brightness 0–2 leaves the LEDs dark**; only 3 and 4 light the keys. The range
  stays 0–4 because 0 is the only backlight-off setting.
- **The daemon holds the device for every operation**, not just uploads, so
  `DaemonGuard` wraps all of `run_on_device` rather than just the upload.
- **Animated GIFs DO work**, via `EpomakerGifCommand`, added in upstream 0.0.9
  and not present on PyPI. Limits from its code: 56 frames, 15 fps, and 128x160
  — the largest frame satisfying `w*h*2 % 4096 == 0` within the 162x173 panel.
  The firmware places animation frames at roughly 1:1, so a smaller frame simply
  covers less screen; 73.1% is the ceiling.
- **Interface 2 is the only one safe to hold.** Interface 1 carries Consumer
  Control — holding it kills the volume knob, dropping the keyboard from six
  input nodes to one. Interface 0 carries typing. See the comment on
  `DEFAULT_INTERFACE`.
- **Screen-field writes must be spaced by ~1.6s.** Sending CPU and temperature
  back-to-back and waiting once per cycle is not equivalent: the firmware drops
  the second value while still reporting success. `DAEMON_SEND_SPACING`.
- **The US ANSI backslash is LED index 75**, which the ISO keymap calls `HASH`.
  Confirmed on hardware.

## Style

- Split by layer (device / imaging / UI), never by widget. `core` must stay
  free of any UI toolkit — that property is what makes a second front end
  cheap, and it is worth protecting.
- Comments explain *why*, especially where the code deviates from the library.
  Do not annotate the obvious.
- User-facing strings describe outcomes in plain language, not mechanisms:
  "Show the whole image", not "letterbox mode". Technical detail belongs in
  tooltips and the README.
- No new runtime dependencies, and prefer ones already present. Image work uses
  **Pillow**, not GdkPixbuf: Pillow is already required for GIF handling and
  works without a display, which is what lets the daemon and TUI run headless.

## Testing without a keyboard

`ANSI_LAYOUT` coverage, geometry overlap, image fitting and both command
builders can all be exercised with no hardware attached — the command classes are
pure. `tools/render_layout.py` renders the layout to a PNG for eyeballing, and
`tools/make_calibration.py` writes the screen calibration targets.

**Test packaging in a clean chroot** (`extra-x86_64-build`), not on a machine
that already has the dependencies. Every packaging bug this project has shipped —
a missing `textual`, a missing `gpustat`, a unit file looked for in the wrong
directory — passed local testing and failed on a real install.

Anything that opens the device needs the keyboard and the udev rule. A command
returning without error is **not** evidence it worked: the firmware silently
ignores plenty. Confirm on the hardware, or say it is unverified.
