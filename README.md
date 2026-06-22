# Jumpbox

A terminal SSH jump host built with **Textual** + **Rich**. Pick a location, drill into
a room, fuzzy-search for a host, and connect — each connection opens in its own new
**tmux pane** next to the dashboard, see "Connecting" below. Requires `tmux`.

## Run it (quickest way to test)

The app is already installed into the venv at `.venv`. From a real terminal:

```bash
# Option A — the convenience launcher
bash run.sh

# Option B — activate the venv, then use the command
source .venv/bin/activate
jumpbox
```

Either way you type **`jumpbox`** once the venv is active.

### Controls

| Key / action        | What it does                          |
| ------------------- | ------------------------------------- |
| Type in a search box| Fuzzy-filter rooms or hosts           |
| ↑ / ↓               | Move through the list                 |
| Enter / double-click| Open the highlighted host in a new pane |
| **F2**              | Open the highlighted host in a new pane |
| **/**               | Jump to the host search box           |
| **Ctrl+R**          | Jump to the room search box           |
| **F5**              | Refresh inventory                     |
| **Ctrl+Q**          | Quit (closes every open host pane too) |

## Project layout

```
MicronJumpbox/
├─ jumpbox/
│  ├─ app.py        # Textual UI (locations tree, hosts list, dialogs)
│  ├─ data.py       # Location/Room/Host model + the built-in demo seed
│  ├─ storage.py    # Reads/writes the persisted inventory (see below)
│  ├─ dialogs.py    # Add/delete modals
│  ├─ connect.py    # Builds the ssh command run in each new pane
│  ├─ panes.py      # tmux session/pane management - the actual "connect"
│  ├─ fuzzy.py      # Dependency-free fuzzy matcher
│  └─ styles.tcss   # All the styling
├─ tests/smoke.py   # Headless test of the core flow
├─ tools/screenshot.py
├─ requirements.txt # Pinned deps for reproducible installs
├─ pyproject.toml   # Package + `jumpbox` command definition
└─ setup.sh / run.sh      # Setup + launcher
```

## Data & persistence

Every location/room/host you add or delete is written to a JSON file at
`~/.jumpbox/inventory.json` on whichever machine the app is running on.

- **First run:** that file doesn't exist yet, so the app seeds it from the
  built-in demo data in `data.py`, then writes it out. From then on, the file
  is the source of truth — `data.py`'s demo set is only ever used again if
  the file goes missing.
- **Every add/delete** writes the full inventory back to disk immediately
  (atomically — a crash mid-write can't corrupt it).
- **F5 / Refresh** re-reads the file from disk (handy if it was edited by
  hand or by another process), it does **not** reset you back to the demo data.
- **A corrupted file** is renamed aside as `inventory.json.bad-<timestamp>`
  instead of being silently overwritten, and the app falls back to a fresh
  demo seed so a bad edit can't make Jumpbox unusable.
- Override the storage location (mainly for tests) with the `JUMPBOX_DATA_DIR`
  environment variable.

> **Per-machine, not shared.** This is one file per machine Jumpbox runs on. If
> everyone SSHes into one shared jump server and runs Jumpbox there (see
> "Connecting" below), everyone reads/writes that *one* machine's file, which
> is effectively a shared inventory already. If Jumpbox instead runs locally
> on each engineer's own machine, each person has their own independent copy.

## Connecting: where the new session actually opens

Jumpbox runs as one pane in a **tmux** window - on launch it re-execs itself
inside `tmux new-session` if it isn't already in one, so this is automatic,
not a separate setup step (it also kills any previous session *for this
same login* first, so every launch starts completely fresh - see "Always
starts fresh" and "Multiple people on the same box" below). Picking a host
(double-click, Enter, F2, or the Connect button) splits a new pane and runs
one direct hop in it - no jump needed, since the pane is already running on
this box, which is the reason these hosts are reachable at all:

```
ssh -p <port> <user>@<target>
```

- **The first host** splits off to the **right** of Jumpbox's own pane.
- **Every host after that** splits **below** the previously opened host
  pane, so the right-hand column just grows downward one pane per
  connection - the dashboard pane never moves or leaves the screen.
- **Click any pane to switch to it** - Jumpbox turns tmux's mouse mode on
  for the whole session, so clicking the dashboard or any open host pane
  focuses it and sends your typing there, same as clicking between windows
  in any other app. (Holding Shift while dragging still gets you the
  terminal's native text selection, for copying something out of a pane.)
- **Ending a connection** is deliberately just typing `exit` inside its
  pane (or letting the connection drop) - there's no Close button anywhere.
  tmux closes that pane immediately and reflows the rest straight back: 3
  panes open, `exit` out of the middle one, 2 remain.

## Passwordless access: using your own SSH keys

Authentication is whatever your normal SSH setup already does - Jumpbox
never sees, stores, or asks for a credential. In practice that means one
of two things, and OpenSSH tries them automatically in this order with no
configuration needed:

1. **A forwarded SSH agent.** If you connect to this box from MobaXterm
   with agent forwarding turned on, `$SSH_AUTH_SOCK` points at that
   forwarded agent (Pageant, or whatever MobaXterm is holding), and every
   host pane - they all inherit it automatically, verified directly - will
   offer your actual local keys to the target host, with no copy of them
   ever touching this box. This is almost certainly what you want:
   - In MobaXterm, open **Session settings** for the connection to this
     box → **SSH** tab → check **"Forward SSH agent"** (uses Pageant if
     it's running, or MobaXterm's own agent if you loaded a key into it).
   - This box's `sshd` needs to allow it too - `AllowAgentForwarding yes`
     in `/etc/ssh/sshd_config` (the default on most distros; check if
     forwarding doesn't seem to work).
   - **Jumpbox tells you on startup** whether it actually found a forwarded
     agent (`$SSH_AUTH_SOCK` set *and* the socket itself exists, not just
     the env var) - watch for that notification right after launch.
2. **A key this box already has.** If this box's own user has a key
   (`~/.ssh/id_ed25519`, etc.) that the target host already trusts, that
   works too, agent or not - same as running `ssh` by hand here would.

If neither applies for a given host, ssh just falls back to its normal
password prompt, exactly as if you'd typed the command yourself - nothing
breaks, it's just not passwordless for that one host yet.

The **🖥 Sessions** tab is a read-only list of what's currently open - a
1-second background check notices a pane that closed (typed `exit`, a
dropped connection) and drops it from the list automatically, so it never
shows a session that's already gone. **Quitting Jumpbox (Ctrl+Q) kills the
whole tmux session** - dashboard and every open host pane - and drops you
back to a plain shell.

### Always starts fresh

Every launch kills any previous tmux session *for this same login* before
creating a new one, and `__main__.py` kills it again on the way out
(`finally`-guarded, so it still happens if Jumpbox crashes outright instead
of exiting cleanly). In practice this means a crashed or killed run never
leaves stale host panes - or a disconnected dashboard - for the next launch
to silently inherit; you always start from a clean dashboard with nothing
open.

### Multiple people on the same box

Each *login* gets its own tmux session - named from `$USER` plus the
actual pty device for that login (`$SSH_TTY`), not just the username - so
two people sharing one shared OS account (common on a bastion where
everyone connects as the same service user) still get two independent
sessions: closing/quitting one never touches the other's dashboard or open
host panes. Relaunching Jumpbox *within the same login* (same user, same
pty) still targets - and freshly restarts - that one login's own session,
exactly as in "Always starts fresh" above.

### Server setup: tmux mouse mode and exact colours

Jumpbox turns mouse mode on for its own session at launch
(`tmux set-option -t <session> mouse on`), so **no server-side tmux config
changes are required** for click-to-switch-panes to work. If you'd rather
have mouse mode on by default for *all* tmux usage on the server (not just
inside Jumpbox's session - e.g. so it's already on if someone attaches to
the session manually before Jumpbox finishes starting), add this once to
that server's `~/.tmux.conf` or `/etc/tmux.conf`:

```
set -g mouse on
```

Jumpbox also forces truecolor passthrough for the whole tmux server at
launch (`tmux set-option -g terminal-overrides ',*:RGB'`), again with no
server-side config required. Without this, tmux quantizes every colour -
including Jumpbox's exact brand purple - down to the nearest of 256 colours
before it ever reaches the actual terminal, *regardless* of whether that
terminal (MobaXterm included) can really display truecolor: verified
directly that with no override, `#b014e5` arrives at an attached client as
`\x1b[38;5;128m` (a 256-colour approximation), and with it, the real
`\x1b[38;2;176;20;229m` goes through unchanged. It's a server-wide option,
so once any one login has set it, every other session on that same server
benefits too - but Jumpbox sets it on every launch regardless, since it's
free and covers the server having just been freshly started.

## Deploying to a server

**Transfer it:** copy the whole `MicronJumpbox` folder over (scp, rsync, git,
whatever's easiest), or use `jumpbox-linux.tar.gz` next to this folder - a
pre-built archive with `.venv`/`__pycache__`/build artifacts already stripped
out, ready to `scp` over and extract:

```bash
scp jumpbox-linux.tar.gz user@linux-server:~/
ssh user@linux-server
mkdir jumpbox && tar -xzf jumpbox-linux.tar.gz -C jumpbox && cd jumpbox
```

**Set it up and run it** (one-time setup, then launch):

```bash
bash setup.sh   # creates .venv, installs deps
bash run.sh     # starts Jumpbox
```

**Verify before poking around the UI:**

```bash
.venv/bin/python -m tests.smoke
```

This drives the real app headlessly and, among everything else, checks that
connecting opens panes in the right place (first one beside Jumpbox, every
one after stacked below the last) running a single direct hop with no
needless jump, that a pane ending on its own (typed `exit`, dropped
connection) gets noticed and removed from the Sessions list without
touching any other open session, that two logins - even two people sharing
one OS account - always get distinct session names, that forwarded-agent
detection reflects a real socket rather than a stale env var, and that
malicious host fields (free text from the Add Host form) can never inject
extra shell commands into the line that ends up running in a real pane.
tmux itself is mocked out, so this never needs (or touches) a real session
- opening an actual pane and typing `exit` in it to watch the layout
reflow is still worth doing by hand once.

To regenerate the UI preview screenshot:

```bash
.venv/bin/python -m tools.screenshot   # writes preview.svg (open in a browser)
```

## Roadmap (next)

- Single-file executable via PyInstaller for "copy-and-run" deployment.
- Pane sizing currently just halves whatever's left each time a host is
  added (so the column isn't perfectly even past 2-3 hosts) - revisit with
  `select-layout` if that turns out to matter in practice.
