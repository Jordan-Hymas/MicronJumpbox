"""Bulk CSV import/export for the inventory.

Hand-adding hundreds of hosts through the Add Host dialog doesn't scale;
whatever the real source of truth turns out to be (a CMDB/IPAM/NetBox
export, or a hand-built spreadsheet), it can be exported to CSV - so CSV
is the interchange format here. The UI stays the day-to-day editing tool;
this is the initial-population and periodic-resync method.

Used from the command line (see __main__.py):

    jumpbox template hosts.csv          # write a starter CSV to fill in
    jumpbox import hosts.csv --dry-run  # report what would change, write nothing
    jumpbox import hosts.csv            # merge into the existing inventory
    jumpbox import hosts.csv --replace  # wipe and rebuild from the CSV alone
    jumpbox export hosts.csv            # dump the current inventory back out

Columns (header row required, names case-insensitive, extra columns
ignored): location, room, name, address are required per row; username,
port, os fall back to config.json's defaults; status defaults to online;
description, tags (separated by spaces or ';'), ssh_args (shell-style
quoting), icon are optional.

Merge semantics (the default): hosts match by *name*, case-insensitively,
across the whole inventory - re-importing an updated export refreshes a
matched host's fields in place (blank optional cells keep the existing
value rather than clearing it) and moves it if its location/room changed,
instead of duplicating it. Rows that fail validation are skipped and
reported with their line number and reason - one bad row never aborts the
rest. Locations, rooms, and tags that don't exist yet are created.

Nothing in here writes to disk - import_csv returns a *new*
locations/tags pair plus a report, and the caller decides whether to
storage.save() it (which is exactly what --dry-run doesn't do).
"""

from __future__ import annotations

import csv
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .connect import ssh_args_error
from .data import Host, Location, Room, Status
from .storage import Config

CSV_COLUMNS = [
    "location",
    "room",
    "name",
    "address",
    "username",
    "port",
    "os",
    "status",
    "description",
    "tags",
    "ssh_args",
    "icon",
    "location_description",
    "room_description",
]
REQUIRED_COLUMNS = ("location", "room", "name", "address")

_TEMPLATE_ROWS = [
    {
        "location": "A14",
        "room": "MDF",
        "name": "us1-b14-core1",
        "address": "10.14.3.3",
        "username": "netadmin",
        "port": "22",
        "os": "Cisco IOS-XE 17.9",
        "status": "online",
        "description": "A14 core switch",
        "tags": "core-switch",
        "ssh_args": "",
        "icon": "🏭",
    },
    {
        "location": "B25",
        "room": "Datacenter",
        "name": "us1-b25-fw1",
        "address": "10.25.2.2",
        "username": "netadmin",
        "port": "22",
        "os": "FortiOS 7.4",
        "status": "online",
        "description": "B25 perimeter firewall",
        "tags": "firewall",
        "ssh_args": "-o KexAlgorithms=+diffie-hellman-group14-sha1",
        "icon": "🏢",
    },
]


@dataclass
class ImportReport:
    """What an import did (or, on a dry run, would do)."""

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)
    new_locations: list[str] = field(default_factory=list)
    new_rooms: list[str] = field(default_factory=list)
    new_tags: list[str] = field(default_factory=list)
    total_rows: int = 0
    replace: bool = False

    @property
    def changed(self) -> int:
        return len(self.added) + len(self.updated) + len(self.moved)

    def summary(self) -> str:
        lines = [
            f"{self.total_rows} row{'s' if self.total_rows != 1 else ''} read"
            + (" (replace mode: existing inventory discarded)" if self.replace else ""),
            f"  added:   {len(self.added)} host{'s' if len(self.added) != 1 else ''}",
            f"  updated: {len(self.updated)} host{'s' if len(self.updated) != 1 else ''}"
            + (f" (+{len(self.moved)} moved to a new room)" if self.moved else ""),
            f"  skipped: {len(self.skipped)} row{'s' if len(self.skipped) != 1 else ''}",
        ]
        if self.new_locations:
            lines.append(f"  new locations: {', '.join(self.new_locations)}")
        if self.new_rooms:
            lines.append(f"  new rooms: {', '.join(self.new_rooms)}")
        if self.new_tags:
            lines.append(f"  new tags: {', '.join(self.new_tags)}")
        for line_number, reason in self.skipped:
            lines.append(f"  ! line {line_number}: {reason}")
        return "\n".join(lines)


def _split_tags(text: str) -> tuple[str, ...]:
    return tuple(t for t in re.split(r"[;\s]+", text.strip()) if t)


class _RowError(Exception):
    """One row's reason for being skipped."""


def _cell(row: dict, column: str) -> str:
    # DictReader yields None for cells a short row doesn't have.
    return (row.get(column) or "").strip()


def _parse_host(row: dict, config: Config, existing: Host | None) -> Host:
    """One CSV row -> a Host. On an update (`existing` set), blank optional
    cells keep the existing value; on an add they fall back to config.json's
    defaults. Raises _RowError with the reason if the row can't be used."""
    name = _cell(row, "name")
    address = _cell(row, "address")

    username = _cell(row, "username") or (
        existing.username if existing else config.default_username
    )
    if not username:
        raise _RowError(
            "no username, and no default_username in config.json to fall back to"
        )

    port_text = _cell(row, "port")
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise _RowError(f"invalid port {port_text!r}")
        port = int(port_text)
    else:
        port = existing.port if existing else config.default_port

    status_text = _cell(row, "status").lower()
    if status_text:
        try:
            status = Status(status_text)
        except ValueError:
            valid = ", ".join(s.value for s in Status)
            raise _RowError(f"invalid status {status_text!r} (expected one of: {valid})")
    else:
        status = existing.status if existing else Status.ONLINE

    ssh_args_text = _cell(row, "ssh_args")
    if ssh_args_text:
        try:
            ssh_args = tuple(shlex.split(ssh_args_text))
        except ValueError as exc:
            raise _RowError(f"unparseable ssh_args: {exc}")
        error = ssh_args_error(ssh_args)
        if error:
            raise _RowError(error)
    else:
        ssh_args = existing.ssh_args if existing else ()

    return Host(
        name=name,
        address=address,
        username=username,
        port=port,
        description=_cell(row, "description")
        or (existing.description if existing else ""),
        os=_cell(row, "os") or (existing.os if existing else config.default_os),
        status=status,
        tags=_split_tags(_cell(row, "tags")) or (existing.tags if existing else ()),
        ssh_args=ssh_args,
    )


class _Mutable:
    """A mutable mirror of the Location/Room tree while rows are applied,
    keyed case-insensitively so 'MDF' and 'mdf' in a hand-edited CSV don't
    silently become two rooms. Materialized back to the frozen dataclasses
    at the end."""

    def __init__(self, locations: list[Location]):
        # loc key -> [name, description, icon, {room key -> [name, description, [hosts]]}]
        self.locations: dict[str, list] = {}
        for location in locations:
            rooms = {
                room.name.lower(): [room.name, room.description, list(room.hosts)]
                for room in location.rooms
            }
            self.locations[location.name.lower()] = [
                location.name,
                location.description,
                location.icon,
                rooms,
            ]

    def find_host(self, name: str) -> tuple[str, str, int] | None:
        """(location key, room key, index) of the host with this name."""
        lowered = name.lower()
        for loc_key, (_, _, _, rooms) in self.locations.items():
            for room_key, (_, _, hosts) in rooms.items():
                for index, host in enumerate(hosts):
                    if host.name.lower() == lowered:
                        return loc_key, room_key, index
        return None

    def ensure_location(
        self, name: str, icon: str, description: str, report: ImportReport
    ) -> str:
        """icon/description apply when the location is first created by
        this import; an existing location's own values always win."""
        key = name.lower()
        if key not in self.locations:
            self.locations[key] = [name, description, icon or "🏢", {}]
            report.new_locations.append(name)
        return key

    def ensure_room(
        self, loc_key: str, name: str, description: str, report: ImportReport
    ) -> str:
        rooms = self.locations[loc_key][3]
        key = name.lower()
        if key not in rooms:
            rooms[key] = [name, description, []]
            report.new_rooms.append(f"{self.locations[loc_key][0]} → {name}")
        return key

    def materialize(self) -> list[Location]:
        return [
            Location(
                name=name,
                description=description,
                icon=icon,
                rooms=[
                    Room(name=room_name, description=room_description, hosts=hosts)
                    for room_name, room_description, hosts in rooms.values()
                ],
            )
            for name, description, icon, rooms in self.locations.values()
        ]


def import_csv(
    csv_path: str | Path,
    locations: list[Location],
    tags: list[str],
    *,
    replace: bool = False,
    config: Config | None = None,
) -> tuple[list[Location], list[str], ImportReport]:
    """Apply `csv_path` to the inventory, returning the new
    (locations, tags, report) - the inputs are never mutated, and nothing
    is written to disk (that's the caller's call; see the module docstring).

    Raises ValueError if the file itself is unusable (missing header /
    required columns) - per-*row* problems are skipped and reported
    instead, never fatal.
    """
    config = config or Config()
    report = ImportReport(replace=replace)

    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError("CSV file is empty - no header row")
        # Normalize the header so "Location" / " location " both work.
        reader.fieldnames = [(name or "").strip().lower() for name in reader.fieldnames]
        missing = [c for c in REQUIRED_COLUMNS if c not in reader.fieldnames]
        if missing:
            raise ValueError(
                f"CSV header is missing required column(s): {', '.join(missing)} "
                f"(expected at least: {', '.join(REQUIRED_COLUMNS)})"
            )
        rows = list(reader)

    tree = _Mutable([] if replace else locations)
    new_tags = set(tags)
    seen_names: dict[str, int] = {}

    for row in rows:
        report.total_rows += 1
        line_number = report.total_rows + 1  # +1 for the header row

        missing_cells = [c for c in REQUIRED_COLUMNS if not _cell(row, c)]
        if missing_cells:
            report.skipped.append(
                (line_number, f"missing required value(s): {', '.join(missing_cells)}")
            )
            continue

        name = _cell(row, "name")
        earlier_line = seen_names.get(name.lower())
        if earlier_line is not None:
            report.skipped.append(
                (line_number, f"duplicate of host {name!r} on line {earlier_line}")
            )
            continue

        found = tree.find_host(name)
        existing = None
        if found is not None:
            loc_key, room_key, index = found
            existing = tree.locations[loc_key][3][room_key][2][index]

        try:
            host = _parse_host(row, config, existing)
        except _RowError as exc:
            report.skipped.append((line_number, str(exc)))
            continue

        seen_names[name.lower()] = line_number
        target_loc = tree.ensure_location(
            _cell(row, "location"),
            _cell(row, "icon"),
            _cell(row, "location_description"),
            report,
        )
        target_room = tree.ensure_room(
            target_loc, _cell(row, "room"), _cell(row, "room_description"), report
        )
        label = f"{host.name} ({tree.locations[target_loc][0]} → {tree.locations[target_loc][3][target_room][0]})"

        if found is None:
            tree.locations[target_loc][3][target_room][2].append(host)
            report.added.append(label)
        elif (loc_key, room_key) == (target_loc, target_room):
            tree.locations[loc_key][3][room_key][2][index] = host
            report.updated.append(label)
        else:
            del tree.locations[loc_key][3][room_key][2][index]
            tree.locations[target_loc][3][target_room][2].append(host)
            report.moved.append(label)

        for tag in host.tags:
            if tag not in new_tags:
                new_tags.add(tag)
                report.new_tags.append(tag)

    return tree.materialize(), sorted(new_tags), report


def export_csv(csv_path: str | Path, locations: list[Location]) -> int:
    """Dump the whole inventory to `csv_path` in import_csv's own format
    (a lossless round trip), returning how many hosts were written. Also
    the bulk-*editing* path: export, fix columns in a spreadsheet,
    re-import."""
    count = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for location in locations:
            for room in location.rooms:
                for host in room.hosts:
                    writer.writerow(
                        {
                            "location": location.name,
                            "room": room.name,
                            "name": host.name,
                            "address": host.address,
                            "username": host.username,
                            "port": host.port,
                            "os": host.os,
                            "status": host.status.value,
                            "description": host.description,
                            "tags": ";".join(host.tags),
                            "ssh_args": shlex.join(host.ssh_args),
                            "icon": location.icon,
                            "location_description": location.description,
                            "room_description": room.description,
                        }
                    )
                    count += 1
    return count


def write_template(csv_path: str | Path) -> None:
    """Write a starter CSV with the header and two example rows to edit or
    replace - the fastest way to see exactly what an import expects."""
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(_TEMPLATE_ROWS)
