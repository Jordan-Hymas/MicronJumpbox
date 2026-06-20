"""The Jumpbox Textual application.

A tree-driven SSH jump host, two tabs:

  Dashboard
    ┌── LOCATIONS ──┐ ┌──────────── HOSTS ────────────┐
    │ search…       │ │ search…                       │
    │ 🏭 Location A │ │ host-01   10.0.0.1    ONLINE  │
    │   Datacenter 1│ │ host-02   10.0.0.2    ONLINE  │
    │   Datacenter 2│ │ …                             │
    │ 🔬 Location B │ ├───────────────────────────────┤
    │   Room 1      │ │ thin detail strip + [Connect] │
    │   Room 2      │ │                               │
    └───────────────┘ └───────────────────────────────┘
  Logs
    A timestamped history of hosts you've jumped to this run, with a
    Reconnect button.

Pick a location in the tree (click it to expand and reveal its rooms), then
click a room to list its hosts on the right - each host appears exactly once,
only in the hosts list, never duplicated in the tree. In the hosts list, a
single click previews a host's details; a double click (or Enter, or F2, or
the Connect button) connects - it takes over the current terminal and hands
control back to the dashboard when the session ends.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)

from . import fuzzy, storage
from .connect import DEMO_MODE, launch_session
from .data import Host, Location, Room, Status
from .dialogs import (
    ActionMenu,
    ConfirmDialog,
    HostFormDialog,
    LocationFormDialog,
    RoomFormDialog,
)

BANNER = r"""
     ██╗██╗   ██╗███╗   ███╗██████╗ ██████╗  ██████╗ ██╗  ██╗
     ██║██║   ██║████╗ ████║██╔══██╗██╔══██╗██╔═══██╗╚██╗██╔╝
     ██║██║   ██║██╔████╔██║██████╔╝██████╔╝██║   ██║ ╚███╔╝
██   ██║██║   ██║██║╚██╔╝██║██╔═══╝ ██╔══██╗██║   ██║ ██╔██╗
╚█████╔╝╚██████╔╝██║ ╚═╝ ██║██║     ██████╔╝╚██████╔╝██╔╝ ██╗
 ╚════╝  ╚═════╝ ╚═╝     ╚═╝╚═╝     ╚═════╝  ╚═════╝ ╚═╝  ╚═╝
""".strip("\n")

STATUS_COLOR = {
    Status.ONLINE: "#50fa7b",
    Status.DEGRADED: "#f1fa8c",
    Status.OFFLINE: "#ff5555",
}

MAX_LOG_ENTRIES = 20


def _location_label(location: Location) -> str:
    return (
        f"{location.icon} [b]{location.name}[/]  "
        f"[dim]{location.online}/{location.total_hosts} online[/]"
    )


def _room_label(room: Room) -> str:
    return f"[b]{room.name}[/]  [dim]{room.online}/{len(room.hosts)} online[/]"


def _host_label(host: Host) -> str:
    color = STATUS_COLOR[host.status]
    return (
        f"[{color}]●[/] [{color}]{host.name}[/]  [dim]{host.address}[/]  "
        f"[{color}]{host.status.label}[/]"
    )


def _log_label(entry: "LogEntry") -> str:
    stamp = entry.when.strftime("%Y-%m-%d %I:%M:%S %p")
    host = entry.host
    color = STATUS_COLOR[host.status]
    return (
        f"[dim]{stamp}[/]  {entry.location.icon} "
        f"[dim]{entry.location.name} → {entry.room.name} →[/] "
        f"[{color}]{host.name}[/]  [dim]{host.target}[/]"
    )


@dataclass
class LogEntry:
    """One timestamped "jumped to this host" entry shown on the Logs tab."""

    when: datetime
    location: Location
    room: Room
    host: Host


class HostItem(ListItem):
    """A selectable host row: single click previews, double click connects."""

    class DoubleClicked(Message):
        def __init__(self, host: Host) -> None:
            self.host = host
            super().__init__()

    def __init__(self, host: Host) -> None:
        super().__init__(classes="host-item")
        self.host = host

    def compose(self) -> ComposeResult:
        yield Static(_host_label(self.host))

    def _on_click(self, event: events.Click) -> None:
        # Textual calls every matching handler up the MRO, so ListItem's
        # own _on_click (which posts Selected on every click) would still
        # fire unless we explicitly suppress it. prevent_default() does
        # that; Enter, F2, and a double click remain the ways to connect.
        event.prevent_default()
        event.stop()
        list_view = self.parent
        if isinstance(list_view, ListView):
            list_view.focus()
            try:
                list_view.index = list(list_view.children).index(self)
            except ValueError:
                pass
        if event.chain >= 2:
            self.post_message(self.DoubleClicked(self.host))


class LogItem(ListItem):
    """A row in the connection log."""

    def __init__(self, entry: LogEntry) -> None:
        super().__init__(classes="log-item")
        self.entry = entry

    def compose(self) -> ComposeResult:
        yield Static(_log_label(self.entry))


class JumpboxApp(App):
    """Terminal jump host dashboard."""

    CSS_PATH = "styles.tcss"
    TITLE = "🔐 Jumpbox"
    SUB_TITLE = "SSH Jump Host"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("/", "focus_host_search", "Find host"),
        Binding("ctrl+r", "focus_location_search", "Find location"),
        Binding("f2", "connect", "Connect"),
        Binding("f5", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.locations: list[Location] = []
        self._current_location: Location | None = None
        self._current_room: Room | None = None
        self.logs: list[LogEntry] = []
        # Lets us clear the host search box without re-triggering a search.
        self._suppress_host_search = False

    # ------------------------------------------------------------------ build
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="topbar"):
            yield Button("✕ Exit", id="quit-btn", variant="error")
        with Vertical(id="banner-wrap"):
            yield Static(BANNER, id="banner")
            yield Static(
                "Secure SSH jump host  ·  Micron Technologies"
                + ("   [#f1fa8c]· DEMO MODE ·[/]" if DEMO_MODE else ""),
                id="subtitle",
            )
        with TabbedContent(id="tabs"):
            with TabPane("📊 Dashboard", id="dashboard"):
                with Horizontal(id="body"):
                    with Vertical(id="locations-pane"):
                        with Horizontal(id="location-toolbar"):
                            yield Input(
                                placeholder="Search locations…", id="location-search"
                            )
                            yield Button(
                                "⋮", id="location-menu-btn", classes="kebab-btn"
                            )
                        yield Tree("Locations", id="locations")
                    with Vertical(id="hosts-pane"):
                        with Horizontal(id="host-toolbar"):
                            yield Input(
                                placeholder="Search hosts…", id="host-search"
                            )
                            yield Button(
                                "⋮", id="host-menu-btn", classes="kebab-btn"
                            )
                        yield ListView(id="hosts")
                        with Vertical(id="detail"):
                            yield Static(id="detail-body")
                            with Horizontal(id="actions"):
                                yield Button(
                                    "▶ Connect", id="connect", variant="success"
                                )
                                yield Button("⟳ Refresh", id="refresh")
            with TabPane("🕓 Logs", id="logs"):
                with Vertical(id="logs-pane"):
                    yield Static(
                        "Timestamped history of hosts you've jumped to this run.",
                        id="logs-caption",
                    )
                    yield ListView(id="log-list")
                    with Horizontal(id="log-actions"):
                        yield Button("↺ Reconnect", id="reconnect-log")
        yield Footer()

    async def on_mount(self) -> None:
        self.theme = "dracula"
        self.query_one("#locations-pane").border_title = "LOCATIONS"
        self.query_one("#hosts-pane").border_title = "HOSTS"
        self.query_one("#detail").border_title = "DETAIL"

        tree = self.query_one("#locations", Tree)
        tree.show_root = False
        tree.guide_depth = 3

        self.locations = storage.load()
        await self._populate_locations()
        await self._populate_logs()

    # --------------------------------------------------------------- populate
    async def _populate_locations(self, query: str = "") -> None:
        locations = fuzzy.filter_items(
            query, self.locations, key=lambda loc: loc.search_text
        )
        tree = self.query_one("#locations", Tree)
        tree.clear()
        tree.root.expand()

        first_location = None
        first_room = None
        first_room_node = None
        current_room_node = None
        for location in locations:
            location_node = tree.root.add(
                _location_label(location), data=location, expand=True
            )
            for room in location.rooms:
                room_node = location_node.add_leaf(_room_label(room), data=room)
                if first_room_node is None:
                    first_room_node, first_location, first_room = (
                        room_node,
                        location,
                        room,
                    )
                if room is self._current_room:
                    current_room_node = room_node

        # A freshly built node's line position isn't known until the next
        # render, so move_cursor() here would silently no-op on its own -
        # the deferred call just places the visual cursor correctly later.
        if current_room_node is not None:
            # Rebuilt while the same room was selected (e.g. adding a host
            # elsewhere, or a room/location was deleted but not this one) -
            # keep it selected instead of jumping back to the first room.
            self.call_after_refresh(tree.move_cursor, current_room_node)
        elif first_room_node is not None:
            self.call_after_refresh(tree.move_cursor, first_room_node)
            await self._select_room(first_location, first_room)
        else:
            self._current_location = None
            self._current_room = None
            await self._populate_hosts()

    async def _populate_hosts(self, query: str = "") -> None:
        view = self.query_one("#hosts", ListView)
        await view.clear()
        hosts: list[Host] = []
        if self._current_room is not None:
            hosts = fuzzy.filter_items(
                query, self._current_room.hosts, key=lambda h: h.search_text
            )
            await view.extend(HostItem(host) for host in hosts)
        if hosts:
            view.index = 0
            self._show_detail(hosts[0])
        else:
            self._show_detail(None)

    async def _populate_logs(self) -> None:
        view = self.query_one("#log-list", ListView)
        await view.clear()
        if not self.logs:
            await view.append(
                ListItem(
                    Static(
                        "[dim]No connections logged yet — connect to a host "
                        "from the Dashboard tab.[/]"
                    )
                )
            )
            return
        await view.extend(LogItem(entry) for entry in self.logs)

    async def _select_room(self, location: Location, room: Room) -> None:
        if room is self._current_room:
            return
        self._current_location = location
        self._current_room = room
        self.query_one("#hosts-pane").border_title = (
            f"HOSTS  ·  {location.name} → {room.name}"
        )
        host_search = self.query_one("#host-search", Input)
        if host_search.value:
            # Clearing fires Input.Changed; suppress that one repopulate.
            self._suppress_host_search = True
            host_search.value = ""
        await self._populate_hosts()

    def _show_detail(self, host: Host | None) -> None:
        body = self.query_one("#detail-body", Static)
        connect = self.query_one("#connect", Button)
        if host is None:
            body.update("[dim]No host selected.[/]")
            connect.disabled = True
            return
        connect.disabled = False
        color = STATUS_COLOR[host.status]
        tags = " ".join(f"[dim]#{t}[/]" for t in host.tags)
        body.update(
            f"[b]{host.name}[/]  [{color}]{host.status.label}[/]   "
            f"[dim]{host.username}@{host.address}:{host.port}[/]   "
            f"[dim]{host.os}[/]\n"
            f"[dim]{host.description}[/]   {tags}"
        )

    def _highlighted_host(self) -> Host | None:
        item = self.query_one("#hosts", ListView).highlighted_child
        return item.host if isinstance(item, HostItem) else None

    # ----------------------------------------------------------------- events
    @on(Input.Changed, "#location-search")
    async def _on_location_search(self, event: Input.Changed) -> None:
        await self._populate_locations(event.value)

    @on(Input.Changed, "#host-search")
    async def _on_host_search(self, event: Input.Changed) -> None:
        if self._suppress_host_search:
            self._suppress_host_search = False
            return
        await self._populate_hosts(event.value)

    @on(Input.Submitted, "#host-search")
    def _on_host_submit(self) -> None:
        self.action_connect()

    @on(Tree.NodeHighlighted, "#locations")
    @on(Tree.NodeSelected, "#locations")
    async def _on_location_tree_activated(
        self, event: Tree.NodeHighlighted | Tree.NodeSelected
    ) -> None:
        node = event.node
        if not isinstance(node.data, Room):
            return
        parent = node.parent
        location = (
            parent.data
            if parent is not None and isinstance(parent.data, Location)
            else self._current_location
        )
        if location is not None:
            await self._select_room(location, node.data)

    @on(ListView.Highlighted, "#hosts")
    def _on_host_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, HostItem):
            self._show_detail(event.item.host)

    @on(ListView.Selected, "#hosts")
    def _on_host_selected(self, event: ListView.Selected) -> None:
        # Only reachable via the Enter key now (single click no longer
        # posts Selected - see HostItem._on_click).
        if isinstance(event.item, HostItem):
            self._connect(event.item.host)

    @on(HostItem.DoubleClicked)
    def _on_host_double_clicked(self, event: HostItem.DoubleClicked) -> None:
        self._connect(event.host)

    @on(Button.Pressed, "#location-menu-btn")
    @work
    async def _on_location_menu_button(self) -> None:
        node = self.query_one("#locations", Tree).cursor_node
        options = [("add-location", "➕ Add Location")]
        if node is not None:
            options.append(("add-room", "➕ Add Room"))
            options.append(("delete", "🗑 Delete Selected"))
        choice = await self.push_screen_wait(ActionMenu("Locations", options))
        if choice == "add-location":
            await self._add_location()
        elif choice == "add-room":
            await self._add_room()
        elif choice == "delete":
            await self._delete_selected_tree_node()

    @on(Button.Pressed, "#host-menu-btn")
    @work
    async def _on_host_menu_button(self) -> None:
        options = [("add-host", "➕ Add Host")]
        if self._highlighted_host() is not None:
            options.append(("delete", "🗑 Delete Selected"))
        choice = await self.push_screen_wait(ActionMenu("Hosts", options))
        if choice == "add-host":
            await self._add_host()
        elif choice == "delete":
            await self._delete_host()

    @on(Button.Pressed, "#connect")
    def _on_connect_button(self) -> None:
        self.action_connect()

    @on(Button.Pressed, "#refresh")
    async def _on_refresh_button(self) -> None:
        await self.action_refresh()

    @on(Button.Pressed, "#reconnect-log")
    async def _on_reconnect_button(self) -> None:
        item = self.query_one("#log-list", ListView).highlighted_child
        if isinstance(item, LogItem):
            self._connect(item.entry.host, item.entry.location, item.entry.room)
            await self._populate_logs()
        else:
            self.notify("No log entry selected.", severity="warning")

    @on(TabbedContent.TabActivated)
    async def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "logs":
            await self._populate_logs()

    @on(Button.Pressed, "#quit-btn")
    async def _on_quit_button(self) -> None:
        await self.action_quit()

    # ---------------------------------------------------------------- actions
    def action_focus_host_search(self) -> None:
        self.query_one("#host-search", Input).focus()

    def action_focus_location_search(self) -> None:
        self.query_one("#location-search", Input).focus()

    def action_connect(self) -> None:
        host = self._highlighted_host()
        if host is None:
            self.notify("No host selected.", severity="warning")
            return
        self._connect(host)

    async def action_refresh(self) -> None:
        self.locations = storage.load()
        self._current_location = None
        self._current_room = None
        self.query_one("#location-search", Input).value = ""
        await self._populate_locations()
        self.notify("Inventory refreshed.", title="Jumpbox")

    # ------------------------------------------------------------- add/remove
    def _save(self) -> None:
        storage.save(self.locations)

    async def _add_location(self) -> None:
        result = await self.push_screen_wait(LocationFormDialog())
        if result is None:
            return
        self.locations.append(result)
        self._save()
        await self._populate_locations(self.query_one("#location-search", Input).value)
        self.notify(f"Added location '{result.name}'.", title="Jumpbox")

    async def _add_room(self) -> None:
        node = self.query_one("#locations", Tree).cursor_node
        location = self._node_location(node)
        if location is None:
            self.notify("Select a location first.", severity="warning")
            return
        result = await self.push_screen_wait(RoomFormDialog(location.name))
        if result is None:
            return
        location.rooms.append(result)
        self._save()
        await self._populate_locations(self.query_one("#location-search", Input).value)
        self.notify(f"Added room '{result.name}' to {location.name}.", title="Jumpbox")

    async def _delete_selected_tree_node(self) -> None:
        node = self.query_one("#locations", Tree).cursor_node
        if node is None:
            self.notify("Nothing selected to delete.", severity="warning")
            return

        if isinstance(node.data, Location):
            location = node.data
            confirmed = await self.push_screen_wait(
                ConfirmDialog(
                    f"Are you sure you want to delete location '{location.name}' "
                    "and everything inside it?"
                )
            )
            if not confirmed:
                return
            self.locations.remove(location)
            self._save()
            if self._current_location is location:
                self._current_location = None
                self._current_room = None
            await self._populate_locations(self.query_one("#location-search", Input).value)
            self.notify(f"Deleted location '{location.name}'.", title="Jumpbox")

        elif isinstance(node.data, Room):
            room = node.data
            confirmed = await self.push_screen_wait(
                ConfirmDialog(
                    f"Are you sure you want to delete room '{room.name}' "
                    "and all its hosts?"
                )
            )
            if not confirmed:
                return
            parent_location = self._node_location(node.parent)
            if parent_location is not None:
                parent_location.rooms.remove(room)
            self._save()
            if self._current_room is room:
                self._current_location = None
                self._current_room = None
            await self._populate_locations(self.query_one("#location-search", Input).value)
            self.notify(f"Deleted room '{room.name}'.", title="Jumpbox")

    async def _add_host(self) -> None:
        if self._current_room is None:
            self.notify("Select a room first.", severity="warning")
            return
        room = self._current_room
        result = await self.push_screen_wait(HostFormDialog(room.name))
        if result is None:
            return
        room.hosts.append(result)
        self._save()
        await self._populate_hosts(self.query_one("#host-search", Input).value)
        await self._populate_locations(self.query_one("#location-search", Input).value)
        self.notify(f"Added host '{result.name}' to {room.name}.", title="Jumpbox")

    async def _delete_host(self) -> None:
        host = self._highlighted_host()
        if host is None or self._current_room is None:
            self.notify("No host selected to delete.", severity="warning")
            return
        confirmed = await self.push_screen_wait(
            ConfirmDialog(f"Are you sure you want to delete host '{host.name}'?")
        )
        if not confirmed:
            return
        self._current_room.hosts.remove(host)
        self._save()
        await self._populate_hosts(self.query_one("#host-search", Input).value)
        await self._populate_locations(self.query_one("#location-search", Input).value)
        self.notify(f"Deleted host '{host.name}'.", title="Jumpbox")

    @staticmethod
    def _node_location(node) -> Location | None:
        """The Location a tree node belongs to (itself, or its parent)."""
        if node is None:
            return None
        if isinstance(node.data, Location):
            return node.data
        if isinstance(node.data, Room) and node.parent is not None:
            return node.parent.data if isinstance(node.parent.data, Location) else None
        return None

    def _connect(
        self, host: Host, location: Location | None = None, room: Room | None = None
    ) -> None:
        location = location or self._current_location
        room = room or self._current_room
        if host.status is Status.OFFLINE:
            self.notify(
                f"{host.name} is OFFLINE — opening a window anyway (demo).",
                title="Heads up",
                severity="warning",
            )

        # No separate window is possible here - hand the whole terminal to
        # the session and resume the dashboard once it ends.
        args = launch_session(host)
        self._run_foreground(args)
        self.notify(f"Welcome back from {host.name}.", title="Jumpbox")

        if location is not None and room is not None:
            self.logs.insert(0, LogEntry(datetime.now(), location, room, host))
            del self.logs[MAX_LOG_ENTRIES:]

    def _run_foreground(self, args: list[str]) -> None:
        """Suspend the dashboard and hand the terminal to `args` (e.g. plain
        ssh) - there's no separate window to open it in instead."""
        with self.suspend():
            subprocess.run(args)


def run() -> None:
    JumpboxApp().run()
