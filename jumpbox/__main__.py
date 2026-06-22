"""Console entry point: lets you run `jumpbox` or `python -m jumpbox`."""

from __future__ import annotations

import os

# Force truecolor rendering before textual is imported (it reads this env var
# at import time). Over SSH, only $TERM is reliably forwarded - $COLORTERM
# isn't unless both client and server explicitly opt in - so color-system
# auto-detection can misjudge the client and fall back to a mode that skips
# painting the theme's background, leaving the terminal's own background
# (e.g. MobaXterm's default white) showing through instead of the dark theme.
os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")

from .app import JumpboxApp
from .panes import TmuxUnavailable, ensure_in_tmux, kill_session


def main() -> None:
    try:
        ensure_in_tmux()
    except TmuxUnavailable as exc:
        raise SystemExit(str(exc))

    app = JumpboxApp()
    try:
        app.run()
    finally:
        # Only set once Jumpbox itself confirmed it's sitting in a real
        # tmux pane (see App.on_mount). The `finally` means this still
        # runs even if the app crashed outright - every host pane closes
        # along with it either way, so the *next* launch never inherits a
        # stale session (ensure_in_tmux() also kills one up front, belt
        # and suspenders). Tearing it down here, after Textual has fully
        # restored the terminal, drops the tab back to a plain shell.
        if app.tmux_session:
            kill_session(app.tmux_session)


if __name__ == "__main__":
    main()
