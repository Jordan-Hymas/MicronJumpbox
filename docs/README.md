# Jumpbox documentation

The top-level [README.md](../README.md) is the quick-start: install it, run it,
the controls table, the short version of how connecting works. This folder is
the deep version - how the whole stack actually fits together, for anyone
maintaining Jumpbox, debugging it, or rolling it out to a team.

Read them in this order if you're new to the codebase:

1. **[ARCHITECTURE.md](ARCHITECTURE.md)** - the big picture. Three layers
   (Textual UI, tmux, OpenSSH) on one process model, and exactly how a
   click on a host turns into a real terminal session. Start here.
2. **[DATA_MODEL.md](DATA_MODEL.md)** - the Location → Room → Host hierarchy,
   how it's held in memory, and how it's persisted to disk as JSON.
3. **[UI_GUIDE.md](UI_GUIDE.md)** - the Textual layer: the widget tree, the
   modal dialogs, the styling/theming system, the per-location colour palette.
4. **[TMUX_INTEGRATION.md](TMUX_INTEGRATION.md)** - the part that makes
   "connect" actually open something: session bootstrap, per-login
   isolation, pane splitting/stacking, mouse mode, truecolor passthrough,
   and how a session ever gets cleaned up.
5. **[SSH_AUTHENTICATION.md](SSH_AUTHENTICATION.md)** - what actually runs
   when you connect, and how key-based, no-password access works (or
   doesn't, and why).
6. **[MOBAXTERM_SETUP.md](MOBAXTERM_SETUP.md)** - **give this one to anyone
   you hand the program to.** The actual click-by-click MobaXterm settings
   needed to get in without typing a password.
7. **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** - symptom → cause → fix, for
   the specific bugs that came up building this and how they were diagnosed.

## The one-paragraph version

Jumpbox is a Textual TUI that runs *on* a jump host. It re-execs itself into
a tmux session on launch. Picking a host doesn't SSH from inside the Python
process at all - it splits a new tmux pane next to Jumpbox's own and runs a
plain `ssh -p <port> <user>@<host>` directly in it, one pane per open
connection, stacking down a column to the right of the dashboard. Whether
that connection needs a password depends entirely on normal OpenSSH
mechanics (an agent forwarded from your client, or a key already on the
box) - Jumpbox never touches a credential. Closing a connection is just
typing `exit` in its pane, same as any other terminal.
