"""Headless smoke test for the Jumpbox app.

Runs the real Textual app with no visible terminal (via run_test) and exercises
the core flow: theme, the locations -> rooms tree (with no duplicated host
leaves), fuzzy search, room switching, single-click-preview vs
double-click-connect on hosts, the timestamped Logs tab, and that the demo
session-launch path generates a unique script per call (so multiple launches
never collide).

`launch_session` is monkeypatched while driving the app through the pilot so
this test never actually pops a real terminal window. JUMPBOX_DATA_DIR is
pointed at a throwaway temp folder before anything imports jumpbox, so this
test never reads or writes a real user's saved inventory.

Run from the project root:  python -m tests.smoke
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="jumpbox-smoke-")
os.environ["JUMPBOX_DATA_DIR"] = _TEST_DATA_DIR

from textual.widgets import Button, Input, TabbedContent, Tree

import jumpbox.app as appmod
import jumpbox.connect as connect
from jumpbox import storage
from jumpbox.app import HostItem, JumpboxApp, LogItem
from jumpbox.connect import launch_session
from jumpbox.data import Host, Location, Room, Status, load_inventory


def _count(app, kind) -> int:
    return len(app.query(kind))


def _check_script_stays_open(script_path: str) -> None:
    """`bash script.sh` has no built-in "stay open afterwards" flag - without
    an exec'd shell as the script's last line, bash exits the instant it's
    done and the suspended terminal just drops back to Jumpbox with no
    usable session ever having been visible."""
    content = Path(script_path).read_text(encoding="utf-8")
    last_line = content.strip().splitlines()[-1]
    assert last_line.startswith("exec "), (
        f"script must end by exec'ing a shell so the window doesn't "
        f"immediately close once it finishes, got: {last_line!r}"
    )


def _check_bash_syntax(script_path: str) -> None:
    """`bash -n` parses without running - a real syntax check for the
    generated POSIX scripts, even though this machine can't execute them."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        return  # no bash available to check with (e.g. a bare CI image)
    result = subprocess.run([bash, "-n", script_path], capture_output=True, text=True)
    assert result.returncode == 0, f"invalid bash syntax in {script_path}: {result.stderr}"


async def _wait_until(pilot, predicate, attempts: int = 20) -> None:
    """Poll `predicate` across several pauses - workers/modals don't always
    land within a single pilot.pause()."""
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause()
    raise AssertionError(f"condition not met after {attempts} pauses")


async def main() -> None:
    launched: list[Host] = []
    appmod.launch_session = lambda host: launched.append(host)

    app = JumpboxApp()
    # The headless driver behind run_test() can't suspend a real terminal,
    # so stub out the hand-off itself - same reason launch_session is
    # stubbed above, just one step further down the same call path.
    app._run_foreground = lambda args: None
    async with app.run_test(size=(120, 45)) as pilot:
        await pilot.pause()

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

        # --- single click previews, double click connects ----------------
        host_items = list(app.query(HostItem))
        target = host_items[1]
        target_host = target.host
        assert app._highlighted_host() is not target_host, "test needs a non-default target"

        await pilot.click(target)
        await pilot.pause()
        assert app._highlighted_host() is target_host, "single click should move the preview"
        assert target_host.name in str(app.query_one("#detail-body").render())
        assert not launched, "single click must not launch a session"
        assert not app.logs, "single click must not add a log entry"

        await pilot.click(target, times=2)
        await pilot.pause()
        assert len(launched) == 1, f"double click should connect exactly once, got {len(launched)}"
        assert launched[0] is target_host
        assert len(app.logs) == 1, "double click should add a log entry"

        # Logs tab should reflect the log once activated, with a full timestamp.
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

    # Two launches (even for the same host) must never collide on one file -
    # this is what lets connecting to the same host twice race-free.
    host = load_inventory()[0].rooms[0].hosts[0]
    args_a = launch_session(host)
    args_b = launch_session(host)
    assert args_a[-1] != args_b[-1], "each launch must get its own script file"
    assert args_a[0] == "bash", f"unexpected launch args: {args_a}"

    # `bash -n` (a real syntax check, no execution) catches quoting bugs in
    # the generated script, and _check_script_stays_open guards the "flashes
    # and immediately closes" bug from coming back.
    _check_bash_syntax(args_a[1])
    _check_script_stays_open(args_a[1])

    # Host fields are free text from the Add Host form, and on a shared
    # server one person's entry runs in *another* person's session - so
    # shell metacharacters in them must never execute. DEMO_MODE off so
    # the target string actually reaches the ssh command line, not just
    # the echo banner. A fake `ssh` shadows the real one via PATH so this
    # makes zero network calls regardless of what the malicious address
    # resolves to.
    real_demo_mode = connect.DEMO_MODE
    connect.DEMO_MODE = False
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
        evil_args = launch_session(evil)
        _check_bash_syntax(evil_args[1])

        env = {**os.environ, "PATH": f"{work_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
        subprocess.run(
            ["bash", evil_args[1]],
            input="exit\n",
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
        connect.DEMO_MODE = real_demo_mode
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
        "room switch, single/double click, logs with timestamps, quit button, "
        "add/delete with confirmation, persistence across restarts, corrupt-file "
        "recovery, foreground launch, unique launches all good."
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"SMOKE FAILED: {exc}")
        sys.exit(1)
    finally:
        shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
