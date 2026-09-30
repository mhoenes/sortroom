"""Command line entry point: python -m email_sorter [--mailbox ID] [--live] [--limit N] [--since DATE] [--check] …"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError, Mailbox, load_credentials, load_mailboxes
from .runtime import BASE_DIR, _lock_is_stale, default_config_path, keep_awake, setup_logging, single_instance  # noqa: F401

log = logging.getLogger("email_sorter")


def _past_date(value: str) -> date:
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None
    if d > date.today():
        raise argparse.ArgumentTypeError("date must not be in the future")
    return d


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="email_sorter", description=__doc__)
    parser.add_argument("--mailbox", metavar="ID",
                        help="only this mailbox (folder name under mailboxes/); normal runs and --check "
                             "default to all mailboxes, maintenance commands need one if there are several")
    parser.add_argument("--live", action="store_true",
                        help="actually move/flag mails (default is a dry run that only writes a CSV report)")
    parser.add_argument("--limit", type=int, help="classify at most N mails this run")
    parser.add_argument("--since", type=_past_date, metavar="YYYY-MM-DD",
                        help="manual backfill: sort all unprocessed mail received since this date "
                             "(month by month, newest first; the scheduled task never does this)")
    parser.add_argument("--check", action="store_true",
                        help="test IMAP login and the classifier connection, list folders, change nothing")
    parser.add_argument("--recheck-expiry", action="store_true",
                        help="one-off: find expiry dates of already sorted offers (with --live also move expired ones)")
    parser.add_argument("--rename-category", nargs=2, metavar=("OLD", "NEW"),
                        help="after renaming a category key in the mailbox config: update the log (with --live)")
    parser.add_argument("--rename-folder", nargs=2, metavar=("OLD", "NEW"),
                        help="rename a folder on the server incl. subfolders and in the log, "
                             "e.g. INBOX/Newsletter INBOX/Werbung (with --live)")
    parser.add_argument("--resort-folder", metavar="FOLDER",
                        help="classify all mail in FOLDER again (e.g. INBOX/Reisen) and move what now belongs "
                             "elsewhere; with --limit N only the newest N (with --live)")
    parser.add_argument("--relocate", metavar="CATEGORY",
                        help="move already sorted mail of CATEGORY into the folder its config now gives it "
                             "(uses the log, no classifier requests; with --live)")
    parser.add_argument("--reconcile", action="store_true",
                        help="compare the log with the mailbox: mark deleted mails, note mails filed by hand "
                             "(with --live; changes nothing on the server)")
    parser.add_argument("--undo-run", metavar="START",
                        help="undo a live run: move its mails back where they came from and remove its stars; "
                             "START is the run's start time as in the run log (2026-09-30T10:12:00) or 'last' "
                             "(with --live)")
    parser.add_argument("--sort-again", action="store_true",
                        help="with --undo-run: sort the mails again at the next run instead of leaving them")
    parser.add_argument("--delete-mailbox", metavar="ID",
                        help="delete a mailbox for good: its settings, login, log and reports "
                             "(nothing on the IMAP server); asks for confirmation unless --yes")
    parser.add_argument("--yes", action="store_true", help="with --delete-mailbox: don't ask")
    parser.add_argument("--config", type=Path, help="shared config (default: config/config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _task(args) -> str:
    for flag, name in (("undo_run", "undo"), ("reconcile", "reconcile"), ("relocate", "relocate"), ("resort_folder", "folder re-sort"),
                       ("rename_category", "category rename"), ("rename_folder", "folder rename"),
                       ("recheck_expiry", "expiry recheck"), ("since", "backfill")):
        if getattr(args, flag):
            return name
    return "run"


def _run_one(box: Mailbox, args) -> int:
    """One task on one mailbox, under that mailbox's lock."""
    from .sorter import run, run_backfill, run_recheck_expiry

    cfg, work = box.cfg, box.workspace
    try:
        creds = load_credentials(box)
    except ConfigError as e:
        log.error("[%s] configuration error: %s", box.id, e)
        return 2
    with single_instance(box.lock_path) as acquired:
        if not acquired:
            log.info("[%s] another run is still active, skipping", box.id)
            return 0
        log.info("[%s] starting %s %s", box.id, "LIVE" if args.live else "dry", _task(args))
        try:
            if args.undo_run:
                from .undo import run_undo
                return run_undo(cfg, creds, work, args.undo_run, live=args.live,
                                sort_again=args.sort_again)["exit_code"]
            if args.reconcile:
                from .reconcile import run_reconcile
                return 0 if run_reconcile(cfg, creds, work, live=args.live)["ok"] else 1
            if args.relocate:
                from .maintenance import relocate_category
                return relocate_category(cfg, creds, args.relocate, live=args.live, base_dir=work).exit_code
            if args.resort_folder:
                from .resort import run_resort
                return run_resort(cfg, creds, work, args.resort_folder, live=args.live, limit=args.limit).exit_code
            if args.rename_category:
                from .maintenance import rename_category
                return rename_category(*args.rename_category, live=args.live, base_dir=work).exit_code
            if args.rename_folder:
                from .maintenance import rename_folder
                return rename_folder(cfg, creds, *args.rename_folder, live=args.live, base_dir=work).exit_code
            if args.recheck_expiry:
                return run_recheck_expiry(cfg, creds, work, live=args.live).exit_code
            if args.since:
                log.info("[%s] backfill since %s%s", box.id, args.since,
                         f", at most {args.limit} mails" if args.limit else "")
                return run_backfill(cfg, creds, work, live=args.live, since=args.since, limit=args.limit).exit_code
            return run(cfg, creds, work, live=args.live, limit=args.limit).exit_code
        except Exception:
            log.exception("[%s] run failed", box.id)
            return 1


def _delete(boxes: dict[str, Mailbox], box_id: str, yes: bool) -> int:
    from .removal import MailboxBusy, delete_mailbox

    if box_id not in boxes:
        log.error("unknown mailbox %r - configured: %s", box_id, ", ".join(boxes) or "none")
        return 2
    box = boxes[box_id]
    if not yes:
        print(f'This deletes the mailbox "{box.name}" ({box.id}) for good: its settings, login, log and '
              "reports. Nothing on the IMAP server is changed.")
        try:
            answer = input(f"Type {box.id} to confirm: ")
        except EOFError:
            answer = ""
        if answer.strip() != box.id:
            print("Not deleted. (Without a terminal, confirm with --yes.)")
            return 1
    try:
        result = delete_mailbox(box)
    except MailboxBusy:
        log.error("[%s] not deleted: another run is active", box.id)
        return 1
    print(f'Mailbox "{box.name}" deleted.')
    if result["revoked"] is False:
        print("Revoking the Google sign-in failed - remove Sortroom's access at myaccount.google.com/connections.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(BASE_DIR / ".env")
    args.config = args.config or default_config_path()
    setup_logging(args.verbose)

    try:
        boxes = load_mailboxes(BASE_DIR, args.config)
    except ConfigError as e:
        log.error("configuration error: %s", e)
        return 2
    if args.delete_mailbox:
        return _delete(boxes, args.delete_mailbox, args.yes)
    if args.mailbox in boxes.broken:
        log.error("[%s] configuration error: %s", args.mailbox, boxes.broken[args.mailbox])
        return 2
    for box_id, error in boxes.broken.items() if not args.mailbox else ():
        log.error("[%s] configuration error, skipped: %s", box_id, error)
    skipped = 2 if boxes.broken and not args.mailbox else 0  # the others run, the exit code still tells
    if not boxes:
        log.error("no mailbox configured yet: add one in the admin UI (Add mailbox)")
        return 2

    if args.mailbox:
        if args.mailbox not in boxes:
            log.error("unknown mailbox %r - configured: %s", args.mailbox, ", ".join(boxes))
            return 2
        selected = [boxes[args.mailbox]]
    elif _task(args) == "run" or args.check or len(boxes) == 1:
        selected = list(boxes.values())
    else:
        log.error("%s needs --mailbox when several are configured: %s", _task(args), ", ".join(boxes))
        return 2

    if args.check:
        from .check import check
        codes = []
        for box in selected:
            print(f"=== {box.name} ({box.id}) ===")
            try:  # the mailbox and the model; the admin UI checks them separately
                codes.append(check(box.cfg, load_credentials(box)))
            except ConfigError as e:
                print(f"  FAILED: {e}")
                codes.append(2)
        return max(codes + [skipped])

    with keep_awake():
        return max([_run_one(box, args) for box in selected] + [skipped])


if __name__ == "__main__":
    sys.exit(main())
