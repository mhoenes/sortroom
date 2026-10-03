"""Container start (the image's ENTRYPOINT): run Sortroom as the user and group from PUID / PGID.

Docker starts the container as root. This gives the mounted folders to PUID:PGID (default 1000:1000,
the image's own user) where their owner doesn't match yet, then gives up root for good and starts the
command (uvicorn). Started as another user already (docker run --user, compose user:), it starts the
command as it is and leaves the folders alone.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

DEFAULT_ID = 1000
FOLDERS = (Path("/app/config"), Path("/app/mailboxes"), Path("/app/logs"))


def ids(env: Mapping[str, str]) -> tuple[int, int]:
    """(uid, gid) from PUID and PGID. Refuses what isn't a whole number, and 0: Sortroom never runs as root."""
    found = []
    for name in ("PUID", "PGID"):
        raw = (env.get(name) or "").strip()
        if not raw:
            found.append(DEFAULT_ID)
        elif raw.isdigit() and 0 < int(raw) < 2 ** 31:
            found.append(int(raw))
        else:
            raise ValueError(f"{name}={raw!r} is not allowed: a whole number from 1 (0 would be root)")
    return found[0], found[1]


def not_owned(folders: tuple[Path, ...], uid: int, gid: int) -> Iterator[Path]:
    """The folders and everything in them that doesn't belong to uid:gid yet (symlinks not followed)."""
    for top in folders:
        if not top.is_dir():
            continue
        for root, dirs, files in os.walk(top):
            # every folder once, as root; links to folders show up in dirs but aren't walked into
            links = [n for n in dirs if os.path.islink(os.path.join(root, n))]
            for path in [Path(root)] + [Path(root, n) for n in files + links]:
                try:
                    st = path.lstat()
                except OSError:
                    continue
                if (st.st_uid, st.st_gid) != (uid, gid):
                    yield path


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit("usage: python -m email_sorter.container <command> [args]")
    if sys.platform != "win32":
        if os.geteuid() == 0:
            try:
                uid, gid = ids(os.environ)
            except ValueError as e:
                sys.exit(f"sortroom: {e}")
            changed, failed, error = 0, 0, None
            for path in not_owned(FOLDERS, uid, gid):
                try:
                    os.lchown(path, uid, gid)
                    changed += 1
                except OSError as e:  # e.g. a network share that doesn't allow it
                    failed, error = failed + 1, e
            if changed:
                print(f"sortroom: gave {changed} file(s) and folder(s) to {uid}:{gid}", flush=True)
            if failed:
                print(f"sortroom: could not change the owner of {failed} file(s) or folder(s) ({error}); "
                      "the admin UI shows what Sortroom can't write as read-only", file=sys.stderr, flush=True)
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)  # for good: a process that isn't root can't get it back
            os.environ["HOME"] = "/app"
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main(sys.argv[1:])
