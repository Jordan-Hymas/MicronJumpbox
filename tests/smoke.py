"""Headless smoke test for the Jumpbox app.

Runs the real Textual app with no visible terminal (via run_test) and exercises
the core flow: theme, the locations -> rooms tree (with no duplicated host
leaves), fuzzy search, room switching, single-click-preview vs
double-click-connect on hosts, opening hosts as tmux panes (a real tmux is
never required - `panes.open_pane`/`live_pane_ids` are monkeypatched so this
runs anywhere), that a session only ever ends from inside its own pane (no
Close button - reconciliation is what notices and prunes it), that two
logins (even two people sharing one OS account) always get distinct tmux
session names, that connect_command() is a single direct hop with no
needless jump, that forwarded-SSH-agent detection reflects a real socket
and not just a leftover env var, the timestamped Logs tab, and that the
ssh command run in each pane can't be hijacked by malicious host fields.

JUMPBOX_DATA_DIR is pointed at a throwaway temp folder before anything
imports jumpbox, so this test never reads or writes a real user's saved
inventory.

Run from the project root:  python -m tests.smoke
"""

import asyncio
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="jumpbox-smoke-")
os.environ["JUMPBOX_DATA_DIR"] = _TEST_DATA_DIR

from textual.widgets import Button, Input, TabbedContent, Tree

import jumpbox.app as appmod
from jumpbox import storage
from jumpbox.app import HostItem, JumpboxApp, LogItem, SessionItem
from jumpbox.connect import connect_command
from jumpbox.data import Host, Location, Room, Status, load_inventory


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

    def fake_open_pane(target_pane: str, command: str, *, stacked: bool) -> str:
        pane_id = f"%{len(opened) + 1}"
        opened.append((target_pane, command, stacked))
        live_panes.add(pane_id)
        return pane_id

    appmod.panes.open_pane = fake_open_pane
    appmod.panes.live_pane_ids = lambda window_id: set(live_panes)

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
        assert len(location_nodes) == 2, f"expected 2 locations, got {len(location_nodes)}"
        assert all(isinstance(n.data, Location) for n in location_nodes)

        room_nodes = location_nodes[0].children
        assert len(room_nodes) == 2, f"expected 2 rooms, got {len(room_nodes)}"
        assert all(isinstance(n.data, Room) for n in room_nodes)
        assert all(len(n.children) == 0 for n in room_nodes), (
            "rooms must not nest host leaves - hosts only live in the hosts list"
        )

        # First room (Datacenter 1) has 3 hosts.
        hosts = _count(app, HostItem)
        assert hosts == 3, f"expected 3 hosts in first room, got {hosts}"

        detail = app.query_one("#detail-body").render()
        assert str(detail).strip(), "detail panel should not be empty"

        # Fuzzy-filter narrows without zeroing out.
        search = app.query_one("#host-search")
        search.value = "edge"
        await pilot.pause()
        filtered = _count(app, HostItem)
        assert 1 <= filtered < 3, f"fuzzy search should narrow results, got {filtered}"

        search.value = ""
        await pilot.pause()
        assert _count(app, HostItem) == 3, "clearing search should restore hosts"

        # Switch room via the tree (second room node) -> different host set.
        tree.move_cursor(room_nodes[1])
        await pilot.pause()
        assert _count(app, HostItem) == 2, "second room (Datacenter 2) should have 2 hosts"
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
        assert not app.logs, "single click must not add a log entry"

        # With no real tmux session (the real state in this headless run),
        # double click must refuse - never silently no-op, never crash.
        await pilot.click(target, times=2)
        await pilot.pause()
        assert not opened, "no tmux session means there's nowhere to open a pane"
        assert not app.logs, "a refused connect must not be logged"

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
        assert len(app.open_sessions) == 1
        assert app.open_sessions[0].host is target_host
        assert app.open_sessions[0].pane_id == "%1"
        assert len(app.logs) == 1, "a successful connect should add a log entry"
        assert app.logs[0].host is target_host

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
        assert len(app.open_sessions) == 2

        # --- Sessions tab lists every open host, read-only ---------------
        app.query_one(TabbedContent).active = "sessions"
        await pilot.pause()
        session_items = list(app.query(SessionItem))
        assert len(session_items) == 2, f"expected 2 open sessions, got {len(session_items)}"

        # There's no Close button - a session only ever ends from inside
        # its own pane (typed `exit`, or the connection just dropping),
        # simulated here by tmux simply no longer reporting that one pane.
        first_pane_id = app.open_sessions[0].pane_id
        live_panes.discard(first_pane_id)
        await app._reconcile_sessions()
        await pilot.pause()
        assert len(app.open_sessions) == 1, "ending one session must not touch the other"
        assert app.open_sessions[0].host is other_host
        assert len(list(app.query(SessionItem))) == 1

        # And once every remaining pane is gone too, the list empties out.
        live_panes.clear()
        await app._reconcile_sessions()
        await pilot.pause()
        assert not app.open_sessions, "reconciliation must drop panes tmux no longer has"
        assert not list(app.query(SessionItem))

        # Logs tab should still carry the full history with real timestamps.
        assert len(app.logs) == 2, "both connects should have been logged"
        app.query_one(TabbedContent).active = "logs"
        await pilot.pause()
        log_items = list(app.query(LogItem))
        assert len(log_items) == len(app.logs), (
            f"logs tab should show {len(app.logs)} entries, got {len(log_items)}"
        )
        stamp = log_items[0].entry.when.strftime("%Y-%m-%d %I:%M:%S %p")
        assert stamp in str(app.query_one(LogItem).query_one("Static").render())

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
        reloaded = storage.load()
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

        reloaded = storage.load()
        reloaded_names = {
            host.name for loc in reloaded for room in loc.rooms for host in room.hosts
        }
        assert host_before.name not in reloaded_names, (
            "deleted host must also be gone from a fresh storage.load() (simulated restart)"
        )

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
        )
        evil_command = connect_command(evil)

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
    recovered = storage.load()
    assert len(recovered) == 2, "corrupt file should fall back to the demo seed"
    backups = list(path.parent.glob(f"{path.name}.bad-*"))
    assert backups, "corrupt file should be renamed aside, not deleted"

    print(
        "SMOKE OK — theme, locations/rooms tree (no dup hosts), fuzzy search, "
        "room switch, no-tmux refusal, pane-opening/stacking, exit-only pane "
        "reconciliation, mouse-mode detection, per-login session isolation, "
        "direct (no-jump) connect commands, forwarded-agent detection, logs "
        "with timestamps, quit button, add/delete with confirmation, "
        "persistence across restarts, corrupt-file recovery, and "
        "connect-command injection safety all good."
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"SMOKE FAILED: {exc}")
        sys.exit(1)
    finally:
        shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
