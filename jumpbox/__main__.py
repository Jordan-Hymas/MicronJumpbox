"""Console entry point: `jumpbox` (or `python -m jumpbox`) runs the app;
`jumpbox import/export/template` are the bulk-inventory commands (see
bulk.py) and never touch tmux or the UI - they're safe to run over a bare
ssh session, in a cron job, or piped into a file."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _run_app() -> None:
    # Force truecolor rendering before textual is imported (it reads this env
    # var at import time). Over SSH, only $TERM is reliably forwarded -
    # $COLORTERM isn't unless both client and server explicitly opt in - so
    # color-system auto-detection can misjudge the client and fall back to a
    # mode that skips painting the theme's background, leaving the terminal's
    # own background (e.g. MobaXterm's default white) showing through instead
    # of the dark theme.
    os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")

    from .app import JumpboxApp
    from .panes import TmuxUnavailable, ensure_in_tmux, kill_session

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


def _refuse_overwrite(path: str, force: bool) -> None:
    if Path(path).exists() and not force:
        raise SystemExit(f"{path} already exists - pass --force to overwrite it.")


def _cmd_import(args: argparse.Namespace) -> None:
    from . import bulk, storage

    fresh = not storage.inventory_path().exists()
    if fresh and not args.replace:
        print(
            "Note: no saved inventory yet, so the merge starts from the "
            "built-in demo data. Pass --replace to import onto a clean slate."
        )
    locations, tags = storage.load()
    try:
        new_locations, new_tags, report = bulk.import_csv(
            args.csv_file,
            locations,
            tags,
            replace=args.replace,
            config=storage.load_config(),
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Import failed: {exc}")

    print(report.summary())
    if args.dry_run:
        print("Dry run: nothing was written.")
        return
    if report.changed == 0 and not args.replace:
        print("Nothing to write.")
        return
    storage.save(new_locations, new_tags)
    print(
        f"Saved to {storage.inventory_path()} "
        f"(previous state kept as {storage.INVENTORY_FILENAME}.1)."
    )


def _cmd_export(args: argparse.Namespace) -> None:
    from . import bulk, storage

    _refuse_overwrite(args.csv_file, args.force)
    locations, _tags = storage.load()
    try:
        count = bulk.export_csv(args.csv_file, locations)
    except OSError as exc:
        raise SystemExit(f"Export failed: {exc}")
    print(f"Wrote {count} host{'s' if count != 1 else ''} to {args.csv_file}.")


def _cmd_template(args: argparse.Namespace) -> None:
    from . import bulk

    _refuse_overwrite(args.csv_file, args.force)
    try:
        bulk.write_template(args.csv_file)
    except OSError as exc:
        raise SystemExit(f"Couldn't write template: {exc}")
    print(
        f"Wrote a starter CSV to {args.csv_file} - edit/replace the example "
        "rows, then load it with: jumpbox import "
        f"{args.csv_file}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="jumpbox",
        description=(
            "Terminal SSH jump host dashboard. With no command, launches the "
            "app; the subcommands manage the inventory in bulk from CSV."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    cmd_import = sub.add_parser(
        "import",
        help="merge a CSV of hosts into the inventory (see 'jumpbox template')",
    )
    cmd_import.add_argument("csv_file", help="CSV file to read")
    cmd_import.add_argument(
        "--replace",
        action="store_true",
        help="discard the existing inventory and rebuild it from the CSV alone",
    )
    cmd_import.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without writing anything",
    )

    cmd_export = sub.add_parser(
        "export", help="dump the current inventory to a CSV (edit + re-import to bulk-edit)"
    )
    cmd_export.add_argument("csv_file", help="CSV file to write")
    cmd_export.add_argument(
        "--force", action="store_true", help="overwrite the file if it exists"
    )

    cmd_template = sub.add_parser(
        "template", help="write a starter CSV showing exactly what import expects"
    )
    cmd_template.add_argument("csv_file", help="CSV file to write")
    cmd_template.add_argument(
        "--force", action="store_true", help="overwrite the file if it exists"
    )

    args = parser.parse_args(argv)
    if args.command == "import":
        _cmd_import(args)
    elif args.command == "export":
        _cmd_export(args)
    elif args.command == "template":
        _cmd_template(args)
    else:
        _run_app()


if __name__ == "__main__":
    main()
