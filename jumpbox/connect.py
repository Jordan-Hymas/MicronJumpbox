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

import asyncio
import os
import shlex
from typing import Iterable

from .data import Host, Status

# ssh options that make *ssh itself* execute a local command line - which
# would hand one user's host entry arbitrary code execution in another
# user's pane on a shared bastion, quoting or no quoting. Matched
# case-insensitively as substrings (ssh_config option names are
# case-insensitive, and `-oProxyCommand=…`/`-o ProxyCommand=…` both exist).
_FORBIDDEN_SSH_OPTIONS = ("proxycommand", "localcommand", "permitlocalcommand")


def ssh_args_error(args: Iterable[str]) -> str | None:
    """Why these per-host ssh options can't be accepted, or None if they're
    fine. Used by the Add/Edit Host form and the bulk importer, so a bad
    option is refused at entry time with a reason, not silently dropped."""
    for arg in args:
        lowered = arg.lower()
        for option in _FORBIDDEN_SSH_OPTIONS:
            if option in lowered:
                return (
                    f"ssh option {arg!r} isn't allowed: it can run a local "
                    "command on this box."
                )
    return None


def safe_ssh_args(args: Iterable[str]) -> tuple[str, ...]:
    """`args` with any forbidden option stripped - defence in depth for
    hosts that never went through the form/importer (a hand-edited
    inventory.json), so a forbidden option can't run even then. A
    forbidden value's own `-o` flag is dropped with it, so the remaining
    command stays well-formed instead of `-o` swallowing the destination."""
    items = list(args)
    kept: list[str] = []
    index = 0
    while index < len(items):
        arg = items[index]
        if arg == "-o" and index + 1 < len(items) and ssh_args_error([items[index + 1]]):
            index += 2
            continue
        if ssh_args_error([arg]):
            index += 1
            continue
        kept.append(arg)
        index += 1
    return tuple(kept)


def connect_command(host: Host, base_args: Iterable[str] = ()) -> str:
    """The ssh command a new pane runs to reach `host` directly.
    `base_args` are site-wide options from config.json (applied to every
    connection); the host's own ssh_args come after them so a per-host
    option wins when both set the same one.

    Host fields are free text from the Add Host form, and on a shared
    server one person's host entry ends up running in another person's
    pane, so the destination and every extra argument are shell-quoted:
    they must stay literal text, never extra shell commands, however they
    were typed into that form (or edited into the JSON by hand)."""
    destination = shlex.quote(host.target)
    args = safe_ssh_args(tuple(base_args) + host.ssh_args)
    extra = " ".join(shlex.quote(arg) for arg in args)
    if extra:
        return f"ssh -p {host.port} {extra} {destination}"
    return f"ssh -p {host.port} {destination}"


async def probe(address: str, port: int, timeout: float) -> Status:
    """One live reachability check of a host's ssh port, from this box -
    which is the box whose reachability actually matters (see
    ARCHITECTURE.md). Interpreting the three possible outcomes:

    - the port accepts a TCP connection -> ONLINE (something is listening
      where ssh will connect);
    - the machine answers but *refuses* that port -> DEGRADED (the host is
      up, but connecting will fail - wrong port, sshd down);
    - no answer within `timeout`, or no route -> OFFLINE.

    Nothing is sent on the connection - it's opened and closed immediately,
    which sshd handles silently long before any auth/logging kicks in."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(address, port), timeout
        )
    except ConnectionRefusedError:
        return Status.DEGRADED
    except (OSError, asyncio.TimeoutError):
        return Status.OFFLINE
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return Status.ONLINE


def forwarded_agent_available() -> bool:
    """Whether an SSH agent is actually reachable here - e.g. because
    MobaXterm forwarded one (Pageant, or a real ssh-agent) when you
    connected to this box. Checks the socket path itself actually exists,
    not just that the env var is set, since a stale leftover value
    pointing at a socket that's gone would otherwise look like a real one."""
    sock = os.environ.get("SSH_AUTH_SOCK", "")
    return bool(sock) and os.path.exists(sock)
