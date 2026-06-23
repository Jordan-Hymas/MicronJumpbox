# Data model & persistence

## The hierarchy

```
Location  (a site/building, e.g. "A14")
 └── Room    (a sub-location, e.g. "MDF")
      └── Host   (a single SSH target, e.g. "us1-b14-sw1")
```

Three frozen dataclasses in `data.py`:

```python
class Status(Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"

@dataclass(frozen=True)
class Host:
    name: str
    address: str
    username: str
    port: int = 22
    description: str = ""
    os: str = "Linux"
    status: Status = Status.ONLINE
    tags: tuple[str, ...] = ()

    @property
    def target(self) -> str:        # "user@host" form for the ssh command line
        return f"{self.username}@{self.address}"

@dataclass(frozen=True)
class Room:
    name: str
    description: str
    hosts: list[Host]

@dataclass(frozen=True)
class Location:
    name: str
    description: str
    rooms: list[Room]
    icon: str = "🏢"
```

`frozen=True` means a `Host`/`Room`/`Location` is never mutated in place -
adding a host means appending a *new* `Host` to a room's `hosts` list, not
editing an existing one's fields. The lists themselves (`rooms`, `hosts`)
are regular mutable lists, so structural changes (add/delete) are plain
list operations; the leaf objects are immutable values.

`Status` exists purely for the colored dot/label in the UI
(`#50fa7b`/`#f1fa8c`/`#ff5555` for online/degraded/offline in `app.py`'s
`STATUS_COLOR`) - it isn't a live health check. Nothing in Jumpbox pings a
host to determine its status; you set it when adding the host (the Add
Host form always creates new hosts as `ONLINE` - there's no UI to change
status after the fact, only to delete and re-add).

`online` / `total_hosts` on `Room`/`Location` are computed properties
(`sum(1 for h in self.hosts if h.status is Status.ONLINE)`, etc.) - they're
recalculated from the current `Status` values every time they're read, not
cached anywhere.

## Where it lives in memory

`JumpboxApp.locations: list[Location]` (in `app.py`) is the single source
of truth while the app is running. Everything else - the locations tree,
the hosts list, the detail panel - is a *view* rebuilt from this list:

- `_populate_locations()` rebuilds the whole `Tree` widget from
  `self.locations` (optionally fuzzy-filtered), every time the inventory
  changes or the location search box changes.
- `_populate_hosts()` rebuilds the `ListView` from whichever `Room` is
  currently selected (`self._current_room`), again optionally filtered.

There's no separate "model" object with change notifications - mutating
`self.locations` (or a `Room`'s `.hosts` list, etc.) and then calling the
relevant `_populate_*()` method is the entire update mechanism. This is
simple specifically *because* the inventory is small (tens to low hundreds
of hosts) - rebuilding the whole tree on every change is cheap at that
scale and avoids a whole class of "did I forget to update this one view"
bugs that incremental diffing would risk.

## Persistence (`storage.py`)

The in-memory list is mirrored to a JSON file:

```
~/.jumpbox/inventory.json          (or $JUMPBOX_DATA_DIR/inventory.json)
```

```json
{
  "version": 1,
  "saved_at": "2026-06-21T04:00:00+00:00",
  "tags": ["access-point", "core-switch", "distribution-switch", "firewall", "router", "switch"],
  "locations": [
    {
      "name": "A14",
      "description": "Fab + core datacenter building",
      "icon": "🏭",
      "rooms": [
        {
          "name": "MDF",
          "description": "Building A14 main distribution frame",
          "hosts": [
            {
              "name": "us1-b14-sw1",
              "address": "10.14.1.2",
              "username": "netadmin",
              "port": 22,
              "description": "Fab 1 access switch",
              "os": "Cisco IOS-XE 17.9",
              "status": "online",
              "tags": ["switch"]
            }
          ]
        }
      ]
    }
  ]
}
```

Key behaviors, all in `storage.py`:

- **First run**: `inventory.json` doesn't exist yet → `load()` seeds it
  from `data.py`'s built-in demo data (five buildings' worth of network
  gear - switches, APs, routers, firewalls - one per `Status`/`Host` field)
  and writes it out immediately. From then on the file is authoritative -
  the demo seed is only ever used again if the file goes missing entirely.
- **Every add/delete** calls `_save()` (`app.py`), which writes the
  **entire** inventory back out, not a diff. `save()` writes to a
  `.tmp` sibling file first and `os.replace()`s it into place - that
  rename is atomic at the filesystem level, so a crash mid-write can
  never leave a half-written, corrupt `inventory.json`.
- **A corrupted file** (hand-edited badly, truncated, whatever) is caught
  by a broad `except (json.JSONDecodeError, KeyError, TypeError, ValueError)`
  around the parse. Instead of silently discarding it, `load()` renames it
  aside to `inventory.json.bad-<timestamp>` and falls back to a fresh demo
  seed - so a bad edit degrades to "you're back to demo data" rather than
  "your data is just gone with no trace."
- **F5 / Refresh** re-reads the file from disk into `self.locations` and
  rebuilds the views. It does *not* reset to the demo seed - that only
  happens on a missing or corrupt file.
- **The tag vocabulary** (`load()`'s second `Inventory` field) is the
  Tags tab's prebuilt list - managed from that tab's own "+" menu (Add
  Tag / Delete Selected), independent of which tags any host actually
  has. The Add Host form's tag picker only offers tags from this list.
  Deleting one strips it from every host that had it, not just the
  vocabulary. Loading a file saved before this existed (no `"tags"` key)
  derives a starting vocabulary from `data.py`'s `DEFAULT_TAGS` unioned
  with whatever tags hosts already carry, so nothing in use disappears.
- **`$JUMPBOX_DATA_DIR`** overrides the `~/.jumpbox` directory entirely.
  This exists for tests (`tests/smoke.py` points it at a fresh temp
  directory before anything else imports `jumpbox`, so the test suite
  never touches a real user's saved inventory) but works the same way for
  a real deployment if you want the data file somewhere other than the
  Jumpbox user's home directory.

### Per-machine, not shared

The inventory file lives on whichever machine the Jumpbox *process* is
running on - which, per [ARCHITECTURE.md](ARCHITECTURE.md), is the
bastion. If your whole team SSHes into one shared bastion and runs Jumpbox
there, you're all reading and writing that **one** machine's file - which
is effectively a shared inventory already, no extra sync needed. Anyone
who adds a host, anyone else sees it next time they hit F5 or relaunch.
There's currently no per-user inventory or access control - everyone who
can run Jumpbox on that box can add, delete, and see every host in it.

## Search

`fuzzy.py` is a small, dependency-free subsequence matcher (`score()`):
a query matches if every character appears in order in the target string
(not necessarily contiguous), with bonus weight for consecutive matches
and matches right after a word boundary (` -_./@:`), and a small penalty
for gaps between matched characters. `filter_items()` applies this to a
list of items via a `key` function and returns them sorted best-match
first, original order preserved for ties.

What gets matched against:

- Locations: `Location.search_text` → `"{name} {description}"`
- Hosts: `Host.search_text` → `"{name} {address} {username} {os} {tag...}"`

So a host's tags, OS, and address are all searchable, not just its name -
typing "rhel" or an IP fragment narrows the host list exactly like typing
part of the hostname would.
