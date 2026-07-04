"""Headless smoke test for the Jumpbox app.

Runs the real Textual app with no visible terminal (via run_test) and exercises
the core flow: theme, the locations -> rooms tree (with no duplicated host
leaves), fuzzy search, room switching, single-click-preview vs
double-click-connect on hosts, opening hosts as tmux panes (a real tmux is
never required - `panes.open_pane`/`live_pane_ids` are monkeypatched so this
runs anywhere), the Tags tab (browsing/filtering hosts by tag across every
location, and connecting to one with its own location/room rather than
whatever's selected on the Dashboard), that a connection only ever closes
from inside its own pane (no Close button - reconciliation is what notices
and marks the Activity row closed, without ever deleting its history), that
two logins (even two people sharing one OS account) always get distinct tmux
session names, that connect_command() is a single direct hop with no
needless jump, that forwarded-SSH-agent detection reflects a real socket
and not just a leftover env var, the timestamped Activity tab, and that the
ssh command run in each pane can't be hijacked by malicious host fields.

JUMPBOX_DATA_DIR is pointed at a throwaway temp folder before anything
imports jumpbox, so this test never reads or writes a real user's saved
inventory.

Run from the project root:  python -m tests.smoke
"""

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="jumpbox-smoke-")
os.environ["JUMPBOX_DATA_DIR"] = _TEST_DATA_DIR

# Live-status probing is exercised on its own further down, against real
# local sockets (see the connect.probe section). Inside the UI run it's
# turned off via config.json: the demo inventory's 10.x addresses aren't
# reachable here, and a background sweep rebuilding the tree mid-test
# would race assertions that hold references to tree nodes.
Path(_TEST_DATA_DIR, "config.json").write_text(
    json.dumps({"probe_interval": 0}), encoding="utf-8"
)

from textual.widgets import Button, Input, Select, TabbedContent, Tree

import jumpbox.app as appmod
from jumpbox import storage
from jumpbox.app import ActivityItem, HostItem, JumpboxApp, TagHostItem, TagItem
from jumpbox.connect import connect_command
from jumpbox.data import DEFAULT_TAGS, Host, Location, Room, Status, load_inventory


def _count(app, kind) -> int:
    return len(app.query(kind))


async def _wait_until(pilot, predicate, attempts: int = 20) -> None:
    """Poll `predicate` across several pauses - workers/modals don't always
    land within a single pilot.pause()."""
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause()
    raise AssertionError(f"condition not met after {attempts} pauses")


async def main() -> None:
    # No real tmux involved in this run - fake the one call that would
    # otherwise shell out, and record what it was asked to do. Backed by
    # the same `live_panes` set live_pane_ids() reports from, so the *real*
    # background reconciliation timer (it keeps running throughout this
    # whole test, same as in the real app) never touches actual tmux.
    opened: list[tuple[str, str, bool]] = []
    live_panes: set[str] = set()
    zoomed: list[str] = []

    def fake_open_pane(target_pane: str, command: str, *, stacked: bool) -> str:
        pane_id = f"%{len(opened) + 1}"
        opened.append((target_pane, command, stacked))
        live_panes.add(pane_id)
        return pane_id

    appmod.panes.open_pane = fake_open_pane
    appmod.panes.live_pane_ids = lambda window_id: set(live_panes)
    appmod.panes.zoom_pane = lambda pane_id: zoomed.append(pane_id)

    app = JumpboxApp()
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()

        assert app.tmux_session is None, (
            "this headless run has no real $TMUX - Jumpbox must detect that "
            "rather than assume a session exists"
        )

        assert app.theme == "dracula", f"expected dracula theme, got {app.theme}"

        tree = app.query_one("#locations", Tree)
        location_nodes = tree.root.children
        assert len(location_nodes) == 5, f"expected 5 locations, got {len(location_nodes)}"
        assert all(isinstance(n.data, Location) for n in location_nodes)

        room_nodes = location_nodes[0].children
        assert len(room_nodes) == 4, f"expected 4 rooms, got {len(room_nodes)}"
        assert all(isinstance(n.data, Room) for n in room_nodes)
        assert all(len(n.children) == 0 for n in room_nodes), (
            "rooms must not nest host leaves - hosts only live in the hosts list"
        )

        # Location search must also match on *room* names, not just the
        # location's own name/description - "IDF" isn't a building, it's a
        # room inside three of them.
        location_search = app.query_one("#location-search", Input)
        location_search.value = "IDF"
        await pilot.pause()
        idf_locations = {n.data.name for n in tree.root.children}
        assert idf_locations == {"A12", "B35", "C17"}, (
            f"expected exactly the buildings with an IDF room, got {idf_locations}"
        )

        # Clearing rebuilds the tree from scratch, so anything holding a
        # reference to a pre-search node (location_nodes, room_nodes) needs
        # to be re-fetched rather than reused.
        location_search.value = ""
        await pilot.pause()
        location_nodes = tree.root.children
        assert len(location_nodes) == 5, "clearing search should restore every location"
        room_nodes = location_nodes[0].children

        # Narrowing to "IDF" moved the selected room to one of A12/B35/C17
        # (the closest still-visible room) - move back to A14's first room
        # explicitly so the rest of this test sees the state it expects.
        tree.move_cursor(room_nodes[0])
        await pilot.pause()

        # First room (Fab 1) has 3 hosts.
        hosts = _count(app, HostItem)
        assert hosts == 3, f"expected 3 hosts in first room, got {hosts}"

        detail = app.query_one("#detail-body").render()
        assert str(detail).strip(), "detail panel should not be empty"

        # Fuzzy-filter narrows without zeroing out.
        search = app.query_one("#host-search")
        search.value = "ap"
        await pilot.pause()
        filtered = _count(app, HostItem)
        assert 1 <= filtered < 3, f"fuzzy search should narrow results, got {filtered}"

        search.value = ""
        await pilot.pause()
        assert _count(app, HostItem) == 3, "clearing search should restore hosts"

        # Switch room via the tree (second room node) -> different host set.
        tree.move_cursor(room_nodes[1])
        await pilot.pause()
        assert _count(app, HostItem) == 2, "second room (Fab 2) should have 2 hosts"
        assert app.query_one("#host-search").value == "", "search resets per room"

        # --- single click previews; double click needs a tmux session ----
        host_items = list(app.query(HostItem))
        target = host_items[1]
        target_host = target.host
        assert app._highlighted_host() is not target_host, "test needs a non-default target"
        expected_command = connect_command(target_host)
        assert expected_command == (
            f"ssh -p 22 {shlex.quote(target_host.target)}"
        ), f"unexpected connect command: {expected_command!r}"

        await pilot.click(target)
        await pilot.pause()
        assert app._highlighted_host() is target_host, "single click should move the preview"
        assert target_host.name in str(app.query_one("#detail-body").render())
        assert not app.activity, "single click must not add an activity entry"

        # With no real tmux session (the real state in this headless run),
        # double click must refuse - never silently no-op, never crash.
        await pilot.click(target, times=2)
        await pilot.pause()
        assert not opened, "no tmux session means there's nowhere to open a pane"
        assert not app.activity, "a refused connect must not be logged"

        # Nothing is connected yet, so there's nothing to fullscreen - the
        # ⛶ buttons must be disabled and F4 must be a safe no-op.
        assert app.query_one("#fullscreen", Button).disabled, (
            "the Dashboard ⛶ button must start disabled"
        )
        assert app.query_one("#fullscreen-activity", Button).disabled
        app.action_fullscreen()
        assert not zoomed, "fullscreen with nothing open must not zoom anything"

        # Fake having a real tmux session, the same way on_mount would set
        # it up after a successful real detection.
        app.tmux_session = "fake-session"
        app._jumpbox_pane_id = "%0"
        app._window_id = "@0"

        await pilot.click(target, times=2)
        await pilot.pause()
        assert len(opened) == 1, f"double click should open exactly one pane, got {len(opened)}"
        first_target, first_command, first_stacked = opened[0]
        assert first_target == "%0", "the first host pane must split off Jumpbox's own pane"
        assert first_stacked is False, "the first host pane must split sideways, not stacked"
        assert first_command == expected_command
        assert len(app.activity) == 1
        assert app.activity[0].host is target_host
        assert app.activity[0].pane_id == "%1"
        assert app.activity[0].is_open, "a fresh connection should start open"

        # Connecting must persist per-host history (last connected / count) -
        # it survives restarts (history.json) and shows in the detail panel.
        assert app.history.get(target_host.name, {}).get("count", 0) == 1, (
            "a connect should record the host into the persisted history"
        )
        assert storage.load_history().get(target_host.name), (
            "history must round-trip through history.json, not just memory"
        )
        assert "last connected" in str(app.query_one("#detail-body").render()), (
            "the detail panel should show when this host was last connected"
        )

        # A second host must stack *below the first host pane*, not split
        # off Jumpbox's pane again.
        other_target = host_items[0]
        other_host = other_target.host
        await pilot.click(other_target, times=2)
        await pilot.pause()
        assert len(opened) == 2
        second_target, _, second_stacked = opened[1]
        assert second_target == "%1", "the second host pane must split off the first host pane"
        assert second_stacked is True, "every host after the first must stack below the last"
        assert len(app.activity) == 2
        assert app.activity[0].host is other_host, "activity is newest first"

        # ⛶ Fullscreen: enabled now that connections are open, and F4 (or
        # the button) zooms the tmux pane of the *most recent* open one.
        assert not app.query_one("#fullscreen", Button).disabled, (
            "the ⛶ button must enable once a connection is open"
        )
        app.action_fullscreen()
        assert zoomed == ["%2"], (
            f"fullscreen should zoom the most recently opened pane, got {zoomed}"
        )
        app.query_one("#fullscreen", Button).press()
        await pilot.pause()
        assert zoomed == ["%2", "%2"], "the Dashboard ⛶ button does the same as F4"

        # --- Tags tab: browse/filter hosts by tag across the *whole*
        # inventory, not just whatever room is selected on the Dashboard ---
        app.query_one(TabbedContent).active = "tags"
        await pilot.pause()
        tag_counts = {item.tag: item.count for item in app.query(TagItem)}
        assert tag_counts.get("core-switch") == 2, (
            f"expected 2 hosts tagged 'core-switch' across both locations, got {tag_counts}"
        )

        tag_search = app.query_one("#tag-search", Input)
        tag_search.value = "core-switch"
        await pilot.pause()
        assert [item.tag for item in app.query(TagItem)] == ["core-switch"], (
            "fuzzy search should narrow the tag list down to the match"
        )

        tag_hosts = list(app.query(TagHostItem))
        assert len(tag_hosts) == 2, f"expected 2 hosts tagged core-switch, got {len(tag_hosts)}"
        assert {item.host.name for item in tag_hosts} == {"us1-b14-core1", "us1-b25-core1"}, (
            "the core-switch tag should pull hosts from every location, not just one"
        )
        assert len({item.location.name for item in tag_hosts}) == 2, (
            "these two hosts must come from two different locations"
        )

        # Double-clicking a Tags-tab row connects it using *that* host's own
        # location/room, not whatever happens to be selected on the Dashboard.
        core_switch_target = tag_hosts[0]
        await pilot.click(core_switch_target, times=2)
        await pilot.pause()
        assert len(opened) == 3, "double click on a Tags-tab host should connect it"
        assert app.activity[0].host is core_switch_target.host
        assert app.activity[0].location is core_switch_target.location
        assert app.activity[0].room is core_switch_target.room

        tag_search.value = ""
        await pilot.pause()

        # --- Activity tab lists every connection, newest first -----------
        app.query_one(TabbedContent).active = "activity"
        await pilot.pause()
        activity_items = list(app.query(ActivityItem))
        assert len(activity_items) == 3, f"expected 3 activity rows, got {len(activity_items)}"
        assert all("OPEN" in str(item.query_one("Static").render()) for item in activity_items), (
            "every connection here is still open, each row should say so"
        )

        # There's no Close button - a connection only ever ends from inside
        # its own pane (typed `exit`, or the connection just dropping),
        # simulated here by tmux simply no longer reporting that one pane.
        oldest_pane_id = app.activity[-1].pane_id
        live_panes.discard(oldest_pane_id)
        await app._reconcile_activity()
        await pilot.pause()
        assert app.activity[-1].closed_at is not None, "the closed pane's entry should close"
        assert all(e.is_open for e in app.activity[:-1]), "ending one must not touch the others"
        assert len(app.activity) == 3, "a closed entry stays as history, never removed"
        rendered = [str(item.query_one("Static").render()) for item in app.query(ActivityItem)]
        assert sum("OPEN" in r for r in rendered) == 2, "exactly 2 rows should still say OPEN"

        # And once every remaining pane is gone too, all three stay as
        # history - Activity never empties out just because panes closed.
        live_panes.clear()
        await app._reconcile_activity()
        await pilot.pause()
        assert all(not e.is_open for e in app.activity), "every entry should now be closed"
        assert len(app.activity) == 3, "closed entries remain as history"
        rendered = [str(item.query_one("Static").render()) for item in app.query(ActivityItem)]
        assert not any("OPEN" in r for r in rendered)
        assert app.query_one("#fullscreen", Button).disabled, (
            "the ⛶ buttons must disable again once every connection closes"
        )
        assert app.query_one("#fullscreen-activity", Button).disabled
        stamp = app.activity[0].opened_at.strftime("%Y-%m-%d %I:%M:%S %p")
        assert stamp in rendered[0]

        # Top-right exit button exists and is wired to quit.
        quit_btn = app.query_one("#quit-btn", Button)
        assert quit_btn.variant == "error"

        app.query_one(TabbedContent).active = "dashboard"
        await pilot.pause()

        def _modal_open():
            return len(app.screen_stack) > 1

        def _modal_closed():
            return len(app.screen_stack) == 1

        # App.query_one always targets the *default* screen by design, never
        # a pushed modal - app.screen.query_one reaches the active one.

        # --- add location: drives the real modal form end to end ---------
        before_locations = len(app.locations)
        app.run_worker(app._add_location())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#f-name", Input).value = "Warehouse 9"
        app.screen.query_one("#form-add", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert len(app.locations) == before_locations + 1, "add-location should append"
        assert app.locations[-1].name == "Warehouse 9"
        new_nodes = app.query_one("#locations", Tree).root.children
        assert any(n.data.name == "Warehouse 9" for n in new_nodes), (
            "tree should be rebuilt with the new location"
        )

        # The whole point: a brand new process reading the same data dir
        # (simulating "exit, then relaunch") must see the new location too.
        reloaded, _reloaded_tags = storage.load()
        assert any(loc.name == "Warehouse 9" for loc in reloaded), (
            "added location must survive a fresh storage.load() (simulated restart)"
        )

        # Adding with an empty name must be rejected, not crash or add a row.
        app.run_worker(app._add_location())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#form-add", Button).press()
        await pilot.pause()
        assert _modal_open(), "validation error must keep the form open"
        assert len(app.locations) == before_locations + 1, "empty name must not add"
        app.screen.query_one("#form-cancel", Button).press()
        await _wait_until(pilot, _modal_closed)

        # --- Tags tab: add a new tag to the prebuilt vocabulary -----------
        before_tags = set(app.tag_vocabulary)
        app.run_worker(app._add_tag())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#f-name", Input).value = "vpn"
        app.screen.query_one("#form-add", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert set(app.tag_vocabulary) == before_tags | {"vpn"}, (
            "add-tag should append to the vocabulary"
        )

        # A duplicate name (case-insensitive) must be rejected, not crash
        # or add a second copy.
        app.run_worker(app._add_tag())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#f-name", Input).value = "VPN"
        app.screen.query_one("#form-add", Button).press()
        await pilot.pause()
        assert _modal_open(), "duplicate tag name must keep the form open"
        assert app.tag_vocabulary.count("vpn") == 1, "must not add a duplicate"
        app.screen.query_one("#form-cancel", Button).press()
        await _wait_until(pilot, _modal_closed)

        # --- add host: drives the real modal form, including the tag
        # picker - a Select of the vocabulary plus an Add button moves the
        # chosen tag into a row of removable chips below it -------------
        host_room = app._current_room
        before_host_count = len(host_room.hosts)
        app.run_worker(app._add_host())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#f-name", Input).value = "edge-test-09"
        app.screen.query_one("#f-address", Input).value = "10.50.0.9"
        app.screen.query_one("#f-username", Input).value = "operator"

        tag_select = app.screen.query_one("#f-tag-select", Select)
        tag_add = app.screen.query_one("#f-tag-add", Button)
        for tag in ("switch", "vpn", "router"):
            tag_select.value = tag
            tag_add.press()
            await pilot.pause()
        assert len(list(app.screen.query(".tag-chip"))) == 3, (
            "each chosen tag should get its own removable chip"
        )

        # Removing the middle chip ("vpn") must drop only that one tag.
        app.screen.query_one("#chip-1", Button).press()
        await pilot.pause()
        chip_labels = [str(b.label) for b in app.screen.query(".tag-chip")]
        assert chip_labels == ["#switch ✕", "#router ✕"], (
            f"expected switch+router left after removing the middle chip, got {chip_labels}"
        )

        app.screen.query_one("#form-add", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert len(host_room.hosts) == before_host_count + 1, "add-host should append"
        new_host = host_room.hosts[-1]
        assert new_host.name == "edge-test-09"
        assert new_host.tags == ("switch", "router"), (
            f"picker should commit only the chips left after removal, got {new_host.tags!r}"
        )

        # The new host's tags should show up on the Tags tab too.
        app.query_one(TabbedContent).active = "tags"
        await pilot.pause()
        assert {item.tag for item in app.query(TagItem)} >= {"switch", "router", "vpn"}, (
            "the new host's tags (and the still-unused 'vpn') should appear on the Tags tab"
        )

        # --- delete tag: a never-used tag removes cleanly, a used one
        # cascades and strips itself out of every host that has it -------
        tag_search = app.query_one("#tag-search", Input)
        tag_search.value = "vpn"
        await pilot.pause()
        assert app._current_tag == "vpn"
        app.run_worker(app._delete_tag())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#confirm-yes", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert "vpn" not in app.tag_vocabulary, "unused tag should be removed from the vocabulary"

        tag_search.value = "router"
        await pilot.pause()
        assert app._current_tag == "router"
        app.run_worker(app._delete_tag())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#confirm-yes", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert "router" not in app.tag_vocabulary, "deleted tag should leave the vocabulary"
        # `_delete_tag` replaces the affected Host (frozen dataclasses can't
        # be mutated in place), so look the row up fresh rather than reuse
        # the now-stale `new_host` reference from before the cascade.
        updated_host = next(h for h in host_room.hosts if h.name == "edge-test-09")
        assert updated_host.tags == ("switch",), (
            f"deleting an in-use tag must strip it from every host that had it, got {updated_host.tags!r}"
        )

        tag_search.value = ""
        await pilot.pause()
        app.query_one(TabbedContent).active = "dashboard"
        await pilot.pause()

        # --- edit host: the form opens prefilled, Save replaces in place -
        # no more delete-and-re-add to fix a typo'd IP ---------------------
        edit_room = app._current_room
        host_to_edit = app._highlighted_host()
        assert host_to_edit is not None
        app.run_worker(app._edit_host())
        await _wait_until(pilot, _modal_open)
        assert app.screen.query_one("#f-name", Input).value == host_to_edit.name, (
            "the edit form must open prefilled with the host's current values"
        )
        app.screen.query_one("#f-address", Input).value = "10.99.9.9"
        app.screen.query_one("#f-ssh-args", Input).value = (
            "-o KexAlgorithms=+diffie-hellman-group14-sha1"
        )
        app.screen.query_one("#form-add", Button).press()
        await _wait_until(pilot, _modal_closed)
        edited = next(h for h in edit_room.hosts if h.name == host_to_edit.name)
        assert edited.address == "10.99.9.9", "Save must apply the new address"
        assert edited.ssh_args == ("-o", "KexAlgorithms=+diffie-hellman-group14-sha1"), (
            "Save must apply the parsed ssh options"
        )
        assert edited.username == host_to_edit.username, (
            "fields left alone must survive the edit unchanged"
        )
        edited_command = connect_command(edited)
        assert "KexAlgorithms" in edited_command and edited_command.endswith(
            shlex.quote(edited.target)
        ), f"ssh_args should be woven into the command: {edited_command!r}"

        reloaded, _ = storage.load()
        reloaded_addresses = {
            h.name: h.address for loc in reloaded for room in loc.rooms for h in room.hosts
        }
        assert reloaded_addresses[host_to_edit.name] == "10.99.9.9", (
            "an edit must survive a fresh storage.load() (simulated restart)"
        )

        # A locally-executing ssh option must be refused at the form, with
        # the form staying open - never saved.
        app.run_worker(app._edit_host())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#f-ssh-args", Input).value = "-o ProxyCommand=evil"
        app.screen.query_one("#form-add", Button).press()
        await pilot.pause()
        assert _modal_open(), "a forbidden ssh option must keep the form open"
        app.screen.query_one("#form-cancel", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert next(
            h for h in edit_room.hosts if h.name == host_to_edit.name
        ).ssh_args == ("-o", "KexAlgorithms=+diffie-hellman-group14-sha1"), (
            "the rejected edit must not have changed anything"
        )

        # --- edit room: rename keeps every host it contains ---------------
        room_before = app._current_room
        room_hosts_before = room_before.hosts
        app.run_worker(app._edit_selected_tree_node())
        await _wait_until(pilot, _modal_open)
        assert app.screen.query_one("#f-name", Input).value == room_before.name, (
            "the edit-room form must open prefilled"
        )
        app.screen.query_one("#f-name", Input).value = room_before.name + " East"
        app.screen.query_one("#form-add", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert app._current_room is not room_before, "edit replaces the frozen Room"
        assert app._current_room.name == room_before.name + " East"
        assert app._current_room.hosts is room_hosts_before, (
            "renaming a room must carry its hosts list along untouched"
        )

        # --- delete host: Cancel must protect, Delete must remove --------
        host_before = app._highlighted_host()
        room_ref = app._current_room
        assert host_before is not None and host_before in room_ref.hosts

        app.run_worker(app._delete_host())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#confirm-no", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert host_before in room_ref.hosts, "Cancel must not delete"

        app.run_worker(app._delete_host())
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#confirm-yes", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert host_before not in room_ref.hosts, "Confirm should delete"

        reloaded, _reloaded_tags = storage.load()
        reloaded_names = {
            host.name for loc in reloaded for room in loc.rooms for host in room.hosts
        }
        assert host_before.name not in reloaded_names, (
            "deleted host must also be gone from a fresh storage.load() (simulated restart)"
        )

        # --- Quick Connect (Ctrl+F): one palette searching every host in
        # every location at once; Enter connects the top match ------------
        opened_before_qc = len(opened)
        app.action_quick_connect()
        await _wait_until(pilot, _modal_open)
        qc_input = app.screen.query_one("#qc-input", Input)
        qc_input.value = "b25-fw"
        await pilot.pause()
        from jumpbox.app import QuickConnectItem

        qc_items = [
            item for item in app.screen.query(QuickConnectItem)
        ]
        assert qc_items and qc_items[0].host.name == "us1-b25-fw1", (
            f"searching 'b25-fw' should surface us1-b25-fw1 first, got "
            f"{[i.host.name for i in qc_items]}"
        )
        assert qc_items[0].location.name == "B25", (
            "the match must carry its own location, regardless of what the "
            "Dashboard has selected"
        )
        await pilot.press("enter")
        await _wait_until(pilot, _modal_closed)
        await _wait_until(pilot, lambda: len(opened) == opened_before_qc + 1)
        assert app.activity[0].host.name == "us1-b25-fw1", (
            "Enter in Quick Connect should connect the highlighted host"
        )
        assert app.activity[0].location.name == "B25"

        # --- the "..." menu itself: opens and a plain Cancel is a no-op ---
        app.query_one("#location-menu-btn", Button).press()
        await _wait_until(pilot, _modal_open)
        app.screen.query_one("#opt-cancel", Button).press()
        await _wait_until(pilot, _modal_closed)
        assert app.query_one("#locations", Tree) is not None, (
            "dismissing the menu should return cleanly to the dashboard"
        )

    # --- on_mount's real tmux-detection branch: a separate app instance,
    # since the rest of this test deliberately runs with no real $TMUX ----
    os.environ["TMUX"] = "/tmp/fake-tmux-socket,0,0"
    appmod.panes.current_pane_id = lambda: "%9"
    appmod.panes.current_window_id = lambda: "@9"
    appmod.panes.current_session_name = lambda: "detected-session"
    mouse_enabled_for: list[str] = []
    appmod.panes.enable_mouse = lambda session: mouse_enabled_for.append(session)
    try:
        detect_app = JumpboxApp()
        async with detect_app.run_test(size=(120, 45)) as detect_pilot:
            await detect_pilot.pause()
            assert detect_app.tmux_session == "detected-session", (
                "on_mount must pick up the real session name when $TMUX is set"
            )
            assert detect_app._jumpbox_pane_id == "%9"
            assert detect_app._window_id == "@9"
            assert mouse_enabled_for == ["detected-session"], (
                "on_mount must turn mouse mode on so clicking any pane focuses "
                "it, instead of leaving the terminal's own mouse handling in the way"
            )
    finally:
        del os.environ["TMUX"]

    # --- Dashboard split at host-pane width: once a host pane is open,
    # Jumpbox's own tmux pane is down to ~40% of the terminal - the hosts
    # panel must get the *bigger* share of what's left, never be squeezed
    # into wrapped rows by a fixed-width locations panel.
    narrow_app = JumpboxApp()
    async with narrow_app.run_test(size=(76, 45)) as narrow_pilot:
        await narrow_pilot.pause()
        locations_width = narrow_app.query_one("#locations-pane").region.width
        hosts_width = narrow_app.query_one("#hosts-pane").region.width
        assert hosts_width >= locations_width, (
            f"at a split-pane width the hosts panel ({hosts_width}) must be at "
            f"least as wide as locations ({locations_width})"
        )

    # connect_command() is a single direct hop - every pane already runs on
    # this box, which is the only reason any of these hosts are reachable
    # at all, so there's nothing to jump through.
    host = load_inventory()[0].rooms[0].hosts[0]
    direct_command = connect_command(host)
    assert direct_command == f"ssh -p {host.port} {shlex.quote(host.target)}", (
        f"unexpected connect command: {direct_command!r}"
    )
    assert "-J" not in direct_command, (
        "no jump is needed - the pane running this command is already on "
        "the box that can reach the target"
    )

    # forwarded_agent_available() must reflect a *real* socket, not just
    # that the env var happens to be set to something.
    from jumpbox.connect import forwarded_agent_available

    real_sock = tempfile.mkstemp(prefix="jumpbox-fake-agent-")[1]
    try:
        os.environ["SSH_AUTH_SOCK"] = real_sock
        assert forwarded_agent_available(), "an existing socket path should count"
        os.environ["SSH_AUTH_SOCK"] = "/no/such/socket/path"
        assert not forwarded_agent_available(), (
            "a stale path that doesn't exist must not look like a real agent"
        )
        del os.environ["SSH_AUTH_SOCK"]
        assert not forwarded_agent_available(), "no env var at all means no agent"
    finally:
        os.environ.pop("SSH_AUTH_SOCK", None)
        Path(real_sock).unlink(missing_ok=True)

    # Two logins must never collide on one tmux session - including two
    # people sharing one OS account, which only differ by pty.
    import jumpbox.panes as panes_module

    os.environ["USER"] = "alice"
    os.environ["SSH_TTY"] = "/dev/pts/3"
    alice_pts3_a = panes_module.session_name()
    alice_pts3_b = panes_module.session_name()
    assert alice_pts3_a == alice_pts3_b, (
        "the same login (same user, same pty) must keep targeting the same "
        "session across calls, so relaunching restarts *its own* session"
    )

    os.environ["SSH_TTY"] = "/dev/pts/7"
    alice_pts7 = panes_module.session_name()
    assert alice_pts7 != alice_pts3_a, (
        "the same shared OS account on a different pty (a second person, on "
        "a shared bastion login) must get a different session"
    )

    os.environ["USER"] = "bob"
    bob_pts7 = panes_module.session_name()
    assert bob_pts7 != alice_pts7, "a different user must get a different session"

    del os.environ["SSH_TTY"]
    no_tty_name = panes_module.session_name()
    assert no_tty_name, "missing $SSH_TTY must still fall back to *something*, not crash"

    # Host fields are free text from the Add Host form, and connect_command()'s
    # output is run *directly* as a real tmux pane's shell command - so on a
    # shared server, one person's malicious host entry must never be able to
    # inject extra shell commands into another person's opened pane. A fake
    # `ssh` shadows the real one via PATH so this makes zero network calls
    # regardless of what the malicious address resolves to.
    work_dir = Path(tempfile.mkdtemp(prefix="jumpbox-injection-check-"))
    marker_name = "pwned_marker"
    try:
        fake_ssh = work_dir / "ssh"
        fake_ssh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        fake_ssh.chmod(0o755)

        evil = Host(
            name="pwn",
            address=f'10.0.0.1"; touch {marker_name} #',
            username=f"user$(touch {marker_name})",
            port=22,
            description=f"`touch {marker_name}`",
            status=Status.ONLINE,
            # ssh options are the one field where quoting alone isn't
            # enough: ProxyCommand would make *ssh itself* run this. It
            # must be stripped from the command outright (hand-edited
            # JSON never passes through the form's validation).
            ssh_args=("-v", "-o", f"ProxyCommand=touch {marker_name}"),
        )
        evil_command = connect_command(evil)
        assert "proxycommand" not in evil_command.lower(), (
            "a locally-executing ssh option must be stripped, not just quoted"
        )
        assert " -v " in evil_command, (
            "stripping a forbidden option must keep the harmless args around it"
        )
        assert not evil_command.rstrip().endswith("-o"), (
            "stripping an option's value must take its -o flag with it"
        )

        env = {**os.environ, "PATH": f"{work_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
        subprocess.run(
            ["bash", "-c", evil_command],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(work_dir),
            timeout=15,
        )
        assert not (work_dir / marker_name).exists(), (
            "malicious host fields executed as shell commands instead of staying literal text"
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # A corrupted inventory file must never crash the app or be silently
    # discarded - it gets backed up aside and a fresh demo seed takes over.
    path = storage.inventory_path()
    path.write_text("{not valid json", encoding="utf-8")
    recovered, recovered_tags = storage.load()
    assert len(recovered) == 5, "corrupt file should fall back to the demo seed"
    assert recovered_tags == sorted(DEFAULT_TAGS), (
        "corrupt file should fall back to the default tag vocabulary too"
    )
    backups = list(path.parent.glob(f"{path.name}.bad-*"))
    assert backups, "corrupt file should be renamed aside, not deleted"

    # --- per-host ssh options: allowed ones flow through quoted, locally-
    # executing ones are refused/stripped, site-wide ones come first ------
    from jumpbox.connect import safe_ssh_args, ssh_args_error

    assert ssh_args_error(["-o", "ProxyCommand=evil"]) is not None
    assert ssh_args_error(["-oProxyCommand=evil"]) is not None, (
        "the glued -oOption form must be caught too"
    )
    assert ssh_args_error(["-o", "LocalCommand=evil"]) is not None
    assert ssh_args_error(["-o", "KexAlgorithms=+diffie-hellman-group14-sha1"]) is None
    assert safe_ssh_args(("-v", "-o", "ProxyCommand=x", "-4")) == ("-v", "-4")

    plain_host = load_inventory()[0].rooms[0].hosts[0]
    with_base = connect_command(plain_host, ("-o", "ConnectTimeout=5"))
    assert "ConnectTimeout=5" in with_base and with_base.endswith(
        shlex.quote(plain_host.target)
    ), f"config.json's site-wide ssh options should apply to every host: {with_base!r}"

    # --- bulk CSV import/export ------------------------------------------
    from jumpbox import bulk
    from jumpbox.storage import Config

    def _all_hosts(locations):
        return {
            h.name: (loc.name, room.name, h)
            for loc in locations
            for room in loc.rooms
            for h in room.hosts
        }

    demo = load_inventory()
    demo_tags = sorted(DEFAULT_TAGS)
    demo_host_count = len(_all_hosts(demo))
    bulk_dir = Path(tempfile.mkdtemp(prefix="jumpbox-bulk-"))
    try:
        # The starter template must itself be importable: its two example
        # rows match demo hosts by name - one updates in place, one has a
        # different room and must *move* there, never duplicate.
        template_path = bulk_dir / "template.csv"
        bulk.write_template(template_path)
        merged, merged_tags, report = bulk.import_csv(template_path, demo, demo_tags)
        assert report.total_rows == 2 and not report.skipped, report.summary()
        assert len(report.updated) == 1 and len(report.moved) == 1 and not report.added, (
            f"template rows must merge with the demo data, got: {report.summary()}"
        )
        assert len(_all_hosts(merged)) == demo_host_count, (
            "a merge re-import must never duplicate hosts"
        )
        assert len(_all_hosts(demo)) == demo_host_count, (
            "import_csv must never mutate its inputs"
        )

        # A messy real-world CSV: blank optional cells fall back to
        # config.json defaults, and each broken row is skipped with a
        # reason - never aborting the good rows around it.
        messy_path = bulk_dir / "messy.csv"
        messy_path.write_text(
            "location,room,name,address,username,port,os,status,description,tags,ssh_args\n"
            "NewSite,MDF,new-sw1,10.99.0.2,,,,,,switch brand-new-tag,\n"
            "NewSite,MDF,bad-port,10.99.0.3,admin,not-a-port,,,,,\n"
            "NewSite,MDF,,10.99.0.4,admin,,,,,,\n"
            "NewSite,MDF,new-sw1,10.99.0.5,admin,,,,,,\n"
            "NewSite,MDF,evil,10.99.0.6,admin,,,,,,-o ProxyCommand=x\n"
            "NewSite,MDF,bad-status,10.99.0.7,admin,,,gone,,,\n"
            "A14,Fab 1,us1-b14-sw1,10.14.1.99,,,,,,,\n",
            encoding="utf-8",
        )
        cfg = Config(default_username="netops")
        messy_result, messy_tags, messy_report = bulk.import_csv(
            messy_path, demo, demo_tags, config=cfg
        )
        assert len(messy_report.added) == 1, messy_report.summary()
        assert len(messy_report.updated) == 1, messy_report.summary()
        assert len(messy_report.skipped) == 5, messy_report.summary()
        skip_reasons = " | ".join(reason for _, reason in messy_report.skipped)
        assert "port" in skip_reasons and "duplicate" in skip_reasons, skip_reasons
        assert "local command" in skip_reasons, (
            f"forbidden ssh options must be refused at import: {skip_reasons}"
        )
        assert "status" in skip_reasons, skip_reasons
        assert messy_report.new_locations == ["NewSite"], messy_report.summary()
        assert "brand-new-tag" in messy_tags, (
            "tags on imported hosts must join the vocabulary"
        )
        hosts_after = _all_hosts(messy_result)
        added_host = hosts_after["new-sw1"][2]
        assert added_host.username == "netops", (
            "a blank username cell must fall back to config.json's default"
        )
        assert added_host.port == 22 and added_host.os == "Linux"
        updated_host = hosts_after["us1-b14-sw1"][2]
        assert updated_host.address == "10.14.1.99", "the update row must apply"
        assert updated_host.username == "netadmin" and updated_host.tags == ("switch",), (
            "blank cells on an update must keep the existing values, not clear them"
        )

        # Export -> import --replace is a lossless round trip.
        export_path = bulk_dir / "export.csv"
        exported_count = bulk.export_csv(export_path, messy_result)
        assert exported_count == len(hosts_after)
        rebuilt, _rebuilt_tags, rebuilt_report = bulk.import_csv(
            export_path, [], [], replace=True
        )
        assert rebuilt_report.replace and not rebuilt_report.skipped
        assert rebuilt == messy_result, (
            "export -> import --replace must reproduce the inventory exactly"
        )

        # A CSV missing required columns fails loudly up front.
        bad_header = bulk_dir / "bad-header.csv"
        bad_header.write_text("name,address\nx,10.0.0.1\n", encoding="utf-8")
        try:
            bulk.import_csv(bad_header, demo, demo_tags)
            raise AssertionError("a header missing required columns must raise")
        except ValueError as exc:
            assert "location" in str(exc)
    finally:
        shutil.rmtree(bulk_dir, ignore_errors=True)

    # --- every save rotates numbered backups (newest = .1, capped) -------
    for _ in range(storage.BACKUP_COUNT + 2):
        storage.save(demo, demo_tags)
    rotated = sorted(
        p.name for p in path.parent.glob(f"{path.name}.[0-9]")
    )
    assert rotated == [
        f"{path.name}.{n}" for n in range(1, storage.BACKUP_COUNT + 1)
    ], f"expected exactly .1..{storage.BACKUP_COUNT}, got {rotated}"
    newest_backup = json.loads((path.parent / f"{path.name}.1").read_text(encoding="utf-8"))
    assert newest_backup["locations"], ".1 must be a real previous inventory state"

    # --- connection history counts and survives reloads ------------------
    storage.record_connection("history-check")
    history = storage.record_connection("history-check")
    assert history["history-check"]["count"] == 2
    assert storage.load_history()["history-check"]["count"] == 2

    # --- config.json: real values read, missing keys keep defaults -------
    config = storage.load_config()
    assert config.probe_interval == 0, "this test's config.json turns probing off"
    assert config.default_port == 22 and config.default_os == "Linux", (
        "keys missing from config.json must keep their defaults"
    )

    # --- live status probes, against real local sockets ------------------
    # A listening port is ONLINE; the same port once closed is DEGRADED
    # (host answered, port refused); an unroutable address is OFFLINE.
    from jumpbox.connect import probe

    probe_server = await asyncio.start_server(
        lambda reader, writer: writer.close(), "127.0.0.1", 0
    )
    probe_port = probe_server.sockets[0].getsockname()[1]
    assert await probe("127.0.0.1", probe_port, 2.0) is Status.ONLINE
    probe_server.close()
    await probe_server.wait_closed()
    assert await probe("127.0.0.1", probe_port, 2.0) is Status.DEGRADED
    assert await probe("10.255.255.1", 1, 0.3) is Status.OFFLINE

    # --- the CLI: template -> dry-run -> import -> export, in its own
    # data dir, driving the real argparse entry point ---------------------
    cli_data_dir = tempfile.mkdtemp(prefix="jumpbox-cli-")
    try:
        cli_env = {**os.environ, "JUMPBOX_DATA_DIR": cli_data_dir}
        cli_csv = str(Path(cli_data_dir) / "hosts.csv")

        def _cli(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, "-m", "jumpbox", *args],
                capture_output=True,
                text=True,
                env=cli_env,
                timeout=60,
            )

        result = _cli("template", cli_csv)
        assert result.returncode == 0 and Path(cli_csv).exists(), result.stderr

        result = _cli("import", cli_csv, "--dry-run")
        assert result.returncode == 0 and "Dry run" in result.stdout, result.stdout
        dry_inventory = json.loads(
            (Path(cli_data_dir) / "inventory.json").read_text(encoding="utf-8")
        )

        result = _cli("import", cli_csv)
        assert result.returncode == 0 and "Saved to" in result.stdout, result.stdout
        real_inventory = json.loads(
            (Path(cli_data_dir) / "inventory.json").read_text(encoding="utf-8")
        )
        assert real_inventory != dry_inventory, (
            "the real import must change what the dry run didn't"
        )

        export_csv_path = str(Path(cli_data_dir) / "export.csv")
        result = _cli("export", export_csv_path)
        assert result.returncode == 0 and "28 hosts" in result.stdout, result.stdout
        result = _cli("export", export_csv_path)
        assert result.returncode != 0, (
            "export onto an existing file must refuse without --force"
        )
        result = _cli("export", export_csv_path, "--force")
        assert result.returncode == 0, result.stderr
    finally:
        shutil.rmtree(cli_data_dir, ignore_errors=True)

    print(
        "SMOKE OK — theme, locations/rooms tree (no dup hosts), fuzzy search "
        "(including locations matched by a room name like IDF/MDF), room "
        "switch, no-tmux refusal, pane-opening/stacking, Tags tab "
        "(cross-location browse/filter/connect, managed vocabulary with "
        "add/delete + Select-based tag picker with removable chips), "
        "exit-only Activity reconciliation (closed entries kept as "
        "history), mouse-mode detection, per-login session isolation, "
        "direct (no-jump) connect commands, forwarded-agent detection, "
        "Activity timestamps, quit button, add/edit/delete with "
        "confirmation (including tagged hosts, prefilled edit forms, and "
        "room renames keeping their hosts), persistence across restarts, "
        "corrupt-file recovery, connect-command injection safety (host "
        "fields AND ssh options), per-host/site-wide ssh options, Quick "
        "Connect palette, connection history (in-app + on disk), rotating "
        "inventory backups, config.json defaults, live status probes "
        "against real sockets, bulk CSV import/export (merge, move, "
        "skip-with-reason, config-default fallback, lossless "
        "replace round trip, bad-header refusal), and the "
        "template/import/export CLI all good."
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"SMOKE FAILED: {exc}")
        sys.exit(1)
    finally:
        shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
