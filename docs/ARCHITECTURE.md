# Architecture

How Jumpbox is deployed, what process is running where, and exactly what
happens between you clicking a host and getting a shell on it.

## Deployment topology

Jumpbox runs **on the jump host itself**, not on your laptop. There is
exactly one extra hop in the whole picture - the one you already made to
get a terminal open on the bastion in the first place:

```
┌─────────────────────────┐        ssh         ┌──────────────────────────────────────┐
│   Your machine           │ ─────────────────▶ │   Jump host / bastion                │
│   (MobaXterm)             │   (the only hop    │                                       │
│                           │   Jumpbox doesn't   │   tmux session "jumpbox-<user>-<tty>"│
│                           │   know or care      │   ┌───────────────┐ ┌──────────────┐ │
│                           │   about)            │   │ Jumpbox pane  │ │ host pane(s) │ │
│                           │                     │   │ (Textual app) │ │ (plain ssh)  │ │
│                           │                     │   └───────────────┘ └──────┬───────┘ │
└─────────────────────────┘                     └────────────────────────────┼─────────┘
                                                                               │ ssh -p <port> <user>@<target>
                                                                               ▼
                                                                  ┌────────────────────────┐
                                                                  │  Target host            │
                                                                  │  (10.20.0.11, etc.)    │
                                                                  │  only reachable from    │
                                                                  │  the bastion's network  │
                                                                  └────────────────────────┘
```

This is *why* the project is shaped the way it is:

- The bastion is the only thing with network access to the target hosts
  (10.x/192.168.x addresses that aren't routable from your laptop) - that's
  the literal definition of a jump host. So **the connection to a target
  has to originate from the bastion**, not from your machine.
- Since Jumpbox already runs on the bastion, the most direct way to "open
  a host" is to run `ssh` *right there* - no second jump, no relay back out
  to your machine and in again (an earlier version of this code did jump
  unnecessarily; `connect.py`'s docstring covers why that was a bug).
- Because everything happens on one machine, the "new window" you get when
  you click a host can't be a new MobaXterm window (Jumpbox has no way to
  reach back out to your desktop) - it's a new **tmux pane**, which is a
  first-class "separate terminal" concept that exists entirely server-side.
  See [TMUX_INTEGRATION.md](TMUX_INTEGRATION.md).

## The three layers

| Layer | What it is | What it's responsible for |
| --- | --- | --- |
| **Textual UI** (`app.py`, `dialogs.py`, `styles.tcss`) | A TUI app, one pane in the tmux window | Browsing the inventory, the Tags/Activity tabs, add/delete forms, all rendering and input handling |
| **tmux** (`panes.py`) | The terminal multiplexer Jumpbox runs inside | Actually opening/stacking/closing the panes that host connections run in; per-login session isolation; mouse mode; color passthrough |
| **OpenSSH** (`connect.py` + whatever `ssh` is already installed) | The actual network client | The real connection to the target host, and all authentication - Jumpbox never touches a credential |

None of these layers know much about the others beyond a narrow interface:
the UI layer calls into `panes.py` with a command string and a target pane
id and gets back a new pane id; `connect.py` only ever produces a string,
it never runs anything itself. That separation is deliberate - it's what
let the "new tab" mechanism get replaced twice during development without
touching the UI or the SSH command construction at all.

## Process lifecycle

```
$ jumpbox                                  (or `bash run.sh`)
   │
   ▼
__main__.py: main()
   │
   ├─ ensure_in_tmux()                     (panes.py)
   │     "$TMUX" already set? ─yes─▶ return, fall through to JumpboxApp()
   │     no:
   │       kill-session  <this login's session name>   (best-effort cleanup)
   │       os.execvp("tmux", ["tmux",
   │             "set-option", "-g", "terminal-overrides", ",*:RGB", ";",
   │             "new-session", "-s", <name>, sys.executable, "-m", "jumpbox"])
   │       ── process image replaced; this exact command runs again, ──
   │          ── one level deeper, now *inside* the new tmux session ──
   │
   ▼  (this is the re-exec'd process, now with $TMUX set)
JumpboxApp().run()                          (Textual's own event loop)
   │
   ├─ on_mount():
   │    - detect the real pane/window/session ids via `tmux display-message`
   │    - turn mouse mode on for the session
   │    - check for a forwarded SSH agent (toast notifications are a
   │      no-op app-wide - see App.notify() in app.py - so this is purely
   │      informational for whatever calls self.notify() elsewhere)
   │    - load the inventory + tag vocabulary from disk (storage.load())
   │    - populate the locations tree, Tags tab, Activity tab
   │    - start the 1-second reconciliation timer
   │
   ├─ ... user drives the dashboard, opens/closes host panes ...
   │
   └─ Ctrl+Q / Exit button → App.exit() → Textual restores the terminal
   │
   ▼
back in main(): `finally` block runs
   │
   └─ kill_session(app.tmux_session)        (panes.py)
         tears down the whole tmux session - dashboard pane and every
         still-open host pane - dropping the tab back to a plain shell
```

The `ensure_in_tmux()` re-exec is the one genuinely unusual thing here: a
single command-line invocation (`jumpbox`) turns into a *different* process
(`tmux`) which then runs the *original* command again as its first pane.
It's the same pattern as any "auto-wrap my shell in tmux" dotfile script,
just done from Python via `os.execvp` instead of from a shell profile.

## Sequence: clicking a host

This is the part that matters most for understanding "why is/isn't this
passwordless" - see the top-level README's "Passwordless access" section
(and `connect.py`'s docstring) for the authentication side specifically.

```
 You                  app.py (Textual)         panes.py               tmux              ssh (in the new pane)
  │                       │                        │                    │                       │
  │ double-click host ───▶│                        │                    │                       │
  │                       │ _connect(host)         │                    │                       │
  │                       │  - look up target pane:│                    │                       │
  │                       │    first host → Jumpbox's own pane          │                       │
  │                       │    later hosts → previous host pane         │                       │
  │                       │  - connect_command(host)                    │                       │
  │                       │    = "ssh -p <port> <user>@<address>"       │                       │
  │                       │                        │                    │                       │
  │                       │ open_pane(target, cmd, stacked=?) ─────────▶│                        │
  │                       │                        │  tmux split-window │                        │
  │                       │                        │  -h or -v, -l 60%/50%,                       │
  │                       │                        │  -P -F '#{pane_id}', <cmd> ─────────────────▶│
  │                       │                        │                    │   new pane is created, │
  │                       │                        │                    │   <cmd> starts running │
  │                       │                        │                    │   in it immediately ──▶│ ssh attempts the
  │                       │                        │                    │                        │ connection, tries
  │                       │◀────────── new pane id ───────────────────── │                        │ agent/keys, falls
  │                       │                        │                    │                        │ back to a password
  │                       │ track ActivityEntry(pane_id, host, ...)      │                        │ prompt if needed
  │                       │ refresh Activity tab    │                    │                        │
  │◀── pane visible, ssh's output/prompt right there in it ─────────────┴────────────────────────┘
```

The Python process is *out of the picture* the instant `open_pane()`
returns - it never proxies the ssh session's input/output. tmux owns that
pane directly; Jumpbox only tracks its id so it can detect when the pane
goes away (see `_reconcile_activity()` in `app.py`, and
[TMUX_INTEGRATION.md](TMUX_INTEGRATION.md)).

## Module map

```
jumpbox/
├── __main__.py    Entry point: ensure_in_tmux() → JumpboxApp().run() → kill_session()
├── app.py         The Textual App: widget tree, event handlers, the _connect()/
│                  _reconcile_activity() glue between the UI and panes.py
├── panes.py       Every tmux interaction: bootstrap, session naming, pane
│                  open/list, mouse mode, the truecolor fix
├── connect.py     Builds the one ssh command string for a host; checks
│                  whether a forwarded agent is reachable. Never runs anything.
├── data.py        Location/Room/Host/Status dataclasses + the built-in demo seed
├── storage.py     JSON persistence for the inventory + tag vocabulary
│                  (~/.jumpbox/inventory.json)
├── dialogs.py     Modal screens: the "+" action menu, delete confirmation,
│                  the four add-forms (Location, Room, Host, Tag)
├── fuzzy.py       Dependency-free fuzzy matcher for the two search boxes
└── styles.tcss    All Textual CSS - layout, the brand colour, the
                   per-location palette's room for tints, dialog chrome
```

`tests/smoke.py` drives the real `JumpboxApp` headlessly (Textual's
`run_test()`) and exercises essentially everything described in this
folder without needing a real terminal or a real tmux - see its own
docstring for exactly what it checks.
