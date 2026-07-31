"""Terminal front end for the Epomaker RT100.

Same capabilities as the GTK app over the same core: backlight colour, the
firmware's light effects, per-key colours on a navigable ANSI layout, still and
animated screen uploads, and the clock/CPU/temperature service.

Runs anywhere a terminal does — over SSH, in a tty, on a machine with no desktop
stack installed. Nothing here imports a GUI toolkit.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Footer, Header, Input, Label, ListItem, ListView, Log, Select,
    Static, Switch, TabbedContent, TabPane,
)

from . import core

CELL = 4  # terminal columns per keycap unit

# Built at import: Textual rejects an empty option list when allow_blank=False.
EFFECT_OPTIONS = (
    [(m.name.replace("_", " ").title(), m.name) for m in core.Profile.Mode]
    if not core.IMPORT_ERROR else [("unavailable", "none")]
)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.strip().lstrip("#")
    if len(value) != 6:
        raise ValueError("Colour must be six hex digits, e.g. 8a00c4")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


class KeyGrid(Static):
    """The ANSI layout as a navigable grid of keycaps.

    Geometry comes from core.ANSI_LAYOUT, the same table the GTK front end
    draws, so the two never disagree about which key is where.
    """

    DEFAULT_CSS = """
    KeyGrid { height: auto; }
    KeyGrid .cap {
        height: 3; content-align: center middle; border: round $panel-lighten-2;
    }
    KeyGrid .cap.-picked { border: round $accent; text-style: bold; }
    KeyGrid .row { height: 3; }
    """

    def __init__(self, keys, **kwargs) -> None:
        super().__init__(**kwargs)
        self.keys = keys
        self.picked: set[str] = set()
        self.colours: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        rows: dict[float, list[tuple[str, tuple]]] = {}
        for name, box in core.ANSI_LAYOUT.items():
            rows.setdefault(box[1], []).append((name, box))
        for y in sorted(rows):
            cursor = 0.0
            widgets = []
            for name, (x, _y, w, _h) in sorted(rows[y], key=lambda item: item[1][0]):
                if x > cursor:
                    widgets.append(
                        Static("", classes="gap")
                    )  # spacer sized below
                    widgets[-1].styles.width = int((x - cursor) * CELL)
                cap = Button(
                    core.keycap_label(name, self.keys), id=f"cap-{name}",
                    classes="cap", compact=True,
                )
                cap.styles.width = max(3, int(w * CELL))
                widgets.append(cap)
                cursor = x + w
            yield Horizontal(*widgets, classes="row")

    def toggle(self, name: str) -> None:
        button = self.query_one(f"#cap-{name}", Button)
        if name in self.picked:
            self.picked.discard(name)
            button.remove_class("-picked")
        else:
            self.picked.add(name)
            button.add_class("-picked")

    def paint(self, hex_colour: str) -> int:
        for name in self.picked:
            self.colours[name] = hex_colour
            self.query_one(f"#cap-{name}", Button).styles.background = hex_colour
        count = len(self.picked)
        self.clear_selection()
        return count

    def clear_selection(self) -> None:
        for name in list(self.picked):
            self.query_one(f"#cap-{name}", Button).remove_class("-picked")
        self.picked.clear()

    def reset(self) -> None:
        for name in list(self.colours):
            self.query_one(f"#cap-{name}", Button).styles.background = None
        self.colours.clear()
        self.clear_selection()


class Confirm(ModalScreen[bool]):
    """Used before anything that takes the keyboard offline for a while."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical {
        width: 60; height: auto; border: thick $accent; background: $surface;
        padding: 1 2;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self.message)
            with Horizontal():
                yield Button("Go ahead", variant="primary", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def _done(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class RT100App(App):
    TITLE = "Epomaker RT100"
    CSS = """
    #status { height: 3; content-align: center middle; }
    #status.-ok { background: $success 30%; }
    #status.-bad { background: $error 40%; }
    .bar { height: auto; padding: 0 1; }
    Log { height: 8; border: round $panel-lighten-2; }
    Label { padding: 0 1; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "rescan", "Re-scan"),
        ("s", "send_keys", "Send key colours"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.settings = core.load_settings()
        self.interface = int(
            self.settings.get("interface", core.DEFAULT_INTERFACE)
        )
        self.unit = core.UserUnit(core.SERVICE_NAME)
        self.keys = None
        self.ready = False
        self.frames: list = []
        self.sensors = core.list_temp_sensors()

    # ------------------------------------------------------------- layout --

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Looking for the keyboard…", id="status")
        with TabbedContent():
            with TabPane("Backlight", id="tab-backlight"):
                with VerticalScroll():
                    with Horizontal(classes="bar"):
                        yield Label("Colour #")
                        yield Input(value="8a00c4", id="solid", max_length=6)
                        yield Button("All keys", variant="primary", id="apply-solid")
                    with Horizontal(classes="bar"):
                        yield Label("Effect")
                        yield Select(
                            EFFECT_OPTIONS, id="effect", allow_blank=False,
                            value=EFFECT_OPTIONS[0][1],
                        )
                        yield Button("Apply", id="apply-effect")
                    with Horizontal(classes="bar"):
                        yield Label("Brightness (only 3-4 light the keys)")
                        yield Select(
                            [(str(v), v) for v in range(0, 5)],
                            id="brightness", allow_blank=False, value=4,
                        )
                    yield Label(
                        "Click keys to select, then Paint. Nothing is sent "
                        "until you press Send (s).", classes="bar",
                    )
                    yield KeyGrid(None, id="grid")
                    with Horizontal(classes="bar"):
                        yield Label("Key colour #")
                        yield Input(value="00fff9", id="keycolour", max_length=6)
                        yield Button("Paint", id="paint")
                        yield Button("Unselect", id="unselect")
                        yield Button("Reset", id="reset")
                        yield Button("Send", variant="primary", id="send-keys")
                    with Horizontal(classes="bar"):
                        yield Button("Test backslash index", id="test-backslash")
            with TabPane("Screen", id="tab-screen"):
                with VerticalScroll():
                    with Horizontal(classes="bar"):
                        yield Label("Image")
                        yield Input(
                            placeholder="path to a png/jpg/gif…", id="imagepath"
                        )
                        yield Button("Load", id="load-image")
                    yield Label("", id="imageinfo", classes="bar")
                    with Horizontal(classes="bar"):
                        yield Label("Fit")
                        yield Select(
                            [(label, key) for key, label, _ in core.FIT_MODES],
                            id="fit", allow_blank=False,
                            value=self.settings.get("fit_mode", "letterbox"),
                        )
                        yield Label("Animate")
                        yield Switch(value=True, id="animate")
                    with Horizontal(classes="bar"):
                        yield Button("Upload", variant="primary", id="upload")
            with TabPane("System", id="tab-system"):
                with VerticalScroll():
                    with Horizontal(classes="bar"):
                        yield Button("Sync clock", variant="primary", id="synctime")
                        yield Button("Send CPU + temp once", id="sendstats")
                    with Horizontal(classes="bar"):
                        yield Label("Sensor")
                        yield Select(
                            [(label, key) for key, label in self.sensors] or
                            [("no sensors", "none")],
                            id="sensor", allow_blank=False,
                        )
                    with Horizontal(classes="bar"):
                        yield Label("Screen updater running")
                        yield Switch(value=False, id="service")
                    yield Label("", id="serviceinfo", classes="bar")
        yield Log(id="log", highlight=True)
        yield Footer()

    # -------------------------------------------------------------- start --

    def on_mount(self) -> None:
        if core.IMPORT_ERROR:
            self.log_line(f"EpomakerController not importable: {core.IMPORT_ERROR}")
            return
        preferred = self.settings.get("temp_sensor", "coretemp-0")
        if any(key == preferred for key, _ in self.sensors):
            self.query_one("#sensor", Select).value = preferred
        try:
            self.keys = core.keymap_keys()
            self.query_one("#grid", KeyGrid).keys = self.keys
        except Exception as exc:
            self.log_line(f"Could not load the keymap: {exc}")
        self.action_rescan()
        self.set_interval(4, self.action_rescan)
        self.set_interval(3, self.refresh_service)
        self.refresh_service()

    def log_line(self, text: str) -> None:
        self.query_one("#log", Log).write_line(text)

    # ------------------------------------------------------------- device --

    def action_rescan(self) -> None:
        status = self.query_one("#status", Static)
        try:
            state = core.scan_bus()
        except Exception as exc:
            status.update(f"Could not scan USB: {exc}")
            return
        status.remove_class("-ok", "-bad")
        if not state["wired"]:
            self.ready = False
            status.add_class("-bad")
            status.update(
                "Only the 2.4 GHz dongle is connected — this app needs the USB cable."
                if state["wireless"]
                else "Keyboard not connected. Plug in its USB-C cable."
            )
            return
        interfaces = state["interfaces"]
        if self.interface not in interfaces:
            self.ready = False
            status.add_class("-bad")
            status.update(
                f"Interface {self.interface} not present (found "
                f"{', '.join(map(str, interfaces))})."
            )
            return
        self.ready = True
        status.add_class("-ok")
        status.update(
            f"RT100 connected — interfaces {', '.join(map(str, interfaces))}, "
            f"using {self.interface}."
        )

    @work(thread=True, exclusive=True)
    def run_on_device(self, label: str, work_fn) -> None:
        """Open the device off the UI thread, run, and always close it."""
        if not self.ready:
            self.call_from_thread(self.log_line, "Keyboard is not available.")
            return
        device = None
        guard = core.DaemonGuard()
        try:
            note = guard.stop()
            if note:
                self.call_from_thread(self.log_line, note)
            device = core.RT100(core.load_main_config(), interface=self.interface)
            device.open_device()
            message = work_fn(device)
            self.call_from_thread(self.log_line, message)
        except core.DevicePermission as exc:
            self.call_from_thread(self.log_line, f"{core.UDEV_FIX}\n({exc})")
        except Exception as exc:
            self.call_from_thread(
                self.log_line, f"{label} failed — {type(exc).__name__}: {exc}"
            )
        finally:
            if device is not None:
                try:
                    device.close_device()
                except Exception:
                    pass
            try:
                note = guard.restart()
                if note:
                    self.call_from_thread(self.log_line, note)
            except Exception:
                pass
            self.call_from_thread(self.refresh_service)

    # ------------------------------------------------------------ actions --

    @on(Button.Pressed, "#apply-solid")
    def _solid(self) -> None:
        try:
            rgb = _hex_to_rgb(self.query_one("#solid", Input).value)
        except ValueError as exc:
            self.log_line(str(exc))
            return
        self.run_on_device(
            "Solid colour",
            lambda d: (d.apply_solid(rgb), f"All keys set to rgb{rgb}.")[1],
        )

    @on(Button.Pressed, "#apply-effect")
    def _effect(self) -> None:
        mode = core.Profile.Mode[self.query_one("#effect", Select).value]
        brightness = min(
            core.Profile.Brightness,
            key=lambda m: abs(m.value - int(self.query_one("#brightness", Select).value)),
        )
        try:
            rgb = _hex_to_rgb(self.query_one("#solid", Input).value)
        except ValueError:
            rgb = (180, 180, 180)

        def apply(device):
            device.set_profile(core.Profile(
                mode=mode, speed=core.Profile.Speed.DEFAULT, brightness=brightness,
                dazzle=core.Profile.Dazzle.OFF, option=core.Profile.Option.DEFAULT,
                rgb=rgb,
            ))
            return f"Effect set to {mode.name.replace('_', ' ').title()}."

        self.run_on_device("Effect", apply)

    @on(Button.Pressed, ".cap")
    def _cap(self, event: Button.Pressed) -> None:
        name = (event.button.id or "")[4:]
        if name:
            self.query_one("#grid", KeyGrid).toggle(name)

    @on(Button.Pressed, "#paint")
    def _paint(self) -> None:
        grid = self.query_one("#grid", KeyGrid)
        try:
            colour = "#" + self.query_one("#keycolour", Input).value.lstrip("#")
            _hex_to_rgb(colour)
        except ValueError as exc:
            self.log_line(str(exc))
            return
        if not grid.picked:
            self.log_line("Select some keys first.")
            return
        self.log_line(f"Painted {grid.paint(colour)} keys — press Send to apply.")

    @on(Button.Pressed, "#unselect")
    def _unselect(self) -> None:
        self.query_one("#grid", KeyGrid).clear_selection()

    @on(Button.Pressed, "#reset")
    def _reset(self) -> None:
        self.query_one("#grid", KeyGrid).reset()

    @on(Button.Pressed, "#send-keys")
    def action_send_keys(self) -> None:
        grid = self.query_one("#grid", KeyGrid)
        wanted: dict[int, tuple[int, int, int]] = {}
        for name, colour in grid.colours.items():
            lookup = core.BACKSLASH_CANDIDATES[0] if name == "BACKSLASH" else name
            key = self.keys.get_key_by_name(lookup) if self.keys else None
            if key:
                wanted[key.value] = _hex_to_rgb(colour)
        self.run_on_device(
            "Per-key colours",
            lambda d: (d.apply_key_colours(wanted),
                       f"Sent {len(wanted)} coloured keys.")[1],
        )

    @on(Button.Pressed, "#test-backslash")
    def _test_backslash(self) -> None:
        order = core.BACKSLASH_CANDIDATES
        current = self.settings.get("backslash_index_name", order[0])
        nxt = order[(order.index(current) + 1) % len(order)] if current in order else order[0]
        self.settings["backslash_index_name"] = nxt
        core.save_settings(self.settings)
        key = self.keys.get_key_by_name(nxt) if self.keys else None
        index = key.value if key else None
        self.run_on_device(
            "Backslash test",
            lambda d: (d.light_one_index(index),
                       f"Lit LED index {index} ({nxt}). If that is your "
                       f"backslash key, keep it.")[1],
        )

    # ------------------------------------------------------------- screen --

    @on(Button.Pressed, "#load-image")
    def _load(self) -> None:
        path = Path(self.query_one("#imagepath", Input).value).expanduser()
        if not path.is_file():
            self.log_line(f"No such file: {path}")
            return
        try:
            self.frames = core.load_frames(str(path))
        except Exception as exc:
            self.log_line(f"Could not read that image — {exc}")
            return
        count = len(self.frames)
        animated = count > 1
        self.query_one("#animate", Switch).disabled = not animated
        sent = min(count, core.GIF_MAX_FRAMES)
        self.query_one("#imageinfo", Label).update(
            f"{path.name}: {count} frames, {self.frames[0].size[0]}x"
            f"{self.frames[0].size[1]}. "
            + (
                f"Animation sends {sent} at {core.GIF_FRAMERATE} fps, "
                f"{core.GIF_DIMENSIONS[0]}x{core.GIF_DIMENSIONS[1]}."
                if animated else
                f"Still upload at {core.IMAGE_DIMENSIONS[0]}x"
                f"{core.IMAGE_DIMENSIONS[1]}."
            )
        )

    @on(Button.Pressed, "#upload")
    def _upload(self) -> None:
        if not self.frames:
            self.log_line("Load an image first.")
            return
        animate = (
            len(self.frames) > 1 and self.query_one("#animate", Switch).value
        )
        mode = self.query_one("#fit", Select).value
        frames = list(self.frames)

        def go(confirmed: bool) -> None:
            if not confirmed:
                return

            def upload(device):
                if animate:
                    handle, temp = tempfile.mkstemp(prefix="rt100-", suffix=".gif")
                    os.close(handle)
                    try:
                        count = core.render_gif(frames, mode, (0, 0, 0), temp)
                        device.send_gif_paced(temp)
                        return f"Animation uploaded — {count} frames."
                    finally:
                        try:
                            os.unlink(temp)
                        except OSError:
                            pass
                device.upload_still(core.fit_image(frames[0], mode, (0, 0, 0)))
                return "Image uploaded."

            self.run_on_device("Upload", upload)

        self.push_screen(
            Confirm(
                "The upload sends about a thousand packets and takes a few "
                "seconds.\nThe keyboard is unresponsive while it runs — do not "
                "unplug it."
            ),
            go,
        )

    # ------------------------------------------------------------- system --

    @on(Button.Pressed, "#synctime")
    def _synctime(self) -> None:
        self.run_on_device(
            "Clock", lambda d: (d.send_time(), "Keyboard clock set.")[1]
        )

    @on(Button.Pressed, "#sendstats")
    def _sendstats(self) -> None:
        sensor = self.query_one("#sensor", Select).value

        def send(device):
            cpu = core.cpu_percent()
            device.send_cpu(cpu)
            temp = core.read_sensor(sensor) if sensor != "none" else None
            if temp is not None:
                device.send_temperature(int(temp))
                return f"Sent CPU {cpu}% and {temp:.0f}°C."
            return f"Sent CPU {cpu}%."

        self.run_on_device("System stats", send)

    @on(Select.Changed, "#sensor")
    def _sensor_changed(self, event: Select.Changed) -> None:
        self.settings["temp_sensor"] = event.value
        core.save_settings(self.settings)

    @on(Switch.Changed, "#service")
    def _service_toggle(self, event: Switch.Changed) -> None:
        if getattr(self, "_suppress", False):
            return
        error = self.unit.start() if event.value else self.unit.stop()
        if error:
            self.log_line(f"Service: {error}")
        self.set_timer(1.0, self.refresh_service)

    def refresh_service(self) -> None:
        switch = self.query_one("#service", Switch)
        installed = self.unit.exists()
        active = installed and self.unit.is_active()
        self._suppress = True
        switch.value = active
        switch.disabled = not installed
        self._suppress = False
        self.query_one("#serviceinfo", Label).update(
            ("Running — clock, CPU and temperature are being refreshed."
             if active else "Stopped.")
            if installed else
            "Not installed. Run ./install-service.sh in the repo."
        )


def main() -> int:
    RT100App().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
