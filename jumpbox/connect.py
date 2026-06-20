"""Launching SSH sessions on Linux.

There's no desktop to put a separate window on, so the session takes over the
*current* terminal instead. The caller is expected to wrap this in
`App.suspend()` (see app.py) so the dashboard hands off the terminal cleanly
and resumes when the session ends. Only one session at a time - select a
host, you're in it; exit it, you're back at the dashboard.

For now this is a *demo*: each session shows a placeholder banner instead of
really connecting. Flip ``DEMO_MODE`` to ``False`` (or set ``JUMPBOX_DEMO=0``)
once SSH keys are ready, and the generated script will run the real ``ssh``
command instead.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
import uuid
from pathlib import Path

from .data import Host

# Real SSH by default now that there's a real inventory to connect to. Set
# JUMPBOX_DEMO=1 to force the placeholder banner back on (e.g. for a demo).
DEMO_MODE = os.environ.get("JUMPBOX_DEMO", "0") != "0"

_SESSION_DIR = Path(tempfile.gettempdir()) / "jumpbox-sessions"


def _safe(name: str) -> str:
    """Make a host name safe to use as a filename."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def _echo(text: str) -> str:
    """A bash `echo` of literal text - safe regardless of what `text`
    contains (quotes, `$`, backticks, `;`, ...). Host name/address/username/
    description all come from a free-text form, and on a shared server one
    person's host entry runs in *another* person's session, so this can't
    just trust the content the way a single-user demo could have."""
    return "echo " + shlex.quote(text)


def _posix_script_lines(host: Host) -> list[str]:
    lines = [
        "#!/usr/bin/env bash",
        _echo(""),
        _echo("  ==========================================================="),
        _echo(f"    JUMPBOX SESSION   {host.name}"),
        _echo("  ==========================================================="),
        _echo(""),
        _echo(f"    Target : {host.target}"),
        _echo(f"    Port   : {host.port}"),
        _echo(f"    OS     : {host.os}"),
        _echo(f"    Notes  : {host.description}"),
        _echo(""),
    ]
    if DEMO_MODE:
        lines += [
            _echo("    [DEMO] SSH is not wired up yet - this is a placeholder."),
            _echo(f"    [DEMO] Later this runs:  ssh -p {host.port} {host.target}"),
            _echo(""),
            _echo("    Type 'exit' to jump back to Jumpbox."),
            _echo(""),
        ]
    else:
        lines += [
            _echo(f"    Connecting to {host.target} ..."),
            _echo(""),
            f"ssh -p {shlex.quote(str(host.port))} {shlex.quote(host.target)}",
            _echo(""),
            _echo("    Session ended. Type 'exit' to jump back to Jumpbox."),
        ]
    # `bash script.sh` has no built-in "stay open afterwards" flag - without
    # this, bash would exit (and the suspended terminal would just drop back
    # to Jumpbox) the instant the script's last line finishes. exec'ing a
    # shell as the final line replaces this process with an interactive one
    # instead, so "type 'exit'" above means something - there's an actual
    # prompt to exit from.
    lines.append("exec bash")
    return lines


def _session_script(host: Host) -> Path:
    """Write a per-host script describing the session, return its path."""
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    # Unique per launch (not just per host) so connecting to the same host
    # twice - or many hosts at once - never races two sessions on one file.
    unique = uuid.uuid4().hex[:8]

    script = _SESSION_DIR / f"session-{_safe(host.name)}-{unique}.sh"
    script.write_text("\n".join(_posix_script_lines(host)) + "\n", encoding="utf-8")
    return script


def launch_session(host: Host) -> list[str]:
    """Build the command that opens a session for `host`.

    Nothing is spawned here - the caller runs the returned command itself,
    inside `App.suspend()`, so it can take over the current terminal.
    """
    script = _session_script(host)
    return ["bash", str(script)]
