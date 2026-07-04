# Changelog

## 2026-07-04 — Bulk import, live status, quick connect, fullscreen, editing

The big push from demo-with-dummy-data toward the real deployment. Full
detail lives in the README and `docs/`; this is the summary of what landed.

### Bulk CSV import/export (`jumpbox/bulk.py` + CLI)

The way the real host list gets loaded — no more hand-adding hosts one
dialog at a time:

```bash
jumpbox template hosts.csv          # starter CSV showing every column
jumpbox import hosts.csv --dry-run  # report what would change, write nothing
jumpbox import hosts.csv            # merge into the existing inventory
jumpbox import hosts.csv --replace  # wipe and rebuild (initial population)
jumpbox export hosts.csv            # dump back out (bulk-edit in Excel, re-import)
```

- Merge matches hosts **by name** across the whole inventory: re-imports
  refresh fields in place and move hosts whose room changed — never
  duplicate. Blank optional cells keep existing values.
- Bad rows are skipped and reported with line number + reason; they never
  abort the rest. Missing locations/rooms/tags are created.
- `export` → `import --replace` is a lossless round trip.

### In the app

- **Ctrl+F Quick Connect** — palette that fuzzy-searches every host in
  every location at once; Enter connects the top match.
- **⛶ Fullscreen / F4** — zooms the newest open host pane over the whole
  window (tmux zoom). Ctrl+B then Z comes back; a fullscreened session
  ending (`exit`) lands back on the dashboard automatically. Buttons on
  the Dashboard and the Activity tab, enabled only while something is open.
- **Edit** for hosts, rooms, and locations in the "+" menus — the same
  forms, opened prefilled with a Save button. Fixing a typo'd IP is no
  longer delete-and-re-add.
- **Live status dots** — a background sweep TCP-probes every host's ssh
  port (`probe_interval` seconds, default 30): open = ONLINE, refused =
  DEGRADED, no answer = OFFLINE. `Status` is no longer decorative.
- **Connection history** — "last connected 2h ago · 5 times" in the detail
  panel and Quick Connect, persisted across runs (`history.json`).
- **Layout fix** — with a host pane open (Jumpbox squeezed to ~40% of the
  terminal), the HOSTS panel now gets the bigger share of the dashboard
  instead of a fixed-width LOCATIONS panel squeezing it into wrapped rows.

### Storage & safety

- **Rotating backups**: every save keeps the previous inventory as
  `inventory.json.1` … `.5`. Any mistake — bad bulk import, fat-fingered
  delete — is undoable by copying a backup back and hitting F5.
- **`config.json`** (created with defaults on first run):
  `default_username`/`default_port`/`default_os` (prefill the Add Host
  form, fill blank CSV cells), `probe_interval`/`probe_timeout`, and
  site-wide `ssh_options` applied to every connection.
- **Per-host `ssh_args`** for gear needing legacy crypto (e.g.
  `-o KexAlgorithms=+diffie-hellman-group14-sha1`). Options that make ssh
  run a *local* command (`ProxyCommand`, `LocalCommand`) are refused at
  entry and stripped at connect time — on a shared bastion, one person's
  host entry runs in another person's pane and must stay inert.

### Tests & housekeeping

- Smoke suite roughly doubled: bulk import/export round trips, probe
  classification against real local sockets, ssh-option injection safety,
  edit forms, Quick Connect, fullscreen button states, backup rotation,
  history persistence, the CLI itself, and a narrow-width layout
  regression check.
- Removed stray scratch files; fixed dead links in `docs/`; venv
  reinstalled editable so `run.sh` always runs the repo's current code.
