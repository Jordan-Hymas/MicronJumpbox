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
   how it's held in memory, how it's persisted to disk as JSON (plus the
   rotating backups, config.json, history.json), and the bulk CSV
   import/export.
3. **[TMUX_INTEGRATION.md](TMUX_INTEGRATION.md)** - the part that makes
   "connect" actually open something: session bootstrap, per-login
   isolation, pane splitting/stacking, mouse mode, truecolor passthrough,
   and how a session ever gets cleaned up.
4. **[DEPLOYMENT_PLAN.md](DEPLOYMENT_PLAN.md)** - the open items for going
   from a dev machine to the real jump-host VM: network reachability to
   the actual switches/APs/routers/firewalls, bulk-loading the real host
   list (the tooling for which now exists - `jumpbox import`), and how
   SSH credentials should work once SSO is in the picture.

SSH authentication (how passwordless access works, and the MobaXterm
agent-forwarding setup to hand to new users) is covered in the top-level
[README](../README.md)'s "Passwordless access" section and in
`connect.py`'s module docstring.

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
