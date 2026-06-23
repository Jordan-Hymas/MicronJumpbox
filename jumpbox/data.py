"""In-memory inventory of locations, rooms, and hosts.

This is placeholder/demo data. Later it can be swapped for a real source
(a YAML/JSON file, an API, an LDAP query, etc.) without touching the UI:
just make `load_inventory()` return the same `Location`/`Room`/`Host` objects.

Hierarchy: a Location (a site/building, e.g. "A14") contains Rooms (e.g.
"MDF"), and each Room contains Hosts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(Enum):
    """Connection status used to color a host's name/status text in the UI."""

    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"

    @property
    def label(self) -> str:
        return self.value.upper()


@dataclass(frozen=True)
class Host:
    """A single SSH target."""

    name: str
    address: str
    username: str
    port: int = 22
    description: str = ""
    os: str = "Linux"
    status: Status = Status.ONLINE
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def target(self) -> str:
        """`user@host` form used for the SSH command line."""
        return f"{self.username}@{self.address}"

    @property
    def search_text(self) -> str:
        """Everything we let the fuzzy finder match against."""
        return " ".join(
            [self.name, self.address, self.username, self.os, *self.tags]
        )


@dataclass(frozen=True)
class Room:
    """A building room within a location (e.g. "Datacenter 1", "Room 2")."""

    name: str
    description: str
    hosts: list[Host]

    @property
    def online(self) -> int:
        return sum(1 for h in self.hosts if h.status is Status.ONLINE)


@dataclass(frozen=True)
class Location:
    """A site/building (e.g. a floor or lab), made up of rooms."""

    name: str
    description: str
    rooms: list[Room]
    icon: str = "🏢"

    @property
    def online(self) -> int:
        return sum(room.online for room in self.rooms)

    @property
    def total_hosts(self) -> int:
        return sum(len(room.hosts) for room in self.rooms)

    @property
    def search_text(self) -> str:
        return f"{self.name} {self.description}"

    @property
    def search_candidates(self) -> list[str]:
        """Every text a search query is allowed to match against: the
        location's own name+description, plus each room's own *name*,
        scored independently rather than concatenated together - so a
        building with several rooms can't rack up a false match just
        because some short query happens to be a coincidental subsequence
        of the combined blob. Room *descriptions* are deliberately left
        out here: ours are repetitive boilerplate ("...main distribution
        frame" on every MDF) that itself contains stray subsequence
        matches for unrelated short queries; room name alone is short and
        specific enough not to. This is what makes searching "IDF" surface
        a building because one of its *rooms* is named IDF, even though
        the building's own name never mentions it - the tree itself
        doesn't filter rooms, matching here just decides whether the
        building shows up at all."""
        return [self.search_text] + [room.name for room in self.rooms]


# The Tags tab's starting vocabulary - what the Add Host form's tag picker
# offers before anyone has added a custom tag of their own. Every demo host
# below is tagged with exactly one of these (its network role), which is
# also why the Tags tab can answer "every switch" or "every AP" in one click.
DEFAULT_TAGS: list[str] = [
    "switch",
    "distribution-switch",
    "core-switch",
    "access-point",
    "router",
    "firewall",
]


def load_inventory() -> list[Location]:
    """Return the demo inventory: five buildings' worth of the network
    gear a network engineer would actually jump to - switches, distribution
    switches, core switches, access points, routers, firewalls - plus two
    real boxes (pve1, Infinitevoid) carried over as-is.

    Hostnames follow the site's own convention: `us1-b{building}-{role}{n}`
    - site (us1) - building number (b14, b12, ...) - role (sw = access
    switch, ds = distribution switch, core = core switch, ap = access
    point, rtr = router, fw = firewall) - an index if there's more than
    one of that role in the building. Each host's tag *is* its role, so
    the Tags tab answers "every switch" or "every AP" across all five
    buildings in one click instead of opening each MDF/IDF room by hand.
    """

    a14 = Location(
        name="A14",
        description="Fab + core datacenter building",
        icon="🏭",
        rooms=[
            Room(
                name="Fab 1",
                description="Production floor network closet",
                hosts=[
                    Host("us1-b14-ap1", "10.14.1.11", "netadmin", description="Fab 1 floor AP - north bay",
                         os="Cisco IOS-XE 17.9", status=Status.ONLINE, tags=("access-point",)),
                    Host("us1-b14-ap2", "10.14.1.12", "netadmin", description="Fab 1 floor AP - south bay",
                         os="Cisco IOS-XE 17.9", status=Status.ONLINE, tags=("access-point",)),
                    Host("us1-b14-sw1", "10.14.1.2", "netadmin", description="Fab 1 access switch",
                         os="Cisco IOS-XE 17.9", status=Status.ONLINE, tags=("switch",)),
                ],
            ),
            Room(
                name="Fab 2",
                description="Production floor network closet",
                hosts=[
                    Host("us1-b14-ap3", "10.14.2.11", "netadmin", description="Fab 2 floor AP",
                         os="Cisco IOS-XE 17.9", status=Status.ONLINE, tags=("access-point",)),
                    Host("us1-b14-sw2", "10.14.2.2", "netadmin", description="Fab 2 access switch",
                         os="Cisco IOS-XE 17.9", status=Status.DEGRADED, tags=("switch",)),
                ],
            ),
            Room(
                name="MDF",
                description="Building A14 main distribution frame",
                hosts=[
                    Host("us1-b14-rtr1", "10.14.3.1", "netadmin", description="A14 edge router",
                         os="Cisco IOS-XE 17.9", status=Status.ONLINE, tags=("router",)),
                    Host("us1-b14-fw1", "10.14.3.2", "netadmin", description="A14 perimeter firewall",
                         os="FortiOS 7.4", status=Status.ONLINE, tags=("firewall",)),
                    Host("us1-b14-core1", "10.14.3.3", "netadmin", description="A14 core switch",
                         os="Cisco IOS-XE 17.9", status=Status.ONLINE, tags=("core-switch",)),
                ],
            ),
            Room(
                name="Datacenter",
                description="Primary datacenter + lab boxes",
                hosts=[
                    Host("us1-b14-ds1", "10.14.4.2", "netadmin", description="Datacenter distribution switch",
                         os="Cisco IOS-XE 17.9", status=Status.ONLINE, tags=("distribution-switch",)),
                    Host("us1-b14-ds2", "10.14.4.3", "netadmin", description="Datacenter distribution switch",
                         os="Cisco IOS-XE 17.9", status=Status.ONLINE, tags=("distribution-switch",)),
                    Host("pve1", "10.10.0.109", "yeti", description="Testing connection from linux server",
                         os="Proxmox", status=Status.ONLINE),
                    Host("Infinitevoid", "10.10.0.123", "yeti", os="Linux", status=Status.ONLINE),
                ],
            ),
        ],
    )

    a12 = Location(
        name="A12",
        description="Fab expansion building",
        icon="🏭",
        rooms=[
            Room(
                name="Fab 3",
                description="Production floor network closet",
                hosts=[
                    Host("us1-b12-ap1", "10.12.1.11", "netadmin", description="Fab 3 floor AP",
                         os="Aruba ArubaOS 8.10", status=Status.ONLINE, tags=("access-point",)),
                    Host("us1-b12-sw1", "10.12.1.2", "netadmin", description="Fab 3 access switch",
                         os="Aruba ArubaOS-CX 10.13", status=Status.ONLINE, tags=("switch",)),
                ],
            ),
            Room(
                name="Fab 4",
                description="Production floor network closet",
                hosts=[
                    Host("us1-b12-ap2", "10.12.2.11", "netadmin", description="Fab 4 floor AP",
                         os="Aruba ArubaOS 8.10", status=Status.OFFLINE, tags=("access-point",)),
                    Host("us1-b12-sw2", "10.12.2.2", "netadmin", description="Fab 4 access switch",
                         os="Aruba ArubaOS-CX 10.13", status=Status.ONLINE, tags=("switch",)),
                ],
            ),
            Room(
                name="IDF",
                description="Building A12 intermediate distribution frame",
                hosts=[
                    Host("us1-b12-ds1", "10.12.3.4", "netadmin", description="A12 distribution switch",
                         os="Aruba ArubaOS-CX 10.13", status=Status.ONLINE, tags=("distribution-switch",)),
                ],
            ),
        ],
    )

    b25 = Location(
        name="B25",
        description="Datacenter + distribution building",
        icon="🏢",
        rooms=[
            Room(
                name="Datacenter",
                description="Core network + distribution gear",
                hosts=[
                    Host("us1-b25-core1", "10.25.1.3", "netadmin", description="B25 core switch",
                         os="Juniper Junos 21.4", status=Status.ONLINE, tags=("core-switch",)),
                    Host("us1-b25-ds1", "10.25.1.4", "netadmin", description="B25 distribution switch",
                         os="Juniper Junos 21.4", status=Status.ONLINE, tags=("distribution-switch",)),
                    Host("us1-b25-ds2", "10.25.1.5", "netadmin", description="B25 distribution switch",
                         os="Juniper Junos 21.4", status=Status.DEGRADED, tags=("distribution-switch",)),
                ],
            ),
            Room(
                name="MDF",
                description="Building B25 main distribution frame",
                hosts=[
                    Host("us1-b25-rtr1", "10.25.2.1", "netadmin", description="B25 edge router",
                         os="Juniper Junos 21.4", status=Status.ONLINE, tags=("router",)),
                    Host("us1-b25-fw1", "10.25.2.2", "netadmin", description="B25 perimeter firewall",
                         os="FortiOS 7.4", status=Status.ONLINE, tags=("firewall",)),
                ],
            ),
        ],
    )

    b35 = Location(
        name="B35",
        description="Office + wiring closets",
        icon="🏢",
        rooms=[
            Room(
                name="IDF",
                description="Building B35 intermediate distribution frame",
                hosts=[
                    Host("us1-b35-sw1", "10.35.1.2", "netadmin", description="B35 office access switch",
                         os="UniFi OS 3.2", status=Status.ONLINE, tags=("switch",)),
                    Host("us1-b35-ap1", "10.35.1.11", "netadmin", description="B35 office AP",
                         os="UniFi OS 3.2", status=Status.ONLINE, tags=("access-point",)),
                ],
            ),
            Room(
                name="MDF",
                description="Building B35 main distribution frame",
                hosts=[
                    Host("us1-b35-rtr1", "10.35.2.1", "netadmin", description="B35 edge router",
                         os="UniFi OS 3.2", status=Status.ONLINE, tags=("router",)),
                ],
            ),
        ],
    )

    c17 = Location(
        name="C17",
        description="Satellite building",
        icon="📡",
        rooms=[
            Room(
                name="MDF",
                description="Building C17 main distribution frame",
                hosts=[
                    Host("us1-b17-sw1", "10.17.1.2", "netadmin", description="C17 access switch",
                         os="Cisco IOS 15.2", status=Status.ONLINE, tags=("switch",)),
                    Host("us1-b17-fw1", "10.17.1.5", "netadmin", description="C17 perimeter firewall",
                         os="FortiOS 7.4", status=Status.OFFLINE, tags=("firewall",)),
                ],
            ),
            Room(
                name="IDF",
                description="Building C17 intermediate distribution frame",
                hosts=[
                    Host("us1-b17-ap1", "10.17.2.11", "netadmin", description="C17 floor AP",
                         os="Cisco IOS 15.2", status=Status.ONLINE, tags=("access-point",)),
                ],
            ),
        ],
    )

    return [a14, a12, b25, b35, c17]
