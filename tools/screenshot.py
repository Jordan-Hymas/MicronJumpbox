"""Render the app headlessly and save an SVG preview.

Usage:  python -m tools.screenshot [output.svg]
Open the resulting .svg in a browser to see exactly how the UI looks.
"""

import asyncio
import sys

from jumpbox.app import JumpboxApp


async def main(out: str) -> None:
    app = JumpboxApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        app.save_screenshot(out)
    print(f"Saved preview to {out}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "preview.svg"
    asyncio.run(main(out))
