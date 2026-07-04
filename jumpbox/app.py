"""The Jumpbox Textual application.

A tree-driven SSH jump host, three tabs:

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
  Tags
    Every tag used by any host, pulled from across the whole inventory -
    not scoped to whatever room happens to be selected on the Dashboard.
    Pick a tag on the left to list every host that carries it on the
    right, each labelled with the location/room it actually lives in;
    double click (or Enter) connects, same as the Dashboard.
  Activity
    Every host you've connected to this run, newest first. A still-open
    one (its pane is alive) shows "● OPEN"; once that pane closes - typed
    `exit`, or the connection just dropping - the row stays put as plain
    history instead of disappearing. Select any row and hit Reconnect to
    open it again.

Pick a location in the tree (click it to expand and reveal its rooms), then
click a room to list its hosts on the right - each host appears exactly once,
only in the hosts list, never duplicated in the tree. In the hosts list, a
single click previews a host's details; a double click (or Enter, or F2, or
the Connect button) opens it as a new tmux pane - the first one splits off to
the right of Jumpbox's own pane, every one after that stacks below the last,
and this dashboard never leaves the screen. See `panes.py` for the mechanism.

Beyond the three tabs: Ctrl+F opens the Quick Connect palette (fuzzy
search over every host in every location at once, Enter connects); the
"+" menus add/edit/delete locations, rooms, and hosts (edit opens the
same form prefilled); a background sweep TCP-probes every host's ssh port
so the status dots reflect live reachability (config.json's
probe_interval, 0 = off); and each connect is recorded to a persistent
per-host history (last connected / count), shown in the detail panel.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
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

from . import fuzzy, panes, storage
from .connect import connect_command, forwarded_agent_available, probe
from .data import Host, Location, Room, Status
from .dialogs import (
    ActionMenu,
    ConfirmDialog,
    HostFormDialog,
    LocationFormDialog,
    RoomFormDialog,
    TagFormDialog,
)

_BANNER_ROWS = [
    r"     ██╗██╗   ██╗███╗   ███╗██████╗ ██████╗  ██████╗ ██╗  ██╗",
    r"     ██║██║   ██║████╗ ████║██╔══██╗██╔══██╗██╔═══██╗╚██╗██╔╝",
    r"     ██║██║   ██║██╔████╔██║██████╔╝██████╔╝██║   ██║ ╚███╔╝",
    r"██   ██║██║   ██║██║╚██╔╝██║██╔═══╝ ██╔══██╗██║   ██║ ██╔██╗",
    r"╚█████╔╝╚██████╔╝██║ ╚═╝ ██║██║     ██████╔╝╚██████╔╝██╔╝ ██╗",
    r" ╚════╝  ╚═════╝ ╚═╝     ╚═╝╚═╝     ╚═════╝  ╚═════╝ ╚═╝  ╚═╝",
]
# Rows must all share one width: `#banner` is centred with `text-align:
# center`, which centres each line of the Static's text independently. A
# row even one cell narrower than the rest gets a different left pad than
# its neighbours, and that mismatch shifts as the terminal is resized -
# the letters appear to warp. Padding every row out to the widest one
# keeps the centring offset identical for all of them at any width.
_BANNER_WIDTH = max(len(row) for row in _BANNER_ROWS)
BANNER = "\n".join(row.ljust(_BANNER_WIDTH) for row in _BANNER_ROWS)

STATUS_COLOR = {
    Status.ONLINE: "#50fa7b",
    Status.DEGRADED: "#f1fa8c",
    Status.OFFLINE: "#ff5555",
}

# Mirrors $accent in styles.tcss. Rich markup embedded in a Python string
# can't reach into Textual's own CSS variables, so this constant is the
# one place to keep the two in sync by hand.
ACCENT = "#b014e5"
OPEN_COLOR = "#50fa7b"

# One (bold, full-saturation) colour per location, cycling for any number
# of them; its rooms use the paired lighter tint of the *same* hue, so a
# location and its own rooms read as a clear group, and different
# locations are easy to tell apart at a glance in the tree.
LOCATION_PALETTE = [
    ("#ff79c6", "#ffb3e0"),  # pink
    ("#8be9fd", "#bdf3fe"),  # cyan
    ("#ffb86c", "#ffd9a8"),  # orange
    ("#bd93f9", "#dac8fd"),  # purple
    ("#69d2c1", "#a8e8db"),  # teal
]

MAX_ACTIVITY_ENTRIES = 20

# How many hosts a live-status sweep probes at once. Bounded so a
# thousand-host inventory doesn't open a thousand simultaneous sockets
# from the bastion.
PROBE_CONCURRENCY = 16


def _ago(iso_timestamp: str) -> str | None:
    """A compact 'how long ago' for a stored ISO timestamp - '3m ago',
    '2h ago', '5d ago' - or None if the value is unparseable."""
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - then).total_seconds()
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _location_label(location: Location, color_index: int) -> str:
    color = LOCATION_PALETTE[color_index % len(LOCATION_PALETTE)][0]
    return (
        f"[bold {color}]{location.icon} {location.name}[/]  "
        f"[dim]{location.online}/{location.total_hosts} online[/]"
    )


def _room_label(room: Room, color_index: int) -> str:
    color = LOCATION_PALETTE[color_index % len(LOCATION_PALETTE)][1]
    return f"[bold {color}]{room.name}[/]  [dim]{room.online}/{len(room.hosts)} online[/]"


def _host_label(host: Host) -> str:
    color = STATUS_COLOR[host.status]
    return (
        f"[{color}]●[/] [{color}]{host.name}[/]  [dim]{host.address}[/]  "
        f"[{color}]{host.status.label}[/]"
    )


def _tag_host_label(location: Location, room: Room, host: Host) -> str:
    color = STATUS_COLOR[host.status]
    return (
        f"{location.icon} [dim]{location.name} → {room.name}[/]  "
        f"[{color}]●[/] [{color}]{host.name}[/]  [dim]{host.address}[/]  "
        f"[{color}]{host.status.label}[/]"
    )


def _activity_label(entry: "ActivityEntry") -> str:
    stamp = entry.opened_at.strftime("%Y-%m-%d %I:%M:%S %p")
    host = entry.host
    color = STATUS_COLOR[host.status]
    state = f"[bold {OPEN_COLOR}]● OPEN[/]" if entry.is_open else "[dim]closed[/]"
    return (
        f"[dim]{stamp}[/]  {entry.location.icon} "
        f"[dim]{entry.location.name} → {entry.room.name} →[/] "
        f"[{color}]{host.name}[/]  [dim]{host.target}[/]  {state}"
    )


@dataclass
class ActivityEntry:
    """One "you connected to this host" event, shown on the Activity tab.

    Starts open the moment a pane is opened (`closed_at` is None, `pane_id`
    set); `_reconcile_activity` fills in `closed_at` once tmux no longer has
    that pane - the only way a session ever ends (see panes.py). The entry
    stays in `self.activity` after that as plain history.
    """

    pane_id: str
    host: Host
    location: Location
    room: Room
    opened_at: datetime
    closed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class _HostRow(ListItem):
    """Shared single-click-preview / double-click-connect row for any list
    of hosts you can open a pane to. `location`/`room` ride along on every
    row (not just looked up from whatever's currently selected) so this
    works the same whether the row came from the Dashboard's hosts list
    (scoped to one room) or the Tags tab's (pulled from across the whole
    inventory)."""

    class Activated(Message):
        def __init__(self, item: "_HostRow") -> None:
            self.item = item
            super().__init__()

    def __init__(self, location: Location, room: Room, host: Host, *, classes: str) -> None:
        super().__init__(classes=classes)
        self.location = location
        self.room = room
        self.host = host

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
            self.post_message(self.Activated(self))


class HostItem(_HostRow):
    """A selectable host row in the Dashboard's Hosts list."""

    def __init__(self, location: Location, room: Room, host: Host) -> None:
        super().__init__(location, room, host, classes="host-item")

    def compose(self) -> ComposeResult:
        yield Static(_host_label(self.host))


class TagHostItem(_HostRow):
    """A host row on the Tags tab - same click behaviour as HostItem, but
    labelled with the location/room that actually owns it, since these
    rows span the whole inventory rather than one selected room."""

    def __init__(self, location: Location, room: Room, host: Host) -> None:
        super().__init__(location, room, host, classes="tag-host-item")

    def compose(self) -> ComposeResult:
        yield Static(_tag_host_label(self.location, self.room, self.host))


class TagItem(ListItem):
    """A row in the Tags tab's tag list: one per distinct tag in use, with
    how many hosts across the whole inventory carry it."""

    def __init__(self, tag: str, count: int) -> None:
        super().__init__(classes="tag-item")
        self.tag = tag
        self.count = count

    def compose(self) -> ComposeResult:
        plural = "" if self.count == 1 else "s"
        yield Static(
            f"[bold {ACCENT}]#{self.tag}[/]  [dim]{self.count} host{plural}[/]"
        )


class ActivityItem(ListItem):
    """A row on the Activity tab: one connection, open or closed."""

    def __init__(self, entry: ActivityEntry) -> None:
        super().__init__(classes="activity-item")
        self.entry = entry

    def compose(self) -> ComposeResult:
        yield Static(_activity_label(self.entry), classes="activity-label")


class QuickConnectItem(ListItem):
    """One search result in the Quick Connect palette."""

    def __init__(
        self, location: Location, room: Room, host: Host, last_connected: str | None
    ) -> None:
        super().__init__(classes="qc-item")
        self.location = location
        self.room = room
        self.host = host
        self._last_connected = last_connected

    def compose(self) -> ComposeResult:
        color = STATUS_COLOR[self.host.status]
        suffix = f"  [dim]· {self._last_connected}[/]" if self._last_connected else ""
        yield Static(
            f"{self.location.icon} [dim]{self.location.name} → {self.room.name}[/]  "
            f"[{color}]●[/] [{color}]{self.host.name}[/]  "
            f"[dim]{self.host.address}[/]{suffix}"
        )


class QuickConnectDialog(ModalScreen["tuple[Location, Room, Host] | None"]):
    """Ctrl+F from anywhere: fuzzy-search every host in the whole
    inventory at once - no drilling into a location and room first - and
    hit Enter to connect to the top match. The answer to "I know the
    hostname, no idea which building it's in" once the inventory is
    hundreds of hosts.

    Resolves to the chosen (location, room, host), or None on Escape; the
    caller does the actual connecting, exactly like any other host row.
    """

    BINDINGS = [
        Binding("escape", "dismiss_quick", "Cancel", show=False),
        Binding("down", "focus_results", "Results", show=False),
    ]

    MAX_RESULTS = 30

    def __init__(self, locations: list[Location], history: dict[str, dict]) -> None:
        super().__init__()
        self._entries = [
            (location, room, host)
            for location in locations
            for room in location.rooms
            for host in room.hosts
        ]
        self._history = history

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog-box wide", id="qc-box"):
            yield Static("Quick Connect", classes="dialog-title")
            yield Input(placeholder="Type to search every host everywhere…", id="qc-input")
            yield ListView(id="qc-list")
            yield Static(
                "[dim]Enter connects the highlighted host · Esc cancels[/]",
                id="qc-hint",
            )

    async def on_mount(self) -> None:
        await self._populate("")
        self.query_one("#qc-input", Input).focus()

    async def _populate(self, query: str) -> None:
        matches = fuzzy.filter_items(
            query,
            self._entries,
            key=lambda entry: [
                entry[2].search_text,
                f"{entry[0].name} {entry[1].name} {entry[2].name}",
            ],
        )[: self.MAX_RESULTS]
        view = self.query_one("#qc-list", ListView)
        await view.clear()
        if not matches:
            await view.append(ListItem(Static("[dim]No hosts match.[/]")))
            return
        await view.extend(
            QuickConnectItem(
                location,
                room,
                host,
                _ago((self._history.get(host.name) or {}).get("last_connected", "")),
            )
            for location, room, host in matches
        )
        view.index = 0

    def action_dismiss_quick(self) -> None:
        self.dismiss(None)

    def action_focus_results(self) -> None:
        self.query_one("#qc-list", ListView).focus()

    @on(Input.Changed, "#qc-input")
    async def _on_query_changed(self, event: Input.Changed) -> None:
        await self._populate(event.value)

    @on(Input.Submitted, "#qc-input")
    def _on_query_submitted(self) -> None:
        self._choose_highlighted()

    @on(ListView.Selected, "#qc-list")
    def _on_result_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, QuickConnectItem):
            self.dismiss((event.item.location, event.item.room, event.item.host))

    def _choose_highlighted(self) -> None:
        view = self.query_one("#qc-list", ListView)
        item = view.highlighted_child
        if not isinstance(item, QuickConnectItem):
            # Enter straight from the input: take the top match if any.
            children = [c for c in view.children if isinstance(c, QuickConnectItem)]
            if not children:
                return
            item = children[0]
        self.dismiss((item.location, item.room, item.host))


class JumpboxApp(App):
    """Terminal jump host dashboard."""

    CSS_PATH = "styles.tcss"
    TITLE = "Jumpbox"
    SUB_TITLE = "SSH Jump Host"

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+f", "quick_connect", "Quick connect"),
        Binding("/", "focus_host_search", "Find host"),
        Binding("ctrl+r", "focus_location_search", "Find location"),
        Binding("f2", "connect", "Connect"),
        Binding("f4", "fullscreen", "Fullscreen host"),
        Binding("f5", "refresh", "Refresh"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.locations: list[Location] = []
        self.tag_vocabulary: list[str] = []
        self.config = storage.load_config()
        # host name -> {"last_connected": ISO timestamp, "count": int},
        # persisted across runs (storage.record_connection).
        self.history: dict[str, dict] = {}
        self._current_location: Location | None = None
        self._current_room: Room | None = None
        self._current_tag: str | None = None
        self.activity: list[ActivityEntry] = []
        # Set once on_mount confirms this process is actually sitting in a
        # tmux pane - lets __main__.py kill the whole session on exit, and
        # lets _connect() refuse to open a pane it has nowhere to put.
        self.tmux_session: str | None = None
        self._jumpbox_pane_id = ""
        self._window_id = ""
        # Lets us clear the host search box without re-triggering a search.
        self._suppress_host_search = False
        # One live-status sweep at a time; a slow network can't stack them.
        self._probe_running = False

    def notify(self, *args, **kwargs) -> None:
        """No-op: toast popups are disabled app-wide.

        Overriding here (rather than stripping every self.notify(...)
        call) keeps all the existing call sites as one-line silencing.
        """
        return

    # ------------------------------------------------------------------ build
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="topbar"):
            yield Button("✕ Exit", id="quit-btn", variant="error")
        with Vertical(id="banner-wrap"):
            yield Static(BANNER, id="banner")
            yield Static(
                "Secure SSH jump host  ·  Micron Technologies",
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
                                "+", id="location-menu-btn", classes="add-btn"
                            )
                        yield Tree("Locations", id="locations")
                    with Vertical(id="hosts-pane"):
                        with Horizontal(id="host-toolbar"):
                            yield Input(
                                placeholder="Search hosts…", id="host-search"
                            )
                            yield Button(
                                "+", id="host-menu-btn", classes="add-btn"
                            )
                        yield ListView(id="hosts")
                        with Vertical(id="detail"):
                            yield Static(id="detail-body")
                            with Horizontal(id="actions"):
                                yield Button(
                                    "▶ Connect", id="connect", variant="success"
                                )
                                yield Button(
                                    "⛶ Fullscreen", id="fullscreen", disabled=True
                                )
                                yield Button("⟳ Refresh", id="refresh")
            with TabPane("🏷 Tags", id="tags"):
                with Horizontal(id="tags-body"):
                    with Vertical(id="tags-pane"):
                        with Horizontal(id="tag-toolbar"):
                            yield Input(placeholder="Search tags…", id="tag-search")
                            yield Button("+", id="tag-menu-btn", classes="add-btn")
                        yield ListView(id="tag-list")
                    with Vertical(id="tag-hosts-pane"):
                        yield ListView(id="tag-hosts")
            with TabPane("🕓 Activity", id="activity"):
                with Vertical(id="activity-pane"):
                    yield Static(
                        "Every host you've connected to this run - still-open "
                        "ones show ● OPEN, closed ones stay as history.",
                        id="activity-caption",
                    )
                    yield ListView(id="activity-list")
                    with Horizontal(id="activity-actions"):
                        yield Button("↺ Reconnect", id="reconnect-activity")
                        yield Button(
                            "⛶ Fullscreen", id="fullscreen-activity", disabled=True
                        )
        yield Footer()

    async def on_mount(self) -> None:
        self.theme = "dracula"
        self.query_one("#locations-pane").border_title = "LOCATIONS"
        self.query_one("#hosts-pane").border_title = "HOSTS"
        self.query_one("#detail").border_title = "DETAIL"
        self.query_one("#tags-pane").border_title = "TAGS"
        self.query_one("#tag-hosts-pane").border_title = "HOSTS"

        tree = self.query_one("#locations", Tree)
        tree.show_root = False
        tree.guide_depth = 3

        if "TMUX" in os.environ:
            try:
                self._jumpbox_pane_id = panes.current_pane_id()
                self._window_id = panes.current_window_id()
                self.tmux_session = panes.current_session_name()
                panes.enable_mouse(self.tmux_session)
            except RuntimeError as exc:
                self.notify(f"tmux session detection failed: {exc}", severity="error")
        else:
            self.notify(
                "Not running inside tmux - connecting has nowhere to open a pane.",
                title="Jumpbox",
                severity="warning",
            )

        if forwarded_agent_available():
            self.notify(
                "SSH agent detected - host connections will try your "
                "forwarded keys before any password.",
                title="Jumpbox",
            )
        else:
            self.notify(
                "No forwarded SSH agent detected ($SSH_AUTH_SOCK) - host "
                "connections will prompt for a password unless this box has "
                "its own key for that host. Enable 'Forward SSH agent' "
                "(Pageant) in MobaXterm's session settings to use your "
                "local keys instead.",
                title="Jumpbox",
                severity="warning",
                timeout=10,
            )

        self.locations, self.tag_vocabulary = storage.load()
        self.history = storage.load_history()
        await self._populate_locations()
        await self._populate_tags()
        await self._populate_activity()
        self.set_interval(1.0, self._reconcile_activity)
        if self.config.probe_interval > 0:
            self._start_status_probe()
            self.set_interval(self.config.probe_interval, self._start_status_probe)

    # --------------------------------------------------------------- populate
    async def _populate_locations(self, query: str = "") -> None:
        locations = fuzzy.filter_items(
            query, self.locations, key=lambda loc: loc.search_candidates
        )
        tree = self.query_one("#locations", Tree)
        tree.clear()
        tree.root.expand()

        first_location = None
        first_room = None
        first_room_node = None
        current_room_node = None
        for location in locations:
            # Indexed against the *full* inventory, not this (possibly
            # filtered) list, so a location's colour stays put regardless
            # of what a search happens to be narrowing the tree down to.
            color_index = self.locations.index(location)
            location_node = tree.root.add(
                _location_label(location, color_index), data=location, expand=True
            )
            for room in location.rooms:
                room_node = location_node.add_leaf(
                    _room_label(room, color_index), data=room
                )
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
        # Remember which host was highlighted so a rebuild that still
        # contains it (a status sweep repainting dots, an edit elsewhere)
        # puts the cursor back rather than yanking it to the top. A room
        # switch naturally won't find the old name and starts at 0.
        previous = view.highlighted_child
        previous_name = previous.host.name if isinstance(previous, HostItem) else None
        await view.clear()
        hosts: list[Host] = []
        if self._current_room is not None:
            hosts = fuzzy.filter_items(
                query, self._current_room.hosts, key=lambda h: h.search_text
            )
            await view.extend(
                HostItem(self._current_location, self._current_room, host)
                for host in hosts
            )
        if hosts:
            index = next(
                (i for i, host in enumerate(hosts) if host.name == previous_name), 0
            )
            view.index = index
            self._show_detail(hosts[index])
        else:
            self._show_detail(None)

    async def _populate_tags(self, query: str = "") -> None:
        counts: dict[str, int] = {}
        for location in self.locations:
            for room in location.rooms:
                for host in room.hosts:
                    for tag in host.tags:
                        counts[tag] = counts.get(tag, 0) + 1

        # The vocabulary (managed from this tab's own "+" menu) is shown
        # even for tags no host has yet (count 0); any tag a host already
        # carries is shown too even if it's missing from the vocabulary
        # (hand-edited data, or a tag deleted from the vocabulary after
        # being used) - nothing real is ever hidden here.
        all_tags = sorted(set(self.tag_vocabulary) | set(counts))
        tags = fuzzy.filter_items(query, all_tags, key=lambda t: t)
        view = self.query_one("#tag-list", ListView)
        await view.clear()

        if not tags:
            await view.append(
                ListItem(
                    Static(
                        "[dim]No tags match.[/]"
                        if query
                        else "[dim]No tags yet — add one with the + button.[/]"
                    )
                )
            )
            await self._select_tag(None)
            return

        await view.extend(TagItem(tag, counts.get(tag, 0)) for tag in tags)
        target = self._current_tag if self._current_tag in tags else tags[0]
        view.index = tags.index(target)
        await self._select_tag(target)

    async def _populate_tag_hosts(self) -> None:
        view = self.query_one("#tag-hosts", ListView)
        await view.clear()
        if self._current_tag is None:
            await view.append(ListItem(Static("[dim]Select a tag to see its hosts.[/]")))
            return
        matches = [
            (location, room, host)
            for location in self.locations
            for room in location.rooms
            for host in room.hosts
            if self._current_tag in host.tags
        ]
        if not matches:
            await view.append(ListItem(Static("[dim]No hosts have this tag.[/]")))
            return
        await view.extend(
            TagHostItem(location, room, host) for location, room, host in matches
        )

    async def _populate_activity(self) -> None:
        view = self.query_one("#activity-list", ListView)
        await view.clear()
        if not self.activity:
            await view.append(
                ListItem(
                    Static(
                        "[dim]No activity yet — connect to a host from the "
                        "Dashboard tab.[/]"
                    )
                )
            )
            return
        await view.extend(ActivityItem(entry) for entry in self.activity)

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

    async def _select_tag(self, tag: str | None) -> None:
        self._current_tag = tag
        self.query_one("#tag-hosts-pane").border_title = (
            f"HOSTS  ·  #{tag}" if tag else "HOSTS"
        )
        await self._populate_tag_hosts()

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
        entry = self.history.get(host.name) or {}
        last = _ago(entry.get("last_connected", ""))
        history_note = ""
        if last:
            count = int(entry.get("count", 0))
            history_note = (
                f"   [dim]last connected {last}"
                + (f" · {count} times[/]" if count > 1 else "[/]")
            )
        body.update(
            f"[b]{host.name}[/]  [{color}]{host.status.label}[/]   "
            f"[dim]{host.username}@{host.address}:{host.port}[/]   "
            f"[dim]{host.os}[/]{history_note}\n"
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
    @on(ListView.Selected, "#tag-hosts")
    def _on_host_selected(self, event: ListView.Selected) -> None:
        # Only reachable via the Enter key now (single click no longer
        # posts Selected - see _HostRow._on_click).
        if isinstance(event.item, _HostRow):
            self._connect(event.item.host, event.item.location, event.item.room)

    @on(_HostRow.Activated)
    def _on_host_row_activated(self, event: _HostRow.Activated) -> None:
        item = event.item
        self._connect(item.host, item.location, item.room)

    @on(ListView.Highlighted, "#tag-list")
    async def _on_tag_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, TagItem):
            await self._select_tag(event.item.tag)

    @on(Input.Changed, "#tag-search")
    async def _on_tag_search(self, event: Input.Changed) -> None:
        await self._populate_tags(event.value)

    @on(Button.Pressed, "#location-menu-btn")
    @work
    async def _on_location_menu_button(self) -> None:
        node = self.query_one("#locations", Tree).cursor_node
        options = [("add-location", "Add Location")]
        if node is not None:
            options.append(("add-room", "Add Room"))
            options.append(("edit", "Edit Selected"))
            options.append(("delete", "Delete Selected"))
        choice = await self.push_screen_wait(ActionMenu("Locations", options))
        if choice == "add-location":
            await self._add_location()
        elif choice == "add-room":
            await self._add_room()
        elif choice == "edit":
            await self._edit_selected_tree_node()
        elif choice == "delete":
            await self._delete_selected_tree_node()

    @on(Button.Pressed, "#host-menu-btn")
    @work
    async def _on_host_menu_button(self) -> None:
        options = [("add-host", "Add Host")]
        if self._highlighted_host() is not None:
            options.append(("edit", "Edit Selected"))
            options.append(("delete", "Delete Selected"))
        choice = await self.push_screen_wait(ActionMenu("Hosts", options))
        if choice == "add-host":
            await self._add_host()
        elif choice == "edit":
            await self._edit_host()
        elif choice == "delete":
            await self._delete_host()

    @on(Button.Pressed, "#tag-menu-btn")
    @work
    async def _on_tag_menu_button(self) -> None:
        options = [("add-tag", "Add Tag")]
        if self._current_tag is not None:
            options.append(("delete", "Delete Selected"))
        choice = await self.push_screen_wait(ActionMenu("Tags", options))
        if choice == "add-tag":
            await self._add_tag()
        elif choice == "delete":
            await self._delete_tag()

    @on(Button.Pressed, "#connect")
    def _on_connect_button(self) -> None:
        self.action_connect()

    @on(Button.Pressed, "#refresh")
    async def _on_refresh_button(self) -> None:
        await self.action_refresh()

    @on(Button.Pressed, "#reconnect-activity")
    def _on_reconnect_button(self) -> None:
        item = self.query_one("#activity-list", ListView).highlighted_child
        if isinstance(item, ActivityItem):
            self._connect(item.entry.host, item.entry.location, item.entry.room)
        else:
            self.notify("No activity entry selected.", severity="warning")

    @on(Button.Pressed, "#fullscreen")
    def _on_fullscreen_button(self) -> None:
        self.action_fullscreen()

    @on(Button.Pressed, "#fullscreen-activity")
    def _on_fullscreen_activity_button(self) -> None:
        # Fullscreen the highlighted connection if it's still open;
        # otherwise fall back to the most recently opened one, same as F4.
        item = self.query_one("#activity-list", ListView).highlighted_child
        if isinstance(item, ActivityItem) and item.entry.is_open:
            self._fullscreen_pane(item.entry.pane_id, item.entry.host.name)
        else:
            self.action_fullscreen()

    @on(TabbedContent.TabActivated)
    async def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "activity":
            await self._populate_activity()
        elif event.pane.id == "tags":
            await self._populate_tags(self.query_one("#tag-search", Input).value)

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

    def action_fullscreen(self) -> None:
        """Zoom the most recently opened still-open host pane to the whole
        window (see panes.zoom_pane for how to get back: Ctrl+B then Z, or
        the fullscreened session simply ending)."""
        open_entries = [entry for entry in self.activity if entry.is_open]
        if not open_entries:
            self.notify("No open connection to fullscreen.", severity="warning")
            return
        entry = open_entries[0]
        self._fullscreen_pane(entry.pane_id, entry.host.name)

    def _fullscreen_pane(self, pane_id: str, host_name: str) -> None:
        if self.tmux_session is None:
            self.notify(
                "No tmux session detected, so there's nothing to fullscreen.",
                severity="error",
            )
            return
        try:
            panes.zoom_pane(pane_id)
        except RuntimeError as exc:
            self.notify(f"Couldn't fullscreen {host_name}: {exc}", severity="error")
            return
        self.notify(
            f"{host_name} is fullscreen — Ctrl+B then Z brings the dashboard back.",
            title="Jumpbox",
        )

    def _update_fullscreen_buttons(self) -> None:
        """The two ⛶ buttons are only enabled while at least one
        connection is open - with notifications disabled app-wide, the
        disabled state is the visible cue that there's nothing to zoom."""
        has_open = any(entry.is_open for entry in self.activity)
        self.query_one("#fullscreen", Button).disabled = not has_open
        self.query_one("#fullscreen-activity", Button).disabled = not has_open

    @work
    async def action_quick_connect(self) -> None:
        result = await self.push_screen_wait(
            QuickConnectDialog(self.locations, self.history)
        )
        if result is not None:
            location, room, host = result
            self._connect(host, location, room)

    async def action_refresh(self) -> None:
        self.locations, self.tag_vocabulary = storage.load()
        self.config = storage.load_config()
        self.history = storage.load_history()
        self._current_location = None
        self._current_room = None
        self.query_one("#location-search", Input).value = ""
        await self._populate_locations()
        await self._populate_tags(self.query_one("#tag-search", Input).value)
        if self.config.probe_interval > 0:
            self._start_status_probe()
        self.notify("Inventory refreshed.", title="Jumpbox")

    # ------------------------------------------------------------- add/remove
    def _save(self) -> None:
        storage.save(self.locations, self.tag_vocabulary)

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

    async def _edit_selected_tree_node(self) -> None:
        """Edit whichever location or room the tree cursor is on. The form
        returns a *new* frozen object carrying the old rooms/hosts lists,
        so everything nested inside survives a rename untouched."""
        node = self.query_one("#locations", Tree).cursor_node
        if node is None:
            self.notify("Nothing selected to edit.", severity="warning")
            return

        if isinstance(node.data, Location):
            location = node.data
            result = await self.push_screen_wait(LocationFormDialog(existing=location))
            if result is None:
                return
            self.locations[self.locations.index(location)] = result
            if self._current_location is location:
                self._current_location = result
            self._save()
            await self._populate_locations(self.query_one("#location-search", Input).value)
            self.notify(f"Updated location '{result.name}'.", title="Jumpbox")

        elif isinstance(node.data, Room):
            room = node.data
            parent_location = self._node_location(node)
            if parent_location is None:
                return
            result = await self.push_screen_wait(
                RoomFormDialog(parent_location.name, existing=room)
            )
            if result is None:
                return
            parent_location.rooms[parent_location.rooms.index(room)] = result
            if self._current_room is room:
                self._current_room = result
                self.query_one("#hosts-pane").border_title = (
                    f"HOSTS  ·  {parent_location.name} → {result.name}"
                )
            self._save()
            await self._populate_locations(self.query_one("#location-search", Input).value)
            self.notify(f"Updated room '{result.name}'.", title="Jumpbox")

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
            await self._populate_tags(self.query_one("#tag-search", Input).value)
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
            await self._populate_tags(self.query_one("#tag-search", Input).value)
            self.notify(f"Deleted room '{room.name}'.", title="Jumpbox")

    async def _add_host(self) -> None:
        if self._current_room is None:
            self.notify("Select a room first.", severity="warning")
            return
        room = self._current_room
        result = await self.push_screen_wait(
            HostFormDialog(room.name, self.tag_vocabulary, config=self.config)
        )
        if result is None:
            return
        room.hosts.append(result)
        self._save()
        await self._populate_hosts(self.query_one("#host-search", Input).value)
        await self._populate_locations(self.query_one("#location-search", Input).value)
        await self._populate_tags(self.query_one("#tag-search", Input).value)
        self.notify(f"Added host '{result.name}' to {room.name}.", title="Jumpbox")

    async def _edit_host(self) -> None:
        host = self._highlighted_host()
        room = self._current_room
        if host is None or room is None or host not in room.hosts:
            self.notify("No host selected to edit.", severity="warning")
            return
        result = await self.push_screen_wait(
            HostFormDialog(
                room.name, self.tag_vocabulary, existing=host, config=self.config
            )
        )
        if result is None:
            return
        room.hosts[room.hosts.index(host)] = result
        self._save()
        await self._populate_hosts(self.query_one("#host-search", Input).value)
        await self._populate_locations(self.query_one("#location-search", Input).value)
        await self._populate_tags(self.query_one("#tag-search", Input).value)
        self.notify(f"Updated host '{result.name}'.", title="Jumpbox")

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
        await self._populate_tags(self.query_one("#tag-search", Input).value)
        self.notify(f"Deleted host '{host.name}'.", title="Jumpbox")

    async def _add_tag(self) -> None:
        result = await self.push_screen_wait(TagFormDialog(self.tag_vocabulary))
        if result is None:
            return
        self.tag_vocabulary = sorted(set(self.tag_vocabulary) | {result})
        self._save()
        await self._populate_tags(self.query_one("#tag-search", Input).value)
        self.notify(f"Added tag '#{result}'.", title="Jumpbox")

    async def _delete_tag(self) -> None:
        tag = self._current_tag
        if tag is None:
            self.notify("Select a tag first.", severity="warning")
            return
        affected = [
            (room, host)
            for location in self.locations
            for room in location.rooms
            for host in room.hosts
            if tag in host.tags
        ]
        question = f"Are you sure you want to delete tag '#{tag}'?"
        if affected:
            question += (
                f" It will be removed from {len(affected)} "
                f"host{'s' if len(affected) != 1 else ''}."
            )
        confirmed = await self.push_screen_wait(ConfirmDialog(question))
        if not confirmed:
            return
        self.tag_vocabulary = [t for t in self.tag_vocabulary if t != tag]
        for room, host in affected:
            room.hosts[room.hosts.index(host)] = replace(
                host, tags=tuple(t for t in host.tags if t != tag)
            )
        self._save()
        await self._populate_locations(self.query_one("#location-search", Input).value)
        await self._populate_hosts(self.query_one("#host-search", Input).value)
        await self._populate_tags(self.query_one("#tag-search", Input).value)
        self.notify(f"Deleted tag '#{tag}'.", title="Jumpbox")

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
                f"{host.name} is OFFLINE — opening a pane anyway.",
                title="Heads up",
                severity="warning",
            )

        if self.tmux_session is None:
            self.notify(
                "No tmux session detected, so there's nowhere to open a new pane.",
                title="Jumpbox",
                severity="error",
            )
            return

        # The first host splits off Jumpbox's own pane, to the right; every
        # host after that splits off the previously opened host pane,
        # below it - so the right-hand column just grows downward.
        open_entries = [entry for entry in self.activity if entry.is_open]
        if open_entries:
            # `self.activity` is newest-first, so the first still-open
            # entry is the most recently opened pane.
            target_pane = open_entries[0].pane_id
            stacked = True
        else:
            target_pane = self._jumpbox_pane_id
            stacked = False

        try:
            new_pane_id = panes.open_pane(
                target_pane,
                connect_command(host, self.config.ssh_options),
                stacked=stacked,
            )
        except RuntimeError as exc:
            self.notify(f"Couldn't open a pane for {host.name}: {exc}", severity="error")
            return

        self.activity.insert(0, ActivityEntry(new_pane_id, host, location, room, datetime.now()))
        self._trim_activity()
        self.history = storage.record_connection(host.name)
        self._show_detail(self._highlighted_host())
        self._update_fullscreen_buttons()
        self.run_worker(self._populate_activity())
        self.notify(f"Opened {host.name} in a new pane.", title="Jumpbox")

    # ------------------------------------------------------------ live status
    def _start_status_probe(self) -> None:
        """Kick one background reachability sweep of every host's ssh port,
        unless one is already running (a slow network can't stack them)."""
        if self._probe_running or not self.locations:
            return
        self.run_worker(self._probe_all_hosts(), group="status-probe")

    async def _probe_all_hosts(self) -> None:
        """TCP-probe every host concurrently (bounded) and repaint whatever
        actually changed. Statuses live only in memory until the next
        save() - a sweep never writes the inventory file by itself.

        `Status` used to be a decorative value set once at add-time; this
        is what makes the green/yellow/red dots mean something: reachable
        from this box right now, host up but port refusing, or no answer.
        """
        self._probe_running = True
        try:
            targets = [
                (room, host)
                for location in self.locations
                for room in location.rooms
                for host in room.hosts
            ]
            if not targets:
                return
            semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)

            async def probe_one(host: Host) -> Status:
                async with semaphore:
                    return await probe(host.address, host.port, self.config.probe_timeout)

            statuses = await asyncio.gather(
                *(probe_one(host) for _room, host in targets)
            )
            changed = False
            for (room, host), status in zip(targets, statuses):
                if status is host.status:
                    continue
                try:
                    # The inventory may have been edited mid-sweep; find the
                    # host by identity and skip it if it's gone.
                    index = room.hosts.index(host)
                except ValueError:
                    continue
                room.hosts[index] = replace(host, status=status)
                changed = True
            if changed:
                await self._populate_locations(
                    self.query_one("#location-search", Input).value
                )
                await self._populate_hosts(self.query_one("#host-search", Input).value)
                await self._populate_tags(self.query_one("#tag-search", Input).value)
        finally:
            self._probe_running = False

    def _trim_activity(self) -> None:
        """Cap closed history at MAX_ACTIVITY_ENTRIES, oldest closed entry
        first to go - still-open entries are never trimmed, no matter how
        many of them there are."""
        kept: list[ActivityEntry] = []
        closed_kept = 0
        for entry in self.activity:
            if entry.is_open:
                kept.append(entry)
            elif closed_kept < MAX_ACTIVITY_ENTRIES:
                kept.append(entry)
                closed_kept += 1
        self.activity = kept

    async def _reconcile_activity(self) -> None:
        """The only way a session ends is from inside its own pane - typing
        `exit`, or the connection just dropping - so this is the only thing
        that ever marks an Activity row closed: poll which panes tmux still
        actually has, and close whichever tracked ones aren't in that set
        any more (the row stays, just no longer "● OPEN")."""
        open_entries = [entry for entry in self.activity if entry.is_open]
        if self.tmux_session is None or not open_entries:
            return
        alive = panes.live_pane_ids(self._window_id)
        if all(entry.pane_id in alive for entry in open_entries):
            return
        now = datetime.now()
        for entry in open_entries:
            if entry.pane_id not in alive:
                entry.closed_at = now
        self._trim_activity()
        self._update_fullscreen_buttons()
        await self._populate_activity()
