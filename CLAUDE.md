# CLAUDE.md — epomaker-rt100-gtk

Guidance for Claude Code working in this repository.

This is a **portable, publishable project**. Write and reason about it as if it
runs on any Linux desktop — do not assume anything about a particular machine's
configuration. Launch Claude from inside this directory when you want it
isolated from surrounding context.

## What this is

A GTK4 / libadwaita GUI wrapping the `EpomakerController` PyPI package to
control an Epomaker RT100 keyboard over USB. One window, one process, one
Python file (`epomaker_rt100_gtk.py`).

## Hard rules

1. **Never invoke `sudo`, `pkexec`, or any privilege escalation from the app.**
   Permission problems get surfaced to the user with the fix printed. The
   library's own `generate_udev_rule()` shells out to sudo — do not call it.
2. **No network access and no telemetry.** Ever.
3. **USB wired only.** The library cannot drive Bluetooth or the 2.4 GHz dongle.
   Do not add UI that implies otherwise.
4. **Never guess hardware facts.** Every constant about the keyboard — LED
   indices, screen dimensions, light modes, packet sizes — must be read from the
   library source at runtime or cited to a specific file and line. If a fact is
   not recorded anywhere, say so and give the user a way to determine it
   empirically, the way the backslash-index test button does. Do not silently
   pick a plausible value.
5. **All HID I/O happens off the GTK main thread**, via `Window.run_on_device`,
   with `GLib.idle_add` for anything touching widgets. The device is always
   closed in a `finally` block.

## Where facts come from

Read these from the installed package rather than from its README or CLI help,
which are both out of date relative to the code:

| Fact | Source |
|---|---|
| LED index per key | `configs/keymaps/EpomakerRT100.json` (99 keys, sparse values 0–101) |
| Screen size | `commands/data/constants.py` → `IMAGE_DIMENSIONS = (162, 173)`, a cv2 dsize so *(w, h)* |
| Light effects | `commands/data/constants.py` → `Profile.Mode` (19 members) |
| Packet size | `commands/data/constants.py` → `BUFF_LENGTH = 64` |
| Device selection | `epomakercontroller.py` → `_find_device_path` / `_select_device_path` |
| Daemon stop/start pattern | the library repo's `service/epomaker-upload-image` |

## Library quirks this code works around

Do not "fix" these by removing the workarounds:

- **No interface argument exists.** `_find_device_path` matches
  `"ROYUAN .* System Control"` against `/sys/class/input/*/device/name` and takes
  the first Wired hit. `RT100._find_device_path` overrides it with a direct
  `hid.enumerate()` filter on `interface_number` so the choice is explicit.
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
- **`hidapi==0.14.0` is pinned and does not build on Python 3.13+.** Install with
  `--no-deps` plus a distro `python-hidapi` or `hidapi==0.15.0`.

## Style

- One file. If it must be split, split by layer (device / fitting / UI), not by
  widget.
- Comments explain *why*, especially where the code deviates from the library.
  Do not annotate the obvious.
- User-facing strings describe outcomes in plain language, not mechanisms:
  "Show the whole image", not "letterbox mode". Technical detail belongs in
  tooltips and the README.
- No new runtime dependencies. Image work uses GdkPixbuf, which arrives with
  GTK, rather than Pillow.

## Testing without a keyboard

`ANSI_LAYOUT` coverage, geometry overlap, image fitting and both command
builders can all be exercised with no hardware attached — the command classes are
pure. `tools/render_layout.py` renders the layout to a PNG for eyeballing.
Anything that opens the device needs the keyboard and the udev rule.
