"""Opening SSH sessions as tmux panes alongside Jumpbox, and tearing them
back down again - the actual mechanism behind "connect".

Jumpbox runs as one pane in a tmux window. Connecting to a host splits a
new pane and runs the ssh command directly in it: the first host splits to
the *right* of Jumpbox's own pane; every host after that splits *below*
the previous host pane, so the right-hand column just grows downward one
pane per connection. There's no Close button anywhere - a session ends
exactly the way any tmux pane normally would: typing `exit` inside it, or
the connection just dropping. Jumpbox's reconciliation timer (see app.py)
notices within a second or two and drops the stale entry from the tracked
list; tmux itself handles reflowing the rest of the layout back.

Mouse mode is turned on for the whole session, so clicking any pane - the
Jumpbox dashboard or any open host - focuses it and routes keyboard input
there, the same as clicking between tabs in any other terminal.

Each *login* gets its own session, named from the user and the actual pty
device for this login ($SSH_TTY) - not just the username - so two people
sharing one login account (a common setup on a shared bastion) still get
two independent sessions, and quitting one never touches the other's. The
same login relaunching still targets - and freshly restarts - its own
session, same as before.
"""

from __future__ import annotations

import getpass
import os
import re
import shutil
import subprocess
import sys

SESSION_PREFIX = "jumpbox"


class TmuxUnavailable(RuntimeError):
    """Raised when there's no tmux binary to drive."""


def session_name() -> str:
    """This login's tmux session name - stable across relaunches *within*
    this login (same user, same pty), distinct from every other concurrent
    login (different user, or the same user on a different pty)."""
    tty = os.environ.get("SSH_TTY", "")
    if not tty:
        try:
            tty = os.ttyname(0)
        except OSError:
            tty = ""
    tty_id = re.sub(r"[^A-Za-z0-9]+", "-", tty).strip("-") or str(os.getpid())
    user = os.environ.get("USER") or getpass.getuser()
    return f"{SESSION_PREFIX}-{user}-{tty_id}"


def ensure_in_tmux() -> None:
    """Re-exec the current process inside a tmux session if it isn't
    already running in one. `os.execvp` replaces this process outright, so
    callers never see it return in that case - the *next* `main()` to run
    is this same command, one level deeper, now with `$TMUX` set.

    Any previous session for *this same login* is killed first, so every
    launch starts completely fresh - never silently re-attaching to
    whatever host panes (or a stale, disconnected dashboard) a previous
    run left behind, e.g. because it crashed instead of exiting through
    __main__'s cleanup. A different login's session is a different name
    entirely and is never touched.

    Also forces truecolor passthrough for the whole tmux server: without
    this, tmux quantizes every colour - including Jumpbox's exact brand
    purple - down to the nearest of 256 colours before it ever reaches the
    actual terminal, regardless of what that terminal can really display
    (verified directly: with no override, a #b014e5 foreground arrives at
    an attached client as `\\x1b[38;5;128m`, a 256-colour approximation;
    with it, the real `\\x1b[38;2;176;20;229m` goes through unchanged).
    It's a server-wide option, so this only has to actually take effect
    once per server - but a tmux server with no sessions exits the instant
    its last client disconnects, so setting it as its own command (run
    *before* new-session, in a separate process) is useless on a fresh
    login: that server is already gone by the time new-session starts a
    new one. Chained into the *same* tmux invocation as new-session below
    (`;` is tmux's own command separator, not the shell's - no quoting
    needed when passed straight to execvp like this) means it lands on
    the one server that's actually about to stick around."""
    if "TMUX" in os.environ:
        return
    if shutil.which("tmux") is None:
        raise TmuxUnavailable(
            "tmux is required so each host connection can open in its own pane."
        )
    name = session_name()
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    os.execvp(
        "tmux",
        [
            "tmux",
            "set-option", "-g", "terminal-overrides", ",*:RGB", ";",
            "new-session", "-s", name, sys.executable, "-m", "jumpbox",
        ],
    )


def _tmux(*args: str) -> str:
    result = subprocess.run(["tmux", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def current_pane_id() -> str:
    return _tmux("display-message", "-p", "#{pane_id}")


def current_window_id() -> str:
    return _tmux("display-message", "-p", "#{window_id}")


def current_session_name() -> str:
    return _tmux("display-message", "-p", "#{session_name}")


def enable_mouse(session_name: str) -> None:
    """Clicking a pane focuses it and forwards the click to whatever's
    running inside, instead of the terminal's own text-selection handling
    eating it. (Holding Shift while dragging still gets you the terminal's
    native selection, for copying text out of a pane.)"""
    _tmux("set-option", "-t", session_name, "mouse", "on")


def open_pane(target_pane: str, command: str, *, stacked: bool) -> str:
    """Split `target_pane` and run `command` in the new pane, returning its
    id. `stacked=False` splits to the right (the first host, off Jumpbox's
    own pane); `stacked=True` splits below (every host after that, off the
    previously opened host pane)."""
    direction = "-v" if stacked else "-h"
    size = "50%" if stacked else "60%"
    return _tmux(
        "split-window", direction, "-t", target_pane, "-l", size,
        "-P", "-F", "#{pane_id}", command,
    )


def zoom_pane(pane_id: str) -> None:
    """Fullscreen `pane_id`: tmux's zoom makes one pane temporarily take
    over the whole window - the layout underneath is untouched, so
    unzooming restores the exact split. Getting back out is tmux's own
    zoom toggle (prefix+Z, i.e. Ctrl+B then Z), or just ending the
    session in the zoomed pane (`exit`) - tmux unzooms automatically when
    the zoomed pane closes, landing straight back on the dashboard.

    select-pane first: -Z acts on a window's *active* pane, so targeting
    an inactive one without selecting it would zoom the wrong pane."""
    _tmux("select-pane", "-t", pane_id)
    _tmux("resize-pane", "-Z", "-t", pane_id)


def kill_session(session_name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)


def live_pane_ids(window_id: str) -> set[str]:
    """Every pane id currently in `window_id` - the only way Jumpbox finds
    out a session ended: there's no Close button, so this notices a pane
    that closed on its own (typed `exit`, a dropped connection, tmux's own
    pane-close key bound to whatever the user's tmux config uses)."""
    try:
        output = _tmux("list-panes", "-t", window_id, "-F", "#{pane_id}")
    except RuntimeError:
        return set()
    return set(output.splitlines())
