"""Command line entry point: python -m email_sorter [--live] [--limit N] [--since DATE] [--check] [--recheck-expiry]"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError, load_config, load_credentials
from .runtime import BASE_DIR, LOCK_PATH, _lock_is_stale, keep_awake, setup_logging, single_instance  # noqa: F401

log = logging.getLogger("email_sorter")


def _past_date(value: str) -> date:
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None
    if d > date.today():
        raise argparse.ArgumentTypeError("date must not be in the future")
    return d


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="email_sorter", description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="actually move/flag mails (default is a dry run that only writes a CSV report)")
    parser.add_argument("--limit", type=int, help="classify at most N mails this run")
    parser.add_argument("--since", type=_past_date, metavar="YYYY-MM-DD",
                        help="manual backfill: sort all unprocessed mail received since this date "
                             "(month by month, newest first; the scheduled task never does this)")
    parser.add_argument("--check", action="store_true",
                        help="test IMAP login and the Jev connection, list folders, change nothing")
    parser.add_argument("--recheck-expiry", action="store_true",
                        help="one-off: find expiry dates of already sorted offers (with --live also tag expired ones)")
    parser.add_argument("--rename-category", nargs=2, metavar=("OLD", "NEW"),
                        help="after renaming a category key in config.toml: update the log (with --live)")
    parser.add_argument("--rename-folder", nargs=2, metavar=("OLD", "NEW"),
                        help="rename a folder on the server incl. subfolders and in the log, "
                             "e.g. INBOX/Newsletter INBOX/Werbung (with --live)")
    parser.add_argument("--resort-folder", metavar="FOLDER",
                        help="classify all mail in FOLDER again (e.g. INBOX/Reisen) and move what now belongs "
                             "elsewhere; with --limit N only the newest N (with --live)")
    parser.add_argument("--relocate", metavar="CATEGORY",
                        help="move already sorted mail of CATEGORY into the folder config.toml now gives it "
                             "(uses the log, no Jev requests; with --live)")
    parser.add_argument("--config", type=Path, default=BASE_DIR / "config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(BASE_DIR / ".env")
    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
        creds = load_credentials(cfg)
    except ConfigError as e:
        log.error("configuration error: %s", e)
        return 2

    if args.check:
        from .check import check
        return check(cfg, creds)

    from .sorter import run, run_backfill, run_recheck_expiry

    with single_instance(LOCK_PATH) as acquired, keep_awake():
        if not acquired:
            log.info("another run is still active, skipping")
            return 0
        task = ("relocate" if args.relocate else "folder re-sort" if args.resort_folder else "category rename" if args.rename_category else "folder rename" if args.rename_folder
                else "expiry recheck" if args.recheck_expiry else "run")
        log.info("starting %s %s", "LIVE" if args.live else "dry", task)
        try:
            if args.relocate:
                from .maintenance import relocate_category
                return relocate_category(cfg, creds, args.relocate, live=args.live).exit_code
            if args.resort_folder:
                from .resort import run_resort
                return run_resort(cfg, creds, BASE_DIR, args.resort_folder, live=args.live,
                                  limit=args.limit).exit_code
            if args.rename_category:
                from .maintenance import rename_category
                return rename_category(*args.rename_category, live=args.live).exit_code
            if args.rename_folder:
                from .maintenance import rename_folder
                return rename_folder(cfg, creds, *args.rename_folder, live=args.live).exit_code
            if args.recheck_expiry:
                return run_recheck_expiry(cfg, creds, BASE_DIR, live=args.live).exit_code
            if args.since:
                log.info("backfill since %s%s", args.since, f", at most {args.limit} mails" if args.limit else "")
                return run_backfill(cfg, creds, BASE_DIR, live=args.live, since=args.since,
                                    limit=args.limit).exit_code
            return run(cfg, creds, BASE_DIR, live=args.live, limit=args.limit).exit_code
        except Exception:
            log.exception("run failed")
            return 1


if __name__ == "__main__":
    sys.exit(main())
