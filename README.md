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
| **Ctrl+F**          | Quick Connect: search every host everywhere, Enter connects |
| **F4** / ⛶ button   | Fullscreen the newest open host pane (Ctrl+B then Z to come back) |
| **/**               | Jump to the host search box (current room) |
| **Ctrl+R**          | Jump to the room search box           |
| **F5**              | Refresh inventory (also re-reads config + reprobes) |
| **Ctrl+Q**          | Quit (closes every open host pane too) |

The **+** menus next to the search boxes add, **edit**, and delete
locations, rooms, and hosts - editing opens the same form prefilled, so
fixing a typo'd IP is Edit → change → Save, not delete-and-re-add.

## Project layout

```
MicronJumpbox/
├─ jumpbox/
│  ├─ app.py        # Textual UI (locations tree, hosts list, quick connect, probes)
│  ├─ data.py       # Location/Room/Host model + the built-in demo seed
│  ├─ storage.py    # Persisted inventory + backups, config.json, history.json
│  ├─ bulk.py       # CSV import/export/template (the `jumpbox import` CLI)
│  ├─ dialogs.py    # Add/edit/delete modals
│  ├─ connect.py    # Builds the ssh command run in each new pane + status probe
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

Every location/room/host you add, edit, or delete, and the Tags tab's tag
vocabulary, is written to a JSON file at `~/.jumpbox/inventory.json` on
whichever machine the app is running on.

- **First run:** that file doesn't exist yet, so the app seeds it from the
  built-in demo data in `data.py` (five buildings' worth of network gear -
  switches, APs, routers, firewalls), then writes it out. From then on, the
  file is the source of truth — `data.py`'s demo set is only ever used
  again if the file goes missing.
- **Every add/edit/delete** writes the full inventory back to disk
  immediately (atomically — a crash mid-write can't corrupt it), and
  first **rotates the previous state** into `inventory.json.1` (newest)
  through `.5` (oldest). Any mistake — a fat-fingered delete, a bad bulk
  import — is undoable: copy a numbered backup back over
  `inventory.json` and hit F5.
- **F5 / Refresh** re-reads the file from disk (handy if it was edited by
  hand or by another process), it does **not** reset you back to the demo data.
- **A corrupted file** is renamed aside as `inventory.json.bad-<timestamp>`
  instead of being silently overwritten, and the app falls back to a fresh
  demo seed so a bad edit can't make Jumpbox unusable.
- **`~/.jumpbox/config.json`** (written with defaults on first run) holds
  site-wide knobs: `default_username`/`default_port`/`default_os` prefill
  the Add Host form and fill blank CSV import columns; `probe_interval` /
  `probe_timeout` control the live status sweeps below (0 turns them
  off); `ssh_options` is a list of extra ssh arguments applied to every
  connection (per-host options come on top).
- **`~/.jumpbox/history.json`** remembers when each host was last
  connected to (and how many times) across runs — shown in the detail
  panel and the Quick Connect palette.
- Override the storage location (mainly for tests) with the `JUMPBOX_DATA_DIR`
  environment variable.

## Bulk import/export (CSV)

Hand-adding hundreds of hosts through the form doesn't scale, so the real
inventory loads from a CSV — whatever the actual source of truth is
(CMDB/IPAM/NetBox export, spreadsheet), it can always be exported to CSV:

```bash
jumpbox template hosts.csv          # starter CSV showing every column
jumpbox import hosts.csv --dry-run  # report what would change, write nothing
jumpbox import hosts.csv            # merge into the existing inventory
jumpbox import hosts.csv --replace  # wipe and rebuild from the CSV alone
jumpbox export hosts.csv            # dump the inventory back out
```

`location, room, name, address` are required per row; username/port/OS
fall back to `config.json`'s defaults; `tags` (space- or `;`-separated),
`status`, `description`, `ssh_args`, and `icon` are optional. Merging
matches hosts **by name** across the whole inventory: re-importing an
updated export refreshes fields in place (blank cells keep existing
values) and moves hosts whose location/room changed — it never
duplicates. Bad rows are skipped and reported with line number and
reason; they never abort the rest. Export → edit in a spreadsheet →
re-import is also the bulk-*editing* workflow, and `export` →
`import --replace` is a lossless round trip.

## Live status + per-host SSH options

- The status dots are **live**: a background sweep TCP-probes every
  host's ssh port from this box every `probe_interval` seconds (default
  30) — port open = `ONLINE` (green), host up but port refused =
  `DEGRADED` (yellow), no answer = `OFFLINE` (red). Nothing is sent on
  the probe connection, and a sweep never writes the inventory file.
- A host can carry **extra ssh arguments** (`ssh_args` — set in the
  Add/Edit Host form or the CSV), for things like old network gear
  needing legacy key exchange:
  `-o KexAlgorithms=+diffie-hellman-group14-sha1`. Options that would
  make ssh run a *local* command (`ProxyCommand`, `LocalCommand`) are
  refused at entry and stripped defensively at connect time — on a shared
  box, one person's host entry runs in another person's pane, and it must
  never be able to execute anything there.

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
- **Fullscreen a host** with the **⛶ Fullscreen** button (next to
  Connect, or on the Activity tab for the selected connection) or **F4** -
  it zooms the most recent open host pane over the whole window using
  tmux's zoom, so the split underneath is untouched. Come back with
  **Ctrl+B then Z** (tmux's zoom toggle), or just end the session (`exit`)
  - tmux unzooms automatically when the zoomed pane closes and you land
  right back on the dashboard. The ⛶ buttons are only enabled while a
  connection is open.
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
   - Jumpbox checks for a forwarded agent (`$SSH_AUTH_SOCK` set *and* the
     socket itself exists, not just the env var) on startup, but toast
     notifications are disabled app-wide (`App.notify()` is a no-op in
     `app.py`) - there's no on-screen heads-up, so if you need to confirm
     it found one, check `$SSH_AUTH_SOCK` yourself in the shell.
2. **A key this box already has.** If this box's own user has a key
   (`~/.ssh/id_ed25519`, etc.) that the target host already trusts, that
   works too, agent or not - same as running `ssh` by hand here would.

If neither applies for a given host, ssh just falls back to its normal
password prompt, exactly as if you'd typed the command yourself - nothing
breaks, it's just not passwordless for that one host yet.

The **🕓 Activity** tab lists every connection made this run, newest
first - a 1-second background check notices a pane that closed (typed
`exit`, a dropped connection) and marks that row closed automatically, but
it's never removed: closed rows stay as history (capped at the last 20),
so the tab doubles as both "what's open right now" (● OPEN) and a log of
everything you've connected to. Select any row and hit Reconnect to open
it again. **Quitting Jumpbox (Ctrl+Q) kills the whole tmux session** -
dashboard and every open host pane - and drops you back to a plain shell.

The **🏷 Tags** tab browses hosts by tag across every location at once
(e.g. every switch, regardless of which building it's in) instead of
drilling into one room at a time. Tags themselves are a managed vocabulary
- add or delete one from this tab's own "+" menu - and the Add Host form's
tag picker only offers tags from that list, so it stays a curated set
instead of free-typed one-offs.

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

The header of `setup.sh` documents exactly what the server needs (the
short version: Python **3.10+** with the venv module, `tmux`, `ssh`, and
either PyPI access or a prefilled `wheels/` folder for a fully **offline
install** - `python3 -m pip download -r requirements.txt -d wheels` on
any machine with internet, then copy the folder over; `setup.sh` detects
it automatically). Re-run `setup.sh` after updating the code - it
installs a snapshot, not a live link to the source.

**Verify before poking around the UI:**

```bash
.venv/bin/python -m tests.smoke
```

This drives the real app headlessly and, among everything else, checks that
connecting opens panes in the right place (first one beside Jumpbox, every
one after stacked below the last) running a single direct hop with no
needless jump, that a pane ending on its own (typed `exit`, dropped
connection) gets noticed and marked closed on the Activity tab - as
history, not removed - without touching any other open connection, that
the Tags tab pulls hosts from across every location for a given tag, that
two logins - even two people sharing one OS account - always get distinct
session names, that forwarded-agent detection reflects a real socket
rather than a stale env var, and that malicious host fields (free text
from the Add Host form) can never inject extra shell commands into the
line that ends up running in a real pane. tmux itself is mocked out, so
this never needs (or touches) a real session - opening an actual pane and
typing `exit` in it to watch the layout reflow is still worth doing by
hand once.

To regenerate the UI preview screenshot:

```bash
.venv/bin/python -m tools.screenshot   # writes preview.svg (open in a browser)
```

## Roadmap (next)

- Single-file executable via PyInstaller for "copy-and-run" deployment.
- Pane sizing currently just halves whatever's left each time a host is
  added (so the column isn't perfectly even past 2-3 hosts) - revisit with
  `select-layout` if that turns out to matter in practice.
- Going from this demo inventory to the real one: the bulk-import tooling
  is ready (`jumpbox template` / `import` / `export` above) - what's left
  is the real host list to feed it, network reachability from the VM, and
  confirming the SSO/credential pattern. See
  [docs/DEPLOYMENT_PLAN.md](docs/DEPLOYMENT_PLAN.md).
