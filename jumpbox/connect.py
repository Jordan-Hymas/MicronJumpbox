"""Building the SSH command run in each new pane.

Every host pane runs *on this box* (see panes.py - it's a tmux pane
alongside Jumpbox's own, not a window anywhere else), and reaching the
hosts Jumpbox lists is the reason this box exists, so there's no jump
needed here: just one direct hop from here to the target.

That single hop is what makes key-based, no-password access work at all.
OpenSSH itself already tries every available identity automatically -
keys held by an agent before falling back to a password prompt - so
there's nothing for Jumpbox to configure for that. What actually has to be
true:

- **An agent is reachable here.** If you connected to this box from
  MobaXterm with "Forward SSH agent" / Pageant enabled, `$SSH_AUTH_SOCK`
  points at that forwarded agent, and ssh will offer whatever keys
  MobaXterm holds *with no copy of them ever touching this box*. tmux
  panes inherit this automatically (verified directly) - no extra wiring.
- **Or this box has its own key the target already trusts** - the normal
  `~/.ssh/id_*` / `~/.ssh/config` for whichever user is running Jumpbox,
  same as any other ssh client.

Either way, Jumpbox never sees, stores, or asks for a credential - if
neither applies for a given host, ssh just falls back to its normal
password prompt, exactly as if you'd typed the command yourself.
"""

from __future__ import annotations

import os
import shlex

from .data import Host


def connect_command(host: Host) -> str:
    """The ssh command a new pane runs to reach `host` directly.

    Host fields are free text from the Add Host form, and on a shared
    server one person's host entry ends up running in another person's
    pane, so the destination is shell-quoted: it must stay literal text,
    never extra shell commands, however it was typed into that form."""
    destination = shlex.quote(host.target)
    return f"ssh -p {host.port} {destination}"


def forwarded_agent_available() -> bool:
    """Whether an SSH agent is actually reachable here - e.g. because
    MobaXterm forwarded one (Pageant, or a real ssh-agent) when you
    connected to this box. Checks the socket path itself actually exists,
    not just that the env var is set, since a stale leftover value
    pointing at a socket that's gone would otherwise look like a real one."""
    sock = os.environ.get("SSH_AUTH_SOCK", "")
    return bool(sock) and os.path.exists(sock)
