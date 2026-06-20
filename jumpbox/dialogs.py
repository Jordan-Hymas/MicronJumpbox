"""Modal dialogs for adding/removing locations, rooms, and hosts.

Each "⋮" button in the dashboard opens an ActionMenu, which routes to either
a confirmation dialog (for deletes) or a small add-form. All of them are
pushed with `push_screen_wait` and resolve to a plain value (or None if the
user cancelled), so the caller in app.py just awaits a result.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from .data import Host, Location, Room, Status


class ActionMenu(ModalScreen[str | None]):
    """A tiny menu of (action_id, label) choices, opened from a "⋮" button."""

    BINDINGS = [Binding("escape", "dismiss_menu", "Cancel", show=False)]

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-box", id="menu-box"):
            yield Static(self._title, classes="dialog-title")
            for action_id, label in self._options:
                yield Button(label, id=f"opt-{action_id}", classes="menu-option")
            yield Button("Cancel", id="opt-cancel", classes="menu-option")

    def action_dismiss_menu(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _on_option(self, event: Button.Pressed) -> None:
        option_id = (event.button.id or "")[len("opt-"):]
        self.dismiss(None if option_id == "cancel" else option_id)


class ConfirmDialog(ModalScreen[bool]):
    """A Yes/No confirmation, used for every delete action."""

    BINDINGS = [Binding("escape", "dismiss_no", "Cancel", show=False)]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-box", id="confirm-box"):
            yield Static(self._question, classes="dialog-title")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="confirm-no")
                yield Button("Delete", id="confirm-yes", variant="error")

    def action_dismiss_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm-yes")
    def _on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#confirm-no")
    def _on_no(self) -> None:
        self.dismiss(False)


class _FormDialog(ModalScreen[object]):
    """Shared chrome for the add-forms below: a title, an error line, Cancel/Add."""

    BINDINGS = [Binding("escape", "dismiss_form", "Cancel", show=False)]

    def action_dismiss_form(self) -> None:
        self.dismiss(None)

    def _error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(f"[#ff5555]{message}[/]")

    @on(Button.Pressed, "#form-cancel")
    def _on_cancel(self) -> None:
        self.dismiss(None)


class LocationFormDialog(_FormDialog):
    """Add a new Location (a site/building)."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-box", id="form-box"):
            yield Static("Add Location", classes="dialog-title")
            yield Static("Name", classes="field-label")
            yield Input(placeholder="e.g. Warehouse 3", id="f-name")
            yield Static("Description", classes="field-label")
            yield Input(placeholder="optional", id="f-description")
            yield Static("", id="form-error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="form-cancel")
                yield Button("Add", id="form-add", variant="success")

    @on(Button.Pressed, "#form-add")
    def _on_add(self) -> None:
        name = self.query_one("#f-name", Input).value.strip()
        if not name:
            self._error("Name is required.")
            return
        description = self.query_one("#f-description", Input).value.strip()
        self.dismiss(Location(name=name, description=description, rooms=[]))


class RoomFormDialog(_FormDialog):
    """Add a new Room (a sub-location) to whichever Location is selected."""

    def __init__(self, location_name: str) -> None:
        super().__init__()
        self._location_name = location_name

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-box", id="form-box"):
            yield Static(f"Add Room to {self._location_name}", classes="dialog-title")
            yield Static("Name", classes="field-label")
            yield Input(placeholder="e.g. Datacenter 3", id="f-name")
            yield Static("Description", classes="field-label")
            yield Input(placeholder="optional", id="f-description")
            yield Static("", id="form-error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="form-cancel")
                yield Button("Add", id="form-add", variant="success")

    @on(Button.Pressed, "#form-add")
    def _on_add(self) -> None:
        name = self.query_one("#f-name", Input).value.strip()
        if not name:
            self._error("Name is required.")
            return
        description = self.query_one("#f-description", Input).value.strip()
        self.dismiss(Room(name=name, description=description, hosts=[]))


class HostFormDialog(_FormDialog):
    """Add a new Host to whichever Room is currently selected."""

    def __init__(self, room_name: str) -> None:
        super().__init__()
        self._room_name = room_name

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-box", id="form-box"):
            yield Static(f"Add Host to {self._room_name}", classes="dialog-title")
            yield Static("Name", classes="field-label")
            yield Input(placeholder="e.g. db-app-03", id="f-name")
            yield Static("Address", classes="field-label")
            yield Input(placeholder="e.g. 10.20.0.20", id="f-address")
            yield Static("Username", classes="field-label")
            yield Input(placeholder="e.g. operator", id="f-username")
            yield Static("Port", classes="field-label")
            yield Input(placeholder="22", id="f-port")
            yield Static("OS", classes="field-label")
            yield Input(placeholder="e.g. Ubuntu 22.04", id="f-os")
            yield Static("Description", classes="field-label")
            yield Input(placeholder="optional", id="f-description")
            yield Static("", id="form-error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="form-cancel")
                yield Button("Add", id="form-add", variant="success")

    @on(Button.Pressed, "#form-add")
    def _on_add(self) -> None:
        name = self.query_one("#f-name", Input).value.strip()
        address = self.query_one("#f-address", Input).value.strip()
        username = self.query_one("#f-username", Input).value.strip()
        port_text = self.query_one("#f-port", Input).value.strip() or "22"
        os_name = self.query_one("#f-os", Input).value.strip() or "Linux"
        description = self.query_one("#f-description", Input).value.strip()

        if not name or not address or not username:
            self._error("Name, address, and username are required.")
            return
        if not port_text.isdigit():
            self._error("Port must be a number.")
            return

        self.dismiss(
            Host(
                name=name,
                address=address,
                username=username,
                port=int(port_text),
                description=description,
                os=os_name,
                status=Status.ONLINE,
            )
        )
