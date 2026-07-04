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
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from .data import DEFAULT_TAGS, Host, Location, Room, Status, load_inventory

INVENTORY_FILENAME = "inventory.json"
HISTORY_FILENAME = "history.json"
CONFIG_FILENAME = "config.json"

# How many previous copies of inventory.json survive as inventory.json.1
# (newest) .. .5 (oldest). Every save rotates, so one bad bulk operation -
# or a fat-fingered delete-location - is always undoable by copying a
# numbered backup back over the file and hitting F5.
BACKUP_COUNT = 5


class Inventory(NamedTuple):
    """What `load()` returns: the locations tree plus the tag vocabulary."""

    locations: list[Location]
    tags: list[str]


@dataclass(frozen=True)
class Config:
    """Site-wide defaults, read from ~/.jumpbox/config.json (created with
    these defaults on first run so it's discoverable). Every field is
    optional in the file - anything missing keeps its default here.

    - default_username/default_port/default_os prefill the Add Host form,
      and fill CSV import columns left blank.
    - probe_interval: seconds between live reachability sweeps of every
      host's ssh port (the status dots). 0 turns probing off entirely.
    - probe_timeout: seconds before an unanswered probe counts as OFFLINE.
    - ssh_options: extra ssh arguments applied to *every* connection, e.g.
      ["-o", "ConnectTimeout=5"] - per-host ssh_args come on top of these.
      Same forbidden-option screening as per-host args (see connect.py).
    """

    default_username: str = ""
    default_port: int = 22
    default_os: str = "Linux"
    probe_interval: float = 30.0
    probe_timeout: float = 1.5
    ssh_options: tuple[str, ...] = ()


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
        "ssh_args": list(host.ssh_args),
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
        ssh_args=tuple(data.get("ssh_args", [])),
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


def _rotate_backups(path: Path) -> None:
    """Keep the last BACKUP_COUNT saved states as `<name>.1` (newest) ..
    `<name>.N` (oldest). The live file is *copied* aside, never moved, so
    a crash anywhere in here still leaves `inventory.json` itself intact."""
    if not path.exists():
        return
    for index in range(BACKUP_COUNT - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{index + 1}"))
    shutil.copy2(path, path.with_name(f"{path.name}.1"))


def _write_json(path: Path, payload: object) -> None:
    """Atomic JSON write: a crash mid-write can't corrupt the target file."""
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def save(locations: list[Location], tags: list[str] = ()) -> None:
    """Write the inventory to disk, rotating the previous state into the
    numbered backups first - so any save (a UI edit, a bulk import) can be
    undone by copying `inventory.json.1` back over `inventory.json`."""
    path = inventory_path()
    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "tags": sorted(set(tags)),
        "locations": [_location_to_dict(loc) for loc in locations],
    }
    _rotate_backups(path)
    _write_json(path, payload)


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


# --------------------------------------------------------------- history
def history_path() -> Path:
    return data_dir() / HISTORY_FILENAME


def load_history() -> dict[str, dict]:
    """Per-host connection history: host name -> {"last_connected": ISO
    timestamp, "count": int}. Unlike the inventory, this is disposable
    convenience data - a missing or unreadable file just means an empty
    history, no backup dance."""
    path = history_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (json.JSONDecodeError, ValueError, OSError):
        return {}


def record_connection(host_name: str) -> dict[str, dict]:
    """Bump `host_name`'s history entry (now + count) and persist it;
    returns the updated history so the app can keep using it in memory."""
    history = load_history()
    entry = history.get(host_name) or {}
    history[host_name] = {
        "last_connected": datetime.now(timezone.utc).isoformat(),
        "count": int(entry.get("count", 0)) + 1,
    }
    _write_json(history_path(), history)
    return history


# ---------------------------------------------------------------- config
def config_path() -> Path:
    return data_dir() / CONFIG_FILENAME


def load_config() -> Config:
    """Read ~/.jumpbox/config.json, writing one out with the defaults on
    first run so the knobs are discoverable. Missing keys keep their
    defaults; an unparseable file just means all-defaults (it's left in
    place untouched for the user to fix, never renamed aside - unlike the
    inventory, nothing is lost by ignoring it)."""
    path = config_path()
    defaults = Config()
    if not path.exists():
        _write_json(
            path,
            {
                "default_username": defaults.default_username,
                "default_port": defaults.default_port,
                "default_os": defaults.default_os,
                "probe_interval": defaults.probe_interval,
                "probe_timeout": defaults.probe_timeout,
                "ssh_options": list(defaults.ssh_options),
            },
        )
        return defaults
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return defaults
        return Config(
            default_username=str(payload.get("default_username", defaults.default_username)),
            default_port=int(payload.get("default_port", defaults.default_port)),
            default_os=str(payload.get("default_os", defaults.default_os)),
            probe_interval=float(payload.get("probe_interval", defaults.probe_interval)),
            probe_timeout=float(payload.get("probe_timeout", defaults.probe_timeout)),
            ssh_options=tuple(
                str(opt) for opt in payload.get("ssh_options", [])
            ),
        )
    except (json.JSONDecodeError, TypeError, ValueError, OSError):
        return defaults
