# Deployment plan

Open items for moving Jumpbox off a dev machine and onto the real jump-host
VM, against the real network. Last updated 2026-06-23.

## Status

Jumpbox currently only runs/has been tested on a local dev machine. It has
not yet been deployed to the target VM, and the inventory is still the
built-in demo data (five buildings' worth of placeholder network gear -
see `data.py`), not the real device list.

## 1. Network reachability: VM → switches / APs / routers / firewalls

Jumpbox does a single *direct* SSH hop per connection - no jump chaining
(see [ARCHITECTURE.md](ARCHITECTURE.md), `connect.py`). That means the
bastion VM itself needs real L3/L4 reachability to every device it lists,
not just the people SSHing into the VM.

Plan:
- Put the VM on (or route it into) a dedicated **out-of-band management
  network/VLAN**, separate from production traffic, that reaches every
  building's management subnet (the A14/A12/B25/B35/C17-style ranges in
  the current demo data).
- On each device/firewall, restrict SSH/management access to **just the
  bastion VM's IP** rather than opening it to broader subnets.
- One bastion VM (or an HA pair) is enough as long as routing/firewall
  rules connect it to every site - no need for one per building.

Status: **blocked on the network team** provisioning the OOB network and
writing the ACLs. Not started.

## 2. Bulk-loading the real host inventory

The storage format (`storage.py` → `inventory.json`) already targets "tens
to low hundreds of hosts" - see [DATA_MODEL.md](DATA_MODEL.md) - so no
redesign is needed for scale. What doesn't scale is hand-adding hundreds
of hosts one at a time through the Add Host dialog.

Plan: write a one-off **bulk-import script** that reads from whatever
source of truth the real host list comes from (CSV/spreadsheet export from
a CMDB/IPAM/NetBox, or just a hand-built spreadsheet) and calls
`storage.save()` once with the fully-built `Location`/`Room`/`Host` tree.
The UI stays the day-to-day editing tool after that, not the initial
population method.

Status: **blocked on receiving the real host list.** Once it's available
(CSV or whatever format it's actually in), write the import script against
its real columns - the buildings/rooms/tag vocabulary already in `data.py`
are a reasonable starting template to map onto, but should be replaced
with the real site list rather than assumed.

## 3. SSH credentials when devices are normally accessed via SSO

Jumpbox never stores or injects credentials (`connect.py`) - it runs plain
`ssh` in a new tmux pane and lets normal OpenSSH mechanics (agent, a local
key, or a password prompt) take over exactly as if you'd typed the command
by hand. The open question is *which* of those actually applies once
"sign in via SSO" is in the picture for these devices. Three candidate
patterns, each needing different (or no) work:

1. **TACACS+/RADIUS AAA backed by the IdP.** The device itself prompts
   interactively (username/password, maybe an MFA push) once `ssh`
   connects - credentials are centrally managed/synced from the IdP, but
   the SSH session is still a normal interactive login. **Needs zero
   changes to Jumpbox** - the prompt just happens in the pane like any
   other ssh login.
2. **Short-lived SSH cert/key from an SSO broker** (Vault SSH secrets
   engine, Teleport, smallstep, etc.). A cert/key lands in an ssh-agent
   after a browser SSO step; that agent then needs to be **forwarded from
   the user's machine through to the VM** for ssh from inside a tmux pane
   to use it automatically. Jumpbox already detects a forwarded agent on
   startup (`forwarded_agent_available()` in `connect.py`) - this pattern
   would just need that forwarding actually set up end to end.
3. **Password vault / PAM checkout** (CyberArk, BeyondTrust, etc.). User
   checks out a credential via an SSO-gated UI, then pastes it in at the
   ssh password prompt. No automation on Jumpbox's side either possible or
   wanted here - it's intentionally hands-off with credentials.

Status: **blocked on having the VM + at least one real device** to test
against. Plan is to `ssh` to one real device by hand from the VM first and
see which pattern actually shows up, then decide whether Jumpbox needs
anything at all (most likely outcome: nothing, if it turns out to be
pattern 1).

## Suggested order of operations

1. Get the VM provisioned; network team sets up OOB reachability (§1).
2. Get the real host list and write the bulk-import script (§2) - can
   happen in parallel with §1.
3. Once the VM is reachable and has at least one real device on the list,
   test an interactive `ssh` by hand to determine the actual SSO pattern
   (§3) - last, since it depends on §1 (and ideally §2) being in place.
