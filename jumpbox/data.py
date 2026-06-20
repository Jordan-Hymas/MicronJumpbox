"""In-memory inventory of locations, rooms, and hosts.

This is placeholder/demo data. Later it can be swapped for a real source
(a YAML/JSON file, an API, an LDAP query, etc.) without touching the UI:
just make `load_inventory()` return the same `Location`/`Room`/`Host` objects.

Hierarchy: a Location (a site/building, e.g. "Production Floor") contains
Rooms (e.g. "Datacenter 1"), and each Room contains Hosts.
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


def load_inventory() -> list[Location]:
    """Return the demo inventory: two locations, each with two rooms."""

    production = Location(
        name="Production Floor",
        description="Fab-facing production systems",
        icon="🏭",
        rooms=[
            Room(
                name="Datacenter 1",
                description="Primary edge + MES systems",
                hosts=[
                    Host("fab-edge-01", "10.20.0.11", "operator", description="Edge gateway / line A",
                         os="Ubuntu 22.04", status=Status.ONLINE, tags=("gateway", "lineA")),
                    Host("fab-edge-02", "10.20.0.12", "operator", description="Edge gateway / line B",
                         os="Ubuntu 22.04", status=Status.ONLINE, tags=("gateway", "lineB")),
                    Host("mes-app-01", "10.20.4.31", "svc-mes", description="MES application node",
                         os="RHEL 9", status=Status.DEGRADED, tags=("mes", "app")),
                ],
            ),
            Room(
                name="Datacenter 2",
                description="Historian + vision systems",
                hosts=[
                    Host("hist-db-01", "10.20.4.50", "dbadmin", description="Process historian DB",
                         os="RHEL 9", status=Status.ONLINE, tags=("database", "historian")),
                    Host("vision-rig-07", "10.20.9.7", "vision", description="Inspection vision rig",
                         os="Windows Server 2022", status=Status.OFFLINE, tags=("vision", "windows")),
                ],
            ),
        ],
    )

    lab = Location(
        name="Engineering Lab",
        description="R&D sandbox and bench equipment",
        icon="🔬",
        rooms=[
            Room(
                name="Room 1",
                description="Bench + test automation",
                hosts=[
                    Host("bench-pi-01", "192.168.50.21", "pi", description="Bench Raspberry Pi",
                         os="Raspberry Pi OS", status=Status.ONLINE, tags=("bench", "arm")),
                    Host("test-rig-02", "192.168.50.42", "tester", description="Automated test rig",
                         os="Debian 12", status=Status.ONLINE, tags=("test", "automation")),
                    Host("ml-train-01", "192.168.60.10", "research", description="GPU training box",
                         os="Ubuntu 24.04", status=Status.DEGRADED, tags=("gpu", "ml")),
                ],
            ),
            Room(
                name="Room 2",
                description="Build + legacy tooling",
                hosts=[
                    Host("build-srv-01", "192.168.60.30", "builder", description="CI / build server",
                         os="Ubuntu 24.04", status=Status.ONLINE, tags=("ci", "build")),
                    Host("legacy-win-09", "192.168.70.9", "labadmin", description="Legacy tooling host",
                         os="Windows 10 LTSC", status=Status.OFFLINE, tags=("legacy", "windows")),
                ],
            ),
        ],
    )

    return [production, lab]
