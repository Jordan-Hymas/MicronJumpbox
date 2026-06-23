# tmux integration

Everything in this document is `jumpbox/panes.py` plus the handful of call
sites in `app.py` and `__main__.py` that use it. This is the layer that
turns "click a host" into an actual new terminal pane, and it's also where
two real, verified bugs got found and fixed during development - both
covered here with the exact evidence, since "trust me it works" isn't
worth much without it.

## Why tmux at all

Jumpbox runs on the bastion (see [ARCHITECTURE.md](ARCHITECTURE.md)), so
there's no separate desktop to put a new window on. tmux panes are the
closest server-side equivalent: a real, independent terminal that can run
its own process, sit alongside other panes, and be closed independently -
all without Jumpbox needing to do any terminal emulation itself.

## Bootstrap: `ensure_in_tmux()`

Called once, at the very top of `__main__.py:main()`, before the Textual
app is even constructed:

```python
def ensure_in_tmux() -> None:
    if "TMUX" in os.environ:
        return
    if shutil.which("tmux") is None:
        raise TmuxUnavailable(...)
    name = session_name()
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    os.execvp("tmux", [
        "tmux",
        "set-option", "-g", "terminal-overrides", ",*:RGB", ";",
        "new-session", "-s", name, sys.executable, "-m", "jumpbox",
    ])
```

If `$TMUX` is already set, the process is already running inside some tmux
session (maybe the user started their own session and ran `jumpbox` inside
it manually) - nothing to do, just continue into `JumpboxApp().run()`.

Otherwise: `os.execvp` **replaces the current process image outright**.
This is not a subprocess call - there's no parent Python process waiting
around afterward. `tmux` becomes the process running in this pty, and the
command it's told to run as the new session's first pane is
`sys.executable -m jumpbox` - i.e. *the exact same command, run again*, one
level deeper. That second invocation finds `$TMUX` already set and falls
straight through to running the app for real.

### Why kill-session runs first

Every login gets a *stable* session name (next section) so relaunching
within the same login always targets the same session. `kill-session`
before `new-session` means **every launch starts completely fresh**: if a
previous run crashed instead of exiting cleanly (so `__main__.py`'s
`finally` block never got to call `kill_session()`), this is what cleans
up the leftover session - and every host pane it had open - before
starting the new one. You're never silently dropped into a stale,
disconnected dashboard from a run that died an hour ago.

### Why the truecolor override is chained, not separate

This one was a real, two-stage bug.

**Stage 1 - tmux quantizes color by default.** Verified directly: attach a
real pty to a tmux session with no special config and print a raw 24-bit
SGR escape sequence -

```
$ printf '\033[38;2;176;20;229mTESTCOLOR\033[0m\n'
```

- and the bytes tmux actually forwards to the attached client are
`\x1b[38;5;128m`, a **256-color** approximation, not the original 24-bit
value - regardless of whether the real terminal on the other end (e.g.
MobaXterm) could have displayed truecolor correctly. tmux decides this
itself, based on what it thinks the attached terminal supports
(`tmux info` shows `RGB: [missing]` by default for a plain
`xterm-256color` client). The fix is a per-server option:

```
tmux set-option -g terminal-overrides ',*:RGB'
```

With that set *before* a client attaches, the same printf comes through
correctly as `\x1b[38;2;176;20;229m` - verified the same way.

**Stage 2 - setting it as a separate command doesn't survive.** The
obvious-looking fix is wrong:

```python
subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
subprocess.run(["tmux", "set-option", "-g", "terminal-overrides", ",*:RGB"], ...)
os.execvp("tmux", ["tmux", "new-session", "-s", name, ...])
```

A tmux **server** with zero sessions and zero attached clients exits
immediately - there's nothing keeping it alive. On a genuinely fresh login
(no prior tmux usage on that socket at all), the `kill-session` call is a
no-op (nothing to kill), and the standalone `set-option -g` call starts a
server just long enough to set the option *and then that server exits*,
because it still has no sessions. By the time `new-session` runs as a
third, separate command, it starts a **brand new** server from scratch -
the override is already gone. Verified directly: `tmux list-sessions`
right after a bare `set-option -g` reports `no server running`.

The fix is to chain `set-option` and `new-session` into **one** tmux
invocation, using tmux's own `;` command separator (not the shell's - this
is passed straight to `execvp` as separate argv elements, no shell
involved, so no quoting needed):

```python
os.execvp("tmux", [
    "tmux",
    "set-option", "-g", "terminal-overrides", ",*:RGB", ";",
    "new-session", "-s", name, sys.executable, "-m", "jumpbox",
])
```

Now both commands run against the *same* server connection, and that
server doesn't get a chance to exit in between - because the second half
of the same invocation immediately gives it a real session. Verified
end-to-end against the actual running app (not just an isolated printf):
starting from `tmux kill-server` (a totally clean slate), the title text
arrives at a real pty-attached client as `\x1b[38;2;176;20;229m` - the
exact brand color, with zero 256-color codes anywhere nearby.

It's a server-wide option, so once any one login on a given tmux server
has triggered this, every other session on that *same* server benefits
too - but Jumpbox sets it on every launch regardless, since it costs
nothing and covers the server having just been freshly (re)started.

## Per-login session naming

```python
def session_name() -> str:
    tty = os.environ.get("SSH_TTY", "") or _ttyname_or_empty()
    tty_id = re.sub(r"[^A-Za-z0-9]+", "-", tty).strip("-") or str(os.getpid())
    user = os.environ.get("USER") or getpass.getuser()
    return f"jumpbox-{user}-{tty_id}"
```

Two things matter here, and the reason both are needed is the same reason
this got built in the first place: **a shared bastion login account.**

If everyone on the team connects as the same OS user (common, so SSH key
management is centralized), `$USER` alone would give every concurrent
login the *same* session name - the second person to launch Jumpbox would
either get rejected, or attach to the first person's session (sharing
their dashboard), or - with the "always starts fresh" `kill-session` above
- **kill the first person's session out from under them**, closing every
host connection they had open. None of those are acceptable.

`$SSH_TTY` (set by `sshd` to the actual pty device for *this* login, e.g.
`/dev/pts/7`) is what actually disambiguates concurrent logins under one
shared account - it's unique per connection even when the username is
identical. So:

- Same user, same pty (relaunching Jumpbox within one login) → same name →
  "always starts fresh" correctly restarts *that login's own* session.
- Same user, different pty (two people sharing one login account,
  concurrently) → different names → fully independent sessions.
- Different user → different name regardless.

Falls back to `os.ttyname(0)` if `$SSH_TTY` isn't set (e.g. a local,
non-SSH run), and to the process id if there's no controlling tty at all
(e.g. a test harness) - in that last case sessions aren't stable across
relaunches, but there's also no real terminal to reattach to anyway.

Verified end-to-end: launched two simulated concurrent logins (same user,
different `$SSH_TTY`) for real, confirmed two independent tmux sessions
existed simultaneously, killed one's process, confirmed the other was
completely unaffected (still attached, still running) - and separately
confirmed `kill_session()` called directly with one specific name only
ever removes that one session.

## Opening and stacking panes

```python
def open_pane(target_pane: str, command: str, *, stacked: bool) -> str:
    direction = "-v" if stacked else "-h"
    size = "50%" if stacked else "60%"
    return _tmux("split-window", direction, "-t", target_pane, "-l", size,
                 "-P", "-F", "#{pane_id}", command)
```

Called from `app.py`'s `_connect()`:

- **No open sessions yet** → split *Jumpbox's own pane* (`-h`, horizontal:
  the new pane appears to its right) → `stacked=False`.
- **One or more open sessions** → split *the most recently opened host
  pane* (`-v`, vertical: the new pane appears below it) → `stacked=True`.

tmux panes form a binary tree of splits, so this produces exactly the
layout you'd expect without any extra bookkeeping: splitting Jumpbox's
pane horizontally creates `[jumpbox | host1]`; splitting `host1`
vertically for the next host nests *inside that branch only*, giving
`[jumpbox | [host1 / host2]]`, and so on - Jumpbox's own pane is never
touched by any of this. Verified the real geometry with `tmux list-panes`:
in a 200-col window, host1 lands at `left=80 width=120 height=24`, host2
at `left=80 top=25 width=120 height=25` directly below it.

`-P -F '#{pane_id}'` makes `split-window` print the new pane's id (e.g.
`%16`) instead of nothing, which is how `open_pane()` returns a value at
all - that id is what `app.py` stores in an `ActivityEntry` to track it.

### Closing a pane

There is deliberately no "Close" button anywhere. A session ends exactly
the way any tmux pane ends: the command running in it exits, whether
because you typed `exit`, the connection dropped, or anything else. tmux
then reflows the remaining panes in that branch to fill the freed space
automatically - verified directly: 3 panes open, kill the middle one, the
other two expand to fill its space, no extra code needed on Jumpbox's side
at all. This used to not be true (see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md) for the `remain-on-exit` detour
and why it was removed) - now a pane closing *is* the close action.

### Noticing a pane is gone: reconciliation

Since there's no button telling Jumpbox "this one's done," it has to
notice on its own. `app.py`'s `_reconcile_activity()` runs every second
(`self.set_interval(1.0, ...)`):

```python
async def _reconcile_activity(self) -> None:
    open_entries = [entry for entry in self.activity if entry.is_open]
    if self.tmux_session is None or not open_entries:
        return
    alive = panes.live_pane_ids(self._window_id)
    if all(entry.pane_id in alive for entry in open_entries):
        return
    now = datetime.now()
    for entry in open_entries:
        if entry.pane_id not in alive:
            entry.closed_at = now
    self._trim_activity()
    await self._populate_activity()
```

`live_pane_ids()` is just `tmux list-panes -t <window_id>` parsed into a
set of ids. Any open `ActivityEntry` whose pane id isn't in that set any
more gets `closed_at` set - it's marked closed (shows as plain history
instead of "● OPEN" on the Activity tab) rather than removed outright, so
the tab keeps a record of every connection made this run, not just the
ones still open. This also catches a pane closed some way Jumpbox didn't
initiate at all - e.g. tmux's own pane-close key, if someone's personal
tmux config binds one.

## Mouse mode

```python
def enable_mouse(session_name: str) -> None:
    _tmux("set-option", "-t", session_name, "mouse", "on")
```

Called once in `on_mount()`, right after the session is detected. Without
it, clicking a pane does nothing useful (the click either gets eaten by
the terminal's own selection handling, or ignored) - tmux's mouse mode is
what makes "click a pane to focus it and route your typing there" work at
all, the same as clicking between windows in any GUI app. It's a *session*
option, so it only needs setting once per session and every pane in it -
Jumpbox's own and every host pane - is covered. Holding **Shift** while
dragging still gets you the terminal's native text selection (for copying
something out of a pane), which is the standard tmux convention for
working around mouse mode capturing drag events.

## Environment inheritance (agent forwarding)

New panes inherit the environment tmux captured when the *session* was
created (refreshed for a documented default set of variables - including
`SSH_AUTH_SOCK` - whenever a client reattaches, via tmux's own
`update-environment`). Verified directly: set a fake `$SSH_AUTH_SOCK`
before `ensure_in_tmux()` runs, split a brand new pane afterward, and
`env | grep SSH_AUTH_SOCK` *inside that new pane* shows the same value.
This is the entire reason a forwarded SSH agent "just works" in every host
pane with zero code in Jumpbox dedicated to it - see
[SSH_AUTHENTICATION.md](SSH_AUTHENTICATION.md).

## Teardown

```python
# __main__.py
try:
    app.run()
finally:
    if app.tmux_session:
        kill_session(app.tmux_session)
```

`kill_session()` is `tmux kill-session -t <name>` - it tears down every
pane in that session, dashboard included, in one call. Running it in a
`finally` means it executes even if the app crashed outright, not just on
a clean Ctrl+Q - so a crash still cleans up every open host pane rather
than orphaning them (and `ensure_in_tmux()`'s `kill-session` on the *next*
launch is the second line of defense if it somehow didn't). It runs
*after* `app.run()` returns, i.e. after Textual has already fully restored
the terminal to normal (cooked) mode - tearing the session down only then
avoids any race with Textual's own cleanup, and the result is the tab just
drops back to a plain shell prompt.
