# Jumpbox

A terminal SSH jump host built with **Textual** + **Rich**. Pick a location, drill into
a room, fuzzy-search for a host, and connect.

> **Status:** "Connect" currently opens a **demo** session instead of a real SSH one.
> Flip `DEMO_MODE` off in `jumpbox/connect.py` (or set `JUMPBOX_DEMO=0`) once SSH keys
> are ready — see "Connecting" below for how sessions actually open, which depends on
> where Jumpbox itself is running.

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
| Enter / click       | Connect to the highlighted host       |
| **F2**              | Connect                               |
| **/**               | Jump to the host search box           |
| **Ctrl+R**          | Jump to the room search box           |
| **F5**              | Refresh inventory                     |
| **Ctrl+Q**          | Quit                                  |

## Project layout

```
MicronJumpbox/
├─ jumpbox/
│  ├─ app.py        # Textual UI (locations tree, hosts list, dialogs)
│  ├─ data.py       # Location/Room/Host model + the built-in demo seed
│  ├─ storage.py    # Reads/writes the persisted inventory (see below)
│  ├─ dialogs.py    # Add/delete modals
│  ├─ connect.py    # Opens the session by taking over the foreground terminal
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

There's no desktop to put a separate window on, so "Connect" takes over the
*current* terminal (Textual's `App.suspend()`). Pick a host, you're in it;
exit that session, you're straight back at the dashboard. One session at a
time.

Real connections (once `DEMO_MODE` is off) just shell out to the real `ssh`
binary - `ssh -p <port> <user>@<host>` - so authentication is whatever your normal SSH
setup already does (keys, `ssh-agent`, `~/.ssh/config`). Jumpbox never handles
credentials itself.

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
the foreground-mode command-construction logic is correct and that the
generated session scripts are valid bash (`bash -n`) and end by exec'ing a
shell so they don't exit (and hand control back to Jumpbox) the instant
they're opened. It can't actually select a host and watch the terminal
hand-off happen for real (no UI to click in a headless test) - that's still
worth doing by hand once.

To regenerate the UI preview screenshot:

```bash
.venv/bin/python -m tools.screenshot   # writes preview.svg (open in a browser)
```

## Roadmap (next)

- Flip `DEMO_MODE` off once real hosts/keys exist - the foreground take-over
  launch mechanism is already in place.
- Single-file executable via PyInstaller for "copy-and-run" deployment.
- Revisit multiple-simultaneous-sessions (e.g. tmux) if it turns out to
  matter enough to be worth the added complexity - intentionally cut for now
  in favor of the simpler single-session foreground model.
