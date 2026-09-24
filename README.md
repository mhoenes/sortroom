# email-sorter

Sorts a Strato IMAP inbox into folders using **Jev** (TypeSafe's "System One"
decision model) through **OpenRouter** (or Vercel AI Gateway). Jev doesn't generate text – it picks one
of your categories and returns probabilities, so it can never invent a folder.

For every new mail it asks Jev two questions in one request:

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

- **Alle Postfächer** – every mailbox with status, today's count, uncertain
  mail and this month's cost
- **Übersicht** (per mailbox) – last run, distribution of the last 7 days,
  run log, uncertain mail
- **Mails** – everything sorted, with search and filters (category, folder,
  period, uncertain, flagged) and a detail panel

The pages only read each mailbox's `state.db`; they never touch IMAP or Jev.
Editing categories and settings, maintenance jobs and correcting single mails
follow in later versions. The API endpoints keep their bearer-token auth.

## Several mailboxes

Each mailbox lives in its own folder with its own settings, log and reports:

```
config.toml                  # shared: [jev] (gateway, model, key name, text length)
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
cd C:\Projekte\email-sorter
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

**Recommended rollout:** run dry runs for a few days, open the CSV reports
(they open directly in German Excel), sharpen category descriptions in
`config.toml` where Jev got it wrong, then go live.

Dry runs don't record anything, so each one re-classifies the same mails
(a fraction of a cent) – that's intended, so you can compare after tweaking.

## Sorting older mail (manual backfill)

Normal and scheduled runs only look at the last `lookback_days`. Older mail is
sorted only when you start it yourself – the scheduled task never passes
`--since`:

```powershell
.venv\Scripts\python -m email_sorter --since 2026-01-01 --limit 100   # dry run, report only
.venv\Scripts\python -m email_sorter --since 2026-01-01 --live        # sort for real
```

It works month by month, newest first, in batches of `max_per_run`. Live
batches are applied immediately, so you can stop it (Ctrl+C) and start it
again later – it continues where it left off. While it runs, scheduled runs
are skipped (shared lock). Old offers whose deadline has passed go straight
to `Werbung/Abgelaufen`.

## Running in Docker next to n8n

The sorter can run as its own container with a small HTTP API that n8n calls.
The container publishes no port; only containers on the n8n Docker network
reach it at `http://email-sorter:8765`.

| Endpoint | What |
|---|---|
| `POST /run` `{"live": true}` | normal run (last `lookback_days`), returns a JSON summary |
| `POST /backfill` `{"since": "2024-01-01", "live": false}` | backfill in the background, returns a job (202) |
| `GET /jobs/{id}` | status and summary of a backfill job |
| `POST /recheck-expiry` `{"live": true}` | find expiry dates of already sorted offers |
| `GET /health` | liveness, no auth |

All endpoints except `/health` need `Authorization: Bearer <API_TOKEN>`.
Runs share one lock: while a backfill runs, `/run` answers 409.

### Docker image

GitHub Actions (`.github/workflows/docker.yml`) runs the tests on every push
and publishes the image to GitHub Container Registry for amd64 and arm64:

- `ghcr.io/<owner>/email-sorter:latest` – latest `main`
- `ghcr.io/<owner>/email-sorter:1.2.3` / `:1.2` – version tags `v1.2.3`
- `ghcr.io/<owner>/email-sorter:sha-abc1234` – every published commit

If the repository is private, the package is private too; log in once on the
Docker host with a personal access token that has `read:packages`:
`echo <token> | docker login ghcr.io -u <github-user> --password-stdin`.

### Setup on the Docker host

1. On the host you only need `docker-compose.yml`, `config.toml`, `.env`
   and `data/state.db` – the code comes with the image. Set
   `EMAIL_SORTER_IMAGE=ghcr.io/<owner>/email-sorter:latest` in `.env`.
2. **Move the state over:** copy `data/state.db` from the old machine into
   `data/` on the host – otherwise the sorter doesn't know what it already
   sorted and expiry dates are lost.
3. Create `.env` (see `.env.example`): IMAP, `OPENROUTER_API_KEY`, a new
   `API_TOKEN` and `N8N_NETWORK` (the Docker network of your n8n container:
   `docker inspect <n8n> --format '{{json .NetworkSettings.Networks}}'`).
4. `mkdir -p data logs reports && sudo chown -R 1000:1000 data logs reports`
   (the container runs as uid 1000).
5. `docker compose pull && docker compose up -d`, then check:
   `docker compose logs -f email-sorter` and
   `docker exec email-sorter python -m email_sorter --check`.

### n8n

Import the workflows from `n8n/` (Workflows → Import from File):

- **`email-sorter-laufend.json`** – every 10 minutes `POST /run`; a failed run
  goes to "Fehlermeldung bauen", where you attach your notification node
  (e-mail, Telegram, ntfy …).
- **`email-sorter-backfill.json`** – manual only: set `since`/`live`/`limit`
  in "Parameter", start it, it polls the job every minute until done.

In both, create one credential of type **Header Auth**: name
`Authorization`, value `Bearer <API_TOKEN>`, and select it in the HTTP nodes.
Then activate the 10-minute workflow.

**Don't run two sorters on the same mailbox.** Once the container is live,
remove the Windows scheduled task (`uninstall-task.ps1`) if you installed it –
each installation has its own `state.db` and they would not know about each
other's work.

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
`docker compose run --rm email-sorter python -m email_sorter --rename-folder …`.

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
`docker compose run --rm email-sorter python -m email_sorter --resort-folder INBOX/Reisen --live`.

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

After the dry-run phase, register a Windows scheduled task (every 10 minutes
while you're logged in, live mode):

```powershell
powershell -ExecutionPolicy Bypass -File .\install-task.ps1 -IntervalMinutes 10
powershell -ExecutionPolicy Bypass -File .\uninstall-task.ps1   # to remove it
```

Overlapping runs are skipped via `data/run.lock`.

## Files

| Path | What |
|---|---|
| `logs/email-sorter.log` | every run (rotating, 5 × 1 MB) |
| `reports/dry-run-*.csv` | dry-run results incl. runner-up category |
| `data/state.db` | SQLite log of every processed mail (category, confidence, cost) |

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
