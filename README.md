# Sortroom

Sorts an IMAP inbox into folders using a classification model behind the
[TypeSafe API](https://docs.typesafe.ai/api) – TypeSafe itself, OpenRouter's decisions API
or any other service that speaks it. The model doesn't generate text – it picks one of your
categories and returns probabilities, so it can never invent a folder.

For every new mail it asks the model these questions in one request:

| Question | Type | Used for |
|---|---|---|
| Which category? | `choice` | moving the mail into its folder |
| Does it need action from me? | `noul` (yes/no probability) | flagging the mail |

Cost is roughly $0.00002 per mail (input tokens only; output is free).

## Admin UI

The container serves a browser UI next to the API: `http://<docker-host>:8765`
(port via `UI_PORT`). Log in with `ADMIN_PASSWORD` from `.env` (at least 8
characters; without it the UI stays locked). Sessions last 7 days; failed
logins are slowed down. Behind an HTTPS reverse proxy set `UI_SECURE_COOKIES=1`.

The UI speaks English and German, set for everybody under **Global settings** →
Language (`[ui] language = "de"` in `config/config.toml`; default `en`).

- **All mailboxes** – every mailbox with status, today's count, uncertain
  mail and this month's cost
- **Overview** (per mailbox) – last run, distribution of the last 7 days,
  run log, uncertain mail
- **Mails** – everything sorted, with search and filters (category, folder,
  period, uncertain, flagged) and a detail panel

- **Categories** – edit name, description, folder, star/expiry switches and a
  per-category folder for expired offers; add or delete categories.
  **Test with the model** classifies the category's last 10 mails and 15 others with
  the draft description (read-only, nothing is moved; about $0.002) and shows
  what would change
- **Settings** – display name, IMAP server/port/inbox, thresholds, waiting
  time, look-back, max per run, default folder for expired offers, and the
  sender rules table

Edits are written to the mailbox's `mailbox.toml` (comments are kept), checked
exactly like the sorter loads them, and the previous version is kept as
`mailbox.toml.bak`. They apply from the next run. A file the container can't
write is shown read-only – `./mailboxes` and `./config` must be writable for
uid 1000. Forms carry a CSRF token.

- **Maintenance** – start a run, backfill, re-sort a folder, move a category into
  its new folder, rename a folder (server, log and settings) or a category key,
  recheck expiry dates, reconcile the log with the mailbox (marks mails you
  deleted as "no longer in the mailbox" so they leave the review list; notes
  uncertain mails you filed by hand; reads only, trash/spam/sent/drafts don't
  count), check the connection. Each job runs in the background
  under the mailbox lock ("Dry run" changes nothing, "Run" is the real thing) and shows
  its log; also shows whether the mailbox's .env variables are set
- **Mails** detail – accept the model's suggestion for an uncertain mail, move a mail
  to another category (logged as "by hand"), or create a sender rule from it
- **Add mailbox** – creates `mailboxes/<id>/mailbox.toml` with the
  categories, thresholds and schedule of an existing mailbox or the built-in
  standard categories in English or German (`email_sorter/example_mailbox.en.toml`,
  `example_mailbox.de.toml`); credentials go
  into `.env` under the variable names you choose (restart the container
  afterwards). This is also how the first mailbox is created. For Gmail (`imap.gmail.com`) the
  copied folders lose their `INBOX/` prefix: Gmail only has top-level labels
  (`Promotions`, not `INBOX/Promotions`); Outlook still shows them under the inbox
- **Global settings** (bottom of the sidebar) – UI language and the shared `[classifier]` settings
  (endpoint, model, text length) in `config/config.toml`

Jobs are kept in memory until the container restarts; backfills started via
`POST /backfill` show up there too. The API endpoints keep their bearer-token
auth.

## Several mailboxes

Each mailbox lives in its own folder with its own settings, log and reports:

```
config/config.toml           # shared: [classifier] (endpoint, model, text length)
mailboxes/
  privat/
    mailbox.toml             # name, [imap], [rules], [schedule], [[sender_rules]], [categories.*]
    data/state.db, data/run.lock
    reports/
  gmail/
    mailbox.toml
    ...
```

The folder name is the mailbox's id (for `--mailbox`, the API and the UI addresses) and
follows its display name: "mh@hoenes.de" lives in `mailboxes/mh-hoenes-de/`, "Büro" in
`mailboxes/buero/` (`-2` etc. when taken). Renaming a mailbox under Settings moves its
folder too – refused while a run or job is active – so scripts using the old id need the new
one. A folder renamed by hand also works; the id is always the folder name.

`mailbox.toml` starts with `name = "Privat"`; in `[imap]`, `user_env` and
`password_env` name the `.env` variables with that mailbox's login (default
`IMAP_USER` / `IMAP_PASSWORD`), e.g. `GMAIL_USER` / `GMAIL_PASSWORD`.

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
.venv\Scripts\python -m pip install -r requirements-api.txt
copy .env.example .env      # then fill in IMAP_USER, IMAP_PASSWORD, CLASSIFIER_API_KEY, ADMIN_PASSWORD
.venv\Scripts\python -m uvicorn email_sorter.api:app --port 8765
```

Open `http://localhost:8765`, log in and create the first mailbox under
"Add mailbox". The CLI below works on the same `mailboxes/`.

## Usage

```powershell
.venv\Scripts\python -m email_sorter --check      # test login + model, list target folders
.venv\Scripts\python -m email_sorter              # DRY RUN: classify, write reports\dry-run-*.csv, change nothing
.venv\Scripts\python -m email_sorter --live       # move + flag for real
```

Options: `--limit N` (classify at most N mails), `-v` (log every decision),
`--config other.toml`.

Fresh mail waits `min_age_hours` in the inbox before it is sorted. With
`sort_read_at_once = true` in `[rules]` (Settings → "Sort read mails
at once"), mail you have already read is sorted at the next run anyway.

**Recommended rollout:** run dry runs for a few days, open the CSV reports
(they open directly in German Excel), sharpen category descriptions on the
Categories page where the model got it wrong, then go live.

Dry runs don't record anything, so each one re-classifies the same mails
(a fraction of a cent) – that's intended, so you can compare after tweaking.

## Sorting older mail (manual backfill)

Normal and scheduled runs only look at the last `lookback_days`. Older mail is
sorted only when you start it yourself (CLI, Maintenance page or `POST /backfill`) –
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
2. `mkdir -p config logs mailboxes && sudo chown -R 1000:1000 config logs mailboxes`
   (the container runs as uid 1000), then copy `config/config.toml` of this
   repository into `config/`.
3. Create `.env` (see `.env.example`): IMAP, `CLASSIFIER_API_KEY` and
   `ADMIN_PASSWORD`; `API_TOKEN` only if you want to use the HTTP API.
4. `docker compose pull && docker compose up -d`, open the UI and create the
   first mailbox under "Add mailbox". Check it with Maintenance →
   "Check connection", or `docker compose logs -f sortroom`.

**Don't run two sorters on the same mailbox** (e.g. the container and a local
copy): each installation has its own `state.db` and they would not know about
each other's work.

## How it decides

1. Looks at mails in `INBOX` from the last `lookback_days` (default 7) that
   aren't in the mailbox's `data/state.db` yet and arrived at least `min_age_hours` ago
   (default 24, by the server's arrival time) – newer mail stays in the
   inbox for a day and is picked up by a later run.
2. Sends sender, recipient, subject, attachment names, whether it's a mailing
   list, and the cleaned body (first 3000 chars, quotes/signatures removed).
3. Confidence ≥ `min_confidence` (0.70) → moved to the category's folder.
   Below that → stays in the inbox.
4. `needs_action` ≥ 0.80, or a category with `flag = true` → flagged (★).
5. Mails are read with `BODY.PEEK` – unread stays unread.

Folders are created automatically on the first live run
(`INBOX/Finanzen` → `INBOX.Finanzen` if the server uses `.` as separator;
`--check` shows which).

## Renaming categories or folders

Easiest on the Maintenance page ("Rename a folder", "Rename a category
key"), which also updates the settings. On the command line, change
`mailbox.toml`, then bring the log (and the server) in line – otherwise
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

`[[sender_rules]]` entries are checked before the model, in order; the first whose
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
folder without a model request (no cost, no flag, no expiry date). Rule-sorted
mail is logged with source `rule`. Applies to normal runs, backfills and
re-sorts; the 24-hour wait still applies. The rules can be edited under
Settings or created from a mail on the Mails page.

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

Only mails the model assigns confidently to a category with a *different* folder
are moved; uncertain mails and mails of inbox categories (e.g. sicherheit)
stay. Flags are not changed. `--limit N` re-sorts only the newest N mails.
The expired-offers folder is refused. Re-sorting `INBOX` leaves mail
younger than `min_age_hours` alone, like normal runs. In Docker:
`docker compose run --rm sortroom python -m email_sorter --resort-folder INBOX/Reisen --live`.

## Expired offers

For categories with `track_expiry = true` (Werbung), the model also answers
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


## Standard categories

"Add mailbox" offers the same twelve categories in English (`email_sorter/example_mailbox.en.toml`)
and German (`example_mailbox.de.toml`); only keys, folders and the mailbox's default name differ.
The English names follow Gmail's tabs where they overlap (Purchases, Promotions, Updates).
The descriptions the model reads are English in both.

| Key (en / de) | Folder (en / de) | Notes |
|---|---|---|
| finance / finanzen | INBOX/Finance / INBOX/Finanzen | routine bills, receipts, statements |
| purchases / bestellungen | INBOX/Purchases / INBOX/Bestellungen | |
| travel / reisen | INBOX/Travel / INBOX/Reisen | travel only |
| appointments / termine | INBOX/Appointments / INBOX/Termine | booked events and appointments |
| documents / unterlagen | INBOX/Documents / INBOX/Unterlagen | documents to keep for years: contracts, official letters, tax certificates |
| promotions / werbung | INBOX/Promotions / INBOX/Werbung | expired offers → …/Expired, …/Abgelaufen |
| updates / benachrichtigungen | INBOX/Updates / INBOX/Benachrichtigungen | |
| portal | same folder as updates / benachrichtigungen | "new document in your customer portal" notices |
| personal / persoenlich | – (inbox) | |
| security / sicherheit | – (inbox) | always flagged |
| suspicious / verdaechtig | INBOX/Suspicious / INBOX/Verdächtig | phishing/scams, never flagged |
| other / sonstiges | – (inbox) | |

Add, rename or remove categories freely – the `description` is what the model
reads, so write it like you'd explain the folder to a person.

## Running automatically

Sortroom starts the runs itself: every mailbox has a schedule under
Settings → Schedule (`[schedule]` in `mailbox.toml`, on by default,
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
| `config/config.toml` | shared settings (`[classifier]`) |
| `mailboxes/<id>/mailbox.toml` | a mailbox's settings (IMAP, rules, categories, schedule) |
| `mailboxes/<id>/data/state.db` | SQLite log of every processed mail (category, confidence, cost) and the run log |
| `mailboxes/<id>/reports/dry-run-*.csv` | dry-run results incl. runner-up category |
| `logs/sortroom.log` | every run (rotating, 5 × 1 MB) |

## Translations

UI texts are written in English in the templates (`{{ _('…') }}`) and in the Python code
(`_("…")` from `email_sorter/i18n.py`); each further language has a catalog
`email_sorter/locale/<code>.json` that maps the English text to its translation
(`[singular, plural]` for `ngettext`). `python tools/i18n_check.py` lists texts missing from a
catalog; the tests fail on a missing one. A new language needs a catalog, an entry in
`i18n.LANGUAGES` and, if wanted, its own `example_mailbox.<code>.toml`.

## Tests

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```

## Caveats

- The wire format is the TypeSafe API. If it changes, only `build_request()` /
  `parse_response()` in `email_sorter/classifier.py` need updating.
- Mail content (first 3000 chars) is sent to the configured endpoint and whoever runs the
  model behind it.
- Switching provider only needs `endpoint` and `model` in `config.toml` (or under Global
  settings) and its key in `CLASSIFIER_API_KEY`. The cost per mail is shown when the
  provider reports it in `usage.cost` (OpenRouter does); otherwise it stays at 0.
- Exit codes: `0` ok, `1` some mails failed (retried next run), `2` config or
  API-key/credit problem.

## Upgrading from 0.7.x

The model settings are no longer named after one model, and there is only one key:

1. In `config/config.toml` rename `[jev]` to `[classifier]` and delete its `api_key_env` line.
2. In `.env` rename the key variable (e.g. `OPENROUTER_API_KEY`) to `CLASSIFIER_API_KEY`.
3. Restart the container. Until step 1 is done it refuses to start with a message saying
   exactly this. The mail log in `state.db` is updated by itself.

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
