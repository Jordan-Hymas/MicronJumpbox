"""Modal dialogs for adding/removing locations, rooms, and hosts.

Each "+" button in the dashboard opens an ActionMenu, which routes to either
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
from textual.widgets import Button, Input, Select, Static

from .data import Host, Location, Room, Status


class ActionMenu(ModalScreen[str | None]):
    """A tiny menu of (action_id, label) choices, opened from a "+" button."""

    BINDINGS = [Binding("escape", "dismiss_menu", "Cancel", show=False)]

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-box menu-card", id="menu-box"):
            yield Static(self._title, classes="dialog-title")
            for action_id, label in self._options:
                variant = (
                    "success"
                    if action_id.startswith("add-")
                    else "error" if action_id == "delete" else "default"
                )
                yield Button(
                    label, id=f"opt-{action_id}", classes="menu-option", variant=variant
                )
            yield Button("Cancel", id="opt-cancel", classes="menu-option menu-cancel")

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


class TagFormDialog(_FormDialog):
    """Add a new tag to the prebuilt vocabulary offered on the Add Host
    form's tag picker (managed from the Tags tab's own "+" menu)."""

    def __init__(self, existing_tags: list[str]) -> None:
        super().__init__()
        self._existing_lower = {t.lower() for t in existing_tags}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-box", id="form-box"):
            yield Static("Add Tag", classes="dialog-title")
            yield Static("Name", classes="field-label")
            yield Input(placeholder="e.g. vpn-concentrator", id="f-name")
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
        if name.lower() in self._existing_lower:
            self._error(f"'{name}' already exists.")
            return
        self.dismiss(name)


class HostFormDialog(_FormDialog):
    """Add a new Host to whichever Room is currently selected.

    Tags are picked from the prebuilt vocabulary (managed on the Tags tab)
    rather than typed freehand - a `Select` dropdown plus an Add button
    moves the chosen tag into a running list of "chips" below it, each
    removable on its own, since a host can carry more than one.
    """

    def __init__(self, room_name: str, tag_vocabulary: list[str]) -> None:
        super().__init__()
        self._room_name = room_name
        self._tag_vocabulary = tag_vocabulary
        self._chosen_tags: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-box wide", id="form-box"):
            yield Static(f"Add Host to {self._room_name}", classes="dialog-title")

            yield Static("Name", classes="field-label")
            yield Input(placeholder="e.g. db-app-03", id="f-name")

            with Horizontal(classes="field-row"):
                with Vertical(classes="field-group left"):
                    yield Static("Address", classes="field-label")
                    yield Input(placeholder="e.g. 10.20.0.20", id="f-address")
                with Vertical(classes="field-group"):
                    yield Static("Port", classes="field-label")
                    yield Input(placeholder="22", id="f-port")

            with Horizontal(classes="field-row"):
                with Vertical(classes="field-group left"):
                    yield Static("Username", classes="field-label")
                    yield Input(placeholder="e.g. operator", id="f-username")
                with Vertical(classes="field-group"):
                    yield Static("OS", classes="field-label")
                    yield Input(placeholder="e.g. Ubuntu 22.04", id="f-os")

            yield Static("Description", classes="field-label")
            yield Input(placeholder="optional", id="f-description")

            yield Static("Tags", classes="field-label")
            with Horizontal(classes="field-row"):
                yield Select(
                    [(tag, tag) for tag in self._tag_vocabulary],
                    prompt="Choose a tag…",
                    id="f-tag-select",
                )
                yield Button("+ Add", id="f-tag-add", classes="add-btn")
            yield Horizontal(id="f-tag-chips")

            yield Static("", id="form-error")
            with Horizontal(classes="dialog-actions"):
                yield Button("Cancel", id="form-cancel")
                yield Button("Add", id="form-add", variant="success")

    async def on_mount(self) -> None:
        await self._refresh_chips()

    async def _refresh_chips(self) -> None:
        chips = self.query_one("#f-tag-chips", Horizontal)
        await chips.remove_children()
        if self._chosen_tags:
            await chips.mount_all(
                Button(f"#{tag} ✕", id=f"chip-{index}", classes="tag-chip")
                for index, tag in enumerate(self._chosen_tags)
            )
        else:
            await chips.mount(Static("[dim]No tags chosen yet[/]", classes="tag-chip-empty"))

    @on(Button.Pressed, "#f-tag-add")
    async def _on_add_tag(self) -> None:
        select = self.query_one("#f-tag-select", Select)
        if select.value is Select.NULL:
            return
        tag = str(select.value)
        if tag not in self._chosen_tags:
            self._chosen_tags.append(tag)
            await self._refresh_chips()
        select.value = Select.NULL

    @on(Button.Pressed, ".tag-chip")
    async def _on_remove_chip(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if not button_id.startswith("chip-"):
            return
        del self._chosen_tags[int(button_id.removeprefix("chip-"))]
        await self._refresh_chips()

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
                tags=tuple(self._chosen_tags),
            )
        )
