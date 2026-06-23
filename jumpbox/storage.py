"""Local on-disk persistence for the locations/rooms/hosts inventory.

Stored as JSON in a per-machine app-data folder (not the install directory),
so it survives reinstalls/upgrades and works regardless of where Jumpbox is
deployed. First run seeds the file from the built-in demo inventory in
data.py; after that, this file is the source of truth - every add/delete in
the app writes straight through to it.

Set JUMPBOX_DATA_DIR to point persistence somewhere else (used by tests so
they never touch a real user's saved inventory).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from .data import DEFAULT_TAGS, Host, Location, Room, Status, load_inventory

INVENTORY_FILENAME = "inventory.json"


class Inventory(NamedTuple):
    """What `load()` returns: the locations tree plus the tag vocabulary."""

    locations: list[Location]
    tags: list[str]


def data_dir() -> Path:
    """Per-user app-data folder: ~/.jumpbox (or $JUMPBOX_DATA_DIR override)."""
    override = os.environ.get("JUMPBOX_DATA_DIR")
    directory = Path(override) if override else (Path.home() / ".jumpbox")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def inventory_path() -> Path:
    return data_dir() / INVENTORY_FILENAME


def _host_to_dict(host: Host) -> dict:
    return {
        "name": host.name,
        "address": host.address,
        "username": host.username,
        "port": host.port,
        "description": host.description,
        "os": host.os,
        "status": host.status.value,
        "tags": list(host.tags),
    }


def _host_from_dict(data: dict) -> Host:
    return Host(
        name=data["name"],
        address=data["address"],
        username=data["username"],
        port=data.get("port", 22),
        description=data.get("description", ""),
        os=data.get("os", "Linux"),
        status=Status(data.get("status", "online")),
        tags=tuple(data.get("tags", [])),
    )


def _room_to_dict(room: Room) -> dict:
    return {
        "name": room.name,
        "description": room.description,
        "hosts": [_host_to_dict(host) for host in room.hosts],
    }


def _room_from_dict(data: dict) -> Room:
    return Room(
        name=data["name"],
        description=data.get("description", ""),
        hosts=[_host_from_dict(h) for h in data.get("hosts", [])],
    )


def _location_to_dict(location: Location) -> dict:
    return {
        "name": location.name,
        "description": location.description,
        "icon": location.icon,
        "rooms": [_room_to_dict(room) for room in location.rooms],
    }


def _location_from_dict(data: dict) -> Location:
    return Location(
        name=data["name"],
        description=data.get("description", ""),
        icon=data.get("icon", "🏢"),
        rooms=[_room_from_dict(r) for r in data.get("rooms", [])],
    )


def _derive_tags(locations: list[Location]) -> list[str]:
    """Fallback for files saved before the tag vocabulary existed: start
    from the built-in defaults, plus whatever tags hosts already carry, so
    upgrading never makes an in-use tag vanish from the Tags tab."""
    used = {
        tag
        for location in locations
        for room in location.rooms
        for host in room.hosts
        for tag in host.tags
    }
    return sorted(set(DEFAULT_TAGS) | used)


def save(locations: list[Location], tags: list[str] = ()) -> None:
    """Write the inventory to disk. Atomic: a crash mid-write can't corrupt it."""
    path = inventory_path()
    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "tags": sorted(set(tags)),
        "locations": [_location_to_dict(loc) for loc in locations],
    }
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def load() -> Inventory:
    """Load the inventory from disk, seeding it from the demo data on first run.

    A corrupt file is renamed aside (never silently discarded) rather than
    just replaced, so a bad edit can't quietly destroy real inventory data.
    """
    path = inventory_path()
    if not path.exists():
        locations = load_inventory()
        tags = sorted(DEFAULT_TAGS)
        save(locations, tags)
        return Inventory(locations, tags)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        locations = [_location_from_dict(loc) for loc in payload["locations"]]
        raw_tags = payload.get("tags")
        tags = sorted(set(raw_tags)) if raw_tags is not None else _derive_tags(locations)
        return Inventory(locations, tags)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        backup = path.with_name(f"{path.name}.bad-{datetime.now():%Y%m%d%H%M%S}")
        path.replace(backup)
        locations = load_inventory()
        tags = sorted(DEFAULT_TAGS)
        save(locations, tags)
        return Inventory(locations, tags)
