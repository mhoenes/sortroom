# Sortroom

Sorts a Strato IMAP inbox into folders using **Jev** (TypeSafe's "System One"
decision model) through **OpenRouter** (or Vercel AI Gateway). Jev doesn't generate text – it picks one
of your categories and returns probabilities, so it can never invent a folder.

For every new mail it asks Jev two questions in one request:

| Question | Type | Used for |
|---|---|---|
| Which category? | `choice` | moving the mail into its folder |
| Does it need action from me? | `noul` (yes/no probability) | flagging the mail |

Cost is roughly $0.00002 per mail (input tokens only; output is free).

## Renamed from email-sorter (0.5.0)

The project was called *email-sorter* before 0.5.0. The Python package is still
`email_sorter` (`python -m email_sorter …` is unchanged). What changed:

| before | since 0.5.0 |
|---|---|
| repository `mhoenes/email-sorter` | `mhoenes/sortroom` (GitHub redirects the old URL) |
| image `ghcr.io/mhoenes/email-sorter` | `ghcr.io/mhoenes/sortroom` |
| `.env`: `EMAIL_SORTER_IMAGE` (required) | `SORTROOM_IMAGE` (optional, defaults to `ghcr.io/mhoenes/sortroom:latest`) |
| Compose service and container `email-sorter` | `sortroom` |
| n8n URL `http://email-sorter:8765` | `http://sortroom:8765` – switch your n8n workflows before updating |
| `logs/email-sorter.log` | `logs/sortroom.log` |

Updating a host: use the new `docker-compose.yml` (it defaults to
`ghcr.io/mhoenes/sortroom:latest`; drop `EMAIL_SORTER_IMAGE` from `.env`), or in
your own copy rename the service and container to `sortroom` and point `image` at
`ghcr.io/mhoenes/sortroom:latest`. Switch the n8n workflows to
`http://sortroom:8765`. Then
`docker compose pull && docker compose up -d --remove-orphans` (removes the old
`email-sorter` container; `data/`, `mailboxes/`, `logs/` and `reports/` are kept).

## Admin UI

The container serves a browser UI next to the API: `http://<docker-host>:8765`
(port via `UI_PORT`). Log in with `ADMIN_PASSWORD` from `.env` (at least 8
characters; without it the UI stays locked). Sessions last 7 days; failed
logins are slowed down. Behind an HTTPS reverse proxy set `UI_SECURE_COOKIES=1`.

- **Alle Postfächer** – every mailbox with status, today's count, uncertain
  mail and this month's cost
- **Übersicht** (per mailbox) – last run, distribution of the last 7 days,
  run log, uncertain mail
- **Mails** – everything sorted, with search and filters (category, folder,
  period, uncertain, flagged) and a detail panel

- **Kategorien** – edit name, description, folder, star/expiry switches and a
  per-category folder for expired offers; add or delete categories.
  **Mit Jev testen** classifies the category's last 10 mails and 15 others with
  the draft description (read-only, nothing is moved; about $0.002) and shows
  what would change
- **Einstellungen** – display name, IMAP server/port/inbox, thresholds, waiting
  time, look-back, max per run, default folder for expired offers, and the
  sender rules table

Edits are written to the mailbox's `mailbox.toml` (comments are kept), checked
exactly like the sorter loads them, and the previous version is kept as
`mailbox.toml.bak`. They apply from the next run. A file the container can't
write (e.g. a single-file `config.toml` mounted `:ro`) is shown read-only – run
`--migrate-mailbox` to get an editable `mailboxes/<id>/mailbox.toml`, and make
sure `./mailboxes` is writable for uid 1000. Forms carry a CSRF token.

- **Wartung** – start a run, backfill, re-sort a folder, move a category into
  its new folder, rename a folder (server, log and settings) or a category key,
  recheck expiry dates, reconcile the log with the mailbox (marks mails you
  deleted as "nicht mehr im Postfach" so they leave the review list; notes
  uncertain mails you filed by hand; reads only, trash/spam/sent/drafts don't
  count), check the connection. Each job runs in the background
  under the mailbox lock (dry run unless "Echt ausführen" is ticked) and shows
  its log; also shows whether the mailbox's .env variables are set
- **Mails** detail – accept Jev's suggestion for an uncertain mail, move a mail
  to another category (logged as "von Hand"), or create a sender rule from it
- **Postfach hinzufügen** – creates `mailboxes/<id>/mailbox.toml` with the
  categories of an existing mailbox; credentials go into `.env` under the
  variable names you choose (restart the container afterwards). Not available
  while a single-file `config.toml` is used – migrate first. For Gmail (`imap.gmail.com`) the
  copied folders lose their `INBOX/` prefix: Gmail only has top-level labels
  (`Werbung`, not `INBOX/Werbung`); Outlook still shows them under the inbox
- **Gemeinsam** – the shared `[jev]` settings in `config.toml` (read-only when
  it is mounted `:ro`)

Jobs are kept in memory until the container restarts; backfills started via
`POST /backfill` show up there too. The API endpoints keep their bearer-token
auth.

## Several mailboxes

Each mailbox lives in its own folder with its own settings, log and reports:

```
config/config.toml           # shared: [jev] (gateway, model, key name, text length)
mailboxes/
  privat/
    mailbox.toml             # name, [imap], [rules], [categories.*]
    data/state.db, data/run.lock
    reports/
  gmail/
    mailbox.toml
    ...
```

`mailbox.toml` starts with `name = "Privat"`; in `[imap]`, `user_env` and
`password_env` name the `.env` variables with that mailbox's login (default
`IMAP_USER` / `IMAP_PASSWORD`), e.g. `GMAIL_USER` / `GMAIL_PASSWORD`.

Without a `mailboxes/` folder, a `config.toml` that still holds `[imap]`,
`[rules]` and `[categories]` is the single mailbox `default`, exactly as
before. To switch an existing setup (dry run first, then `--live`):

```powershell
.venv\Scripts\python -m email_sorter --migrate-mailbox privat --name Privat
```

In Docker (stop the service first; `./mailboxes` must be writable for uid 1000):

```bash
docker compose stop sortroom
mkdir -p mailboxes && sudo chown -R 1000:1000 mailboxes
docker compose run --rm sortroom python -m email_sorter --migrate-mailbox privat --name Privat --live
docker compose up -d
```

It writes `mailboxes/privat/mailbox.toml` from those sections and moves
`data/state.db` and the reports there. `config.toml` is not touched; its
mailbox sections are ignored from then on and can be deleted.

Normal runs and `--check` cover every mailbox; `--mailbox privat` limits to
one. Maintenance commands (`--since`, `--resort-folder`, `--rename-*`,
`--relocate`, `--recheck-expiry`) need `--mailbox` when there are several.
The HTTP API: `POST /run` runs all (or `{"mailbox": "privat"}`) and answers
`{"ok": …, "results": {"privat": {…}}}`; `/backfill` and `/recheck-expiry`
take `"mailbox"`; `GET /mailboxes` lists them.

## Setup

```powershell
cd C:\Projekte\sortroom
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then fill in IMAP_USER, IMAP_PASSWORD, OPENROUTER_API_KEY
```

## Usage

```powershell
.venv\Scripts\python -m email_sorter --check      # test login + Jev, list target folders
.venv\Scripts\python -m email_sorter              # DRY RUN: classify, write reports\dry-run-*.csv, change nothing
.venv\Scripts\python -m email_sorter --live       # move + flag for real
```

Options: `--limit N` (classify at most N mails), `-v` (log every decision),
`--config other.toml`.

Fresh mail waits `min_age_hours` in the inbox before it is sorted. With
`sort_read_at_once = true` in `[rules]` (Einstellungen → "Gelesene sofort
einsortieren"), mail you have already read is sorted at the next run anyway.

**Recommended rollout:** run dry runs for a few days, open the CSV reports
(they open directly in German Excel), sharpen category descriptions in
`config.toml` where Jev got it wrong, then go live.

Dry runs don't record anything, so each one re-classifies the same mails
(a fraction of a cent) – that's intended, so you can compare after tweaking.

## Sorting older mail (manual backfill)

Normal and scheduled runs only look at the last `lookback_days`. Older mail is
sorted only when you start it yourself (CLI, Wartung page or `POST /backfill`) –
the built-in schedule never does:

```powershell
.venv\Scripts\python -m email_sorter --since 2026-01-01 --limit 100   # dry run, report only
.venv\Scripts\python -m email_sorter --since 2026-01-01 --live        # sort for real
```

It works month by month, newest first, in batches of `max_per_run`. Live
batches are applied immediately, so you can stop it (Ctrl+C) and start it
again later – it continues where it left off. While it runs, scheduled runs
are skipped (shared lock). Old offers whose deadline has passed go straight
to `Werbung/Abgelaufen`.

## Running in Docker

The container serves the admin UI on port 8765 and runs every mailbox on its
own schedule (see *Running automatically*); nothing else is needed.

It also has a small HTTP API to trigger runs from outside. All endpoints except
`/health` need `Authorization: Bearer <API_TOKEN>`; runs share the mailbox lock,
so while one runs, `/run` answers 409.

| Endpoint | What |
|---|---|
| `POST /run` `{"live": true}` | normal run (last `lookback_days`), returns a JSON summary |
| `POST /backfill` `{"since": "2024-01-01", "live": false}` | backfill in the background, returns a job (202) |
| `GET /jobs/{id}` | status and summary of a backfill job |
| `POST /recheck-expiry` `{"live": true}` | find expiry dates of already sorted offers |
| `GET /health` | liveness, no auth |

### Docker image

GitHub Actions (`.github/workflows/docker.yml`) runs the tests on every push
and publishes the image to GitHub Container Registry for amd64 and arm64:

- `ghcr.io/<owner>/sortroom:latest` – latest `main`
- `ghcr.io/<owner>/sortroom:0.5.0` / `:0.5` – version tags `v0.5.0`
- `ghcr.io/<owner>/sortroom:sha-abc1234` – every published commit

If the repository is private, the package is private too; log in once on the
Docker host with a personal access token that has `read:packages`:
`echo <token> | docker login ghcr.io -u <github-user> --password-stdin`.

### Setup on the Docker host

1. On the host you only need `docker-compose.yml`, `.env` and
   `config/config.toml` – the code comes with the image. `docker-compose.yml`
   uses `ghcr.io/mhoenes/sortroom:latest`; for a fork or a pinned version
   change the `image` line or set `SORTROOM_IMAGE` in `.env`.
2. `mkdir -p config logs reports mailboxes && sudo chown -R 1000:1000 config logs reports mailboxes`
   (the container runs as uid 1000), then put `config.toml` into `config/` –
   start from `config/config.toml` of this repository.
3. Create `.env` (see `.env.example`): IMAP, `OPENROUTER_API_KEY` and
   `ADMIN_PASSWORD`; `API_TOKEN` only if you want to use the HTTP API.
4. Turn the mailbox sections of `config.toml` into the first mailbox:
   `docker compose run --rm sortroom python -m email_sorter --migrate-mailbox privat --name Privat --live`.
   Further mailboxes are added in the UI ("Postfach hinzufügen"). Coming
   from an older installation, copy its `data/state.db` to
   `mailboxes/privat/data/state.db` so already sorted mail isn't sorted again.
5. `docker compose pull && docker compose up -d`, then check:
   `docker compose logs -f sortroom` and
   `docker exec sortroom python -m email_sorter --check`.

**Updating from 0.6.0 or earlier:** the shared config moved into a folder and
`data/` is no longer mounted (it only held the log of single-file setups,
which now live in `mailboxes/<id>/data/`). On the host: `mkdir config &&
mv config.toml config/ && sudo chown -R 1000:1000 config`; in your compose
file replace `./config.toml:/app/config.toml:ro` with `./config:/app/config`
and remove `./data:/app/data`; then `docker compose up -d`. Until the compose
file is changed, the old mount keeps working. If Sortroom logs a warning
about a single-file setup at startup, run `--migrate-mailbox` first.

### Upgrading from an n8n setup (before 0.6.0)

Runs used to be started by an n8n workflow. Since 0.6.0 Sortroom starts them
itself, every 10 minutes per mailbox by default. After updating:

1. Check Einstellungen → Zeitplan for each mailbox (on, 10 minutes).
2. Deactivate the n8n workflow. Until then nothing runs twice – a run that
   finds the mailbox busy is skipped – but there are more runs than needed.
3. In your own `docker-compose.yml`, the `networks:` entries for n8n can go.

**Don't run two sorters on the same mailbox** (e.g. the container and a local
copy): each installation has its own `state.db` and they would not know about
each other's work.

## How it decides

1. Looks at mails in `INBOX` from the last `lookback_days` (default 7) that
   aren't in `data/state.db` yet and arrived at least `min_age_hours` ago
   (default 24, by the server's arrival time) – newer mail stays in the
   inbox for a day and is picked up by a later run.
2. Sends sender, recipient, subject, attachment names, whether it's a mailing
   list, and the cleaned body (first 3000 chars, quotes/signatures removed).
3. Confidence ≥ `min_confidence` (0.70) → moved to the category's folder.
   Below that → stays in the inbox.
4. `needs_action` ≥ 0.80, or a category with `flag = true` → flagged (★).
5. Mails are read with `BODY.PEEK` – unread stays unread.

Folders are created automatically on the first live run
(`INBOX/Finanzen` → `INBOX.Finanzen` if Strato uses `.` as separator;
`--check` shows which).

## Renaming categories or folders

Change `config.toml`, then bring the log (and the server) in line – otherwise
the sorter creates a new empty folder and old mail stays under the old name.
Stop any running sorter first, run without `--live` to preview:

```powershell
.venv\Scripts\python -m email_sorter --rename-category newsletter werbung --live
.venv\Scripts\python -m email_sorter --rename-folder INBOX/Newsletter INBOX/Werbung --live
```

`--rename-folder` renames the folder on the IMAP server including subfolders
and mail, moves the subscriptions and updates the log. In Docker:
`docker compose run --rm sortroom python -m email_sorter --rename-folder …`.

## Sender rules

`[[sender_rules]]` entries are checked before Jev, in order; the first whose
`match` is part of the sender address (case-insensitive) decides:

```toml
[[sender_rules]]
match = "frombrotherdevice@brother.com"   # scanner: always stays in the inbox
action = "inbox"

[[sender_rules]]
match = "newsletter@update.lieferando.de"
action = "werbung"                        # any category key
```

`inbox` leaves the mail untouched; a category moves it to that category's
folder without a Jev request (no cost, no flag, no expiry date). Rule-sorted
mail is logged with source `rule`. Applies to normal runs, backfills and
re-sorts; the 24-hour wait still applies. The older
`rules.keep_in_inbox_from = [...]` list still works and counts as `inbox` rules.

## Run log

Every run, backfill, re-sort and expiry recheck is written to the `runs`
table of the mailbox's `state.db` (counts, cost, categories, errors, also
for dry runs and crashes); entries older than 180 days are dropped.

## Re-sorting a folder

After changing categories, mail that is already sorted stays where it is.
To sort one folder again with the current categories:

```powershell
.venv\Scripts\python -m email_sorter --resort-folder INBOX/Reisen            # dry run, CSV report
.venv\Scripts\python -m email_sorter --resort-folder INBOX/Reisen --live     # move
```

Only mails Jev assigns confidently to a category with a *different* folder
are moved; uncertain mails and mails of inbox categories (e.g. sicherheit)
stay. Flags are not changed. `--limit N` re-sorts only the newest N mails.
The expired-offers folder is refused. Re-sorting `INBOX` leaves mail
younger than `min_age_hours` alone, like normal runs. In Docker:
`docker compose run --rm sortroom python -m email_sorter --resort-folder INBOX/Reisen --live`.

## Expired offers

For categories with `track_expiry = true` (Werbung), Jev also answers
whether the mail is a time-limited offer and roughly when it ends (same day,
1–2 days, a week, a month). An explicit deadline in the text ("gültig bis
30.09.", "endet am 1. Oktober", "ends October 3rd") takes precedence.

Every live run moves mails whose last valid day has passed to
`expired_folder` (`INBOX/Werbung/Abgelaufen`), so they can be deleted in one
go. Without an `expired_folder` the dates are only recorded. Mails you deleted
or moved meanwhile are skipped. (IMAP keywords were dropped in 1.2: no common
client showed them reliably.)

Mails sorted before this feature existed can be checked once:

```powershell
.venv\Scripts\python -m email_sorter --recheck-expiry          # find dates only
.venv\Scripts\python -m email_sorter --recheck-expiry --live   # and move expired ones
```


## Categories (`config.toml`)

| Key | Folder | Notes |
|---|---|---|
| finanzen | INBOX/Finanzen | routine bills, receipts, statements |
| bestellungen | INBOX/Bestellungen | |
| reisen | INBOX/Reisen | travel only |
| termine | INBOX/Termine | booked events and appointments |
| unterlagen | INBOX/Unterlagen | documents to keep for years: contracts, official letters, tax certificates |
| werbung | INBOX/Werbung | expired offers → Werbung/Abgelaufen |
| benachrichtigungen | INBOX/Benachrichtigungen | |
| portal | – (inbox) | "new document in your customer portal" notices |
| persoenlich | – (inbox) | |
| sicherheit | – (inbox) | always flagged |
| verdaechtig | INBOX/Verdächtig | phishing/scams, never flagged |
| sonstiges | – (inbox) | |

Add, rename or remove categories freely – the `description` is what Jev
reads, so write it like you'd explain the folder to a person.

## Running automatically

Sortroom starts the runs itself: every mailbox has a schedule under
Einstellungen → Zeitplan (`[schedule]` in `mailbox.toml`, on by default,
every 10 minutes):

```toml
[schedule]
enabled = true
interval_minutes = 10
```

The first run comes a minute after the container starts. A run that finds
the mailbox busy (manual run, backfill, maintenance job) is skipped and comes
again at the next slot. The overview shows when the next run is due; changes
apply without a restart. `SORTROOM_SCHEDULER=off` in the environment switches
the schedule off for the whole process, e.g. for a second container that
should only serve the UI.

Without the container, run `python -m email_sorter --live` periodically with
any scheduler; overlapping runs of a mailbox are skipped via its
`data/run.lock`.

## Files

| Path | What |
|---|---|
| `config/config.toml` | shared settings (`[jev]`); in a single-file setup also the mailbox |
| `mailboxes/<id>/mailbox.toml` | a mailbox's settings (IMAP, rules, categories, schedule) |
| `mailboxes/<id>/data/state.db` | SQLite log of every processed mail (category, confidence, cost) and the run log |
| `mailboxes/<id>/reports/dry-run-*.csv` | dry-run results incl. runner-up category |
| `logs/sortroom.log` | every run (rotating, 5 × 1 MB) |
| `data/state.db`, `reports/` | the same for a single-file setup (no `mailboxes/`) |

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

## Caveats

- Jev is in beta. If the API shape changes, only `build_request()` / `parse_response()` in
  `email_sorter/jev.py` need updating.
- Mail content (first 3000 chars) is sent to the gateway (OpenRouter/Vercel) and TypeSafe.
- Switching gateway (OpenRouter ↔ Vercel) only needs `endpoint`, `model`
  and `api_key_env` in `config.toml` – see the commented alternative there.
- Exit codes: `0` ok, `1` some mails failed (retried next run), `2` config or
  API-key/credit problem.

## License

Copyright (C) 2026 Matthias Hönes

Sortroom is free software: you can redistribute it and/or modify it under the
terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. It is distributed in the hope that it will be useful, but
WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE. See [LICENSE](LICENSE) for the full text.

Because the admin UI is a network service, the AGPL asks anyone who runs a
**modified** version for other people to offer those users its source code.
The sidebar links to this repository; if you change Sortroom and let others
use your instance, point that link at your own source.
