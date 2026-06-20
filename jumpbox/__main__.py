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


def main() -> None:
    JumpboxApp().run()


if __name__ == "__main__":
    main()
